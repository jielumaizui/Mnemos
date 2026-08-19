"""Schema authority for typed knowledge-coverage gap revisions.

This file owns only the knowledge-coverage store used by
``BlindspotDiscovery``.  User cognitive blindspots remain owned by Hamartia and
interaction preferences remain owned by the persona/profile context.  An old
``blindspots`` table is never altered by a constructor; it requires the
explicit reconciliation command.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = "mnemos.knowledge_coverage_gap_store.v1"
SCHEMA_COMPONENT = "cognitive.knowledge_coverage_gap"
REGISTRY_TABLE = "mnemos_schema_registry"

REVISION_TABLE = "knowledge_coverage_gap_revisions"
HEAD_TABLE = "knowledge_coverage_gap_heads"
QUARANTINE_TABLE = "blindspot_legacy_quarantine"

REVISION_DDL = f"""
CREATE TABLE {REVISION_TABLE} (
    revision_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    topic TEXT NOT NULL,
    normalized_topic TEXT NOT NULL,
    dimension TEXT NOT NULL CHECK (dimension IN (
        'missing_topic', 'missing_form', 'domain_sparsity',
        'temporal_staleness', 'relation_sparsity',
        'unsolved_trail', 'unrecorded_trail'
    )),
    description TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    status TEXT NOT NULL CHECK (status IN (
        'detected', 'reminded', 'investigating', 'resolved',
        'mitigated', 'ignored', 'expired'
    )),
    detected_at TEXT NOT NULL,
    reminded_at TEXT NOT NULL DEFAULT '',
    last_reminded_at TEXT NOT NULL DEFAULT '',
    last_session_id TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    principal_id TEXT NOT NULL DEFAULT '',
    evidence_refs_json TEXT NOT NULL,
    resolution_condition TEXT NOT NULL,
    resolution_evidence_json TEXT NOT NULL DEFAULT '[]',
    resolved_at TEXT NOT NULL DEFAULT '',
    supersedes_revision_id TEXT,
    consumers_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(asset_id, revision_number),
    FOREIGN KEY (supersedes_revision_id) REFERENCES {REVISION_TABLE}(revision_id)
)
"""

HEAD_DDL = f"""
CREATE TABLE {HEAD_TABLE} (
    asset_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY (revision_id) REFERENCES {REVISION_TABLE}(revision_id)
)
"""

QUARANTINE_DDL = f"""
CREATE TABLE {QUARANTINE_TABLE} (
    legacy_row_hash TEXT PRIMARY KEY,
    source_table TEXT NOT NULL,
    source_primary_key TEXT NOT NULL,
    classification TEXT NOT NULL,
    reason TEXT NOT NULL,
    row_json TEXT NOT NULL,
    quarantined_at TEXT NOT NULL
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
    f"CREATE INDEX idx_knowledge_gap_current_topic ON {REVISION_TABLE}(scope_id, normalized_topic)",
    f"CREATE INDEX idx_knowledge_gap_status ON {REVISION_TABLE}(status, expires_at)",
)

TRIGGER_DDL = (
    f"""CREATE TRIGGER trg_{REVISION_TABLE}_immutable_update
        BEFORE UPDATE ON {REVISION_TABLE}
        BEGIN SELECT RAISE(ABORT, 'knowledge coverage gap revisions are immutable'); END""",
    f"""CREATE TRIGGER trg_{REVISION_TABLE}_immutable_delete
        BEFORE DELETE ON {REVISION_TABLE}
        BEGIN SELECT RAISE(ABORT, 'knowledge coverage gap revisions are immutable'); END""",
)


