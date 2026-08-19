"""Object-level Scheme A migration for pre-COG-048 training assets."""

from __future__ import annotations

import base64
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Sequence

from core.cognitive.state_contract import canonical_json, sha256_json
from core.cognitive.state_schema import (
    CANONICAL_DDL_HASH as COGNITIVE_STATE_DDL_HASH,
    STATE_SCHEMA_VERSION,
    inspect_cognitive_state_schema,
    reconcile_cognitive_state_schema,
)
from core.cognitive.training_governance_static_audit import (
    audit_retired_training_surfaces,
)
from core.cognitive.training_migration_barrier import (
    activate_training_migration_barrier,
    assert_training_governance_enabled,
    deactivate_training_migration_barrier,
)
from core.migrations.model_call_ledger_reconcile.runtime import (
    mnemos_runtime_is_active,
)
from core.scoring.training_schema import (
    CANONICAL_DDL_HASH as TRAINING_DDL_HASH,
    OWNED_TABLES,
    TRAINING_SCHEMA_VERSION,
    initialize_training_schema,
    inspect_training_schema,
)
from core.utils import load_json_value


TRAINING_HISTORY_SCHEMA_VERSION = "mnemos.training_history_inventory.v1"
TRAINING_HISTORY_REASON_CODE = "historical_unverified_training_asset"
TRAINING_ACTIVATION_REASON_CODE = "training_governance_activation"
TRAINING_ACTIVATION_SOURCE_KEY = "training-governance-activation:v1"
TRAINING_HISTORY_SPEC_HASH = sha256_json(
    {
        "schema_version": TRAINING_HISTORY_SCHEMA_VERSION,
        "semantic_policy": "object_level_quarantine_without_promotion",
        "source_database_classes": [
            "scoring",
            "rule_weight_optimizer",
            "rule_weights",
        ],
        "target_database_class": "cognitive_state_target",
    }
)


@dataclass(frozen=True)
class TrainingSourceDatabase:
    """One exact historical database class included in Scheme A inventory."""

    database_class: str
    path: Path


@dataclass(frozen=True)
class HistoricalTrainingObject:
    """Sensitive-byte-free identity and hashes for one historical row."""

    database_class: str
    table: str
    primary_key: tuple[tuple[str, Any], ...]
    schema_fingerprint: str
    field_manifest: tuple[str, ...]
    row_hash: str
    activation_state: str

    @property
    def primary_key_hash(self) -> str:
        """Return the canonical hash of the exact primary-key mapping."""

        return str(sha256_json(dict(self.primary_key)))

    @property
    def source_key(self) -> str:
        """Return the stable quarantine key for this source object."""

        identity = {
            "schema_version": TRAINING_HISTORY_SCHEMA_VERSION,
            "database_class": self.database_class,
            "table": self.table,
            "primary_key": dict(self.primary_key),
            "schema_fingerprint": self.schema_fingerprint,
        }
        suffix = str(sha256_json(identity)).split(":", 1)[1][:40]
        return "training-history:" + suffix

    def public_manifest(self) -> dict[str, Any]:
        """Return only non-sensitive identity and integrity evidence."""

        return {
            "database_class": self.database_class,
            "table": self.table,
            "primary_key_hash": self.primary_key_hash,
            "schema_fingerprint": self.schema_fingerprint,
            "field_manifest_hash": sha256_json(list(self.field_manifest)),
            "row_hash": self.row_hash,
            "activation_state": self.activation_state,
        }

    def quarantine_payload(
        self,
        *,
        prior_feedback_quarantine_refs: Sequence[str],
    ) -> dict[str, Any]:
        """Build an immutable quarantine payload linked to prior COG-038 rows."""

        return {
            "schema_version": "mnemos.historical_unverified_training_asset.v1",
            "source_identity": {
                "database_class": self.database_class,
                "table": self.table,
                "primary_key": dict(self.primary_key),
                "primary_key_hash": self.primary_key_hash,
                "schema_fingerprint": self.schema_fingerprint,
            },
            "row_hash": self.row_hash,
            "field_manifest": list(self.field_manifest),
            "activation_state": self.activation_state,
            "prior_feedback_quarantine_refs": list(sorted(set(prior_feedback_quarantine_refs))),
            "semantic_state": TRAINING_HISTORY_REASON_CODE,
            "prediction_created": False,
            "objective_outcome_created": False,
            "training_admission_created": False,
            "dataset_split_created": False,
            "training_run_created": False,
            "model_head_created": False,
            "bayesian_prior_activated": False,
            "optimizer_weight_activated": False,
        }


