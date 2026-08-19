"""Contracts for object-level cognitive-action target-state reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Sequence

from core.hephaestus.cognitive_action_targets import (
    TARGET_STATE_HASH_CONTRACT_VERSION,
)
from core.hephaestus.distill_action_store import canonical_json, sha256_json, stable_id


RECONCILIATION_SCHEMA_VERSION = (
    "mnemos.cognitive_action_target_state_reconciliation.v2"
)
MIGRATION_ID = "database.cognitive_action_target_state.v3"
RECONCILIATION_BATCH_TABLE = "cognitive_action_target_state_reconciliation_batches"
RECONCILIATION_TABLE = "cognitive_action_target_state_reconciliations"
RECONCILIATION_TRIGGER_NAMES = frozenset(
    {
        "trg_cognitive_action_state_reconciliation_no_update",
        "trg_cognitive_action_state_reconciliation_no_delete",
        "trg_cognitive_action_state_reconcile_batch_no_update",
        "trg_cognitive_action_state_reconcile_batch_no_delete",
    }
)
RECONCILIATION_BATCH_COLUMNS = frozenset(
    {
        "batch_id",
        "schema_version",
        "migration_contract_hash",
        "state_contract_version",
        "inventory_hash",
        "object_manifest_hash",
        "object_count",
        "inventory_manifest_json",
        "object_manifest_json",
        "applied_at",
    }
)
RECONCILIATION_COLUMNS = frozenset(
    {
        "reconciliation_id",
        "batch_id",
        "effect_id",
        "cognitive_action_id",
        "action",
        "target",
        "target_object_id",
        "recorded_contract_version",
        "recorded_after_hash",
        "artifact_hash",
        "command_hash",
        "effect_hash",
        "original_receipt_hash",
        "target_row_hash",
        "expected_state_hash",
        "current_state_hash",
        "state_contract_version",
        "migration_contract_hash",
        "inventory_hash",
        "object_manifest_hash",
        "applied_at",
    }
)

MIGRATION_CONTRACT = {
    "schema_version": RECONCILIATION_SCHEMA_VERSION,
    "migration_id": MIGRATION_ID,
    "target_state_contract": TARGET_STATE_HASH_CONTRACT_VERSION,
    "source_bindings": [
        "immutable_action_command",
        "immutable_action_artifact",
        "immutable_action_effect",
        "immutable_target_receipt",
        "current_target_object",
    ],
    "write_policy": "append_only_target_owned_receipt",
    "snapshot_policy": (
        "migration_time_full_row_snapshot_plus_current_action_owned_state"
    ),
}
MIGRATION_CONTRACT_HASH = sha256_json(MIGRATION_CONTRACT)

RECONCILIATION_BATCH_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {RECONCILIATION_BATCH_TABLE} (
    batch_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    migration_contract_hash TEXT NOT NULL,
    state_contract_version TEXT NOT NULL,
    inventory_hash TEXT NOT NULL,
    object_manifest_hash TEXT NOT NULL,
    object_count INTEGER NOT NULL CHECK(object_count >= 1),
    inventory_manifest_json TEXT NOT NULL CHECK(json_valid(inventory_manifest_json)),
    object_manifest_json TEXT NOT NULL CHECK(json_valid(object_manifest_json)),
    applied_at TEXT NOT NULL
);
"""

