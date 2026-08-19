"""Canonical append-only state owner for distill and cognitive actions.

This module is the only owner of the ``distill_actions.db`` schema.  The
router records immutable parent outcomes and cognitive intents; the worker
leases commands, appends attempt events, and may mark a command ``applied``
only while committing a target-owned reciprocal effect receipt.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.db_utils import render_sql


SCHEMA_VERSION = "mnemos.distill_action_store.v2"
ARTIFACT_SCHEMA_VERSION = "mnemos.distill_cognitive_action.v2"


class DistillActionSchemaError(RuntimeError):
    """Raised when a historical or drifted action database opens at runtime."""


class DistillActionStateError(RuntimeError):
    """Raised when an immutable identity or monotonic transition is violated."""


@dataclass(frozen=True)
class CognitiveEffectCommit:
    """Evidence returned by a real target service after its durable commit."""

    effect_id: str
    target: str
    target_object_id: str
    before_hash: str
    after_hash: str
    expected_delta_hash: str
    reciprocal_receipt: str
    receipt_db_path: str
    committed_at: str
    detail: Mapping[str, Any]

    def validate(self) -> None:
        required = {
            "effect_id": self.effect_id,
            "target": self.target,
            "target_object_id": self.target_object_id,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "expected_delta_hash": self.expected_delta_hash,
            "reciprocal_receipt": self.reciprocal_receipt,
            "receipt_db_path": self.receipt_db_path,
            "committed_at": self.committed_at,
        }
        missing = sorted(key for key, value in required.items() if not str(value or ""))
        if missing:
            raise DistillActionStateError(
                "target effect receipt is incomplete: " + ", ".join(missing)
            )
        if self.before_hash == self.after_hash:
            raise DistillActionStateError("target effect did not change the target state")


SCHEMA_SQL = """
CREATE TABLE distill_action_schema_registry (
    schema_name TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    registered_at TEXT NOT NULL
);