_TABLE_SPECS: dict[
    str,
    dict[str, tuple[tuple[str, ...], tuple[str, ...]]],
] = {
    "scoring": {
        "ground_truth_signals": (
            ("id",),
            (
                "id",
                "profile_id",
                "session_id",
                "signal_type",
                "signal_value",
                "confidence",
                "latency_hours",
                "created_at",
            ),
        ),
        "scorer_training_queue": (
            ("id",),
            (
                "id",
                "session_id",
                "dimension",
                "features_json",
                "priority",
                "earliest_train_at",
                "status",
                "retry_count",
                "created_at",
                "updated_at",
            ),
        ),
        "scorer_feedback_events": (
            ("feedback_event_id",),
            ("feedback_event_id", "session_id", "dimension", "created_at"),
        ),
        "scorer_models": (
            ("id",),
            (
                "id",
                "dimension",
                "model_version",
                "model_type",
                "model_blob",
                "model_hash",
                "train_samples",
                "is_active",
                "created_at",
                "meta_json",
            ),
        ),
        "bayesian_scorer_state": (
            ("dimension",),
            (
                "dimension",
                "alpha",
                "beta",
                "prior_alpha",
                "prior_beta",
                "total_samples",
                "neg_likelihood",
                "last_updated",
                "updated_at",
            ),
        ),
        "bayesian_feedback": (
            ("id",),
            ("id", "dimension", "is_positive", "weight", "context_json", "created_at"),
        ),
    },
    "rule_weight_optimizer": {
        "rule_outcomes": (
            ("id",),
            (
                "id",
                "rule_name",
                "predicted_score",
                "actual_label",
                "created_at",
                "source_event_id",
            ),
        ),
        "optimize_log": (
            ("id",),
            ("id", "rule_name", "triggered_at", "source_event_id"),
        ),
        "weight_history": (
            ("id",),
            (
                "id",
                "rule_name",
                "old_weight",
                "new_weight",
                "accuracy",
                "sample_count",
                "optimized_at",
            ),
        ),
    },
    "rule_weights": {
        "rule_weights": (
            ("rule_name",),
            ("rule_name", "weight", "updated_at"),
        ),
        "layer5_dimension_weights": (
            ("dimension",),
            (
                "dimension",
                "weight",
                "positive_count",
                "negative_count",
                "updated_at",
            ),
        ),
    },
}

_JSON_FIELDS = frozenset({"features_json", "meta_json", "context_json"})
_SQL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def training_source_databases(
    database_dir: Path,
) -> tuple[TrainingSourceDatabase, ...]:
    """Return the three authoritative historical training source databases."""

    root = Path(database_dir).expanduser()
    return (
        TrainingSourceDatabase("scoring", root / "mnemos.db"),
        TrainingSourceDatabase(
            "rule_weight_optimizer",
            root / "rule_weight_optimizer.db",
        ),
        TrainingSourceDatabase("rule_weights", root / "rule_weights.db"),
    )


def build_training_history_inventory(
    database_dir: Path,
    *,
    connections: Mapping[str, sqlite3.Connection] | None = None,
) -> dict[str, Any]:
    """Inventory every whitelisted historical row with deterministic hashes."""

    objects: list[HistoricalTrainingObject] = []
    schemas: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    for source in training_source_databases(database_dir):
        if not source.path.is_file():
            missing.append(source.database_class)
            continue
        supplied = connections.get(source.database_class) if connections else None
        if supplied is not None:
            selected, fingerprints = _inventory_source(source, supplied)
        else:
            with _connect(source.path, read_only=True) as conn:
                selected, fingerprints = _inventory_source(source, conn)
        objects.extend(selected)
        schemas[source.database_class] = fingerprints
    ordered = tuple(
        sorted(
            objects,
            key=lambda item: (
                item.database_class,
                item.table,
                canonical_json(dict(item.primary_key)),
            ),
        )
    )
    public = [item.public_manifest() for item in ordered]
    object_manifest_hash = sha256_json(public)
    counts_by_table = Counter(f"{item.database_class}.{item.table}" for item in ordered)
    active_legacy_model_count = sum(
        1
        for item in ordered
        if item.database_class == "scoring"
        and item.table == "scorer_models"
        and item.activation_state == "active"
    )
    material = {
        "schema_version": TRAINING_HISTORY_SCHEMA_VERSION,
        "spec_hash": TRAINING_HISTORY_SPEC_HASH,
        "object_manifest_hash": object_manifest_hash,
        "object_count": len(ordered),
        "counts_by_table": dict(sorted(counts_by_table.items())),
        "schema_fingerprints": schemas,
        "missing_database_classes": sorted(missing),
        "active_legacy_model_count": active_legacy_model_count,
    }
    return {
        **material,
        "inventory_hash": sha256_json(material),
        "objects": ordered,
        "sensitive_bytes_in_report": 0,
    }


def public_training_inventory_report(
    inventory: Mapping[str, Any],
    *,
    target_db: Path,
) -> dict[str, Any]:
    """Return a sensitive-byte-free dry-run report and live coverage delta."""

    coverage = inspect_training_history_coverage(target_db, inventory)
    return {
        "schema_version": TRAINING_HISTORY_SCHEMA_VERSION,
        "status": "dry_run",
        "inventory_hash": inventory["inventory_hash"],
        "object_manifest_hash": inventory["object_manifest_hash"],
        "object_count": inventory["object_count"],
        "counts_by_table": inventory["counts_by_table"],
        "schema_fingerprints": inventory["schema_fingerprints"],
        "missing_database_classes": inventory["missing_database_classes"],
        "active_legacy_model_count": inventory["active_legacy_model_count"],
        "coverage": coverage,
        "sensitive_bytes_in_report": 0,
        "apply_required": (coverage["uncovered"] > 0 or not coverage["activation_marker_valid"]),
    }


