"""SQLite persistence boundary for SyncEngine's sync-log and audit records."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any


_SECONDS_PER_HOUR = 3600
_SYNCED_TURNS_RESULT_INDEX = 7
_SYNC_LOG_STATE_ERRORS = (
    sqlite3.Error,
    OSError,
    RuntimeError,
    json.JSONDecodeError,
    ValueError,
)


class SyncLogUnavailableError(RuntimeError):
    """Durable sync-log state is unavailable, never semantically empty."""

    def __init__(self, operation: str) -> None:
        self.operation = str(operation)
        super().__init__(f"sync_log_unavailable:{self.operation}")


class SyncLogStore:
    """Read and write SyncEngine's durable deduplication and audit state."""

    def __init__(
        self,
        get_connection: Callable[[], sqlite3.Connection],
        *,
        config: Any,
    ) -> None:
        self._get_connection = get_connection
        self._config = config

    def _assert_write_not_frozen(self, agent_name: str, session_id: str = "") -> None:
        """Refuse a sync/backfill write that would recreate a frozen subject.

        This is deliberately at the durable persistence boundary as well as
        the SyncEngine entry point.  A future backfill caller must not be able
        to bypass a confirmed ownership freeze by calling ``SyncLogStore``
        directly.
        """

        from core.privacy.ownership_freeze import cognitive_write_is_frozen

        if cognitive_write_is_frozen(
            self._config,
            agent=str(agent_name),
            session_id=str(session_id),
        ):
            raise PermissionError("sync source metadata write blocked by data ownership freeze")

    def record_batch(self, records: list[tuple[Any, ...]]) -> bool:
        """Write a prepared batch of sync records in one transaction."""
        if not records:
            return True
        for record in records:
            self._assert_write_not_frozen(str(record[0]), str(record[1]))
        conn: sqlite3.Connection | None = None
        try:
            conn = self._get_connection()
            conn.executemany(
                """
                INSERT OR REPLACE INTO sync_log
                (agent_name, session_id, turn_number, content_hash, backend_uids,
                 status, synced_at, distill_status, error, artifact_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                records,
            )
            conn.commit()
            return True
        except _SYNC_LOG_STATE_ERRORS:
            if conn is not None:
                conn.rollback()
            logging.getLogger(__name__).warning(
                "[SyncEngine] 批量 sync_log 写入失败", exc_info=True
            )
            return False

    def synced_batch(
        self,
        agent_name: str,
        session_id: str,
        turn_numbers: list[int],
    ) -> dict[int, dict[str, Any]]:
        """Return deduplication records for an internally enumerated turn batch."""
        cache: dict[int, dict[str, Any]] = {}
        if not turn_numbers:
            return cache
        try:
            placeholders = ",".join("?" * len(turn_numbers))
            rows = self._get_connection().execute(
                f"""
                SELECT turn_number, content_hash, backend_uids, status
                FROM sync_log
                WHERE agent_name = ? AND session_id = ? AND turn_number IN ({placeholders})
                """,  # nosec B608: internally generated bind-marker count
                (agent_name, session_id, *turn_numbers),
            ).fetchall()
            for row in rows:
                cache[int(row[0])] = {
                    "content_hash": row[1],
                    "backend_uids": json.loads(row[2]) if row[2] else [],
                    "status": row[3],
                }
        except _SYNC_LOG_STATE_ERRORS as exc:
            logging.getLogger(__name__).warning(
                "[SyncEngine] 批量 sync_log 查询失败", exc_info=True
            )
            raise SyncLogUnavailableError("synced_batch") from exc
        return cache

    def exact_persona_turns(
        self,
        agent_name: str,
        session_id: str,
        turn_numbers: list[int],
    ) -> set[int]:
        """Return turns with exactly one durable persona projection.

        Missing and duplicate rows are both non-canonical and must be repaired
        before a synced-turn receipt can credit the persona consumer.
        """
        if not turn_numbers:
            return set()
        try:
            placeholders = ",".join("?" * len(turn_numbers))
            rows = self._get_connection().execute(
                f"""
                SELECT turn_number, COUNT(*)
                FROM user_signals
                WHERE agent = ? AND session_id = ?
                  AND turn_number IN ({placeholders})
                GROUP BY turn_number
                """,  # nosec B608: internally generated bind-marker count
                (agent_name, session_id, *turn_numbers),
            ).fetchall()
            return {
                int(row[0])
                for row in rows
                if int(row[1]) == 1
            }
        except _SYNC_LOG_STATE_ERRORS as exc:
            logging.getLogger(__name__).warning(
                "[SyncEngine] persona projection query failed",
                exc_info=True,
            )
            raise SyncLogUnavailableError("exact_persona_turns") from exc

    def last_synced_turn(self, agent_name: str, session_id: str) -> int:
        """Return the next turn ordinal after the last non-failed sync record."""
        try:
            row = self._get_connection().execute(
                "SELECT MAX(turn_number) FROM sync_log "
                "WHERE agent_name = ? AND session_id = ? AND status != 'failed'",
                (agent_name, session_id),
            ).fetchone()
            return (int(row[0]) + 1) if row and row[0] is not None else 0
        except _SYNC_LOG_STATE_ERRORS as exc:
            logging.getLogger(__name__).warning(
                "Caught unexpected error at sync_engine.py", exc_info=True
            )
            raise SyncLogUnavailableError("last_synced_turn") from exc

    def synced_turns(self, agent_name: str, session_id: str) -> list[int]:
        """Return all successfully persisted turn ordinals for a session."""
        try:
            rows = self._get_connection().execute(
                """
                SELECT turn_number FROM sync_log
                WHERE agent_name = ? AND session_id = ?
                  AND status IN ('new', 'updated', 'synced', 'backfilled', 'skipped_backend')
                """,
                (agent_name, session_id),
            ).fetchall()
            return [int(row[0]) for row in rows]
        except _SYNC_LOG_STATE_ERRORS as exc:
            logging.getLogger(__name__).warning(
                "Caught unexpected error at sync_engine.py", exc_info=True
            )
            raise SyncLogUnavailableError("synced_turns") from exc

    def record_audit(
        self,
        source: str,
        audit_type: str,
        *,
        skipped_missing: int = 0,
        skipped_large: int = 0,
        skipped_stale: int = 0,
        skipped_unchanged: int = 0,
        skipped_over_limit: int = 0,
        selected: int = 0,
        synced_turns: int = 0,
    ) -> None:
        """Append one L1 scan/backfill audit summary."""
        self._assert_write_not_frozen(source)
        conn: sqlite3.Connection | None = None
        try:
            conn = self._get_connection()
            conn.execute(
                """
                INSERT INTO sync_audit
                    (source, audit_type, skipped_missing, skipped_large,
                     skipped_stale, skipped_unchanged, skipped_over_limit,
                     selected, synced_turns, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    audit_type,
                    skipped_missing,
                    skipped_large,
                    skipped_stale,
                    skipped_unchanged,
                    skipped_over_limit,
                    selected,
                    synced_turns,
                    time.time(),
                ),
            )
            conn.commit()
        except _SYNC_LOG_STATE_ERRORS as exc:
            if conn is not None:
                conn.rollback()
            logging.getLogger(__name__).warning("[SyncEngine] record_audit 失败", exc_info=True)
            raise SyncLogUnavailableError("record_audit") from exc

    def audit_summary(self, hours: int = 24) -> dict[str, dict[str, int]]:
        """Aggregate audit counters by source over the requested time window."""
        try:
            cutoff = time.time() - hours * _SECONDS_PER_HOUR
            rows = self._get_connection().execute(
                """
                SELECT source,
                       SUM(skipped_missing) as sm,
                       SUM(skipped_large) as sl,
                       SUM(skipped_stale) as ss,
                       SUM(skipped_unchanged) as su,
                       SUM(skipped_over_limit) as so,
                       SUM(selected) as se,
                       SUM(synced_turns) as st
                FROM sync_audit
                WHERE created_at >= ?
                GROUP BY source
                """,
                (cutoff,),
            ).fetchall()
            return {
                str(row[0]): {
                    "skipped_missing": int(row[1] or 0),
                    "skipped_large": int(row[2] or 0),
                    "skipped_stale": int(row[3] or 0),
                    "skipped_unchanged": int(row[4] or 0),
                    "skipped_over_limit": int(row[5] or 0),
                    "selected": int(row[6] or 0),
                    "synced_turns": int(row[_SYNCED_TURNS_RESULT_INDEX] or 0),
                }
                for row in rows
            }
        except _SYNC_LOG_STATE_ERRORS as exc:
            logging.getLogger(__name__).warning(
                "[SyncEngine] get_audit_summary 失败", exc_info=True
            )
            raise SyncLogUnavailableError("audit_summary") from exc

    def synced_record(
        self,
        agent_name: str,
        session_id: str,
        turn_number: int,
    ) -> dict[str, Any] | None:
        """Return one exact deduplication record for a turn."""
        try:
            row = self._get_connection().execute(
                "SELECT content_hash, backend_uids, status FROM sync_log "
                "WHERE agent_name = ? AND session_id = ? AND turn_number = ?",
                (agent_name, session_id, turn_number),
            ).fetchone()
            if row:
                return {
                    "content_hash": row[0],
                    "backend_uids": json.loads(row[1]) if row[1] else [],
                    "status": row[2],
                }
        except _SYNC_LOG_STATE_ERRORS as exc:
            logging.getLogger(__name__).warning("Caught unexpected error", exc_info=True)
            raise SyncLogUnavailableError("synced_record") from exc
        return None

    def record_sync(
        self,
        agent_name: str,
        session_id: str,
        turn_number: int,
        content_hash: str,
        backend_uids: list[str],
        status: str,
        *,
        error: str | None = None,
        artifact_path: str | None = None,
    ) -> None:
        """Persist one terminal sync outcome and its optional artifact pointer."""
        self._assert_write_not_frozen(agent_name, session_id)
        conn: sqlite3.Connection | None = None
        try:
            conn = self._get_connection()
            conn.execute(
                """
                INSERT OR REPLACE INTO sync_log
                (agent_name, session_id, turn_number, content_hash, backend_uids,
                 status, synced_at, distill_status, error, artifact_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_name,
                    session_id,
                    turn_number,
                    content_hash,
                    json.dumps(backend_uids)
                    if isinstance(backend_uids, list)
                    else json.dumps([backend_uids]),
                    status,
                    datetime.now().isoformat(),
                    "pending" if status in ("new", "updated") else "skipped",
                    error,
                    artifact_path,
                ),
            )
            conn.commit()
        except _SYNC_LOG_STATE_ERRORS as exc:
            if conn is not None:
                conn.rollback()
            logging.getLogger(__name__).warning("Caught unexpected error", exc_info=True)
            raise SyncLogUnavailableError("record_sync") from exc

    def failed_records(
        self,
        agent_name: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return recent failed sync records, optionally scoped to one agent."""
        try:
            if agent_name:
                rows = self._get_connection().execute(
                    "SELECT agent_name, session_id, turn_number, content_hash, error "
                    "FROM sync_log WHERE status = 'failed' AND agent_name = ? "
                    "ORDER BY synced_at DESC LIMIT ?",
                    (agent_name, limit),
                ).fetchall()
            else:
                rows = self._get_connection().execute(
                    "SELECT agent_name, session_id, turn_number, content_hash, error "
                    "FROM sync_log WHERE status = 'failed' "
                    "ORDER BY synced_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [
                {
                    "agent_name": row[0],
                    "session_id": row[1],
                    "turn_number": row[2],
                    "content_hash": row[3],
                    "error": row[4],
                }
                for row in rows
            ]
        except _SYNC_LOG_STATE_ERRORS as exc:
            logging.getLogger(__name__).warning(
                "Caught unexpected error at sync_engine.py", exc_info=True
            )
            raise SyncLogUnavailableError("failed_records") from exc
