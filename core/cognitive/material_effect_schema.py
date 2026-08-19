"""Canonical schema authority for target-local material-effect journals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from core.db_utils import render_sql


SCHEMA_COMPONENT = "material_target_effects"
SCHEMA_VERSION = "mnemos.material_target_effect_schema.v1"
ROW_SCHEMA_VERSION = "mnemos.target_material_effect.v1"
REGISTRY_TABLE = "mnemos_material_effect_schema_registry"
TABLE_NAME = "material_target_effects"
FAMILY_INDEX = "idx_material_target_effect_family"


def configured_material_effect_databases(config: Any) -> tuple[Path, ...]:
    """Resolve the canonical target databases that own material-effect rows."""

    database_dir = Path(config.database_dir).expanduser()
    get_value = getattr(config, "get", None)
    policy_value = (
        get_value("policy_patch.db_path", None)
        if callable(get_value)
        else None
    )
    policy_db = (
        Path(policy_value).expanduser()
        if policy_value
        else database_dir / "policy_patches.db"
    )
    cognitive_graph = Path(
        getattr(config, "cognitive_graph_db_path", None)
        or database_dir / "cognitive_graph.db"
    ).expanduser()
    return tuple(
        dict.fromkeys(
            path.resolve(strict=False)
            for path in (
                policy_db,
                database_dir / "user_signals.db",
                cognitive_graph,
                database_dir / "knowledge_graph.db",
            )
        )
    )


TABLE_DDL = f"""
CREATE TABLE {TABLE_NAME} (
    command_id TEXT PRIMARY KEY,
    effect_id TEXT NOT NULL UNIQUE,
    decision_revision_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    executor_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    before_hash TEXT NOT NULL,
    after_hash TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    reason_code TEXT NOT NULL DEFAULT '',
    retry_exhausted INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    schema_version TEXT NOT NULL
)
"""
INDEX_DDL = f"""
CREATE INDEX {FAMILY_INDEX}
ON {TABLE_NAME}(owner, executor_id, action_type)
"""
REGISTRY_DDL = f"""
CREATE TABLE IF NOT EXISTS {REGISTRY_TABLE} (
    component TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    schema_hash TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


class MaterialEffectSchemaError(RuntimeError):
    """The target database requires explicit material-effect reconciliation."""


@dataclass(frozen=True)
class MaterialEffectSchemaState:
    """Read-only classification of one target database's effect journal."""

    classification: str
    schema_hash: str
    canonical_schema_hash: str
    registry_version: str
    registry_hash: str
    row_count: int
    migration_required: bool
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Return whether the live schema is canonical and migration-free."""

        return not self.migration_required and not self.errors

    def as_dict(self) -> dict[str, Any]:
        """Return a stable machine-readable schema inspection."""

        return {
            "classification": self.classification,
            "schema_version": SCHEMA_VERSION,
            "schema_hash": self.schema_hash,
            "canonical_schema_hash": self.canonical_schema_hash,
            "registry_version": self.registry_version,
            "registry_hash": self.registry_hash,
            "row_count": self.row_count,
            "migration_required": self.migration_required,
            "errors": list(self.errors),
            "ok": self.ok,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _table_signature(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    if table not in {TABLE_NAME, REGISTRY_TABLE}:
        raise MaterialEffectSchemaError(
            f"unsupported material-effect schema table: {table}"
        )
    columns = [
        {
            "cid": int(row[0]),
            "name": str(row[1]),
            "type": str(row[2]).upper(),
            "notnull": int(row[3]),
            "default": None if row[4] is None else str(row[4]),
            "pk": int(row[5]),
            "hidden": int(row[6]),
        }
        for row in conn.execute(
            "SELECT * FROM pragma_table_xinfo(?) ORDER BY cid",
            (table,),
        ).fetchall()
    ]
    indexes = []
    for row in conn.execute(
        "SELECT * FROM pragma_index_list(?) ORDER BY seq",
        (table,),
    ).fetchall():
        name = str(row[1])
        indexes.append(
            {
                "name": name if not name.startswith("sqlite_autoindex_") else "<auto>",
                "unique": int(row[2]),
                "origin": str(row[3]),
                "partial": int(row[4]),
                "columns": [
                    str(value[2])
                    for value in conn.execute(
                        "SELECT * FROM pragma_index_info(?) ORDER BY seqno",
                        (name,),
                    ).fetchall()
                ],
            }
        )
    indexes.sort(key=lambda value: (value["name"], value["columns"]))
    return {"columns": columns, "indexes": indexes}


def _target_signature(conn: sqlite3.Connection) -> dict[str, Any]:
    return _table_signature(conn, TABLE_NAME)


def _signature_hash(signature: dict[str, Any]) -> str:
    raw = json.dumps(
        signature,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _create_target_objects(conn: sqlite3.Connection) -> None:
    conn.execute(TABLE_DDL)
    conn.execute(INDEX_DDL)


def _canonical_signature_hash() -> str:
    with sqlite3.connect(":memory:") as conn:
        _create_target_objects(conn)
        return _signature_hash(_target_signature(conn))


CANONICAL_SCHEMA_HASH = _canonical_signature_hash()


def _canonical_registry_hash() -> str:
    with sqlite3.connect(":memory:") as conn:
        conn.execute(REGISTRY_DDL)
        return _signature_hash(_table_signature(conn, REGISTRY_TABLE))


CANONICAL_REGISTRY_HASH = _canonical_registry_hash()


def _registry_state(conn: sqlite3.Connection) -> tuple[str, str, str]:
    if not _table_exists(conn, REGISTRY_TABLE):
        return "", "", ""
    registry_hash = _signature_hash(_table_signature(conn, REGISTRY_TABLE))
    if registry_hash != CANONICAL_REGISTRY_HASH:
        return "", "", "unknown material-effect registry structure"
    row = conn.execute(
        f"SELECT schema_version, schema_hash FROM {REGISTRY_TABLE} "  # nosec B608
        "WHERE component=?",
        (SCHEMA_COMPONENT,),
    ).fetchone()
    return (
        (str(row[0]), str(row[1]), "")
        if row is not None
        else ("", "", "")
    )


def inspect_material_effect_schema(
    conn: sqlite3.Connection,
) -> MaterialEffectSchemaState:
    """Inspect the journal without creating tables or changing registry state."""

    registry_version, registry_hash, registry_error = _registry_state(conn)
    if not _table_exists(conn, TABLE_NAME):
        if registry_error or registry_version or registry_hash:
            return MaterialEffectSchemaState(
                "unknown",
                "",
                CANONICAL_SCHEMA_HASH,
                registry_version,
                registry_hash,
                0,
                True,
                (
                    registry_error
                    or "material-effect registry exists without its target journal",
                ),
            )
        return MaterialEffectSchemaState(
            "absent",
            "",
            CANONICAL_SCHEMA_HASH,
            "",
            "",
            0,
            True,
            (),
        )
    signature_hash = _signature_hash(_target_signature(conn))
    row_count = int(
        conn.execute(
            render_sql(
                "SELECT COUNT(*) FROM {table}",
                identifiers={"table": TABLE_NAME},
            )
        ).fetchone()[0]
    )
    errors = tuple(value for value in (registry_error,) if value)
    if signature_hash != CANONICAL_SCHEMA_HASH:
        return MaterialEffectSchemaState(
            "unknown",
            signature_hash,
            CANONICAL_SCHEMA_HASH,
            registry_version,
            registry_hash,
            row_count,
            True,
            errors or ("material-effect journal structure is not canonical",),
        )
    if (
        registry_version == SCHEMA_VERSION
        and registry_hash == CANONICAL_SCHEMA_HASH
        and not errors
    ):
        return MaterialEffectSchemaState(
            "canonical",
            signature_hash,
            CANONICAL_SCHEMA_HASH,
            registry_version,
            registry_hash,
            row_count,
            False,
            (),
        )
    return MaterialEffectSchemaState(
        "canonical_unregistered",
        signature_hash,
        CANONICAL_SCHEMA_HASH,
        registry_version,
        registry_hash,
        row_count,
        True,
        errors,
    )


def _domain_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name!=? ORDER BY name",
            (REGISTRY_TABLE,),
        ).fetchall()
    )


def _write_registry(conn: sqlite3.Connection) -> None:
    conn.execute(REGISTRY_DDL)
    conn.execute(
        f"""
        INSERT INTO {REGISTRY_TABLE} (
            component, schema_version, schema_hash, applied_at
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(component) DO UPDATE SET
            schema_version=excluded.schema_version,
            schema_hash=excluded.schema_hash,
            applied_at=excluded.applied_at
        """,  # nosec B608
        (SCHEMA_COMPONENT, SCHEMA_VERSION, CANONICAL_SCHEMA_HASH, _now()),
    )


def initialize_material_effect_schema(conn: sqlite3.Connection) -> None:
    """Initialize only an empty target DB; existing DBs must reconcile first."""

    state = inspect_material_effect_schema(conn)
    if state.classification == "canonical":
        return
    if state.classification != "absent" or _domain_tables(conn):
        raise MaterialEffectSchemaError(
            "material-effect schema migration required; run "
            "scripts/reconcile_material_effect_schema.py"
        )
    _create_target_objects(conn)
    _write_registry(conn)
    final = inspect_material_effect_schema(conn)
    if not final.ok:
        raise MaterialEffectSchemaError(
            "fresh material-effect schema initialization failed"
        )


def validate_material_effect_schema(conn: sqlite3.Connection) -> None:
    """Fail closed unless the exact schema and registry are already canonical."""

    state = inspect_material_effect_schema(conn)
    if not state.ok:
        raise MaterialEffectSchemaError(
            "material-effect schema migration required; run "
            "scripts/reconcile_material_effect_schema.py"
        )


def reconcile_material_effect_schema(
    conn: sqlite3.Connection,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Preview or apply an absent/unregistered canonical journal migration."""

    before = inspect_material_effect_schema(conn)
    if before.classification == "unknown":
        raise MaterialEffectSchemaError(
            "unknown material-effect schema cannot be migrated automatically"
        )
    required = before.classification != "canonical"
    if apply and required:
        if before.classification == "absent":
            _create_target_objects(conn)
        _write_registry(conn)
    after = inspect_material_effect_schema(conn) if apply else before
    if apply and not after.ok:
        raise MaterialEffectSchemaError(
            "post-migration material-effect schema verification failed"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "migration_required": required,
        "applied": bool(apply and required),
        "before": before.as_dict(),
        "after": after.as_dict(),
        "ok": after.ok if apply else before.classification != "unknown",
    }