def inspect_training_history_coverage(
    target_db: Path,
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare the exact inventory with canonical quarantine and activation rows."""

    objects = tuple(inventory.get("objects") or ())
    if not Path(target_db).is_file():
        return {
            "covered": 0,
            "uncovered": len(objects),
            "unexpected": 0,
            "invalid": 0,
            "prior_feedback_links": 0,
            "uncovered_by_table": dict(
                sorted(Counter(f"{item.database_class}.{item.table}" for item in objects).items())
            ),
            "activation_marker_present": False,
            "activation_marker_valid": False,
        }
    with _connect(Path(target_db), read_only=True) as conn:
        return _coverage_in_connection(conn, objects, inventory)


def reconcile_training_history(
    *,
    database_dir: Path,
    expected_inventory_hash: str,
    expected_object_manifest_hash: str,
    backup_dir: Path,
    repo_root: Path,
) -> dict[str, Any]:
    """Back up and quarantine an exact inventory under an exclusive barrier."""

    root = Path(database_dir).expanduser()
    target_db = root / "producer_consumer_ledger.db"
    if _runtime_is_active():
        raise RuntimeError(
            "mnemos daemon and MCP services must be inactive before training history apply"
        )
    _require_database_set(root)
    static = audit_retired_training_surfaces(repo_root)
    if any(static.values()):
        raise RuntimeError("legacy training production readers or writers remain")
    barrier = activate_training_migration_barrier(
        root,
        inventory_hash=str(expected_inventory_hash),
    )
    backup_manifest: Path | None = None
    release_barrier = True
    try:
        try:
            assert_training_governance_enabled(root)
        except RuntimeError as exc:
            if str(exc) != "training_governance_migration_in_progress":
                raise
        else:
            raise RuntimeError("training migration barrier did not block runtime")

        connections = _lock_databases(root)
        try:
            inventory = build_training_history_inventory(
                root,
                connections=connections,
            )
            _require_expected_inventory(
                inventory,
                expected_inventory_hash=expected_inventory_hash,
                expected_object_manifest_hash=expected_object_manifest_hash,
            )
            if inventory["missing_database_classes"]:
                raise RuntimeError("training history source database is missing")
            if int(inventory["active_legacy_model_count"]) != 0:
                raise RuntimeError("active legacy scorer model remains")
            for database_class, conn in connections.items():
                if _connection_integrity(conn) != "ok":
                    raise RuntimeError(f"training history integrity failed: {database_class}")
            backup_manifest = _backup_databases(
                root,
                backup_dir=backup_dir,
                inventory=inventory,
                connections=connections,
            )
        finally:
            _release_locks(connections)

        state_report = _reconcile_canonical_state(target_db)
        training_report = _initialize_training_projection(root / "mnemos.db")
        clean_state = _governed_state_counts(target_db, root / "mnemos.db")
        if any(clean_state.values()):
            raise RuntimeError("governed training pre-activation state is not empty")
        with _connect(target_db) as target:
            prior_index = _prior_feedback_index(target)
            effect = _append_quarantines(
                target,
                tuple(inventory["objects"]),
                prior_index=prior_index,
            )
            marker = _activation_payload(inventory)
            marker_effect = _append_activation_marker(target, marker)
            coverage = _coverage_in_connection(
                target,
                tuple(inventory["objects"]),
                inventory,
            )
            if (
                coverage["uncovered"]
                or coverage["unexpected"]
                or coverage["invalid"]
                or not coverage["activation_marker_valid"]
            ):
                raise RuntimeError("training history coverage verification failed")
            target.commit()
        replay_inventory = build_training_history_inventory(root)
        _require_expected_inventory(
            replay_inventory,
            expected_inventory_hash=expected_inventory_hash,
            expected_object_manifest_hash=expected_object_manifest_hash,
        )
        return {
            "schema_version": "mnemos.training_history_reconciliation.v1",
            "status": "applied",
            "inventory_hash": inventory["inventory_hash"],
            "object_manifest_hash": inventory["object_manifest_hash"],
            "object_count": inventory["object_count"],
            "counts_by_table": inventory["counts_by_table"],
            "effect": effect,
            "activation_marker": marker_effect,
            "coverage": coverage,
            "state_schema": state_report,
            "training_schema": training_report,
            "governed_state_counts": clean_state,
            "backup_manifest": str(backup_manifest),
            "barrier_verified": True,
            "static_audit": static,
        }
    except BaseException:
        if backup_manifest is not None:
            try:
                _restore_under_active_barrier(
                    root,
                    backup_manifest,
                    expected_inventory_hash=str(expected_inventory_hash),
                    expected_object_manifest_hash=str(expected_object_manifest_hash),
                )
            except BaseException as rollback_error:
                release_barrier = False
                raise RuntimeError(
                    "training history apply failed and automatic rollback failed; "
                    "migration barrier remains active"
                ) from rollback_error
        raise
    finally:
        if release_barrier:
            deactivate_training_migration_barrier(root, owner_id=barrier.owner_id)


def restore_training_history(
    *,
    database_dir: Path,
    restore_manifest: Path,
) -> dict[str, Any]:
    """Restore every database class from one fully validated backup manifest."""

    root = Path(database_dir).expanduser()
    if _runtime_is_active():
        raise RuntimeError(
            "mnemos daemon and MCP services must be inactive before training history restore"
        )
    manifest_path = Path(restore_manifest).expanduser().resolve(strict=True)
    manifest = load_json_value(manifest_path)
    if not isinstance(manifest, Mapping):
        raise ValueError("training history restore manifest is invalid")
    if manifest.get("schema_version") != "mnemos.training_history_backup_manifest.v1":
        raise ValueError("training history restore manifest schema mismatch")
    core = dict(manifest)
    supplied_hash = core.pop("manifest_hash", "")
    if supplied_hash != sha256_json(core):
        raise ValueError("training history restore manifest hash mismatch")
    entries = _validate_restore_manifest(root, manifest)
    barrier = activate_training_migration_barrier(
        root,
        inventory_hash=str(manifest["inventory_hash"]),
    )
    recovery_manifest: Path | None = None
    recovery_inventory: Mapping[str, Any] | None = None
    release_barrier = True
    try:
        connections = _lock_databases(root)
        try:
            recovery_inventory = build_training_history_inventory(
                root,
                connections=connections,
            )
            _require_expected_inventory(
                recovery_inventory,
                expected_inventory_hash=str(manifest["inventory_hash"]),
                expected_object_manifest_hash=str(manifest["object_manifest_hash"]),
            )
            recovery_manifest = _backup_databases(
                root,
                backup_dir=(manifest_path.parent / f"restore-recovery-{barrier.owner_id}"),
                inventory=recovery_inventory,
                connections=connections,
            )
        finally:
            _release_locks(connections)

        restored: dict[str, str] = {}
        for database_class in sorted(entries):
            entry = entries[database_class]
            result = _restore_database(
                Path(str(entry["backup_path"])),
                Path(str(entry["source_path"])),
                expected_logical_hash=str(entry["backup_logical_hash"]),
            )
            restored[database_class] = result
        inventory = build_training_history_inventory(root)
        _require_expected_inventory(
            inventory,
            expected_inventory_hash=str(manifest["inventory_hash"]),
            expected_object_manifest_hash=str(manifest["object_manifest_hash"]),
        )
        return {
            "schema_version": "mnemos.training_history_restore.v1",
            "status": "restored",
            "inventory_hash": inventory["inventory_hash"],
            "object_manifest_hash": inventory["object_manifest_hash"],
            "validated_backup_count": len(entries),
            "restored_logical_hashes": restored,
            "recovery_manifest": str(recovery_manifest),
        }
    except BaseException:
        if recovery_manifest is not None and recovery_inventory is not None:
            try:
                _restore_under_active_barrier(
                    root,
                    recovery_manifest,
                    expected_inventory_hash=str(recovery_inventory["inventory_hash"]),
                    expected_object_manifest_hash=str(recovery_inventory["object_manifest_hash"]),
                )
            except BaseException as rollback_error:
                release_barrier = False
                raise RuntimeError(
                    "training history restore failed and automatic rollback failed; "
                    f"recovery_manifest={recovery_manifest}; migration barrier remains active"
                ) from rollback_error
        raise
    finally:
        if release_barrier:
            deactivate_training_migration_barrier(root, owner_id=barrier.owner_id)


def _inventory_source(
    source: TrainingSourceDatabase,
    conn: sqlite3.Connection,
) -> tuple[list[HistoricalTrainingObject], dict[str, str]]:
    select_sql = {
        "scoring": {
            "ground_truth_signals": "SELECT * FROM ground_truth_signals ORDER BY id",
            "scorer_training_queue": "SELECT * FROM scorer_training_queue ORDER BY id",
            "scorer_feedback_events": (
                "SELECT * FROM scorer_feedback_events ORDER BY feedback_event_id"
            ),
            "scorer_models": "SELECT * FROM scorer_models ORDER BY id",
            "bayesian_scorer_state": ("SELECT * FROM bayesian_scorer_state ORDER BY dimension"),
            "bayesian_feedback": "SELECT * FROM bayesian_feedback ORDER BY id",
        },
        "rule_weight_optimizer": {
            "rule_outcomes": "SELECT * FROM rule_outcomes ORDER BY id",
            "optimize_log": "SELECT * FROM optimize_log ORDER BY id",
            "weight_history": "SELECT * FROM weight_history ORDER BY id",
        },
        "rule_weights": {
            "rule_weights": "SELECT * FROM rule_weights ORDER BY rule_name",
            "layer5_dimension_weights": (
                "SELECT * FROM layer5_dimension_weights ORDER BY dimension"
            ),
        },
    }
    conn.row_factory = sqlite3.Row
    objects: list[HistoricalTrainingObject] = []
    fingerprints: dict[str, str] = {}
    for table, (primary_key, expected_columns) in _TABLE_SPECS[source.database_class].items():
        if not _table_exists(conn, table):
            continue
        allowed_tables = frozenset(_TABLE_SPECS[source.database_class])
        columns = _columns(conn, table, allowed_tables=allowed_tables)
        names = tuple(str(item[1]) for item in columns)
        if names != expected_columns:
            raise RuntimeError(f"unknown training history schema: {source.database_class}.{table}")
        actual_primary = tuple(
            str(item[1])
            for item in sorted(columns, key=lambda value: int(value[5]))
            if int(item[5]) > 0
        )
        if actual_primary != primary_key:
            raise RuntimeError(
                f"training history primary key mismatch: {source.database_class}.{table}"
            )
        fingerprint = _schema_fingerprint(conn, table, columns)
        fingerprints[table] = fingerprint
        if set(select_sql[source.database_class]) != set(allowed_tables):
            raise RuntimeError("training history inventory query registry drift")
        rows = conn.execute(select_sql[source.database_class][table]).fetchall()
        for row in rows:
            normalized = {name: _normalize_sql_value(row[name]) for name in expected_columns}
            _validate_json_fields(
                source.database_class,
                table,
                normalized,
            )
            objects.append(
                HistoricalTrainingObject(
                    database_class=source.database_class,
                    table=table,
                    primary_key=tuple((name, normalized[name]) for name in primary_key),
                    schema_fingerprint=fingerprint,
                    field_manifest=expected_columns,
                    row_hash=str(sha256_json(normalized)),
                    activation_state=_activation_state(table, normalized),
                )
            )
    return objects, fingerprints


def _activation_state(table: str, row: Mapping[str, Any]) -> str:
    if table == "scorer_models":
        return "active" if int(row.get("is_active") or 0) else "inactive"
    if table == "scorer_training_queue":
        return "queue:" + str(row.get("status") or "unknown")
    if table in {"rule_weights", "layer5_dimension_weights", "bayesian_scorer_state"}:
        return "persisted_aggregate"
    if table in {"rule_outcomes", "optimize_log", "weight_history"}:
        return "optimizer_history"
    return "historical"


def _validate_json_fields(
    database_class: str,
    table: str,
    row: Mapping[str, Any],
) -> None:
    for field in _JSON_FIELDS & set(row):
        value = row.get(field)
        if value in {None, ""}:
            continue
        if not isinstance(value, str):
            raise RuntimeError(f"malformed training history JSON: {database_class}.{table}.{field}")
        try:
            json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"malformed training history JSON: {database_class}.{table}.{field}"
            ) from exc


def _coverage_in_connection(
    conn: sqlite3.Connection,
    objects: Sequence[HistoricalTrainingObject],
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    expected = {item.source_key: item for item in objects}
    if not _table_exists(conn, "cognitive_state_migration_quarantine"):
        return {
            "covered": 0,
            "uncovered": len(expected),
            "unexpected": 0,
            "invalid": 0,
            "prior_feedback_links": 0,
            "uncovered_by_table": dict(
                sorted(
                    Counter(
                        f"{item.database_class}.{item.table}" for item in expected.values()
                    ).items()
                )
            ),
            "activation_marker_present": False,
            "activation_marker_valid": False,
        }
    prior_index = _prior_feedback_index(conn)
    rows = conn.execute(
        """
        SELECT source_key, payload_json, payload_hash
        FROM cognitive_state_migration_quarantine
        WHERE reason_code=?
        """,
        (TRAINING_HISTORY_REASON_CODE,),
    ).fetchall()
    actual = {str(row["source_key"]): row for row in rows}
    covered = 0
    invalid = 0
    prior_links = 0
    covered_keys: set[str] = set()
    for source_key, item in expected.items():
        refs = prior_index.get(_source_identity_key(item), ())
        payload = item.quarantine_payload(
            prior_feedback_quarantine_refs=refs,
        )
        row = actual.get(source_key)
        if (
            row is not None
            and str(row["payload_hash"]) == sha256_json(payload)
            and _json_value(str(row["payload_json"])) == payload
        ):
            covered += 1
            covered_keys.add(source_key)
            prior_links += len(refs)
        elif row is not None:
            invalid += 1
    marker = _activation_payload(inventory)
    marker_row = conn.execute(
        """
        SELECT payload_json, payload_hash
        FROM cognitive_state_migration_quarantine
        WHERE source_key=? AND reason_code=?
        """,
        (TRAINING_ACTIVATION_SOURCE_KEY, TRAINING_ACTIVATION_REASON_CODE),
    ).fetchone()
    marker_valid = bool(
        marker_row is not None
        and str(marker_row["payload_hash"]) == sha256_json(marker)
        and _json_value(str(marker_row["payload_json"])) == marker
    )
    uncovered_by_table = Counter(
        f"{item.database_class}.{item.table}"
        for source_key, item in expected.items()
        if source_key not in covered_keys
    )
    return {
        "covered": covered,
        "uncovered": len(expected) - covered,
        "unexpected": len(set(actual) - set(expected)),
        "invalid": invalid,
        "prior_feedback_links": prior_links,
        "uncovered_by_table": dict(sorted(uncovered_by_table.items())),
        "activation_marker_present": marker_row is not None,
        "activation_marker_valid": marker_valid,
    }


def _append_quarantines(
    conn: sqlite3.Connection,
    objects: Sequence[HistoricalTrainingObject],
    *,
    prior_index: Mapping[str, Sequence[str]],
) -> dict[str, int]:
    inserted = 0
    existing = 0
    for item in objects:
        payload = item.quarantine_payload(
            prior_feedback_quarantine_refs=prior_index.get(
                _source_identity_key(item),
                (),
            ),
        )
        payload_json = canonical_json(payload)
        payload_hash = str(sha256_json(payload))
        field_manifest = canonical_json(list(item.field_manifest))
        quarantine_id = "training-history-quarantine-" + item.source_key.split(":", 1)[1]
        row = conn.execute(
            """
            SELECT source_table, field_manifest, payload_json, payload_hash
            FROM cognitive_state_migration_quarantine
            WHERE source_key=? AND reason_code=?
            """,
            (item.source_key, TRAINING_HISTORY_REASON_CODE),
        ).fetchone()
        source_table = f"training_history.{item.database_class}.{item.table}"
        if row is not None:
            if tuple(str(value) for value in row) != (
                source_table,
                field_manifest,
                payload_json,
                payload_hash,
            ):
                raise RuntimeError("immutable training quarantine conflict")
            existing += 1
            continue
        conn.execute(
            """
            INSERT INTO cognitive_state_migration_quarantine (
                quarantine_id, source_table, source_key, reason_code,
                field_manifest, payload_json, payload_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                quarantine_id,
                source_table,
                item.source_key,
                TRAINING_HISTORY_REASON_CODE,
                field_manifest,
                payload_json,
                payload_hash,
                _now(),
            ),
        )
        inserted += 1
    return {"inserted": inserted, "existing": existing}


