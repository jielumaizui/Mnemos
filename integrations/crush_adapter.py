# Crush Adapter — Charm Crush 主动接入适配器
# Crush 使用 ~/.config/crush/crush.json 配置 MCP，优先使用项目本地 ./.crush/crush.db。

import logging
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


from integrations.active import (
    crush_mcp_configured,
    upsert_crush_mcp_server,
)
from integrations.olympus import AgentAdapter, AgentRegistry

logger = logging.getLogger(__name__)


class CrushAdapter(AgentAdapter):
    """Charm Crush 适配器。

    Crush 支持通过 MCP 主动调用外部工具，但本身不提供 session hooks/wrappers。
    因此生命周期捕获仍由被动数据源 ``CrushSource`` 负责；主动接入侧只安装
    MCP server 配置与使用策略。
    """

    @property
    def name(self) -> str:
        return "crush"

    @property
    def priority(self) -> int:
        return 7

    def get_data_dir(self) -> Optional[Path]:
        candidates: List[Path] = []
        env_home = os.getenv("CRUSH_HOME") or os.getenv("CRUSH_DATA_DIR")
        if env_home:
            candidates.append(Path(env_home).expanduser())
        candidates.extend(
            [
                Path.cwd() / ".crush",
                Path.home() / ".crush",
                Path.home() / ".config" / "crush",
            ]
        )
        for candidate in candidates:
            if (candidate / "crush.db").exists():
                return candidate
        return candidates[0] if candidates else None

    def get_config_path(self) -> Optional[Path]:
        return Path.home() / ".config" / "crush" / "crush.json"

    def is_available(self) -> bool:
        data_dir = self.get_data_dir()
        if data_dir and (data_dir / "crush.db").exists():
            return True
        return bool(shutil.which("crush"))

    def is_hooks_installed(self) -> bool:
        """Crush 没有 session hooks 机制，视为已满足。"""
        return True

    def install_hooks(self) -> bool:
        """Crush 没有 hooks 需要安装；确保数据目录存在即可。"""
        data_dir = self.get_data_dir()
        if data_dir:
            data_dir.mkdir(parents=True, exist_ok=True)
        return True

    def is_mcp_configured(self) -> bool:
        config_path = self.get_config_path()
        if not config_path:
            return False
        return crush_mcp_configured(config_path)

    def install_mcp_server(self) -> bool:
        config_path = self.get_config_path()
        if not config_path:
            return False
        config_path.parent.mkdir(parents=True, exist_ok=True)
        return upsert_crush_mcp_server(config_path)

    def collect_signals(self, days: int = 7) -> List[Dict]:
        """从 Crush 数据库采集最近会话信号。"""
        data_dir = self.get_data_dir()
        if not data_dir:
            return []
        db_path = data_dir / "crush.db"
        if not db_path.exists():
            return []

        cutoff = datetime.now().timestamp() - days * 86400
        signals: List[Dict] = []
        try:
            with sqlite3.connect(str(db_path), timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """
                    SELECT id, created_at
                    FROM sessions
                    WHERE created_at >= ?
                    ORDER BY created_at DESC
                    """,
                    (cutoff,),
                )
                for row in cursor.fetchall():
                    signals.append(
                        {
                            "source": "crush",
                            "session_id": str(row["id"]),
                            "timestamp": datetime.fromtimestamp(
                                row["created_at"]
                            ).isoformat(),
                            "working_dir": "",
                            "agent": "crush",
                        }
                    )
        except (OSError, ValueError, TypeError, sqlite3.Error) as e:
            logger.debug("读取 Crush 信号失败: %s", e, exc_info=True)
        return signals


AgentRegistry.register(CrushAdapter)
