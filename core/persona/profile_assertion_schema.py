"""Canonical schema authority for the Profile v2 assertion revision ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SCHEMA_COMPONENT = "persona.profile_assertion_ledger"
SCHEMA_VERSION = "mnemos.profile_assertion_ledger.v1"
REGISTRY_TABLE = "mnemos_schema_registry"

PROFILE_ASSERTION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS profile_assertion_revisions (
    revision_id TEXT PRIMARY KEY,
    assertion_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    supersedes_revision_id TEXT REFERENCES profile_assertion_revisions(revision_id),
    dimension TEXT NOT NULL,
    claim TEXT NOT NULL,
    supporting_signals TEXT NOT NULL,
    contradicting_signals TEXT DEFAULT '[]',
    confidence REAL DEFAULT 0.0,
    privacy_level TEXT DEFAULT 'local',
    last_verified_at TEXT NOT NULL,
    revision_policy TEXT DEFAULT 'revise_on_contradiction',
    status TEXT DEFAULT 'active',
    access_control TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(assertion_id, revision_number),
    UNIQUE(assertion_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_profile_assertion_revisions_assertion
ON profile_assertion_revisions(assertion_id, revision_number DESC);
CREATE INDEX IF NOT EXISTS idx_profile_assertion_revisions_status
ON profile_assertion_revisions(status);

CREATE TABLE IF NOT EXISTS profile_assertion_heads (
    assertion_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL UNIQUE REFERENCES profile_assertion_revisions(revision_id),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS profile_assertion_revision_delete_permits (
    assertion_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER IF NOT EXISTS profile_assertion_revisions_no_update
BEFORE UPDATE ON profile_assertion_revisions
BEGIN
    SELECT RAISE(ABORT, 'profile assertion revisions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS profile_assertion_revisions_no_delete
BEFORE DELETE ON profile_assertion_revisions
WHEN NOT EXISTS (
    SELECT 1
    FROM profile_assertion_revision_delete_permits AS permit
    WHERE permit.assertion_id=OLD.assertion_id
)
BEGIN
    SELECT RAISE(ABORT, 'profile assertion revisions are append-only');
END;
"""

PROFILE_ASSERTION_PROJECTION_SQL = """
CREATE TABLE IF NOT EXISTS profile_assertions (
    assertion_id TEXT PRIMARY KEY,
    current_revision_id TEXT REFERENCES profile_assertion_revisions(revision_id),
    dimension TEXT NOT NULL,
    claim TEXT NOT NULL,
    supporting_signals TEXT NOT NULL,
    contradicting_signals TEXT DEFAULT '[]',
    confidence REAL DEFAULT 0.0,
    privacy_level TEXT DEFAULT 'local',
    last_verified_at TEXT NOT NULL,
    revision_policy TEXT DEFAULT 'revise_on_contradiction',
    status TEXT DEFAULT 'active',
    access_control TEXT NOT NULL DEFAULT '',
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_profile_assertions_dimension ON profile_assertions(dimension);
CREATE INDEX IF NOT EXISTS idx_profile_assertions_status ON profile_assertions(status);
"""