def _activation_payload(inventory: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "mnemos.training_governance_activation.v1",
        "inventory_hash": inventory["inventory_hash"],
        "object_manifest_hash": inventory["object_manifest_hash"],
        "object_count": inventory["object_count"],
        "quarantine_reason_code": TRAINING_HISTORY_REASON_CODE,
        "cognitive_state_schema_version": STATE_SCHEMA_VERSION,
        "cognitive_state_ddl_hash": COGNITIVE_STATE_DDL_HASH,
        "training_schema_version": TRAINING_SCHEMA_VERSION,
        "training_ddl_hash": TRAINING_DDL_HASH,
        "active_legacy_model_count": inventory["active_legacy_model_count"],
        "legacy_assets_promoted": False,
        "active_admission_created": False,
        "active_model_head_created": False,
        "legacy_bayesian_state_loaded": False,
        "legacy_rule_weight_loaded": False,
    }


def _append_activation_marker(
    conn: sqlite3.Connection,
    payload: Mapping[str, Any],
) -> dict[str, int]:
    payload_json = canonical_json(dict(payload))
    payload_hash = str(sha256_json(dict(payload)))
    row = conn.execute(
        """
        SELECT source_table, field_manifest, payload_json, payload_hash
        FROM cognitive_state_migration_quarantine
        WHERE source_key=? AND reason_code=?
        """,
        (TRAINING_ACTIVATION_SOURCE_KEY, TRAINING_ACTIVATION_REASON_CODE),
    ).fetchone()
    expected = (
        "training_history.activation",
        "[]",
        payload_json,
        payload_hash,
    )
    if row is not None:
        if tuple(str(value) for value in row) != expected:
            raise RuntimeError("immutable training activation marker conflict")
        return {"inserted": 0, "existing": 1}
    suffix = payload_hash.split(":", 1)[1][:40]
    conn.execute(
        """
        INSERT INTO cognitive_state_migration_quarantine (
            quarantine_id, source_table, source_key, reason_code,
            field_manifest, payload_json, payload_hash, created_at
        ) VALUES (?, ?, ?, ?, '[]', ?, ?, ?)
        """,
        (
            "training-governance-activation-" + suffix,
            expected[0],
            TRAINING_ACTIVATION_SOURCE_KEY,
            TRAINING_ACTIVATION_REASON_CODE,
            payload_json,
            payload_hash,
            _now(),
        ),
    )
    return {"inserted": 1, "existing": 0}


