"""Canonical append-only schema authority for ``action_ledger.db``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable

SCHEMA_VERSION = "mnemos.action_ledger_schema.v1"
SCHEMA_COMPONENT = "action_ledger"
REGISTRY_TABLE = "mnemos_schema_registry"

CANONICAL_DDL = f"""
CREATE TABLE action_ledger (
    action_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    actor TEXT NOT NULL,
    action_type TEXT NOT NULL,
    target TEXT NOT NULL,
    before_ref TEXT NOT NULL DEFAULT '',
    after_ref TEXT NOT NULL DEFAULT '',
    evidence_refs_json TEXT NOT NULL CHECK(json_valid(evidence_refs_json)),
    quality_decision_id TEXT NOT NULL DEFAULT '',
    verification_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(verification_json)),
    rollback_ref TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_action_ledger_type_status ON action_ledger(action_type, status);
CREATE TABLE {REGISTRY_TABLE} (
    component TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    ddl_hash TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TRIGGER action_ledger_no_update
BEFORE UPDATE ON action_ledger BEGIN
    SELECT RAISE(ABORT, 'action_ledger is append-only');
END;
CREATE TRIGGER action_ledger_no_delete
BEFORE DELETE ON action_ledger BEGIN
    SELECT RAISE(ABORT, 'action_ledger is append-only');
END;
"""


class ActionLedgerSchemaError(RuntimeError):
    """The action ledger requires explicit schema reconciliation."""


@dataclass(frozen=True)
class ActionLedgerSchemaState:
    classification: str
    ddl_hash: str
    canonical_ddl_hash: str
    registry_version: str
    registry_hash: str
    row_count: int
    migration_required: bool
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.migration_required and not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "schema_version": SCHEMA_VERSION,
            "ddl_hash": self.ddl_hash,
            "canonical_ddl_hash": self.canonical_ddl_hash,
            "registry_version": self.registry_version,
            "registry_hash": self.registry_hash,
            "row_count": self.row_count,
            "migration_required": self.migration_required,
            "errors": list(self.errors),
            "ok": self.ok,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _objects(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
            "WHERE (name='action_ledger' OR name='idx_action_ledger_type_status' "
            "OR name IN ('action_ledger_no_update', 'action_ledger_no_delete') "
            f"OR name='{REGISTRY_TABLE}') ORDER BY type, name"  # nosec B608
        ).fetchall()
    ]


def _hash(conn: sqlite3.Connection) -> str:
    payload: list[dict[str, Any]] = []
    for object_type, name, sql in _objects(conn):
        item: dict[str, Any] = {
            "type": str(object_type),
            "name": str(name),
            "sql": " ".join(str(sql).split()),
        }
        if object_type == "table":
            item["columns"] = [
                tuple(row)
                for row in conn.execute(f"PRAGMA table_xinfo('{name}')").fetchall()
            ]
        payload.append(item)
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _execute_ddl(conn: sqlite3.Connection) -> None:
    statement = ""
    for line in CANONICAL_DDL.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            statement = ""
            if sql:
                conn.execute(sql)
    if statement.strip():
        raise ActionLedgerSchemaError("action ledger DDL is incomplete")


def _canonical_hash() -> str:
    with sqlite3.connect(":memory:") as conn:
        _execute_ddl(conn)
        return _hash(conn)


CANONICAL_DDL_HASH = _canonical_hash()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def inspect_action_ledger_schema(conn: sqlite3.Connection) -> ActionLedgerSchemaState:
    if not _table_exists(conn, "action_ledger"):
        return ActionLedgerSchemaState("absent", "", CANONICAL_DDL_HASH, "", "", 0, False, ())
    row_count = int(conn.execute("SELECT COUNT(*) FROM action_ledger").fetchone()[0])
    ddl_hash = _hash(conn)
    registry_version = ""
    registry_hash = ""
    registry_error = ""
    if _table_exists(conn, REGISTRY_TABLE):
        registry_columns = tuple(
            str(row[1]) for row in conn.execute(f"PRAGMA table_info('{REGISTRY_TABLE}')")
        )
        if registry_columns != ("component", "schema_version", "ddl_hash", "applied_at"):
            registry_error = "unknown action schema registry structure"
        else:
            row = conn.execute(
                f"SELECT schema_version, ddl_hash FROM {REGISTRY_TABLE} WHERE component=?",  # nosec B608
                (SCHEMA_COMPONENT,),
            ).fetchone()
            if row:
                registry_version, registry_hash = str(row[0]), str(row[1])
    if (
        ddl_hash == CANONICAL_DDL_HASH
        and registry_version == SCHEMA_VERSION
        and registry_hash == CANONICAL_DDL_HASH
    ):
        return ActionLedgerSchemaState(
            "canonical",
            ddl_hash,
            CANONICAL_DDL_HASH,
            registry_version,
            registry_hash,
            row_count,
            False,
            (),
        )
    columns = tuple(str(row[1]) for row in conn.execute("PRAGMA table_info(action_ledger)"))
    expected = (
        "action_id",
        "schema_version",
        "actor",
        "action_type",
        "target",
        "before_ref",
        "after_ref",
        "evidence_refs_json",
        "quality_decision_id",
        "verification_json",
        "rollback_ref",
        "status",
        "created_at",
    )
    if columns == expected and not registry_error:
        classification = "legacy_mutable_v0"
        errors: tuple[str, ...] = ()
    else:
        classification = "unknown"
        errors = tuple(
            value
            for value in ("unknown action ledger schema" if columns != expected else "", registry_error)
            if value
        )
    return ActionLedgerSchemaState(
        classification,
        ddl_hash,
        CANONICAL_DDL_HASH,
        registry_version,
        registry_hash,
        row_count,
        True,
        errors,
    )


def _write_registry(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"INSERT INTO {REGISTRY_TABLE} VALUES (?, ?, ?, ?)",  # nosec B608
        (SCHEMA_COMPONENT, SCHEMA_VERSION, CANONICAL_DDL_HASH, _now()),
    )


def initialize_action_ledger_schema(path: Path | sqlite3.Connection) -> None:
    if isinstance(path, sqlite3.Connection):
        owns = False
        conn = path
    else:
        owns = True
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
    try:
        state = inspect_action_ledger_schema(conn)
        if state.classification == "absent":
            _execute_ddl(conn)
            _write_registry(conn)
            conn.commit()
        elif not state.ok:
            raise ActionLedgerSchemaError(
                "action ledger migration required; run scripts/reconcile_action_ledger.py"
            )
    finally:
        if owns:
            conn.close()


def reconcile_action_ledger_schema(
    conn: sqlite3.Connection,
    *,
    apply: bool = False,
    failpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    before = inspect_action_ledger_schema(conn)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "applied": False,
        "before": before.as_dict(),
        "after": before.as_dict(),
    }
    if before.ok:
        report["action"] = "already_canonical"
        return report
    if before.classification == "unknown":
        raise ActionLedgerSchemaError("unknown action ledger schema cannot be migrated")
    if not apply:
        report["action"] = (
            "create_fresh_schema" if before.classification == "absent" else "make_append_only"
        )
        return report
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if before.classification == "absent":
            _execute_ddl(conn)
        else:
            conn.execute("DROP TRIGGER IF EXISTS action_ledger_no_update")
            conn.execute("DROP TRIGGER IF EXISTS action_ledger_no_delete")
            conn.execute("ALTER TABLE action_ledger RENAME TO __legacy__action_ledger")
            conn.execute("DROP INDEX IF EXISTS idx_action_ledger_type_status")
            legacy_registry = ""
            if _table_exists(conn, REGISTRY_TABLE):
                legacy_registry = f"__legacy__{REGISTRY_TABLE}"
                conn.execute(
                    f'ALTER TABLE "{REGISTRY_TABLE}" RENAME TO "{legacy_registry}"'  # nosec B608
                )
            _execute_ddl(conn)
            conn.execute(
                """
                INSERT INTO action_ledger (
                    action_id, schema_version, actor, action_type, target,
                    before_ref, after_ref, evidence_refs_json,
                    quality_decision_id, verification_json, rollback_ref,
                    status, created_at
                )
                SELECT action_id, schema_version, actor, action_type, target,
                       before_ref, after_ref, evidence_refs_json,
                       quality_decision_id, verification_json, rollback_ref,
                       status, created_at
                FROM __legacy__action_ledger
                """
            )
            if legacy_registry:
                conn.execute(
                    f"""
                    INSERT INTO {REGISTRY_TABLE}(
                        component, schema_version, ddl_hash, applied_at
                    )
                    SELECT component, schema_version, ddl_hash, applied_at
                    FROM "{legacy_registry}" WHERE component != ?
                    """,  # nosec B608 - internally generated table name
                    (SCHEMA_COMPONENT,),
                )
            if failpoint:
                failpoint("after_copy")
            conn.execute("DROP TABLE __legacy__action_ledger")
            if legacy_registry:
                conn.execute(f'DROP TABLE "{legacy_registry}"')  # nosec B608
        _write_registry(conn)
        after = inspect_action_ledger_schema(conn)
        if not after.ok or after.row_count != before.row_count:
            raise ActionLedgerSchemaError("post-migration action ledger verification failed")
        if failpoint:
            failpoint("before_commit")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    report.update({"applied": True, "action": "migrated", "after": after.as_dict()})
    return report
