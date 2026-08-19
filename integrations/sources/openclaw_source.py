# -*- coding: utf-8 -*-
"""
OpenClawSource — OpenClaw Agent 同步插件

实现 AgentSource 接口，接入 SyncFramework。
OpenClaw 原生会话可同时位于 trajectory JSONL、普通 session JSONL 和每日 corpus。
所有声明格式共同进入发现分母；只有内容等价或可证明的内容扩展才收敛。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
from core.config import get_config
from core.sync_framework.agent_source import (
    NativeSourceContractError,
    SessionInfo,
    Turn,
)
from core.ops.durable_io import read_native_bytes

from integrations.sources.base import BaseAgentSource, native_path_kind
from integrations.sources.openclaw_identity import (
    OpenClawSessionCandidate as _OpenClawSessionCandidate,
    artifact_id,
    corpus_timestamp,
    is_turn_prefix,
    path_session_id,
    source_kind_for_path,
    turn_fingerprint,
)
from integrations.sources.openclaw_payload import (
    content_to_text_attachments_and_refs,
    dedupe_raw_refs,
    dict_items,
    invalid_present_ref,
    malformed_collection_refs,
    messages_snapshot_to_text,
    read_native_jsonl,
    session_identity_refs,
    tool_alias_conflict_refs,
)

logger = logging.getLogger(__name__)

# 语料行格式：[path#Lline] Role: content
SESSION_LINE_RE = re.compile(
    r"^\[(?P<path>[^\]]+)#L(?P<line>\d+)\]\s+(?P<role>User|Assistant):\s*(?P<content>.*)$"
)


class OpenClawSource(BaseAgentSource):  # noqa: Vulture - loaded by SourceRegistry builtin reflection.
    """OpenClaw 数据源插件"""

    _cap_tool_calls = "available"
    _cap_tool_results = "available"
    _cap_reasoning = "available"
    _cap_attachments = "available"
    _cap_source_fidelity = "full"
    _cap_memory_scope = "openclaw_memory_plugins_plus_native_session_artifacts"
    _cap_host_memory_default = "plugin_dependent"
    _cap_host_memory_effect = (
        "OpenClaw memory plugins affect prompts; all declared session artifacts remain passive capture sources"
    )
    _cap_transcript_kind = "native_trajectory_normal_jsonl_and_corpus"
    _cap_compression = "trajectory_artifacts_may_redact_deep_config_not_conversation_turns"

    _default_extra_tags = ["source_fidelity=full"]

    @property
    def name(self) -> str:
        return "openclaw"

    @property
    def model_tag(self) -> str:
        return "openclaw"

    @property
    def data_dir(self) -> Optional[Path]:
        config = get_config()
        env = config.get("integrations.openclaw.state_dir")
        candidates = [Path(env).expanduser()] if env else []
        candidates.append(Path.home() / ".openclaw")
        return self._resolve_data_dir(candidates)

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        return {
            "type": "polling",
            "interval": 3600,
            "recursive": True,
        }

    def discover_sessions(self) -> List[SessionInfo]:
        """Discover every declared native format before canonical selection."""
        base = self.data_dir
        if not base:
            return []

        candidates = self._discover_jsonl_candidates(base)
        candidates.extend(self._discover_corpus_candidates(base))
        return self._canonicalize_candidates(candidates)

    def _format_variants(self) -> tuple[Mapping[str, Any], ...]:
        """Read OpenClaw format ownership, priority, and identity from the manifest."""
        resolution = get_agent_source_support_manifest().source(self.name).native[
            "format_resolution"
        ]
        variants = resolution["variants"]
        return tuple(variant for variant in variants if isinstance(variant, Mapping))

    def _format_priority(self, source_kind: str) -> int:
        for variant in self._format_variants():
            if variant.get("source_kind") == source_kind:
                return int(variant["priority"])
        raise ValueError(f"OpenClaw source kind is absent from the manifest: {source_kind}")

    def _discover_jsonl_candidates(self, base: Path) -> List[_OpenClawSessionCandidate]:
        """Enumerate trajectory and ordinary session JSONL artifacts together."""
        candidates: List[_OpenClawSessionCandidate] = []
        for variant in self._format_variants():
            source_kind = str(variant["source_kind"])
            if source_kind == "corpus":
                continue
            exclude_suffix = str(variant.get("exclude_suffix") or "")
            for path in sorted(base.glob(str(variant["path_glob"]))):
                if native_path_kind(path) != "file" or (
                    exclude_suffix and path.name.endswith(exclude_suffix)
                ):
                    continue
                candidate = self._candidate_from_jsonl(path, source_kind)
                if candidate is not None:
                    candidates.append(candidate)
        return candidates

    def _discover_corpus_candidates(self, base: Path) -> List[_OpenClawSessionCandidate]:
        """Split daily corpus artifacts by their native embedded session identity."""
        candidates: List[_OpenClawSessionCandidate] = []
        for variant in self._format_variants():
            if variant.get("source_kind") != "corpus":
                continue
            for corpus_path in sorted(base.glob(str(variant["path_glob"]))):
                if native_path_kind(corpus_path) != "file":
                    continue
                parsed_sessions = self._parse_corpus(corpus_path)
                if not parsed_sessions:
                    candidate = self._candidate_from_turns(
                        source_path=corpus_path,
                        native_session_id=corpus_path.stem,
                        source_kind="corpus",
                        turns=[],
                    )
                    if candidate is not None:
                        candidates.append(candidate)
                for native_session_id, messages in sorted(parsed_sessions.items()):
                    turns = self._pair_messages(
                        messages,
                        native_session_id,
                        source_file=str(corpus_path),
                        timestamp=corpus_timestamp(corpus_path),
                    )
                    candidate = self._candidate_from_turns(
                        source_path=corpus_path,
                        native_session_id=native_session_id,
                        source_kind="corpus",
                        turns=turns,
                    )
                    if candidate is not None:
                        candidates.append(candidate)
        return candidates

    def _candidate_from_jsonl(
        self,
        path: Path,
        source_kind: str,
    ) -> Optional[_OpenClawSessionCandidate]:
        events = read_native_jsonl(path)
        native_session_id = self._native_session_id(path, events)
        turns = self._parse_jsonl_session(
            path,
            native_session_id,
            events=events,
            source_kind=source_kind,
        )
        return self._candidate_from_turns(
            source_path=path,
            native_session_id=native_session_id,
            source_kind=source_kind,
            turns=turns,
        )

    def _candidate_from_turns(
        self,
        *,
        source_path: Path,
        native_session_id: str,
        source_kind: str,
        turns: List[Turn],
    ) -> Optional[_OpenClawSessionCandidate]:
        try:
            mtime = source_path.stat().st_mtime
        except OSError:
            raise NativeSourceContractError(
                "native_openclaw_artifact_stat_failed"
            ) from None
        fingerprints = tuple(turn_fingerprint(turn) for turn in turns)
        content_hash = hashlib.sha256("\n".join(fingerprints).encode("utf-8")).hexdigest()
        artifact_identity = artifact_id(source_path, source_kind)
        return _OpenClawSessionCandidate(
            source_path=source_path,
            native_session_id=native_session_id,
            source_kind=source_kind,
            artifact_id=artifact_identity,
            content_hash=content_hash,
            turn_fingerprints=fingerprints,
            turn_count=len(turns),
            mtime=mtime,
        )

    def _preferred_candidate(
        self,
        candidates: List[_OpenClawSessionCandidate],
    ) -> _OpenClawSessionCandidate:
        return max(
            candidates,
            key=lambda item: (self._format_priority(item.source_kind), item.artifact_id),
        )

    def _canonicalize_candidates(
        self,
        candidates: List[_OpenClawSessionCandidate],
    ) -> List[SessionInfo]:
        """Merge proven duplicate formats and preserve unprovable divergence."""
        by_native: Dict[str, List[_OpenClawSessionCandidate]] = {}
        for candidate in candidates:
            by_native.setdefault(candidate.native_session_id, []).append(candidate)
        sessions: List[SessionInfo] = []
        for native_session_id, same_native in sorted(by_native.items()):
            longest = max(
                (candidate.turn_fingerprints for candidate in same_native),
                key=lambda fingerprints: (len(fingerprints), fingerprints),
            )
            if all(
                is_turn_prefix(candidate.turn_fingerprints, longest)
                for candidate in same_native
            ):
                longest_candidates = [
                    candidate
                    for candidate in same_native
                    if candidate.turn_fingerprints == longest
                ]
                selection = (
                    "identical_content"
                    if all(candidate.turn_fingerprints == longest for candidate in same_native)
                    else "content_extension"
                )
                sessions.append(
                    self._session_info_from_candidates(
                        canonical_session_id=native_session_id,
                        native_session_id=native_session_id,
                        candidates=same_native,
                        representative=self._preferred_candidate(longest_candidates),
                        canonical_selection=selection,
                        identity_mode="native_session_id",
                    )
                )
                continue
            by_content: Dict[str, List[_OpenClawSessionCandidate]] = {}
            for candidate in same_native:
                by_content.setdefault(candidate.content_hash, []).append(candidate)
            all_legacy_ids = sorted(
                {
                    (
                        f"{native_session_id}::artifact::"
                        f"{candidate.artifact_id.removeprefix('openclaw-artifact-')}"
                    )
                    for candidate in same_native
                }
            )
            for matching_content in by_content.values():
                representative = self._preferred_candidate(matching_content)
                digest = representative.artifact_id.removeprefix("openclaw-artifact-")
                sessions.append(
                    self._session_info_from_candidates(
                        canonical_session_id=f"{native_session_id}::artifact::{digest}",
                        native_session_id=native_session_id,
                        candidates=matching_content,
                        representative=representative,
                        canonical_selection="divergent_content",
                        identity_mode="divergent_artifact",
                        extra_aliases=all_legacy_ids,
                        identity_reconciliation_required=True,
                    )
                )
        sessions.sort(key=lambda item: (-(item.mtime or 0.0), item.session_id))
        return sessions

    @staticmethod
    def _session_info_from_candidates(
        *,
        canonical_session_id: str,
        native_session_id: str,
        candidates: List[_OpenClawSessionCandidate],
        representative: _OpenClawSessionCandidate,
        canonical_selection: str,
        identity_mode: str,
        extra_aliases: Optional[List[str]] = None,
        identity_reconciliation_required: bool = False,
    ) -> SessionInfo:
        aliases = list(
            dict.fromkeys(
                [
                    native_session_id,
                    *(extra_aliases or []),
                ]
            )
        )
        metadata: Dict[str, Any] = {
            "native_session_id": native_session_id,
            "native_session_content_hash": representative.content_hash,
            "source_artifact_id": representative.artifact_id,
            "source_artifact_ids": sorted(candidate.artifact_id for candidate in candidates),
            "source_artifact_count": len(candidates),
            "source_formats": sorted({candidate.source_kind for candidate in candidates}),
            "canonical_selection": canonical_selection,
            "canonical_identity_mode": identity_mode,
        }
        if identity_reconciliation_required:
            metadata.update(
                {
                    "identity_contract_version": "openclaw-divergent-artifact-v2",
                    "legacy_canonical_session_ids": sorted(extra_aliases or []),
                    "identity_reconciliation_required": True,
                    "identity_activation_state": "requires_raw_store_reconciliation_check",
                }
            )
        return SessionInfo(
            session_id=canonical_session_id,
            source_path=representative.source_path,
            working_dir=str(representative.source_path.parent),
            mtime=max(candidate.mtime for candidate in candidates),
            canonical_session_id=canonical_session_id,
            session_aliases=aliases,
            source_kind=representative.source_kind,
            metadata=metadata,
            source_paths=[candidate.source_path for candidate in candidates],
        )

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """Parse either declared OpenClaw JSONL artifacts or corpus files."""
        if session_path.name.endswith(".jsonl"):
            return self._parse_jsonl_session(session_path)

        parsed_sessions = self._parse_corpus(session_path)
        all_turns = []
        session_idx = 0
        source_file = str(session_path)
        timestamp = corpus_timestamp(session_path)
        for inner_session_id, messages in parsed_sessions.items():
            turns = self._pair_messages(
                messages,
                inner_session_id,
                base_turn_number=session_idx * 1000,
                source_file=source_file,
                timestamp=timestamp,
            )
            all_turns.extend(turns)
            session_idx += 1
        return all_turns

    def parse_session(self, session_info: SessionInfo) -> List[Turn]:
        """Parse the exact discovered format/session instead of a fallback aggregate."""
        metadata = session_info.metadata or {}
        native_session_id = str(metadata.get("native_session_id") or session_info.session_id)
        if session_info.source_kind == "corpus_fallback":
            return self.parse_turns(session_info.source_path)
        primary_turns = self._parse_declared_artifact(
            session_info.source_path,
            native_session_id,
            session_info.source_kind,
            metadata_present=bool(metadata),
        )
        artifact_paths = list(
            dict.fromkeys(
                [
                    session_info.source_path,
                    *(session_info.source_paths or []),
                ]
            )
        )
        if len(artifact_paths) == 1:
            return primary_turns
        parsed_artifacts: List[tuple[Path, str, List[Turn]]] = []
        for path in artifact_paths:
            source_kind = source_kind_for_path(path)
            turns = (
                primary_turns
                if path == session_info.source_path
                else self._parse_declared_artifact(
                    path,
                    native_session_id,
                    source_kind,
                    metadata_present=True,
                )
            )
            parsed_artifacts.append((path, source_kind, turns))
        for turn_index, primary_turn in enumerate(primary_turns):
            merged_refs: List[Dict[str, Any]] = []
            merged_sources: List[str] = []
            for path, source_kind, turns in parsed_artifacts:
                if turn_index >= len(turns):
                    continue
                candidate_turn = turns[turn_index]
                if turn_fingerprint(candidate_turn) != turn_fingerprint(primary_turn):
                    continue
                artifact_identity = artifact_id(path, source_kind)
                merged_sources.extend(candidate_turn.source_files or [str(path)])
                merged_refs.extend(
                    {
                        **ref,
                        "source_artifact_id": artifact_identity,
                    }
                    for ref in candidate_turn.raw_event_refs
                )
            primary_turn.raw_event_refs = dedupe_raw_refs(merged_refs)
            primary_turn.source_files = list(dict.fromkeys(merged_sources))
        return primary_turns

    def _parse_declared_artifact(
        self,
        path: Path,
        native_session_id: str,
        source_kind: Optional[str],
        *,
        metadata_present: bool,
    ) -> List[Turn]:
        if source_kind == "corpus" or path.suffix == ".txt":
            parsed_sessions = self._parse_corpus(path)
            if not metadata_present and native_session_id not in parsed_sessions:
                return self.parse_turns(path)
            messages = parsed_sessions.get(native_session_id, [])
            return self._pair_messages(
                messages,
                native_session_id,
                source_file=str(path),
                timestamp=corpus_timestamp(path),
            )
        return self._parse_jsonl_session(
            path,
            native_session_id,
            source_kind=source_kind,
        )

    def _native_session_id(self, path: Path, events: List[Dict[str, Any]]) -> str:
        """Prefer recorded native session IDs over an artifact filename fallback."""
        for event in events:
            data = event.get("data")
            payloads: List[Dict[str, Any]] = [event]
            if isinstance(data, dict):
                payloads.append(data)
                payloads.extend(dict_items(data.get("message")))
                payloads.extend(dict_items(data.get("messages")))
            payloads.extend(dict_items(event.get("message")))
            payloads.extend(dict_items(event.get("messages")))
            for payload in payloads:
                for key in ("sessionId", "session_id"):
                    value = payload.get(key)
                    if value:
                        return str(value)
        return path_session_id(path)

    def _parse_jsonl_session(
        self,
        session_path: Path,
        native_session_id: Optional[str] = None,
        *,
        events: Optional[List[Dict[str, Any]]] = None,
        source_kind: Optional[str] = None,
    ) -> List[Turn]:
        """Parse either completed-event trajectories or ordinary role JSONL."""
        parsed_events = (
            events if events is not None else read_native_jsonl(session_path)
        )
        session_id = native_session_id or self._native_session_id(session_path, parsed_events)
        is_trajectory = (
            source_kind == "trajectory"
            or session_path.name.endswith(".trajectory.jsonl")
        )
        if is_trajectory:
            turns = self._parse_trajectory(session_path, events=parsed_events)
            for turn in turns:
                turn.metadata = {**turn.metadata, "session_id": session_id}
            return turns
        return self._parse_normal_jsonl_events(parsed_events, session_path, session_id)

    def _parse_normal_jsonl_events(
        self,
        events: List[Dict[str, Any]],
        session_path: Path,
        native_session_id: str,
    ) -> List[Turn]:
        """Parse ordinary role/content records when no completed trajectory exists."""
        messages: List[Dict[str, Any]] = []
        timestamp = ""
        for event in events:
            preserved_ref = event.get("_mnemos_raw_event_ref")
            if isinstance(preserved_ref, dict):
                messages.append(
                    {
                        "role": "native_raw",
                        "raw_event_refs": [preserved_ref],
                    }
                )
                continue
            candidates: List[Dict[str, Any]] = []
            envelope_refs: List[Dict[str, Any]] = []
            data_timestamp = ""
            if event.get("role") not in (None, ""):
                candidates.append(event)
            elif any(
                event.get(key) not in (None, "")
                for key in ("id", "eventId", "ts", "timestamp")
            ):
                envelope_refs.append(
                    {
                        "event_type": "normal_event_provenance",
                        "raw": {
                            key: event[key]
                            for key in ("id", "eventId", "ts", "timestamp")
                            if event.get(key) not in (None, "")
                        },
                    }
                )
            if not event.get("role"):
                envelope_refs.extend(session_identity_refs(event))
            data = event.get("data")
            if isinstance(data, dict):
                envelope_refs.extend(session_identity_refs(data))
                data_timestamp = str(
                    data.get("ts") or data.get("timestamp") or ""
                )
                data_provenance = {
                    key: data[key]
                    for key in ("id", "eventId", "ts", "timestamp")
                    if data.get(key) not in (None, "")
                }
                if data_provenance:
                    envelope_refs.append(
                        {
                            "event_type": "normal_data_provenance",
                            "raw": data_provenance,
                        }
                    )
                for key in ("message", "messages"):
                    value = data.get(key)
                    candidates.extend(dict_items(value))
                    envelope_refs.extend(
                        malformed_collection_refs(
                            value,
                            f"malformed_data_{key}",
                        )
                    )
                data_residual = {
                    key: value
                    for key, value in data.items()
                    if key
                    not in {
                        "message",
                        "messages",
                        "sessionId",
                        "session_id",
                        "ts",
                        "timestamp",
                        "id",
                        "eventId",
                    }
                }
                if data_residual:
                    envelope_refs.append(
                        {
                            "event_type": "normal_data_envelope_residual",
                            "raw": data_residual,
                        }
                    )
            elif data is not None:
                envelope_refs.append(
                    {
                        "event_type": "malformed_normal_data_envelope",
                        "raw": data,
                    }
                )
            for key in ("message", "messages"):
                value = event.get(key)
                candidates.extend(dict_items(value))
                envelope_refs.extend(
                    malformed_collection_refs(
                        value,
                        f"malformed_event_{key}",
                    )
                )
            if not event.get("role"):
                event_residual = {
                    key: value
                    for key, value in event.items()
                    if key
                    not in {
                        "data",
                        "message",
                        "messages",
                        "sessionId",
                        "session_id",
                        "ts",
                        "timestamp",
                        "id",
                        "eventId",
                    }
                }
                if event_residual:
                    envelope_refs.append(
                        {
                            "event_type": "normal_event_envelope_residual",
                            "raw": event_residual,
                        }
                    )
            if not candidates:
                messages.append(
                    {
                        "role": "native_raw",
                        "raw_event_refs": [
                            {
                                "event_type": "unparsed_normal_event",
                                "raw": event,
                            }
                        ],
                    }
                )
                continue
            for candidate_index, message in enumerate(candidates):
                role = str(message.get("role") or "").lower()
                if role not in {"user", "assistant", "tool"}:
                    messages.append(
                        {
                            "role": "native_raw",
                            "raw_event_refs": [
                                *(
                                    envelope_refs
                                    if candidate_index == 0
                                    else []
                                ),
                                {
                                    "event_type": "unparsed_normal_message",
                                    "role": role,
                                    "raw": message,
                                },
                            ],
                        }
                    )
                    continue
                content = message.get("content", message.get("text", ""))
                text, attachments, content_refs = (
                    content_to_text_attachments_and_refs(content)
                )
                if (
                    "content" in message
                    and "text" in message
                    and message["content"] != message["text"]
                ):
                    content_refs.append(
                        {
                            "event_type": "conflicting_content_fields",
                            "raw": {
                                "content": message["content"],
                                "text": message["text"],
                            },
                        }
                    )
                if not text and isinstance(message.get("text"), str):
                    text = message["text"]
                tool_calls_value = (
                    message["tool_calls"]
                    if "tool_calls" in message
                    else message.get("toolCalls")
                )
                tool_results_value = (
                    message["tool_results"]
                    if "tool_results" in message
                    else message.get("toolResults")
                )
                raw_event_refs = [
                    *(envelope_refs if candidate_index == 0 else []),
                    *content_refs,
                    *malformed_collection_refs(
                        tool_calls_value,
                        "malformed_tool_calls",
                    ),
                    *malformed_collection_refs(
                        tool_results_value,
                        "malformed_tool_results",
                    ),
                ]
                raw_event_refs.extend(session_identity_refs(message))
                raw_event_refs.extend(tool_alias_conflict_refs(message))
                for field_name, field_value, event_type in (
                    ("tool_calls", message.get("tool_calls"), "malformed_tool_calls"),
                    ("toolCalls", message.get("toolCalls"), "malformed_tool_calls"),
                    (
                        "tool_results",
                        message.get("tool_results"),
                        "malformed_tool_results",
                    ),
                    (
                        "toolResults",
                        message.get("toolResults"),
                        "malformed_tool_results",
                    ),
                ):
                    if field_name in message and field_value is None:
                        raw_event_refs.append(
                            {
                                "event_type": event_type,
                                "field": field_name,
                                "raw": None,
                            }
                        )
                message_provenance = {
                    key: message[key]
                    for key in (
                        "id",
                        "messageId",
                        "eventId",
                        "ts",
                        "timestamp",
                    )
                    if message.get(key) not in (None, "")
                }
                if message_provenance:
                    raw_event_refs.append(
                        {
                            "event_type": "normal_message_provenance",
                            "raw": message_provenance,
                        }
                    )
                known_fields = {
                    "role",
                    "content",
                    "text",
                    "tool_calls",
                    "toolCalls",
                    "tool_results",
                    "toolResults",
                    "reasoning",
                    "thinking",
                    "sessionId",
                    "session_id",
                    "ts",
                    "timestamp",
                    "id",
                    "messageId",
                    "eventId",
                }
                residual = {
                    key: value
                    for key, value in message.items()
                    if key not in known_fields
                }
                if residual:
                    raw_event_refs.append(
                        {
                            "event_type": "normal_message_residual",
                            "raw": residual,
                        }
                    )
                reasoning_value = (
                    message["reasoning"]
                    if "reasoning" in message
                    else message.get("thinking")
                )
                reasoning = reasoning_value if isinstance(reasoning_value, str) else ""
                for reasoning_key in ("reasoning", "thinking"):
                    raw_event_refs.extend(
                        invalid_present_ref(
                            message,
                            reasoning_key,
                            lambda value: isinstance(value, str),
                            "malformed_reasoning",
                        )
                    )
                if (
                    "reasoning" in message
                    and "thinking" in message
                    and message["reasoning"] != message["thinking"]
                ):
                    raw_event_refs.append(
                        {
                            "event_type": "conflicting_reasoning_fields",
                            "raw": {
                                "reasoning": message["reasoning"],
                                "thinking": message["thinking"],
                            },
                        }
                    )
                if not (
                    text
                    or attachments
                    or tool_calls_value not in (None, "", [])
                    or tool_results_value not in (None, "", [])
                    or reasoning
                ):
                    raw_event_refs.append(
                        {
                            "event_type": "empty_normal_message",
                            "raw": {
                                key: message[key]
                                for key in (
                                    "role",
                                    "content",
                                    "text",
                                    "tool_calls",
                                    "toolCalls",
                                    "tool_results",
                                    "toolResults",
                                    "reasoning",
                                    "thinking",
                                )
                                if key in message
                            },
                        }
                    )
                if role == "tool" and content not in ("", None, []):
                    raw_event_refs.append(
                        {
                            "event_type": "tool_message_content",
                            "raw": content,
                        }
                    )
                message_timestamp = str(
                    message.get("ts")
                    or message.get("timestamp")
                    or data_timestamp
                    or event.get("ts")
                    or event.get("timestamp")
                    or ""
                )
                messages.append(
                    {
                        "role": role,
                        "content": text,
                        "attachments": attachments,
                        "tool_calls": dict_items(tool_calls_value),
                        "tool_results": dict_items(tool_results_value),
                        "reasoning": reasoning,
                        "raw_event_refs": raw_event_refs,
                        "timestamp": message_timestamp,
                    }
                )
                timestamp = timestamp or message_timestamp
        return self._pair_messages(
            messages,
            native_session_id,
            source_file=str(session_path),
            timestamp=timestamp,
        )

    def _parse_trajectory(
        self,
        trajectory_path: Path,
        *,
        events: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Turn]:
        """Parse OpenClaw native trajectory events."""
        parsed_events = events if events is not None else read_native_jsonl(
            trajectory_path
        )
        if not parsed_events:
            return []
        session_id = trajectory_path.name.removesuffix(".trajectory.jsonl")
        metadata: Dict[str, Any] = {"session_id": session_id}
        tool_calls: List[Dict[str, Any]] = []
        raw_event_refs: List[Dict[str, Any]] = []
        artifacts: Dict[str, Any] = {}
        completed_events: List[tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
        for event in parsed_events:
            preserved_ref = event.get("_mnemos_raw_event_ref")
            if isinstance(preserved_ref, dict):
                raw_event_refs.append(preserved_ref)
                continue
            etype = str(event.get("type") or "")
            raw_data = event.get("data", {})
            data = raw_data
            if not isinstance(raw_data, dict):
                data = {}
                raw_event_refs.append(
                    {
                        "event_type": etype or "malformed_trajectory_event",
                        "raw": event,
                        "parse_error": "non_object_data",
                    }
                )
            envelope_residual = {
                key: value
                for key, value in event.items()
                if key
                not in {
                    "type",
                    "ts",
                    "timestamp",
                    "sessionId",
                    "session_id",
                    "workspaceDir",
                    "provider",
                    "modelId",
                    "modelApi",
                    "runId",
                    "id",
                    "eventId",
                    "data",
                }
            }
            envelope_refs: List[Dict[str, Any]] = []
            envelope_refs.extend(session_identity_refs(event))
            event_provenance = {
                key: event[key]
                for key in (
                    "id",
                    "eventId",
                    "ts",
                    "timestamp",
                )
                if event.get(key) not in (None, "")
            }
            if event_provenance:
                envelope_refs.append(
                    {
                        "event_type": "trajectory_event_provenance",
                        "source_event_type": etype,
                        "raw": event_provenance,
                    }
                )
            if envelope_residual:
                envelope_refs.append(
                    {
                        "event_type": "trajectory_event_envelope_residual",
                        "source_event_type": etype,
                        "raw": envelope_residual,
                    }
                )
            for key in (
                "sessionId",
                "workspaceDir",
                "provider",
                "modelId",
                "modelApi",
                "runId",
            ):
                if key not in event:
                    continue
                value = event[key]
                if isinstance(value, str) and value:
                    metadata.setdefault(key, value)
                    if key == "workspaceDir":
                        raw_event_refs.append(
                            {
                                "event_type": "trajectory_workspace_provenance",
                                "raw": value,
                            }
                        )
                else:
                    raw_event_refs.append(
                        {
                            "event_type": "malformed_trajectory_metadata",
                            "field": key,
                            "raw": value,
                        }
                    )
            if etype == "trace.metadata":
                model = data.get("model")
                if isinstance(model, dict):
                    metadata["model"] = model
                residual = {
                    key: value
                    for key, value in data.items()
                    if key != "model" or not isinstance(model, dict)
                }
                if residual:
                    raw_event_refs.append(
                        {
                            "event_type": "trace_metadata_residual",
                            "raw": residual,
                        }
                    )
                raw_event_refs.extend(envelope_refs)
            elif etype == "model.completed":
                completed_events.append((event, envelope_refs))
            elif etype == "trace.artifacts":
                artifacts = data
                tool_metas = data.get("toolMetas")
                tool_calls.extend(dict_items(tool_metas))
                raw_event_refs.extend(
                    malformed_collection_refs(
                        tool_metas,
                        "malformed_tool_calls",
                    )
                )
                raw_event_refs.extend(
                    invalid_present_ref(
                        data,
                        "toolMetas",
                        lambda value: isinstance(value, (dict, list)),
                        "malformed_tool_calls",
                    )
                )
                residual = {
                    key: value
                    for key, value in data.items()
                    if key not in {"finalStatus", "toolMetas"}
                }
                if residual:
                    raw_event_refs.append(
                        {
                            "event_type": "trace_artifacts_residual",
                            "raw": residual,
                        }
                    )
                raw_event_refs.extend(envelope_refs)
            elif etype:
                raw_event_refs.append({"event_type": etype, "raw": event})
            else:
                raw_event_refs.append(
                    {
                        "event_type": "untyped_trajectory_event",
                        "raw": event,
                    }
                )
        if "finalStatus" in artifacts:
            final_status = artifacts["finalStatus"]
            if isinstance(final_status, str) and final_status:
                metadata["final_status"] = final_status
            else:
                raw_event_refs.append(
                    {
                        "event_type": "malformed_final_status",
                        "raw": final_status,
                    }
                )

        turns: List[Turn] = []
        for idx, (event, event_envelope_refs) in enumerate(completed_events):
            data = event.get("data", {})
            if not isinstance(data, dict):
                data = {}
            (
                user_text,
                assistant_text,
                attachments,
                snapshot_refs,
            ) = messages_snapshot_to_text(data.get("messagesSnapshot"))
            alternate_refs: List[Dict[str, Any]] = []
            if not user_text:
                final_prompt = data.get("finalPromptText")
                if isinstance(final_prompt, str):
                    user_text = final_prompt
            elif (
                isinstance(data.get("finalPromptText"), str)
                and data["finalPromptText"] != user_text
            ):
                alternate_refs.append(
                    {
                        "event_type": "conflicting_final_prompt",
                        "raw": data["finalPromptText"],
                    }
                )
            if not assistant_text:
                assistant_texts = data.get("assistantTexts")
                if isinstance(assistant_texts, list):
                    assistant_text = "\n\n".join(
                        item for item in assistant_texts if isinstance(item, str) and item
                    )
            elif (
                isinstance(data.get("assistantTexts"), list)
                and all(isinstance(item, str) for item in data["assistantTexts"])
            ):
                alternate_assistant = "\n\n".join(
                    item for item in data["assistantTexts"] if item
                )
                if alternate_assistant != assistant_text:
                    alternate_refs.append(
                        {
                            "event_type": "conflicting_assistant_texts",
                            "raw": data["assistantTexts"],
                        }
                    )
            reasoning_value = (
                data["reasoning"] if "reasoning" in data else data.get("thinking")
            )
            reasoning = reasoning_value if isinstance(reasoning_value, str) else ""
            tool_results_value = (
                data["toolResults"]
                if "toolResults" in data
                else data.get("tool_results")
            )
            tool_results = dict_items(tool_results_value)
            turn_refs = [
                *raw_event_refs,
                *event_envelope_refs,
                *snapshot_refs,
                *alternate_refs,
                *malformed_collection_refs(
                    tool_results_value,
                    "malformed_tool_results",
                ),
            ]
            turn_refs.extend(tool_alias_conflict_refs(data))
            turn_refs.extend(
                invalid_present_ref(
                    data,
                    "usage",
                    lambda value: isinstance(value, dict),
                    "malformed_usage",
                )
            )
            turn_refs.extend(
                invalid_present_ref(
                    data,
                    "messagesSnapshot",
                    lambda value: isinstance(value, list),
                    "malformed_messages_snapshot",
                )
            )
            turn_refs.extend(
                invalid_present_ref(
                    data,
                    "finalPromptText",
                    lambda value: isinstance(value, str),
                    "malformed_final_prompt_text",
                )
            )
            turn_refs.extend(
                invalid_present_ref(
                    data,
                    "assistantTexts",
                    lambda value: isinstance(value, list)
                    and all(isinstance(item, str) for item in value),
                    "malformed_assistant_text",
                )
            )
            turn_refs.extend(
                invalid_present_ref(
                    data,
                    "toolResults",
                    lambda value: isinstance(value, (dict, list)),
                    "malformed_tool_results",
                )
            )
            turn_refs.extend(
                invalid_present_ref(
                    data,
                    "tool_results",
                    lambda value: isinstance(value, (dict, list)),
                    "malformed_tool_results",
                )
            )
            for reasoning_key in ("reasoning", "thinking"):
                turn_refs.extend(
                    invalid_present_ref(
                        data,
                        reasoning_key,
                        lambda value: isinstance(value, str),
                        "malformed_reasoning",
                    )
                )
            if (
                "reasoning" in data
                and "thinking" in data
                and data["reasoning"] != data["thinking"]
            ):
                turn_refs.append(
                    {
                        "event_type": "conflicting_reasoning_fields",
                        "raw": {
                            "reasoning": data["reasoning"],
                            "thinking": data["thinking"],
                        },
                    }
                )
            residual = {
                key: value
                for key, value in data.items()
                if key
                not in {
                    "usage",
                    "messagesSnapshot",
                    "finalPromptText",
                    "assistantTexts",
                    "reasoning",
                    "thinking",
                    "toolResults",
                    "tool_results",
                }
            }
            if residual:
                turn_refs.append(
                    {
                        "event_type": "trajectory_completed_residual",
                        "raw": residual,
                    }
                )
            if not (
                user_text
                or assistant_text
                or tool_calls
                or tool_results
                or reasoning
                or attachments
                or turn_refs
            ):
                turn_refs.append(
                    {
                        "event_type": "empty_trajectory_completion",
                        "raw": {
                            "type": event.get("type"),
                            "data": data,
                        },
                    }
                )
            if not (
                user_text
                or assistant_text
                or tool_calls
                or tool_results
                or reasoning
                or turn_refs
            ):
                continue
            turn_metadata = dict(metadata)
            if isinstance(data.get("usage"), dict):
                turn_metadata["usage"] = data["usage"]
            turns.append(
                Turn(
                    turn_number=idx,
                    user_content=user_text,
                    assistant_content=assistant_text,
                    timestamp=event.get("ts"),
                    metadata=turn_metadata,
                    tool_calls=list(tool_calls),
                    tool_results=tool_results,
                    reasoning=reasoning,
                    attachments=attachments,
                    raw_event_refs=turn_refs,
                    source_files=[str(trajectory_path)],
                    completeness={
                        "visible_text": "full",
                        "tool_calls": "full" if tool_calls else "unavailable",
                        "tool_results": "full" if tool_results else "unavailable",
                        "reasoning": "full" if reasoning else "unavailable",
                        "attachments": "full" if attachments else "unavailable",
                        "truncated": False,
                        "loss_reasons": [],
                    },
                )
            )
        if not turns and (
            raw_event_refs
            or tool_calls
            or len(metadata) > 1
            or artifacts
        ):
            turns.append(
                Turn(
                    turn_number=0,
                    user_content="",
                    assistant_content="",
                    metadata=dict(metadata),
                    tool_calls=list(tool_calls),
                    raw_event_refs=list(raw_event_refs),
                    source_files=[str(trajectory_path)],
                    completeness={
                        "visible_text": "full",
                        "tool_calls": "full" if tool_calls else "unavailable",
                        "tool_results": "unavailable",
                        "reasoning": "unavailable",
                        "attachments": "unavailable",
                        "truncated": False,
                        "loss_reasons": [],
                    },
                )
            )
        return turns

    def _parse_corpus(self, corpus_path: Path) -> Dict[str, List[Dict[str, Any]]]:
        """解析语料文件，按 session_id 分组"""
        sessions: Dict[str, List[Dict[str, Any]]] = {}
        fallback_id = corpus_path.stem

        try:
            raw_lines = read_native_bytes(corpus_path).splitlines()
        except OSError:
            raise NativeSourceContractError(
                "native_openclaw_corpus_read_failed"
            ) from None

        for line_number, raw_line in enumerate(raw_lines, start=1):
            if not raw_line.strip():
                continue
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                sessions.setdefault(fallback_id, []).append(
                    {
                        "role": "native_raw",
                        "raw_event_refs": [
                            {
                                "line_number": line_number,
                                "raw_base64": base64.b64encode(raw_line).decode(
                                    "ascii"
                                ),
                                "raw_encoding": "base64",
                                "decode_error": "invalid_utf8",
                            }
                        ],
                    }
                )
                continue
            m = SESSION_LINE_RE.match(line)
            if not m:
                sessions.setdefault(fallback_id, []).append(
                    {
                        "role": "native_raw",
                        "raw_event_refs": [
                            {
                                "line_number": line_number,
                                "raw": line,
                                "parse_error": "unmatched_corpus_line",
                            }
                        ],
                    }
                )
                continue

            # 从 path 提取 session_id
            path_str = m.group("path")
            session_match = re.search(r"sessions/([^/#]+)", path_str)
            session_id = session_match.group(1) if session_match else fallback_id

            role = m.group("role").lower()
            msg_content = m.group("content")

            sessions.setdefault(session_id, []).append(
                {
                    "role": role,
                    "content": msg_content,
                    "raw_event_refs": [
                        {
                            "event_type": "corpus_line_provenance",
                            "raw": {
                                "path": path_str,
                                "line": int(m.group("line")),
                                "artifact_line_number": line_number,
                            },
                        }
                    ],
                }
            )

        return sessions

    def _pair_messages(
        self,
        messages: List[Dict[str, Any]],
        session_id: str,
        base_turn_number: int = 0,
        source_file: str = "",
        timestamp: str = "",
    ) -> List[Turn]:
        """将消息列表配对为 Turn 列表"""
        turns: List[Turn] = []
        user_content = ""
        assistant_content = ""
        turn_number = base_turn_number
        attachments: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        tool_results: List[Dict[str, Any]] = []
        reasoning_parts: List[str] = []
        raw_event_refs: List[Dict[str, Any]] = []
        turn_timestamp = timestamp

        def has_payload() -> bool:
            return bool(
                user_content
                or assistant_content
                or attachments
                or tool_calls
                or tool_results
                or reasoning_parts
                or raw_event_refs
            )

        def build_turn() -> Turn:
            return Turn(
                turn_number=turn_number,
                user_content=user_content,
                assistant_content=assistant_content,
                timestamp=turn_timestamp or None,
                metadata={
                    key: value
                    for key, value in {
                        "session_id": session_id,
                        "timestamp": turn_timestamp,
                    }.items()
                    if value
                },
                tool_calls=list(tool_calls),
                tool_results=list(tool_results),
                reasoning="\n".join(reasoning_parts),
                attachments=list(attachments),
                raw_event_refs=list(raw_event_refs),
                source_files=[source_file] if source_file else [],
                completeness={
                    "visible_text": "full",
                    "tool_calls": "full" if tool_calls else "unavailable",
                    "tool_results": "full" if tool_results else "unavailable",
                    "reasoning": "full" if reasoning_parts else "unavailable",
                    "attachments": "full" if attachments else "unavailable",
                    "truncated": False,
                    "loss_reasons": [],
                },
            )

        for msg in messages:
            role = msg.get("role", "")
            content = str(msg.get("content") or "")

            if role == "user":
                if has_payload():
                    turns.append(build_turn())
                    turn_number += 1
                user_content = content
                assistant_content = ""
                turn_timestamp = str(msg.get("timestamp") or timestamp or "")
                attachments = list(msg.get("attachments") or [])
                tool_calls = list(msg.get("tool_calls") or [])
                tool_results = list(msg.get("tool_results") or [])
                reasoning_parts = [
                    str(msg.get("reasoning"))
                ] if msg.get("reasoning") else []
                raw_event_refs = list(msg.get("raw_event_refs") or [])

            elif role == "assistant":
                if not turn_timestamp:
                    turn_timestamp = str(msg.get("timestamp") or timestamp or "")
                if content:
                    assistant_content += (
                        ("\n" if assistant_content else "") + content
                    )
                attachments.extend(msg.get("attachments") or [])
                tool_calls.extend(msg.get("tool_calls") or [])
                tool_results.extend(msg.get("tool_results") or [])
                if msg.get("reasoning"):
                    reasoning_parts.append(str(msg["reasoning"]))
                raw_event_refs.extend(msg.get("raw_event_refs") or [])

            elif role == "tool":
                if not turn_timestamp:
                    turn_timestamp = str(msg.get("timestamp") or timestamp or "")
                tool_calls.extend(msg.get("tool_calls") or [])
                tool_results.extend(msg.get("tool_results") or [])
                raw_event_refs.extend(msg.get("raw_event_refs") or [])

            else:
                if not turn_timestamp:
                    turn_timestamp = str(msg.get("timestamp") or timestamp or "")
                raw_event_refs.extend(msg.get("raw_event_refs") or [])

        # 保存最后一轮
        if has_payload():
            turns.append(build_turn())

        return turns

    def get_session_state(self, session_info: SessionInfo) -> Optional[Dict[str, Any]]:  # noqa
        """Track the exact selected native artifact, regardless of source format."""
        return self._compute_session_state([session_info.source_path])

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """OpenClaw 自定义标签"""
        tags = super().build_extra_tags(turn)
        if turn.metadata.get("session_id"):
            tags.append(f"openclaw-session={turn.metadata['session_id'][:8]}")
        return tags