def _prior_feedback_index(
    conn: sqlite3.Connection,
) -> dict[str, tuple[str, ...]]:
    if not _table_exists(conn, "cognitive_state_migration_quarantine"):
        return {}
    index: dict[str, set[str]] = {}
    rows = conn.execute(
        """
        SELECT quarantine_id, payload_json, payload_hash
        FROM cognitive_state_migration_quarantine
        WHERE reason_code='historical_unattributed_feedback'
        """
    ).fetchall()
    for row in rows:
        payload = _json_value(str(row["payload_json"]))
        if not isinstance(payload, Mapping):
            continue
        identity = payload.get("source_identity")
        if not isinstance(identity, Mapping):
            continue
        database_class = str(identity.get("database_class") or "")
        table = str(identity.get("table") or "")
        primary_key = identity.get("primary_key")
        if not database_class or not table or not isinstance(primary_key, Mapping):
            continue
        key = _identity_key(database_class, table, primary_key)
        reference = (
            "cognitive_state_migration_quarantine:"
            + str(row["quarantine_id"])
            + ":"
            + str(row["payload_hash"])
        )
        index.setdefault(key, set()).add(reference)
    return {key: tuple(sorted(values)) for key, values in index.items()}


def _source_identity_key(item: HistoricalTrainingObject) -> str:
    return _identity_key(
        item.database_class,
        item.table,
        dict(item.primary_key),
    )


