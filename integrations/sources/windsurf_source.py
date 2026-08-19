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
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.runtime_environment import environment_get
from core.sync_framework.agent_source import (
    NativeSourceContractError,
    SessionInfo,
    Turn,
)
from core.ops.durable_io import open_native_text

from integrations.sources.base import (
    BaseAgentSource,
    attach_native_container_residual,
    lossless_text_message_turns,
    native_path_kind,
)

logger = logging.getLogger(__name__)


class WindsurfSource(BaseAgentSource):
    """Windsurf 数据源插件

    ⚠️ EXPERIMENTAL: 当前实现基于公开信息推断，尚未在真实 Windsurf 环境验证。
    """

    experimental = True

    _cap_reasoning = "unknown"
    _cap_attachments = "unknown"
    _cap_source_fidelity = "experimental"

    @property
    def name(self) -> str:
        return "windsurf"

    @property
    def model_tag(self) -> str:
        return "windsurf"

    @staticmethod
    def _candidate_data_dirs() -> List[Path]:
        configured = str(environment_get("WINDSURF_HOME", "")).strip()
        if configured:
            return [Path(configured).expanduser()]
        candidates = []
        if sys.platform == "darwin":
            candidates.extend(
                [
                    Path.home() / "Library" / "Application Support" / "Windsurf",
                    Path.home() / ".windsurf",
                ]
            )
        elif sys.platform == "linux":
            candidates.extend(
                [
                    Path.home() / ".config" / "Windsurf",
                    Path.home() / ".windsurf",
                ]
            )
        elif sys.platform == "win32":
            candidates.append(Path.home() / "AppData" / "Roaming" / "Windsurf")
        return list(dict.fromkeys(candidates))

    @property
    def data_dir(self) -> Optional[Path]:
        return self._resolve_data_dir(self._candidate_data_dirs())

    def observed_roots(self) -> List[Path]:
        """Bind installed data or existing candidate parents for verified empty."""

        configured = str(environment_get("WINDSURF_HOME", "")).strip()
        if configured:
            return [Path(configured).expanduser()]
        resolved = self.data_dir
        if resolved is not None:
            return [resolved]
        return list(
            dict.fromkeys(
                candidate.parent
                for candidate in self._candidate_data_dirs()
                if native_path_kind(candidate.parent) == "directory"
            )
        )

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
        return self._discover_vscode_workspace_sessions(
            base,
            ["chat_history.json", "session.json", "conversations.json", "history.json"],
            ["chat_history.json", "conversations.json", "history.json"],
            prefix="windsurf",
        )

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析 Windsurf 会话文件为 Turn 列表"""
        try:
            with open_native_text(session_path) as f:
                data = json.load(f)
        except (OSError, IOError, json.JSONDecodeError):
            raise NativeSourceContractError(
                "native_windsurf_json_read_failed"
            ) from None

        messages: Any = data
        consumed_keys: set[str] = set()
        if isinstance(data, list):
            messages = data
        elif isinstance(data, dict):
            for key in ("conversations", "messages", "history"):
                if key in data:
                    messages = data[key]
                    consumed_keys = {key}
                    break
        turns = lossless_text_message_turns(messages, source_name=self.name)
        if consumed_keys:
            return attach_native_container_residual(
                turns,
                data,
                consumed_keys=consumed_keys,
                source_name=self.name,
            )
        return turns
