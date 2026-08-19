"""Idempotent persistence seam for Layer 5 reflection signals."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any


def ensure_source_event_schema(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(reflection_signals)").fetchall()}
    if "source_event_id" not in columns:
        conn.execute("ALTER TABLE reflection_signals ADD COLUMN source_event_id TEXT DEFAULT ''")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_reflection_signal_source_event
        ON reflection_signals(source_event_id) WHERE source_event_id <> ''
        """)


def _insert_reflection_signal(
    conn: sqlite3.Connection,
    *,
    dimension: str,
    value: str,
    confidence: float,
    source: str,
    source_event_id: str,
    observed_at: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO reflection_signals (
            dimension, value, confidence, source, source_event_id, timestamp
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            dimension,
            value,
            confidence,
            source,
            source_event_id,
            observed_at,
        ),
    )
    signal_id = int(cursor.lastrowid or 0)
    if cursor.rowcount == 0 and source_event_id:
        row = conn.execute(
            "SELECT id FROM reflection_signals WHERE source_event_id=?",
            (source_event_id,),
        ).fetchone()
        return int(row[0]) if row else 0
    return signal_id


def persist_reflection_signal(
    store: Any,
    *,
    dimension: str,
    value: str,
    confidence: float,
    source: str,
    source_event_id: str,
) -> int:
    conn = store._pool.get_conn()
    now = datetime.now().isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ensure_source_event_schema(conn)
        signal_id = _insert_reflection_signal(
            conn,
            dimension=dimension,
            value=value,
            confidence=confidence,
            source=source,
            source_event_id=source_event_id,
            observed_at=now,
        )
        if not signal_id:
            raise RuntimeError("reflection signal did not produce a durable identity")
        # Reflection output is an assistant-derived signal.  It may inform a
        # later authorized persona analysis, but it cannot by itself create a
        # user profile assertion or usage effect.
        conn.commit()
    except (sqlite3.Error, RuntimeError, ValueError, TypeError):
        conn.rollback()
        raise
    return signal_id