def _identity_key(
    database_class: str,
    table: str,
    primary_key: Mapping[str, Any],
) -> str:
    return str(
        sha256_json(
            {
                "database_class": database_class,
                "table": table,
                "primary_key": dict(primary_key),
            }
        )
    )


def _reconcile_canonical_state(target_db: Path) -> dict[str, Any]:
    with _connect(target_db) as conn:
        report = reconcile_cognitive_state_schema(conn, apply=True)
        state = inspect_cognitive_state_schema(conn)
        if not state.ok:
            raise RuntimeError("cognitive state v4 reconciliation failed")
        if _connection_integrity(conn) != "ok":
            raise RuntimeError("cognitive state integrity failed after reconciliation")
        conn.commit()
        return {
            "classification": state.classification,
            "schema_version": state.schema_version,
            "ddl_hash": state.ddl_hash,
            "action": report["action"],
        }


def _initialize_training_projection(scoring_db: Path) -> dict[str, Any]:
    with _connect(scoring_db) as conn:
        initialize_training_schema(conn)
        state = inspect_training_schema(conn)
        if not state.ok:
            raise RuntimeError("governed training projection initialization failed")
        if _connection_integrity(conn) != "ok":
            raise RuntimeError("scoring integrity failed after training schema initialization")
        conn.commit()
        return {
            "classification": state.classification,
            "schema_version": state.schema_version,
            "ddl_hash": state.ddl_hash,
        }


