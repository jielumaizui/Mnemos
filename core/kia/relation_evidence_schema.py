# -*- coding: utf-8 -*-
"""Canonical schema authority for ``knowledge_graph.db.relation_evidence``.

Constructors may create this schema only for a fresh database.  Existing tables
must already match the registered version/hash; legacy schemas are changed only
through the explicit reconciliation entrypoint.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = "mnemos.relation_evidence_schema.v1"
SCHEMA_COMPONENT = "knowledge_graph.relation_evidence"
REGISTRY_TABLE = "mnemos_schema_registry"

CANONICAL_TABLE_DDL = """
CREATE TABLE relation_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_id INTEGER NOT NULL,
    evidence_type TEXT NOT NULL,
    content TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (relation_id) REFERENCES relations(id) ON DELETE CASCADE
)
"""

REGISTRY_DDL = f"""
CREATE TABLE IF NOT EXISTS {REGISTRY_TABLE} (
    component TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    ddl_hash TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""

INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_evidence_relation "
    "ON relation_evidence(relation_id)"
)

_CANONICAL_COLUMNS = (
    ("id", "INTEGER", 0, None, 1),
    ("relation_id", "INTEGER", 1, None, 0),
    ("evidence_type", "TEXT", 1, None, 0),
    ("content", "TEXT", 0, None, 0),
    ("created_at", "TEXT", 0, "CURRENT_TIMESTAMP", 0),
)
_LEGACY_RM_COLUMNS = (
    ("id", "INTEGER", 0, None, 1),
    ("relation_id", "INTEGER", 1, None, 0),
    ("evidence_type", "TEXT", 0, "'quote'", 0),
    ("content", "TEXT", 0, "''", 0),
    ("created_at", "TEXT", 0, "CURRENT_TIMESTAMP", 0),
)
_CANONICAL_FOREIGN_KEYS = (
    ("relations", "relation_id", "id", "NO ACTION", "CASCADE"),
)
_CANONICAL_INDEXES = (("idx_evidence_relation", ("relation_id",)),)


class RelationEvidenceSchemaError(RuntimeError):
    """The relation evidence table is unsafe to use without reconciliation."""


@dataclass(frozen=True)
class RelationEvidenceSchemaState:
    classification: str
    schema_version: str
    ddl_hash: str
    canonical_ddl_hash: str
    registry_version: str
    registry_ddl_hash: str
    row_count: int
    null_evidence_type_count: int
    blank_evidence_type_count: int
    null_content_count: int
    column_signature: tuple[tuple[Any, ...], ...]
    foreign_key_signature: tuple[tuple[str, ...], ...]
    index_signature: tuple[tuple[str, tuple[str, ...]], ...]
    migration_required: bool
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors and not self.migration_required

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "classification": self.classification,
            "ddl_hash": self.ddl_hash,
            "canonical_ddl_hash": self.canonical_ddl_hash,
            "registry_version": self.registry_version,
            "registry_ddl_hash": self.registry_ddl_hash,
            "row_count": self.row_count,
            "null_evidence_type_count": self.null_evidence_type_count,
            "blank_evidence_type_count": self.blank_evidence_type_count,
            "null_content_count": self.null_content_count,
            "columns": [
                {
                    "name": item[0],
                    "type": item[1],
                    "notnull": item[2],
                    "default": item[3],
                    "pk": item[4],
                }
                for item in self.column_signature
            ],
            "foreign_keys": [list(item) for item in self.foreign_key_signature],
            "indexes": [
                {"name": name, "columns": list(columns)}
                for name, columns in self.index_signature
            ],
            "migration_required": self.migration_required,
            "errors": list(self.errors),
            "ok": self.ok,
        }


def _columns(conn: sqlite3.Connection) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
        for row in conn.execute("PRAGMA table_info(relation_evidence)").fetchall()
    )


def _foreign_keys(conn: sqlite3.Connection) -> tuple[tuple[str, ...], ...]:
    return tuple(
        (
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]).upper(),
            str(row[6]).upper(),
        )
        for row in conn.execute("PRAGMA foreign_key_list(relation_evidence)").fetchall()
    )


def _signature_hash(
    columns: tuple[tuple[Any, ...], ...],
    foreign_keys: tuple[tuple[str, ...], ...],
    indexes: tuple[tuple[str, tuple[str, ...]], ...],
) -> str:
    payload = {"columns": columns, "foreign_keys": foreign_keys, "indexes": indexes}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