def _ddl_hash() -> str:
    payload = "\n".join(
        (
            REVISION_DDL.strip(),
            HEAD_DDL.strip(),
            QUARANTINE_DDL.strip(),
            *(item.strip() for item in INDEX_DDL),
            *(item.strip() for item in TRIGGER_DDL),
        )
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


CANONICAL_DDL_HASH = _ddl_hash()


class BlindspotAssetSchemaError(RuntimeError):
    """The blindspot database needs an explicit schema reconciliation."""


@dataclass(frozen=True)
class BlindspotAssetSchemaState:
    status: str
    classification: str
    schema_version: str
    ddl_hash: str
    row_count: int
    current_count: int
    quarantine_count: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.status == "ready" and not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "classification": self.classification,
            "schema_version": self.schema_version,
            "ddl_hash": self.ddl_hash,
            "canonical_ddl_hash": CANONICAL_DDL_HASH,
            "row_count": self.row_count,
            "current_count": self.current_count,
            "quarantine_count": self.quarantine_count,
            "errors": list(self.errors),
            "ok": self.ok,
        }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _registry(conn: sqlite3.Connection) -> tuple[str, str]:
    if not _table_exists(conn, REGISTRY_TABLE):
        return "", ""
    row = conn.execute(
        f"SELECT schema_version, ddl_hash FROM {REGISTRY_TABLE} WHERE component=?",  # nosec B608
        (SCHEMA_COMPONENT,),
    ).fetchone()
    return (str(row[0]), str(row[1])) if row else ("", "")


def _normalized_sql(value: str) -> str:
    return " ".join(str(value or "").strip().rstrip(";").split()).casefold()


def _object_sql(conn: sqlite3.Connection, object_type: str, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
        (object_type, name),
    ).fetchone()
    return str(row[0] or "") if row else ""


def inspect_blindspot_asset_schema(conn: sqlite3.Connection) -> BlindspotAssetSchemaState:
    has_revision = _table_exists(conn, REVISION_TABLE)
    has_legacy = _table_exists(conn, "blindspots")
    if not has_revision:
        classification = "legacy_topic_table" if has_legacy else "absent"
        return BlindspotAssetSchemaState(
            status="reconciliation_required" if has_legacy else "uninitialized",
            classification=classification,
            schema_version=SCHEMA_VERSION,
            ddl_hash="",
            row_count=0,
            current_count=0,
            quarantine_count=0,
            errors=(
                ("legacy blindspots table requires explicit reconciliation",) if has_legacy else ()
            ),
        )

    required_tables = {REVISION_TABLE, HEAD_TABLE, QUARANTINE_TABLE}
    missing = sorted(table for table in required_tables if not _table_exists(conn, table))
    registry_version, registry_hash = _registry(conn)
    errors: list[str] = []
    if missing:
        errors.append("missing canonical tables: " + ",".join(missing))
    if has_legacy:
        errors.append("legacy blindspots table remains active beside canonical schema")
    if registry_version != SCHEMA_VERSION or registry_hash != CANONICAL_DDL_HASH:
        errors.append("blindspot schema registry version/hash mismatch")
    expected_table_sql = {
        REVISION_TABLE: REVISION_DDL,
        HEAD_TABLE: HEAD_DDL,
        QUARANTINE_TABLE: QUARANTINE_DDL,
    }
    for table, expected_sql in expected_table_sql.items():
        if table in missing:
            continue
        if _normalized_sql(_object_sql(conn, "table", table)) != _normalized_sql(expected_sql):
            errors.append(f"canonical table DDL mismatch: {table}")
    expected_indexes = {
        "idx_knowledge_gap_current_topic": INDEX_DDL[0],
        "idx_knowledge_gap_status": INDEX_DDL[1],
    }
    for name, expected_sql in expected_indexes.items():
        if _normalized_sql(_object_sql(conn, "index", name)) != _normalized_sql(expected_sql):
            errors.append(f"canonical index missing or drifted: {name}")
    expected_triggers = {
        f"trg_{REVISION_TABLE}_immutable_update": TRIGGER_DDL[0],
        f"trg_{REVISION_TABLE}_immutable_delete": TRIGGER_DDL[1],
    }
    for name, expected_sql in expected_triggers.items():
        if _normalized_sql(_object_sql(conn, "trigger", name)) != _normalized_sql(expected_sql):
            errors.append(f"canonical immutability trigger missing or drifted: {name}")
    row_count = int(conn.execute(f"SELECT COUNT(*) FROM {REVISION_TABLE}").fetchone()[0])
    current_count = (
        int(conn.execute(f"SELECT COUNT(*) FROM {HEAD_TABLE}").fetchone()[0]) if not missing else 0
    )
    quarantine_count = (
        int(conn.execute(f"SELECT COUNT(*) FROM {QUARANTINE_TABLE}").fetchone()[0])
        if QUARANTINE_TABLE not in missing
        else 0
    )
    if not missing:
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            errors.append(f"blindspot schema foreign-key violations: {len(foreign_key_errors)}")
        orphan_heads = int(conn.execute(f"""SELECT COUNT(*) FROM {HEAD_TABLE} h
                    LEFT JOIN {REVISION_TABLE} r ON r.revision_id=h.revision_id
                    WHERE r.revision_id IS NULL""").fetchone()[0])  # nosec B608
        if orphan_heads:
            errors.append(f"orphan knowledge-gap heads: {orphan_heads}")
        noncurrent_heads = int(conn.execute(f"""SELECT COUNT(*) FROM {HEAD_TABLE} h
                    JOIN {REVISION_TABLE} r ON r.revision_id=h.revision_id
                    JOIN (
                        SELECT asset_id, MAX(revision_number) AS max_revision
                        FROM {REVISION_TABLE} GROUP BY asset_id
                    ) latest ON latest.asset_id=h.asset_id
                    WHERE r.asset_id != h.asset_id
                       OR r.revision_number != latest.max_revision""").fetchone()[0])  # nosec B608
        if noncurrent_heads:
            errors.append(f"non-current knowledge-gap heads: {noncurrent_heads}")
        missing_heads = int(conn.execute(f"""SELECT COUNT(*) FROM (
                        SELECT DISTINCT asset_id FROM {REVISION_TABLE}
                    ) r LEFT JOIN {HEAD_TABLE} h ON h.asset_id=r.asset_id
                    WHERE h.asset_id IS NULL""").fetchone()[0])  # nosec B608
        if missing_heads:
            errors.append(f"knowledge-gap assets without heads: {missing_heads}")
        invalid_json = int(conn.execute(f"""SELECT COUNT(*) FROM {REVISION_TABLE}
                    WHERE NOT json_valid(evidence_refs_json)
                       OR NOT json_valid(resolution_evidence_json)
                       OR NOT json_valid(consumers_json)""").fetchone()[0])  # nosec B608
        if invalid_json:
            errors.append(f"invalid knowledge-gap JSON fields: {invalid_json}")
        invalid_lineage = int(conn.execute(f"""SELECT COUNT(*) FROM {REVISION_TABLE} r
                    LEFT JOIN {REVISION_TABLE} parent
                      ON parent.revision_id=r.supersedes_revision_id
                    WHERE (r.revision_number=1 AND r.supersedes_revision_id IS NOT NULL)
                       OR (r.revision_number>1 AND (
                            parent.revision_id IS NULL
                            OR parent.asset_id != r.asset_id
                            OR parent.revision_number != r.revision_number - 1
                       ))""").fetchone()[0])  # nosec B608
        if invalid_lineage:
            errors.append(f"invalid knowledge-gap revision lineage: {invalid_lineage}")
    return BlindspotAssetSchemaState(
        status="ready" if not errors else "reconciliation_required",
        classification="canonical" if not errors else "unknown_or_drifted",
        schema_version=SCHEMA_VERSION,
        ddl_hash=registry_hash,
        row_count=row_count,
        current_count=current_count,
        quarantine_count=quarantine_count,
        errors=tuple(errors),
    )


