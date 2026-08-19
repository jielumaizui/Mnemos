# -*- coding: utf-8 -*-
"""
HermesSource — Hermes Agent 同步插件

实现 AgentSource 接口，接入 SyncFramework。
替代旧的 MemorySyncBridge 依赖，直接解析 Hermes 的 JSONL 会话文件。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

from core.sync_framework.agent_source import (
    NativeSourceContractError,
    SessionInfo,
    SessionParseResult,
    Turn,
)
from core.ops.durable_io import open_native_text, read_native_bytes

from integrations.sources.base import (
    BaseAgentSource,
    attach_native_container_residual,
    native_path_kind,
    stable_path_session_id,
)

logger = logging.getLogger(__name__)


def _load_json_session(session_path: Path) -> Tuple[List[Any], str, str, Dict[str, Any]]:
    """加载 Hermes 新版会话及未投影的顶层容器字段。"""
    try:
        with open_native_text(session_path) as handle:
            data = json.load(handle)
    except (OSError, IOError, json.JSONDecodeError):
        raise NativeSourceContractError("native_hermes_json_read_failed") from None

    messages = data.get("messages", [])
    if not isinstance(messages, list):
        raise NativeSourceContractError("native_hermes_messages_invalid")

    session_id = data.get("session_id", session_path.stem)
    model = data.get("model", "")
    return messages, session_id, model, data


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: List[str] = []
        for item in content:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    texts.append(str(item.get("text", "")))
                elif "text" in item:
                    texts.append(str(item.get("text", "")))
        return "\n\n".join(text for text in texts if text)
    return str(content or "")


def _extract_hermes_tool_calls(msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    raw_calls = msg.get("tool_calls", [])
    if isinstance(raw_calls, list):
        for call in raw_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function", {})
            function = function if isinstance(function, dict) else {}
            calls.append(
                {
                    "id": call.get("id") or call.get("call_id") or "",
                    "name": function.get("name") or call.get("name", ""),
                    "arguments": function.get("arguments") or call.get("arguments", {}),
                    "raw": call,
                }
            )

    content = msg.get("content", [])
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type", "")
            if ptype in {"tool_call", "tool_use", "function_call"}:
                calls.append(
                    {
                        "id": part.get("id") or part.get("tool_call_id") or "",
                        "name": part.get("name", ""),
                        "arguments": part.get("input") or part.get("arguments", {}),
                        "raw": part,
                    }
                )
    return calls


def _extract_hermes_tool_result(msg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": msg.get("tool_call_id") or msg.get("id") or "",
        "name": msg.get("name", ""),
        "output": _text_from_content(msg.get("content", "")),
        "raw": msg,
    }


def _extract_hermes_assistant_content(
    msg: Dict[str, Any],
) -> Tuple[str, str, str, List[Dict[str, Any]], List[str]]:
    """从 assistant 消息提取内容、reasoning 与原始事件。

    Returns:
        (assistant_content, content_reasoning, msg_reasoning, raw_events, loss_reasons)
    """
    content = msg.get("content", "")
    msg_reasoning = msg.get("reasoning", "")
    content_reasoning = ""
    raw_events: List[Dict[str, Any]] = []
    loss_reasons: List[str] = []

    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict):
                ptype = part.get("type", "")
                if ptype == "text":
                    texts.append(part.get("text", ""))
                elif ptype == "thinking":
                    # P0-6: 不再截断 thinking
                    content_reasoning = part.get("thinking", "")
                elif ptype in {"tool_call", "tool_use", "function_call"}:
                    raw_events.append({"role": "assistant", "event_type": ptype, "raw": part})
                else:
                    # 未知 block 入 raw_event_refs
                    raw_events.append({"role": "assistant", "event_type": ptype, "raw": part})
                    loss_reasons.append(f"assistant_unknown_block:{ptype}")
            elif isinstance(part, str):
                texts.append(part)
        assistant_content = "\n\n".join(texts)
    else:
        assistant_content = str(content)

    if msg_reasoning:
        msg_reasoning = msg_reasoning if isinstance(msg_reasoning, str) else str(msg_reasoning)

    return assistant_content, content_reasoning, msg_reasoning, raw_events, loss_reasons


def _message_timestamp(msg: Dict[str, Any]) -> Optional[str]:
    """Extract a stable message timestamp across Hermes JSON/JSONL variants."""
    for key in ("timestamp", "created_at", "createdAt", "time", "ts"):
        value = msg.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, (int, float)):
            epoch = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
        return str(value)
    return None


def _flush_hermes_turn(
    turns: List[Turn],
    turn_number: int,
    user_content: str,
    assistant_content: str,
    turn_meta: Dict[str, Any],
    turn_reasoning: str,
    turn_tool_calls: List[Dict[str, Any]],
    turn_tool_results: List[Dict[str, Any]],
    turn_raw_events: List[Dict[str, Any]],
    completeness_loss: List[str],
    session_path: Path,
) -> int:
    """将当前轮次追加到 turns；空轮次则跳过，返回下一个 turn_number。"""
    if not any(
        (
            user_content,
            assistant_content,
            turn_reasoning,
            turn_tool_calls,
            turn_tool_results,
            turn_raw_events,
        )
    ):
        return turn_number

    turns.append(
        Turn(
            turn_number=turn_number,
            user_content=user_content,
            assistant_content=assistant_content,
            timestamp=turn_meta.get("timestamp"),
            metadata=turn_meta,
            reasoning=turn_reasoning,
            tool_calls=turn_tool_calls,
            tool_results=turn_tool_results,
            raw_event_refs=turn_raw_events,
            source_files=[str(session_path)],
            completeness={
                "visible_text": "full",
                "tool_calls": "full" if turn_tool_calls else "unavailable",
                "tool_results": "full" if turn_tool_results else "unavailable",
                "reasoning": "full" if turn_reasoning else "unavailable",
                "attachments": "unavailable",
                "truncated": False,
                "loss_reasons": completeness_loss,
            },
        )
    )
    return turn_number + 1


class HermesSource(BaseAgentSource):  # noqa: Vulture - loaded by SourceRegistry builtin reflection.
    """Hermes 数据源插件"""

    _cap_tool_calls = True
    _cap_tool_results = True
    _cap_reasoning = True
    _cap_source_fidelity = "full"
    _cap_memory_scope = "unknown_host_memory_plus_local_sessions"
    _cap_host_memory_default = "host_dependent_unknown"
    _cap_host_memory_effect = (
        "Mnemos treats Hermes memory as prompt context only when present; local sessions remain capture source"
    )
    _cap_transcript_kind = "local_session_json_or_jsonl"
    _cap_compression = "raw_or_host_serialized_session"

    _default_extra_tags = ["has-reasoning=true"]

    @property
    def name(self) -> str:
        return "hermes"

    @property
    def model_tag(self) -> str:
        return "hermes"

    @property
    def data_dir(self) -> Optional[Path]:
        """返回 Hermes 会话文件所在目录。

        新版 Hermes 把会话存为 ``~/.hermes/sessions/*.json``。将 data_dir 直接指向
        sessions 子目录，可使 watchdog 只监听真正会变化的会话文件，避免被
        logs/profiles/cron 等高频文件持续触发。
        """
        return self._resolve_data_dir_with_env(
            "HERMES_HOME",
            [Path.home() / ".hermes"],
            subdir="sessions",
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
        """发现所有可同步的 Hermes 会话。

        JSONL and JSON are both declared Hermes session formats; sessions.json is an index.
        """
        base = self.data_dir
        if not base:
            return []
        sessions_dir = self._sessions_dir(base)
        if native_path_kind(sessions_dir) == "missing":
            return []

        sessions: List[SessionInfo] = []
        skip_names = {"sessions.json"}
        for source_kind, pattern in (("jsonl", "*.jsonl"), ("json", "*.json")):
            for info in self._discover_by_glob(
                sessions_dir,
                pattern,
                recursive=False,
                skip_names=skip_names,
            ):
                info.canonical_session_id = stable_path_session_id(
                    f"hermes-{source_kind}",
                    sessions_dir,
                    info.source_path,
                    native_id=info.session_id,
                )
                info.session_aliases = [info.source_path.name]
                info.source_kind = source_kind
                sessions.append(info)
        return sessions

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 Hermes 会话文件为 Turn 列表。

        根据扩展名自动选择解析器：*.jsonl 为旧版逐行格式，*.json 为新版完整会话对象。
        """
        if session_path.suffix.lower() == ".json":
            return self._parse_turns_from_json(session_path)
        return self._parse_turns_from_jsonl(session_path)

    def parse_session_result(self, session_info: SessionInfo) -> SessionParseResult:
        """Classify sensitive provider failure artifacts without treating them as empty."""

        path = session_info.source_path
        if path.suffix.lower() == ".json":
            try:
                raw = read_native_bytes(path)
                payload = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise NativeSourceContractError("native_hermes_json_read_failed") from None
            if self._is_provider_request_failure_artifact(payload):
                return SessionParseResult(
                    turns=(),
                    disposition="evidence_excluded",
                    reason_code="provider_request_failure_artifact",
                )
        turns = tuple(self.parse_session(session_info) or [])
        return SessionParseResult(
            turns=turns,
            disposition="parsed" if turns else "typed_empty",
            reason_code="native_turns_parsed" if turns else "valid_empty_native_session",
        )

    @staticmethod
    def _is_provider_request_failure_artifact(payload: Any) -> bool:
        if not isinstance(payload, dict) or "messages" in payload:
            return False
        if not {"error", "reason", "request", "session_id", "timestamp"} <= set(payload):
            return False
        request = payload.get("request")
        return bool(
            isinstance(request, dict)
            and {"method", "url", "headers", "body"} <= set(request)
            and isinstance(request.get("headers"), dict)
        )

    def _parse_turns_from_jsonl(self, session_path: Path) -> List[Turn]:
        """解析 Hermes JSONL 会话文件为 Turn 列表 — P0-6 完整录入版"""
        turns: List[Turn] = []
        try:
            with open_native_text(session_path) as f:
                lines = f.readlines()
        except (OSError, IOError):
            raise NativeSourceContractError(
                "native_hermes_jsonl_read_failed"
            ) from None

        user_content = ""
        assistant_content = ""
        turn_meta: Dict[str, Any] = {}
        turn_number = 0
        turn_reasoning = ""
        turn_tool_calls: List[Dict[str, Any]] = []
        turn_tool_results: List[Dict[str, Any]] = []
        turn_raw_events: List[Dict[str, Any]] = []
        completeness_loss: List[str] = []

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                raise NativeSourceContractError(
                    "native_hermes_jsonl_decode_failed"
                ) from None

            if not isinstance(msg, dict):
                turn_raw_events.append(
                    {
                        "role": "",
                        "event_type": "non_object_native_record",
                        "raw": msg,
                    }
                )
                completeness_loss.append("message_not_dict")
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            timestamp = _message_timestamp(msg)

            # 跳过系统消息，但记录到 raw_event_refs
            if role in ("system", "_system"):
                turn_raw_events.append({"role": role, "event_type": "system", "raw": msg})
                continue

            if role == "user":
                has_open_turn = bool(
                    user_content
                    or assistant_content
                    or turn_tool_calls
                    or turn_tool_results
                )
                leading_refs = [] if has_open_turn else list(turn_raw_events)
                leading_loss = [] if has_open_turn else list(completeness_loss)
                if has_open_turn:
                    turn_number = _flush_hermes_turn(
                        turns,
                        turn_number,
                        user_content,
                        assistant_content,
                        turn_meta,
                        turn_reasoning,
                        turn_tool_calls,
                        turn_tool_results,
                        turn_raw_events,
                        completeness_loss,
                        session_path,
                    )

                user_content = content if isinstance(content, str) else str(content)
                assistant_content = ""
                turn_meta = {"timestamp": timestamp} if timestamp else {}
                turn_reasoning = ""
                turn_tool_calls = []
                turn_tool_results = []
                turn_raw_events = leading_refs + [
                    {"role": "user", "event_type": "native_message", "raw": msg}
                ]
                completeness_loss = leading_loss

            elif role == "assistant":
                turn_raw_events.append(
                    {"role": "assistant", "event_type": "native_message", "raw": msg}
                )
                if timestamp and "timestamp" not in turn_meta:
                    turn_meta["timestamp"] = timestamp
                if isinstance(content, list):
                    texts = []
                    for part in content:
                        if isinstance(part, dict):
                            ptype = part.get("type", "")
                            if ptype == "text":
                                texts.append(part.get("text", ""))
                            elif ptype == "thinking":
                                # P0-6: 不再截断 thinking
                                turn_reasoning = part.get("thinking", "")
                                turn_meta["reasoning"] = turn_reasoning
                            elif ptype in {"tool_call", "tool_use", "function_call"}:
                                turn_tool_calls.extend(
                                    _extract_hermes_tool_calls({"content": [part]})
                                )
                                turn_raw_events.append(
                                    {"role": "assistant", "event_type": ptype, "raw": part}
                                )
                            else:
                                turn_raw_events.append(
                                    {"role": "assistant", "event_type": ptype, "raw": part}
                                )
                                completeness_loss.append(f"assistant_unknown_block:{ptype}")
                        elif isinstance(part, str):
                            texts.append(part)
                    assistant_content = "\n\n".join(texts)
                else:
                    assistant_content = str(content)
                if msg.get("tool_calls"):
                    turn_tool_calls.extend(
                        _extract_hermes_tool_calls({"tool_calls": msg.get("tool_calls")})
                    )

            elif role == "tool":
                turn_tool_results.append(_extract_hermes_tool_result(msg))
                turn_raw_events.append({"role": "tool", "event_type": "tool_result", "raw": msg})
            else:
                turn_raw_events.append({"role": role, "event_type": "unknown", "raw": msg})
                completeness_loss.append(f"unknown_role:{role}")

        # 保存最后一轮
        if (
            user_content
            or assistant_content
            or turn_reasoning
            or turn_tool_calls
            or turn_tool_results
            or turn_raw_events
        ):
            _flush_hermes_turn(
                turns,
                turn_number,
                user_content,
                assistant_content,
                turn_meta,
                turn_reasoning,
                turn_tool_calls,
                turn_tool_results,
                turn_raw_events,
                completeness_loss,
                session_path,
            )

        return turns

    def _parse_turns_from_json(self, session_path: Path) -> List[Turn]:
        """解析 Hermes 新版 *.json 完整会话文件为 Turn 列表。"""
        messages, session_id, model, container = _load_json_session(session_path)

        turns: List[Turn] = []
        user_content = ""
        assistant_content = ""
        turn_meta: Dict[str, Any] = {}
        turn_number = 0
        turn_reasoning = ""
        turn_tool_calls: List[Dict[str, Any]] = []
        turn_tool_results: List[Dict[str, Any]] = []
        turn_raw_events: List[Dict[str, Any]] = []
        completeness_loss: List[str] = []

        for msg in messages:
            if not isinstance(msg, dict):
                turn_raw_events.append(
                    {
                        "role": "",
                        "event_type": "non_object_native_record",
                        "raw": msg,
                    }
                )
                completeness_loss.append("message_not_dict")
                continue

            role = msg.get("role", "")
            content = msg.get("content", "")
            timestamp = _message_timestamp(msg)

            if role in ("system", "_system"):
                turn_raw_events.append({"role": role, "event_type": "system", "raw": msg})
                continue

            if role == "user":
                has_open_turn = bool(
                    user_content
                    or assistant_content
                    or turn_tool_calls
                    or turn_tool_results
                )
                existing_refs = [] if has_open_turn else list(turn_raw_events)
                existing_loss = [] if has_open_turn else list(completeness_loss)
                if has_open_turn:
                    turn_number = _flush_hermes_turn(
                        turns,
                        turn_number,
                        user_content,
                        assistant_content,
                        turn_meta,
                        turn_reasoning,
                        turn_tool_calls,
                        turn_tool_results,
                        turn_raw_events,
                        completeness_loss,
                        session_path,
                    )

                user_content = content if isinstance(content, str) else str(content)
                assistant_content = ""
                turn_meta = {
                    "session_id": session_id,
                    "model": model,
                    "timestamp": timestamp,
                }
                turn_reasoning = ""
                turn_tool_calls = []
                turn_tool_results = []
                turn_raw_events = existing_refs + [
                    {"role": "user", "event_type": "native_message", "raw": msg}
                ]
                completeness_loss = existing_loss

            elif role == "assistant":
                turn_raw_events.append(
                    {"role": "assistant", "event_type": "native_message", "raw": msg}
                )
                (
                    assistant_content,
                    content_reasoning,
                    msg_reasoning,
                    new_raw_events,
                    new_loss,
                ) = _extract_hermes_assistant_content(msg)
                turn_raw_events.extend(new_raw_events)
                completeness_loss.extend(new_loss)
                turn_tool_calls.extend(_extract_hermes_tool_calls(msg))

                if msg_reasoning:
                    turn_reasoning = msg_reasoning
                elif content_reasoning:
                    turn_reasoning = content_reasoning
                if turn_reasoning:
                    turn_meta["reasoning"] = turn_reasoning

            elif role == "tool":
                turn_tool_results.append(_extract_hermes_tool_result(msg))
                turn_raw_events.append({"role": "tool", "event_type": "tool_result", "raw": msg})

            else:
                turn_raw_events.append({"role": role, "event_type": "unknown", "raw": msg})
                completeness_loss.append(f"unknown_role:{role}")

        _flush_hermes_turn(
            turns,
            turn_number,
            user_content,
            assistant_content,
            turn_meta,
            turn_reasoning,
            turn_tool_calls,
            turn_tool_results,
            turn_raw_events,
            completeness_loss,
            session_path,
        )

        return attach_native_container_residual(
            turns,
            container,
            consumed_keys={"messages", "session_id", "model"},
            source_name=self.name,
        )

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Hermes 自定义标签"""
        tags = []
        if turn.metadata.get("reasoning"):
            tags.append("has-reasoning=true")
        return tags
