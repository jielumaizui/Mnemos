# Kimi Adapter — Kimi Code CLI 适配器
# 基于文件系统轮询：读取 Kimi / Kimi Code 本地会话文件。

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from integrations.active import (
    generated_wrapper,
    json_mcp_configured,
    kimi_hooks_configured,
    upsert_json_mcp_server,
    upsert_kimi_hooks,
    wrapper_uses_active_bridge,
)
from integrations.olympus import AgentAdapter, AgentRegistry
from core.sync_framework.agent_source import parse_discovered_session

logger = logging.getLogger(__name__)


class KimiAdapter(AgentAdapter):
    """Kimi Code CLI 适配器

    Kimi 采用 JSON Lines 格式存储会话：
    - ~/.kimi/sessions/{workspace_id}/{session_id}/context*.jsonl
    - ~/.kimi-code/sessions/{workspace_id}/{session_id}/agents/main/wire.jsonl
    """

    @property
    def name(self) -> str:
        return "kimi"

    @property
    def priority(self) -> int:
        return 6

    def is_available(self) -> bool:
        """检测 Kimi 是否安装"""
        data_dir = self.get_data_dir()
        return bool(data_dir and data_dir.exists()) or bool(
            shutil.which("kimi") or shutil.which("kimi-code")
        )

    def get_data_dir(self) -> Optional[Path]:
        candidates: List[Path] = []
        for env_name in ("KIMI_CODE_HOME", "KIMI_HOME"):
            value = os.getenv(env_name)
            if value:
                candidates.append(Path(value).expanduser())
        candidates.extend([Path.home() / ".kimi-code", Path.home() / ".kimi"])
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0] if candidates else None

    def is_hooks_installed(self) -> bool:
        """检查 Kimi config.toml 中是否已注册 Mnemos hooks"""
        config_path = self.get_data_dir() / "config.toml"  # type: ignore[operator]
        wrapper_path = self.get_data_dir() / "mnemos_wrapper.py"  # type: ignore[operator]
        return kimi_hooks_configured(config_path, wrapper_path) and wrapper_uses_active_bridge(
            wrapper_path
        )

    def is_mcp_configured(self) -> bool:
        data_dir = self.get_data_dir()
        return bool(data_dir and json_mcp_configured(data_dir / "mcp.json"))

    def install_mcp_server(self) -> bool:
        data_dir = self.get_data_dir()
        if data_dir is None:
            return False
        return upsert_json_mcp_server(
            data_dir / "mcp.json",
            agent="kimi",
        )

    def install_hooks(self) -> bool:
        """在 Kimi config.toml 中注册 session hooks"""
        try:
            config_path = self.get_data_dir() / "config.toml"  # type: ignore[operator]
            if not config_path.exists():
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text("", encoding="utf-8")

            wrapper_path = self.get_data_dir() / "mnemos_wrapper.py"  # type: ignore[operator]

            # 生成 wrapper 脚本
            wrapper_path.write_text(generated_wrapper(self.name), encoding="utf-8")
            upsert_kimi_hooks(config_path, wrapper_path)
            self.install_mcp_server()

            logger.info("[KimiAdapter] Hooks 已安装到 %s", config_path)
            return True
        except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
            logger.warning("[KimiAdapter] 安装 hooks 失败: %s", e, exc_info=True)
            return False

    def collect_signals(self, days: int = 7) -> List[Dict]:
        """从 Kimi sessions 目录采集信号"""
        from integrations.sources.kimi_source import KimiSource

        signals = []  # type: ignore[var-annotated]
        data_dir = self.get_data_dir()
        if not data_dir or not data_dir.exists():
            return signals
        cutoff = datetime.now().timestamp() - days * 86400

        source = KimiSource()
        source._override_data_dir = data_dir  # type: ignore[attr-defined]
        for session in source.discover_sessions():
            if session.mtime and session.mtime < cutoff:
                continue
            try:
                turns = parse_discovered_session(source, session)
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
                logger.debug("读取 Kimi session 失败 %s: %s", session.source_path, e, exc_info=True)
                continue
            messages = []
            for turn in turns:
                if turn.user_content:
                    messages.append({"role": "user", "content": turn.user_content})
                if turn.assistant_content:
                    messages.append({"role": "assistant", "content": turn.assistant_content})
            if not messages:
                continue
            signals.append(
                {
                    "source": "kimi",
                    "session_id": session.session_id,
                    "native_session_id": session.metadata.get(
                        "native_session_id",
                        session.session_aliases[0] if session.session_aliases else session.session_id,
                    ),
                    "source_kind": session.source_kind or "",
                    "source_artifact_id": session.metadata.get("source_artifact_id", ""),
                    "parent_session_id": session.metadata.get("parent_session_id", ""),
                    "canonical_parent_session_id": session.metadata.get(
                        "canonical_parent_session_id", ""
                    ),
                    "parent_source_artifact_id": session.metadata.get(
                        "parent_source_artifact_id", ""
                    ),
                    "identity_contract_version": session.metadata.get(
                        "identity_contract_version", ""
                    ),
                    "workspace": session.working_dir or "",
                    "timestamp": datetime.fromtimestamp(
                        session.mtime or datetime.now().timestamp()
                    ).isoformat(),
                    "messages": messages,
                    "file": str(session.source_path),
                }
            )

        return signals


AgentRegistry.register(KimiAdapter)
