# Typhon — 百头巨龙，盖亚之子
# OpenClaw 适配器 — 多爪并行，SQLite 驱动的 Agent 集成

import json
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from integrations.olympus import AgentAdapter, AgentRegistry

logger = logging.getLogger(__name__)


class OpenClawAdapter(AgentAdapter):
    """OpenClaw Agent 适配器

    OpenClaw 采用 SQLite 作为输入接口：
    - Mnemos 读取 OpenClaw SQLite 数据库获取会话历史
    - 蒸馏任务写入 SQLite 任务表等待 OpenClaw 处理
    """

    @property
    def name(self) -> str:
        return "openclaw"

    @property
    def priority(self) -> int:
        return 3

    def _sqlite_path(self) -> Path:
        """OpenClaw SQLite 数据库路径"""
        from core.config import get_config
        custom = getattr(get_config(), "openclaw_sqlite_path", None)
        if custom:
            return Path(custom)
        return Path.home() / ".openclaw" / "sessions.db"

    def is_available(self) -> bool:
        """检测 OpenClaw 是否安装"""
        return self._sqlite_path().exists()

    def get_data_dir(self) -> Optional[Path]:
        return Path.home() / ".openclaw"

    def on_session_start(self, working_dir: str, user_message: str = "") -> Dict:
        knowledge = self.inject_knowledge("general", context_text=user_message)
        return {"agent": "openclaw", "knowledge": knowledge}

    def on_session_end(self, working_dir: str, session_messages: List[Dict] = None) -> Dict:
        sid = None
        if session_messages:
            try:
                from core.kia.amphora import enqueue
                from datetime import datetime
                wd = working_dir or __import__('os').getcwd()
                dir_hash = __import__('hashlib').md5(wd.encode()).hexdigest()[:8]
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                sid = f"openclaw:{dir_hash}:{ts}"
                enqueue(
                    session_id=sid,
                    messages=session_messages,
                    meta={"source": "openclaw", "working_dir": wd}
                )
            except Exception as e:
                logger.warning(f"OpenClaw 蒸馏入队失败: {e}")
        return {"saved": True, "distill_task_id": sid}

    def install_hooks(self) -> bool:
        """OpenClaw 无 hook 概念，需在配置中指定 SQLite 路径"""
        print("请在 OpenClaw 配置中启用 SQLite 会话存储：")
        print(f"  SQLite 路径: {self._sqlite_path()}")
        return True

    def collect_signals(self, days: int = 7) -> List[Dict]:
        """从 OpenClaw SQLite 读取会话信号"""
        signals = []
        db_path = self._sqlite_path()
        if not db_path.exists():
            return signals

        try:
            with sqlite3.connect(str(db_path), timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                from datetime import datetime, timedelta
                cutoff = (datetime.now() - timedelta(days=days)).isoformat()
                cursor = conn.execute(
                    "SELECT * FROM sessions WHERE timestamp > ? ORDER BY timestamp DESC",
                    (cutoff,)
                )
                for row in cursor.fetchall():
                    signals.append({
                        "source": "openclaw",
                        "session_id": row["session_id"],
                        "timestamp": row["timestamp"],
                        "task_type": row.get("task_type", "unknown"),
                        "raw": dict(row),
                    })
        except Exception as e:
            logger.warning(f"读取 OpenClaw SQLite 失败: {e}")

        return signals

    def inject_knowledge(self, task_type: str, subtype: str = "", context_text: str = "") -> Dict:
        from core.kia.prophasis import PreFlightInjector
        from core.kia.kairos import TimeWindow, TimeWindowType
        injector = PreFlightInjector()
        time_window = TimeWindow(window=TimeWindowType.IMMEDIATE, days_until=0)
        knowledge = injector.inject(task_type, subtype, time_window, context_text)
        if not knowledge:
            return {"loaded": False}
        return {
            "loaded": True,
            "task_type": knowledge.task_type,
            "checklist": [c.item for c in knowledge.checklist],
        }

    def delegate_distillation(self, task_path: Path, output_path: Path) -> bool:
        """委托 OpenClaw 执行蒸馏

        策略：将任务写入 OpenClaw SQLite 的 mnemos_tasks 表。
        """
        try:
            task = json.loads(task_path.read_text(encoding="utf-8"))
            db_path = self._sqlite_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(db_path), timeout=10) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS mnemos_tasks (
                        id TEXT PRIMARY KEY,
                        type TEXT,
                        payload TEXT,
                        output_path TEXT,
                        status TEXT DEFAULT 'pending',
                        created_at TEXT
                    )
                """)
                from datetime import datetime
                conn.execute(
                    "INSERT OR REPLACE INTO mnemos_tasks (id, type, payload, output_path, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        task.get("session_id", "unknown"),
                        "distillation",
                        json.dumps(task, ensure_ascii=False),
                        str(output_path),
                        "pending",
                        datetime.now().isoformat(),
                    )
                )
            return True
        except Exception as e:
            logger.warning(f"委托 OpenClaw 蒸馏失败: {e}")
            return False


AgentRegistry.register(OpenClawAdapter)
