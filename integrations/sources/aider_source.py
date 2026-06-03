# -*- coding: utf-8 -*-
"""
AiderSource — Aider (AI pair programming) 同步插件

实现 AgentSource 接口，接入 SyncFramework。
Aider 的聊天记录保存在项目目录的 `.aider.chat.history.md` 中，格式为 Markdown。

数据位置：
- 项目根目录下的 `.aider.chat.history.md`
- 环境变量 AIDER_CHAT_HISTORY_FILE 可覆盖路径
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.config import get_config
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

logger = logging.getLogger(__name__)


class AiderSource(AgentSource):
    """Aider 数据源插件"""

    @property
    def name(self) -> str:
        return "aider"

    @property
    def model_tag(self) -> str:
        return "aider"

    @property
    def data_dir(self) -> Optional[Path]:
        return None  # Aider 的历史文件分散在各项目目录

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        return {
            "type": "watchdog",
            "events": ["modified", "created"],
            "debounce": 3.0,
            "recursive": True,
        }

    def discover_sessions(self) -> List[SessionInfo]:
        """
        发现所有可同步的 Aider 聊天记录。
        扫描用户主目录下各项目的 `.aider.chat.history.md`。
        """
        sessions = []
        # 从环境变量获取搜索根目录
        search_roots = []
        env_roots = os.getenv("AIDER_PROJECT_ROOTS", "")
        if env_roots:
            search_roots = [Path(p.strip()).expanduser() for p in env_roots.split(",")]
        else:
            # 默认搜索常见项目目录
            home = Path.home()
            for candidate in ["Projects", "project", "workspace", "code", "dev"]:
                p = home / candidate
                if p.exists():
                    search_roots.append(p)
            if not search_roots:
                search_roots = [home]

        seen = set()
        for root in search_roots:
            if not root.exists():
                continue
            for history_file in root.rglob(".aider.chat.history.md"):
                if str(history_file) in seen:
                    continue
                seen.add(str(history_file))
                # session_id 用文件所在项目目录名
                session_id = history_file.parent.name
                sessions.append(SessionInfo(
                    session_id=session_id,
                    source_path=history_file,
                    working_dir=str(history_file.parent),
                    mtime=history_file.stat().st_mtime,
                ))

        return sessions

    def completeness_capabilities(self) -> Dict[str, Any]:
        return {
            "visible_text": True,
            "tool_calls": False,
            "tool_results": False,
            "reasoning": "not_available",
            "attachments": "unknown",
            "raw_files": True,
            "source_fidelity": "full",
        }

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 Aider 的 Markdown 聊天记录为 Turn 列表 — P0-6 完整录入版"""
        turns = []
        try:
            content = session_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"[AiderSource] 读取失败 {session_path}: {e}")
            return turns

        # Aider 格式:
        # #### /message
        # user content
        #
        # #### assistant
        # assistant content
        #
        # 或:
        # > message
        # user content
        #
        # < assistant
        # assistant content

        # 更宽松的匹配：寻找明显的分隔符
        sections = re.split(r'\n####\s+', content)
        if len(sections) <= 1:
            # 尝试另一种格式
            sections = re.split(r'\n(?:>\s*message|<\s*assistant)\s*\n', content)

        turn_number = 0
        current_user = ""
        current_assistant = ""
        unrecognised_sections: List[str] = []
        completeness_loss: List[str] = []

        for section in sections:
            section = section.strip()
            if not section:
                continue

            lines = section.split('\n')
            header = lines[0].strip().lower() if lines else ""

            # 支持多种 header 格式: /message, #### /message, > message, assistant, #### assistant
            is_user = header in ('message', '/message', '/ask', 'ask', '> message') or 'message' in header
            is_assistant = header in ('assistant', '< assistant') or header == '#### assistant'

            if is_user and not is_assistant:
                if current_assistant or current_user:
                    turns.append(Turn(
                        turn_number=turn_number,
                        user_content=current_user,
                        assistant_content=current_assistant,
                        raw_event_refs=([{"type": "unrecognised", "sections": unrecognised_sections}] if unrecognised_sections else []),
                        source_files=[str(session_path)],
                        completeness={
                            "visible_text": "full",
                            "tool_results": "unavailable",
                            "reasoning": "unavailable",
                            "attachments": "unavailable",
                            "truncated": False,
                            "loss_reasons": completeness_loss,
                        },
                    ))
                    turn_number += 1
                current_user = '\n'.join(lines[1:]).strip()
                current_assistant = ""
                unrecognised_sections = []
                completeness_loss = []
            elif is_assistant:
                current_assistant = '\n'.join(lines[1:]).strip()
            else:
                # 无法识别 header，记录到 unrecognised 待后续写入 artifact
                unrecognised_sections.append(section)
                completeness_loss.append(f"unrecognised_header:{header[:50]}")
                # 尝试作为内容延续
                if current_assistant:
                    current_assistant += '\n' + section
                elif current_user:
                    current_user += '\n' + section

        # 保存最后一轮
        if current_user or current_assistant:
            turns.append(Turn(
                turn_number=turn_number,
                user_content=current_user,
                assistant_content=current_assistant,
                raw_event_refs=([{"type": "unrecognised", "sections": unrecognised_sections}] if unrecognised_sections else []),
                source_files=[str(session_path)],
                completeness={
                    "visible_text": "full",
                    "tool_results": "unavailable",
                    "reasoning": "unavailable",
                    "attachments": "unavailable",
                    "truncated": False,
                    "loss_reasons": completeness_loss,
                },
            ))

        return turns

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Aider 自定义标签"""
        tags = []
        combined = f"{turn.user_content}\n{turn.assistant_content}"
        if "```" in combined:
            tags.append("has-code=true")
        return tags
