# -*- coding: utf-8 -*-
"""
KimiSource — Kimi Agent 同步插件

实现 AgentSource 接口，接入 SyncFramework。
支持 Kimi 的归档机制：context.jsonl + context_1.jsonl 等。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.config import get_config
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

logger = logging.getLogger(__name__)


class KimiSource(AgentSource):
    """Kimi 数据源插件"""

    @property
    def name(self) -> str:
        return "kimi"

    @property
    def model_tag(self) -> str:
        return "kimi-k2.5"

    @property
    def data_dir(self) -> Optional[Path]:
        path = Path.home() / ".kimi"
        return path if path.exists() else None

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        return {
            "type": "hybrid",
            "events": ["modified", "created"],
            "debounce": 5.0,
            "recursive": True,
        }

    def discover_sessions(self) -> List[SessionInfo]:
        """发现所有可同步的 Kimi 会话"""
        base = self.data_dir
        if not base:
            return []

        sessions_dir = base / "sessions"
        if not sessions_dir.exists():
            return []

        sessions = []
        for context_file in sessions_dir.rglob("context.jsonl"):
            session_dir = context_file.parent
            workspace_dir = session_dir.parent
            sessions.append(SessionInfo(
                session_id=session_dir.name,
                source_path=context_file,
                working_dir=str(workspace_dir),
                mtime=context_file.stat().st_mtime,
            ))
        return sessions

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 Kimi 会话文件（含归档文件）为 Turn 列表"""
        all_messages = self._read_all_context_files(session_path.parent)
        return self._pair_messages_to_turns(all_messages)

    def _read_all_context_files(self, session_dir: Path) -> List[Dict[str, Any]]:
        """读取所有 context*.jsonl 文件（包括归档），按顺序合并"""
        context_files = sorted(session_dir.glob("context*.jsonl"))
        all_messages = []

        for cf in context_files:
            try:
                with open(cf, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                            all_messages.append(msg)
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.warning(f"[KimiSource] 读取失败 {cf}: {e}")

        return all_messages

    def _pair_messages_to_turns(self, messages: List[Dict[str, Any]]) -> List[Turn]:
        """将消息列表配对为 Turn 列表"""
        turns = []
        user_content = ""
        assistant_content = ""
        turn_meta: Dict[str, Any] = {}
        turn_number = 0

        for msg in messages:
            role = msg.get("role", "")

            # 跳过系统消息
            if role in ("_system_prompt", "_checkpoint", "_usage", "system"):
                continue

            if role == "user":
                # 如果已有 assistant 内容，保存上一轮
                if assistant_content:
                    turns.append(Turn(
                        turn_number=turn_number,
                        user_content=user_content,
                        assistant_content=assistant_content,
                        metadata=turn_meta,
                    ))
                    turn_number += 1

                # 处理列表格式 [{"type": "text", "text": "..."}]
                raw = msg.get("content", "")
                if isinstance(raw, list):
                    texts = [
                        item.get("text", "")
                        for item in raw
                        if isinstance(item, dict) and item.get("type") == "text"
                    ]
                    user_content = "\n".join(texts)
                else:
                    user_content = str(raw)

                assistant_content = ""
                turn_meta = {}

            elif role == "assistant":
                parts = msg.get("content", [])
                texts = []
                reasoning = ""

                if isinstance(parts, list):
                    for p in parts:
                        if not isinstance(p, dict):
                            continue
                        ptype = p.get("type", "")
                        if ptype == "text":
                            texts.append(p.get("text", ""))
                        elif ptype == "think":
                            t = p.get("think", "")
                            if t:
                                reasoning = t[:2000]
                elif isinstance(parts, str):
                    texts.append(parts)

                assistant_content = "\n\n".join(texts)
                if reasoning:
                    turn_meta["reasoning"] = reasoning

            elif role == "tool":
                # tool 结果追加到 assistant_content
                tool_content = msg.get("content", "")
                if isinstance(tool_content, list):
                    tool_texts = [
                        item.get("text", "")
                        for item in tool_content
                        if isinstance(item, dict) and item.get("type") == "text"
                    ]
                    tool_content = "\n".join(tool_texts)
                if tool_content:
                    assistant_content += f"\n\n[TOOL_RESULT]{tool_content}[/TOOL_RESULT]"

        # 保存最后一轮
        if user_content or assistant_content:
            turns.append(Turn(
                turn_number=turn_number,
                user_content=user_content,
                assistant_content=assistant_content,
                metadata=turn_meta,
            ))

        return turns

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Kimi 自定义标签"""
        tags = []
        if turn.metadata.get("reasoning"):
            tags.append("has-reasoning=true")
        return tags
