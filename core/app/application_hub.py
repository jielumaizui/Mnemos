# -*- coding: utf-8 -*-
"""
ApplicationHub — 应用层统一调度

职责：去重、优先级排序、速率限制
优先级：search(0) > blind_spot(1) > push(2) > evolution(3) > dispute(4) > incremental(5)
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AppOutput:
    """应用层输出"""
    output_type: str  # search / blind_spot / predictive_push / evolution_alert / dispute / incremental
    priority: int
    knowledge_id: str
    content: str
    context: str = ""
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

    def explain(self) -> str:
        """可解释性输出"""
        return (f"[{self.output_type}] priority={self.priority} "
                f"id={self.knowledge_id} context={self.context[:50]}")


# 速率限制配置
RATE_LIMITS = {
    "blind_spot": {"max_per_day": 1, "cooldown_sec": 86400},
    "predictive_push": {"max_per_10min": 1, "cooldown_sec": 600, "max_per_batch": 3},
    "evolution_alert": {"search_only": True},
    "dispute": {"weekly_only": True},
    "incremental": {"max_per_day": 2, "cooldown_sec": 43200},
}

# 去重窗口
DEDUP_WINDOW_SEC = 86400  # 24 小时

# 全局频率限制：每秒最多 1 个主动输出
MIN_INTERVAL_SEC = 1.0


class PushPenaltyTracker:
    """推送惩罚追踪器 — 忽略次数 → 冷却倍数"""

    PENALTY_LEVELS = [
        (1, 1.5),   # 忽略1次 → 1.5x 冷却
        (2, 2.0),   # 忽略2次 → 2.0x 冷却
        (3, 6.0),   # 忽略3次 → 6.0x 冷却 = 暂停1小时
    ]

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.DB_PATH = Path(db_path).expanduser()
        else:
            from core.config import get_config
            self.DB_PATH = get_config().data_dir / "push_penalty.db"
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS push_penalties (
                    topic TEXT PRIMARY KEY,
                    ignore_count INTEGER DEFAULT 0,
                    last_ignore_at TEXT,
                    cooldown_until TEXT
                )
            """)

    def record_ignore(self, topic: str) -> float:
        """记录用户忽略，返回冷却倍数"""
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT ignore_count FROM push_penalties WHERE topic = ?", (topic,)
            )
            row = cursor.fetchone()
            count = (row[0] + 1) if row else 1

            # 计算冷却倍数
            multiplier = 1.0
            for threshold, mult in self.PENALTY_LEVELS:
                if count >= threshold:
                    multiplier = mult

            # 计算冷却截止时间
            base_cooldown = RATE_LIMITS.get("predictive_push", {}).get("cooldown_sec", 600)
            cooldown_sec = base_cooldown * multiplier
            cooldown_until = (datetime.now() + timedelta(seconds=cooldown_sec)).isoformat()

            conn.execute("""
                INSERT OR REPLACE INTO push_penalties (topic, ignore_count, last_ignore_at, cooldown_until)
                VALUES (?, ?, ?, ?)
            """, (topic, count, datetime.now().isoformat(), cooldown_until))

        return multiplier

    def record_accept(self, topic: str) -> None:
        """记录用户接受，重置惩罚"""
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                UPDATE push_penalties SET ignore_count = 0
                WHERE topic = ?
            """, (topic,))

    def is_in_cooldown(self, topic: str) -> bool:
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT cooldown_until FROM push_penalties WHERE topic = ?", (topic,)
            )
            row = cursor.fetchone()
            if not row or not row[0]:
                return False
            try:
                return datetime.now() < datetime.fromisoformat(row[0])
            except ValueError:
                return False


class ApplicationHub:
    """应用层统一调度中心"""

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.DB_PATH = Path(db_path).expanduser()
        else:
            from core.config import get_config
            self.DB_PATH = get_config().data_dir / "application_hub.db"
        self._init_db()
        self.penalty_tracker = PushPenaltyTracker(db_path)
        self._last_output_time = 0.0

    def _init_db(self):
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS output_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    output_type TEXT NOT NULL,
                    knowledge_id TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_oh_dedup
                ON output_history(knowledge_id, timestamp)
            """)

    def submit(self, outputs: List[AppOutput]) -> List[AppOutput]:
        """
        提交输出请求，返回经过去重、优先级排序、速率限制后的输出列表。
        """
        if not outputs:
            return []

        # 1. 去重：24 小时内相同 knowledge_id 不重复输出
        now = time.time()
        dedup_cutoff = now - DEDUP_WINDOW_SEC
        recent_ids = self._get_recent_ids(dedup_cutoff)

        filtered = [o for o in outputs if o.knowledge_id not in recent_ids]
        if not filtered:
            return []

        # 2. 优先级排序
        filtered.sort(key=lambda o: o.priority)

        # 3. 速率限制
        result = []
        for output in filtered:
            if self._check_rate_limit(output):
                # 全局频率限制
                if output.output_type != "search":
                    if now - self._last_output_time < MIN_INTERVAL_SEC:
                        continue
                    self._last_output_time = now
                result.append(output)
                self._record_output(output)

        return result

    def _check_rate_limit(self, output: AppOutput) -> bool:
        """检查输出类型速率限制"""
        limits = RATE_LIMITS.get(output.output_type)
        if not limits:
            return True

        # search 类型不受限制
        if output.output_type == "search":
            return True

        # 搜索附加型（只在搜索时展示）
        if limits.get("search_only"):
            return False  # 不主动输出

        # 周报型
        if limits.get("weekly_only"):
            return False  # 不主动输出

        now = datetime.now()

        # 检查冷却
        cooldown_sec = limits.get("cooldown_sec", 0)
        if cooldown_sec:
            last = self._get_last_output_time(output.output_type)
            if last and (now - last).total_seconds() < cooldown_sec:
                return False

        # 检查每日上限
        max_per_day = limits.get("max_per_day")
        if max_per_day:
            count = self._count_today_outputs(output.output_type)
            if count >= max_per_day:
                return False

        # 检查推送惩罚
        if output.output_type == "predictive_push":
            topic = output.knowledge_id.split(":")[0] if ":" in output.knowledge_id else output.knowledge_id
            if self.penalty_tracker.is_in_cooldown(topic):
                return False

        return True

    def _get_recent_ids(self, since: float) -> set:
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT knowledge_id FROM output_history WHERE timestamp >= ?",
                (since,),
            )
            return {row[0] for row in cursor.fetchall()}

    def _get_last_output_time(self, output_type: str) -> Optional[datetime]:
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT timestamp FROM output_history WHERE output_type = ? ORDER BY timestamp DESC LIMIT 1",
                (output_type,),
            )
            row = cursor.fetchone()
            if row:
                return datetime.fromtimestamp(row[0])
        return None

    def _count_today_outputs(self, output_type: str) -> int:
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM output_history WHERE output_type = ? AND timestamp >= ?",
                (output_type, today_start.timestamp()),
            )
            return cursor.fetchone()[0]

    def _record_output(self, output: AppOutput) -> None:
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute(
                "INSERT INTO output_history (output_type, knowledge_id, timestamp) VALUES (?, ?, ?)",
                (output.output_type, output.knowledge_id, output.timestamp),
            )
