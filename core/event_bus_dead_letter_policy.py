"""Dead-letter selection and archival policy for the event bus."""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, cast

logger = logging.getLogger(__name__)


class EventBusDeadLetterPolicyMixin:
    """Provide dead-letter selection, replay, and archival policy helpers."""

    # Structural contract supplied by ``EventBus``.
    _get_conn: Callable[[], sqlite3.Connection]
    _dead_letter_replay_max_age_hours: int
    _dead_letter_replay_per_type_limit: int
    _handlers_lock: Any
    _handlers: Dict[str, List[Callable[..., Any]]]
    _NO_PERSIST_EVENT_TYPES: Set[str]

    def _select_no_consumer_candidates(
        self,
        event_types: Optional[List[str]],
        limit: int,
        handled_types: set[str],
        has_wildcard: bool,
    ) -> List[sqlite3.Row]:
        """构建动态 SQL 并获取候选 no_consumer 死信行。"""
        clauses = ["status = 'no_consumer'"]
        params: List[Any] = []
        if not has_wildcard:
            placeholders = ",".join("?" * len(handled_types))
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(sorted(handled_types))
        elif event_types:
            requested_types = set(event_types)
            placeholders = ",".join("?" * len(requested_types))
            clauses.append(f"event_type IN ({placeholders})")
            params.extend(sorted(requested_types))

        params.append(limit)
        conn = self._get_conn()
        return cast(
            List[sqlite3.Row],
            conn.execute(
                f"SELECT * FROM dead_letters WHERE {' AND '.join(clauses)} ORDER BY id ASC LIMIT ?",  # nosec B608
                params,
            ).fetchall(),
        )

    def _filter_replay_rows(
        self,
        rows: List[sqlite3.Row],
        max_age_hours: Optional[int],
        per_type_limit: Optional[int],
    ) -> List[sqlite3.Row]:
        """应用时间窗口和单类型上限过滤候选行。"""
        max_age = (
            max_age_hours
            if max_age_hours is not None
            else self._dead_letter_replay_max_age_hours
        )
        per_type = (
            per_type_limit
            if per_type_limit is not None
            else self._dead_letter_replay_per_type_limit
        )
        cutoff: Optional[datetime] = None
        if max_age:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age)

        type_counts: Dict[str, int] = {}
        filtered_rows = []
        for row in rows:
            if cutoff:
                ts = row["timestamp"] or row["created_at"]
                if ts:
                    try:
                        dt = datetime.fromisoformat(ts)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if dt < cutoff:
                            continue
                    except (ValueError, TypeError):
                        # 时间戳格式异常时保留该行，避免误删
                        pass
            et = row["event_type"]
            if per_type and type_counts.get(et, 0) >= per_type:
                continue
            type_counts[et] = type_counts.get(et, 0) + 1
            filtered_rows.append(row)
        return filtered_rows

    def archive_no_consumer_events(self) -> int:
        """[P1-7] 将无消费者 pending/processing 事件移出活跃队列。"""
        with self._handlers_lock:
            consumer_types = set(self._handlers.keys())
            has_wildcard = "*" in self._handlers
        if has_wildcard:
            return 0
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, event_type FROM events WHERE status IN ('pending', 'processing')"
        ).fetchall()
        telemetry_ids: List[int] = []
        dead_letter_ids: List[int] = []
        for row in rows:
            if row["event_type"] in consumer_types:
                continue
            ids = telemetry_ids if row["event_type"] in self._NO_PERSIST_EVENT_TYPES else dead_letter_ids
            ids.append(row["id"])
        archived = len(telemetry_ids) + len(dead_letter_ids)
        if telemetry_ids:
            placeholders = ",".join("?" * len(telemetry_ids))
            conn.execute(
                f"UPDATE events SET status = 'archived' WHERE id IN ({placeholders})",  # nosec B608
                telemetry_ids,
            )
        if dead_letter_ids:
            placeholders = ",".join("?" * len(dead_letter_ids))
            now_iso = datetime.now(timezone.utc).isoformat()
            reason = "no registered handler for this event type (archive_no_consumer_events)"
            conn.execute(
                f"""INSERT INTO dead_letters
                    (timestamp, trace_id, event_type, source, payload_json,
                     status, retry_count, created_at, failure_reason)
                    SELECT timestamp, trace_id, event_type, source, payload_json,
                           'no_consumer', retry_count, ?, ?
                    FROM events WHERE id IN ({placeholders})""",  # nosec B608: internally generated ? placeholders
                (now_iso, reason, *dead_letter_ids),
            )
            conn.execute(
                f"DELETE FROM events WHERE id IN ({placeholders})",  # nosec B608: internally generated ? placeholders
                dead_letter_ids,
            )
        if archived:
            conn.commit()
            logger.info("[EventBus] 归档 %s 个无消费者事件", archived)
        return archived
