# -*- coding: utf-8 -*-
"""Durable correction outbox for feedback on consumed retrospectives."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "mnemos.recap_feedback.v2"
NEGATIVE_FEEDBACK = {"inaccurate", "irrelevant", "outdated"}
POSITIVE_FEEDBACK = {"accurate", "useful"}
SUCCESS_STATES = {"committed", "intentional_skip"}
NONTERMINAL_REVIEW_STATE = "pending_review"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_json(value).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


class RecapFeedbackOutbox:
    """Persist feedback commands and derive completion from correction receipts."""

    def __init__(self, db_path: str | Path, *, initialize: bool = True):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if initialize:
            self._init_db()

    def create(
        self,
        *,
        recap_id: str,
        feedback_type: str,
        comment: str,
        source_agent: str,
        supersedes_ref: str = "",
    ) -> str:
        if feedback_type not in NEGATIVE_FEEDBACK | POSITIVE_FEEDBACK:
            raise ValueError(f"unsupported recap feedback type: {feedback_type}")
        identity = {
            "recap_id": recap_id,
            "feedback_type": feedback_type,
            "comment": comment.strip(),
            "source_agent": source_agent,
        }
        if supersedes_ref:
            identity["supersedes_ref"] = supersedes_ref
        event_id = _stable_id("recap-feedback", identity)
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute(
                """
                SELECT event_id FROM recap_feedback_events
                WHERE recap_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (recap_id,),
            ).fetchone()
            existing = conn.execute(
                """
                SELECT event_id FROM recap_feedback_events
                WHERE recap_id=? AND feedback_type=? AND comment=?
                  AND source_agent=? AND supersedes_ref=?
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (
                    recap_id,
                    feedback_type,
                    comment,
                    source_agent,
                    supersedes_ref,
                ),
            ).fetchone()
            if existing and latest and existing["event_id"] == latest["event_id"]:
                return str(existing["event_id"])
            if latest and not supersedes_ref:
                raise ValueError("conflicting recap feedback requires supersedes_event_id")
            if supersedes_ref and (not latest or latest["event_id"] != supersedes_ref):
                raise ValueError("supersedes_event_id is not the latest recap feedback event")
            conn.execute(
                """
                INSERT INTO recap_feedback_events (
                    event_id, recap_id, schema_version, feedback_type, comment,
                    source_agent, supersedes_ref, status, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, '')
                """,
                (
                    event_id,
                    recap_id,
                    SCHEMA_VERSION,
                    feedback_type,
                    comment,
                    source_agent,
                    supersedes_ref,
                    now,
                ),
            )
            if feedback_type in NEGATIVE_FEEDBACK:
                effects = conn.execute(
                    """
                    SELECT command_id, canonical_target, effect_ref
                    FROM recap_consumption_commands
                    WHERE recap_id=? AND status='committed'
                    ORDER BY canonical_target
                    """,
                    (recap_id,),
                ).fetchall()
                if not effects:
                    raise ValueError("recap has no committed effects to correct")
                for effect in effects:
                    command_id = _stable_id(
                        "recap-correction",
                        {"event_id": event_id, "source_command_id": effect["command_id"]},
                    )
                    conn.execute(
                        """
                        INSERT INTO recap_correction_commands (
                            command_id, event_id, recap_id, source_command_id,
                            canonical_target, source_effect_ref, status,
                            attempt_count, processing_started_at, effect_ref,
                            evidence_json, last_error, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, '', '', '{}', '', ?, ?)
                        """,
                        (
                            command_id,
                            event_id,
                            recap_id,
                            effect["command_id"],
                            effect["canonical_target"],
                            effect["effect_ref"] or "",
                            now,
                            now,
                        ),
                    )
            else:
                command_id = _stable_id(
                    "recap-correction",
                    {"event_id": event_id, "canonical_target": "feedback_outcome"},
                )
                conn.execute(
                    """
                    INSERT INTO recap_correction_commands (
                        command_id, event_id, recap_id, source_command_id,
                        canonical_target, source_effect_ref, status,
                        attempt_count, processing_started_at, effect_ref,
                        evidence_json, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, '', 'feedback_outcome', '', 'pending', 0, '', '', '{}', '', ?, ?)
                    """,
                    (command_id, event_id, recap_id, now, now),
                )
        self._aggregate(event_id)
        return event_id

    def claim(self, event_id: str, *, lease_seconds: int = 300) -> list[dict[str, Any]]:
        now_dt = datetime.now(timezone.utc)
        cutoff = (now_dt - timedelta(seconds=max(1, lease_seconds))).isoformat()
        now = now_dt.isoformat()
        claimed: list[dict[str, Any]] = []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM recap_correction_commands
                WHERE event_id=? AND (
                    status IN ('pending', 'retryable_failed')
                    OR (status='processing' AND processing_started_at < ?)
                ) ORDER BY canonical_target
                """,
                (event_id, cutoff),
            ).fetchall()
            for row in rows:
                changed = conn.execute(
                    """
                    UPDATE recap_correction_commands
                    SET status='processing', attempt_count=attempt_count+1,
                        processing_started_at=?, updated_at=?
                    WHERE command_id=? AND (
                        status IN ('pending', 'retryable_failed')
                        OR (status='processing' AND processing_started_at < ?)
                    )
                    """,
                    (now, now, row["command_id"], cutoff),
                ).rowcount
                if changed:
                    current = conn.execute(
                        "SELECT * FROM recap_correction_commands WHERE command_id=?",
                        (row["command_id"],),
                    ).fetchone()
                    claimed.append(dict(current))
        self._aggregate(event_id)
        return claimed

    def finish(
        self,
        command_id: str,
        *,
        status: str,
        effect_ref: str = "",
        evidence: Mapping[str, Any] | None = None,
        error: str = "",
    ) -> None:
        allowed = SUCCESS_STATES | {
            NONTERMINAL_REVIEW_STATE,
            "retryable_failed",
            "dead",
        }
        if status not in allowed:
            raise ValueError(f"invalid recap correction status: {status}")
        now = _now()
        with self._connect() as conn:
            command = conn.execute(
                "SELECT * FROM recap_correction_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if not command:
                raise ValueError(f"unknown recap correction command: {command_id}")
            if command["status"] in SUCCESS_STATES:
                return
            receipt_id = _stable_id(
                "recap-correction-receipt",
                {"command_id": command_id, "attempt": int(command["attempt_count"] or 0)},
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO recap_correction_receipts (
                    receipt_id, command_id, event_id, recap_id,
                    canonical_target, attempt_no, status, effect_ref,
                    evidence_json, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    command_id,
                    command["event_id"],
                    command["recap_id"],
                    command["canonical_target"],
                    int(command["attempt_count"] or 0),
                    status,
                    effect_ref,
                    _json(dict(evidence or {})),
                    str(error or "")[:1000],
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE recap_correction_commands
                SET status=?, processing_started_at='', effect_ref=?,
                    evidence_json=?, last_error=?, updated_at=?
                WHERE command_id=?
                """,
                (
                    status,
                    effect_ref,
                    _json(dict(evidence or {})),
                    str(error or "")[:1000],
                    now,
                    command_id,
                ),
            )
            event_id = str(command["event_id"])
        self._aggregate(event_id)

    def bind_canonical_feedback(
        self,
        event_id: str,
        canonical_feedback: Mapping[str, Any],
    ) -> None:
        """Bind one immutable canonical reaction/attribution result to the event."""

        payload = dict(canonical_feedback)
        reaction_event_id = str(payload.get("feedback_event_id") or "")
        attribution_revision_id = str(
            payload.get("attribution_revision_id") or ""
        )
        if not reaction_event_id or not attribution_revision_id:
            raise ValueError("canonical recap feedback binding is incomplete")
        payload_json = _json(payload)
        payload_hash = _stable_id("canonical-recap-feedback", payload)
        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM recap_feedback_events WHERE event_id=?",
                (event_id,),
            ).fetchone() is None:
                raise ValueError("unknown recap feedback event")
            conn.execute(
                """
                INSERT OR IGNORE INTO recap_feedback_canonical_bindings (
                    event_id, reaction_event_id, attribution_revision_id,
                    payload_hash, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    reaction_event_id,
                    attribution_revision_id,
                    payload_hash,
                    payload_json,
                    _now(),
                ),
            )
            stored = conn.execute(
                """
                SELECT reaction_event_id, attribution_revision_id,
                       payload_hash, payload_json
                FROM recap_feedback_canonical_bindings WHERE event_id=?
                """,
                (event_id,),
            ).fetchone()
        expected = (
            reaction_event_id,
            attribution_revision_id,
            payload_hash,
            payload_json,
        )
        if stored is None or tuple(str(value) for value in stored) != expected:
            raise RuntimeError("immutable canonical recap feedback binding conflict")

    def canonical_feedback(self, event_id: str) -> dict[str, Any]:
        """Return the immutable canonical reaction binding for recovery."""

        with self._connect(read_only=True) as conn:
            row = conn.execute(
                """
                SELECT payload_hash, payload_json
                FROM recap_feedback_canonical_bindings WHERE event_id=?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            raise ValueError("canonical recap feedback binding is missing")
        payload = json.loads(str(row["payload_json"]))
        if _stable_id("canonical-recap-feedback", payload) != str(row["payload_hash"]):
            raise ValueError("canonical recap feedback binding hash mismatch")
        return dict(payload)

    def mark_effect_state(
        self,
        *,
        recap_id: str,
        canonical_target: str,
        status: str,
        source_event_id: str,
        effect_ref: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO recap_effect_states (
                    recap_id, canonical_target, status, source_event_id,
                    effect_ref, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(recap_id, canonical_target) DO UPDATE SET
                    status=excluded.status,
                    source_event_id=excluded.source_event_id,
                    effect_ref=excluded.effect_ref,
                    updated_at=excluded.updated_at
                """,
                (recap_id, canonical_target, status, source_event_id, effect_ref, _now()),
            )

    def view(self, event_id: str) -> dict[str, Any]:
        with self._connect(read_only=True) as conn:
            event = conn.execute(
                "SELECT * FROM recap_feedback_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if not event:
                raise ValueError(f"unknown recap feedback event: {event_id}")
            commands = conn.execute(
                "SELECT * FROM recap_correction_commands WHERE event_id=? ORDER BY canonical_target",
                (event_id,),
            ).fetchall()
        receipts = [
            {
                "command_id": row["command_id"],
                "canonical_target": row["canonical_target"],
                "source_command_id": row["source_command_id"] or "",
                "status": row["status"],
                "attempt_count": int(row["attempt_count"] or 0),
                "effect_ref": row["effect_ref"] or "",
                "error": row["last_error"] or "",
                "evidence": json.loads(row["evidence_json"] or "{}"),
            }
            for row in commands
        ]
        return {
            "feedback_event_id": event["event_id"],
            "recap_id": event["recap_id"],
            "feedback_type": event["feedback_type"],
            "correction_status": event["status"],
            "terminal": event["status"] == "complete",
            "supersedes_ref": event["supersedes_ref"] or "",
            "correction_receipts": receipts,
            "failed_targets": [
                row["canonical_target"]
                for row in commands
                if row["status"] in {"retryable_failed", "dead"}
            ],
            "pending_review_targets": [
                row["canonical_target"]
                for row in commands
                if row["status"] == NONTERMINAL_REVIEW_STATE
            ],
            "effect_evidence": [row["effect_ref"] for row in commands if row["effect_ref"]],
        }

    def pending_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect(read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT event_id, recap_id, feedback_type, comment,
                       source_agent, supersedes_ref, status
                FROM recap_feedback_events
                WHERE status IN (
                    'pending', 'processing', 'partial', 'retryable_failed',
                    'pending_review'
                )
                ORDER BY created_at LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_for_recap(self, recap_id: str) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        with self._connect(read_only=True) as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recap_feedback_events'"
            ).fetchone():
                return None
            row = conn.execute(
                """
                SELECT event_id FROM recap_feedback_events
                WHERE recap_id=? ORDER BY created_at DESC LIMIT 1
                """,
                (recap_id,),
            ).fetchone()
        return self.view(str(row["event_id"])) if row else None

    def superseded_retrieval_paths(self) -> set[str]:
        if not self.db_path.exists():
            return set()
        with self._connect(read_only=True) as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recap_effect_states'"
            ).fetchone():
                return set()
            rows = conn.execute(
                """
                SELECT DISTINCT p.page_path
                FROM recap_effect_states AS e
                JOIN recap_consumption_plans AS p ON p.recap_id=e.recap_id
                WHERE e.canonical_target='knowledge_retrieval'
                  AND e.status IN ('superseded', 'blocked')
                  AND p.page_path <> ''
                """
            ).fetchall()
        return {str(row[0]).removesuffix(".md") for row in rows}

    def _aggregate(self, event_id: str) -> None:
        now = _now()
        with self._connect() as conn:
            states = [
                row[0]
                for row in conn.execute(
                    "SELECT status FROM recap_correction_commands WHERE event_id=?",
                    (event_id,),
                ).fetchall()
            ]
            if states and all(state in SUCCESS_STATES for state in states):
                status = "complete"
                completed_at = now
            elif any(state == "dead" for state in states):
                status = "dead"
                completed_at = ""
            elif any(state == "retryable_failed" for state in states):
                status = "partial" if any(state in SUCCESS_STATES for state in states) else "retryable_failed"
                completed_at = ""
            elif any(state == NONTERMINAL_REVIEW_STATE for state in states):
                status = NONTERMINAL_REVIEW_STATE
                completed_at = ""
            elif any(state == "processing" for state in states):
                status = "processing"
                completed_at = ""
            else:
                status = "pending"
                completed_at = ""
            conn.execute(
                """
                UPDATE recap_feedback_events
                SET status=?, completed_at=CASE WHEN ? <> '' THEN ? ELSE completed_at END
                WHERE event_id=?
                """,
                (status, completed_at, completed_at, event_id),
            )

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        else:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recap_feedback_events'"
            ).fetchone()
            if existing:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(recap_feedback_events)")
                }
                if "schema_version" not in columns:
                    legacy_name = "recap_feedback_events_legacy_root010"
                    if conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (legacy_name,),
                    ).fetchone():
                        raise RuntimeError("legacy recap feedback migration is incomplete")
                    conn.execute(f"ALTER TABLE recap_feedback_events RENAME TO {legacy_name}")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS recap_feedback_events (
                    event_id TEXT PRIMARY KEY,
                    recap_id TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    feedback_type TEXT NOT NULL,
                    comment TEXT DEFAULT '',
                    source_agent TEXT DEFAULT '',
                    supersedes_ref TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_recap_feedback_recap
                    ON recap_feedback_events(recap_id, created_at);

                CREATE TABLE IF NOT EXISTS recap_correction_commands (
                    command_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    recap_id TEXT NOT NULL,
                    source_command_id TEXT DEFAULT '',
                    canonical_target TEXT NOT NULL,
                    source_effect_ref TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    processing_started_at TEXT DEFAULT '',
                    effect_ref TEXT DEFAULT '',
                    evidence_json TEXT DEFAULT '{}',
                    last_error TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(event_id, source_command_id, canonical_target),
                    FOREIGN KEY(event_id) REFERENCES recap_feedback_events(event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_recap_correction_status
                    ON recap_correction_commands(status, updated_at);

                CREATE TABLE IF NOT EXISTS recap_correction_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    recap_id TEXT NOT NULL,
                    canonical_target TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    effect_ref TEXT DEFAULT '',
                    evidence_json TEXT DEFAULT '{}',
                    error TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(command_id, attempt_no),
                    FOREIGN KEY(command_id) REFERENCES recap_correction_commands(command_id)
                );

                CREATE TABLE IF NOT EXISTS recap_effect_states (
                    recap_id TEXT NOT NULL,
                    canonical_target TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    effect_ref TEXT DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(recap_id, canonical_target)
                );

                CREATE TABLE IF NOT EXISTS recap_feedback_canonical_bindings (
                    event_id TEXT PRIMARY KEY,
                    reaction_event_id TEXT NOT NULL,
                    attribution_revision_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(event_id) REFERENCES recap_feedback_events(event_id)
                );
                CREATE TRIGGER IF NOT EXISTS recap_feedback_canonical_bindings_no_update
                BEFORE UPDATE ON recap_feedback_canonical_bindings
                BEGIN SELECT RAISE(ABORT, 'recap feedback canonical binding is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS recap_feedback_canonical_bindings_no_delete
                BEFORE DELETE ON recap_feedback_canonical_bindings
                BEGIN SELECT RAISE(ABORT, 'recap feedback canonical binding is immutable'); END;
                """
            )
