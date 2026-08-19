# -*- coding: utf-8 -*-
"""
GeminiCliSource — Google Gemini CLI 同步插件

实现 AgentSource 接口，接入 SyncFramework。
Gemini CLI (google-gemini-cli) 的会话记录通常保存在用户主目录下。

数据位置：
- macOS: ~/.gemini/sessions/
- Linux: ~/.config/gemini/sessions/
- 环境变量 GEMINI_HOME 可覆盖
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

from core.sync_framework.agent_source import (
    NativeSourceContractError,
    SessionInfo,
    Turn,
)
from core.ops.durable_io import open_native_text

from integrations.sources.base import BaseAgentSource, stable_path_session_id

logger = logging.getLogger(__name__)


def _load_messages(session_path: Path) -> List[Dict[str, Any]]:
    """从 JSONL 会话文件安全读取消息列表。"""
    messages: List[Dict[str, Any]] = []
    try:
        with open_native_text(session_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    decoded = json.loads(line)
                except json.JSONDecodeError:
                    raise NativeSourceContractError(
                        "native_gemini_jsonl_decode_failed"
                    ) from None
                messages.append(
                    decoded
                    if isinstance(decoded, dict)
                    else {"_mnemos_raw_native_record": decoded}
                )
    except (OSError, UnicodeError):
        raise NativeSourceContractError(
            "native_gemini_transcript_read_failed"
        ) from None
    return messages


def _extract_gemini_content(
    msg: Dict[str, Any],
) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """从单条 Gemini 消息提取内容、tool_call、tool_result 与原始事件。"""
    raw_message = msg.get("_mnemos_raw_native_record", msg)
    role = str(msg.get("role") or "").lower()
    content = msg.get("content", "")
    parts = msg.get("parts", [])
    native_ref = {
        "role": role,
        "event_type": "native_message",
        "raw": raw_message,
    }

    if not parts or content:
        loss = [] if role else ["unknown_role:empty"]
        return str(content), [], [], [native_ref], loss

    texts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    tool_results: List[Dict[str, Any]] = []
    raw_events: List[Dict[str, Any]] = [native_ref]
    loss_reasons: List[str] = []

    for p in parts:
        if isinstance(p, dict):
            if "text" in p:
                texts.append(p["text"])
            elif "function_call" in p:
                fc = p["function_call"]
                tool_calls.append(
                    {
                        "name": fc.get("name", "unknown"),
                        "input": fc.get("args", {}),
                    }
                )
            elif "function_response" in p:
                fr = p["function_response"]
                tool_results.append(
                    {
                        "tool_use_id": fr.get("id", ""),
                        "content": str(fr.get("response", "")),
                    }
                )
            else:
                # 非 text 块入 raw_event_refs
                raw_events.append({"role": role, "event_type": "part", "raw": p})
                loss_reasons.append(f"unknown_part:{list(p.keys())}")
        elif isinstance(p, str):
            texts.append(p)

    return "\n".join(texts), tool_calls, tool_results, raw_events, loss_reasons


def _build_gemini_turn(
    turn_number: int,
    user_content: str,
    assistant_content: str,
    turn_meta: Dict[str, Any],
    turn_tool_calls: List[Dict[str, Any]],
    turn_tool_results: List[Dict[str, Any]],
    turn_raw_events: List[Dict[str, Any]],
    completeness_loss: List[str],
    session_path: Path,
) -> Turn:
    """构造 Gemini CLI Turn，统一 completeness 与 raw_event_refs。"""
    return Turn(
        turn_number=turn_number,
        user_content=user_content,
        assistant_content=assistant_content,
        metadata=turn_meta,
        tool_calls=turn_tool_calls,
        tool_results=turn_tool_results,
        raw_event_refs=turn_raw_events,
        source_files=[str(session_path)],
        completeness={
            "visible_text": "full",
            "tool_results": "full" if turn_tool_results else "unavailable",
            "reasoning": "unavailable",
            "attachments": "unavailable",
            "truncated": False,
            "loss_reasons": completeness_loss,
        },
    )


class GeminiCliSource(BaseAgentSource):
    """Gemini CLI 数据源插件"""

    _cap_tool_calls = True
    _cap_tool_results = True
    _cap_reasoning = "unknown"
    _cap_attachments = "unknown"
    _cap_source_fidelity = "full"

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def model_tag(self) -> str:
        return "gemini-cli"

    @property
    def data_dir(self) -> Optional[Path]:
        return self._resolve_data_dir_with_env(
            "GEMINI_HOME",
            [Path.home() / ".gemini", Path.home() / ".config" / "gemini"],
        )

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        return {
            "type": "watchdog",
            "events": ["modified", "created"],
            "debounce": 5.0,
            "recursive": True,
        }

    def discover_sessions(self) -> List[SessionInfo]:
        """发现所有可同步的 Gemini CLI 会话"""
        base = self.data_dir
        if not base:
            return []
        sessions_dir = self._sessions_dir(base)
        sessions = self._discover_by_glob(sessions_dir, "*.jsonl")
        for session in sessions:
            canonical_id = stable_path_session_id(
                "gemini",
                sessions_dir,
                session.source_path,
                native_id=session.session_id,
            )
            session.canonical_session_id = canonical_id
            session.session_aliases = [session.session_id]
            session.source_kind = "jsonl"
        return sessions

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 Gemini CLI JSONL 会话文件为 Turn 列表 — P0-6 完整录入版"""
        turns: List[Turn] = []
        messages = _load_messages(session_path)

        user_content = ""
        assistant_content = ""
        turn_number = 0
        turn_meta: Dict[str, Any] = {}
        turn_tool_calls: List[Dict[str, Any]] = []
        turn_tool_results: List[Dict[str, Any]] = []
        turn_raw_events: List[Dict[str, Any]] = []
        completeness_loss: List[str] = []

        for msg in messages:
            role = msg.get("role", "").lower()
            content, new_calls, new_results, new_raw, new_loss = _extract_gemini_content(
                msg
            )

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
                    turns.append(
                        _build_gemini_turn(
                            turn_number,
                            user_content,
                            assistant_content,
                            turn_meta,
                            turn_tool_calls,
                            turn_tool_results,
                            turn_raw_events,
                            completeness_loss,
                            session_path,
                        )
                    )
                    turn_number += 1
                user_content = content
                assistant_content = ""
                turn_meta = {}
                turn_tool_calls = list(new_calls)
                turn_tool_results = list(new_results)
                turn_raw_events = leading_refs + list(new_raw)
                completeness_loss = leading_loss + list(new_loss)
            elif role in ("assistant", "model"):
                turn_tool_calls.extend(new_calls)
                turn_tool_results.extend(new_results)
                turn_raw_events.extend(new_raw)
                completeness_loss.extend(new_loss)
                assistant_content = content
                turn_meta = {
                    "timestamp": msg.get("timestamp", ""),
                }
            else:
                turn_tool_calls.extend(new_calls)
                turn_tool_results.extend(new_results)
                turn_raw_events.extend(new_raw)
                completeness_loss.extend(
                    [*new_loss, f"unknown_role:{role or 'empty'}"]
                )

        # 保存最后一轮
        if (
            user_content
            or assistant_content
            or turn_tool_calls
            or turn_tool_results
            or turn_raw_events
        ):
            turns.append(
                _build_gemini_turn(
                    turn_number,
                    user_content,
                    assistant_content,
                    turn_meta,
                    turn_tool_calls,
                    turn_tool_results,
                    turn_raw_events,
                    completeness_loss,
                    session_path,
                )
            )

        return turns

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Gemini CLI 自定义标签"""
        return []