def _governed_state_counts(
    target_db: Path,
    scoring_db: Path,
) -> dict[str, int]:
    with _connect(target_db, read_only=True) as target:
        revisions = int(
            target.execute(
                """
                SELECT COUNT(*) FROM cognitive_state_revisions
                WHERE object_type IN ('training_admission_record','training_run_record')
                """
            ).fetchone()[0]
        )
        heads = int(
            target.execute(
                """
                SELECT COUNT(*) FROM cognitive_state_heads
                WHERE object_type IN ('training_admission_record','training_run_record')
                """
            ).fetchone()[0]
        )
    with _connect(scoring_db, read_only=True) as scoring:
        allowed_tables = frozenset(OWNED_TABLES)
        projection_rows = sum(
            int(
                scoring.execute(
                    "SELECT COUNT(*) FROM "  # nosec B608
                    + _quote_sql_identifier(table, allowed_tables)
                ).fetchone()[0]
            )
            for table in OWNED_TABLES
        )
    return {
        "training_revisions": revisions,
        "training_heads": heads,
        "training_projection_rows": projection_rows,
    }


def _require_database_set(root: Path) -> None:
    required = {source.database_class: source.path for source in training_source_databases(root)}
    required["cognitive_state_target"] = root / "producer_consumer_ledger.db"
    missing = [database_class for database_class, path in required.items() if not path.is_file()]
    if missing:
        raise RuntimeError(
            "training history required databases missing: " + ",".join(sorted(missing))
        )


def _database_map(root: Path) -> dict[str, Path]:
    result = {source.database_class: source.path for source in training_source_databases(root)}
    result["cognitive_state_target"] = root / "producer_consumer_ledger.db"
    return result


def _lock_databases(root: Path) -> dict[str, sqlite3.Connection]:
    connections: dict[str, sqlite3.Connection] = {}
    try:
        for database_class, path in sorted(
            _database_map(root).items(),
            key=lambda item: str(item[1]),
        ):
            conn = _connect(path)
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
            connections[database_class] = conn
        return connections
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        _release_locks(connections)
        raise


def _release_locks(connections: Mapping[str, sqlite3.Connection]) -> None:
    for conn in reversed(list(connections.values())):
        try:
            conn.rollback()
        finally:
            conn.close()


