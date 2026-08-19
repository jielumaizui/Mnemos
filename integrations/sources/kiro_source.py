"""Kiro CLI passive sync source.

Kiro stores CLI session events under ``~/.kiro/sessions/cli`` as JSONL files.
Each line is a typed event such as ``Prompt``, ``AssistantMessage``, or
``ToolResults``.  This source keeps the event stream read-only and reconstructs
turns without requiring Kiro-specific hooks.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.sync_framework.agent_source import (
    NativeSourceContractError,
    SessionInfo,
    Turn,
)
from core.ops.durable_io import read_native_bytes
from integrations.sources.base import (
    BaseAgentSource,
    native_mapping_residual_ref,
    native_path_kind,
    stable_path_session_id,
)


def _kiro_sidecar_ref(path: Path) -> Dict[str, Any]:
    """Preserve one declared JSON/history sidecar without path disclosure."""

    try:
        raw = read_native_bytes(path)
    except OSError:
        raise NativeSourceContractError(
            "native_kiro_sidecar_read_failed"
        ) from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "event_type": "native_sidecar",
            "artifact_kind": path.suffix.lstrip("."),
            "raw_base64": base64.b64encode(raw).decode("ascii"),
            "raw_encoding": "base64",
            "decode_error": "invalid_utf8",
        }
    if path.suffix == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError:
            return {
                "event_type": "native_sidecar",
                "artifact_kind": "json",
                "raw": text,
                "decode_error": "invalid_json",
            }
        return {
            "event_type": "native_sidecar",
            "artifact_kind": "json",
            "raw_text": text,
            "raw_encoding": "utf-8",
        }
    return {
        "event_type": "native_sidecar",
        "artifact_kind": "history",
        "raw": text,
    }


def _timestamp(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        epoch = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    return str(value)


def _event_data(event: Dict[str, Any]) -> Dict[str, Any]:
    data = event.get("data", {})
    return data if isinstance(data, dict) else {}


def _event_timestamp(event: Dict[str, Any]) -> Optional[str]:
    data = _event_data(event)
    meta = data.get("meta", {})
    if isinstance(meta, dict):
        for key in ("timestamp", "created_at", "createdAt", "time", "ts"):
            stamp = _timestamp(meta.get(key))
            if stamp:
                return stamp
    for key in ("timestamp", "created_at", "createdAt", "time", "ts"):
        stamp = _timestamp(data.get(key) or event.get(key))
        if stamp:
            return stamp
    return None


def _content_blocks(value: Any) -> List[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        return [{"kind": "text", "data": value}]
    return []


def _extract_blocks(
    blocks: List[Any],
) -> Tuple[
    str,
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    str,
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    List[str],
]:
    texts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    tool_results: List[Dict[str, Any]] = []
    reasoning_parts: List[str] = []
    attachments: List[Dict[str, Any]] = []
    raw_refs: List[Dict[str, Any]] = []
    loss_reasons: List[str] = []

    for block in blocks:
        if not isinstance(block, dict):
            raw_refs.append(
                {
                    "event_type": "non_object_content_block",
                    "raw": block,
                }
            )
            loss_reasons.append("non_object_content_block")
            continue
        kind = str(block.get("kind") or block.get("type") or "")
        kind_lc = kind.lower()
        data = block.get("data")
        if kind_lc == "text":
            texts.append(data if isinstance(data, str) else str(data or ""))
            residual = native_mapping_residual_ref(
                block,
                consumed_keys={"kind", "type", "data"},
                event_type="content_block_residual",
                block_kind=kind or "text",
            )
            if residual is not None:
                raw_refs.append(residual)
        elif kind_lc == "tooluse":
            tool_call = data if isinstance(data, dict) else {"raw": data}
            tool_calls.append(tool_call)
            raw_refs.append({"event_type": "tool_use", "raw": block})
        elif kind_lc == "toolresult":
            tool_result = data if isinstance(data, dict) else {"raw": data}
            tool_results.append(tool_result)
            raw_refs.append({"event_type": "tool_result", "raw": block})
        elif kind_lc in {"thinking", "reasoning", "think"}:
            if isinstance(data, dict):
                value = data.get("text") or data.get("thinking") or data.get("reasoning") or ""
            else:
                value = data
            if value:
                reasoning_parts.append(str(value))
            raw_refs.append({"event_type": "reasoning", "raw": block})
        elif any(token in kind_lc for token in ("file", "image", "media", "attachment")):
            payload = data if isinstance(data, dict) else {"value": data}
            attachments.append(
                {
                    "type": kind or "attachment",
                    "name": payload.get("name") or payload.get("filename") or "",
                    "path": payload.get("path") or "",
                    "url": payload.get("url") or "",
                    "mime_type": payload.get("mime_type") or payload.get("media_type") or "",
                    "raw": block,
                }
            )
            raw_refs.append({"event_type": kind or "attachment", "raw": block})
        elif kind:
            raw_refs.append({"event_type": kind, "raw": block})
            loss_reasons.append(f"unknown_block:{kind}")
        else:
            raw_refs.append({"event_type": "unknown_block", "raw": block})
            loss_reasons.append("unknown_block")

    return (
        "\n\n".join(text for text in texts if text),
        tool_calls,
        tool_results,
        "\n".join(part for part in reasoning_parts if part),
        attachments,
        raw_refs,
        loss_reasons,
    )


def _flush_turn(
    turns: List[Turn],
    *,
    turn_number: int,
    user_content: str,
    assistant_parts: List[str],
    timestamp: Optional[str],
    metadata: Dict[str, Any],
    tool_calls: List[Dict[str, Any]],
    tool_results: List[Dict[str, Any]],
    reasoning_parts: List[str],
    attachments: List[Dict[str, Any]],
    raw_event_refs: List[Dict[str, Any]],
    loss_reasons: List[str],
    source_path: Path,
) -> int:
    assistant_content = "\n\n".join(part for part in assistant_parts if part)
    if not any(
        (
            user_content,
            assistant_content,
            tool_calls,
            tool_results,
            reasoning_parts,
            attachments,
            raw_event_refs,
        )
    ):
        return turn_number

    turns.append(
        Turn(
            turn_number=turn_number,
            user_content=user_content,
            assistant_content=assistant_content,
            timestamp=timestamp,
            metadata=metadata,
            tool_calls=tool_calls,
            tool_results=tool_results,
            reasoning="\n".join(part for part in reasoning_parts if part),
            attachments=attachments,
            raw_event_refs=raw_event_refs,
            source_files=[str(source_path)],
            completeness={
                "visible_text": "full",
                "tool_calls": "full" if tool_calls else "unavailable",
                "tool_results": "full" if tool_results else "unavailable",
                "reasoning": "full" if reasoning_parts else "unavailable",
                "attachments": "full" if attachments else "unavailable",
                "truncated": False,
                "loss_reasons": loss_reasons,
            },
        )
    )
    return turn_number + 1


class KiroSource(BaseAgentSource):
    """Kiro JSONL event source."""

    _cap_tool_calls = True
    _cap_tool_results = True
    _cap_reasoning = True
    _cap_attachments = "available"
    _cap_source_fidelity = "full"
    _cap_memory_scope = "kiro_home_sessions"
    _cap_host_memory_default = "settings_dependent"
    _cap_host_memory_effect = (
        "Kiro memory/settings affect model context; sessions/cli JSONL remains the passive capture source"
    )
    _cap_transcript_kind = "native_cli_jsonl_events"
    _cap_compression = "raw_events_with_related_json_history_files"

    _default_extra_tags = ["source=kiro"]

    @property
    def name(self) -> str:
        return "kiro"

    @property
    def model_tag(self) -> str:
        return "kiro"

    @property
    def data_dir(self) -> Optional[Path]:
        return self._resolve_data_dir_with_env(
            "KIRO_HOME",
            [Path.home() / ".kiro"],
            subdir="sessions/cli",
        )

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        return {
            "type": "watchdog",
            "events": ["modified", "created"],
            "debounce": 3.0,
            "recursive": True,
        }

    def discover_sessions(self) -> List[SessionInfo]:
        base = self.data_dir
        if base is None:
            return []
        try:
            if native_path_kind(base) == "missing":
                return []
            grouped: Dict[str, Dict[str, Path]] = {}
            for path in base.iterdir():
                if native_path_kind(path) != "file" or path.suffix not in {
                    ".jsonl",
                    ".json",
                    ".history",
                }:
                    continue
                grouped.setdefault(path.stem, {})[path.suffix] = path
            sessions: List[SessionInfo] = []
            for session_id, paths_by_suffix in grouped.items():
                paths = [
                    paths_by_suffix[suffix]
                    for suffix in (".jsonl", ".json", ".history")
                    if suffix in paths_by_suffix
                ]
                source_path = paths[0]
                canonical_id = stable_path_session_id(
                    "kiro",
                    base,
                    base / session_id,
                    native_id=session_id,
                )
                sessions.append(
                    SessionInfo(
                        session_id=session_id,
                        source_path=source_path,
                        working_dir=str(base),
                        mtime=max(path.stat().st_mtime for path in paths),
                        canonical_session_id=canonical_id,
                        session_aliases=[path.name for path in paths],
                        source_kind=(
                            "cli_jsonl_bundle"
                            if ".jsonl" in paths_by_suffix
                            else "orphan_sidecar_bundle"
                        ),
                        source_paths=list(paths),
                    )
                )
            sessions.sort(
                key=lambda item: (-(item.mtime or 0.0), item.session_id)
            )
            return sessions
        except OSError:
            raise NativeSourceContractError(
                "native_kiro_session_discovery_failed"
            ) from None

    def parse_turns(self, session_path: Path) -> List[Turn]:
        events = (
            self._read_jsonl(session_path)
            if session_path.suffix.lower() == ".jsonl"
            else []
        )
        turns: List[Turn] = []
        user_content = ""
        assistant_parts: List[str] = []
        turn_number = 0
        turn_timestamp: Optional[str] = None
        turn_meta: Dict[str, Any] = {"session_id": session_path.stem}
        turn_tool_calls: List[Dict[str, Any]] = []
        turn_tool_results: List[Dict[str, Any]] = []
        turn_reasoning: List[str] = []
        turn_attachments: List[Dict[str, Any]] = []
        turn_raw_refs: List[Dict[str, Any]] = [
            _kiro_sidecar_ref(sidecar)
            for sidecar in self.native_artifact_paths(
                SessionInfo(session_id=session_path.stem, source_path=session_path)
            )
            if sidecar.suffix != ".jsonl"
        ]
        turn_loss: List[str] = []

        for event in events:
            kind = str(event.get("kind") or "")
            native_data = event.get("data", {})
            data = _event_data(event)
            timestamp = _event_timestamp(event)
            envelope_residual = native_mapping_residual_ref(
                event,
                consumed_keys={"kind", "data"},
                event_type="native_event_envelope_residual",
                native_kind=kind or "unknown_event",
            )
            consumed_data_keys: set[str] = set()
            if isinstance(native_data, dict) and isinstance(
                native_data.get("content"),
                (str, list, dict),
            ):
                consumed_data_keys.add("content")
            if kind == "Prompt":
                consumed_data_keys.add("message_id")
            elif kind == "AssistantMessage":
                consumed_data_keys.add("message_id")
            data_residual = native_mapping_residual_ref(
                native_data,
                consumed_keys=consumed_data_keys,
                event_type="native_event_data_residual",
                native_kind=kind or "unknown_event",
            )
            event_residuals = [
                ref
                for ref in (envelope_residual, data_residual)
                if ref is not None
            ]

            if kind == "Prompt":
                has_open_turn = bool(
                    user_content
                    or assistant_parts
                    or turn_tool_calls
                    or turn_tool_results
                    or turn_reasoning
                    or turn_attachments
                )
                existing_refs = [] if has_open_turn else list(turn_raw_refs)
                existing_loss = [] if has_open_turn else list(turn_loss)
                if has_open_turn:
                    turn_number = _flush_turn(
                        turns,
                        turn_number=turn_number,
                        user_content=user_content,
                        assistant_parts=assistant_parts,
                        timestamp=turn_timestamp,
                        metadata=turn_meta,
                        tool_calls=turn_tool_calls,
                        tool_results=turn_tool_results,
                        reasoning_parts=turn_reasoning,
                        attachments=turn_attachments,
                        raw_event_refs=turn_raw_refs,
                        loss_reasons=turn_loss,
                        source_path=session_path,
                    )
                text, calls, results, reasoning, attachments, raw_refs, loss = _extract_blocks(
                    _content_blocks(data.get("content"))
                )
                user_content = text
                assistant_parts = []
                turn_timestamp = timestamp
                turn_meta = {
                    "session_id": session_path.stem,
                    "prompt_message_id": data.get("message_id"),
                }
                turn_tool_calls = calls
                turn_tool_results = results
                turn_reasoning = [reasoning] if reasoning else []
                turn_attachments = attachments
                turn_raw_refs = existing_refs + event_residuals + raw_refs
                turn_loss = existing_loss + loss
            elif kind == "AssistantMessage":
                text, calls, results, reasoning, attachments, raw_refs, loss = _extract_blocks(
                    _content_blocks(data.get("content"))
                )
                if text:
                    assistant_parts.append(text)
                if timestamp and turn_timestamp is None:
                    turn_timestamp = timestamp
                if data.get("message_id"):
                    turn_meta.setdefault("assistant_message_ids", []).append(data.get("message_id"))
                turn_tool_calls.extend(calls)
                turn_tool_results.extend(results)
                if reasoning:
                    turn_reasoning.append(reasoning)
                turn_attachments.extend(attachments)
                turn_raw_refs.extend(event_residuals)
                turn_raw_refs.extend(raw_refs)
                turn_loss.extend(loss)
            elif kind == "ToolResults":
                text, calls, results, reasoning, attachments, raw_refs, loss = _extract_blocks(
                    _content_blocks(data.get("content"))
                )
                if text:
                    assistant_parts.append(text)
                turn_tool_calls.extend(calls)
                turn_tool_results.extend(results)
                if reasoning:
                    turn_reasoning.append(reasoning)
                turn_attachments.extend(attachments)
                turn_raw_refs.extend(event_residuals)
                turn_raw_refs.extend(raw_refs)
                turn_loss.extend(loss)
            else:
                turn_raw_refs.append({"event_type": kind or "unknown_event", "raw": event})
                turn_loss.append(f"unknown_event:{kind or 'unknown'}")

        _flush_turn(
            turns,
            turn_number=turn_number,
            user_content=user_content,
            assistant_parts=assistant_parts,
            timestamp=turn_timestamp,
            metadata=turn_meta,
            tool_calls=turn_tool_calls,
            tool_results=turn_tool_results,
            reasoning_parts=turn_reasoning,
            attachments=turn_attachments,
            raw_event_refs=turn_raw_refs,
            loss_reasons=turn_loss,
            source_path=session_path,
        )
        return turns

    def native_artifact_paths(self, session_info: SessionInfo) -> List[Path]:
        """Declare the JSONL transcript and every existing native sidecar."""

        paths = list(session_info.source_paths or [])
        if not paths:
            for suffix in (".jsonl", ".json", ".history"):
                sibling = session_info.source_path.with_suffix(suffix)
                if native_path_kind(sibling) != "missing":
                    paths.append(sibling)
        if not paths or any(native_path_kind(path) != "file" for path in paths):
            raise NativeSourceContractError(
                "native_kiro_artifact_set_incomplete"
            )
        return paths

    def get_session_state(self, session_info: SessionInfo) -> Optional[Dict[str, Any]]:
        return self._compute_session_state(
            self.native_artifact_paths(session_info)
        )
