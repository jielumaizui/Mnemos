"""Durable multi-process lease contract for EventBus delivery."""

from __future__ import annotations

import contextvars
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, TYPE_CHECKING

from core.db_utils import render_sql
from core.event_bus_contract import Event
from core.ops.event_subject_provenance import (
    TOMBSTONE_TABLE as EVENT_SUBJECT_TOMBSTONE_TABLE,
    event_is_tombstoned,
    record_event_subject_provenance,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "mnemos.event_bus_lease.v1"
REQUIRED_COLUMNS = {
    "lease_owner",
    "lease_expires_at",
    "lease_epoch",
}
_PENDING_EVENT_COUNT_SQL = render_sql(
    """SELECT COUNT(*) as cnt FROM events WHERE status = 'pending'
       AND (next_attempt_at='' OR next_attempt_at<=?)
       AND NOT EXISTS (
           SELECT 1 FROM {tombstone_table} AS tombstone
           WHERE tombstone.trace_id=events.trace_id
       )""",
    identifiers={"tombstone_table": EVENT_SUBJECT_TOMBSTONE_TABLE},
)
_PENDING_EVENT_ROWS_SQL = render_sql(
    """SELECT * FROM events
       WHERE status = 'pending' AND (next_attempt_at='' OR next_attempt_at<=?)
         AND NOT EXISTS (
             SELECT 1 FROM {tombstone_table} AS tombstone
             WHERE tombstone.trace_id=events.trace_id
         )
       ORDER BY id ASC
       LIMIT ?""",
    identifiers={"tombstone_table": EVENT_SUBJECT_TOMBSTONE_TABLE},
)


class EventBusLeaseLifecycleMixin:
    """Own multi-process claims, lease renewal, and fenced handler execution."""

    if TYPE_CHECKING:
        _handler_executor: Any
        _handler_timeout_seconds: float
        _lease_owner: str
        _lease_seconds: float
        _runtime_config: Any

        def _get_conn(self) -> sqlite3.Connection: ...

        def _release_transient_connections(self) -> None: ...

    def _release_owned_leases(self) -> None:
        """Voluntarily release queued work when this process shuts down cleanly."""

        try:
            conn = self._get_conn()
            conn.execute(
                """UPDATE events
                   SET status='pending', lease_owner='', lease_expires_at=''
                   WHERE status='processing' AND lease_owner=?""",
                (self._lease_owner,),
            )
            conn.commit()
        except (OSError, sqlite3.Error):
            logger.warning("EventBus lease release failed", exc_info=True)

    def _lease_expiry(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=self._lease_seconds)).isoformat()

    def _claim_pending_batch(self, limit: int) -> tuple[list[Event], int]:
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """UPDATE events
                   SET status='pending', lease_owner='', lease_expires_at=''
                   WHERE status='processing'
                     AND (lease_owner='' OR lease_expires_at='' OR lease_expires_at<=?)""",
                (now,),
            )
            total_row = conn.execute(_PENDING_EVENT_COUNT_SQL, (now,)).fetchone()
            total = int(total_row["cnt"]) if total_row else 0
            rows = conn.execute(
                _PENDING_EVENT_ROWS_SQL,
                (now, max(1, int(limit))),
            ).fetchall()
            claimed: list[sqlite3.Row] = []
            for row in rows:
                updated = conn.execute(
                    """UPDATE events
                       SET status='processing', lease_owner=?, lease_expires_at=?,
                           lease_epoch=lease_epoch + 1
                       WHERE id=? AND status='pending'
                         AND (next_attempt_at='' OR next_attempt_at<=?)""",
                    (self._lease_owner, self._lease_expiry(), int(row["id"]), now),
                ).rowcount
                if updated == 1:
                    claimed.append(row)
            conn.commit()
        except (OSError, sqlite3.Error):
            conn.rollback()
            raise
        events = [event for row in claimed if (event := Event.from_row(row)) is not None]
        return events, total

    def _claim_event(self, event: Event) -> bool:
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if event_is_tombstoned(conn, event.trace_id):
                conn.rollback()
                return False
            row = conn.execute(
                """SELECT status, next_attempt_at, lease_owner, lease_expires_at
                   FROM events WHERE trace_id=?""",
                (event.trace_id,),
            ).fetchone()
            if row is None:
                prior_claim = conn.execute(
                    "SELECT 1 FROM event_trace_claims WHERE trace_id=?",
                    (event.trace_id,),
                ).fetchone()
                if prior_claim is not None:
                    conn.rollback()
                    return False
                payload_json = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
                payload_fingerprint = hashlib.sha256(
                    "\x1f".join((event.event_type, event.source, payload_json)).encode("utf-8")
                ).hexdigest()
                conn.execute(
                    """INSERT INTO event_trace_claims
                       (trace_id, event_type, source, payload_fingerprint, claimed_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        event.trace_id,
                        event.event_type,
                        event.source,
                        payload_fingerprint,
                        now,
                    ),
                )
                conn.execute(
                    """INSERT INTO events
                       (timestamp, trace_id, event_type, source, payload_json,
                        status, retry_count, lease_owner, lease_expires_at,
                        lease_epoch, created_at)
                       VALUES (?, ?, ?, ?, ?, 'processing', 0, ?, ?, 1, ?)""",
                    (
                        event.timestamp,
                        event.trace_id,
                        event.event_type,
                        event.source,
                        payload_json,
                        self._lease_owner,
                        self._lease_expiry(),
                        now,
                    ),
                )
                record_event_subject_provenance(
                    conn,
                    trace_id=event.trace_id,
                    subject_provenance=event.subject_provenance,
                    ownership_config=self._runtime_config,
                )
                conn.commit()
                return True
            status = str(row["status"])
            owner = str(row["lease_owner"])
            expires = str(row["lease_expires_at"])
            due = not str(row["next_attempt_at"]) or str(row["next_attempt_at"]) <= now
            claimable = status == "pending" and due
            claimable = claimable or (
                status == "processing" and (not owner or not expires or expires <= now)
            )
            if status == "processing" and owner == self._lease_owner:
                conn.execute(
                    "UPDATE events SET lease_expires_at=? WHERE trace_id=?",
                    (self._lease_expiry(), event.trace_id),
                )
                conn.commit()
                return True
            if not claimable:
                conn.rollback()
                return False
            updated = conn.execute(
                """UPDATE events
                   SET status='processing', lease_owner=?, lease_expires_at=?,
                       lease_epoch=lease_epoch + 1
                   WHERE trace_id=? AND (
                       (status='pending' AND (next_attempt_at='' OR next_attempt_at<=?))
                       OR (status='processing' AND
                           (lease_owner='' OR lease_expires_at='' OR lease_expires_at<=?))
                   )""",
                (
                    self._lease_owner,
                    self._lease_expiry(),
                    event.trace_id,
                    now,
                    now,
                ),
            ).rowcount
            conn.commit()
            return updated == 1
        except (OSError, sqlite3.Error):
            conn.rollback()
            raise

    def _renew_event_lease(self, trace_id: str) -> bool:
        conn = self._get_conn()
        updated = conn.execute(
            """UPDATE events SET lease_expires_at=?
               WHERE trace_id=? AND status='processing' AND lease_owner=?""",
            (self._lease_expiry(), trace_id, self._lease_owner),
        ).rowcount
        conn.commit()
        return updated == 1

    def _invoke_handler(
        self,
        handler: Callable[[Event], Any],
        event: Event,
        handler_name: str,
    ) -> Any:
        """Run one handler under timeout and renewable-lease semantics."""

        # A timed-out Python thread cannot be cancelled. Releasing a cognition
        # episode lease while that thread can still commit target effects would
        # permit a second process to replay the same durable command. These
        # exactly-once projection handlers therefore run to a typed terminal
        # outcome under a renewable lease.
        timeout = (
            0.0
            if event.event_type == "cognition_episode_committed"
            else self._handler_timeout_seconds
        )
        heartbeat_interval = max(0.1, min(10.0, self._lease_seconds / 3.0))
        if timeout > 0 and self._handler_executor:
            context = contextvars.copy_context()
            future = self._handler_executor.submit(context.run, handler, event)
            started = time.monotonic()
            while True:
                elapsed = time.monotonic() - started
                remaining = timeout - elapsed
                if remaining <= 0:
                    logger.error(
                        "[EventBus] 处理器 %s 处理事件 %s 超时 (%.1fs)",
                        handler_name,
                        event.event_type,
                        timeout,
                    )
                    raise TimeoutError
                wait_seconds = min(heartbeat_interval, remaining)
                try:
                    return future.result(timeout=wait_seconds)
                except TimeoutError:
                    if future.done():
                        return future.result()
                    if not self._renew_event_lease(event.trace_id):
                        raise RuntimeError("EventBus lease was lost during handler execution")

        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()

        def renew_while_running() -> None:
            while not heartbeat_stop.wait(heartbeat_interval):
                try:
                    if not self._renew_event_lease(event.trace_id):
                        lease_lost.set()
                        return
                except (OSError, sqlite3.Error):
                    logger.error("EventBus handler lease renewal failed", exc_info=True)
                    lease_lost.set()
                    return
                finally:
                    self._release_transient_connections()

        heartbeat = threading.Thread(
            target=renew_while_running,
            daemon=True,
            name=f"EventBus-Lease-{event.trace_id}",
        )
        heartbeat.start()
        try:
            result = handler(event)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=min(1.0, heartbeat_interval))
        if lease_lost.is_set():
            raise RuntimeError("EventBus lease was lost during handler execution")
        return result


def event_bus_lease_schema_gaps(conn: sqlite3.Connection) -> list[str]:
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "events" not in tables:
        return ["missing_base_table:events"]
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(events)")}
    gaps = [f"missing_lease_column:events.{name}" for name in sorted(REQUIRED_COLUMNS - columns)]
    indexes = {str(row[1]) for row in conn.execute("PRAGMA index_list(events)").fetchall()}
    if "idx_events_lease" not in indexes:
        gaps.append("missing_lease_index:idx_events_lease")
    return gaps


def inspect_event_bus_lease_schema(db_path: Path) -> dict[str, Any]:
    path = Path(db_path).expanduser().resolve(strict=False)
    if not path.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "path": str(path),
            "ok": False,
            "gaps": ["database_missing"],
        }
    try:
        with sqlite3.connect(f"file:{path.resolve(strict=True)}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            gaps = event_bus_lease_schema_gaps(conn)
    except (OSError, sqlite3.Error) as exc:
        gaps = [f"schema_read_error:{type(exc).__name__}:{exc}"]
    return {
        "schema_version": SCHEMA_VERSION,
        "path": str(path),
        "ok": not gaps,
        "gaps": gaps,
    }


def validate_event_bus_lease_schema(conn: sqlite3.Connection) -> None:
    gaps = event_bus_lease_schema_gaps(conn)
    if gaps:
        raise RuntimeError(
            "EventBus lease schema requires explicit reconciliation: " + ", ".join(gaps)
        )


def initialize_event_bus_lease_schema(db_path: Path) -> None:
    """Explicitly add lease fencing to an initialized EventBus database."""

    path = Path(db_path).expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path), timeout=60) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "events" not in tables:
                raise RuntimeError("EventBus base schema must be initialized first")
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(events)")}
            if "lease_owner" not in columns:
                conn.execute("ALTER TABLE events ADD COLUMN lease_owner TEXT NOT NULL DEFAULT ''")
            if "lease_expires_at" not in columns:
                conn.execute(
                    "ALTER TABLE events ADD COLUMN lease_expires_at TEXT NOT NULL DEFAULT ''"
                )
            if "lease_epoch" not in columns:
                conn.execute("ALTER TABLE events ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_events_lease
                   ON events(status, lease_expires_at, lease_owner)""")
            validate_event_bus_lease_schema(conn)
            conn.commit()
        except (RuntimeError, sqlite3.Error):
            conn.rollback()
            raise