CREATE TABLE distill_action_log (
    action_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_agent TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    distill_intent TEXT NOT NULL DEFAULT '',
    claim_id TEXT NOT NULL DEFAULT '',
    target_page TEXT NOT NULL DEFAULT '',
    target_kind TEXT NOT NULL DEFAULT '',
    source_event_ids TEXT NOT NULL DEFAULT '[]',
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    backup_path TEXT NOT NULL DEFAULT '',
    result_status TEXT NOT NULL,
    result_detail TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    merge_decision_card TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE distill_action_events (
    event_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE knowledge_action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_action_id TEXT NOT NULL UNIQUE,
    action_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    change_type TEXT NOT NULL,
    target_page TEXT NOT NULL DEFAULT '',
    backup_path TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE cognitive_action_intents (
    cognitive_action_id TEXT PRIMARY KEY,
    distill_action_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    session_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    cognitive_action TEXT NOT NULL,
    parent_status TEXT NOT NULL,
    disposition TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    fragment_ids TEXT NOT NULL,
    source_event_ids TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE cognitive_action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cognitive_action_id TEXT NOT NULL UNIQUE,
    distill_action_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_agent TEXT NOT NULL DEFAULT '',
    claim_id TEXT NOT NULL DEFAULT '',
    cognitive_action TEXT NOT NULL,
    target_kind TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    source_event_ids TEXT NOT NULL DEFAULT '[]',
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    artifact_path TEXT NOT NULL DEFAULT '',
    artifact_schema_version TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    artifact_payload TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    fragment_ids TEXT NOT NULL,
    acl_payload TEXT NOT NULL,
    input_spec_hash TEXT NOT NULL,
    extraction_output_hash TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT '',
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE cognitive_action_events (
    event_id TEXT PRIMARY KEY,
    cognitive_action_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE cognitive_action_attempt_events (
    event_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    cognitive_action_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE cognitive_action_effects (
    effect_id TEXT PRIMARY KEY,
    cognitive_action_id TEXT NOT NULL UNIQUE,
    target TEXT NOT NULL,
    target_object_id TEXT NOT NULL,
    before_hash TEXT NOT NULL,
    after_hash TEXT NOT NULL,
    expected_delta_hash TEXT NOT NULL,
    reciprocal_receipt TEXT NOT NULL,
    receipt_db_path TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE cognitive_action_consumptions (
    consumption_id TEXT PRIMARY KEY,
    cognitive_action_id TEXT NOT NULL UNIQUE,
    consumed_at TEXT NOT NULL,
    consumer TEXT NOT NULL,
    status TEXT NOT NULL,
    effect_id TEXT NOT NULL UNIQUE,
    detail TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_distill_action_log_session
ON distill_action_log(session_id, created_at);
CREATE INDEX idx_knowledge_action_log_action
ON knowledge_action_log(action_id, created_at);
CREATE INDEX idx_cognitive_action_intents_parent
ON cognitive_action_intents(distill_action_id, created_at);
CREATE INDEX idx_cognitive_action_log_distill
ON cognitive_action_log(distill_action_id, created_at);
CREATE INDEX idx_cognitive_action_log_state
ON cognitive_action_log(status, next_attempt_at, lease_expires_at, created_at);
CREATE INDEX idx_cognitive_action_events_action
ON cognitive_action_events(cognitive_action_id, created_at);
CREATE INDEX idx_cognitive_action_attempts_action
ON cognitive_action_attempt_events(cognitive_action_id, created_at);
"""

OWNED_TABLES = frozenset(
    {
        "distill_action_schema_registry",
        "distill_action_log",
        "distill_action_events",
        "knowledge_action_log",
        "cognitive_action_intents",
        "cognitive_action_log",
        "cognitive_action_events",
        "cognitive_action_attempt_events",
        "cognitive_action_effects",
        "cognitive_action_consumptions",
    }
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: Any, size: int = 24) -> str:
    raw = canonical_json(list(parts)).encode("utf-8")
    return f"{prefix}_" + hashlib.sha256(raw).hexdigest()[:size]


def schema_hash() -> str:
    descriptor = "\n".join(line.rstrip() for line in SCHEMA_SQL.strip().splitlines())
    return hashlib.sha256(descriptor.encode("utf-8")).hexdigest()


def _schema_structure_descriptor(conn: sqlite3.Connection) -> str:
    """Describe the owned physical schema, including implicit constraints."""
    objects: list[dict[str, Any]] = []
    for table in sorted(OWNED_TABLES):
        safe_table = table.replace('"', '""')
        table_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if table_row is None:
            objects.append({"table": table, "missing": True})
            continue
        indexes: list[dict[str, Any]] = []
        for index_row in conn.execute(f'PRAGMA index_list("{safe_table}")').fetchall():
            index_name = str(index_row[1])
            safe_index = index_name.replace('"', '""')
            index_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ).fetchone()
            indexes.append(
                {
                    "name": index_name,
                    "unique": int(index_row[2]),
                    "origin": str(index_row[3]),
                    "partial": int(index_row[4]),
                    "columns": [
                        list(row)
                        for row in conn.execute(
                            f'PRAGMA index_xinfo("{safe_index}")'
                        ).fetchall()
                    ],
                    "sql": " ".join(str(index_sql[0]).split())
                    if index_sql and index_sql[0]
                    else None,
                }
            )
        objects.append(
            {
                "table": table,
                "sql": " ".join(str(table_row[0]).split()),
                "columns": [
                    list(row)
                    for row in conn.execute(
                        f'PRAGMA table_info("{safe_table}")'
                    ).fetchall()
                ],
                "foreign_keys": [
                    list(row)
                    for row in conn.execute(
                        f'PRAGMA foreign_key_list("{safe_table}")'
                    ).fetchall()
                ],
                "indexes": sorted(indexes, key=lambda item: item["name"]),
            }
        )
    return canonical_json(objects)


@lru_cache(maxsize=1)
def canonical_schema_structure_hash() -> str:
    with sqlite3.connect(":memory:") as conn:
        conn.executescript(SCHEMA_SQL)
        descriptor = _schema_structure_descriptor(conn)
    return hashlib.sha256(descriptor.encode("utf-8")).hexdigest()


def validate_schema_structure(conn: sqlite3.Connection) -> None:
    actual = hashlib.sha256(
        _schema_structure_descriptor(conn).encode("utf-8")
    ).hexdigest()
    if actual != canonical_schema_structure_hash():
        raise DistillActionSchemaError(
            "distill action physical schema drift; run explicit reconciliation"
        )


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Install the canonical action tables into a database without owned tables."""
    conn.executescript(SCHEMA_SQL)
    validate_schema_structure(conn)
    conn.execute(
        """
        INSERT INTO distill_action_schema_registry (
            schema_name, schema_version, schema_hash, registered_at
        ) VALUES ('distill_actions', ?, ?, ?)
        """,
        (SCHEMA_VERSION, schema_hash(), now_utc()),
    )
    conn.commit()


class DistillActionStore:
    """Own and enforce all state transitions in ``distill_actions.db``."""

    def __init__(self, db_path: Path, *, ensure_db: bool = True):
        self.db_path = Path(db_path)
        if ensure_db:
            self.ensure_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if str(row[0]) != "sqlite_sequence"
            }
            if "distill_action_schema_registry" not in tables:
                legacy_owned = sorted(tables.intersection(OWNED_TABLES))
                if legacy_owned:
                    raise DistillActionSchemaError(
                        "legacy distill action schema tables found: "
                        + ", ".join(legacy_owned)
                        + "; run scripts/reconcile_cognitive_action_effects.py"
                    )
                initialize_schema(conn)
                return
            self._validate_schema(conn)

    @staticmethod
    def _validate_schema(conn: sqlite3.Connection) -> None:
        try:
            row = conn.execute(
                """
                SELECT schema_version, schema_hash
                FROM distill_action_schema_registry
                WHERE schema_name='distill_actions'
                """
            ).fetchone()
        except sqlite3.Error as exc:
            raise DistillActionSchemaError(
                "legacy distill action schema; run scripts/reconcile_cognitive_action_effects.py"
            ) from exc
        if not row or row[0] != SCHEMA_VERSION or row[1] != schema_hash():
            raise DistillActionSchemaError(
                "distill action schema registry mismatch; run explicit reconciliation"
            )
        validate_schema_structure(conn)

    def get_action(self, action_id: str) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM distill_action_log WHERE action_id=?",
                (action_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_actions_for_session(self, session_id: str) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM distill_action_log
                WHERE session_id=? ORDER BY created_at, action_id
                """,
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_recent_actions(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        safe_limit = max(1, min(int(limit or 20), 500))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM distill_action_log
                ORDER BY created_at DESC, action_id DESC LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_knowledge_actions(self, action_id: str) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM knowledge_action_log
                WHERE action_id=? ORDER BY created_at, id
                """,
                (action_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_cognitive_actions(self, action_id: str) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM cognitive_action_log
                WHERE distill_action_id=? ORDER BY created_at, id
                """,
                (action_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_cognitive_intents(self, action_id: str) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM cognitive_action_intents
                WHERE distill_action_id=? ORDER BY created_at, cognitive_action_id
                """,
                (action_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def cognitive_action_counts(self) -> dict[str, int]:
        if not self.db_path.exists():
            return {}
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT cognitive_action, COUNT(*) AS count
                FROM cognitive_action_log
                GROUP BY cognitive_action ORDER BY cognitive_action
                """
            ).fetchall()
        return {str(row["cognitive_action"]): int(row["count"]) for row in rows}

    def insert_parent_action(self, values: Mapping[str, Any]) -> dict[str, Any]:
        columns = (
            "action_id",
            "created_at",
            "session_id",
            "source_agent",
            "action",
            "distill_intent",
            "claim_id",
            "target_page",
            "target_kind",
            "source_event_ids",
            "evidence_refs",
            "backup_path",
            "result_status",
            "result_detail",
            "error",
            "merge_decision_card",
        )
        payload = {column: values.get(column, "") for column in columns}
        with self.connect() as conn:
            conn.execute(
                render_sql(
                    """
                INSERT INTO distill_action_log ({columns})
                VALUES ({values})
                ON CONFLICT(action_id) DO NOTHING
                """,
                    identifier_lists={"columns": columns},
                    placeholder_counts={"values": len(columns)},
                ),
                tuple(payload[column] for column in columns),
            )
            row = conn.execute(
                "SELECT * FROM distill_action_log WHERE action_id=?",
                (payload["action_id"],),
            ).fetchone()
            if row is None:
                raise DistillActionStateError("parent action insert did not persist")
            immutable = (
                "session_id",
                "source_agent",
                "action",
                "distill_intent",
                "claim_id",
            )
            drift = [key for key in immutable if str(row[key]) != str(payload[key])]
            if drift:
                raise DistillActionStateError(
                    "parent action identity drift: " + ", ".join(drift)
                )
            event_id = stable_id("dae", payload["action_id"], "recorded")
            conn.execute(
                """
                INSERT INTO distill_action_events (
                    event_id, action_id, created_at, event_type, status, detail
                ) VALUES (?, ?, ?, 'recorded', ?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (
                    event_id,
                    payload["action_id"],
                    str(row["created_at"]),
                    str(row["result_status"]),
                    canonical_json({"target_kind": row["target_kind"]}),
                ),
            )
            conn.commit()
        return dict(row)

    def insert_knowledge_action(
        self,
        action_id: str,
        *,
        change_type: str,
        target_page: str,
        backup_path: str,
        event_type: str,
        detail: Mapping[str, Any],
    ) -> None:
        detail_json = canonical_json(dict(detail))
        identity = stable_id(
            "ka",
            action_id,
            change_type,
            target_page,
            event_type,
            detail_json,
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO knowledge_action_log (
                    knowledge_action_id, action_id, created_at, change_type,
                    target_page, backup_path, event_type, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(knowledge_action_id) DO NOTHING
                """,
                (
                    identity,
                    action_id,
                    now_utc(),
                    change_type,
                    target_page,
                    backup_path,
                    event_type,
                    detail_json,
                ),
            )
            conn.commit()

    def record_cognitive_intent(
        self,
        *,
        cognitive_action_id: str,
        parent: Mapping[str, Any],
        cognitive_action: str,
        episode_id: str,
        fragment_ids: Sequence[str],
        artifact: Mapping[str, Any],
        artifact_path: Path,
        target_kind: str,
        evidence_refs: Sequence[str],
        acl: Mapping[str, Any],
        input_spec_hash: str,
        extraction_output_hash: str,
        detail: Mapping[str, Any],
        allow_command: bool = True,
    ) -> bool:
        parent_status = str(parent.get("result_status") or "")
        disposition = (
            "authority_blocked"
            if not allow_command
            else ("parent_not_committed" if parent_status != "applied" else "command_created")
        )
        created_at = str(artifact.get("created_at") or now_utc())
        normalized_fragment_ids = tuple(
            dict.fromkeys(str(value) for value in fragment_ids if value)
        )
        if not episode_id or not normalized_fragment_ids:
            raise DistillActionStateError("cognitive intent requires episode and fragment mapping")
        source_event_ids = _json_list(parent.get("source_event_ids"))
        intent_detail = {**dict(detail), "target_kind": target_kind}
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO cognitive_action_intents (
                    cognitive_action_id, distill_action_id, created_at,
                    session_id, claim_id, cognitive_action, parent_status,
                    disposition, episode_id, fragment_ids, source_event_ids, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cognitive_action_id) DO NOTHING
                """,
                (
                    cognitive_action_id,
                    str(parent["action_id"]),
                    created_at,
                    str(parent["session_id"]),
                    str(parent["claim_id"]),
                    cognitive_action,
                    parent_status,
                    disposition,
                    episode_id,
                    canonical_json(normalized_fragment_ids),
                    canonical_json(source_event_ids),
                    canonical_json(intent_detail),
                ),
            )
            if parent_status != "applied" or not allow_command:
                conn.commit()
                return False

            artifact_payload = canonical_json(dict(artifact))
            artifact_hash = sha256_json(artifact)
            conn.execute(
                """
                INSERT INTO cognitive_action_log (
                    cognitive_action_id, distill_action_id, created_at,
                    session_id, source_agent, claim_id, cognitive_action,
                    target_kind, status, source_event_ids, evidence_refs,
                    artifact_path, artifact_schema_version, artifact_hash,
                    artifact_payload, episode_id, fragment_ids, acl_payload,
                    input_spec_hash, extraction_output_hash, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cognitive_action_id) DO NOTHING
                """,
                (
                    cognitive_action_id,
                    str(parent["action_id"]),
                    created_at,
                    str(parent["session_id"]),
                    str(parent["source_agent"]),
                    str(parent["claim_id"]),
                    cognitive_action,
                    target_kind,
                    canonical_json(source_event_ids),
                    canonical_json(list(evidence_refs)),
                    str(artifact_path),
                    ARTIFACT_SCHEMA_VERSION,
                    artifact_hash,
                    artifact_payload,
                    episode_id,
                    canonical_json(normalized_fragment_ids),
                    canonical_json(dict(acl)),
                    input_spec_hash,
                    extraction_output_hash,
                    canonical_json(dict(detail)),
                ),
            )
            row = conn.execute(
                "SELECT artifact_hash FROM cognitive_action_log WHERE cognitive_action_id=?",
                (cognitive_action_id,),
            ).fetchone()
            if row is None or str(row[0]) != artifact_hash:
                raise DistillActionStateError("cognitive action replay changed its artifact")
            event_id = stable_id("cae", cognitive_action_id, "queued")
            conn.execute(
                """
                INSERT INTO cognitive_action_events (
                    event_id, cognitive_action_id, created_at, event_type,
                    from_status, to_status, detail
                ) VALUES (?, ?, ?, 'enqueued', '', 'queued', ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (event_id, cognitive_action_id, created_at, canonical_json({})),
            )
            conn.commit()
        return True

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        action_id: str = "",
    ) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="microseconds")
        lease_expires = (now + timedelta(seconds=max(1, lease_seconds))).isoformat(
            timespec="microseconds"
        )
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            params: list[Any] = [now_text, now_text]
            action_clause = ""
            if action_id:
                action_clause = " AND cognitive_action_id=?"
                params.append(action_id)
            row = conn.execute(
                render_sql(
                    """
                SELECT * FROM cognitive_action_log
                WHERE (
                    (status IN ('queued', 'retry') AND (next_attempt_at='' OR next_attempt_at<=?))
                    OR (status='processing' AND lease_expires_at<>'' AND lease_expires_at<=?)
                )
                {action_clause}
                ORDER BY created_at, id LIMIT 1
                """,
                    fixed_fragments={
                        "action_clause": (
                            action_clause,
                            {"", " AND cognitive_action_id=?"},
                        )
                    },
                ),
                tuple(params),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            previous = str(row["status"])
            attempt_number = int(row["attempt_count"] or 0) + 1
            attempt_id = stable_id(
                "caa",
                str(row["cognitive_action_id"]),
                attempt_number,
                uuid.uuid4().hex,
            )
            updated = conn.execute(
                """
                UPDATE cognitive_action_log
                SET status='processing', attempt_count=?, lease_owner=?,
                    lease_expires_at=?, error=''
                WHERE cognitive_action_id=? AND status=?
                """,
                (
                    attempt_number,
                    worker_id,
                    lease_expires,
                    str(row["cognitive_action_id"]),
                    previous,
                ),
            ).rowcount
            if updated != 1:
                conn.rollback()
                return None
            self._append_action_event(
                conn,
                str(row["cognitive_action_id"]),
                event_type="leased",
                from_status=previous,
                to_status="processing",
                detail={"worker_id": worker_id, "attempt_id": attempt_id},
            )
            self._append_attempt_event(
                conn,
                attempt_id,
                str(row["cognitive_action_id"]),
                "started",
                {"worker_id": worker_id, "attempt_number": attempt_number},
            )
            conn.commit()
            claimed = conn.execute(
                "SELECT * FROM cognitive_action_log WHERE cognitive_action_id=?",
                (str(row["cognitive_action_id"]),),
            ).fetchone()
        payload = dict(claimed) if claimed else None
        if payload is not None:
            payload["attempt_id"] = attempt_id
        return payload

    def complete_effect(
        self,
        *,
        row: Mapping[str, Any],
        worker_id: str,
        effect: CognitiveEffectCommit,
    ) -> None:
        effect.validate()
        action_id = str(row["cognitive_action_id"])
        attempt_id = str(row.get("attempt_id") or "")
        self._validate_reciprocal_effect(row, effect)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM cognitive_action_log WHERE cognitive_action_id=?",
                (action_id,),
            ).fetchone()
            if current is None:
                raise DistillActionStateError("cognitive action disappeared during commit")
            existing = conn.execute(
                "SELECT * FROM cognitive_action_effects WHERE cognitive_action_id=?",
                (action_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO cognitive_action_effects (
                        effect_id, cognitive_action_id, target, target_object_id,
                        before_hash, after_hash, expected_delta_hash,
                        reciprocal_receipt, receipt_db_path, committed_at, detail
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        effect.effect_id,
                        action_id,
                        effect.target,
                        effect.target_object_id,
                        effect.before_hash,
                        effect.after_hash,
                        effect.expected_delta_hash,
                        effect.reciprocal_receipt,
                        effect.receipt_db_path,
                        effect.committed_at,
                        canonical_json(dict(effect.detail)),
                    ),
                )
            elif str(existing["effect_id"]) != effect.effect_id:
                raise DistillActionStateError("cognitive action already has a different effect")
            consumption_id = stable_id("cac", action_id, effect.target)
            conn.execute(
                """
                INSERT INTO cognitive_action_consumptions (
                    consumption_id, cognitive_action_id, consumed_at,
                    consumer, status, effect_id, detail
                ) VALUES (?, ?, ?, ?, 'applied', ?, ?)
                ON CONFLICT(consumption_id) DO NOTHING
                """,
                (
                    consumption_id,
                    action_id,
                    effect.committed_at,
                    effect.target,
                    effect.effect_id,
                    canonical_json(
                        {
                            "target_object_id": effect.target_object_id,
                            "reciprocal_receipt": effect.reciprocal_receipt,
                        }
                    ),
                ),
            )
            if str(current["status"]) != "applied":
                if (
                    str(current["status"]) != "processing"
                    or str(current["lease_owner"]) != worker_id
                ):
                    raise DistillActionStateError(
                        "worker no longer owns the cognitive action lease"
                    )
                conn.execute(
                    """
                    UPDATE cognitive_action_log
                    SET status='applied', processed_at=?, error='',
                        lease_owner='', lease_expires_at='', next_attempt_at=''
                    WHERE cognitive_action_id=?
                    """,
                    (effect.committed_at, action_id),
                )
                self._append_action_event(
                    conn,
                    action_id,
                    event_type="effect_committed",
                    from_status="processing",
                    to_status="applied",
                    detail={"effect_id": effect.effect_id},
                )
                self._append_attempt_event(
                    conn,
                    attempt_id,
                    action_id,
                    "committed",
                    {"effect_id": effect.effect_id},
                )
            conn.commit()

    def _validate_reciprocal_effect(
        self,
        row: Mapping[str, Any],
        effect: CognitiveEffectCommit,
    ) -> None:
        """Independently prove that the target database signed the effect."""
        receipt_path = Path(effect.receipt_db_path).resolve()
        if receipt_path == self.db_path.resolve():
            raise DistillActionStateError("action database cannot self-sign a target effect")
        if not receipt_path.is_file():
            raise DistillActionStateError("target reciprocal receipt database is missing")
        try:
            uri = f"file:{receipt_path}?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                conn.row_factory = sqlite3.Row
                stored = conn.execute(
                    """
                    SELECT * FROM cognitive_action_target_receipts
                    WHERE effect_id=?
                    """,
                    (effect.effect_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise DistillActionStateError(
                "target reciprocal receipt cannot be independently read"
            ) from exc
        if stored is None:
            raise DistillActionStateError("target reciprocal receipt row is missing")
        expected = {
            "cognitive_action_id": str(row["cognitive_action_id"]),
            "action": str(row["cognitive_action"]),
            "target": effect.target,
            "target_object_id": effect.target_object_id,
            "before_hash": effect.before_hash,
            "after_hash": effect.after_hash,
            "expected_delta_hash": effect.expected_delta_hash,
            "artifact_hash": str(row["artifact_hash"]),
            "committed_at": effect.committed_at,
        }
        drift = [key for key, value in expected.items() if str(stored[key]) != str(value)]
        if drift:
            raise DistillActionStateError(
                "target reciprocal receipt drift: " + ", ".join(sorted(drift))
            )
        if str(stored["schema_version"]) != "mnemos.cognitive_action_target_receipt.v1":
            raise DistillActionStateError("target reciprocal receipt schema is unsupported")
        expected_ref = (
            f"{receipt_path.name}:cognitive_action_target_receipts:{effect.effect_id}"
        )
        if effect.reciprocal_receipt != expected_ref:
            raise DistillActionStateError("target reciprocal receipt reference drift")

    def fail_attempt(
        self,
        *,
        row: Mapping[str, Any],
        worker_id: str,
        error: str,
        retryable: bool,
        max_attempts: int,
    ) -> str:
        action_id = str(row["cognitive_action_id"])
        attempt_id = str(row.get("attempt_id") or "")
        attempt_count = int(row.get("attempt_count") or 0)
        next_status = "retry" if retryable and attempt_count < max_attempts else "dead"
        next_attempt_at = (
            (datetime.now(timezone.utc) + timedelta(seconds=min(60, 2**attempt_count))).isoformat(
                timespec="microseconds"
            )
            if next_status == "retry"
            else ""
        )
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status, lease_owner FROM cognitive_action_log WHERE cognitive_action_id=?",
                (action_id,),
            ).fetchone()
            if current is None or str(current["status"]) != "processing":
                raise DistillActionStateError("cannot fail an unleased cognitive action")
            if str(current["lease_owner"]) != worker_id:
                raise DistillActionStateError("worker no longer owns the cognitive action lease")
            conn.execute(
                """
                UPDATE cognitive_action_log
                SET status=?, next_attempt_at=?, lease_owner='', lease_expires_at='',
                    processed_at=?, error=?
                WHERE cognitive_action_id=?
                """,
                (next_status, next_attempt_at, now_utc(), error, action_id),
            )
            self._append_action_event(
                conn,
                action_id,
                event_type="attempt_failed",
                from_status="processing",
                to_status=next_status,
                detail={"error": error, "retryable": retryable},
            )
            self._append_attempt_event(
                conn,
                attempt_id,
                action_id,
                "retryable_failed" if next_status == "retry" else "dead",
                {"error": error},
            )
            conn.commit()
        return next_status

    @staticmethod
    def _append_action_event(
        conn: sqlite3.Connection,
        action_id: str,
        *,
        event_type: str,
        from_status: str,
        to_status: str,
        detail: Mapping[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO cognitive_action_events (
                event_id, cognitive_action_id, created_at, event_type,
                from_status, to_status, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "cae_" + uuid.uuid4().hex,
                action_id,
                now_utc(),
                event_type,
                from_status,
                to_status,
                canonical_json(dict(detail)),
            ),
        )

    @staticmethod
    def _append_attempt_event(
        conn: sqlite3.Connection,
        attempt_id: str,
        action_id: str,
        event_type: str,
        detail: Mapping[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO cognitive_action_attempt_events (
                event_id, attempt_id, cognitive_action_id, created_at,
                event_type, detail
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "caae_" + uuid.uuid4().hex,
                attempt_id,
                action_id,
                now_utc(),
                event_type,
                canonical_json(dict(detail)),
            ),
        )


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    try:
        loaded = json.loads(str(value or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    return list(loaded) if isinstance(loaded, list) else []
