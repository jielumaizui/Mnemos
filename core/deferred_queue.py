"""
DeferredQueue — 延迟处理队列

【E14 全库修复】管理待批量处理的评分/蒸馏项，支持延迟执行和聚合。
与 core/hephaestus/deferred_distill.py 协作：deferred_distill 管理蒸馏任务，
DeferredQueue 管理任意类型的延迟处理项。
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Callable
import logging

logger = logging.getLogger(__name__)


class DeferredItem:
    """延迟处理项"""

    def __init__(self, item_type: str, payload: Dict,
                 priority: int = 5, delay_seconds: int = 0):
        self.item_type = item_type
        self.payload = payload
        self.priority = priority
        self.delay_seconds = delay_seconds
        self.created_at = datetime.now().isoformat()
        self.id = None


class DeferredQueue:
    """延迟处理队列：支持优先级、延迟、批量聚合"""

    def __init__(self, db_path: Path = None, max_batch_size: int = 50):
        self.db_path = db_path or Path.home() / ".mnemos" / "deferred_queue.db"
        self.max_batch_size = max_batch_size
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS deferred_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 5,
                    status TEXT NOT NULL DEFAULT 'pending',
                    scheduled_at TEXT NOT NULL,
                    processed_at TEXT,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 3,
                    error TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_deferred_status_priority
                ON deferred_items(status, priority, scheduled_at)
            """)
            conn.commit()

    def enqueue(self, item_type: str, payload: Dict,
                priority: int = 5, delay_seconds: int = 0,
                max_retries: int = 3) -> int:
        """添加延迟处理项"""
        scheduled_at = (datetime.now() + timedelta(seconds=delay_seconds)).isoformat()

        with self._lock:
            with sqlite3.connect(str(self.db_path)) as conn:
                cursor = conn.execute("""
                    INSERT INTO deferred_items
                    (item_type, payload_json, priority, status, scheduled_at)
                    VALUES (?, ?, ?, 'pending', ?)
                """, (item_type, json.dumps(payload, ensure_ascii=False),
                      priority, scheduled_at))
                conn.commit()
                return cursor.lastrowid

    def get_ready_items(self, item_type: str = None,
                        limit: int = None) -> List[DeferredItem]:
        """获取已到执行时间的待处理项"""
        now = datetime.now().isoformat()
        limit = limit or self.max_batch_size

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            if item_type:
                rows = conn.execute("""
                    SELECT * FROM deferred_items
                    WHERE status = 'pending'
                      AND scheduled_at <= ?
                      AND item_type = ?
                      AND retry_count < max_retries
                    ORDER BY priority ASC, scheduled_at ASC
                    LIMIT ?
                """, (now, item_type, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM deferred_items
                    WHERE status = 'pending'
                      AND scheduled_at <= ?
                      AND retry_count < max_retries
                    ORDER BY priority ASC, scheduled_at ASC
                    LIMIT ?
                """, (now, limit)).fetchall()

        items = []
        for row in rows:
            item = DeferredItem(
                item_type=row["item_type"],
                payload=json.loads(row["payload_json"]),
                priority=row["priority"],
            )
            item.id = row["id"]
            item.created_at = row["scheduled_at"]
            item.error = row["error"]
            item.retry_count = row["retry_count"]
            item.max_retries = row["max_retries"]
            items.append(item)
        return items

    def mark_done(self, item_id: int):
        """标记完成"""
        with self._lock:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute("""
                    UPDATE deferred_items
                    SET status = 'done', processed_at = ?
                    WHERE id = ?
                """, (datetime.now().isoformat(), item_id))
                conn.commit()

    def mark_failed(self, item_id: int, error: str = None):
        """标记失败（自动重试）"""
        with self._lock:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("""
                    SELECT retry_count, max_retries FROM deferred_items WHERE id = ?
                """, (item_id,)).fetchone()

                if not row:
                    return

                retry_count = row["retry_count"] + 1
                if retry_count >= row["max_retries"]:
                    status = "archived"
                    conn.execute("""
                        UPDATE deferred_items
                        SET status = ?, retry_count = ?, error = ?
                        WHERE id = ?
                    """, (status, retry_count, error, item_id))
                else:
                    status = "pending"
                    # 指数退避
                    delay = min(3600, 60 * (2 ** retry_count))
                    scheduled_at = (datetime.now() + timedelta(seconds=delay)).isoformat()
                    conn.execute("""
                        UPDATE deferred_items
                        SET status = ?, retry_count = ?, error = ?, scheduled_at = ?
                        WHERE id = ?
                    """, (status, retry_count, error, scheduled_at, item_id))
                conn.commit()

    def process_batch(self, handler: Callable[[DeferredItem], bool],
                      item_type: str = None) -> Dict:
        """
        批量处理就绪的项

        Args:
            handler: 处理函数，接收 DeferredItem，返回是否成功
            item_type: 只处理指定类型的项

        Returns:
            {"processed": int, "succeeded": int, "failed": int}
        """
        items = self.get_ready_items(item_type)
        stats = {"processed": 0, "succeeded": 0, "failed": 0}

        for item in items:
            stats["processed"] += 1
            try:
                success = handler(item)
                if success:
                    self.mark_done(item.id)
                    stats["succeeded"] += 1
                else:
                    self.mark_failed(item.id, "Handler returned False")
                    stats["failed"] += 1
            except Exception as e:
                self.mark_failed(item.id, str(e))
                stats["failed"] += 1

        return stats

    def get_stats(self) -> Dict:
        """队列统计"""
        with sqlite3.connect(str(self.db_path)) as conn:
            total = conn.execute("SELECT COUNT(*) FROM deferred_items").fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM deferred_items WHERE status = 'pending'"
            ).fetchone()[0]
            done = conn.execute(
                "SELECT COUNT(*) FROM deferred_items WHERE status = 'done'"
            ).fetchone()[0]
            archived = conn.execute(
                "SELECT COUNT(*) FROM deferred_items WHERE status = 'archived'"
            ).fetchone()[0]

        return {
            "total": total,
            "pending": pending,
            "done": done,
            "archived": archived,
        }

    def cleanup_old(self, days: int = 7) -> int:
        """清理旧的已完成/已归档项"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._lock:
            with sqlite3.connect(str(self.db_path)) as conn:
                cursor = conn.execute("""
                    DELETE FROM deferred_items
                    WHERE status IN ('done', 'archived')
                      AND processed_at < ?
                """, (cutoff,))
                conn.commit()
                return cursor.rowcount
