# -*- coding: utf-8 -*-
"""Durable recap consumption plans, commands, and receipt aggregation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "mnemos.recap_consumption.v2"
CONSUMER_VERSION = "v1"
PROCESSING_LEASE_SECONDS = 300

TARGET_ALIASES: dict[str, str] = {
    "wiki_search": "knowledge_retrieval",
    "context_aware_search": "knowledge_retrieval",
    "preflight": "policy_patch",
    "guard": "policy_patch",
    "policy_patch": "policy_patch",
    "follow_up": "follow_up",
    "persona": "persona",
    "scheduler": "scheduler",
    "scoring": "scoring",
}

SUCCESS_RECEIPT_STATES = {"committed", "intentional_skip"}
RETRYABLE_RECEIPT_STATES = {"pending", "processing", "retryable_failed"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_json(value).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


class RecapConsumptionLedger:
    """Own recap plan identity and derive aggregate state from target receipts."""

    def __init__(self, db_path: str | Path, *, initialize: bool = True):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if initialize:
            self._init_db()

    def create_plan(
        self,
        *,
        recap_id: str,
        requested_targets: Iterable[str],
        activation_rules: Mapping[str, Any],
        consume_priority: str,
        follow_up_at: str,
        page_path: str,
        record_payload: Mapping[str, Any],
    ) -> str:
        requested = list(dict.fromkeys(str(item).strip() for item in requested_targets if str(item).strip()))
        unknown = [item for item in requested if item not in TARGET_ALIASES]
        if unknown:
            raise ValueError(f"unregistered recap consumption targets: {sorted(unknown)}")
        if not requested:
            raise ValueError("recap consumption plan requires at least one target")

        canonical: dict[str, list[str]] = {}
        for target in requested:
            canonical.setdefault(TARGET_ALIASES[target], []).append(target)
        ordered_targets = sorted(canonical, key=lambda item: (item != "policy_patch", item))
        stable_record = dict(record_payload)
        stable_record.pop("reviewed_at", None)
        revision_payload = {
            "recap_id": recap_id,
            "requested_targets": requested,
            "canonical_targets": canonical,
            "activation_rules": dict(activation_rules),
            "follow_up_at": follow_up_at,
            "page_path": page_path,
            "record": stable_record,
        }
        revision_hash = hashlib.sha256(_json(revision_payload).encode("utf-8")).hexdigest()
        plan_id = _hash_id("recap-plan", {"recap_id": recap_id, "revision_hash": revision_hash})
        now = utcnow()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO recap_consumption_plans (
                    plan_id, recap_id, revision_hash, schema_version, targets,
                    requested_targets, activation_rules, consume_priority,
                    follow_up_at, page_path, record_json, status,
                    created_at, updated_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, '')
                """,
                (
                    plan_id,
                    recap_id,
                    revision_hash,
                    SCHEMA_VERSION,
                    _json(requested),
                    _json(requested),
                    _json(dict(activation_rules)),
                    consume_priority,
                    follow_up_at,
                    page_path,
                    _json(dict(record_payload)),
                    now,
                    now,
                ),
            )
            for canonical_target in ordered_targets:
                command_id = _hash_id(
                    "recap-command",
                    {"plan_id": plan_id, "canonical_target": canonical_target},
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO recap_consumption_commands (
                        command_id, plan_id, recap_id, canonical_target,
                        requested_targets, required, status, attempt_count,
                        processing_started_at, effect_ref, evidence_json,
                        last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, 'pending', 0, '', '', '{}', '', ?, ?)
                    """,
                    (
                        command_id,
                        plan_id,
                        recap_id,
                        canonical_target,
                        _json(canonical[canonical_target]),
                        now,
                        now,
                    ),
                )
        self._aggregate(plan_id)
        return plan_id

    def claim(self, plan_id: str, *, lease_seconds: int = PROCESSING_LEASE_SECONDS) -> list[dict[str, Any]]:
        now_dt = datetime.now(timezone.utc)
        cutoff = (now_dt - timedelta(seconds=max(1, lease_seconds))).isoformat()
        now = now_dt.isoformat()
        claimed: list[dict[str, Any]] = []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM recap_consumption_commands
                WHERE plan_id = ? AND (
                    status IN ('pending', 'retryable_failed')
                    OR (status = 'processing' AND processing_started_at < ?)
                )
                ORDER BY command_id
                """,
                (plan_id, cutoff),
            ).fetchall()
            for row in rows:
                changed = conn.execute(
                    """
                    UPDATE recap_consumption_commands
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
                        "SELECT * FROM recap_consumption_commands WHERE command_id=?",
                        (row["command_id"],),
                    ).fetchone()
                    claimed.append(dict(current))
        self._aggregate(plan_id)
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
        allowed = SUCCESS_RECEIPT_STATES | {"retryable_failed", "dead"}
        if status not in allowed:
            raise ValueError(f"invalid recap receipt status: {status}")
        now = utcnow()
        with self._connect() as conn:
            command = conn.execute(
                "SELECT * FROM recap_consumption_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
            if not command:
                raise ValueError(f"unknown recap consumption command: {command_id}")
            if command["status"] in SUCCESS_RECEIPT_STATES:
                return
            attempt = int(command["attempt_count"] or 0)
            receipt_id = _hash_id(
                "recap-receipt",
                {"command_id": command_id, "consumer_version": CONSUMER_VERSION, "attempt": attempt},
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO recap_consumption_receipts (
                    receipt_id, command_id, plan_id, recap_id, consumer,
                    consumer_version, attempt_no, status, effect_ref,
                    evidence_json, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    command_id,
                    command["plan_id"],
                    command["recap_id"],
                    command["canonical_target"],
                    CONSUMER_VERSION,
                    attempt,
                    status,
                    effect_ref,
                    _json(dict(evidence or {})),
                    str(error or "")[:1000],
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE recap_consumption_commands
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
            plan_id = str(command["plan_id"])
        self._aggregate(plan_id)

    def plan(self, plan_id: str) -> dict[str, Any]:
        with self._connect(read_only=True) as conn:
            plan = conn.execute(
                "SELECT * FROM recap_consumption_plans WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if not plan:
                raise ValueError(f"unknown recap consumption plan: {plan_id}")
            commands = conn.execute(
                """
                SELECT * FROM recap_consumption_commands
                WHERE plan_id=?
                ORDER BY CASE WHEN canonical_target='policy_patch' THEN 0 ELSE 1 END,
                         canonical_target
                """,
                (plan_id,),
            ).fetchall()
        target_statuses = []
        outcomes = []
        for row in commands:
            evidence = json.loads(row["evidence_json"] or "{}")
            requested = json.loads(row["requested_targets"] or "[]")
            item = {
                "command_id": row["command_id"],
                "canonical_target": row["canonical_target"],
                "requested_targets": requested,
                "required": bool(row["required"]),
                "status": row["status"],
                "attempt_count": int(row["attempt_count"] or 0),
                "effect_ref": row["effect_ref"] or "",
                "evidence": evidence,
                "error": row["last_error"] or "",
            }
            target_statuses.append(item)
            outcomes.append(
                {
                    "consumer": row["canonical_target"],
                    "outcome": evidence.get("outcome", row["status"]),
                    "evidence": row["effect_ref"] or evidence.get("reason", ""),
                }
            )
        required = [item for item in target_statuses if item["required"]]
        terminal = [item for item in required if item["status"] in SUCCESS_RECEIPT_STATES]
        failed = [
            item["canonical_target"]
            for item in required
            if item["status"] in {"retryable_failed", "dead"}
        ]
        return {
            "plan_id": plan["plan_id"],
            "recap_id": plan["recap_id"],
            "targets": json.loads(plan["targets"] or "[]"),
            "activation_rules": json.loads(plan["activation_rules"] or "{}"),
            "consume_priority": plan["consume_priority"],
            "follow_up_at": plan["follow_up_at"] or "",
            "outcomes": outcomes,
            "plan_status": plan["status"],
            "target_statuses": target_statuses,
            "required_receipt_count": len(required),
            "terminal_receipt_count": len(terminal),
            "consumed_at": plan["consumed_at"] or "",
            "retryable": any(item["status"] in RETRYABLE_RECEIPT_STATES for item in required),
            "failed_targets": failed,
            "effect_evidence": [item["effect_ref"] for item in terminal if item["effect_ref"]],
        }

    def pending_plans(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect(read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT plan_id, recap_id, page_path, record_json, status
                FROM recap_consumption_plans
                WHERE status IN ('pending', 'processing', 'partial', 'retryable_failed')
                ORDER BY updated_at LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                **dict(row),
                "record": json.loads(row["record_json"] or "{}"),
            }
            for row in rows
        ]

    def latest_plan_for_recap(self, recap_id: str) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        with self._connect(read_only=True) as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recap_consumption_plans'"
            ).fetchone():
                return None
            row = conn.execute(
                """
                SELECT plan_id FROM recap_consumption_plans
                WHERE recap_id=? ORDER BY created_at DESC LIMIT 1
                """,
                (recap_id,),
            ).fetchone()
        return self.plan(str(row["plan_id"])) if row else None

    def plan_source(self, plan_id: str) -> dict[str, Any]:
        """Return the immutable source payload used to create a plan."""
        with self._connect(read_only=True) as conn:
            row = conn.execute(
                """
                SELECT page_path, record_json FROM recap_consumption_plans
                WHERE plan_id=?
                """,
                (plan_id,),
            ).fetchone()
        if not row:
            raise ValueError(f"unknown recap consumption plan: {plan_id}")
        return {
            "page_path": str(row["page_path"] or ""),
            "record": json.loads(row["record_json"] or "{}"),
        }

    def _aggregate(self, plan_id: str) -> None:
        now = utcnow()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, required FROM recap_consumption_commands WHERE plan_id=?",
                (plan_id,),
            ).fetchall()
            required = [row["status"] for row in rows if row["required"]]
            if required and all(status in SUCCESS_RECEIPT_STATES for status in required):
                status = "consumed"
                consumed_at = now
            elif any(status == "dead" for status in required):
                status = "dead"
                consumed_at = ""
            elif any(status == "retryable_failed" for status in required):
                status = "partial" if any(s in SUCCESS_RECEIPT_STATES for s in required) else "retryable_failed"
                consumed_at = ""
            elif any(status == "processing" for status in required):
                status = "processing"
                consumed_at = ""
            else:
                status = "pending"
                consumed_at = ""
            conn.execute(
                """
                UPDATE recap_consumption_plans
                SET status=?, consumed_at=CASE WHEN ? <> '' THEN ? ELSE consumed_at END,
                    updated_at=? WHERE plan_id=?
                """,
                (status, consumed_at, consumed_at, now, plan_id),
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
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recap_consumption_plans'"
            ).fetchone()
            if existing:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(recap_consumption_plans)")
                }
                if "plan_id" not in columns:
                    legacy_name = "recap_consumption_plans_legacy_root010"
                    legacy_exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (legacy_name,),
                    ).fetchone()
                    if legacy_exists:
                        raise RuntimeError("legacy recap consumption table migration is incomplete")
                    conn.execute(f"ALTER TABLE recap_consumption_plans RENAME TO {legacy_name}")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS recap_consumption_plans (
                    plan_id TEXT PRIMARY KEY,
                    recap_id TEXT NOT NULL,
                    revision_hash TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    targets TEXT NOT NULL,
                    requested_targets TEXT NOT NULL,
                    activation_rules TEXT NOT NULL,
                    consume_priority TEXT NOT NULL,
                    follow_up_at TEXT DEFAULT '',
                    page_path TEXT DEFAULT '',
                    record_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    consumed_at TEXT DEFAULT '',
                    UNIQUE(recap_id, revision_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_recap_consumption_plan_status
                    ON recap_consumption_plans(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_recap_consumption_plan_recap
                    ON recap_consumption_plans(recap_id, created_at);

                CREATE TABLE IF NOT EXISTS recap_consumption_commands (
                    command_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    recap_id TEXT NOT NULL,
                    canonical_target TEXT NOT NULL,
                    requested_targets TEXT NOT NULL,
                    required INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    processing_started_at TEXT DEFAULT '',
                    effect_ref TEXT DEFAULT '',
                    evidence_json TEXT DEFAULT '{}',
                    last_error TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(plan_id, canonical_target),
                    FOREIGN KEY(plan_id) REFERENCES recap_consumption_plans(plan_id)
                );
                CREATE INDEX IF NOT EXISTS idx_recap_consumption_command_status
                    ON recap_consumption_commands(status, updated_at);

                CREATE TABLE IF NOT EXISTS recap_consumption_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    command_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    recap_id TEXT NOT NULL,
                    consumer TEXT NOT NULL,
                    consumer_version TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    effect_ref TEXT DEFAULT '',
                    evidence_json TEXT DEFAULT '{}',
                    error TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(command_id, consumer_version, attempt_no),
                    FOREIGN KEY(command_id) REFERENCES recap_consumption_commands(command_id)
                );
                CREATE INDEX IF NOT EXISTS idx_recap_consumption_receipt_plan
                    ON recap_consumption_receipts(plan_id, consumer, status);
                """
            )
