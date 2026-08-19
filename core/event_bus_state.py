"""Retry, external-decision, and operational state for EventBus."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

logger = logging.getLogger(__name__)


class EventBusStateLifecycleMixin:
    """Keep durable scheduling and statistics outside the dispatch core."""

    def _mark_failed(self: Any, trace_id: str, reason: str = "") -> None:
        conn = self._get_conn()
        row = conn.execute(
            """SELECT id, retry_count FROM events
               WHERE trace_id=? AND status='processing' AND lease_owner=?""",
            (trace_id, self._lease_owner),
        ).fetchone()
        if not row:
            return
        new_retry = row["retry_count"] + 1
        if new_retry >= self._max_retries:
            conn.execute(
                """INSERT INTO dead_letters (
                       timestamp, trace_id, event_type, source, payload_json,
                       status, retry_count, created_at, failure_reason)
                   SELECT timestamp, trace_id, event_type, source, payload_json,
                       'dead', retry_count + 1, created_at, ?
                   FROM events
                   WHERE trace_id=? AND status='processing' AND lease_owner=?""",
                (reason, trace_id, self._lease_owner),
            )
            deleted = conn.execute(
                """DELETE FROM events
                   WHERE trace_id=? AND status='processing' AND lease_owner=?""",
                (trace_id, self._lease_owner),
            ).rowcount
            if deleted != 1:
                conn.rollback()
                raise RuntimeError("EventBus lease was lost before retry exhaustion")
            conn.execute("DELETE FROM event_deferred_keys WHERE trace_id=?", (trace_id,))
            conn.commit()
            dl_count = int(conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0])
            if dl_count > self._dead_letter_alert:
                logger.warning("EventBus dead letters above alert: %s", dl_count)
            if dl_count > self._dead_letter_max:
                conn.execute(
                    """DELETE FROM dead_letters WHERE id IN (
                         SELECT id FROM dead_letters
                         WHERE event_type!='cognition_episode_committed'
                         ORDER BY id LIMIT ?)""",
                    (dl_count - self._dead_letter_max,),
                )
                conn.commit()
            logger.warning("EventBus event %s exhausted %s retries", trace_id, new_retry)
            return

        delay = min(
            self._retry_max_seconds,
            self._retry_base_seconds * (2 ** max(0, new_retry - 1)),
        )
        next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        updated = conn.execute(
            """UPDATE events
               SET status='pending', retry_count=?, next_attempt_at=?,
                   lease_owner='', lease_expires_at=''
               WHERE trace_id=? AND status='processing' AND lease_owner=?""",
            (new_retry, next_attempt, trace_id, self._lease_owner),
        ).rowcount
        if updated != 1:
            conn.rollback()
            raise RuntimeError("EventBus lease was lost before retry transition")
        conn.commit()
        logger.info("EventBus retry %s/%s scheduled in %.1fs", new_retry, self._max_retries, delay)

    def _mark_deferred(self: Any, trace_id: str, reason: str, deferred_keys: set[str]) -> None:
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        unresolved = {
            key
            for key in deferred_keys
            if conn.execute(
                "SELECT 1 FROM event_resolved_deferred_keys WHERE deferred_key=?",
                (key,),
            ).fetchone()
            is None
        }
        updated = conn.execute(
            """UPDATE events
               SET status=?, next_attempt_at='', lease_owner='', lease_expires_at=''
               WHERE trace_id=? AND status='processing' AND lease_owner=?""",
            (
                "awaiting_decision" if unresolved else "pending",
                trace_id,
                self._lease_owner,
            ),
        ).rowcount
        if updated != 1:
            conn.rollback()
            raise RuntimeError("EventBus lease was lost before deferred transition")
        conn.executemany(
            """INSERT OR IGNORE INTO event_deferred_keys
               (trace_id, deferred_key, created_at) VALUES (?, ?, ?)""",
            ((trace_id, key, now) for key in unresolved),
        )
        conn.commit()
        logger.info("EventBus event %s awaits external decision: %s", trace_id, reason)

    def resume_deferred(self: Any, deferred_key: str) -> int:
        conn = self._get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO event_resolved_deferred_keys
               (deferred_key, resolved_at) VALUES (?, ?)""",
            (deferred_key, datetime.now(timezone.utc).isoformat()),
        )
        rows = conn.execute(
            "SELECT trace_id FROM event_deferred_keys WHERE deferred_key=?",
            (deferred_key,),
        ).fetchall()
        conn.execute("DELETE FROM event_deferred_keys WHERE deferred_key=?", (deferred_key,))
        resumed = 0
        for row in rows:
            trace_id = str(row["trace_id"])
            remaining = conn.execute(
                "SELECT 1 FROM event_deferred_keys WHERE trace_id=? LIMIT 1",
                (trace_id,),
            ).fetchone()
            if remaining is None:
                conn.execute(
                    """UPDATE events SET status='pending', next_attempt_at=''
                       WHERE trace_id=? AND status='awaiting_decision'""",
                    (trace_id,),
                )
                resumed += 1
        conn.commit()
        return resumed

    def stats(self: Any) -> Dict[str, int]:
        conn = self._get_conn()
        result = {
            str(row["status"]): int(row["cnt"])
            for row in conn.execute("""SELECT status, COUNT(*) AS cnt FROM events
                   WHERE status IN ('pending','processing','awaiting_decision','done')
                   GROUP BY status""").fetchall()
        }
        for status in ("pending", "processing", "awaiting_decision", "done"):
            result.setdefault(status, 0)
        result["orphan_awaiting_decision"] = int(
            conn.execute("""SELECT COUNT(*) FROM events AS event
                   WHERE event.status='awaiting_decision' AND NOT EXISTS (
                     SELECT 1 FROM event_deferred_keys AS key
                     WHERE key.trace_id=event.trace_id)""").fetchone()[0]
        )
        result["dead_letters"] = int(
            conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]
        )
        result["total_recorded"] = sum(
            result[key]
            for key in ("pending", "processing", "awaiting_decision", "done", "dead_letters")
        )
        result["queue_depth"] = self._queue.qsize()
        result["max_latency_ms"] = self._max_latency_ms
        return result

    def stats_by_type(self: Any) -> Dict[str, Dict[str, int]]:
        conn = self._get_conn()
        result: Dict[str, Dict[str, int]] = {}
        for status in ("pending", "processing", "awaiting_decision", "done"):
            rows = conn.execute(
                """SELECT event_type, COUNT(*) AS cnt FROM events
                   WHERE status=? GROUP BY event_type""",
                (status,),
            ).fetchall()
            result[status] = {str(row["event_type"]): int(row["cnt"]) for row in rows}
        return result

    def cleanup_stale(self: Any, max_age_hours: int = 24) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        conn = self._get_conn()
        cursor = conn.execute(
            """UPDATE events
               SET status='archived', lease_owner='', lease_expires_at=''
               WHERE timestamp<? AND (
                   status='pending' OR
                   (status='processing' AND
                    (lease_owner='' OR lease_expires_at='' OR lease_expires_at<=?))
               )""",
            (cutoff, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return int(cursor.rowcount)
