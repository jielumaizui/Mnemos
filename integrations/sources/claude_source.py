# -*- coding: utf-8 -*-
"""
ClaudeSource — Claude Code Agent 同步插件

实现 AgentSource 接口，接入 SyncFramework。
从 claude_live_sync.py 迁移的完整 JSONL 消息解析逻辑：
  - thinking/tool_use/tool_result 内容块提取
  - 增量同步（offset 追踪）
  - 工具调用/推理内容提取
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config import get_config
from core.sync_framework.agent_source import (
    NativeSourceContractError,
    SessionInfo,
    Turn,
)
from core.ops.durable_io import open_native_text

from integrations.sources.base import BaseAgentSource, native_path_kind


def _normalize_content_blocks(
    raw_content: Any,
) -> Tuple[str, str, List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """解析 content 内容块数组，提取文本、reasoning、工具调用/结果及未知块。"""
    if not isinstance(raw_content, list):
        return str(raw_content), "", [], [], []

    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    tool_results: List[Dict[str, Any]] = []
    raw_event_refs: List[Dict[str, Any]] = []

    for part in raw_content:
        if isinstance(part, str):
            content_parts.append(part)
            continue
        if not isinstance(part, dict):
            raw_event_refs.append(
                {
                    "type": "non_object_content_block",
                    "raw": part,
                }
            )
            continue
        part_type = part.get("type", "")
        if part_type == "text":
            content_parts.append(part.get("text", ""))
            residual = {
                key: value
                for key, value in part.items()
                if key not in {"type", "text"}
            }
            if residual:
                raw_event_refs.append(
                    {
                        "type": "text_block_residual",
                        "raw": residual,
                    }
                )
        elif part_type in ("thinking", "reasoning"):
            # P0-6: 不再截断 reasoning
            reasoning = str(part.get("thinking", part.get("text", "")) or "")
            if reasoning:
                reasoning_parts.append(reasoning)
            residual = {
                key: value
                for key, value in part.items()
                if key not in {"type", "thinking", "text"}
            }
            if residual:
                raw_event_refs.append(
                    {
                        "type": "reasoning_block_residual",
                        "raw": residual,
                    }
                )
        elif part_type == "tool_use":
            tool_calls.append(
                {
                    "name": part.get("name", "unknown"),
                    "input": part.get("input", {}),
                    "id": part.get("id", ""),
                }
            )
            raw_event_refs.append(
                {"type": "native_tool_use", "raw": part}
            )
        elif part_type == "tool_result":
            # P0-6: 完整保留 tool_result，不再截断 stdout/stderr
            tool_results.append(
                {
                    "stdout": str(part.get("content", "")),
                    "tool_use_id": part.get("tool_use_id", ""),
                }
            )
            raw_event_refs.append(
                {"type": "native_tool_result", "raw": part}
            )
        elif "content" in part:
            content_parts.append(str(part["content"]))
            raw_event_refs.append(
                {"type": part_type or "content_block", "raw": part}
            )
        else:
            # 未知块记录到 raw_event_refs
            raw_event_refs.append({"type": part_type, "raw": part})

    content = "\n".join(content_parts)
    return content, "\n".join(reasoning_parts), tool_calls, tool_results, raw_event_refs


def _normalize_tool_call(t: Any) -> Dict[str, Any]:
    """把单个 tool_call 结构统一为 {name, input, id}。"""
    if not isinstance(t, dict):
        return {"name": "unknown", "input": {}, "id": "", "raw": t}
    func = t.get("function", {}) or {}
    return {
        "name": t.get("name", func.get("name", "unknown")),
        "input": t.get("input", t.get("arguments", func.get("arguments", {}))),
        "id": t.get("id", ""),
    }


def _claude_artifact_identity(
    projects_dir: Path,
    transcript_path: Path,
    *,
    disambiguate_project_transcript: bool = False,
) -> Tuple[str, str, List[str], Dict[str, Any]]:
    """Return the storage identity and lineage for one native Claude JSONL.

    Claude keeps ordinary project transcripts and nested subagent transcripts
    below the same ``projects`` root.  A subagent filename alone is not a
    session identity: names can repeat beneath one parent session, so its
    canonical id includes a deterministic hash of the native artifact path.
    Project transcripts retain their native filename id for backwards
    compatibility with existing canonical Raw rows.
    """
    relative = transcript_path.relative_to(projects_dir)
    relative_parts = relative.parts
    artifact_digest = hashlib.sha256(
        f"claude-project-artifact-v1:{relative.as_posix()}".encode("utf-8")
    ).hexdigest()[:24]
    metadata: Dict[str, Any] = {
        "source_artifact_id": f"claude-artifact-{artifact_digest}",
    }

    try:
        subagent_index = relative_parts.index("subagents")
    except ValueError:
        subagent_index = -1

    if subagent_index < 2:
        canonical_id = transcript_path.stem
        if disambiguate_project_transcript:
            canonical_id = f"{canonical_id}::project::{artifact_digest}"
        return canonical_id, "project_transcript", [transcript_path.stem], metadata

    parent_session_id = relative_parts[subagent_index - 1] if subagent_index >= 2 else ""
    if parent_session_id:
        metadata["parent_session_id"] = parent_session_id
        metadata["parent_relation"] = "native_subagent_path"
    else:
        # The artifact remains independently captureable even if a future
        # Claude layout lacks the documented parent-session path segment.
        # Do not invent a parent session id from the project directory.
        metadata["parent_relation"] = "native_subagent_parent_unresolved"

    canonical_id = f"{parent_session_id or 'unresolved'}::subagent::{artifact_digest}"
    return canonical_id, "subagent", [transcript_path.stem], metadata


class ClaudeSource(BaseAgentSource):
    """Claude Code 数据源插件"""

    _cap_tool_calls = True
    _cap_tool_results = True
    _cap_reasoning = True
    _cap_attachments = False
    _cap_source_fidelity = "full"
    _cap_memory_scope = "host_memory_imports_plus_project_transcripts"
    _cap_host_memory_default = "project_user_memory_loaded_when_configured"
    _cap_host_memory_effect = (
        "CLAUDE.md/imported memory affects prompts but ~/.claude/projects JSONL remains the capture source"
    )
    _cap_transcript_kind = "native_project_jsonl"
    _cap_compression = "raw_transcript_blocks_no_mnemos_compression"

    _default_extra_tags = ["has-tools=true", "has-reasoning=true"]

    @property
    def name(self) -> str:
        return "claude"

    @property
    def model_tag(self) -> str:
        return "claude-code"

    @property
    def data_dir(self) -> Optional[Path]:
        """返回 Claude 会话文件所在目录。

        会话实际位于 ``~/.claude/projects/**/*.jsonl``。将 data_dir 直接指向
        projects 目录，可使 watchdog 只监听真正的会话文件，避免被 config、
        cache、logs 等高频文件持续触发。
        """
        config = get_config()
        root = config.claude_data_dir if hasattr(config, "claude_data_dir") else None
        root = Path(root).expanduser() if root else Path.home() / ".claude"
        if native_path_kind(root) == "missing":
            return None
        projects = root / "projects"
        return (
            projects
            if native_path_kind(projects) != "missing"
            else root
        )

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        return {
            "type": "watchdog",
            "events": ["modified"],
            "debounce": 5.0,
            "recursive": True,
        }

    def discover_sessions(self) -> List[SessionInfo]:
        """Discover every native Claude project/subagent transcript recursively."""
        base = self.data_dir
        if not base:
            return []
        projects_dir = base if base.name == "projects" else base / "projects"
        if native_path_kind(projects_dir) == "missing":
            return []

        candidates: List[Tuple[float, str, Path]] = []
        try:
            transcript_paths = projects_dir.rglob("*.jsonl")
            for path in transcript_paths:
                if native_path_kind(path) != "file":
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    raise NativeSourceContractError(
                        "native_claude_discovery_stat_failed"
                    ) from None
                candidates.append((mtime, path.relative_to(projects_dir).as_posix(), path))
        except OSError:
            raise NativeSourceContractError(
                "native_claude_discovery_failed"
            ) from None

        project_stem_counts: Dict[str, int] = {}
        for _mtime, relative_path, path in candidates:
            relative_parts = Path(relative_path).parts
            try:
                subagent_index = relative_parts.index("subagents")
            except ValueError:
                subagent_index = -1
            if subagent_index < 2:
                project_stem_counts[path.stem] = project_stem_counts.get(path.stem, 0) + 1

        discovered: List[Tuple[float, str, SessionInfo]] = []
        for mtime, relative_path, path in candidates:
            session_id, source_kind, aliases, metadata = _claude_artifact_identity(
                projects_dir,
                path,
                disambiguate_project_transcript=project_stem_counts.get(path.stem, 0) > 1,
            )
            discovered.append(
                (
                    mtime,
                    relative_path,
                    SessionInfo(
                        session_id=session_id,
                        source_path=path,
                        working_dir=str(path.parent),
                        mtime=mtime,
                        canonical_session_id=session_id,
                        session_aliases=aliases,
                        source_kind=source_kind,
                        metadata=metadata,
                    ),
                )
            )

        discovered.sort(key=lambda item: (-item[0], item[1]))
        sessions = [item[2] for item in discovered]
        return sessions

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 JSONL 会话文件为 Turn 列表 — P0-6 完整录入版"""
        turns = []  # type: ignore[var-annotated]
        try:
            with open_native_text(session_path) as f:
                lines = f.readlines()
        except (OSError, IOError):
            raise NativeSourceContractError(
                "native_claude_transcript_read_failed"
            ) from None

        current_user = ""
        current_assistant = ""
        current_meta: Dict[str, Any] = {}
        current_user_native_id = ""
        turn_number = 0
        completeness_loss: List[str] = []
        # Claude JSONL can contain an exact repeated replay of the same native
        # user/assistant pair after recovery.  Native IDs define the logical
        # event boundary, so emitting that replay as another turn creates a
        # denominator that canonical Raw cannot represent without inventing a
        # second identity.  Keep a payload fingerprint to remove only bytewise
        # equivalent replays; a conflicting reuse remains visible through an
        # auditable fallback identity below.
        native_payloads: Dict[str, str] = {}

        def append_current_turn() -> bool:
            """Append one completed pair, preserving identity conflicts safely."""
            nonlocal turn_number
            metadata = dict(current_meta)
            loss_reasons = list(completeness_loss)
            native_event_id = str(metadata.get("native_event_id") or "")
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "user_content": current_user,
                        "assistant_content": current_assistant,
                        "timestamp": metadata.get("timestamp"),
                        "tool_calls": metadata.get("tool_calls", []),
                        "tool_results": metadata.get("tool_results", []),
                        "reasoning": metadata.get("reasoning", ""),
                        "raw_event_refs": metadata.get("raw_event_refs", []),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            if native_event_id:
                existing = native_payloads.get(native_event_id)
                if existing == fingerprint:
                    return False
                if existing is not None:
                    # Preserve a malformed-but-distinct visible event instead
                    # of collapsing it under an explicit ID that has already
                    # been used.  The shared identity resolver receives the
                    # parser/artifact offset once source metadata is bound.
                    metadata.pop("native_event_id", None)
                    metadata["parser_offset"] = str(turn_number)
                    metadata["native_event_identity_conflict"] = (
                        "explicit_native_id_payload_conflict"
                    )
                    metadata.setdefault("raw_event_refs", []).append(
                        {
                            "event_type": "native_event_id_payload_conflict",
                            "conflicting_native_event_id": native_event_id,
                            "resolution": "parser_artifact_offset",
                        }
                    )
                else:
                    native_payloads[native_event_id] = fingerprint

            turns.append(
                Turn(
                    turn_number=turn_number,
                    user_content=current_user,
                    assistant_content=current_assistant,
                    timestamp=metadata.get("timestamp"),
                    metadata=metadata,
                    tool_calls=metadata.get("tool_calls", []),
                    tool_results=metadata.get("tool_results", []),
                    reasoning=metadata.get("reasoning", ""),
                    raw_event_refs=metadata.get("raw_event_refs", []),
                    source_files=[str(session_path)],
                    completeness={
                        "visible_text": "full",
                        "tool_results": (
                            "full" if metadata.get("tool_results") else "unavailable"
                        ),
                        "reasoning": "full" if metadata.get("reasoning") else "unavailable",
                        "attachments": "unavailable",
                        "truncated": False,
                        "loss_reasons": loss_reasons,
                    },
                )
            )
            turn_number += 1
            return True

        def merge_standardized_metadata(standardized: Dict[str, Any]) -> None:
            """Accumulate every visible block from one native role event."""
            if not current_meta.get("timestamp") and standardized.get("timestamp"):
                current_meta["timestamp"] = standardized["timestamp"]
            for key in ("tool_calls", "tool_results", "raw_event_refs"):
                values = standardized.get(key, [])
                if values:
                    current_meta.setdefault(key, []).extend(values)
            reasoning = str(standardized.get("reasoning") or "")
            if reasoning:
                prior = str(current_meta.get("reasoning") or "")
                current_meta["reasoning"] = (
                    f"{prior}\n{reasoning}" if prior else reasoning
                )
            if not current_meta.get("native_event_id"):
                native_event_id = (
                    current_user_native_id
                    or str(standardized.get("native_event_id") or "")
                )
                if native_event_id:
                    current_meta["native_event_id"] = native_event_id

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                raise NativeSourceContractError(
                    "native_claude_jsonl_decode_failed"
                ) from None

            standardized = self._standardize_message(msg)
            if not standardized:
                current_meta.setdefault("raw_event_refs", []).append(
                    {
                        "event_type": (
                            "non_object_native_record"
                            if not isinstance(msg, dict)
                            else "unrecognized_native_record"
                        ),
                        "raw": msg,
                    }
                )
                completeness_loss.append(
                    "native_record_preserved_without_normalization"
                )
                continue

            role = standardized.get("role", "")
            content = standardized.get("content", "")

            if role == "user":
                if current_user or current_assistant or current_meta:
                    append_current_turn()
                    completeness_loss = []
                current_user = content
                current_assistant = ""
                current_meta = {}
                current_user_native_id = str(standardized.get("native_event_id") or "")
                merge_standardized_metadata(standardized)
            elif role == "assistant":
                if content:
                    current_assistant = (
                        f"{current_assistant}\n{content}"
                        if current_assistant
                        else content
                    )
                merge_standardized_metadata(standardized)

        # 保存最后一轮
        if current_user or current_assistant or current_meta:
            append_current_turn()

        return turns

    def _standardize_message(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        标准化 Claude Code JSONL 消息格式 — P0-6 完整录入版。

        修复：不再在解析阶段截断 thinking / tool_result。
        大内容由 CaptureService 层写入 artifact。
        """
        if not isinstance(msg, dict):
            return None

        message_data = msg.get("message", msg)
        if not isinstance(message_data, dict):
            return None
        role = message_data.get("role", "") or msg.get("type", "")

        raw_content = message_data.get("content", "")
        tool_calls_raw = message_data.get("tool_calls") or msg.get("tool_calls") or []
        tool_calls = tool_calls_raw if isinstance(tool_calls_raw, list) else []
        tool_results_raw = msg.get("toolUseResult") or msg.get("tool_results") or []
        tool_results = tool_results_raw if isinstance(tool_results_raw, list) else []

        content, reasoning, extracted_calls, extracted_results, raw_event_refs = _normalize_content_blocks(
            raw_content
        )
        message_residual = {
            key: value
            for key, value in message_data.items()
            if key not in {"role", "content", "tool_calls"}
        }
        if message_residual:
            raw_event_refs.append(
                {
                    "type": "native_message_residual",
                    "raw": message_residual,
                }
            )
        if message_data is not msg:
            outer_residual = {
                key: value
                for key, value in msg.items()
                if key
                not in {
                    "message",
                    "timestamp",
                    "type",
                    "toolUseResult",
                    "tool_results",
                    "tool_calls",
                }
            }
            if outer_residual:
                raw_event_refs.append(
                    {
                        "type": "native_record_residual",
                        "raw": outer_residual,
                    }
                )
        if tool_calls_raw and not isinstance(tool_calls_raw, list):
            raw_event_refs.append(
                {
                    "type": "malformed_tool_calls",
                    "raw": tool_calls_raw,
                }
            )
        if tool_results_raw and not isinstance(tool_results_raw, list):
            raw_event_refs.append(
                {
                    "type": "malformed_tool_results",
                    "raw": tool_results_raw,
                }
            )
        if isinstance(tool_calls_raw, list) and tool_calls_raw:
            raw_event_refs.append(
                {
                    "type": "native_tool_calls",
                    "raw": tool_calls_raw,
                }
            )
        if isinstance(tool_results_raw, list) and tool_results_raw:
            raw_event_refs.append(
                {
                    "type": "native_tool_results",
                    "raw": tool_results_raw,
                }
            )
        tool_calls.extend(extracted_calls)
        tool_results.extend(extracted_results)

        # 允许 tool-only 消息（role 和 tool_calls 存在但 content 为空）
        if not role:
            return None
        if (
            not content
            and not tool_calls
            and not tool_results
            and not reasoning
            and not raw_event_refs
        ):
            return None

        result: Dict[str, Any] = {
            "role": role,
            "content": content,
            "timestamp": msg.get("timestamp", ""),
        }
        # Here `message_data` is the parser's actual message record, so its
        # explicit id is safe to promote.  Generic IDs elsewhere remain
        # intentionally ignored by the shared identity resolver.
        for candidate in (message_data, msg):
            for key in ("event_id", "eventId", "message_id", "messageId", "uuid", "id"):
                value = candidate.get(key)
                if isinstance(value, (str, int)) and str(value):
                    result["native_event_id"] = f"claude:{key}:{value}"
                    break
            if result.get("native_event_id"):
                break

        if tool_calls:
            result["tool_calls"] = [_normalize_tool_call(t) for t in tool_calls]

        if tool_results:
            result["tool_results"] = tool_results

        if reasoning:
            result["reasoning"] = reasoning

        if raw_event_refs:
            result["raw_event_refs"] = raw_event_refs

        return result

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Claude 自定义标签"""
        tags = []
        meta = turn.metadata
        if meta.get("tool_calls"):
            tags.append("has-tools=true")
        if meta.get("reasoning"):
            tags.append("has-reasoning=true")
        return tags
