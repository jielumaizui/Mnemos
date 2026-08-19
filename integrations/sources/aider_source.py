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

import base64
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.runtime_environment import environment_get
from core.sync_framework.agent_source import (
    NativeSourceContractError,
    SessionInfo,
    Turn,
)
from core.ops.durable_io import read_native_bytes

from integrations.sources.base import (
    BaseAgentSource,
    native_path_kind,
    stable_path_session_id,
)

_USER_HEADERS = {"message", "/message", "/ask", "ask", "user", "human", "you", "me"}
_ASSISTANT_HEADERS = {"assistant", "ai", "aider", "coder"}


def _normalize_header(header: str) -> str:
    """归一化 aider 聊天记录的 section header。"""
    # 去掉 markdown 标题标记、引用符号、尖括号、斜杠
    normalized = re.sub(r"^(#+\s*|>\s*|<\s*|/\s*)", "", header).strip().lower()
    # 去掉末尾冒号/点
    normalized = normalized.rstrip(":.。")
    return normalized


class AiderSource(BaseAgentSource):
    """Aider 数据源插件"""

    _cap_reasoning = "not_available"
    _cap_attachments = "unknown"
    _cap_source_fidelity = "full"

    @property
    def name(self) -> str:
        return "aider"

    @property
    def model_tag(self) -> str:
        return "aider"

    @property
    def data_dir(self) -> Optional[Path]:
        return None  # Aider 的历史文件分散在各项目目录

    @staticmethod
    def _configured_history_file() -> Optional[Path]:
        raw = str(
            environment_get("AIDER_CHAT_HISTORY_FILE", "")
        ).strip()
        return Path(raw).expanduser() if raw else None

    @staticmethod
    def _search_roots() -> List[Path]:
        env_roots = os.getenv("AIDER_PROJECT_ROOTS", "")
        if env_roots:
            return [
                Path(value.strip()).expanduser()
                for value in env_roots.split(",")
                if value.strip()
            ]
        home = Path.home()
        roots = [
            home / candidate
            for candidate in ("Projects", "project", "workspace", "code", "dev")
            if native_path_kind(home / candidate) == "directory"
        ]
        return roots or [home]

    def observed_roots(self) -> List[Path]:
        """Declare the exact roots inspected even when no history is present."""

        history_file = self._configured_history_file()
        roots = []
        if history_file is not None:
            history_kind = native_path_kind(history_file)
            if history_kind not in {"missing", "file"}:
                raise ValueError("configured_aider_history_is_not_a_regular_file")
            roots.append(history_file.parent if history_kind == "file" else history_file)
        roots.extend(self._search_roots())
        return list(dict.fromkeys(roots))

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
        seen: set[Path] = set()
        try:
            configured_history = self._configured_history_file()
            if (
                configured_history is not None
                and native_path_kind(configured_history) == "file"
            ):
                resolved_history = configured_history.resolve(strict=True)
                seen.add(resolved_history)
                canonical_id = stable_path_session_id(
                    "aider",
                    configured_history.parent,
                    configured_history,
                    native_id=configured_history.parent.name,
                )
                sessions.append(
                    SessionInfo(
                        session_id=configured_history.parent.name,
                        source_path=configured_history,
                        working_dir=str(configured_history.parent),
                        mtime=configured_history.stat().st_mtime,
                        canonical_session_id=canonical_id,
                        session_aliases=[configured_history.parent.name],
                        source_kind="markdown_history",
                    )
                )
            for root in self._search_roots():
                if native_path_kind(root) != "directory":
                    continue
                for history_file in root.rglob(".aider.chat.history.md"):
                    resolved_history = history_file.resolve(strict=True)
                    if resolved_history in seen:
                        continue
                    seen.add(resolved_history)
                    session_id = stable_path_session_id(
                        "aider",
                        root,
                        history_file,
                        native_id=history_file.parent.name,
                    )
                    sessions.append(
                        SessionInfo(
                            session_id=history_file.parent.name,
                            source_path=history_file,
                            working_dir=str(history_file.parent),
                            mtime=history_file.stat().st_mtime,
                            canonical_session_id=session_id,
                            session_aliases=[history_file.parent.name],
                            source_kind="markdown_history",
                        )
                    )
            return sessions
        except OSError:
            raise NativeSourceContractError(
                "native_aider_session_discovery_failed"
            ) from None

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 Aider 的 Markdown 聊天记录为 Turn 列表 — P0-6 完整录入版"""
        turns = []  # type: ignore[var-annotated]
        try:
            native_bytes = read_native_bytes(session_path)
        except (OSError, IOError):
            raise NativeSourceContractError(
                "native_aider_history_read_failed"
            ) from None
        try:
            content = native_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return [
                Turn(
                    turn_number=0,
                    user_content="",
                    assistant_content="",
                    raw_event_refs=[
                        {
                            "event_type": "native_markdown_history",
                            "raw_base64": base64.b64encode(native_bytes).decode(
                                "ascii"
                            ),
                            "raw_encoding": "base64",
                            "decode_error": "invalid_utf8",
                        }
                    ],
                    source_files=[str(session_path)],
                    completeness={
                        "visible_text": "unavailable",
                        "tool_results": "unavailable",
                        "reasoning": "unavailable",
                        "attachments": "unavailable",
                        "truncated": False,
                        "loss_reasons": ["native_markdown_invalid_utf8"],
                    },
                )
            ]

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
        sections = re.split(r"\n####\s+", content)
        if len(sections) <= 1:
            # 尝试另一种格式
            sections = re.split(r"\n(?:>\s*message|<\s*assistant)\s*\n", content)

        turn_number = 0
        current_user = ""
        current_assistant = ""
        unrecognised_sections: List[str] = []
        completeness_loss: List[str] = []

        for section in sections:
            section = section.strip()
            if not section:
                continue

            lines = section.split("\n")
            header = lines[0].strip().lower() if lines else ""

            # [P1-48] 使用归一化后的 header 匹配，避免 "system_message" 等子串误报
            normalized = _normalize_header(header)
            is_user = normalized in _USER_HEADERS or normalized.startswith(
                ("user ", "human ", "you ", "me ")
            )
            is_assistant = normalized in _ASSISTANT_HEADERS or normalized.startswith(
                ("assistant ", "ai ", "aider ", "coder ")
            )

            if is_user and not is_assistant:
                if current_assistant or current_user:
                    turns.append(
                        Turn(
                            turn_number=turn_number,
                            user_content=current_user,
                            assistant_content=current_assistant,
                            raw_event_refs=(
                                [{"type": "unrecognised", "sections": unrecognised_sections}]
                                if unrecognised_sections
                                else []
                            ),
                            source_files=[str(session_path)],
                            completeness={
                                "visible_text": "full",
                                "tool_results": "unavailable",
                                "reasoning": "unavailable",
                                "attachments": "unavailable",
                                "truncated": False,
                                "loss_reasons": completeness_loss,
                            },
                        )
                    )
                    turn_number += 1
                current_user = "\n".join(lines[1:]).strip()
                current_assistant = ""
                unrecognised_sections = []
                completeness_loss = []
            elif is_assistant:
                current_assistant = "\n".join(lines[1:]).strip()
            else:
                # 无法识别 header，记录到 unrecognised 待后续写入 artifact
                unrecognised_sections.append(section)
                completeness_loss.append(f"unrecognised_header:{header[:50]}")
                # 尝试作为内容延续
                if current_assistant:
                    current_assistant += "\n" + section
                elif current_user:
                    current_user += "\n" + section

        # 保存最后一轮
        if current_user or current_assistant:
            turns.append(
                Turn(
                    turn_number=turn_number,
                    user_content=current_user,
                    assistant_content=current_assistant,
                    raw_event_refs=(
                        [{"type": "unrecognised", "sections": unrecognised_sections}]
                        if unrecognised_sections
                        else []
                    ),
                    source_files=[str(session_path)],
                    completeness={
                        "visible_text": "full",
                        "tool_results": "unavailable",
                        "reasoning": "unavailable",
                        "attachments": "unavailable",
                        "truncated": False,
                        "loss_reasons": completeness_loss,
                    },
                )
            )

        native_ref = {
            "event_type": "native_markdown_history",
            "raw": content,
        }
        if turns:
            turns[0].raw_event_refs.insert(0, native_ref)
        elif content:
            turns.append(
                Turn(
                    turn_number=0,
                    user_content="",
                    assistant_content="",
                    raw_event_refs=[native_ref],
                    source_files=[str(session_path)],
                    completeness={
                        "visible_text": "raw_only",
                        "tool_results": "unavailable",
                        "reasoning": "unavailable",
                        "attachments": "unavailable",
                        "truncated": False,
                        "loss_reasons": [
                            "unrecognized_native_markdown_structure"
                        ],
                    },
                )
            )

        return turns

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Aider 自定义标签"""
        tags = []
        combined = f"{turn.user_content}\n{turn.assistant_content}"
        if "```" in combined:
            tags.append("has-code=true")
        return tags
