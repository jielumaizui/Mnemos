# -*- coding: utf-8 -*-
"""
HermesSource — Hermes Agent 同步插件

实现 AgentSource 接口，接入 SyncFramework。
替代旧的 MemorySyncBridge 依赖，直接解析 Hermes 的 JSONL 会话文件。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.config import get_config
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

logger = logging.getLogger(__name__)


class HermesSource(AgentSource):
    """Hermes 数据源插件"""

    @property
    def name(self) -> str:
        return "hermes"

    @property
    def model_tag(self) -> str:
        return "hermes"

    @property
    def data_dir(self) -> Optional[Path]:
        path = Path.home() / ".hermes"
        return path if path.exists() else None

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        return {
            "type": "watchdog",
            "events": ["created"],
            "debounce": 3.0,
            "recursive": True,
        }

    def discover_sessions(self) -> List[SessionInfo]:
        """发现所有可同步的 Hermes 会话"""
        base = self.data_dir
        if not base:
            return []

        sessions_dir = base / "sessions"
        if not sessions_dir.exists():
            return []

        sessions = []
        for jsonl_file in sessions_dir.glob("*.jsonl"):
            sessions.append(SessionInfo(
                session_id=jsonl_file.stem,
                source_path=jsonl_file,
                working_dir=str(jsonl_file.parent),
                mtime=jsonl_file.stat().st_mtime,
            ))
        return sessions

    def completeness_capabilities(self) -> Dict[str, Any]:
        return {
            "visible_text": True,
            "tool_calls": False,
            "tool_results": False,
            "reasoning": "available",
            "attachments": "unknown",
            "raw_files": True,
            "source_fidelity": "full",
        }

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 Hermes JSONL 会话文件为 Turn 列表 — P0-6 完整录入版"""
        turns = []
        try:
            with open(session_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            logger.warning(f"[HermesSource] 读取失败 {session_path}: {e}")
            return turns

        user_content = ""
        assistant_content = ""
        turn_meta: Dict[str, Any] = {}
        turn_number = 0
        turn_reasoning = ""
        turn_raw_events: List[Dict[str, Any]] = []
        completeness_loss: List[str] = []

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                completeness_loss.append("json_decode_error")
                continue

            role = msg.get("role", "")
            content = msg.get("content", "")

            # 跳过系统消息，但记录到 raw_event_refs
            if role in ("system", "_system"):
                turn_raw_events.append({"role": role, "event_type": "system", "raw": msg})
                continue

            if role == "user":
                if assistant_content:
                    turns.append(Turn(
                        turn_number=turn_number,
                        user_content=user_content,
                        assistant_content=assistant_content,
                        metadata=turn_meta,
                        reasoning=turn_reasoning,
                        raw_event_refs=turn_raw_events,
                        source_files=[str(session_path)],
                        completeness={
                            "visible_text": "full",
                            "tool_results": "unavailable",
                            "reasoning": "full" if turn_reasoning else "unavailable",
                            "attachments": "unavailable",
                            "truncated": False,
                            "loss_reasons": completeness_loss,
                        },
                    ))
                    turn_number += 1

                user_content = str(content) if not isinstance(content, str) else content
                assistant_content = ""
                turn_meta = {}
                turn_reasoning = ""
                turn_raw_events = []
                completeness_loss = []

            elif role == "assistant":
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
                            else:
                                # 未知 block 入 raw_event_refs
                                turn_raw_events.append({"role": "assistant", "event_type": ptype, "raw": part})
                                completeness_loss.append(f"assistant_unknown_block:{ptype}")
                        elif isinstance(part, str):
                            texts.append(part)
                    assistant_content = "\n\n".join(texts)
                else:
                    assistant_content = str(content)

        # 保存最后一轮
        if user_content or assistant_content:
            turns.append(Turn(
                turn_number=turn_number,
                user_content=user_content,
                assistant_content=assistant_content,
                metadata=turn_meta,
                reasoning=turn_reasoning,
                raw_event_refs=turn_raw_events,
                source_files=[str(session_path)],
                completeness={
                    "visible_text": "full",
                    "tool_results": "unavailable",
                    "reasoning": "full" if turn_reasoning else "unavailable",
                    "attachments": "unavailable",
                    "truncated": False,
                    "loss_reasons": completeness_loss,
                },
            ))

        return turns

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Hermes 自定义标签"""
        tags = []
        if turn.metadata.get("reasoning"):
            tags.append("has-reasoning=true")
        return tags
