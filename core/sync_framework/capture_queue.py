# -*- coding: utf-8 -*-
"""
CaptureQueue — SQLite 持久化队列

职责：
- 入队/出队/状态管理
- 按 source_agent 隔离
- daemon 重启后 pending 队列可恢复

不重复实现：去重逻辑、分片逻辑、L1 storage 写入（这些由 CaptureService/SyncEngine 负责）
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

from core.config import get_config
from core.db_utils import SqlitePool
from core.pipeline_receipts import SessionEndReceipt
from core.sync_framework.capture_duplicate_policy import CaptureDuplicatePolicy
from core.sync_framework.capture_handoff import CaptureHandoffStore
from core.sync_framework.capture_schema import CaptureQueueSchema

# Constants extracted from magic numbers
MAX_DEPTH = 10000

logger = logging.getLogger(__name__)


class CaptureQueueOperationError(RuntimeError):
    """Typed queue failure that cannot be mistaken for an empty denominator."""

    code = "capture_queue_dequeue_failed"

    def __init__(self, code: str = "capture_queue_dequeue_failed") -> None:
        self.code = str(code)
        super().__init__(self.code)


def _decode_capture_payload(value: Any) -> Dict[str, Any]:
    try:
        payload = json.loads(str(value))
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError):
        raise CaptureQueueOperationError(
            "capture_queue_payload_decode_failed"
        ) from None
    if not isinstance(payload, dict):
        raise CaptureQueueOperationError(
            "capture_queue_payload_not_mapping"
        )
    return payload


class CaptureQueue:
    """SQLite 持久化队列，按来源隔离"""

    def __init__(self, db_path: Optional[str] = None):
        config = get_config()
        self.db_path = Path(db_path or config.database_dir / "capture_queue.db").expanduser()
        # Schema bootstrap/migration is deliberately not a constructor side
        # effect.  A producer must fail before it creates, alters, or cleans
        # evidence; the reviewed-plan `reconcile_capture_queue_schema.py`
        # apply path owns that
        # explicit boundary.
        CaptureQueueSchema.require_current(self.db_path)
        self._lock = threading.Lock()
        self._pool = SqlitePool(self.db_path)
        self._round_robin_index = 0
        # In-memory pending counters to avoid COUNT(*) on every enqueue (S31).
        self._pending_count = 0
        self._pending_by_source: Dict[str, int] = {}
        self.recalibrate_counters()

    def close(self):
        """关闭持久连接"""
        self._pool.close()

    def recalibrate_counters(self) -> None:
        """从数据库刷新内存 pending 计数器；失败时回退到 COUNT。

        注意：查询在锁外完成，只在获取 self._lock 后做赋值，避免长时间阻塞入队。
        """
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT source_agent, COUNT(*) FROM capture_events WHERE status = 'pending' GROUP BY source_agent"  # noqa: E501
            )
            pending_by_source = dict(cursor.fetchall())
            pending_count = sum(pending_by_source.values())
            with self._lock:
                self._pending_by_source = pending_by_source
                self._pending_count = pending_count
        except (sqlite3.Error, OSError) as e:
            logger.warning("[CaptureQueue] 计数器校准失败: %s", e)
            with self._lock:
                self._pending_count = self.get_pending_count()
                self._pending_by_source = {}

    def record_raw_write_failure(
        self,
        *,
        source_agent: str,
        session_id: str,
        turn_number: int,
        content_hash: str,
        error: str,
    ) -> None:
        """Record a canonical raw-store failure that blocked formal capture."""
        conn = self._pool.get_conn()
        try:
            conn.execute(
                """
                INSERT INTO capture_raw_failures (
                    source_agent, session_id, turn_number, content_hash, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source_agent,
                    session_id,
                    turn_number,
                    content_hash,
                    error,
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def enqueue(
        self,
        *,
        source_agent: str,
        session_id: str,
        turn_id: Optional[str],
        turn_number: int,
        payload: Dict[str, Any],
        content_hash: str,
        raw_revision_id: str,
        replay_generation: int = 0,
    ) -> str:
        """
        Enqueue one canonical Raw revision exactly once per generation.

        The permanent receipt is inserted in the same transaction as the
        short-lived queue payload.  Payload cleanup can therefore never make
        an old capture eligible for an accidental second enqueue.

        如果队列满（全局或单来源），返回 'backpressure'。
        成功返回 'queued'。
        """
        config = get_config()
        max_depth = config.get("capture.max_queue_depth", MAX_DEPTH)
        per_source_max = config.get("capture.per_source_max_queue_depth", 1000)
        identity = CaptureDuplicatePolicy.build(
            source_agent=source_agent,
            raw_revision_id=raw_revision_id,
            replay_generation=replay_generation,
        )

        with self._lock:
            if self.is_duplicate(identity.value):
                return "duplicate"
            conn: sqlite3.Connection | None = None
            try:
                conn = self._pool.get_conn()
                cursor = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")
                existing = cursor.execute(
                    "SELECT 1 FROM capture_idempotency_receipts WHERE idempotency_key=?",
                    (identity.value,),
                ).fetchone()
                if existing:
                    conn.rollback()
                    return "duplicate"
                # Capacity is a cross-process invariant.  In-memory counters are
                # only local telemetry and can be stale after another queue
                # instance drains or fills this database.
                pending_count = int(
                    cursor.execute(
                        "SELECT COUNT(*) FROM capture_events WHERE status='pending'"
                    ).fetchone()[0]
                )
                source_pending = int(
                    cursor.execute(
                        "SELECT COUNT(*) FROM capture_events "
                        "WHERE status='pending' AND source_agent=?",
                        (source_agent,),
                    ).fetchone()[0]
                )
                if pending_count >= max_depth:
                    conn.rollback()
                    logger.warning(
                        "[CaptureQueue] 全局队列已满 (%s/%s), source=%s, session=%s",
                        pending_count,
                        max_depth,
                        source_agent,
                        session_id,
                    )
                    return "backpressure"
                if source_pending >= per_source_max:
                    conn.rollback()
                    logger.warning(
                        "[CaptureQueue] 来源队列已满 (%s/%s), source=%s, session=%s",
                        source_pending,
                        per_source_max,
                        source_agent,
                        session_id,
                    )
                    return "backpressure"
                now = datetime.now().isoformat()
                cursor.execute(
                    """
                    INSERT INTO capture_idempotency_receipts (
                        idempotency_key, source_agent, raw_revision_id,
                        replay_generation, capture_event_id, identity_kind, created_at
                    ) VALUES (?, ?, ?, ?, NULL, 'canonical_raw_revision', ?)
                    """,
                    (
                        identity.value,
                        identity.source_agent,
                        identity.raw_revision_id,
                        identity.replay_generation,
                        now,
                    ),
                )
                payload_json = json.dumps(payload, ensure_ascii=False)
                cursor.execute(
                    """
                    INSERT INTO capture_events
                    (dedupe_key, source_agent, session_id, turn_id, turn_number,
                     payload_json, content_hash, raw_revision_id, replay_generation,
                     status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identity.value,
                        source_agent,
                        session_id,
                        turn_id,
                        turn_number,
                        payload_json,
                        content_hash,
                        identity.raw_revision_id,
                        identity.replay_generation,
                        "pending",
                        now,
                    ),
                )
                if cursor.lastrowid is None:
                    raise sqlite3.IntegrityError("capture enqueue did not return an event id")
                event_id = int(cursor.lastrowid)
                cursor.execute(
                    """
                    UPDATE capture_idempotency_receipts SET capture_event_id=?
                    WHERE idempotency_key=?
                    """,
                    (event_id, identity.value),
                )
                conn.commit()
                # 内存计数器递增，避免下次入队再做 COUNT(*)。
                self._pending_count += 1
                self._pending_by_source[source_agent] = (
                    self._pending_by_source.get(source_agent, 0) + 1
                )
                return "queued"

            except sqlite3.IntegrityError:
                try:
                    if conn is None:
                        raise CaptureQueueOperationError(
                            "capture_queue_connection_unavailable"
                        )
                    conn.rollback()
                except (sqlite3.Error, OSError):
                    pass
                return "duplicate" if self.is_duplicate(identity.value) else "error"
            except (sqlite3.Error, OSError) as e:
                logger.error("[CaptureQueue] 入队失败: %s", e, exc_info=True)
                try:
                    if conn is not None:
                        conn.rollback()
                except (sqlite3.Error, OSError):
                    logger.warning(
                        "[capture_queue] (sqlite3.Error, OSError) suppressed", exc_info=True
                    )
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
            conn: sqlite3.Connection | None = None
            try:
                conn = self._pool.get_conn()
                conn.execute("BEGIN IMMEDIATE")
                conn.row_factory = sqlite3.Row  # noqa
                cursor = conn.cursor()

                if source_agent:
                    cursor.execute(
                        """
                        SELECT * FROM capture_events
                        WHERE (status = 'pending' OR (status = 'deferred' AND deferred_until <= ?))
                          AND source_agent = ?
                        ORDER BY session_id, turn_number
                        LIMIT ?
                    """,
                        (datetime.now().isoformat(), source_agent, limit),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT * FROM capture_events
                        WHERE (status = 'pending' OR (status = 'deferred' AND deferred_until <= ?))
                        ORDER BY source_agent, session_id, turn_number
                        LIMIT ?
                    """,
                        (datetime.now().isoformat(), limit),
                    )

                rows = cursor.fetchall()
                results = []
                ids = []
                for row in rows:
                    record = dict(row)
                    record["payload"] = _decode_capture_payload(
                        record["payload_json"]
                    )
                    results.append(record)
                    ids.append(record["id"])

                # 统计实际从 pending 转出的数量（deferred 不计入 pending 计数）
                pending_by_source_delta: Dict[str, int] = {}
                for row in rows:
                    if row["status"] == "pending":
                        src = row["source_agent"]
                        pending_by_source_delta[src] = pending_by_source_delta.get(src, 0) + 1

                # 标记为 processing（带状态校验，防止跨进程/跨线程 race）
                if ids:
                    placeholders = ",".join("?" * len(ids))
                    cursor.execute(
                        f"""
                        UPDATE capture_events
                        SET status = 'processing', processed_at = ?
                        WHERE id IN ({placeholders}) AND status IN ('pending', 'deferred')
                    """,  # nosec B608
                        (datetime.now().isoformat(), *ids),
                    )
                conn.commit()

                # 同步递减内存计数器
                for src, delta in pending_by_source_delta.items():
                    self._pending_count = max(0, self._pending_count - delta)
                    self._pending_by_source[src] = max(
                        0, self._pending_by_source.get(src, 0) - delta
                    )

                return results

            except CaptureQueueOperationError:
                if conn is not None:
                    conn.rollback()
                raise
            except (sqlite3.Error, OSError):
                if conn is not None:
                    conn.rollback()
                raise CaptureQueueOperationError() from None

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
            conn: sqlite3.Connection | None = None
            try:
                conn = self._pool.get_conn()
                conn.execute("BEGIN IMMEDIATE")
                conn.row_factory = sqlite3.Row  # noqa
                cursor = conn.cursor()

                # 1. 获取所有有 pending 或已到期的 deferred 的来源，并按轮询起点旋转顺序
                now = datetime.now().isoformat()
                cursor.execute(
                    """
                    SELECT DISTINCT source_agent FROM capture_events
                    WHERE status = 'pending' OR (status = 'deferred' AND deferred_until <= ?)
                """,
                    (now,),
                )
                sources = [row[0] for row in cursor.fetchall()]
                if not sources:
                    conn.commit()
                    return []

                # 轮询起点旋转，避免排在前面的来源长期优先
                start = self._round_robin_index % len(sources)
                sources = sources[start:] + sources[:start]
                self._round_robin_index = (self._round_robin_index + 1) % len(sources)

                per_source_limit = max(1, limit // len(sources))
                results = []
                ids = []
                slots_remaining = limit

                # 2. Round-robin 每个来源取一部分，严格不超过 limit
                for src in sources:
                    if slots_remaining <= 0:
                        break
                    fetch_limit = min(per_source_limit, slots_remaining)
                    cursor.execute(
                        """
                        SELECT * FROM capture_events
                        WHERE (status = 'pending' OR (status = 'deferred' AND deferred_until <= ?))
                          AND source_agent = ?
                        ORDER BY session_id, turn_number
                        LIMIT ?
                    """,
                        (now, src, fetch_limit),
                    )
                    fetched = cursor.fetchall()
                    for row in fetched:
                        record = dict(row)
                        record["payload"] = _decode_capture_payload(
                            record["payload_json"]
                        )
                        results.append(record)
                        ids.append(record["id"])
                    slots_remaining -= len(fetched)

                # 3. 如果还有余量，按全局顺序补充
                remaining = limit - len(results)
                if remaining > 0:
                    # 排除已经取过的 id（用临时表替代 NOT IN，避免大数据集性能问题）
                    if ids:
                        cursor.execute(
                            "CREATE TEMP TABLE IF NOT EXISTS _deq_exclude (id INTEGER PRIMARY KEY)"
                        )
                        cursor.execute("DELETE FROM _deq_exclude")
                        cursor.executemany(
                            "INSERT OR IGNORE INTO _deq_exclude (id) VALUES (?)",
                            [(i,) for i in ids],
                        )
                        cursor.execute(
                            """
                            SELECT * FROM capture_events e
                            WHERE (e.status = 'pending'
                                   OR (e.status = 'deferred' AND e.deferred_until <= ?))
                              AND NOT EXISTS (SELECT 1 FROM _deq_exclude x WHERE x.id = e.id)
                            ORDER BY e.source_agent, e.session_id, e.turn_number
                            LIMIT ?
                        """,
                            (
                                now,
                                remaining,
                            ),
                        )
                    else:
                        cursor.execute(
                            """
                            SELECT * FROM capture_events
                            WHERE (status = 'pending'
                                   OR (status = 'deferred' AND deferred_until <= ?))
                            ORDER BY source_agent, session_id, turn_number
                            LIMIT ?
                        """,
                            (
                                now,
                                remaining,
                            ),
                        )

                    for row in cursor.fetchall():
                        record = dict(row)
                        record["payload"] = _decode_capture_payload(
                            record["payload_json"]
                        )
                        results.append(record)
                        ids.append(record["id"])

                # 统计实际从 pending 转出的数量（deferred 不计入 pending 计数）
                pending_by_source_delta: Dict[str, int] = {}
                for row in results:
                    if row["status"] == "pending":
                        src = row["source_agent"]
                        pending_by_source_delta[src] = pending_by_source_delta.get(src, 0) + 1

                # 标记为 processing（带状态校验，防止 race）
                if ids:
                    placeholders = ",".join("?" * len(ids))
                    cursor.execute(
                        f"""
                        UPDATE capture_events
                        SET status = 'processing', processed_at = ?
                        WHERE id IN ({placeholders}) AND status IN ('pending', 'deferred')
                    """,  # nosec B608: internally generated ? placeholders
                        (datetime.now().isoformat(), *ids),
                    )
                conn.commit()

                # 同步递减内存计数器
                for src, delta in pending_by_source_delta.items():
                    self._pending_count = max(0, self._pending_count - delta)
                    self._pending_by_source[src] = max(
                        0, self._pending_by_source.get(src, 0) - delta
                    )

                return results

            except CaptureQueueOperationError:
                if conn is not None:
                    conn.rollback()
                raise
            except (sqlite3.Error, OSError):
                if conn is not None:
                    conn.rollback()
                raise CaptureQueueOperationError() from None

    def update_status(
        self,
        event_id: int,
        status: str,
        error: Optional[str] = None,
        deferred_until: Optional[str] = None,
    ):
        """更新事件状态，支持 deferred 重试时间戳。"""
        with self._lock:
            conn: sqlite3.Connection | None = None
            try:
                conn = self._pool.get_conn()
                cursor = conn.cursor()
                conn.execute("BEGIN IMMEDIATE")

                # 查询旧状态，用于同步内存 pending 计数器
                cursor.execute(
                    "SELECT source_agent, status FROM capture_events WHERE id = ?",
                    (event_id,),
                )
                old_row = cursor.fetchone()
                old_status = old_row[1] if old_row else None
                source_agent = old_row[0] if old_row else None

                now = datetime.now().isoformat()
                if error:
                    if deferred_until:
                        cursor.execute(
                            """
                            UPDATE capture_events
                            SET status = ?, error = ?, retry_count = retry_count + 1,
                                deferred_until = ?, processed_at = ?
                            WHERE id = ?
                        """,
                            (status, error, deferred_until, now, event_id),
                        )
                    else:
                        cursor.execute(
                            """
                            UPDATE capture_events
                            SET status = ?, error = ?, retry_count = retry_count + 1,
                                processed_at = ?
                            WHERE id = ?
                        """,
                            (status, error, now, event_id),
                        )
                else:
                    if deferred_until:
                        cursor.execute(
                            """
                            UPDATE capture_events
                            SET status = ?, deferred_until = ?, processed_at = ?
                            WHERE id = ?
                        """,
                            (status, deferred_until, now, event_id),
                        )
                    elif status in ("done", "failed"):
                        cursor.execute(
                            """
                            UPDATE capture_events
                            SET status = ?, processed_at = ?, deferred_until = NULL
                            WHERE id = ?
                        """,
                            (status, now, event_id),
                        )
                    else:
                        cursor.execute(
                            """
                            UPDATE capture_events
                            SET status = ?, processed_at = ?
                            WHERE id = ?
                        """,
                            (status, now, event_id),
                        )
                conn.commit()

                # 同步内存 pending 计数器（在锁内执行，线程安全）
                if old_status is not None:
                    if old_status == "pending" and status != "pending":
                        self._pending_count = max(0, self._pending_count - 1)
                        if source_agent:
                            self._pending_by_source[source_agent] = max(
                                0, self._pending_by_source.get(source_agent, 0) - 1
                            )
                    elif old_status != "pending" and status == "pending":
                        self._pending_count += 1
                        if source_agent:
                            self._pending_by_source[source_agent] = (
                                self._pending_by_source.get(source_agent, 0) + 1
                            )
            except (sqlite3.Error, OSError):
                if conn is not None:
                    conn.rollback()
                raise CaptureQueueOperationError(
                    "capture_queue_status_update_failed"
                ) from None

    # ---------- Capture -> Amphora transactional outbox ----------

    def create_distillation_handoff(
        self,
        source_agent: str,
        session_id: str,
        events: List[Dict[str, Any]],
        *,
        enabled: bool = True,
        input_revision: str = "",
    ) -> Dict[str, Any]:
        """Persist an outbox intent before Capture events can become done."""
        missing_raw = [
            int(event.get("id") or 0)
            for event in events
            if not str(event.get("raw_revision_id") or "")
        ]
        if missing_raw:
            raise ValueError(
                "capture handoff requires canonical raw receipts; "
                f"missing queue event ids: {missing_raw}"
            )
        with self._lock:
            conn = self._pool.get_conn()
            previous_factory = conn.row_factory
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("BEGIN IMMEDIATE")
                handoff = CaptureHandoffStore.create(
                    conn,
                    source_agent=source_agent,
                    session_id=session_id,
                    events=events,
                    enabled=enabled,
                    input_revision=input_revision,
                )
                conn.commit()
                return handoff
            except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
                conn.rollback()
                raise
            finally:
                conn.row_factory = previous_factory

    def list_distillation_handoffs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return outbox rows that still require an Amphora receipt."""
        conn = self._pool.get_conn()
        previous_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            return CaptureHandoffStore.list_dispatchable(conn, limit)
        finally:
            conn.row_factory = previous_factory

    def get_distillation_handoff(
        self,
        source_agent: str,
        session_id: str,
        input_revision: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch the latest or exact Capture-to-Amphora handoff receipt."""
        conn = self._pool.get_conn()
        previous_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            if input_revision:
                row = conn.execute(
                    """
                    SELECT * FROM capture_distillation_handoffs
                    WHERE source_agent=? AND session_id=? AND input_revision=?
                    """,
                    (source_agent, session_id, input_revision),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM capture_distillation_handoffs
                    WHERE source_agent=? AND session_id=?
                    ORDER BY created_at DESC, receipt_id DESC LIMIT 1
                    """,
                    (source_agent, session_id),
                ).fetchone()
            return CaptureHandoffStore.row(row)
        finally:
            conn.row_factory = previous_factory

    def fail_distillation_handoff(self, receipt_id: str, error: str) -> None:
        """Keep a failed cross-database handoff retryable and auditable."""
        with self._lock:
            conn = self._pool.get_conn()
            try:
                CaptureHandoffStore.mark_failed(conn, receipt_id, error)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def commit_distillation_handoff(
        self,
        receipt_id: str,
        *,
        downstream_receipt_id: str,
        downstream_task_id: str,
    ) -> None:
        """Atomically attach the Amphora receipt and mark source events done."""
        with self._lock:
            conn = self._pool.get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                CaptureHandoffStore.commit(
                    conn,
                    receipt_id,
                    downstream_receipt_id=downstream_receipt_id,
                    downstream_task_id=downstream_task_id,
                )
                conn.commit()
            except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
                conn.rollback()
                raise

    def get_pending_count(self, source_agent: Optional[str] = None) -> int:
        """获取 pending 数量"""
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            if source_agent:
                cursor.execute(
                    "SELECT COUNT(*) FROM capture_events WHERE status = 'pending' AND source_agent = ?",  # noqa: E501
                    (source_agent,),
                )
            else:
                cursor.execute("SELECT COUNT(*) FROM capture_events WHERE status = 'pending'")
            return cursor.fetchone()[0]  # type: ignore[no-any-return]
        except (sqlite3.Error, OSError):
            raise CaptureQueueOperationError(
                "capture_queue_pending_count_failed"
            ) from None

    def get_pending_counts_by_source(self) -> Dict[str, int]:
        """按 source_agent 获取 pending 数量，用于状态面板和 MCP 诊断。"""
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT source_agent, COUNT(*) FROM capture_events WHERE status = 'pending' GROUP BY source_agent"  # noqa: E501
            )
            return {str(source): int(count) for source, count in cursor.fetchall()}
        except (sqlite3.Error, OSError):
            raise CaptureQueueOperationError(
                "capture_queue_pending_counts_failed"
            ) from None

    def get_status(
        self,
        source_agent: str,
        session_id: str,
        turn_number: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """查询指定事件状态"""
        conn = None
        previous_factory = None
        try:
            conn = self._pool.get_conn()
            previous_factory = conn.row_factory
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if turn_number is not None:
                cursor.execute(
                    """
                    SELECT * FROM capture_events
                    WHERE source_agent = ? AND session_id = ? AND turn_number = ?
                    ORDER BY created_at DESC LIMIT 1
                """,
                    (source_agent, session_id, turn_number),
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM capture_events
                    WHERE source_agent = ? AND session_id = ?
                    ORDER BY created_at DESC LIMIT 1
                """,
                    (source_agent, session_id),
                )
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
        except (sqlite3.Error, OSError):
            raise CaptureQueueOperationError(
                "capture_queue_status_read_failed"
            ) from None
        finally:
            if conn is not None:
                conn.row_factory = previous_factory

    def get_event_statuses(self, event_ids: List[int]) -> Dict[int, str]:
        """Return exact post-batch states for worker receipt reporting."""
        if not event_ids:
            return {}
        placeholders = ",".join("?" for _ in event_ids)
        conn = self._pool.get_conn()
        rows = conn.execute(
            f"SELECT id, status FROM capture_events WHERE id IN ({placeholders})",  # nosec B608
            tuple(int(value) for value in event_ids),
        ).fetchall()
        return {int(event_id): str(status) for event_id, status in rows}

    def is_duplicate(self, idempotency_key: str) -> bool:
        """Return permanent receipt existence; queue payload age is irrelevant."""
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 1 FROM capture_idempotency_receipts
                WHERE idempotency_key = ? LIMIT 1
                """,
                (idempotency_key,),
            )
            return cursor.fetchone() is not None
        except (sqlite3.Error, OSError):
            raise CaptureQueueOperationError(
                "capture_queue_idempotency_read_failed"
            ) from None

    def reset_processing_to_pending(self) -> int:
        """启动时恢复：将所有卡住的 processing 状态回退到 pending"""
        conn: sqlite3.Connection | None = None
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE capture_events
                SET status = 'pending', processed_at = NULL, deferred_until = NULL
                WHERE status = 'processing'
            """)
            conn.commit()
            reset_count = cursor.rowcount
            if reset_count > 0:
                logger.info(
                    "[CaptureQueue] 崩溃恢复: %s 个 processing 事件已回退到 pending", reset_count
                )
                self.recalibrate_counters()
            return reset_count
        except (sqlite3.Error, OSError):
            if conn is not None:
                conn.rollback()
            raise CaptureQueueOperationError(
                "capture_queue_recovery_failed"
            ) from None

    def dequeue_by_session(
        self,
        source_agent: str,
        session_id: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """按 session 过滤出队（用于 end_session flush）"""
        with self._lock:
            conn: sqlite3.Connection | None = None
            try:
                conn = self._pool.get_conn()
                conn.execute("BEGIN IMMEDIATE")
                conn.row_factory = sqlite3.Row  # noqa
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT * FROM capture_events
                    WHERE (status = 'pending' OR (status = 'deferred' AND deferred_until <= ?))
                      AND source_agent = ? AND session_id = ?
                    ORDER BY turn_number
                    LIMIT ?
                """,
                    (datetime.now().isoformat(), source_agent, session_id, limit),
                )
                rows = cursor.fetchall()
                results = []
                ids = []
                for row in rows:
                    record = dict(row)
                    record["payload"] = _decode_capture_payload(
                        record["payload_json"]
                    )
                    results.append(record)
                    ids.append(record["id"])

                # 统计实际从 pending 转出的数量（deferred 不计入 pending 计数）
                pending_delta = sum(1 for row in rows if row["status"] == "pending")

                if ids:
                    placeholders = ",".join("?" * len(ids))
                    cursor.execute(
                        f"""
                        UPDATE capture_events
                        SET status = 'processing', processed_at = ?
                        WHERE id IN ({placeholders}) AND status IN ('pending', 'deferred')
                    """,  # nosec B608: internally generated ? placeholders
                        (datetime.now().isoformat(), *ids),
                    )
                conn.commit()

                # 同步递减内存计数器
                if pending_delta:
                    self._pending_count = max(0, self._pending_count - pending_delta)
                    self._pending_by_source[source_agent] = max(
                        0, self._pending_by_source.get(source_agent, 0) - pending_delta
                    )

                return results
            except CaptureQueueOperationError:
                if conn is not None:
                    conn.rollback()
                raise
            except (sqlite3.Error, OSError):
                if conn is not None:
                    conn.rollback()
                raise CaptureQueueOperationError() from None

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
        except (sqlite3.Error, OSError):
            raise CaptureQueueOperationError(
                "capture_queue_backoff_read_failed"
            ) from None
        return {"error_count": 0, "last_retry_at": None}

    def set_backoff_state(self, source_agent: str, error_count: int, last_retry_at: str):
        """写入来源退避状态"""
        conn: sqlite3.Connection | None = None
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO source_backoff (source_agent, error_count, last_retry_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_agent) DO UPDATE SET
                    error_count = excluded.error_count,
                    last_retry_at = excluded.last_retry_at
            """,
                (source_agent, error_count, last_retry_at),
            )
            conn.commit()
        except (sqlite3.Error, OSError):
            if conn is not None:
                conn.rollback()
            raise CaptureQueueOperationError(
                "capture_queue_backoff_write_failed"
            ) from None

    def clear_backoff_state(self, source_agent: str):
        """清除来源退避状态"""
        conn: sqlite3.Connection | None = None
        try:
            conn = self._pool.get_conn()
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM source_backoff WHERE source_agent = ?",
                (source_agent,),
            )
            conn.commit()
        except (sqlite3.Error, OSError):
            if conn is not None:
                conn.rollback()
            raise CaptureQueueOperationError(
                "capture_queue_backoff_clear_failed"
            ) from None

    # ---------- session end 标记（供 end_session 异步 flush）----------

    def mark_session_end(self, source_agent: str, session_id: str) -> SessionEndReceipt:
        """Persist a session-end handoff and return its durable receipt."""
        now = datetime.now().isoformat()
        receipt_id = "session-end-" + hashlib.sha256(
            f"{source_agent}\0{session_id}".encode("utf-8")
        ).hexdigest()[:24]
        conn = self._pool.get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO session_end_events (
                    source_agent, session_id, receipt_id, status, error, created_at, updated_at
                ) VALUES (?, ?, ?, 'handoff_pending', '', ?, ?)
                ON CONFLICT(source_agent, session_id) DO UPDATE SET
                    receipt_id = excluded.receipt_id,
                    status = 'handoff_pending',
                    error = '',
                    updated_at = excluded.updated_at
                """,
                (source_agent, session_id, receipt_id, now, now),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return SessionEndReceipt(receipt_id, source_agent, session_id, "handoff_pending")

    def get_session_end_markers(self) -> List[Dict[str, str]]:
        """获取所有待处理的 session end 标记"""
        conn = self._pool.get_conn()
        previous_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT source_agent, session_id, receipt_id, status, error
                FROM session_end_events WHERE status IN ('handoff_pending', 'retryable_failed')
                """
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.row_factory = previous_factory

    def clear_session_end_marker(self, source_agent: str, session_id: str):
        """Commit a session-end receipt without deleting its audit record."""
        conn = self._pool.get_conn()
        try:
            conn.execute(
                """
                UPDATE session_end_events
                SET status='committed', error='', updated_at=?
                WHERE source_agent=? AND session_id=?
                """,
                (datetime.now().isoformat(), source_agent, session_id),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def fail_session_end(self, source_agent: str, session_id: str, error: str) -> None:
        """Persist a retryable session-end failure instead of masking it."""
        conn = self._pool.get_conn()
        try:
            conn.execute(
                """
                UPDATE session_end_events SET status='retryable_failed', error=?, updated_at=?
                WHERE source_agent=? AND session_id=?
                """,
                (str(error)[:2000], datetime.now().isoformat(), source_agent, session_id),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def get_session_end_receipt(self, source_agent: str, session_id: str) -> Dict[str, Any]:
        """Read the durable session-end state for status/MCP surfaces."""
        conn = self._pool.get_conn()
        previous_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT source_agent, session_id, receipt_id, status, error, created_at, updated_at
                FROM session_end_events WHERE source_agent=? AND session_id=?
                """,
                (source_agent, session_id),
            ).fetchone()
            return dict(row) if row else {}
        finally:
            conn.row_factory = previous_factory

    def session_has_open_capture(self, source_agent: str, session_id: str) -> bool:
        """Return True while any Capture event still lacks a terminal handoff outcome."""
        conn = self._pool.get_conn()
        row = conn.execute(
            """
            SELECT 1 FROM capture_events
            WHERE source_agent=? AND session_id=?
              AND status IN ('pending', 'deferred', 'processing', 'handoff_pending')
            LIMIT 1
            """,
            (source_agent, session_id),
        ).fetchone()
        return row is not None

    def session_failed_count(self, source_agent: str, session_id: str) -> int:
        """Count terminal Capture failures that prevent a successful session-end receipt."""
        conn = self._pool.get_conn()
        return int(
            conn.execute(
                """
                SELECT COUNT(*) FROM capture_events
                WHERE source_agent=? AND session_id=? AND status='failed'
                """,
                (source_agent, session_id),
            ).fetchone()[0]
        )