def _backup_databases(
    root: Path,
    *,
    backup_dir: Path,
    inventory: Mapping[str, Any],
    connections: Mapping[str, sqlite3.Connection],
) -> Path:
    destination = Path(backup_dir).expanduser().resolve(strict=False)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    suffix = str(inventory["inventory_hash"]).split(":", 1)[1][:16]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    entries: list[dict[str, Any]] = []
    for database_class, source in sorted(_database_map(root).items()):
        source_conn = connections[database_class]
        source_logical_hash = _connection_logical_hash(source_conn)
        backup = destination / f"{database_class}.{suffix}.{stamp}.db"
        _sqlite_backup(source, backup)
        if _integrity(backup) != "ok":
            raise RuntimeError(f"training history backup integrity failed: {database_class}")
        backup_logical_hash = _database_logical_hash(backup)
        if backup_logical_hash != source_logical_hash:
            raise RuntimeError(f"training history backup snapshot mismatch: {database_class}")
        entries.append(
            {
                "database_class": database_class,
                "source_path": str(source.resolve(strict=True)),
                "backup_path": str(backup),
                "file_hash": _file_hash(backup),
                "integrity": "ok",
                "source_logical_hash": source_logical_hash,
                "backup_logical_hash": backup_logical_hash,
            }
        )
    manifest: dict[str, Any] = {
        "schema_version": "mnemos.training_history_backup_manifest.v1",
        "inventory_hash": inventory["inventory_hash"],
        "object_manifest_hash": inventory["object_manifest_hash"],
        "object_count": inventory["object_count"],
        "database_classes": sorted(item["database_class"] for item in entries),
        "backups": entries,
        "created_at": _now(),
    }
    manifest["manifest_hash"] = sha256_json(manifest)
    path = destination / f"training-history-manifest.{suffix}.{stamp}.json"
    _write_private_file(
        path,
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    return path


def _validate_restore_manifest(
    root: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    expected = _database_map(root)
    declared = manifest.get("database_classes")
    backups = manifest.get("backups")
    if not isinstance(declared, list) or not isinstance(backups, list):
        raise ValueError("training history restore manifest is incomplete")
    entries: dict[str, Mapping[str, Any]] = {}
    for item in backups:
        if not isinstance(item, Mapping):
            raise ValueError("training history backup entry is invalid")
        database_class = str(item.get("database_class") or "")
        if database_class in entries or database_class not in expected:
            raise ValueError("training history backup class is invalid")
        entries[database_class] = item
    if set(entries) != set(expected) or sorted(entries) != sorted(str(value) for value in declared):
        raise ValueError("training history backup class set is incomplete")
    for database_class, item in entries.items():
        source = Path(str(item.get("source_path") or ""))
        backup = Path(str(item.get("backup_path") or ""))
        if source.resolve(strict=False) != expected[database_class].resolve(strict=False):
            raise ValueError("training history backup source path mismatch")
        if not backup.is_file() or _file_hash(backup) != item.get("file_hash"):
            raise ValueError(f"training history backup hash mismatch: {database_class}")
        if _integrity(backup) != "ok" or item.get("integrity") != "ok":
            raise ValueError(f"training history backup integrity mismatch: {database_class}")
        logical_hash = _database_logical_hash(backup)
        if logical_hash != item.get("backup_logical_hash") or logical_hash != item.get(
            "source_logical_hash"
        ):
            raise ValueError(f"training history backup logical hash mismatch: {database_class}")
    return entries


def _restore_under_active_barrier(
    root: Path,
    manifest_path: Path,
    *,
    expected_inventory_hash: str,
    expected_object_manifest_hash: str,
) -> dict[str, str]:
    manifest = load_json_value(Path(manifest_path).resolve(strict=True))
    if not isinstance(manifest, Mapping):
        raise ValueError("training history rollback manifest is invalid")
    core = dict(manifest)
    supplied_hash = core.pop("manifest_hash", "")
    if manifest.get(
        "schema_version"
    ) != "mnemos.training_history_backup_manifest.v1" or supplied_hash != sha256_json(core):
        raise ValueError("training history rollback manifest proof mismatch")
    entries = _validate_restore_manifest(root, manifest)
    restored: dict[str, str] = {}
    for database_class in sorted(entries):
        entry = entries[database_class]
        restored[database_class] = _restore_database(
            Path(str(entry["backup_path"])),
            Path(str(entry["source_path"])),
            expected_logical_hash=str(entry["backup_logical_hash"]),
        )
    inventory = build_training_history_inventory(root)
    _require_expected_inventory(
        inventory,
        expected_inventory_hash=expected_inventory_hash,
        expected_object_manifest_hash=expected_object_manifest_hash,
    )
    return restored


def _restore_database(
    backup: Path,
    destination: Path,
    *,
    expected_logical_hash: str,
) -> str:
    with _connect(backup, read_only=True) as source, _connect(destination) as target:
        source.backup(target)
        target.commit()
        if _connection_integrity(target) != "ok":
            raise RuntimeError("training history restored database integrity failed")
    logical_hash = _database_logical_hash(destination)
    if logical_hash != expected_logical_hash:
        raise RuntimeError("training history restored database hash mismatch")
    return logical_hash


def _require_expected_inventory(
    inventory: Mapping[str, Any],
    *,
    expected_inventory_hash: str,
    expected_object_manifest_hash: str,
) -> None:
    if str(inventory["inventory_hash"]) != str(expected_inventory_hash):
        raise RuntimeError("training history inventory hash drift")
    if str(inventory["object_manifest_hash"]) != str(expected_object_manifest_hash):
        raise RuntimeError("training history object manifest hash drift")


def _runtime_is_active() -> bool:
    return mnemos_runtime_is_active()


def _database_logical_hash(path: Path) -> str:
    with _connect(path, read_only=True) as conn:
        return _connection_logical_hash(conn)


def _connection_logical_hash(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    tables = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
    ]
    allowed_tables = frozenset(tables)
    for table in tables:
        quoted_table = _quote_sql_identifier(table, allowed_tables)
        columns = _columns(conn, table, allowed_tables=allowed_tables)
        schema_rows = conn.execute(
            """
            SELECT type, name, sql FROM sqlite_master
            WHERE tbl_name=? AND type IN ('table','index','trigger')
            ORDER BY type, name
            """,
            (table,),
        ).fetchall()
        digest.update(
            canonical_json(
                {
                    "table": table,
                    "columns": [list(item) for item in columns],
                    "schema": [list(item) for item in schema_rows],
                }
            ).encode("utf-8")
        )
        primary = [
            str(item[1])
            for item in sorted(columns, key=lambda value: int(value[5]))
            if int(item[5]) > 0
        ]
        order = primary or [str(item[1]) for item in columns]
        query = f"SELECT * FROM {quoted_table}"  # nosec B608
        if order:
            allowed_columns = frozenset(str(item[1]) for item in columns)
            query += " ORDER BY " + ", ".join(
                _quote_sql_identifier(name, allowed_columns) for name in order
            )
        for row in conn.execute(query):
            payload = {
                str(item[1]): _normalize_sql_value(row[index]) for index, item in enumerate(columns)
            }
            digest.update(canonical_json(payload).encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def _schema_fingerprint(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[tuple[Any, ...]],
) -> str:
    rows = conn.execute(
        """
        SELECT type, name, sql FROM sqlite_master
        WHERE tbl_name=? AND type IN ('table','index','trigger')
        ORDER BY type, name
        """,
        (table,),
    ).fetchall()
    return str(
        sha256_json(
            {
                "table": table,
                "columns": [list(item) for item in columns],
                "schema": [list(item) for item in rows],
            }
        )
    )


def _columns(
    conn: sqlite3.Connection,
    table: str,
    *,
    allowed_tables: frozenset[str],
) -> tuple[tuple[Any, ...], ...]:
    quoted_table = _quote_sql_identifier(table, allowed_tables)
    return tuple(tuple(row) for row in conn.execute(f"PRAGMA table_info({quoted_table})"))


def _quote_sql_identifier(identifier: str, allowed: frozenset[str]) -> str:
    normalized = str(identifier)
    if normalized not in allowed or _SQL_IDENTIFIER.fullmatch(normalized) is None:
        raise RuntimeError("unapproved training history SQL identifier")
    return f'"{normalized}"'


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _normalize_sql_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"blob_base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (str, int, float)) or value is None:
        return value
    return str(value)


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(
            f"file:{Path(path).resolve(strict=True)}?mode=ro",
            uri=True,
        )
    else:
        conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        destination,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    os.close(descriptor)
    with _connect(source, read_only=True) as source_conn, _connect(destination) as target_conn:
        source_conn.backup(target_conn)


def _connection_integrity(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return str(row[0] if row else "")


def _integrity(path: Path) -> str:
    with _connect(path, read_only=True) as conn:
        return _connection_integrity(conn)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _write_private_file(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def _json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
