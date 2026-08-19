# -*- coding: utf-8 -*-
"""
CodexSource — Codex Agent 同步插件

实现 AgentSource 接口，接入 SyncFramework。
Codex rollout 是会话期间持续追加的 JSONL；运行时监听 created/modified，
离线全量对账则显式把当前调用线程延后到下一代，避免把尚未完成的未来
尾部伪装成一个可关闭的历史分母。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.config import get_config
from core.runtime_environment import environment_get
from core.sync_framework.agent_source import (
    NativeSourceContractError,
    SessionInfo,
    Turn,
)
from core.ops.durable_io import open_native_text

from integrations.sources.base import (
    BaseAgentSource,
    native_path_kind,
    stable_path_session_id,
)

logger = logging.getLogger(__name__)


def _is_attachment_block(block: Dict[str, Any]) -> bool:
    btype = str(block.get("type") or "").lower()
    if any(token in btype for token in ("image", "file", "attachment", "media")):
        return True
    return any(key in block for key in ("file_id", "filename", "path", "mime_type", "url"))


def _attachment_from_block(block: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": block.get("type", "attachment"),
        "id": block.get("id") or block.get("file_id") or "",
        "name": block.get("filename") or block.get("name") or "",
        "path": block.get("path") or "",
        "url": block.get("url") or "",
        "mime_type": block.get("mime_type") or block.get("media_type") or "",
        "raw": block,
    }


def _tool_call_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": payload.get("id", ""),
        "call_id": payload.get("call_id", ""),
        "name": payload.get("name", ""),
        "namespace": payload.get("namespace", ""),
        "arguments": payload.get("arguments", {}),
    }


def _tool_result_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "call_id": payload.get("call_id", ""),
        "output": payload.get("output", ""),
        "raw": payload,
    }


def _reasoning_text(payload: Dict[str, Any]) -> str:
    summary = payload.get("summary")
    if isinstance(summary, list):
        parts: List[str] = []
        for item in summary:
            if isinstance(item, dict):
                text = item.get("text") or item.get("summary") or item.get("content")
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(summary, str):
        return summary
    return ""


def _reasoning_ref(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "event_type": "reasoning",
        "id": payload.get("id", ""),
        "summary_available": bool(_reasoning_text(payload)),
        "encrypted": bool(payload.get("encrypted_content")),
        "metadata": payload.get("metadata")
        or payload.get("internal_chat_message_metadata_passthrough")
        or {},
    }


def _record_residual(
    record: Dict[str, Any],
    payload: Dict[str, Any],
    *,
    payload_keys: set[str],
) -> Dict[str, Any]:
    """Keep only unnormalized record fields, never duplicate known text."""

    outer = {
        key: value
        for key, value in record.items()
        if key not in {"type", "payload"}
    }
    payload_residual = {
        key: value
        for key, value in payload.items()
        if key not in payload_keys
    }
    residual: Dict[str, Any] = {}
    if outer:
        residual["record"] = outer
    if payload_residual:
        residual["payload"] = payload_residual
    return residual


class CodexSource(BaseAgentSource):  # noqa: Vulture - loaded by SourceRegistry builtin reflection.
    """Codex 数据源插件"""

    _cap_tool_calls = True
    _cap_tool_results = True
    _cap_reasoning = "summary_or_encrypted"
    _cap_attachments = "available"
    _cap_source_fidelity = "full"
    _cap_memory_scope = "host_memory_context_plus_raw_rollout"
    _cap_host_memory_default = "off_until_user_enables_codex_memories"
    _cap_host_memory_effect = (
        "changes model context only; rollout capture still records the actual conversation"
    )
    _cap_transcript_kind = "native_rollout_jsonl"
    _cap_compression = "raw_events_with_encrypted_or_summary_reasoning"

    @property
    def name(self) -> str:
        return "codex"

    @property
    def model_tag(self) -> str:
        return "codex"

    @property
    def data_dir(self) -> Optional[Path]:
        config = get_config()
        # 环境变量优先
        for env_key in ("CODEX_HOME", "XDG_CONFIG_HOME"):
            env = config.get(f"integrations.codex.{env_key.lower()}")
            if env:
                p = Path(env).expanduser()
                if native_path_kind(p) != "missing":
                    return p

        for env_key in ("CODEX_HOME", "XDG_CONFIG_HOME"):
            val = os.getenv(env_key)
            if val:
                if env_key == "XDG_CONFIG_HOME":
                    p = Path(val) / "codex"
                else:
                    p = Path(val).expanduser()
                if native_path_kind(p) != "missing":
                    return p

        # 标准路径
        for std in ("~/.codex", "~/.config/codex"):
            p = Path(std).expanduser()
            if native_path_kind(p) != "missing":
                sessions = p / "sessions"
                return (
                    sessions
                    if native_path_kind(sessions) != "missing"
                    else p
                )
        return None

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        return {
            "type": "watchdog",
            "events": ["created", "modified", "moved"],
            "debounce": 1.0,
            "recursive": True,
        }

    @staticmethod
    def current_active_session_id() -> str:
        value = str(
            environment_get("CODEX_THREAD_ID", "")
        ).strip().lower()
        if re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            value,
        ):
            return value
        return ""

    def discover_sessions(self) -> List[SessionInfo]:
        """发现所有可同步的 Codex rollout 文件"""
        base = self.data_dir
        if not base:
            return []
        sessions_dir = self._sessions_dir(base)
        sessions = self._discover_by_glob(
            sessions_dir,
            "rollout-*.jsonl",
            session_id_func=self._extract_codex_session_id,
            working_dir_from="parent",
        )
        for info in sessions:
            info.canonical_session_id = (
                info.session_id
                if re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                    r"[0-9a-f]{4}-[0-9a-f]{12}",
                    info.session_id,
                )
                else stable_path_session_id(
                    "codex",
                    sessions_dir,
                    info.source_path,
                    native_id=info.session_id,
                )
            )
            info.session_aliases = [info.source_path.stem]
            info.source_kind = "rollout"
        return sessions

    def _extract_codex_session_id(self, path: Path) -> str:
        uuid_match = re.search(
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            path.name,
        )
        return uuid_match.group(1) if uuid_match else path.stem

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 Codex rollout JSONL 文件为 Turn 列表"""
        messages = self._parse_rollout(session_path)
        return self._pair_messages_to_turns(messages, session_path)

    def get_session_state(self, session_info: SessionInfo) -> Optional[Dict[str, Any]]:  # noqa
        """Track only the exact rollout consumed by this native session."""
        return self._compute_session_state([session_info.source_path])

    def _parse_rollout(self, rollout_path: Path) -> List[Dict[str, Any]]:
        """解析 rollout 文件，提取消息列表 — P0-6 完整录入版"""
        messages: List[Dict[str, Any]] = []
        raw_event_refs: List[Dict[str, Any]] = []
        tool_calls: List[Dict[str, Any]] = []
        tool_results: List[Dict[str, Any]] = []
        reasoning_parts: List[str] = []
        attachments: List[Dict[str, Any]] = []

        def flush_pending(role: str, content: str) -> None:
            nonlocal raw_event_refs, tool_calls, tool_results, reasoning_parts, attachments
            if not role:
                return
            messages.append(
                {
                    "role": role,
                    "content": content,
                    "raw_event_refs": list(raw_event_refs),
                    "tool_calls": list(tool_calls),
                    "tool_results": list(tool_results),
                    "reasoning": "\n".join(part for part in reasoning_parts if part),
                    "attachments": list(attachments),
                }
            )
            raw_event_refs = []
            tool_calls = []
            tool_results = []
            reasoning_parts = []
            attachments = []

        try:
            with open_native_text(rollout_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        raise NativeSourceContractError(
                            "native_codex_jsonl_decode_failed"
                        ) from None

                    if not isinstance(obj, dict):
                        raw_event_refs.append(
                            {
                                "event_type": "non_object_native_record",
                                "raw": obj,
                            }
                        )
                        continue
                    event_type = obj.get("type", "")

                    if event_type == "response_item":
                        payload = obj.get("payload", {})
                        if not isinstance(payload, dict):
                            raw_event_refs.append(
                                {
                                    "event_type": "malformed_response_item",
                                    "raw": obj,
                                }
                            )
                            continue
                        payload_type = payload.get("type")
                        if payload_type == "message":
                            residual = _record_residual(
                                obj,
                                payload,
                                payload_keys={"type", "role", "content"},
                            )
                            if residual:
                                raw_event_refs.append(
                                    {
                                        "event_type": "response_item_residual",
                                        "raw": residual,
                                    }
                                )
                            role = payload.get("role", "")
                            texts = []
                            content_blocks = payload.get("content", [])
                            if not isinstance(content_blocks, list):
                                raw_event_refs.append(
                                    {
                                        "event_type": "malformed_message_content",
                                        "raw": content_blocks,
                                    }
                                )
                                content_blocks = []
                            for block in content_blocks:
                                if isinstance(block, dict):
                                    btype = block.get("type", "")
                                    if btype in ("input_text", "output_text"):
                                        texts.append(block.get("text", ""))
                                        block_residual = {
                                            key: value
                                            for key, value in block.items()
                                            if key not in {"type", "text"}
                                        }
                                        if block_residual:
                                            raw_event_refs.append(
                                                {
                                                    "event_type": "text_block_residual",
                                                    "raw": block_residual,
                                                }
                                            )
                                    elif btype in ("tool_call", "function_call"):
                                        tool_calls.append(_tool_call_from_payload(block))
                                        raw_event_refs.append({"event_type": "tool_call", "raw": block})
                                    elif btype in ("tool_output", "function_call_output"):
                                        tool_results.append(_tool_result_from_payload(block))
                                        raw_event_refs.append({"event_type": "tool_output", "raw": block})
                                    elif _is_attachment_block(block):
                                        attachments.append(_attachment_from_block(block))
                                        raw_event_refs.append({"event_type": btype, "raw": block})
                                    else:
                                        raw_event_refs.append({"event_type": btype, "raw": block})
                                else:
                                    raw_event_refs.append(
                                        {
                                            "event_type": "non_object_content_block",
                                            "raw": block,
                                        }
                                    )
                            flush_pending(role, "\n".join(texts))
                        elif payload_type == "function_call":
                            tool_calls.append(_tool_call_from_payload(payload))
                            raw_event_refs.append({"event_type": "function_call", "raw": payload})
                        elif payload_type == "function_call_output":
                            tool_results.append(_tool_result_from_payload(payload))
                            raw_event_refs.append(
                                {"event_type": "function_call_output", "raw": payload}
                            )
                        elif payload_type == "reasoning":
                            text = _reasoning_text(payload)
                            if text:
                                reasoning_parts.append(text)
                            raw_event_refs.append(_reasoning_ref(payload))
                        else:
                            raw_event_refs.append({"event_type": event_type, "raw": obj})

                    elif event_type == "event_msg":
                        payload = obj.get("payload", {})
                        if not isinstance(payload, dict):
                            raw_event_refs.append(
                                {
                                    "event_type": "malformed_event_message",
                                    "raw": obj,
                                }
                            )
                            continue
                        if payload.get("type") == "user_message":
                            residual = _record_residual(
                                obj,
                                payload,
                                payload_keys={"type", "message"},
                            )
                            if residual:
                                raw_event_refs.append(
                                    {
                                        "event_type": "user_message_residual",
                                        "raw": residual,
                                    }
                                )
                            msg = payload.get("message", "")
                            if msg:
                                flush_pending("user", str(msg))
                        elif payload.get("type") == "mcp_tool_call_end":
                            invocation = payload.get("invocation", {})
                            if isinstance(invocation, dict):
                                tool_calls.append(
                                    {
                                        "call_id": payload.get("call_id", ""),
                                        "name": invocation.get("tool", ""),
                                        "namespace": invocation.get("server", ""),
                                        "arguments": invocation.get("arguments", {}),
                                    }
                                )
                            if "result" in payload:
                                tool_results.append(
                                    {
                                        "call_id": payload.get("call_id", ""),
                                        "output": payload.get("result"),
                                        "raw": payload,
                                    }
                                )
                            raw_event_refs.append({"event_type": "mcp_tool_call_end", "raw": obj})
                        else:
                            # P0-6: 保留非 message 事件引用
                            raw_event_refs.append({"event_type": event_type, "raw": obj})
                    else:
                        # P0-6: 保留所有非 message 事件
                        raw_event_refs.append({"event_type": event_type, "raw": obj})
            if (
                raw_event_refs
                or tool_calls
                or tool_results
                or reasoning_parts
                or attachments
            ):
                flush_pending("raw_native_record", "")

        except (OSError, IOError, json.JSONDecodeError):
            raise NativeSourceContractError(
                "native_codex_rollout_read_failed"
            ) from None

        return messages

    def _pair_messages_to_turns(
        self, messages: List[Dict[str, Any]], session_path: Optional[Path] = None
    ) -> List[Turn]:
        """将消息列表配对为 Turn 列表"""
        turns = []
        user_content = ""
        assistant_content = ""
        turn_number = 0
        turn_raw_events: List[Dict[str, Any]] = []
        turn_tool_calls: List[Dict[str, Any]] = []
        turn_tool_results: List[Dict[str, Any]] = []
        turn_reasoning_parts: List[str] = []
        turn_attachments: List[Dict[str, Any]] = []
        completeness_loss: List[str] = []
        source_file = str(session_path) if session_path else ""

        def has_reasoning_metadata(raw_events: List[Dict[str, Any]]) -> bool:
            return any(ref.get("event_type") == "reasoning" for ref in raw_events)

        def append_turn() -> None:
            reasoning = "\n".join(part for part in turn_reasoning_parts if part)
            turns.append(
                Turn(
                    turn_number=turn_number,
                    user_content=user_content,
                    assistant_content=assistant_content,
                    raw_event_refs=list(turn_raw_events),
                    tool_calls=list(turn_tool_calls),
                    tool_results=list(turn_tool_results),
                    reasoning=reasoning,
                    attachments=list(turn_attachments),
                    source_files=[source_file] if source_file else [],
                    completeness={
                        "visible_text": "full",
                        "tool_calls": "full" if turn_tool_calls else "unavailable",
                        "tool_results": "full" if turn_tool_results else "unavailable",
                        "reasoning": (
                            "full"
                            if reasoning
                            else "metadata"
                            if has_reasoning_metadata(turn_raw_events)
                            else "unavailable"
                        ),
                        "attachments": "full" if turn_attachments else "unavailable",
                        "truncated": False,
                        "loss_reasons": list(completeness_loss),
                    },
                )
            )

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            msg_raw_events = msg.get("raw_event_refs", [])
            msg_tool_calls = msg.get("tool_calls", [])
            msg_tool_results = msg.get("tool_results", [])
            msg_reasoning = msg.get("reasoning", "")
            msg_attachments = msg.get("attachments", [])

            if role == "user":
                if assistant_content:
                    append_turn()
                    turn_number += 1
                    turn_raw_events = []
                    turn_tool_calls = []
                    turn_tool_results = []
                    turn_reasoning_parts = []
                    turn_attachments = []
                # 累积 user message 的 raw_event_refs
                if msg_raw_events:
                    turn_raw_events.extend(msg_raw_events)
                turn_tool_calls.extend(msg_tool_calls)
                turn_tool_results.extend(msg_tool_results)
                if msg_reasoning:
                    turn_reasoning_parts.append(msg_reasoning)
                turn_attachments.extend(msg_attachments)
                user_content = content
                assistant_content = ""
                completeness_loss = []

            elif role == "assistant":
                assistant_content += ("\n\n" if assistant_content else "") + content
                if msg_raw_events:
                    turn_raw_events.extend(msg_raw_events)
                turn_tool_calls.extend(msg_tool_calls)
                turn_tool_results.extend(msg_tool_results)
                if msg_reasoning:
                    turn_reasoning_parts.append(msg_reasoning)
                turn_attachments.extend(msg_attachments)
            else:
                if msg_raw_events:
                    turn_raw_events.extend(msg_raw_events)
                turn_tool_calls.extend(msg_tool_calls)
                turn_tool_results.extend(msg_tool_results)
                if msg_reasoning:
                    turn_reasoning_parts.append(msg_reasoning)
                turn_attachments.extend(msg_attachments)
                completeness_loss.append(
                    f"unknown_native_role:{role or 'empty'}"
                )

        # 保存最后一轮
        if (
            user_content
            or assistant_content
            or turn_raw_events
            or turn_tool_calls
            or turn_tool_results
            or turn_reasoning_parts
            or turn_attachments
        ):
            append_turn()

        return turns

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Codex 自定义标签"""
        return []
