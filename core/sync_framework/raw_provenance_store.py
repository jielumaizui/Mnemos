"""Canonical Raw provenance edge and gap persistence.

The public RawEventStore owns turn revisions and lifecycle state.  This module
owns the smaller, append-only provenance surface that links consumers back to
the exact revision spans they used.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any


INTENTIONAL_NO_OBSERVATION_STATUS = "intentional_no_observation"
INTENTIONAL_NO_OBSERVATION_REASONS = {
    "empty_visible_content",
    "no_supported_signal",
}


def _utcnow() -> str:
    # Provenance receipts participate in cross-process freshness gates.  A
    # naive host-local timestamp can look eight hours "in the future" when
    # the readiness auditor correctly compares it to UTC.
    return datetime.now(timezone.utc).isoformat()


def _provenance_edge_id(
    source_revision_id: str,
    span_start: int,
    span_end: int,
    consumer_type: str,
    consumer_id: str,
) -> str:
    raw = (
        f"{source_revision_id}\0{span_start}\0{span_end}\0"
        f"{consumer_type}\0{consumer_id}"
    )
    return "rawedge-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _provenance_gap_id(consumer_type: str, consumer_id: str, reason: str) -> str:
    raw = f"{consumer_type}\0{consumer_id}\0{reason}"
    return "rawgap-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


class RawProvenanceStore:
    """Persist exact Raw provenance without owning Raw revision lifecycle."""

    def __init__(
        self,
        *,
        get_connection: Callable[[], sqlite3.Connection],
        get_turn: Callable[
            [str, sqlite3.Connection],
            Mapping[str, Any] | None,
        ],
        resolve_logical_event_id: Callable[[str, sqlite3.Connection], str],
    ) -> None:
        self._get_connection = get_connection
        self._get_turn = get_turn
        self._resolve_logical_event_id = resolve_logical_event_id

    def _begin_write_transaction(self, code: str) -> sqlite3.Connection:
        conn = self._get_connection()
        if conn.in_transaction:
            raise RuntimeError(f"{code}_transaction_already_active")
        conn.execute("BEGIN IMMEDIATE")
        return conn

    def record_edge(
        self,
        *,
        source_revision_id: str,
        span_start: int,
        span_end: int,
        consumer_type: str,
        consumer_id: str,
    ) -> str:
        """Atomically persist one provenance edge and all coupled projections."""
        conn = self._begin_write_transaction("raw_provenance")
        try:
            edge_id = self._record_edge_in_transaction(
                conn=conn,
                source_revision_id=source_revision_id,
                span_start=span_start,
                span_end=span_end,
                consumer_type=consumer_type,
                consumer_id=consumer_id,
            )
            conn.commit()
            return edge_id
        except BaseException:
            conn.rollback()
            raise

    def _record_edge_in_transaction(
        self,
        *,
        conn: sqlite3.Connection,
        source_revision_id: str,
        span_start: int,
        span_end: int,
        consumer_type: str,
        consumer_id: str,
    ) -> str:
        if span_start < 0 or span_end <= span_start:
            raise ValueError("raw provenance span must be non-empty and ordered")
        if not self._get_turn(source_revision_id, conn):
            raise KeyError(f"unknown raw revision: {source_revision_id}")
        edge_id = _provenance_edge_id(
            source_revision_id,
            span_start,
            span_end,
            consumer_type,
            consumer_id,
        )
        logical_event_id = self._resolve_logical_event_id(
            source_revision_id,
            conn,
        )
        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO raw_provenance_edges (
                edge_id, source_revision_id, span_start, span_end,
                consumer_type, consumer_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                edge_id,
                source_revision_id,
                span_start,
                span_end,
                consumer_type,
                consumer_id,
                _utcnow(),
            ),
        )
        if inserted.rowcount:
            now = _utcnow()
            conn.execute(
                """
                UPDATE raw_metrics
                SET reference_count=reference_count+1,
                    last_accessed_at=?, updated_at=?
                WHERE event_id=?
                """,
                (now, now, logical_event_id),
            )
        if consumer_type == "observation":
            # A later, exact observation edge supersedes a previous terminal
            # no-observation receipt for the same immutable Raw revision.  Do
            # not delete the receipt: the revision history must remain
            # auditable, but the current revision can no longer be counted as
            # intentionally unobserved.
            conn.execute(
                """
                UPDATE raw_provenance_gaps
                SET status='resolved', resolved_at=?
                WHERE consumer_type='observation'
                  AND consumer_id=?
                  AND status=?
                """,
                (_utcnow(), source_revision_id, INTENTIONAL_NO_OBSERVATION_STATUS),
            )
        return edge_id

    def record_intentional_no_observation(
        self,
        *,
        source_revision_id: str,
        reason: str,
    ) -> str:
        """Record a typed terminal result for an eligible Raw revision.

        A ``raw_provenance_gaps`` row is deliberately reused here rather than
        inventing a second Raw-consumer ledger.  The row is a durable,
        revision-addressed receipt: ``consumer_id`` is the immutable Raw
        revision and its terminal status is explicit.  It must never be used
        for extractor failures or unknown eligibility.
        """
        if reason not in INTENTIONAL_NO_OBSERVATION_REASONS:
            raise ValueError(f"unsupported intentional observation reason: {reason}")
        conn = self._begin_write_transaction("raw_provenance_no_observation")
        try:
            turn = self._get_turn(source_revision_id, conn)
            if not turn:
                raise KeyError(f"unknown raw revision: {source_revision_id}")
            source_agent = str(turn.get("source_agent") or "")
            session_id = str(turn.get("session_id") or "")
            consumer_type = "observation"
            consumer_id = source_revision_id
            gap_id = _provenance_gap_id(consumer_type, consumer_id, reason)
            now = _utcnow()
            conn.execute(
                """
                INSERT INTO raw_provenance_gaps (
                    gap_id, consumer_type, consumer_id, source_agent, session_id,
                    reason, status, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(consumer_type, consumer_id, reason) DO UPDATE SET
                    source_agent=excluded.source_agent,
                    session_id=excluded.session_id,
                    status=excluded.status,
                    resolved_at=NULL
                """,
                (
                    gap_id,
                    consumer_type,
                    consumer_id,
                    source_agent,
                    session_id,
                    reason,
                    INTENTIONAL_NO_OBSERVATION_STATUS,
                    now,
                ),
            )
            conn.commit()
            return gap_id
        except BaseException:
            conn.rollback()
            raise

    def list_edges(self, revision_id: str) -> list[dict[str, Any]]:
        rows = self._get_connection().execute(
            """
            SELECT edge_id, source_revision_id, span_start, span_end,
                   consumer_type, consumer_id
            FROM raw_provenance_edges
            WHERE source_revision_id=? ORDER BY created_at, edge_id
            """,
            (revision_id,),
        ).fetchall()
        keys = (
            "edge_id",
            "source_revision_id",
            "span_start",
            "span_end",
            "consumer_type",
            "consumer_id",
        )
        return [dict(zip(keys, row)) for row in rows]

    def record_gap(
        self,
        *,
        consumer_type: str,
        consumer_id: str,
        reason: str,
        source_agent: str = "",
        session_id: str = "",
    ) -> str:
        """Record a missing proof rather than inventing a provenance edge."""
        if not consumer_type or not consumer_id or not reason:
            raise ValueError("provenance gap identity and reason are required")
        gap_id = _provenance_gap_id(consumer_type, consumer_id, reason)
        conn = self._begin_write_transaction("raw_provenance_gap")
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO raw_provenance_gaps (
                    gap_id, consumer_type, consumer_id, source_agent,
                    session_id, reason, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending_rebuild', ?)
                """,
                (
                    gap_id,
                    consumer_type,
                    consumer_id,
                    source_agent,
                    session_id,
                    reason,
                    _utcnow(),
                ),
            )
            conn.commit()
            return gap_id
        except BaseException:
            conn.rollback()
            raise

    def resolve_gaps(self, *, consumer_type: str, consumer_id: str) -> int:
        conn = self._begin_write_transaction("raw_provenance_gap_resolution")
        try:
            cursor = conn.execute(
                """
                UPDATE raw_provenance_gaps
                SET status='resolved', resolved_at=?
                WHERE consumer_type=? AND consumer_id=? AND status!='resolved'
                """,
                (_utcnow(), consumer_type, consumer_id),
            )
            conn.commit()
            return int(cursor.rowcount or 0)
        except BaseException:
            conn.rollback()
            raise

    def gap_counts(self) -> dict[str, int]:
        rows = self._get_connection().execute(
            "SELECT status, COUNT(*) FROM raw_provenance_gaps GROUP BY status"
        ).fetchall()
        return {str(status): int(count) for status, count in rows}
