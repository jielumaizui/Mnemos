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
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from core.config import get_config

logger = logging.getLogger(__name__)


@dataclass
class AppOutput:
    """应用层输出"""

    output_type: (
        str  # search / blind_spot / predictive_push / evolution_alert / dispute / incremental
    )
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
        return (
            f"[{self.output_type}] priority={self.priority} "
            f"id={self.knowledge_id} context={self.context[:50]}"
        )


# 命名常量
DEDUP_WINDOW_SEC = 86400  # 24 小时去重窗口
BLINDSPOT_COOLDOWN_SEC = 86400  # 24 小时盲点冷却
INCREMENTAL_COOLDOWN_SEC = 43200  # 12 小时增量冷却

# 速率限制配置
RATE_LIMITS = {
    # blind_spot 的冷却策略下放到 BlindspotDiscovery：
    # 同一 topic 在同一 session 内只提醒一次；忽略后 7 天冷却。
    # ApplicationHub 只保留每日上限作为兜底保护。
    "blind_spot": {
        "max_per_day": 5,
        "cooldown_sec": 0,
        "delegated_cooldown_sec": BLINDSPOT_COOLDOWN_SEC,
    },
    "evolution_alert": {"search_only": True},
    "dispute": {"weekly_only": True},
    "incremental": {"max_per_day": 2, "cooldown_sec": INCREMENTAL_COOLDOWN_SEC},
}

# 全局频率限制：每秒最多 1 个主动输出
MIN_INTERVAL_SEC = 1.0


