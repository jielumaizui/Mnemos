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
import os
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.config import get_config
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

logger = logging.getLogger(__name__)


class GeminiCliSource(AgentSource):
    """Gemini CLI 数据源插件"""

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def model_tag(self) -> str:
        return "gemini-cli"

    @property
    def data_dir(self) -> Optional[Path]:
        # 测试覆盖支持
        if hasattr(self, '_override_data_dir'):
            return self._override_data_dir
        config = get_config()
        # 环境变量优先
        env_home = os.getenv("GEMINI_HOME")
        if env_home:
            p = Path(env_home).expanduser()
            if p.exists():
                return p

        # 标准路径
        for std in ("~/.gemini", "~/.config/gemini"):
            p = Path(std).expanduser()
            if p.exists():
                return p
        return None

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

        sessions_dir = base / "sessions"
        if not sessions_dir.exists():
            return []

        sessions = []
        for session_file in sessions_dir.rglob("*.jsonl"):
            sessions.append(SessionInfo(
                session_id=session_file.stem,
                source_path=session_file,
                working_dir=str(session_file.parent),
                mtime=session_file.stat().st_mtime,
            ))
        return sessions

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 Gemini CLI JSONL 会话文件为 Turn 列表"""
        turns = []
        messages = []

        try:
            with open(session_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        messages.append(msg)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            logger.warning(f"[GeminiCliSource] 读取失败 {session_path}: {e}")
            return turns

        user_content = ""
        assistant_content = ""
        turn_number = 0
        turn_meta: Dict[str, Any] = {}

        for msg in messages:
            role = msg.get("role", "").lower()
            content = msg.get("content", "")
            parts = msg.get("parts", [])

            # Gemini 格式可能是 parts 数组
            if parts and not content:
                texts = []
                for p in parts:
                    if isinstance(p, dict) and "text" in p:
                        texts.append(p["text"])
                    elif isinstance(p, str):
                        texts.append(p)
                content = "\n".join(texts)

            if role == "user":
                if assistant_content:
                    turns.append(Turn(
                        turn_number=turn_number,
                        user_content=user_content,
                        assistant_content=assistant_content,
                        metadata=turn_meta,
                    ))
                    turn_number += 1
                user_content = str(content)
                assistant_content = ""
                turn_meta = {}
            elif role in ("assistant", "model"):
                assistant_content = str(content)
                turn_meta = {
                    "timestamp": msg.get("timestamp", ""),
                }

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
        """Gemini CLI 自定义标签"""
        return []
