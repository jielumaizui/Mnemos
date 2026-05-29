# -*- coding: utf-8 -*-
"""
WindsurfSource — Windsurf (Codeium) IDE 同步插件

实现 AgentSource 接口，接入 SyncFramework。
Windsurf 基于 VS Code，聊天记录可能保存在用户主目录下。

数据位置（调研中）：
- macOS: ~/.windsurf/ 或 ~/Library/Application Support/Windsurf/
- Linux: ~/.config/Windsurf/ 或 ~/.windsurf/
- Windows: %APPDATA%/Windsurf/

⚠️ 当前实现基于公开信息推断，需实际 Windsurf 环境验证。
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


class WindsurfSource(AgentSource):
    """Windsurf 数据源插件

    ⚠️ EXPERIMENTAL: 当前实现基于公开信息推断，尚未在真实 Windsurf 环境验证。
    """

    experimental = True

    @property
    def name(self) -> str:
        return "windsurf"

    @property
    def model_tag(self) -> str:
        return "windsurf"

    @property
    def data_dir(self) -> Optional[Path]:
        # 测试覆盖支持
        if hasattr(self, '_override_data_dir'):
            return self._override_data_dir
        config = get_config()
        # 环境变量优先
        env = os.getenv("WINDSURF_HOME")
        if env:
            p = Path(env).expanduser()
            if p.exists():
                return p

        # 标准路径
        if sys.platform == "darwin":
            for p in [
                Path.home() / "Library" / "Application Support" / "Windsurf",
                Path.home() / ".windsurf",
            ]:
                if p.exists():
                    return p
        elif sys.platform == "linux":
            for p in [
                Path.home() / ".config" / "Windsurf",
                Path.home() / ".windsurf",
            ]:
                if p.exists():
                    return p
        elif sys.platform == "win32":
            p = Path.home() / "AppData" / "Roaming" / "Windsurf"
            if p.exists():
                return p
        return None

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        return {
            "type": "polling",
            "interval": 3600,
            "pattern": "*.json",
        }

    def discover_sessions(self) -> List[SessionInfo]:
        """发现 Windsurf 会话文件"""
        base = self.data_dir
        if not base:
            return []

        sessions = []

        # 查找 workspaceStorage
        workspace_dir = base / "workspaceStorage"
        if workspace_dir.exists():
            for ws_dir in workspace_dir.iterdir():
                if not ws_dir.is_dir():
                    continue
                for candidate in ["chat_history.json", "session.json", "conversations.json", "history.json"]:
                    cf = ws_dir / candidate
                    if cf.exists():
                        sessions.append(SessionInfo(
                            session_id=ws_dir.name,
                            source_path=cf,
                            working_dir=str(ws_dir),
                            mtime=cf.stat().st_mtime,
                        ))
                        break

        # 查找全局存储
        global_dir = base / "User" / "globalStorage"
        if global_dir.exists():
            for candidate in ["chat_history.json", "conversations.json", "history.json"]:
                cf = global_dir / candidate
                if cf.exists():
                    sessions.append(SessionInfo(
                        session_id=f"windsurf-global-{candidate}",
                        source_path=cf,
                        working_dir=str(global_dir),
                        mtime=cf.stat().st_mtime,
                    ))

        return sessions

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 Windsurf 会话文件为 Turn 列表"""
        turns = []
        try:
            with open(session_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"[WindsurfSource] 解析失败 {session_path}: {e}")
            return turns

        messages = []
        if isinstance(data, list):
            messages = data
        elif isinstance(data, dict):
            messages = data.get("conversations") or data.get("messages") or data.get("history") or []

        user_content = ""
        assistant_content = ""
        turn_number = 0

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "").lower()
            content = msg.get("content", "")
            if isinstance(content, list):
                texts = [p.get("text", "") for p in content if isinstance(p, dict)]
                content = "\n".join(texts)

            if role == "user":
                if assistant_content:
                    turns.append(Turn(
                        turn_number=turn_number,
                        user_content=user_content,
                        assistant_content=assistant_content,
                    ))
                    turn_number += 1
                user_content = str(content)
                assistant_content = ""
            elif role in ("assistant", "model", "ai"):
                assistant_content = str(content)

        if user_content or assistant_content:
            turns.append(Turn(
                turn_number=turn_number,
                user_content=user_content,
                assistant_content=assistant_content,
            ))

        return turns

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Windsurf 自定义标签"""
        return []


import sys