CANONICAL_DDL_HASH = _signature_hash(
    _CANONICAL_COLUMNS,
    _CANONICAL_FOREIGN_KEYS,
    _CANONICAL_INDEXES,
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _indexes(conn: sqlite3.Connection) -> tuple[tuple[str, tuple[str, ...]], ...]:
    result: list[tuple[str, tuple[str, ...]]] = []
    for row in conn.execute("PRAGMA index_list(relation_evidence)").fetchall():
        name = str(row[1])
        if name.startswith("sqlite_autoindex_"):
            continue
        columns = tuple(
            str(item[0])
            for item in conn.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                (name,),
            ).fetchall()
        )
        result.append((name, columns))
    return tuple(sorted(result))


def _registry_row(conn: sqlite3.Connection) -> tuple[str, str]:
    if not _table_exists(conn, REGISTRY_TABLE):
        return "", ""
    try:
        row = conn.execute(
            f"SELECT schema_version, ddl_hash FROM {REGISTRY_TABLE} WHERE component=?",  # nosec B608
            (SCHEMA_COMPONENT,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise RelationEvidenceSchemaError(
            f"invalid {REGISTRY_TABLE} schema; explicit repair is required: {exc}"
        ) from exc
    return (str(row[0]), str(row[1])) if row else ("", "")


def inspect_relation_evidence_schema(conn: sqlite3.Connection) -> RelationEvidenceSchemaState:
    if not _table_exists(conn, "relation_evidence"):
        return RelationEvidenceSchemaState(
            classification="absent",
            schema_version=SCHEMA_VERSION,
            ddl_hash="",
            canonical_ddl_hash=CANONICAL_DDL_HASH,
            registry_version="",
            registry_ddl_hash="",
            row_count=0,
            null_evidence_type_count=0,
            blank_evidence_type_count=0,
            null_content_count=0,
            column_signature=(),
            foreign_key_signature=(),
            index_signature=(),
            migration_required=False,
            errors=(),
        )

    columns = _columns(conn)
    foreign_keys = _foreign_keys(conn)
    indexes = _indexes(conn)
    ddl_hash = _signature_hash(columns, foreign_keys, indexes)
    if columns == _CANONICAL_COLUMNS and foreign_keys == _CANONICAL_FOREIGN_KEYS:
        classification = "canonical_signature"
    elif columns == _LEGACY_RM_COLUMNS and foreign_keys == _CANONICAL_FOREIGN_KEYS:
        classification = "legacy_relation_manager_v0"
    else:
        classification = "unknown"

    column_names = {str(item[0]) for item in columns}
    row_count = int(conn.execute("SELECT COUNT(*) FROM relation_evidence").fetchone()[0])
    if {"evidence_type", "content"} <= column_names:
        row = conn.execute(
            """SELECT SUM(evidence_type IS NULL),
                      SUM(evidence_type IS NOT NULL AND TRIM(evidence_type) = ''),
                      SUM(content IS NULL)
                 FROM relation_evidence"""
        ).fetchone()
        null_type = int(row[0] or 0)
        blank_type = int(row[1] or 0)
        null_content = int(row[2] or 0)
    else:
        null_type = blank_type = null_content = 0
    registry_version, registry_hash = _registry_row(conn)
    if classification == "canonical_signature":
        classification = (
            "canonical"
            if ddl_hash == CANONICAL_DDL_HASH
            and registry_version == SCHEMA_VERSION
            and registry_hash == CANONICAL_DDL_HASH
            else "legacy_knowledge_graph_v0"
        )
    errors: list[str] = []
    migration_required = False

    if classification == "unknown":
        errors.append("unknown relation_evidence schema; automatic migration is refused")
        migration_required = True
    elif classification != "canonical":
        migration_required = True
    if null_type or blank_type:
        errors.append(
            "relation_evidence contains missing evidence_type values; "
            "manual classification is required before migration"
        )
        migration_required = True
    if registry_version != SCHEMA_VERSION or registry_hash != CANONICAL_DDL_HASH:
        migration_required = True

    return RelationEvidenceSchemaState(
        classification=classification,
        schema_version=SCHEMA_VERSION,
        ddl_hash=ddl_hash,
        canonical_ddl_hash=CANONICAL_DDL_HASH,
        registry_version=registry_version,
        registry_ddl_hash=registry_hash,
        row_count=row_count,
        null_evidence_type_count=null_type,
        blank_evidence_type_count=blank_type,
        null_content_count=null_content,
        column_signature=columns,
        foreign_key_signature=foreign_keys,
        index_signature=indexes,
        migration_required=migration_required,
        errors=tuple(errors),
    )


def _write_registry_row(conn: sqlite3.Connection) -> None:
    conn.execute(REGISTRY_DDL)
    conn.execute(
        f"""INSERT INTO {REGISTRY_TABLE}(component, schema_version, ddl_hash, applied_at)
             VALUES (?, ?, ?, ?)
             ON CONFLICT(component) DO UPDATE SET
                 schema_version=excluded.schema_version,
                 ddl_hash=excluded.ddl_hash,
                 applied_at=excluded.applied_at""",  # nosec B608
        (
            SCHEMA_COMPONENT,
            SCHEMA_VERSION,
            CANONICAL_DDL_HASH,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def initialize_relation_evidence_schema(conn: sqlite3.Connection) -> None:
    """Create a fresh schema or validate an already registered canonical schema."""
    state = inspect_relation_evidence_schema(conn)
    if state.classification == "absent":
        conn.execute(CANONICAL_TABLE_DDL)
        conn.execute(INDEX_DDL)
        _write_registry_row(conn)
        return
    if not state.ok:
        detail = "; ".join(state.errors) or (
            f"classification={state.classification}, registry={state.registry_version or 'missing'}"
        )
        raise RelationEvidenceSchemaError(
            "relation_evidence migration required; run "
            "scripts/reconcile_relation_evidence_schema.py before opening writers: "
            + detail
        )


def validate_existing_relation_evidence_schema(conn: sqlite3.Connection) -> None:
    """Fail before any constructor-side DDL when an existing table needs migration."""
    state = inspect_relation_evidence_schema(conn)
    if state.classification == "absent" or state.ok:
        return
    detail = "; ".join(state.errors) or (
        f"classification={state.classification}, registry={state.registry_version or 'missing'}"
    )
    raise RelationEvidenceSchemaError(
        "relation_evidence migration required before database initialization: " + detail
    )


def _copy_prior_rows(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO relation_evidence(
               id, relation_id, evidence_type, content, created_at
           )
           SELECT id, relation_id, evidence_type, content, created_at
             FROM relation_evidence__legacy"""
    )


def reconcile_relation_evidence_schema(
    conn: sqlite3.Connection,
    *,
    apply: bool = False,
    copy_rows: Callable[[sqlite3.Connection], None] = _copy_prior_rows,
) -> dict[str, Any]:
    """Inspect or transactionally reconcile either recognized prior schema."""
    before = inspect_relation_evidence_schema(conn)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "applied": False,
        "before": before.as_dict(),
        "after": before.as_dict(),
        "would_rebuild": before.classification == "legacy_relation_manager_v0",
        "row_count_preserved": True,
    }
    if before.classification == "unknown" or before.errors:
        raise RelationEvidenceSchemaError("; ".join(before.errors))
    if before.classification == "absent":
        if not apply:
            report["action"] = "create_fresh_schema"
            return report
    elif before.ok:
        report["action"] = "already_canonical"
        return report
    elif not apply:
        report["action"] = "register_or_rebuild"
        return report

    conn.execute("BEGIN IMMEDIATE")
    try:
        if before.classification == "absent":
            conn.execute(CANONICAL_TABLE_DDL)
        elif before.classification == "legacy_relation_manager_v0":
            conn.execute("ALTER TABLE relation_evidence RENAME TO relation_evidence__legacy")
            conn.execute(CANONICAL_TABLE_DDL)
            copy_rows(conn)
            copied = int(conn.execute("SELECT COUNT(*) FROM relation_evidence").fetchone()[0])
            if copied != before.row_count:
                raise RelationEvidenceSchemaError(
                    f"row count changed during migration: {before.row_count} -> {copied}"
                )
            conn.execute("DROP TABLE relation_evidence__legacy")
        conn.execute(INDEX_DDL)
        _write_registry_row(conn)
        after = inspect_relation_evidence_schema(conn)
        if not after.ok or after.ddl_hash != CANONICAL_DDL_HASH:
            raise RelationEvidenceSchemaError(
                "post-migration schema verification failed: " + json.dumps(after.as_dict())
            )
        conn.commit()
    except (sqlite3.Error, RuntimeError):
        conn.rollback()
        raise

    report.update(
        {
            "applied": True,
            "action": "created" if before.classification == "absent" else "migrated",
            "after": after.as_dict(),
            "row_count_preserved": before.row_count == after.row_count,
        }
    )
    return report


def inspect_database(db_path: Path, *, read_only: bool = True) -> RelationEvidenceSchemaState:
    target = Path(db_path)
    if read_only:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(target)
    try:
        return inspect_relation_evidence_schema(conn)
    finally:
        conn.close()
