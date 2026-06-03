# -*- coding: utf-8 -*-
"""
CursorSource — Cursor IDE 同步插件

实现 AgentSource 接口，接入 SyncFramework。
Cursor 基于 VS Code，聊天记录可能保存在 SQLite 数据库或 JSON 文件中。

数据位置（调研中）：
- macOS: ~/Library/Application Support/Cursor/
- Linux: ~/.config/Cursor/
- 可能的文件：
  - User/globalStorage/state.vscdb (SQLite)
  - workspaceStorage/*/state.vscdb
  - 或直接在工作区目录下的 .cursor/ 文件夹

⚠️ 当前实现基于公开信息推断，需实际 Cursor 环境验证。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.config import get_config
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

logger = logging.getLogger(__name__)


class CursorSource(AgentSource):
    """Cursor 数据源插件

    ⚠️ EXPERIMENTAL: 当前实现基于 VS Code 结构推断，尚未在真实 Cursor 环境验证。
    """

    experimental = True

    @property
    def name(self) -> str:
        return "cursor"

    @property
    def model_tag(self) -> str:
        return "cursor"

    @property
    def data_dir(self) -> Optional[Path]:
        # 测试覆盖支持
        if hasattr(self, '_override_data_dir'):
            return self._override_data_dir
        config = get_config()
        if sys.platform == "darwin":
            p = Path.home() / "Library" / "Application Support" / "Cursor"
            if p.exists():
                return p
        elif sys.platform == "linux":
            p = Path.home() / ".config" / "Cursor"
            if p.exists():
                return p
        elif sys.platform == "win32":
            p = Path.home() / "AppData" / "Roaming" / "Cursor"
            if p.exists():
                return p

        # 环境变量
        env = os.getenv("CURSOR_HOME")
        if env:
            p = Path(env).expanduser()
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
        """发现 Cursor 会话文件"""
        base = self.data_dir
        if not base:
            return []

        sessions = []

        # 尝试查找 workspaceStorage 中的 JSON 会话文件
        workspace_dir = base / "workspaceStorage"
        if workspace_dir.exists():
            for ws_dir in workspace_dir.iterdir():
                if not ws_dir.is_dir():
                    continue
                # 查找可能的会话记录文件
                for candidate in ["chat_history.json", "session.json", "conversations.json"]:
                    cf = ws_dir / candidate
                    if cf.exists():
                        sessions.append(SessionInfo(
                            session_id=ws_dir.name,
                            source_path=cf,
                            working_dir=str(ws_dir),
                            mtime=cf.stat().st_mtime,
                        ))
                        break

        # 尝试查找全局存储
        global_dir = base / "User" / "globalStorage"
        if global_dir.exists():
            for candidate in ["chat_history.json", "conversations.json"]:
                cf = global_dir / candidate
                if cf.exists():
                    sessions.append(SessionInfo(
                        session_id=f"cursor-global-{candidate}",
                        source_path=cf,
                        working_dir=str(global_dir),
                        mtime=cf.stat().st_mtime,
                    ))

        # 尝试 SQLite 数据库
        for db_path in base.rglob("*.vscdb"):
            sessions.append(SessionInfo(
                session_id=f"cursor-db-{db_path.stem}",
                source_path=db_path,
                working_dir=str(db_path.parent),
                mtime=db_path.stat().st_mtime,
            ))

        return sessions

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 Cursor 会话文件为 Turn 列表"""
        if session_path.suffix == ".json":
            return self._parse_json_session(session_path)
        elif session_path.suffix == ".vscdb":
            return self._parse_sqlite_session(session_path)
        return []

    def _parse_json_session(self, session_path: Path) -> List[Turn]:
        """解析 JSON 格式的会话记录"""
        turns = []
        try:
            with open(session_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"[CursorSource] JSON 解析失败 {session_path}: {e}")
            return turns

        # Cursor 可能的格式: 数组或对象
        messages = []
        if isinstance(data, list):
            messages = data
        elif isinstance(data, dict):
            # 可能是 {conversations: [...]} 或 {messages: [...]}
            messages = data.get("conversations") or data.get("messages") or []

        return self._messages_to_turns(messages)

    def _parse_sqlite_session(self, session_path: Path) -> List[Turn]:
        """尝试从 SQLite 数据库解析会话"""
        turns = []
        try:
            with sqlite3.connect(str(session_path)) as conn:
                cursor = conn.cursor()
                # 尝试常见的表名
                for table in ["ItemTable", "items", "conversations", "messages"]:
                    try:
                        cursor.execute(f"SELECT key, value FROM {table} WHERE key LIKE '%chat%' OR key LIKE '%conversation%'")
                        rows = cursor.fetchall()
                        for row in rows:
                            try:
                                data = json.loads(row[1])
                                messages = data if isinstance(data, list) else data.get("messages", [])
                                turns.extend(self._messages_to_turns(messages))
                            except Exception:
                                continue
                    except sqlite3.OperationalError:
                        continue
        except Exception as e:
            logger.warning(f"[CursorSource] SQLite 解析失败 {session_path}: {e}")
        return turns

    def _messages_to_turns(self, messages: List[Dict[str, Any]]) -> List[Turn]:
        """将消息列表转为 Turn 列表"""
        turns = []
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

    def completeness_capabilities(self) -> Dict[str, Any]:
        return {
            "visible_text": True,
            "tool_calls": False,
            "tool_results": False,
            "reasoning": "unknown",
            "attachments": "unknown",
            "raw_files": True,
            "source_fidelity": "experimental",
        }

    def get_session_state(self, session_info: SessionInfo) -> Optional[Dict[str, Any]]:
        """Cursor 聚合状态：JSON + SQLite 多源"""
        base = session_info.source_path.parent
        files = []
        for pattern in ["*.json", "*.vscdb"]:
            files.extend(base.rglob(pattern))
        if not files:
            return None
        total_size = 0
        max_mtime = 0
        file_entries = []
        for f in sorted(files):
            try:
                stat = f.stat()
                total_size += stat.st_size
                max_mtime = max(max_mtime, stat.st_mtime)
                file_entries.append(f"{f.name}:{stat.st_size}:{stat.st_mtime}")
            except OSError:
                pass
        import hashlib
        fingerprint = hashlib.md5("|".join(file_entries).encode()).hexdigest()[:16]
        return {
            "mtime": max_mtime,
            "size": total_size,
            "file_count": len(files),
            "fingerprint": fingerprint,
        }

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Cursor 自定义标签"""
        return ["source_fidelity=experimental"]


import sys