REGISTRY_SQL = f"""
CREATE TABLE IF NOT EXISTS {REGISTRY_TABLE} (
    component TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    ddl_hash TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""

OWNED_SCHEMA_OBJECTS = (
    "profile_assertions",
    "idx_profile_assertions_dimension",
    "idx_profile_assertions_status",
    "profile_assertion_revisions",
    "idx_profile_assertion_revisions_assertion",
    "idx_profile_assertion_revisions_status",
    "profile_assertion_heads",
    "profile_assertion_revision_delete_permits",
    "profile_assertion_revisions_no_update",
    "profile_assertion_revisions_no_delete",
)


def _normalize_sql(value: Any) -> str:
    return " ".join(str(value or "").split()).lower()


def _owned_schema_sql(conn: sqlite3.Connection) -> dict[str, str]:
    placeholders = ",".join("?" for _ in OWNED_SCHEMA_OBJECTS)
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        f"WHERE name IN ({placeholders})",  # nosec B608: fixed placeholder count
        OWNED_SCHEMA_OBJECTS,
    ).fetchall()
    return {str(name): _normalize_sql(sql) for name, sql in rows}


def _canonical_owned_schema_sql() -> dict[str, str]:
    with sqlite3.connect(":memory:") as conn:
        conn.executescript(PROFILE_ASSERTION_PROJECTION_SQL)
        conn.executescript(PROFILE_ASSERTION_SCHEMA_SQL)
        return _owned_schema_sql(conn)


CANONICAL_OWNED_SCHEMA_SQL = _canonical_owned_schema_sql()
_PROJECTION_SCHEMA_OBJECTS = (
    "profile_assertions",
    "idx_profile_assertions_dimension",
    "idx_profile_assertions_status",
)


def profile_assertion_projection_is_canonical(conn: sqlite3.Connection) -> bool:
    live = _owned_schema_sql(conn)
    return all(
        live.get(name) == CANONICAL_OWNED_SCHEMA_SQL.get(name)
        for name in _PROJECTION_SCHEMA_OBJECTS
    )


def _canonical_hash() -> str:
    payload = {
        "component": SCHEMA_COMPONENT,
        "schema_version": SCHEMA_VERSION,
        "owned_schema_sql": CANONICAL_OWNED_SCHEMA_SQL,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


CANONICAL_SCHEMA_HASH = _canonical_hash()


@dataclass(frozen=True)
class ProfileAssertionSchemaState:
    errors: tuple[str, ...]
    registry_version: str
    registry_hash: str

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "component": SCHEMA_COMPONENT,
            "canonical_schema_hash": CANONICAL_SCHEMA_HASH,
            "registry_version": self.registry_version,
            "registry_hash": self.registry_hash,
            "errors": list(self.errors),
            "ok": self.ok,
        }


class ProfileAssertionSchemaError(RuntimeError):
    """Raised when the ledger schema is absent, drifted, or unregistered."""


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, tuple[Any, ...]]:
    return {
        str(row[1]): (str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()  # nosec B608
    }


def _foreign_keys(conn: sqlite3.Connection, table: str) -> set[tuple[str, str, str]]:
    return {
        (str(row[2]), str(row[3]), str(row[4]))
        for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()  # nosec B608
    }


def inspect_profile_assertion_schema(
    conn: sqlite3.Connection,
    *,
    require_registry: bool = True,
) -> ProfileAssertionSchemaState:
    errors: list[str] = []
    required_columns = {
        "profile_assertions": {
            "assertion_id",
            "current_revision_id",
            "dimension",
            "claim",
            "supporting_signals",
            "contradicting_signals",
            "confidence",
            "privacy_level",
            "last_verified_at",
            "revision_policy",
            "status",
            "access_control",
            "updated_at",
        },
        "profile_assertion_revisions": {
            "revision_id",
            "assertion_id",
            "revision_number",
            "content_hash",
            "supersedes_revision_id",
            "dimension",
            "claim",
            "supporting_signals",
            "contradicting_signals",
            "confidence",
            "privacy_level",
            "last_verified_at",
            "revision_policy",
            "status",
            "access_control",
            "created_at",
        },
        "profile_assertion_heads": {"assertion_id", "revision_id", "updated_at"},
        "profile_assertion_revision_delete_permits": {
            "assertion_id",
            "request_id",
            "created_at",
        },
    }
    for table, required in required_columns.items():
        columns = _columns(conn, table)
        if not columns:
            errors.append(f"{table}_missing")
        elif missing := sorted(required - set(columns)):
            errors.append(f"{table}_missing_columns:{','.join(missing)}")

    if (
        "profile_assertion_revisions",
        "supersedes_revision_id",
        "revision_id",
    ) not in _foreign_keys(conn, "profile_assertion_revisions"):
        errors.append("revision_supersedes_self_fk_missing")
    if (
        "profile_assertion_revisions",
        "revision_id",
        "revision_id",
    ) not in _foreign_keys(conn, "profile_assertion_heads"):
        errors.append("head_revision_fk_missing")
    if (
        "profile_assertion_revisions",
        "current_revision_id",
        "revision_id",
    ) not in _foreign_keys(conn, "profile_assertions"):
        errors.append("projection_revision_fk_missing")

    trigger_names = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='profile_assertion_revisions'"
        ).fetchall()
    }
    for trigger in (
        "profile_assertion_revisions_no_update",
        "profile_assertion_revisions_no_delete",
    ):
        if trigger not in trigger_names:
            errors.append(f"{trigger}_missing")

    live_owned_schema = _owned_schema_sql(conn)
    if live_owned_schema != CANONICAL_OWNED_SCHEMA_SQL:
        missing = sorted(set(CANONICAL_OWNED_SCHEMA_SQL) - set(live_owned_schema))
        drifted = sorted(
            name
            for name in set(CANONICAL_OWNED_SCHEMA_SQL) & set(live_owned_schema)
            if CANONICAL_OWNED_SCHEMA_SQL[name] != live_owned_schema[name]
        )
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if drifted:
            details.append("drifted=" + ",".join(drifted))
        errors.append("profile_assertion_live_schema_hash_mismatch:" + ";".join(details))

    registry_version = ""
    registry_hash = ""
    if _columns(conn, REGISTRY_TABLE):
        row = conn.execute(
            f"SELECT schema_version, ddl_hash FROM {REGISTRY_TABLE} WHERE component=?",  # nosec B608
            (SCHEMA_COMPONENT,),
        ).fetchone()
        if row is not None:
            registry_version, registry_hash = str(row[0]), str(row[1])
    if require_registry and (
        registry_version != SCHEMA_VERSION or registry_hash != CANONICAL_SCHEMA_HASH
    ):
        errors.append("profile_assertion_schema_registry_mismatch")
    return ProfileAssertionSchemaState(tuple(errors), registry_version, registry_hash)


def register_profile_assertion_schema(conn: sqlite3.Connection) -> None:
    state = inspect_profile_assertion_schema(conn, require_registry=False)
    if not state.ok:
        raise ProfileAssertionSchemaError(
            "profile assertion schema requires explicit reconciliation: " + ", ".join(state.errors)
        )
    conn.execute(REGISTRY_SQL)
    conn.execute(
        f"""
        INSERT INTO {REGISTRY_TABLE}(component, schema_version, ddl_hash, applied_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(component) DO UPDATE SET
            schema_version=excluded.schema_version,
            ddl_hash=excluded.ddl_hash,
            applied_at=excluded.applied_at
        WHERE schema_version IS NOT excluded.schema_version
           OR ddl_hash IS NOT excluded.ddl_hash
        """,  # nosec B608
        (
            SCHEMA_COMPONENT,
            SCHEMA_VERSION,
            CANONICAL_SCHEMA_HASH,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def validate_profile_assertion_schema(conn: sqlite3.Connection) -> None:
    state = inspect_profile_assertion_schema(conn)
    if not state.ok:
        raise ProfileAssertionSchemaError(
            "profile assertion schema is unsafe: " + ", ".join(state.errors)
        )
