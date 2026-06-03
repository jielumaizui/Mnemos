# -*- coding: utf-8 -*-
"""
OpenClawSource — OpenClaw Agent 同步插件

实现 AgentSource 接口，接入 SyncFramework。
OpenClaw 的语料文件是每日批量生成（session-corpus/YYYY-MM-DD.txt），
使用定时轮询为主（每小时），不依赖实时文件追加。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.config import get_config
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

logger = logging.getLogger(__name__)

# 语料行格式：[path#Lline] Role: content
SESSION_LINE_RE = re.compile(
    r"^\[(?P<path>[^\]]+)#L(?P<line>\d+)\]\s+(?P<role>User|Assistant):\s*(?P<content>.*)$"
)


class OpenClawSource(AgentSource):
    """OpenClaw 数据源插件"""

    @property
    def name(self) -> str:
        return "openclaw"

    @property
    def model_tag(self) -> str:
        return "openclaw"

    @property
    def data_dir(self) -> Optional[Path]:
        config = get_config()
        # 环境变量优先
        env = config.get("integrations.openclaw.state_dir")
        if env:
            p = Path(env).expanduser()
            if p.exists():
                return p
        # 标准路径
        p = Path.home() / ".openclaw"
        return p if p.exists() else None

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        return {
            "type": "polling",
            "interval": 3600,
            "recursive": True,
        }

    def discover_sessions(self) -> List[SessionInfo]:
        """
        发现所有可同步的 OpenClaw 语料文件。
        每日一个语料文件，按日期组织。
        """
        base = self.data_dir
        if not base:
            return []

        corpus_dir = base / "workspace" / "memory" / ".dreams" / "session-corpus"
        if not corpus_dir.exists():
            return []

        sessions = []
        for corpus_file in sorted(corpus_dir.glob("*.txt")):
            sessions.append(SessionInfo(
                session_id=corpus_file.stem,  # YYYY-MM-DD
                source_path=corpus_file,
                working_dir=str(corpus_file.parent),
                mtime=corpus_file.stat().st_mtime,
            ))
        return sessions

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """
        解析 OpenClaw 语料文件为 Turn 列表。
        按行解析，将 User/Assistant 配对为 Turn。
        修复：不同内部 session 的 turn_number 加偏移，避免 sync_log 冲突。
        """
        parsed_sessions = self._parse_corpus(session_path)
        all_turns = []
        session_idx = 0
        for inner_session_id, messages in parsed_sessions.items():
            turns = self._pair_messages(messages, inner_session_id, base_turn_number=session_idx * 1000)
            all_turns.extend(turns)
            session_idx += 1
        return all_turns

    def _parse_corpus(self, corpus_path: Path) -> Dict[str, List[Dict[str, str]]]:
        """解析语料文件，按 session_id 分组"""
        sessions: Dict[str, List[Dict[str, str]]] = {}
        fallback_id = corpus_path.stem

        try:
            content = corpus_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"[OpenClawSource] 读取失败 {corpus_path}: {e}")
            return sessions

        for line in content.splitlines():
            m = SESSION_LINE_RE.match(line)
            if not m:
                continue

            # 从 path 提取 session_id
            path_str = m.group("path")
            session_match = re.search(r'sessions/([a-f0-9-]+)', path_str)
            session_id = session_match.group(1) if session_match else fallback_id

            role = m.group("role").lower()
            msg_content = m.group("content")

            sessions.setdefault(session_id, []).append({
                "role": role,
                "content": msg_content,
            })

        return sessions

    def _pair_messages(
        self, messages: List[Dict[str, str]], session_id: str, base_turn_number: int = 0
    ) -> List[Turn]:
        """将消息列表配对为 Turn 列表"""
        turns = []
        user_content = ""
        assistant_content = ""
        turn_number = base_turn_number

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user":
                if assistant_content:
                    turns.append(Turn(
                        turn_number=turn_number,
                        user_content=user_content,
                        assistant_content=assistant_content,
                        metadata={"session_id": session_id},
                    ))
                    turn_number += 1
                user_content = content
                assistant_content = ""

            elif role == "assistant":
                assistant_content += ("\n" if assistant_content else "") + content

        # 保存最后一轮
        if user_content or assistant_content:
            turns.append(Turn(
                turn_number=turn_number,
                user_content=user_content,
                assistant_content=assistant_content,
                metadata={"session_id": session_id},
            ))

        return turns

    def get_session_state(self, session_info: SessionInfo) -> Optional[Dict[str, Any]]:
        """OpenClaw 聚合状态：session-corpus 下所有日文件"""
        corpus_dir = session_info.source_path.parent
        files = list(corpus_dir.glob("*.txt"))
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

    def completeness_capabilities(self) -> Dict[str, Any]:
        return {
            "visible_text": True,
            "tool_calls": False,
            "tool_results": False,
            "reasoning": "not_available",
            "attachments": "not_available",
            "raw_files": True,
            "source_fidelity": "derived",
        }

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """OpenClaw 自定义标签"""
        tags = [
            "source_fidelity=derived",
        ]
        if turn.metadata.get("session_id"):
            tags.append(f"openclaw-session={turn.metadata['session_id'][:8]}")
        return tags