class PushPenaltyTracker:
    """推送惩罚追踪器 — 忽略次数 → 冷却倍数"""

    PENALTY_LEVELS = [
        (1, 1.5),  # 忽略1次 → 1.5x 冷却
        (2, 2.0),  # 忽略2次 → 2.0x 冷却
        (3, 6.0),  # 忽略3次 → 6.0x 冷却 = 暂停1小时
    ]

    def __init__(self, db_path: Optional[str] = None):
        if db_path:
            self.DB_PATH = Path(db_path).expanduser()
        else:
            self.DB_PATH = get_config().database_dir / "push_penalty.db"
        self._init_db()

    def _init_db(self):
        self.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS push_penalties (
                    topic TEXT PRIMARY KEY,
                    ignore_count INTEGER DEFAULT 0,
                    last_ignore_at TEXT,
                    cooldown_until TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS push_penalty_feedback_events (
                    feedback_event_id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)

    def record_ignore(self, topic: str, *, feedback_event_id: str = "") -> float:
        """记录用户忽略，返回冷却倍数"""
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if feedback_event_id:
                inserted = conn.execute(
                    """
                    INSERT OR IGNORE INTO push_penalty_feedback_events (
                        feedback_event_id, topic, action, created_at
                    ) VALUES (?, ?, 'ignore', ?)
                    """,
                    (feedback_event_id, topic, datetime.now().isoformat()),
                )
                if inserted.rowcount == 0:
                    row = conn.execute(
                        "SELECT ignore_count FROM push_penalties WHERE topic = ?",
                        (topic,),
                    ).fetchone()
                    return self._multiplier_for_count(int(row[0]) if row else 0)
            cursor = conn.execute(
                "SELECT ignore_count FROM push_penalties WHERE topic = ?", (topic,)
            )
            row = cursor.fetchone()
            count = (row[0] + 1) if row else 1

            # 计算冷却倍数
            multiplier = self._multiplier_for_count(count)

            # 计算冷却截止时间
            base_cooldown = self._predictive_push_base_cooldown_seconds()
            cooldown_sec = base_cooldown * multiplier
            cooldown_until = (datetime.now() + timedelta(seconds=cooldown_sec)).isoformat()

            conn.execute(
                """
                INSERT OR REPLACE INTO push_penalties (
                    topic, ignore_count, last_ignore_at, cooldown_until
                )
                VALUES (?, ?, ?, ?)
            """,
                (topic, count, datetime.now().isoformat(), cooldown_until),
            )

        return multiplier

    def record_accept(self, topic: str, *, feedback_event_id: str = "") -> None:
        """记录用户接受，重置惩罚并清除冷却"""
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if feedback_event_id:
                inserted = conn.execute(
                    """
                    INSERT OR IGNORE INTO push_penalty_feedback_events (
                        feedback_event_id, topic, action, created_at
                    ) VALUES (?, ?, 'accept', ?)
                    """,
                    (feedback_event_id, topic, datetime.now().isoformat()),
                )
                if inserted.rowcount == 0:
                    return
            conn.execute(
                """
                UPDATE push_penalties
                SET ignore_count = 0, cooldown_until = NULL
                WHERE topic = ?
            """,
                (topic,),
            )

    @classmethod
    def _multiplier_for_count(cls, count: int) -> float:
        multiplier = 1.0
        for threshold, configured in cls.PENALTY_LEVELS:
            if count >= threshold:
                multiplier = configured
        return multiplier

    @staticmethod
    def _predictive_push_base_cooldown_seconds() -> int:
        from core.cognitive.delivery_router import DeliveryBudgetPolicy

        policy = DeliveryBudgetPolicy.from_config()
        return max(1, int(policy.same_topic_cooldown_hours)) * 3600

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
            self.DB_PATH = get_config().database_dir / "application_hub.db"
        self._init_db()
        # 统一使用 push_penalty.db 作为推送惩罚/冷却库；
        # 若传入了 db_path，则惩罚库放在同目录下的 push_penalty.db，便于测试隔离。
        penalty_db_path = self.DB_PATH.with_name("push_penalty.db")
        self.penalty_tracker = PushPenaltyTracker(db_path=str(penalty_db_path))
        self._last_output_time = self._load_last_output_time()

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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS hub_meta (
                    key TEXT PRIMARY KEY,
                    value REAL NOT NULL
                )
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
        batch_counts: Dict[str, int] = {}
        for output in filtered:
            now = time.time()
            max_per_batch = self._max_per_batch(output.output_type)
            if max_per_batch is not None:
                if batch_counts.get(output.output_type, 0) >= max_per_batch:
                    logger.debug(
                        "[%s] 已达单批上限 %d，跳过 %s",
                        output.output_type,
                        max_per_batch,
                        output.knowledge_id,
                    )
                    continue

            if self._check_rate_limit(output):
                # 全局频率限制
                if output.output_type != "search":
                    if now - self._last_output_time < MIN_INTERVAL_SEC:
                        logger.debug(
                            "全局间隔限制：跳过 %s",
                            output.knowledge_id,
                        )
                        continue
                    self._last_output_time = now
                    self._save_last_output_time(now)
                result.append(output)
                self._record_output(output)
                if max_per_batch is not None:
                    batch_counts[output.output_type] = batch_counts.get(output.output_type, 0) + 1

        return result

    def _check_rate_limit(self, output: AppOutput) -> bool:
        """检查输出类型速率限制"""
        limits = RATE_LIMITS.get(output.output_type)
        if output.output_type == "predictive_push":
            limits = {}
        if not limits:
            if output.output_type != "predictive_push":
                return True

        # search 类型不受限制
        if output.output_type == "search":
            return True

        # 搜索附加型（只在搜索时展示）
        if limits.get("search_only"):  # type: ignore[attr-defined]
            return False  # 不主动输出

        # 周报型
        if limits.get("weekly_only"):  # type: ignore[attr-defined]
            return False  # 不主动输出

        now = datetime.now()

        # 检查冷却
        cooldown_sec = limits.get("cooldown_sec", 0)  # type: ignore[attr-defined]
        if cooldown_sec:
            last = self._get_last_output_time(output.output_type)
            if last and (now - last).total_seconds() < cooldown_sec:
                return False

        # 检查每日上限
        max_per_day = limits.get("max_per_day")  # type: ignore[attr-defined]
        if max_per_day:
            count = self._count_today_outputs(output.output_type)
            if count >= max_per_day:
                return False

        # 检查 10 分钟上限
        max_per_10min = limits.get("max_per_10min")  # type: ignore[attr-defined]
        if max_per_10min:
            count = self._count_recent_outputs(output.output_type, minutes=10)
            if count >= max_per_10min:
                return False

        # 检查推送惩罚
        if output.output_type == "predictive_push":
            topic = (
                output.knowledge_id.split(":")[0]
                if ":" in output.knowledge_id
                else output.knowledge_id
            )
            if self.penalty_tracker.is_in_cooldown(topic):
                return False

        return True

    @staticmethod
    def _delivery_policy():
        from core.cognitive.delivery_router import DeliveryBudgetPolicy

        return DeliveryBudgetPolicy.from_config()

    def _max_per_batch(self, output_type: str) -> int | None:
        if output_type == "predictive_push":
            return max(1, int(self._delivery_policy().per_task_total))
        limits = RATE_LIMITS.get(output_type) or {}
        if not isinstance(limits, dict):
            return None
        value = limits.get("max_per_batch")
        return int(value) if value is not None else None

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
                "SELECT timestamp FROM output_history WHERE output_type = ? ORDER BY timestamp DESC LIMIT 1",  # noqa: E501
                (output_type,),
            )
            row = cursor.fetchone()
            if row:
                return datetime.fromtimestamp(row[0])
            logger.debug("无 %s 历史输出记录", output_type)
        return None

    def _load_last_output_time(self) -> float:
        """从 hub_meta 读取最近一次主动输出时间，实现全局间隔持久化。"""
        try:
            with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
                cursor = conn.execute(
                    "SELECT value FROM hub_meta WHERE key = ?", ("last_output_time",)
                )
                row = cursor.fetchone()
                if row:
                    return float(row[0])
        except sqlite3.Error as e:
            logger.debug("读取全局间隔失败: %s", e)
        return 0.0

    def _save_last_output_time(self, ts: float) -> None:
        """将最近一次主动输出时间持久化到 hub_meta。"""
        try:
            with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO hub_meta (key, value) VALUES (?, ?)",
                    ("last_output_time", ts),
                )
        except sqlite3.Error as e:
            logger.debug("保存全局间隔失败: %s", e)

    def _count_today_outputs(self, output_type: str) -> int:
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM output_history WHERE output_type = ? AND timestamp >= ?",
                (output_type, today_start.timestamp()),
            )
            return cursor.fetchone()[0]  # type: ignore[no-any-return]

    def _count_recent_outputs(self, output_type: str, minutes: int) -> int:
        """统计指定类型在最近 N 分钟内的输出次数。"""
        cutoff = datetime.now() - timedelta(minutes=minutes)
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM output_history WHERE output_type = ? AND timestamp >= ?",
                (output_type, cutoff.timestamp()),
            )
            return cursor.fetchone()[0]  # type: ignore[no-any-return]

    def _record_output(self, output: AppOutput) -> None:
        with sqlite3.connect(str(self.DB_PATH), timeout=10) as conn:
            conn.execute(
                "INSERT INTO output_history (output_type, knowledge_id, timestamp) VALUES (?, ?, ?)",  # noqa: E501
                (output.output_type, output.knowledge_id, output.timestamp),
            )
