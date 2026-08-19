"""Read-only status access for the Capture queue.

Status/health calls must never instantiate the mutable queue/service stack:
those constructors can provision schema, reset state, or run retention work.
This module owns the small, read-only query seam instead.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from core.ops.durable_io import DurableIOError, inspect_path_kind
from core.ops.readiness_query_budget import connect_readonly_sqlite


class CaptureStatusReader:
    """Read existing Capture queue state without provisioning or cleanup."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)

    def _base(self, source_agent: str, session_id: str, turn_number: int | None) -> dict[str, Any]:
        return {
            "source_agent": source_agent,
            "session_id": session_id,
            "turn_number": turn_number,
            "retry_count": 0,
            "error": "",
            "input_revision": "",
            "handoff_receipt_id": "",
            "handoff_status": "",
            "downstream_receipt_id": "",
            "session_end_receipt_id": "",
            "session_end_status": "",
            "pending_counts": {"total": 0, "by_source": {}},
        }

    def _connect(self) -> sqlite3.Connection:
        # Immutable mode prevents a diagnostic read from creating or changing
        # -wal/-shm sidecars.  Active WAL frames are handled fail-closed below.
        return connect_readonly_sqlite(
            self.db_path,
            immutable=True,
        )

    def _has_uncheckpointed_wal(self) -> bool:
        wal_path = self.db_path.with_name(f"{self.db_path.name}-wal")
        try:
            wal_kind = inspect_path_kind(wal_path)
            if wal_kind == "missing":
                return False
            if wal_kind != "file":
                return True
            return wal_path.stat().st_size > 0
        except (DurableIOError, OSError):
            return True

    @staticmethod
    def _has_table(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    @staticmethod
    def _row_dict(cursor: sqlite3.Cursor, row: tuple[Any, ...] | None) -> dict[str, Any]:
        if row is None:
            return {}
        columns = [str(column[0]) for column in cursor.description or ()]
        return dict(zip(columns, row))

    @staticmethod
    def _pending_counts(conn: sqlite3.Connection) -> dict[str, Any]:
        cursor = conn.execute(
            "SELECT source_agent, COUNT(*) FROM capture_events "
            "WHERE status='pending' GROUP BY source_agent"
        )
        by_source = {str(source): int(count) for source, count in cursor.fetchall()}
        return {"total": sum(by_source.values()), "by_source": by_source}

    def read(
        self,
        source_agent: str,
        session_id: str,
        turn_number: int | None = None,
    ) -> dict[str, Any]:
        """Return one queue state or ``uninitialized``/``not_found`` safely."""
        result = self._base(source_agent, session_id, turn_number)
        try:
            db_kind = inspect_path_kind(self.db_path)
        except DurableIOError:
            result["status"] = "unavailable"
            result["error"] = "capture queue state is unavailable"
            return result
        if db_kind == "missing":
            result["status"] = "uninitialized"
            return result
        if db_kind != "file":
            result["status"] = "unavailable"
            result["error"] = "capture queue state is not a regular file"
            return result
        if self._has_uncheckpointed_wal():
            result["status"] = "read_only_wal_pending"
            result["error"] = "capture queue has uncheckpointed WAL frames"
            return result
        try:
            with self._connect() as conn:
                if not self._has_table(conn, "capture_events"):
                    result["status"] = "uninitialized"
                    return result
                result["pending_counts"] = self._pending_counts(conn)
                if turn_number is None:
                    cursor = conn.execute(
                        "SELECT * FROM capture_events WHERE source_agent=? AND session_id=? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (source_agent, session_id),
                    )
                else:
                    cursor = conn.execute(
                        "SELECT * FROM capture_events "
                        "WHERE source_agent=? AND session_id=? AND turn_number=? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (source_agent, session_id, turn_number),
                    )
                record = self._row_dict(cursor, cursor.fetchone())
                if not record:
                    result["status"] = "not_found"
                    return result
                result.update(
                    {
                        "status": str(record.get("status") or "uninitialized"),
                        "turn_number": record.get("turn_number", turn_number),
                        "retry_count": int(record.get("retry_count") or 0),
                        "created_at": record.get("created_at"),
                        "processed_at": record.get("processed_at"),
                        "error": str(record.get("error") or ""),
                    }
                )
                self._add_handoff(conn, result, source_agent, session_id)
                self._add_session_end(conn, result, source_agent, session_id)
                return result
        except (OSError, sqlite3.Error):
            result["status"] = "unavailable"
            result["error"] = "capture queue state is unreadable"
            return result

    def _add_handoff(
        self,
        conn: sqlite3.Connection,
        result: dict[str, Any],
        source_agent: str,
        session_id: str,
    ) -> None:
        if not self._has_table(conn, "capture_distillation_handoffs"):
            return
        cursor = conn.execute(
            "SELECT * FROM capture_distillation_handoffs "
            "WHERE source_agent=? AND session_id=? "
            "ORDER BY created_at DESC, receipt_id DESC LIMIT 1",
            (source_agent, session_id),
        )
        handoff = self._row_dict(cursor, cursor.fetchone())
        result.update(
            {
                "input_revision": str(handoff.get("input_revision") or ""),
                "handoff_receipt_id": str(handoff.get("receipt_id") or ""),
                "handoff_status": str(handoff.get("status") or ""),
                "downstream_receipt_id": str(handoff.get("downstream_receipt_id") or ""),
            }
        )

    def _add_session_end(
        self,
        conn: sqlite3.Connection,
        result: dict[str, Any],
        source_agent: str,
        session_id: str,
    ) -> None:
        if not self._has_table(conn, "session_end_events"):
            return
        cursor = conn.execute(
            "SELECT * FROM session_end_events WHERE source_agent=? AND session_id=?",
            (source_agent, session_id),
        )
        receipt = self._row_dict(cursor, cursor.fetchone())
        result.update(
            {
                "session_end_receipt_id": str(receipt.get("receipt_id") or ""),
                "session_end_status": str(receipt.get("status") or ""),
            }
        )
