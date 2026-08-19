"""Read-only exact lookup helpers for runtime and cognitive producer events."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping


def find_produced_event(
    db_path: Path,
    flow_id: str,
    *,
    item_id: str,
    metadata_match: Mapping[str, Any],
) -> dict[str, str] | None:
    """Return the immutable producer event whose metadata matches every key."""
    expected = {
        str(key): value
        for key, value in metadata_match.items()
        if value not in (None, "")
    }
    if not expected:
        return None
    with _connect_ro(db_path) as conn:
        rows = conn.execute(
            """
            SELECT event_id, generation_id, metadata
            FROM runtime_flow_events
            WHERE flow_id = ? AND direction = 'produced' AND item_id = ?
            ORDER BY created_at DESC, event_id DESC
            """,
            (flow_id, item_id),
        ).fetchall()
    for event_id, generation_id, raw_metadata in rows:
        try:
            metadata = json.loads(str(raw_metadata or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(metadata, dict) and all(
            metadata.get(key) == value for key, value in expected.items()
        ):
            return {"event_id": str(event_id), "generation_id": str(generation_id)}
    return None


def find_runtime_terminal_receipts(
    db_path: Path,
    flow_id: str,
    *,
    production_event_id: str,
) -> list[dict[str, Any]]:
    """Return immutable terminal receipts for one exact producer event."""

    if not production_event_id:
        return []
    with _connect_ro(db_path) as conn:
        rows = conn.execute(
            """
            SELECT receipt_id, consumer_id, status, item_id, generation_id,
                   created_at, metadata
            FROM runtime_flow_receipts
            WHERE flow_id=? AND production_event_id=?
            ORDER BY created_at, receipt_id
            """,
            (flow_id, production_event_id),
        ).fetchall()
    receipts: list[dict[str, Any]] = []
    for row in rows:
        try:
            metadata = json.loads(str(row[6] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        receipts.append(
            {
                "receipt_id": str(row[0]),
                "consumer_id": str(row[1]),
                "status": str(row[2]),
                "item_id": str(row[3]),
                "generation_id": str(row[4]),
                "created_at": str(row[5]),
                "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
            }
        )
    return receipts


def cognitive_event_exists(db_path: Path, event_id: str) -> bool:
    """Return whether an immutable cognitive producer event exists."""
    with _connect_ro(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM cognitive_data_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    return row is not None


def cognitive_event_allows_consumer(
    db_path: Path,
    event_id: str,
    consumer_id: str,
) -> bool:
    """Return whether the producer explicitly intended this consumer."""
    with _connect_ro(db_path) as conn:
        row = conn.execute(
            "SELECT intended_consumers FROM cognitive_data_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
    if row is None:
        return False
    try:
        consumers = json.loads(str(row[0] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(consumers, list) and consumer_id in {str(value) for value in consumers}


def cognitive_event_current_consumption(
    db_path: Path,
    event_id: str,
    consumer_id: str,
) -> dict[str, Any] | None:
    """Return the immutable current terminal receipt for one consumer pair."""

    with _connect_ro(db_path) as conn:
        row = conn.execute(
            """
            SELECT c.consumption_id, c.status, c.outcome, c.receipt_state,
                   c.metadata, COALESCE(c.supersedes_consumption_id, ''),
                   COALESCE(c.correction_of_consumption_id, '')
            FROM cognitive_data_consumer_heads AS h
            JOIN cognitive_data_consumptions AS c
              ON c.consumption_id=h.consumption_id
            WHERE h.event_id=? AND h.consumer_id=?
            """,
            (event_id, consumer_id),
        ).fetchone()
    if row is None:
        return None
    try:
        metadata = json.loads(str(row[4] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    return {
        "consumption_id": str(row[0]),
        "status": str(row[1]),
        "outcome": str(row[2]),
        "receipt_state": str(row[3]),
        "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
        "supersedes_consumption_id": str(row[5]),
        "correction_of_consumption_id": str(row[6]),
    }


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