RECONCILIATION_OBJECT_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {RECONCILIATION_TABLE} (
    reconciliation_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    effect_id TEXT NOT NULL UNIQUE,
    cognitive_action_id TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    target TEXT NOT NULL CHECK(target='observation_store'),
    target_object_id TEXT NOT NULL UNIQUE,
    recorded_contract_version TEXT NOT NULL,
    recorded_after_hash TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    command_hash TEXT NOT NULL,
    effect_hash TEXT NOT NULL,
    original_receipt_hash TEXT NOT NULL,
    target_row_hash TEXT NOT NULL,
    expected_state_hash TEXT NOT NULL,
    current_state_hash TEXT NOT NULL,
    state_contract_version TEXT NOT NULL,
    migration_contract_hash TEXT NOT NULL,
    inventory_hash TEXT NOT NULL,
    object_manifest_hash TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    FOREIGN KEY(batch_id) REFERENCES {RECONCILIATION_BATCH_TABLE}(batch_id)
);
"""

RECONCILIATION_TRIGGER_SQL = {
    "trg_cognitive_action_state_reconciliation_no_update": f"""
        CREATE TRIGGER IF NOT EXISTS trg_cognitive_action_state_reconciliation_no_update
        BEFORE UPDATE ON {RECONCILIATION_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'cognitive action target-state reconciliation is immutable');
        END;
    """,
    "trg_cognitive_action_state_reconciliation_no_delete": f"""
        CREATE TRIGGER IF NOT EXISTS trg_cognitive_action_state_reconciliation_no_delete
        BEFORE DELETE ON {RECONCILIATION_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'cognitive action target-state reconciliation is immutable');
        END;
    """,
    "trg_cognitive_action_state_reconcile_batch_no_update": f"""
        CREATE TRIGGER IF NOT EXISTS trg_cognitive_action_state_reconcile_batch_no_update
        BEFORE UPDATE ON {RECONCILIATION_BATCH_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'cognitive action target-state batch is immutable');
        END;
    """,
    "trg_cognitive_action_state_reconcile_batch_no_delete": f"""
        CREATE TRIGGER IF NOT EXISTS trg_cognitive_action_state_reconcile_batch_no_delete
        BEFORE DELETE ON {RECONCILIATION_BATCH_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'cognitive action target-state batch is immutable');
        END;
    """,
}
RECONCILIATION_SCHEMA_SQL = "\n".join(
    (
        RECONCILIATION_BATCH_SCHEMA_SQL,
        RECONCILIATION_OBJECT_SCHEMA_SQL,
        *(RECONCILIATION_TRIGGER_SQL[name] for name in sorted(RECONCILIATION_TRIGGER_SQL)),
    )
)


@dataclass(frozen=True)
class CognitiveActionStateReconciliationPaths:
    """Exact action and Observation stores participating in reconciliation."""

    database_dir: Path

    @property
    def action_path(self) -> Path:
        return self.database_dir / "distill_actions.db"

    @property
    def observations_path(self) -> Path:
        return self.database_dir / "observations.db"


@dataclass(frozen=True)
class CognitiveActionStateCandidate:
    """One exact recorded receipt/current object pair eligible for reconciliation."""

    reconciliation_id: str
    effect_id: str
    cognitive_action_id: str
    action: str
    target: str
    target_object_id: str
    recorded_contract_version: str
    recorded_after_hash: str
    artifact_hash: str
    command_hash: str
    effect_hash: str
    original_receipt_hash: str
    target_row_hash: str
    expected_state_hash: str
    current_state_hash: str

    def manifest(self) -> dict[str, str]:
        """Return the complete immutable per-object reconciliation manifest."""

        return {
            "reconciliation_id": self.reconciliation_id,
            "effect_id": self.effect_id,
            "cognitive_action_id": self.cognitive_action_id,
            "action": self.action,
            "target": self.target,
            "target_object_id": self.target_object_id,
            "recorded_contract_version": self.recorded_contract_version,
            "recorded_after_hash": self.recorded_after_hash,
            "artifact_hash": self.artifact_hash,
            "command_hash": self.command_hash,
            "effect_hash": self.effect_hash,
            "original_receipt_hash": self.original_receipt_hash,
            "target_row_hash": self.target_row_hash,
            "expected_state_hash": self.expected_state_hash,
            "current_state_hash": self.current_state_hash,
            "state_contract_version": TARGET_STATE_HASH_CONTRACT_VERSION,
            "migration_contract_hash": MIGRATION_CONTRACT_HASH,
        }


@dataclass(frozen=True)
class CognitiveActionStateReconciliationPlan:
    """Read-only reviewed inventory and exact eligible object manifest."""

    paths: CognitiveActionStateReconciliationPaths
    candidates: tuple[CognitiveActionStateCandidate, ...]
    current: tuple[Mapping[str, str], ...]
    blocked: tuple[Mapping[str, str], ...]
    inventory_entries: tuple[Mapping[str, str], ...]
    inventory_manifest_json: str
    inventory_hash: str
    object_manifest_hash: str

    @property
    def ok(self) -> bool:
        return not self.blocked

    @property
    def requires_apply(self) -> bool:
        return bool(self.candidates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "ok": self.ok,
            "status": (
                "blocked"
                if not self.ok
                else "reconciliation_required"
                if self.requires_apply
                else "clean"
            ),
            "migration_contract_hash": MIGRATION_CONTRACT_HASH,
            "state_contract_version": TARGET_STATE_HASH_CONTRACT_VERSION,
            "inventory_hash": self.inventory_hash,
            "object_manifest_hash": self.object_manifest_hash,
            "counts": {
                "candidate": len(self.candidates),
                "current": len(self.current),
                "blocked": len(self.blocked),
                "inventory": len(self.inventory_entries),
            },
            "candidate_effect_ids": [value.effect_id for value in self.candidates],
            "blocked": [dict(value) for value in self.blocked],
            "paths": {
                "database_dir": str(self.paths.database_dir),
                "action_db": str(self.paths.action_path),
                "target_db": str(self.paths.observations_path),
            },
        }


def make_reconciliation_id(effect_id: str, recorded_after_hash: str) -> str:
    """Derive the immutable object receipt id from its recorded effect hash."""

    return str(
        stable_id(
            "cog_action_state_reconciliation",
            effect_id,
            recorded_after_hash,
            TARGET_STATE_HASH_CONTRACT_VERSION,
            size=24,
        )
    )


def make_batch_id(inventory_hash: str, object_manifest_hash: str) -> str:
    """Derive the immutable reconciliation batch id from reviewed manifests."""

    return str(
        stable_id(
            "cog_action_state_batch",
            inventory_hash,
            object_manifest_hash,
            size=24,
        )
    )


def finalize_plan_hashes(
    *,
    inventory_entries: Sequence[Mapping[str, Any]],
    candidate_manifests: Sequence[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
    blocked: Sequence[Mapping[str, Any]],
) -> tuple[str, str, str]:
    """Return canonical inventory payload and both independently reviewable hashes."""

    object_manifest_hash = sha256_json(list(candidate_manifests))
    payload = {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "migration_contract_hash": MIGRATION_CONTRACT_HASH,
        "state_contract_version": TARGET_STATE_HASH_CONTRACT_VERSION,
        "counts": {
            "inventory": len(inventory_entries),
            "candidates": len(candidate_manifests),
            "current": len(current),
            "blocked": len(blocked),
        },
        "inventory": list(inventory_entries),
        "candidates": list(candidate_manifests),
        "current": list(current),
        "blocked": list(blocked),
    }
    inventory_manifest_json = canonical_json(payload)
    inventory_hash = sha256_json(payload)
    return inventory_manifest_json, inventory_hash, object_manifest_hash


def validated_inventory_manifest(
    manifest_json: str,
    *,
    inventory_hash: str,
    object_manifest_hash: str,
) -> dict[str, Any] | None:
    """Decode and recompute every hash binding in one stored inventory manifest."""

    try:
        payload = json.loads(manifest_json)
    except (json.JSONDecodeError, TypeError):
        return None
    required = {
        "schema_version",
        "migration_contract_hash",
        "state_contract_version",
        "counts",
        "inventory",
        "candidates",
        "current",
        "blocked",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        return None
    if canonical_json(payload) != manifest_json:
        return None
    if (
        payload.get("schema_version") != RECONCILIATION_SCHEMA_VERSION
        or payload.get("migration_contract_hash") != MIGRATION_CONTRACT_HASH
        or payload.get("state_contract_version")
        != TARGET_STATE_HASH_CONTRACT_VERSION
    ):
        return None
    collections = ("inventory", "candidates", "current", "blocked")
    if any(not isinstance(payload.get(key), list) for key in collections):
        return None
    counts = payload.get("counts")
    if not isinstance(counts, dict) or set(counts) != set(collections):
        return None
    if any(counts.get(key) != len(payload[key]) for key in collections):
        return None
    if sha256_json(payload) != inventory_hash:
        return None
    if sha256_json(payload["candidates"]) != object_manifest_hash:
        return None
    return payload


def _column_signature(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, str, int, int], ...]:
    if table not in {RECONCILIATION_BATCH_TABLE, RECONCILIATION_TABLE}:
        raise ValueError("unsupported reconciliation table")
    return tuple(
        (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in connection.execute(f"PRAGMA table_info({table})")  # nosec B608
    )


def _unique_columns(
    connection: sqlite3.Connection,
    table: str,
) -> frozenset[tuple[str, ...]]:
    if table not in {RECONCILIATION_BATCH_TABLE, RECONCILIATION_TABLE}:
        raise ValueError("unsupported reconciliation table")
    indexes = connection.execute(f"PRAGMA index_list({table})").fetchall()  # nosec B608
    result: set[tuple[str, ...]] = set()
    for index in indexes:
        if not bool(index[2]):
            continue
        name = str(index[1])
        if re.fullmatch(r"[A-Za-z0-9_]+", name) is None:
            return frozenset()
        columns = tuple(
            str(row[2])
            for row in connection.execute(f"PRAGMA index_info('{name}')")  # nosec B608
        )
        result.add(columns)
    return frozenset(result)


def _normalized_sql(value: str) -> str:
    normalized = re.sub(r"\s+", "", value).lower()
    normalized = normalized.replace("ifnotexists", "")
    return normalized.rstrip(";")


def reconciliation_schema_is_valid(connection: sqlite3.Connection) -> bool:
    """Validate the exact append-only reciprocal schema without changing it."""

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if not {RECONCILIATION_BATCH_TABLE, RECONCILIATION_TABLE} <= tables:
        return False
    batch_columns = _column_signature(connection, RECONCILIATION_BATCH_TABLE)
    object_columns = _column_signature(connection, RECONCILIATION_TABLE)
    expected_batch_columns = tuple(
        (
            name,
            "INTEGER" if name == "object_count" else "TEXT",
            0 if name == "batch_id" else 1,
            1 if name == "batch_id" else 0,
        )
        for name in (
            "batch_id",
            "schema_version",
            "migration_contract_hash",
            "state_contract_version",
            "inventory_hash",
            "object_manifest_hash",
            "object_count",
            "inventory_manifest_json",
            "object_manifest_json",
            "applied_at",
        )
    )
    expected_object_columns = tuple(
        (
            name,
            "TEXT",
            0 if name == "reconciliation_id" else 1,
            1 if name == "reconciliation_id" else 0,
        )
        for name in (
            "reconciliation_id",
            "batch_id",
            "effect_id",
            "cognitive_action_id",
            "action",
            "target",
            "target_object_id",
            "recorded_contract_version",
            "recorded_after_hash",
            "artifact_hash",
            "command_hash",
            "effect_hash",
            "original_receipt_hash",
            "target_row_hash",
            "expected_state_hash",
            "current_state_hash",
            "state_contract_version",
            "migration_contract_hash",
            "inventory_hash",
            "object_manifest_hash",
            "applied_at",
        )
    )
    if (
        batch_columns != expected_batch_columns
        or object_columns != expected_object_columns
    ):
        return False
    if _unique_columns(connection, RECONCILIATION_BATCH_TABLE) != frozenset(
        {("batch_id",)}
    ):
        return False
    if _unique_columns(connection, RECONCILIATION_TABLE) != frozenset(
        {
            ("reconciliation_id",),
            ("effect_id",),
            ("cognitive_action_id",),
            ("target_object_id",),
        }
    ):
        return False
    table_sql = {
        str(row[0]): _normalized_sql(str(row[1] or ""))
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table'"
        )
        if str(row[0]) in {RECONCILIATION_BATCH_TABLE, RECONCILIATION_TABLE}
    }
    expected_table_sql = {
        RECONCILIATION_BATCH_TABLE: _normalized_sql(
            RECONCILIATION_BATCH_SCHEMA_SQL
        ),
        RECONCILIATION_TABLE: _normalized_sql(RECONCILIATION_OBJECT_SCHEMA_SQL),
    }
    if table_sql != expected_table_sql:
        return False
    triggers = {
        str(row[0]): (str(row[1]), _normalized_sql(str(row[2] or "")))
        for row in connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger'"
        )
        if str(row[0]) in RECONCILIATION_TRIGGER_NAMES
    }
    if set(triggers) != RECONCILIATION_TRIGGER_NAMES:
        return False
    trigger_tables = {
        "trg_cognitive_action_state_reconciliation_no_update": RECONCILIATION_TABLE,
        "trg_cognitive_action_state_reconciliation_no_delete": RECONCILIATION_TABLE,
        "trg_cognitive_action_state_reconcile_batch_no_update": (
            RECONCILIATION_BATCH_TABLE
        ),
        "trg_cognitive_action_state_reconcile_batch_no_delete": (
            RECONCILIATION_BATCH_TABLE
        ),
    }
    for name, table in trigger_tables.items():
        actual_table, actual_sql = triggers[name]
        if actual_table != table or actual_sql != _normalized_sql(
            RECONCILIATION_TRIGGER_SQL[name]
        ):
            return False
    foreign_keys = connection.execute(
        f"PRAGMA foreign_key_list({RECONCILIATION_TABLE})"  # nosec B608
    ).fetchall()
    foreign_key_signature = tuple(
        (
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]).upper(),
            str(row[6]).upper(),
            str(row[7]).upper(),
        )
        for row in foreign_keys
    )
    return foreign_key_signature == (
        (
            RECONCILIATION_BATCH_TABLE,
            "batch_id",
            "batch_id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
    )


__all__ = [
    "CognitiveActionStateCandidate",
    "CognitiveActionStateReconciliationPaths",
    "CognitiveActionStateReconciliationPlan",
    "MIGRATION_CONTRACT_HASH",
    "MIGRATION_ID",
    "RECONCILIATION_BATCH_TABLE",
    "RECONCILIATION_SCHEMA_SQL",
    "RECONCILIATION_SCHEMA_VERSION",
    "RECONCILIATION_TABLE",
    "finalize_plan_hashes",
    "make_batch_id",
    "make_reconciliation_id",
    "reconciliation_schema_is_valid",
    "validated_inventory_manifest",
]
