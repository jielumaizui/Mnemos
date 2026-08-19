"""Append-only trusted push write journal."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from core.trust.config import TrustedPushConfig, load_trusted_push_config
from core.trust.models import JournalEventInput, new_id, sha256_json, utc_now_iso


class WriteJournal:
    """Append-only journal with an event-level hash chain."""

    def __init__(self, db_path: Path | None = None, *, config: TrustedPushConfig | None = None):
        self._config = config or load_trusted_push_config()
        self.db_path = Path(db_path or self._config.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS journal_events (
                    event_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    target_uri TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def append_event(self, event: JournalEventInput) -> str:
        """Append one event with BEGIN IMMEDIATE; never updates previous events."""

        event_id = new_id("journal")
        created_at = utc_now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                prev = conn.execute(
                    "SELECT event_hash FROM journal_events ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                previous_hash = prev["event_hash"] if prev else "genesis"
                event_hash = sha256_json(
                    {
                        "event_id": event_id,
                        "created_at": created_at,
                        **event.canonical_payload(previous_hash),
                    }
                )
                conn.execute(
                    """
                    INSERT INTO journal_events (
                        event_id, proposal_id, event_type, target_uri, content_hash,
                        previous_hash, event_hash, actor, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event.proposal_id,
                        event.event_type,
                        event.target_uri,
                        event.content_hash,
                        previous_hash,
                        event_hash,
                        event.actor,
                        json.dumps(event.metadata, ensure_ascii=False),
                        created_at,
                    ),
                )
                conn.execute("COMMIT")
                return event_id
            except (sqlite3.Error, TypeError, ValueError):
                conn.execute("ROLLBACK")
                raise

    def events_for_proposal(self, proposal_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM journal_events
                WHERE proposal_id = ?
                ORDER BY rowid ASC
                """,
                (proposal_id,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def open_prepares(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT je.*
                FROM journal_events je
                JOIN (
                    SELECT proposal_id, MAX(rowid) AS max_rowid
                    FROM journal_events
                    GROUP BY proposal_id
                ) latest
                  ON latest.proposal_id = je.proposal_id
                 AND latest.max_rowid = je.rowid
                WHERE je.event_type = 'prepare'
                ORDER BY je.rowid ASC
                """
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def verify_hash_chain(self) -> bool:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM journal_events ORDER BY rowid ASC"
            ).fetchall()
        previous_hash = "genesis"
        for row in rows:
            payload = {
                "event_id": row["event_id"],
                "created_at": row["created_at"],
                "proposal_id": row["proposal_id"],
                "event_type": row["event_type"],
                "target_uri": row["target_uri"],
                "content_hash": row["content_hash"],
                "metadata": json.loads(row["metadata_json"]),
                "actor": row["actor"],
                "previous_hash": previous_hash,
            }
            if row["previous_hash"] != previous_hash:
                return False
            if row["event_hash"] != sha256_json(payload):
                return False
            previous_hash = row["event_hash"]
        return True


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data = dict(row)
    data["metadata"] = json.loads(data.pop("metadata_json"))
    return data