def initialize_blindspot_asset_schema(db_path: Path) -> None:
    """Create a fresh canonical database; refuse to mutate an existing legacy DB."""

    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        state = inspect_blindspot_asset_schema(conn)
        if state.status == "ready":
            return
        if state.classification != "absent":
            raise BlindspotAssetSchemaError("explicit blindspot schema reconciliation is required")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            _install_schema(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _install_schema(conn: sqlite3.Connection) -> None:
    conn.execute(REVISION_DDL)
    conn.execute(HEAD_DDL)
    conn.execute(QUARANTINE_DDL)
    for statement in INDEX_DDL:
        conn.execute(statement)
    for statement in TRIGGER_DDL:
        conn.execute(statement)
    conn.execute(REGISTRY_DDL)
    conn.execute(
        f"INSERT INTO {REGISTRY_TABLE}(component, schema_version, ddl_hash, applied_at) "  # nosec B608
        "VALUES (?, ?, ?, ?)",
        (
            SCHEMA_COMPONENT,
            SCHEMA_VERSION,
            CANONICAL_DDL_HASH,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def _legacy_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "blindspots"):
        return []
    columns = [str(row[1]) for row in conn.execute("PRAGMA table_info(blindspots)")]
    if not columns or "topic" not in columns:
        raise BlindspotAssetSchemaError("unknown legacy blindspots schema")
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute("SELECT * FROM blindspots ORDER BY topic")]


def reconcile_blindspot_asset_schema(
    conn: sqlite3.Connection,
    *,
    apply: bool = False,
    failpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Plan or apply a lossless legacy quarantine with zero semantic promotion."""

    before = inspect_blindspot_asset_schema(conn)
    if before.ok:
        return {
            "before": before.as_dict(),
            "after": before.as_dict(),
            "legacy_row_count": 0,
            "quarantined_row_count": before.quarantine_count,
            "active_promotion_count": 0,
            "changed": False,
        }
    if before.classification == "absent":
        raise BlindspotAssetSchemaError("fresh stores must use explicit schema initialization")
    if before.classification != "legacy_topic_table":
        raise BlindspotAssetSchemaError("unknown blindspot schema; automatic migration is refused")

    legacy_rows = _legacy_rows(conn)
    plan: list[dict[str, str]] = []
    for row in legacy_rows:
        description = str(row.get("description") or "")
        classification = (
            "historical_unscoped_knowledge_gap"
            if description.startswith("知识库中缺少关于")
            else "historical_ambiguous_blindspot"
        )
        reason = "legacy row lacks scope, evidence, expiry, revision, and typed resolution proof"
        plan.append(
            {
                "legacy_row_hash": canonical_row_hash(row),
                "source_primary_key": str(row.get("topic") or ""),
                "classification": classification,
                "reason": reason,
                "row_json": json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )

    if apply:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            _install_schema(conn)
            if failpoint is not None:
                failpoint("after_schema_install")
            quarantined_at = datetime.now(timezone.utc).isoformat()
            for item in plan:
                conn.execute(
                    f"""INSERT INTO {QUARANTINE_TABLE} (
                        legacy_row_hash, source_table, source_primary_key,
                        classification, reason, row_json, quarantined_at
                    ) VALUES (?, 'blindspots', ?, ?, ?, ?, ?)""",  # nosec B608
                    (
                        item["legacy_row_hash"],
                        item["source_primary_key"],
                        item["classification"],
                        item["reason"],
                        item["row_json"],
                        quarantined_at,
                    ),
                )
            if failpoint is not None:
                failpoint("after_quarantine_copy")
            conn.execute("ALTER TABLE blindspots RENAME TO blindspots_legacy_v0")
            if failpoint is not None:
                failpoint("after_legacy_rename")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    after = inspect_blindspot_asset_schema(conn) if apply else before
    return {
        "before": before.as_dict(),
        "after": after.as_dict(),
        "legacy_row_count": len(legacy_rows),
        "quarantined_row_count": len(plan) if apply else 0,
        "planned_quarantine_count": len(plan),
        "active_promotion_count": 0,
        "conservation_ok": len(plan) == len(legacy_rows),
        "classification_counts": {
            classification: sum(1 for item in plan if item["classification"] == classification)
            for classification in sorted({item["classification"] for item in plan})
        },
        "changed": apply,
    }


def read_blindspot_schema_status(db_path: Path) -> BlindspotAssetSchemaState:
    """Inspect without creating the database, WAL, SHM, tables, or directories."""

    path = Path(db_path).expanduser()
    if not path.exists():
        return BlindspotAssetSchemaState(
            status="uninitialized",
            classification="absent",
            schema_version=SCHEMA_VERSION,
            ddl_hash="",
            row_count=0,
            current_count=0,
            quarantine_count=0,
            errors=(),
        )
    with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as conn:
        return inspect_blindspot_asset_schema(conn)


def canonical_row_hash(row: dict[str, Any]) -> str:
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "BlindspotAssetSchemaError",
    "BlindspotAssetSchemaState",
    "CANONICAL_DDL_HASH",
    "HEAD_TABLE",
    "QUARANTINE_TABLE",
    "REVISION_TABLE",
    "SCHEMA_VERSION",
    "canonical_row_hash",
    "initialize_blindspot_asset_schema",
    "inspect_blindspot_asset_schema",
    "read_blindspot_schema_status",
    "reconcile_blindspot_asset_schema",
]
