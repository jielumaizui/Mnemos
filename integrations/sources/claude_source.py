# -*- coding: utf-8 -*-
"""
ClaudeSource — Claude Code Agent 同步插件

实现 AgentSource 接口，接入 SyncFramework。

TODO: 当前为骨架实现，需从 claude_live_sync.py 迁移以下逻辑：
  - JSONL 消息解析（_standardize_message）
  - 增量同步（offset 追踪）
  - 工具调用/thinking 内容提取

迁移完成后，claude_live_sync.py 将退化为兼容层（直接委托给 SyncEngine）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.config import get_config
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

logger = logging.getLogger(__name__)


class ClaudeSource(AgentSource):
    """Claude Code 数据源插件"""

    @property
    def name(self) -> str:
        return "claude"

    @property
    def model_tag(self) -> str:
        return "claude-code"

    @property
    def data_dir(self) -> Optional[Path]:
        config = get_config()
        return config.claude_data_dir if hasattr(config, "claude_data_dir") else None

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        return {
            "type": "watchdog",
            "events": ["modified"],
            "debounce": 5.0,
            "recursive": True,
        }

    def discover_sessions(self) -> List[SessionInfo]:
        """发现所有可同步的 Claude 会话"""
        base = self.data_dir
        if not base:
            return []

        projects_dir = base / "projects"
        if not projects_dir.exists():
            return []

        sessions = []
        for jsonl_file in projects_dir.rglob("*.jsonl"):
            sessions.append(SessionInfo(
                session_id=jsonl_file.stem,
                source_path=jsonl_file,
                working_dir=str(jsonl_file.parent),
                mtime=jsonl_file.stat().st_mtime,
            ))
        return sessions

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 JSONL 会话文件为 Turn 列表"""
        turns = []
        try:
            with open(session_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            logger.warning(f"[ClaudeSource] 读取失败 {session_path}: {e}")
            return turns

        current_user = ""
        current_assistant = ""
        turn_number = 0

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            standardized = self._standardize_message(msg)
            if not standardized:
                continue

            role = standardized.get("role", "")
            content = standardized.get("content", "")

            if role == "user":
                # 如果已有 assistant 内容，保存上一轮
                if current_assistant:
                    turns.append(Turn(
                        turn_number=turn_number,
                        user_content=current_user,
                        assistant_content=current_assistant,
                        timestamp=standardized.get("timestamp"),
                        metadata={
                            "tool_calls": standardized.get("tool_calls", []),
                            "tool_results": standardized.get("tool_results", []),
                            "reasoning": standardized.get("reasoning", ""),
                        },
                    ))
                    turn_number += 1
                    current_user = content
                    current_assistant = ""
                else:
                    current_user = content
            elif role == "assistant":
                current_assistant = content

        # 保存最后一轮
        if current_user or current_assistant:
            turns.append(Turn(
                turn_number=turn_number,
                user_content=current_user,
                assistant_content=current_assistant,
                metadata={},
            ))

        return turns

    def _standardize_message(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """标准化 Claude Code JSONL 消息格式

        TODO: 当前为简化版，需从 claude_live_sync.py 完整迁移。
        """
        if not isinstance(msg, dict):
            return None

        message_data = msg.get("message", msg)
        role = message_data.get("role", "")
        if not role:
            role = msg.get("type", "")

        raw_content = message_data.get("content", "")
        if isinstance(raw_content, list):
            content_parts = []
            for part in raw_content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        content_parts.append(part.get("text", ""))
                    elif "content" in part:
                        content_parts.append(str(part["content"]))
            content = "\n".join(content_parts)
        else:
            content = str(raw_content)

        if not role or not content:
            return None

        return {
            "role": role,
            "content": content,
            "timestamp": msg.get("timestamp", ""),
        }

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Claude 自定义标签"""
        tags = []
        meta = turn.metadata
        if meta.get("tool_calls"):
            tags.append("has-tools=true")
        if meta.get("reasoning"):
            tags.append("has-reasoning=true")
        return tags
