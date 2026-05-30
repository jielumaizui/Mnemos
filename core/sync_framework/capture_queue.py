# -*- coding: utf-8 -*-
"""
CaptureQueue — SQLite 持久化队列

职责：
- 入队/出队/状态管理
- 按 source_agent 隔离
- daemon 重启后 pending 队列可恢复

不重复实现：去重逻辑、分片逻辑、Memos 写入（这些由 CaptureService/SyncEngine 负责）
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.config import get_config
from core.db_utils import SqlitePool

logger = logging.getLogger(__name__)


class CaptureQueue:
    """SQLite 持久化队列，按来源隔离"""

    def __init__(self, db_path: Optional[str] = None):
        config = get_config()
        self.db_path = Path(db_path or config.data_dir / "capture_queue.db").expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._pool = SqlitePool(self.db_path)
        self._init_db()

    def close(self):
        """关闭持久连接"""
        self._pool.close()

    def _init_db(self):
        """初始化队列数据库"""
        conn = self._pool.get_conn()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS capture_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT UNIQUE,
                source_agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id TEXT,
                turn_number INTEGER,
                payload_json TEXT,
                content_hash TEXT,
                status TEXT DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                created_at TEXT,
                processed_at TEXT,
                error TEXT,
                working_dir TEXT
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_dedupe_key
            ON capture_events(dedupe_key)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_source_status
            ON capture_events(source_agent, status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_turn
            ON capture_events(session_id, turn_number)
        """)
        # 退避状态表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS source_backoff (
                source_agent TEXT PRIMARY KEY,
                error_count INTEGER DEFAULT 0,
                last_retry_at TEXT
            )
        """)
        # session 结束标记表（供 end_session 异步 flush 用）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_end_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                created_at TEXT,
                UNIQUE(source_agent, session_id)
            )
        """)
        conn.commit()

    def enqueue(
        self,
        dedupe_key: str,
        source_agent: str,
        session_id: str,
        turn_id: Optional[str],
        turn_number: int,
        payload: Dict[str, Any],
        content_hash: str,
    ) -> str:
        """
        入队。如果 dedupe_key 已存在，返回 'duplicate'。
        如果队列满（全局或单来源），返回 'backpressure'。
        成功返回 'queued'。
        """
        config = get_config()
        max_depth = config.get("capture.max_queue_depth", 10000)
        per_source_max = config.get("capture.per_source_max_queue_depth", 1000)

        with self._lock:
            try:
                conn = self._pool.get_conn()
                cursor = conn.cursor()

                # 检查全局队列深度
                cursor.execute(
                    "SELECT COUNT(*) FROM capture_events WHERE status = 'pending'"
                )
                pending_count = cursor.fetchone()[0]
                if pending_count >= max_depth:
                    logger.warning(
                        f"[CaptureQueue] 全局队列已满 ({pending_count}/{max_depth}), "
                        f"source={source_agent}, session={session_id}"
                    )
                    return "backpressure"

                # 检查单来源队列深度
                cursor.execute(
                    "SELECT COUNT(*) FROM capture_events WHERE status = 'pending' AND source_agent = ?",
                    (source_agent,),
                )
                source_pending = cursor.fetchone()[0]
                if source_pending >= per_source_max:
                    logger.warning(
                        f"[CaptureQueue] 来源队列已满 ({source_pending}/{per_source_max}), "
                        f"source={source_agent}, session={session_id}"
                    )
                    return "backpressure"

                # 尝试插入（dedupe_key 唯一）
                cursor.execute("""
                    INSERT OR IGNORE INTO capture_events
                    (dedupe_key, source_agent, session_id, turn_id, turn_number,
                     payload_json, content_hash, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    dedupe_key,
                    source_agent,
                    session_id,
                    turn_id,
                    turn_number,
                    json.dumps(payload, ensure_ascii=False),
                    content_hash,
                    "pending",
                    datetime.now().isoformat(),
                ))
                conn.commit()

                if cursor.rowcount == 0:
                    return "duplicate"
                return "queued"

            except sqlite3.IntegrityError:
                return "duplicate"
            except Exception as e:
                logger.error(f"[CaptureQueue] 入队失败: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
                return "error"

    def dequeue(
        self,
        source_agent: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        出队。按 source_agent 过滤，同一 session 内按 turn_number 排序。
        出队时状态改为 processing。
        """
        with self._lock:
            try:
                conn = self._pool.get_conn()
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                if source_agent:
                    cursor.execute("""
                        SELECT * FROM capture_events
                        WHERE status = 'pending' AND source_agent = ?
                        ORDER BY session_id, turn_number
                        LIMIT ?
                    """, (source_agent, limit))
                else:
                    cursor.execute("""
                        SELECT * FROM capture_events
                        WHERE status = 'pending'
                        ORDER BY source_agent, session_id, turn_number
                        LIMIT ?
                    """, (limit,))

                rows = cursor.fetchall()
                results = []
                ids = []
                for row in rows:
                    record = dict(row)
                    try:
                        record["payload"] = json.loads(record["payload_json"])
                    except Exception:
                        record["payload"] = {}
                    results.append(record)
                    ids.append(record["id"])

                # 标记为 processing（带状态校验，防止跨进程/跨线程 race）
                if ids:
                    placeholders = ",".join("?" * len(ids))
                    cursor.execute(f"""
                        UPDATE capture_events
                        SET status = 'processing', processed_at = ?
                        WHERE id IN ({placeholders}) AND status = 'pending'
                    """, (datetime.now().isoformat(), *ids))
                    conn.commit()

                return results

            except Exception as e:
                logger.error(f"[CaptureQueue] 出队失败: {e}")
                return []

    def dequeue_fair(
        self,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        公平出队：round-robin 按来源分配配额，避免高流量来源独占 batch。

        策略：
        1. 查询所有有 pending 的来源
        2. 每个来源最多取 limit // num_sources（至少 1）
        3. 如果总数不足 limit，再按全局顺序补充
        """
        with self._lock:
            try:
                conn = self._pool.get_conn()
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                # 1. 获取所有有 pending 的来源
                cursor.execute("""
                    SELECT DISTINCT source_agent FROM capture_events
                    WHERE status = 'pending'
                """)
                sources = [row[0] for row in cursor.fetchall()]
                if not sources:
                    return []

                per_source_limit = max(1, limit // len(sources))
                results = []
                ids = []
                slots_remaining = limit

                # 2. Round-robin 每个来源取一部分，严格不超过 limit
                for src in sources:
                    if slots_remaining <= 0:
                        break
                    fetch_limit = min(per_source_limit, slots_remaining)
                    cursor.execute("""
                        SELECT * FROM capture_events
                        WHERE status = 'pending' AND source_agent = ?
                        ORDER BY session_id, turn_number
                        LIMIT ?
                    """, (src, fetch_limit))
                    fetched = cursor.fetchall()
                    for row in fetched:
                        record = dict(row)
                        try:
                            record["payload"] = json.loads(record["payload_json"])
                        except Exception:
                            record["payload"] = {}
                        results.append(record)
                        ids.append(record["id"])
                    slots_remaining -= len(fetched)

                # 3. 如果还有余量，按全局顺序补充
                remaining = limit - len(results)
                if remaining > 0:
                    # 排除已经取过的 id（用临时表替代 NOT IN，避免大数据集性能问题）
                    if ids:
                        cursor.execute("CREATE TEMP TABLE IF NOT EXISTS _deq_exclude (id INTEGER PRIMARY KEY)")
                        cursor.execute("DELETE FROM _deq_exclude")
                        cursor.executemany(
                            "INSERT OR IGNORE INTO _deq_exclude (id) VALUES (?)",
                            [(i,) for i in ids],
                        )
                        cursor.execute("""
                            SELECT * FROM capture_events e
                            WHERE e.status = 'pending'
                              AND NOT EXISTS (SELECT 1 FROM _deq_exclude x WHERE x.id = e.id)
                            ORDER BY e.source_agent, e.session_id, e.turn_number
                            LIMIT ?
                        """, (remaining,))
                    else:
                        cursor.execute("""
                            SELECT * FROM capture_events
                            WHERE status = 'pending'
                            ORDER BY source_agent, session_id, turn_number
                            LIMIT ?
                        """, (remaining,))

                    for row in cursor.fetchall():
                        record = dict(row)
                        try:
                            record["payload"] = json.loads(record["payload_json"])
                        except Exception:
                            record["payload"] = {}
                        results.append(record)
                        ids.append(record["id"])

                # 标记为 processing（带状态校验，防止 race）
                if ids:
                    placeholders = ",".join("?" * len(ids))
                    cursor.execute(f"""
                        UPDATE capture_events
                        SET status = 'processing', processed_at = ?
                        WHERE id IN ({placeholders}) AND status = 'pending'
                    """, (datetime.now().isoformat(), *ids))
                    conn.commit()

                return results

            except Exception as e:
                logger.error(f"[CaptureQueue] 公平出队失败: {e}")
                return []

    def update_status(
        self,
        event_id: int,
        status: str,
        error: Optional[str] = None,
    ):
        """更新事件状态"""
        with self._lock:
            try:
                conn = self._pool.get_conn()
                cursor = conn.cursor()
                if error:
                    cursor.execute("""
                        UPDATE capture_events
                        SET status = ?, error = ?, retry_count = retry_count + 1
                        WHERE id = ?
                    """, (status, error, event_id))
                else:
                    cursor.execute("""
                        UPDATE capture_events
                        SET status = ?, processed_at = ?
                        WHERE id = ?
                    """, (status, datetime.now().isoformat(), event_id))
                conn.commit()
            except Exception as e:
                logger.error(f"[CaptureQueue] 更新状态失败: {e}")

    def get_pending_count(self, source_agent: Optional[str] = None) -> int:
        """获取 pending 数量"""
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            if source_agent:
                cursor.execute(
                    "SELECT COUNT(*) FROM capture_events WHERE status = 'pending' AND source_agent = ?",
                    (source_agent,),
                )
            else:
                cursor.execute(
                    "SELECT COUNT(*) FROM capture_events WHERE status = 'pending'"
                )
            return cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"[CaptureQueue] 统计失败: {e}")
            return 0

    def get_status(
        self,
        source_agent: str,
        session_id: str,
        turn_number: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """查询指定事件状态"""
        try:
            conn = self._pool.get_conn()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if turn_number is not None:
                cursor.execute("""
                    SELECT * FROM capture_events
                    WHERE source_agent = ? AND session_id = ? AND turn_number = ?
                    ORDER BY created_at DESC LIMIT 1
                """, (source_agent, session_id, turn_number))
            else:
                cursor.execute("""
                    SELECT * FROM capture_events
                    WHERE source_agent = ? AND session_id = ?
                    ORDER BY created_at DESC LIMIT 1
                """, (source_agent, session_id))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
        except Exception as e:
            logger.error(f"[CaptureQueue] 查询状态失败: {e}")
            return None

    def is_duplicate(self, dedupe_key: str, ttl_days: int = 30) -> bool:
        """检查 dedupe_key 是否已存在（30 天内）"""
        cutoff = (datetime.now() - timedelta(days=ttl_days)).isoformat()
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 1 FROM capture_events
                WHERE dedupe_key = ? AND created_at > ?
                LIMIT 1
            """, (dedupe_key, cutoff))
            return cursor.fetchone() is not None
        except Exception as e:
            logger.error(f"[CaptureQueue] 查重失败: {e}")
            return False

    def reset_processing_to_pending(self) -> int:
        """启动时恢复：将所有卡住的 processing 状态回退到 pending"""
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE capture_events
                SET status = 'pending', processed_at = NULL
                WHERE status = 'processing'
            """)
            conn.commit()
            reset_count = cursor.rowcount
            if reset_count > 0:
                logger.warning(
                    f"[CaptureQueue] 崩溃恢复: {reset_count} 个 processing 事件已回退到 pending"
                )
            return reset_count
        except Exception as e:
            logger.error(f"[CaptureQueue] 恢复 processing 失败: {e}")
            return 0

    def dequeue_by_session(
        self,
        source_agent: str,
        session_id: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """按 session 过滤出队（用于 end_session flush）"""
        with self._lock:
            try:
                conn = self._pool.get_conn()
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT * FROM capture_events
                    WHERE status = 'pending' AND source_agent = ? AND session_id = ?
                    ORDER BY turn_number
                    LIMIT ?
                """, (source_agent, session_id, limit))
                rows = cursor.fetchall()
                results = []
                ids = []
                for row in rows:
                    record = dict(row)
                    try:
                        record["payload"] = json.loads(record["payload_json"])
                    except Exception:
                        record["payload"] = {}
                    results.append(record)
                    ids.append(record["id"])

                if ids:
                    placeholders = ",".join("?" * len(ids))
                    cursor.execute(f"""
                        UPDATE capture_events
                        SET status = 'processing', processed_at = ?
                        WHERE id IN ({placeholders}) AND status = 'pending'
                    """, (datetime.now().isoformat(), *ids))
                    conn.commit()

                return results
            except Exception as e:
                logger.error(f"[CaptureQueue] 按 session 出队失败: {e}")
                return []

    def get_backoff_state(self, source_agent: str) -> Dict[str, Any]:
        """读取来源退避状态"""
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT error_count, last_retry_at FROM source_backoff WHERE source_agent = ?",
                (source_agent,),
            )
            row = cursor.fetchone()
            if row:
                return {"error_count": row[0], "last_retry_at": row[1]}
        except Exception as e:
            logger.error(f"[CaptureQueue] 读取退避状态失败: {e}")
        return {"error_count": 0, "last_retry_at": None}

    def set_backoff_state(self, source_agent: str, error_count: int, last_retry_at: str):
        """写入来源退避状态"""
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO source_backoff (source_agent, error_count, last_retry_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_agent) DO UPDATE SET
                    error_count = excluded.error_count,
                    last_retry_at = excluded.last_retry_at
            """, (source_agent, error_count, last_retry_at))
            conn.commit()
        except Exception as e:
            logger.error(f"[CaptureQueue] 写入退避状态失败: {e}")

    def clear_backoff_state(self, source_agent: str):
        """清除来源退避状态"""
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM source_backoff WHERE source_agent = ?",
                (source_agent,),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"[CaptureQueue] 清除退避状态失败: {e}")

    # ---------- session end 标记（供 end_session 异步 flush）----------

    def mark_session_end(self, source_agent: str, session_id: str):
        """标记 session 已结束，worker 会优先 flush 该 session"""
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO session_end_events (source_agent, session_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_agent, session_id) DO UPDATE SET
                    created_at = excluded.created_at
            """, (source_agent, session_id, datetime.now().isoformat()))
            conn.commit()
        except Exception as e:
            logger.error(f"[CaptureQueue] 标记 session end 失败: {e}")

    def get_session_end_markers(self) -> List[Dict[str, str]]:
        """获取所有待处理的 session end 标记"""
        conn = None
        try:
            conn = self._pool.get_conn()
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT source_agent, session_id FROM session_end_events")
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"[CaptureQueue] 读取 session end 标记失败: {e}")
            return []
        finally:
            if conn is not None:
                conn.row_factory = None

    def clear_session_end_marker(self, source_agent: str, session_id: str):
        """清除指定 session 的 end 标记"""
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM session_end_events WHERE source_agent = ? AND session_id = ?",
                (source_agent, session_id),
            )
            conn.commit()
        except Exception as e:
            logger.error(f"[CaptureQueue] 清除 session end 标记失败: {e}")

    def cleanup_old(self, days: int = 30):
        """清理旧记录"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM capture_events
                WHERE created_at < ? AND status IN ('done', 'duplicate', 'failed')
            """, (cutoff,))
            conn.commit()
        except Exception as e:
            logger.error(f"[CaptureQueue] 清理失败: {e}")
