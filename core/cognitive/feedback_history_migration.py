"""Object-level inventory and quarantine migration for pre-COG-038 feedback."""

from __future__ import annotations

import base64
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from core.cognitive.feedback_migration_barrier import (
    activate_feedback_migration_barrier,
    assert_feedback_writes_enabled,
    deactivate_feedback_migration_barrier,
)
from core.cognitive.state_contract import sha256_json
from core.cognitive.state_schema import inspect_cognitive_state_schema
from core.migrations.model_call_ledger_reconcile.runtime import (
    mnemos_runtime_is_active,
)
from core.utils import load_json_value


FEEDBACK_HISTORY_SCHEMA_VERSION = "mnemos.feedback_history_inventory.v1"
FEEDBACK_HISTORY_REASON_CODE = "historical_unattributed_feedback"
FEEDBACK_HISTORY_SPEC_HASH = sha256_json(
    {
        "schema_version": FEEDBACK_HISTORY_SCHEMA_VERSION,
        "domains": [
            "delivery_feedback",
            "scoring_search",
            "reflection_optimizer",
        ],
        "semantic_policy": "quarantine_without_promotion",
    }
)


@dataclass(frozen=True)
class FeedbackSourceDatabase:
    """One fixed historical database in the feedback migration denominator."""

    database_class: str
    path: Path
    domain: str


@dataclass(frozen=True)
class HistoricalFeedbackObject:
    """Immutable object-level identity for one historical feedback row."""

    domain: str
    database_class: str
    table: str
    primary_key: tuple[tuple[str, Any], ...]
    schema_fingerprint: str
    field_manifest: tuple[str, ...]
    source_refs: tuple[str, ...]
    row_hash: str
    projection_links: tuple[str, ...] = ()

    @property
    def primary_key_hash(self) -> str:
        return str(sha256_json(dict(self.primary_key)))

    @property
    def source_key(self) -> str:
        identity = {
            "schema_version": FEEDBACK_HISTORY_SCHEMA_VERSION,
            "domain": self.domain,
            "database_class": self.database_class,
            "table": self.table,
            "primary_key": dict(self.primary_key),
            "schema_fingerprint": self.schema_fingerprint,
        }
        return "feedback-history:" + str(sha256_json(identity)).split(":", 1)[1][:40]

    def public_manifest(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "database_class": self.database_class,
            "table": self.table,
            "primary_key_hash": self.primary_key_hash,
            "schema_fingerprint": self.schema_fingerprint,
            "field_manifest_hash": sha256_json(list(self.field_manifest)),
            "row_hash": self.row_hash,
            "projection_link_hash": sha256_json(list(self.projection_links)),
            "projection_link_count": len(self.projection_links),
        }

    def quarantine_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "mnemos.historical_unattributed_feedback.v1",
            "source_identity": {
                "domain": self.domain,
                "database_class": self.database_class,
                "table": self.table,
                "primary_key": dict(self.primary_key),
                "primary_key_hash": self.primary_key_hash,
                "schema_fingerprint": self.schema_fingerprint,
            },
            "row_hash": self.row_hash,
            "field_manifest": list(self.field_manifest),
            "source_refs": list(self.source_refs),
            "projection_links": list(self.projection_links),
            "semantic_state": "historical_unattributed_feedback",
            "active_promotion": False,
            "reaction_created": False,
            "attribution_created": False,
            "objective_outcome_created": False,
            "target_command_created": False,
            "training_admitted": False,
        }


def feedback_source_databases(database_dir: Path) -> tuple[FeedbackSourceDatabase, ...]:
    """Return the fixed source database classes for COG-038 migration."""

    root = Path(database_dir).expanduser()
    return (
        FeedbackSourceDatabase("delivery_events", root / "delivery_events.db", "delivery_feedback"),
        FeedbackSourceDatabase("feedback_signals", root / "feedback_signals.db", "delivery_feedback"),
        FeedbackSourceDatabase("scoring", root / "mnemos.db", "scoring_search"),
        FeedbackSourceDatabase("reflections", root / "reflections.db", "reflection_optimizer"),
        FeedbackSourceDatabase(
            "rule_weight_optimizer",
            root / "rule_weight_optimizer.db",
            "reflection_optimizer",
        ),
    )


def build_feedback_history_inventory(
    database_dir: Path,
    *,
    connections: Mapping[str, sqlite3.Connection] | None = None,
) -> dict[str, Any]:
    """Build the deterministic object-level historical feedback inventory."""

    sources = feedback_source_databases(database_dir)
    objects: list[HistoricalFeedbackObject] = []
    schema_fingerprints: dict[str, dict[str, str]] = {}
    for source in sources:
        if not source.path.is_file():
            continue
        external = connections.get(source.database_class) if connections else None
        if external is not None:
            selected, schemas = _inventory_database(source, external)
        else:
            with _connect(source.path, read_only=True) as conn:
                selected, schemas = _inventory_database(source, conn)
        objects.extend(selected)
        schema_fingerprints[source.database_class] = schemas
    linked = _with_projection_links(objects)
    public = [item.public_manifest() for item in linked]
    object_manifest_hash = sha256_json(public)
    counts_by_domain = Counter(item.domain for item in linked)
    counts_by_table = Counter(
        f"{item.database_class}.{item.table}" for item in linked
    )
    inventory_material = {
        "schema_version": FEEDBACK_HISTORY_SCHEMA_VERSION,
        "spec_hash": FEEDBACK_HISTORY_SPEC_HASH,
        "object_manifest_hash": object_manifest_hash,
        "object_count": len(linked),
        "counts_by_domain": dict(sorted(counts_by_domain.items())),
        "counts_by_table": dict(sorted(counts_by_table.items())),
        "schema_fingerprints": schema_fingerprints,
    }
    return {
        **inventory_material,
        "inventory_hash": sha256_json(inventory_material),
        "objects": tuple(linked),
        "sensitive_bytes_in_report": 0,
    }


def inspect_feedback_history_coverage(
    target_db: Path,
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare quarantine rows with the exact historical inventory."""

    objects = tuple(inventory.get("objects") or ())
    if not Path(target_db).is_file():
        return {
            "covered": 0,
            "uncovered": len(objects),
            "unexpected": 0,
            "active_promotion": 0,
        }
    with _connect(Path(target_db), read_only=True) as conn:
        return _inspect_feedback_history_coverage_in_connection(conn, objects)


def _inspect_feedback_history_coverage_in_connection(
    conn: sqlite3.Connection,
    objects: Sequence[HistoricalFeedbackObject],
) -> dict[str, int]:
    expected = {item.source_key: item for item in objects}
    if not _table_exists(conn, "cognitive_state_migration_quarantine"):
        return {
            "covered": 0,
            "uncovered": len(objects),
            "unexpected": 0,
            "active_promotion": 0,
        }
    rows = conn.execute(
        """
        SELECT source_key, payload_json, payload_hash
        FROM cognitive_state_migration_quarantine
        WHERE reason_code=?
        """,
        (FEEDBACK_HISTORY_REASON_CODE,),
    ).fetchall()
    actual = {str(row["source_key"]): row for row in rows}
    active_promotion = _count_active_feedback_objects(conn)
    covered = 0
    for source_key, item in expected.items():
        row = actual.get(source_key)
        payload = item.quarantine_payload()
        if (
            row is not None
            and str(row["payload_hash"]) == sha256_json(payload)
            and _json_load(str(row["payload_json"])) == payload
        ):
            covered += 1
    return {
        "covered": covered,
        "uncovered": len(expected) - covered,
        "unexpected": len(set(actual) - set(expected)),
        "active_promotion": active_promotion,
    }


def reconcile_feedback_history(
    *,
    database_dir: Path,
    expected_inventory_hash: str,
    expected_object_manifest_hash: str,
    backup_dir: Path,
) -> dict[str, Any]:
    """Apply the fail-closed historical feedback quarantine migration."""

    root = Path(database_dir).expanduser()
    target_db = root / "producer_consumer_ledger.db"
    if _daemon_is_active():
        raise RuntimeError("mnemos daemon must be inactive before feedback history apply")
    _assert_canonical_target(target_db)
    barrier = activate_feedback_migration_barrier(
        root,
        inventory_hash=str(expected_inventory_hash),
    )
    try:
        try:
            assert_feedback_writes_enabled(root)
        except RuntimeError as exc:
            if str(exc) != "feedback_migration_in_progress":
                raise
        else:
            raise RuntimeError("feedback migration barrier did not block writers")
        connections = _lock_feedback_databases(root, target_db)
        try:
            frozen = build_feedback_history_inventory(root, connections=connections)
            _require_expected_inventory(
                frozen,
                expected_inventory_hash=expected_inventory_hash,
                expected_object_manifest_hash=expected_object_manifest_hash,
            )
            for database_class, conn in connections.items():
                if _connection_integrity(conn) != "ok":
                    raise RuntimeError(
                        f"feedback source integrity failed: {database_class}"
                    )
            backup_manifest = _backup_feedback_databases(
                root,
                target_db=target_db,
                backup_dir=backup_dir,
                inventory=frozen,
                connections=connections,
            )
            target_conn = connections["cognitive_state_target"]
            before_active = _active_counts_in_connection(target_conn)
            effect = _append_quarantine_objects_in_connection(
                target_conn,
                frozen["objects"],
            )
            after_active = _active_counts_in_connection(target_conn)
            if before_active != after_active:
                raise RuntimeError("feedback history migration promoted active cognition")
            coverage = _inspect_feedback_history_coverage_in_connection(
                target_conn,
                tuple(frozen["objects"]),
            )
            if (
                coverage["uncovered"]
                or coverage["unexpected"]
                or coverage["active_promotion"]
            ):
                raise RuntimeError("feedback history coverage verification failed")
            target_conn.commit()
        finally:
            _release_source_locks(connections)
        return {
            "schema_version": "mnemos.feedback_history_reconciliation.v1",
            "status": "applied",
            "inventory_hash": frozen["inventory_hash"],
            "object_manifest_hash": frozen["object_manifest_hash"],
            "object_count": frozen["object_count"],
            "counts_by_domain": frozen["counts_by_domain"],
            "counts_by_table": frozen["counts_by_table"],
            "effect": effect,
            "coverage": coverage,
            "active_head_delta": after_active["heads"] - before_active["heads"],
            "active_revision_delta": (
                after_active["revisions"] - before_active["revisions"]
            ),
            "backup_manifest": str(backup_manifest),
            "barrier_verified": True,
        }
    finally:
        deactivate_feedback_migration_barrier(root, owner_id=barrier.owner_id)


def restore_feedback_history(
    *,
    database_dir: Path,
    restore_manifest: Path,
) -> dict[str, Any]:
    """Restore every sealed source and target database from one manifest."""

    root = Path(database_dir).expanduser()
    if _daemon_is_active():
        raise RuntimeError("mnemos daemon must be inactive before feedback history restore")
    manifest_path = Path(restore_manifest).expanduser().resolve(strict=True)
    manifest = load_json_value(manifest_path)
    if not isinstance(manifest, Mapping):
        raise ValueError("feedback history restore manifest is invalid")
    if manifest.get("schema_version") != "mnemos.feedback_history_backup_manifest.v2":
        raise ValueError("feedback history restore manifest schema mismatch")
    manifest_core = dict(manifest)
    supplied_manifest_hash = manifest_core.pop("manifest_hash", "")
    if supplied_manifest_hash != sha256_json(manifest_core):
        raise ValueError("feedback history restore manifest hash mismatch")
    target_db = root / "producer_consumer_ledger.db"
    barrier = activate_feedback_migration_barrier(
        root,
        inventory_hash=str(manifest["inventory_hash"]),
    )
    try:
        entries = _validate_feedback_restore_manifest(root, manifest)
        target_entry = entries["cognitive_state_target"]
        backup_path = Path(str(target_entry["backup_path"])).resolve(strict=True)
        backup_logical_hash = str(target_entry["backup_logical_hash"])
        restored = _restore_sqlite_backup(
            backup_path,
            target_db,
            expected_logical_hash=backup_logical_hash,
        )
        return {
            "schema_version": "mnemos.feedback_history_restore.v1",
            "status": "restored",
            "inventory_hash": manifest["inventory_hash"],
            "object_manifest_hash": manifest["object_manifest_hash"],
            "target_integrity": restored["integrity"],
            "validated_backup_count": len(entries),
        }
    finally:
        deactivate_feedback_migration_barrier(root, owner_id=barrier.owner_id)


def _validate_feedback_restore_manifest(
    database_dir: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    """Validate every sealed source and target backup before target restore."""

    declared = manifest.get("database_classes")
    backups = manifest.get("backups")
    if not isinstance(declared, list) or not isinstance(backups, list):
        raise ValueError("feedback history backup manifest is incomplete")
    entries: dict[str, Mapping[str, Any]] = {}
    for item in backups:
        if not isinstance(item, Mapping):
            raise ValueError("feedback history backup entry is invalid")
        database_class = str(item.get("database_class") or "")
        if not database_class or database_class in entries:
            raise ValueError("feedback history backup database class is invalid")
        entries[database_class] = item
    if sorted(entries) != sorted(str(value) for value in declared):
        raise ValueError("feedback history backup class manifest mismatch")
    expected_sources = {
        source.database_class: source.path
        for source in feedback_source_databases(database_dir)
    }
    expected_sources["cognitive_state_target"] = (
        Path(database_dir) / "producer_consumer_ledger.db"
    )
    if set(entries) != set(expected_sources):
        raise ValueError("feedback history backup class set is incomplete")
    for database_class, item in entries.items():
        expected_source = expected_sources[database_class]
        declared_source_text = str(item.get("source_path") or "")
        if not declared_source_text:
            raise ValueError("feedback history backup source path is missing")
        declared_source = Path(declared_source_text)
        expected_resolved = expected_source.resolve(strict=False)
        if declared_source.resolve(strict=False) != expected_resolved:
            raise ValueError("feedback history backup source path mismatch")
        state = str(item.get("state") or "")
        if state == "absent":
            if database_class == "cognitive_state_target":
                raise ValueError("feedback history target backup is missing")
            if expected_source.exists():
                raise ValueError(
                    f"feedback history absent source appeared after backup: {database_class}"
                )
            if any(
                str(item.get(field_name) or "")
                for field_name in (
                    "backup_path",
                    "file_hash",
                    "source_logical_hash",
                    "backup_logical_hash",
                )
            ) or item.get("integrity") != "not_applicable":
                raise ValueError(
                    f"feedback history absent source entry is invalid: {database_class}"
                )
            continue
        if state != "present":
            raise ValueError(
                f"feedback history backup state is invalid: {database_class}"
            )
        backup_candidate = Path(str(item.get("backup_path") or ""))
        if not expected_source.is_file() or not declared_source.is_file():
            raise ValueError(
                f"feedback history source is missing: {database_class}"
            )
        if not backup_candidate.is_file():
            raise ValueError(
                f"feedback history backup is missing: {database_class}"
            )
        source_path = expected_source.resolve(strict=True)
        backup_path = backup_candidate.resolve(strict=True)
        if _file_hash(backup_path) != item.get("file_hash"):
            raise ValueError(
                f"feedback history backup hash mismatch: {database_class}"
            )
        if _integrity(backup_path) != "ok" or item.get("integrity") != "ok":
            raise ValueError(
                f"feedback history backup integrity mismatch: {database_class}"
            )
        logical_hash = _database_logical_hash(backup_path)
        if (
            logical_hash != item.get("backup_logical_hash")
            or logical_hash != item.get("source_logical_hash")
        ):
            raise ValueError(
                f"feedback history backup logical hash mismatch: {database_class}"
            )
        if database_class != "cognitive_state_target" and (
            _database_logical_hash(source_path) != item.get("source_logical_hash")
        ):
            raise ValueError(
                f"feedback history source advanced after backup: {database_class}"
            )
    return entries


def public_inventory_report(
    inventory: Mapping[str, Any],
    *,
    target_db: Path,
) -> dict[str, Any]:
    """Return a content-safe migration inventory and coverage report."""

    coverage = inspect_feedback_history_coverage(target_db, inventory)
    return {
        "schema_version": FEEDBACK_HISTORY_SCHEMA_VERSION,
        "status": "dry_run",
        "inventory_hash": inventory["inventory_hash"],
        "object_manifest_hash": inventory["object_manifest_hash"],
        "object_count": inventory["object_count"],
        "counts_by_domain": inventory["counts_by_domain"],
        "counts_by_table": inventory["counts_by_table"],
        "schema_fingerprints": inventory["schema_fingerprints"],
        "coverage": coverage,
        "sensitive_bytes_in_report": 0,
        "apply_required": coverage["uncovered"] > 0,
    }


def _inventory_database(
    source: FeedbackSourceDatabase,
    conn: sqlite3.Connection,
) -> tuple[list[HistoricalFeedbackObject], dict[str, str]]:
    conn.row_factory = sqlite3.Row
    specs = _selection_specs(source.database_class, conn)
    objects: list[HistoricalFeedbackObject] = []
    schemas: dict[str, str] = {}
    for table, predicate, params in specs:
        if not _table_exists(conn, table):
            continue
        columns = _columns(conn, table)
        _validate_known_columns(source.database_class, table, columns)
        primary = tuple(
            name for _, name, _type, _notnull, _default, pk in columns if int(pk) > 0
        )
        if not primary:
            raise RuntimeError(
                f"feedback history source lacks primary identity: {source.database_class}.{table}"
            )
        schema_fingerprint = _schema_fingerprint(conn, table, columns)
        schemas[table] = schema_fingerprint
        query = f'SELECT * FROM "{table}"'  # nosec B608 - fixed table registry
        if predicate:
            query += " WHERE " + predicate
        query += " ORDER BY " + ", ".join(f'"{name}"' for name in primary)
        for row in conn.execute(query, params).fetchall():
            normalized_row = {
                name: _normalize_sql_value(row[name]) for _, name, *_rest in columns
            }
            _validate_structured_fields(
                source.database_class,
                table,
                normalized_row,
            )
            primary_key = tuple((name, normalized_row[name]) for name in primary)
            source_refs = _source_refs(normalized_row)
            objects.append(
                HistoricalFeedbackObject(
                    domain=source.domain,
                    database_class=source.database_class,
                    table=table,
                    primary_key=primary_key,
                    schema_fingerprint=schema_fingerprint,
                    field_manifest=tuple(name for _, name, *_rest in columns),
                    source_refs=source_refs,
                    row_hash=sha256_json(normalized_row),
                )
            )
    _validate_projection_receipts(source.database_class, conn)
    return objects, schemas


def _selection_specs(
    database_class: str,
    conn: sqlite3.Connection,
) -> tuple[tuple[str, str, tuple[Any, ...]], ...]:
    if database_class == "delivery_events":
        delivery_columns = (
            {item[1] for item in _columns(conn, "delivery_events")}
            if _table_exists(conn, "delivery_events")
            else set()
        )
        legacy_links = [
            name for name in ("feedback", "outcome_id") if name in delivery_columns
        ]
        delivery_predicate = " OR ".join(
            f"COALESCE(\"{name}\", '')<>''" for name in legacy_links
        ) or "0"
        return (
            ("delivery_events", delivery_predicate, ()),
            ("feedback_events", "", ()),
            ("feedback_receipts", "", ()),
            ("cognitive_outcomes", "", ()),
            ("outcome_feedback_events", "", ()),
            ("outcome_projection_receipts", "", ()),
        )
    if database_class == "feedback_signals":
        return (("feedback_signals", "", ()),)
    if database_class == "scoring":
        search_columns = (
            {item[1] for item in _columns(conn, "search_sessions")}
            if _table_exists(conn, "search_sessions")
            else set()
        )
        interaction_columns = [
            name
            for name in (
                "clicked_path",
                "clicked_at",
                "opened_path",
                "opened_at",
                "ignored_at",
                "outcome_status",
                "outcome_at",
            )
            if name in search_columns
        ]
        search_predicate = " OR ".join(
            f"COALESCE(\"{name}\", '')<>''" for name in interaction_columns
        ) or "0"
        return (
            ("search_sessions", search_predicate, ()),
            (
                "ground_truth_signals",
                "signal_type IN ('search_click','search_ignore') "
                "OR session_id LIKE 'feedback-%'",
                (),
            ),
            (
                "scorer_training_queue",
                "session_id LIKE 'feedback-%' OR "
                "json_extract(features_json, '$.source') IN "
                "('push_feedback','search_click','search_ignore',"
                "'dialog_reminder','reflection_feedback','delivery_feedback')",
                (),
            ),
            ("scorer_feedback_events", "", ()),
            ("bayesian_feedback", "", ()),
        )
    if database_class == "reflections":
        return (
            (
                "reflection_records",
                "COALESCE(feedback_type,'')<>'' "
                "OR COALESCE(implicit_feedback_type,'')<>''",
                (),
            ),
            (
                "layer5_experiences",
                "type='outcome_feedback'",
                (),
            ),
            (
                "cognitive_shifts",
                "shift_type='outcome_feedback'",
                (),
            ),
        )
    if database_class == "rule_weight_optimizer":
        prefix = (
            "rule_name LIKE 'push_feedback:%' OR "
            "rule_name LIKE 'search_click:%' OR "
            "rule_name LIKE 'search_ignore:%' OR "
            "rule_name LIKE 'dialog_reminder:%' OR "
            "rule_name LIKE 'reflection_feedback:%' OR "
            "rule_name LIKE 'delivery_feedback:%'"
        )
        return (
            ("rule_outcomes", f"COALESCE(source_event_id,'')<>'' OR {prefix}", ()),
            ("optimize_log", f"COALESCE(source_event_id,'')<>'' OR {prefix}", ()),
            ("weight_history", prefix, ()),
        )
    raise ValueError(f"unknown feedback source database class: {database_class}")


def _names(value: str) -> frozenset[str]:
    return frozenset(value.split())


_KNOWN_COLUMNS: dict[tuple[str, str], frozenset[str]] = {
    ("delivery_events", "delivery_events"): _names(
        "event_id created_at source subject channel target requested_level "
        "delivered_level decision reason profile cooldown_key task_key "
        "trust_decision_id trust_score task_fit_score interruption_cost "
        "feedback feedback_at outcome_id metadata_json"
    ),
    ("delivery_events", "feedback_events"): _names(
        "feedback_event_id delivery_event_id created_at completed_at principal_id "
        "principal_agent project session_id subject action status "
        "required_consumers_json metadata_json"
    ),
    ("delivery_events", "feedback_receipts"): _names(
        "feedback_event_id consumer status started_at completed_at attempt_count "
        "receipt_json error"
    ),
    ("delivery_events", "cognitive_outcomes"): _names(
        "outcome_id created_at source subject action dimension label confidence "
        "delivery_event_id metadata_json"
    ),
    ("delivery_events", "outcome_feedback_events"): _names(
        "feedback_event_id status result_json updated_at"
    ),
    ("delivery_events", "outcome_projection_receipts"): _names(
        "feedback_event_id projection status result_json error attempt_count updated_at"
    ),
    ("feedback_signals", "feedback_signals"): _names(
        "signal_id created_at source subject action polarity scope_type scope_value "
        "target_ref source_event_id metadata_json"
    ),
    ("scoring", "search_sessions"): _names(
        "id session_id query result_paths created_at clicked_path clicked_at "
        "opened_path opened_at ignored_at outcome_status outcome_at"
    ),
    ("scoring", "ground_truth_signals"): _names(
        "id profile_id session_id signal_type signal_value confidence latency_hours "
        "created_at"
    ),
    ("scoring", "scorer_training_queue"): _names(
        "id session_id dimension features_json priority earliest_train_at status "
        "retry_count created_at updated_at"
    ),
    ("scoring", "scorer_feedback_events"): _names(
        "feedback_event_id session_id dimension created_at"
    ),
    ("scoring", "bayesian_feedback"): _names(
        "id dimension is_positive weight context_json created_at"
    ),
    ("reflections", "reflection_records"): _names(
        "id created_at trigger trigger_event user_query mirror_snapshots "
        "mirror_dimensions insight_summary insight_key_points insight_dimensions "
        "temporal_context feedback_type feedback_comment feedback_given_at "
        "fed_back_to_observations fed_back_to_knowledge implicit_feedback_type "
        "implicit_feedback_confidence implicit_feedback_signals implicit_feedback_at "
        "internal_validation access_control"
    ),
    ("reflections", "layer5_experiences"): _names(
        "id type dimension dimensions trigger confidence summary reason from_state "
        "to_state evidence timestamp created_at source_event_id access_control"
    ),
    ("reflections", "cognitive_shifts"): _names(
        "id dimension shift_type from_state to_state confidence evidence first_seen_at "
        "shift_detected_at related_reflection_id source_event_id access_control"
    ),
    ("rule_weight_optimizer", "rule_outcomes"): _names(
        "id rule_name predicted_score actual_label created_at source_event_id"
    ),
    ("rule_weight_optimizer", "optimize_log"): _names(
        "id rule_name triggered_at source_event_id"
    ),
    ("rule_weight_optimizer", "weight_history"): _names(
        "id rule_name old_weight new_weight accuracy sample_count optimized_at"
    ),
}


def _validate_known_columns(
    database_class: str,
    table: str,
    columns: Sequence[tuple[Any, ...]],
) -> None:
    allowed = _KNOWN_COLUMNS.get((database_class, table))
    if allowed is None:
        raise RuntimeError(f"unregistered feedback source table: {database_class}.{table}")
    actual = {str(item[1]) for item in columns}
    required = _required_columns(database_class, table)
    if not required.issubset(actual) or not actual.issubset(allowed):
        raise RuntimeError(
            f"unknown feedback source schema: {database_class}.{table}"
        )


def _required_columns(database_class: str, table: str) -> frozenset[str]:
    primary_required = {
        "delivery_events": {"event_id"},
        "feedback_events": {"feedback_event_id", "delivery_event_id"},
        "feedback_receipts": {"feedback_event_id", "consumer"},
        "cognitive_outcomes": {"outcome_id", "delivery_event_id"},
        "outcome_feedback_events": {"feedback_event_id"},
        "outcome_projection_receipts": {"feedback_event_id", "projection"},
        "feedback_signals": {"signal_id", "source_event_id", "target_ref"},
        "search_sessions": {"id", "session_id"},
        "ground_truth_signals": {"id", "session_id", "signal_type"},
        "scorer_training_queue": {"id", "session_id", "features_json"},
        "scorer_feedback_events": {"feedback_event_id", "session_id"},
        "bayesian_feedback": {"id"},
        "reflection_records": {"id", "feedback_type", "implicit_feedback_type"},
        "layer5_experiences": {"id", "type", "source_event_id"},
        "cognitive_shifts": {"id", "shift_type", "source_event_id"},
        "rule_outcomes": {"id", "rule_name", "source_event_id"},
        "optimize_log": {"id", "rule_name", "source_event_id"},
        "weight_history": {"id", "rule_name"},
    }
    return frozenset(primary_required[table])


def _with_projection_links(
    objects: Sequence[HistoricalFeedbackObject],
) -> tuple[HistoricalFeedbackObject, ...]:
    ref_index: dict[str, set[str]] = defaultdict(set)
    for item in objects:
        for ref in item.source_refs:
            ref_index[ref].add(item.source_key)
    linked = []
    for item in objects:
        neighbors = sorted(
            {
                key
                for ref in item.source_refs
                for key in ref_index[ref]
                if key != item.source_key
            }
        )
        linked.append(replace(item, projection_links=tuple(neighbors)))
    return tuple(sorted(linked, key=lambda item: item.source_key))


def _source_refs(row: Mapping[str, Any]) -> tuple[str, ...]:
    refs: set[str] = set()
    for field in (
        "feedback_event_id",
        "delivery_event_id",
        "event_id",
        "outcome_id",
        "source_event_id",
        "session_id",
        "related_reflection_id",
        "rule_name",
        "target_ref",
    ):
        value = row.get(field)
        if value not in {None, ""}:
            refs.add(f"{field}:{value}")
            if field == "event_id":
                refs.add(f"delivery_event_id:{value}")
            elif field == "source_event_id" and str(value).startswith("feedback-"):
                refs.add(f"feedback_event_id:{value}")
            elif field == "target_ref" and str(value).startswith("delivery-"):
                refs.add(f"delivery_event_id:{value}")
    metadata_fields = ("metadata_json", "result_json", "receipt_json", "features_json", "context_json")
    for field in metadata_fields:
        value = row.get(field)
        if not isinstance(value, str) or not value:
            continue
        loaded = _json_load(value, default={})
        if not isinstance(loaded, Mapping):
            raise RuntimeError(f"malformed feedback source JSON: {field}")
        for ref_field in (
            "feedback_event_id",
            "delivery_event_id",
            "outcome_id",
            "session_id",
            "source_event_id",
            "reflection_id",
        ):
            ref_value = loaded.get(ref_field)
            if ref_value not in {None, ""}:
                refs.add(f"{ref_field}:{ref_value}")
                if ref_field == "source_event_id" and str(ref_value).startswith(
                    "feedback-"
                ):
                    refs.add(f"feedback_event_id:{ref_value}")
    return tuple(sorted(refs))


def _append_quarantine_objects_in_connection(
    conn: sqlite3.Connection,
    objects: Iterable[HistoricalFeedbackObject],
) -> dict[str, int]:
    inserted = 0
    existing = 0
    for item in objects:
        payload = item.quarantine_payload()
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_hash = sha256_json(payload)
        quarantine_id = "cogquarantine-" + sha256_json(
            {
                "source_key": item.source_key,
                "reason_code": FEEDBACK_HISTORY_REASON_CODE,
            }
        ).split(":", 1)[1][:32]
        row = conn.execute(
            """
            SELECT quarantine_id, field_manifest, payload_json, payload_hash
            FROM cognitive_state_migration_quarantine
            WHERE source_key=? AND reason_code=?
            """,
            (item.source_key, FEEDBACK_HISTORY_REASON_CODE),
        ).fetchone()
        field_manifest = json.dumps(
            list(item.field_manifest),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if row is not None:
            if (
                str(row["quarantine_id"]) != quarantine_id
                or str(row["field_manifest"]) != field_manifest
                or str(row["payload_json"]) != payload_json
                or str(row["payload_hash"]) != payload_hash
            ):
                raise RuntimeError("immutable feedback quarantine conflict")
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
                f"feedback_history.{item.domain}.{item.database_class}.{item.table}",
                item.source_key,
                FEEDBACK_HISTORY_REASON_CODE,
                field_manifest,
                payload_json,
                payload_hash,
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            ),
        )
        inserted += 1
    return {"inserted": inserted, "existing": existing}


def _backup_feedback_databases(
    database_dir: Path,
    *,
    target_db: Path,
    backup_dir: Path,
    inventory: Mapping[str, Any],
    connections: Mapping[str, sqlite3.Connection],
) -> Path:
    destination = Path(backup_dir).expanduser().resolve(strict=False)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    entries: list[dict[str, Any]] = []
    sources = list(feedback_source_databases(database_dir)) + [
        FeedbackSourceDatabase("cognitive_state_target", target_db, "target")
    ]
    suffix = str(inventory["inventory_hash"]).split(":", 1)[1][:16]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for source in sources:
        if not source.path.is_file():
            if source.database_class == "cognitive_state_target":
                raise RuntimeError("feedback history target database is missing")
            entries.append(
                {
                    "database_class": source.database_class,
                    "state": "absent",
                    "source_path": str(source.path.resolve(strict=False)),
                    "backup_path": "",
                    "file_hash": "",
                    "integrity": "not_applicable",
                    "source_logical_hash": "",
                    "backup_logical_hash": "",
                }
            )
            continue
        backup_path = destination / f"{source.database_class}.{suffix}.{stamp}.db"
        source_conn = connections.get(source.database_class)
        if source_conn is None:
            raise RuntimeError(
                f"feedback backup source is not locked: {source.database_class}"
            )
        source_logical_hash = _connection_logical_hash(source_conn)
        # The BEGIN IMMEDIATE owner above freezes this committed snapshot.  A
        # separate read-only handle is required because SQLite's backup API
        # cannot advance while its source connection owns a write transaction.
        _sqlite_backup(source.path, backup_path)
        integrity = _integrity(backup_path)
        if integrity != "ok":
            raise RuntimeError(
                f"feedback backup integrity failed: {source.database_class}"
            )
        backup_logical_hash = _database_logical_hash(backup_path)
        if source_logical_hash != backup_logical_hash:
            raise RuntimeError(
                f"feedback backup logical snapshot mismatch: {source.database_class}"
            )
        entries.append(
            {
                "database_class": source.database_class,
                "state": "present",
                "source_path": str(source.path.resolve(strict=True)),
                "backup_path": str(backup_path),
                "file_hash": _file_hash(backup_path),
                "integrity": integrity,
                "source_logical_hash": source_logical_hash,
                "backup_logical_hash": backup_logical_hash,
            }
        )
    manifest = {
        "schema_version": "mnemos.feedback_history_backup_manifest.v2",
        "inventory_hash": inventory["inventory_hash"],
        "object_manifest_hash": inventory["object_manifest_hash"],
        "object_count": inventory["object_count"],
        "backups": entries,
        "database_classes": sorted(str(item["database_class"]) for item in entries),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    manifest["manifest_hash"] = sha256_json(manifest)
    manifest_path = destination / f"feedback-history-manifest.{suffix}.{stamp}.json"
    if manifest_path.exists():
        raise FileExistsError(f"feedback history backup manifest exists: {manifest_path}")
    # trusted-scan: backup owner=cognitive target=feedback_history_manifest expires=never reviewed manifest
    _write_private_text_file(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    return manifest_path


def _lock_feedback_databases(
    database_dir: Path,
    target_db: Path,
) -> dict[str, sqlite3.Connection]:
    connections: dict[str, sqlite3.Connection] = {}
    try:
        sources = (*feedback_source_databases(database_dir), FeedbackSourceDatabase(
            "cognitive_state_target", target_db, "target"
        ))
        for source in sorted(
            sources,
            key=lambda item: str(item.path),
        ):
            if not source.path.is_file():
                continue
            conn = _connect(source.path)
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
            connections[source.database_class] = conn
        return connections
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        _release_source_locks(connections)
        raise


def _release_source_locks(connections: Mapping[str, sqlite3.Connection]) -> None:
    for conn in reversed(list(connections.values())):
        try:
            conn.rollback()
        finally:
            conn.close()


def _require_expected_inventory(
    inventory: Mapping[str, Any],
    *,
    expected_inventory_hash: str,
    expected_object_manifest_hash: str,
) -> None:
    if str(inventory["inventory_hash"]) != str(expected_inventory_hash):
        raise RuntimeError("feedback history inventory hash drift")
    if str(inventory["object_manifest_hash"]) != str(expected_object_manifest_hash):
        raise RuntimeError("feedback history object manifest hash drift")


def _active_counts(target_db: Path) -> dict[str, int]:
    with _connect(target_db, read_only=True) as conn:
        return _active_counts_in_connection(conn)


def _active_counts_in_connection(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "heads": int(conn.execute("SELECT COUNT(*) FROM cognitive_state_heads").fetchone()[0]),
        "revisions": int(
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_state_revisions WHERE admission_state='active'"
            ).fetchone()[0]
        ),
    }


def _count_active_feedback_objects(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM cognitive_state_revisions
            WHERE admission_state='active'
              AND object_type IN ('user_reaction_event','feedback_attribution_record')
              AND source_event_id LIKE 'feedback-history:%'
            """
        ).fetchone()[0]
    )


def _assert_canonical_target(target_db: Path) -> None:
    if not Path(target_db).is_file():
        raise RuntimeError("canonical cognitive state target is missing")
    with _connect(target_db, read_only=True) as conn:
        state = inspect_cognitive_state_schema(conn)
    if state.classification != "canonical":
        raise RuntimeError(
            "cognitive state schema v3 reconciliation required before feedback history apply"
        )


def _validate_projection_receipts(
    database_class: str,
    conn: sqlite3.Connection,
) -> None:
    checks: tuple[tuple[str, str, str], ...] = ()
    if database_class == "delivery_events":
        checks = (
            ("feedback_receipts", "feedback_events", "feedback_event_id"),
            (
                "outcome_projection_receipts",
                "outcome_feedback_events",
                "feedback_event_id",
            ),
        )
    for child, parent, key in checks:
        if not _table_exists(conn, child) or not _table_exists(conn, parent):
            continue
        row = conn.execute(
            f'''SELECT COUNT(*) FROM "{child}" AS child
                LEFT JOIN "{parent}" AS parent ON parent."{key}"=child."{key}"
                WHERE parent."{key}" IS NULL''',  # nosec B608 - fixed registry
        ).fetchone()
        if row and int(row[0]) > 0:
            raise RuntimeError(
                f"orphan feedback projection receipt: {database_class}.{child}"
            )


_STRUCTURED_FIELDS: dict[tuple[str, str], frozenset[str]] = {
    key: frozenset(
        field
        for field in fields
        if field.endswith("_json")
        or field
        in {
            "result_paths",
            "mirror_snapshots",
            "mirror_dimensions",
            "insight_key_points",
            "insight_dimensions",
            "temporal_context",
            "implicit_feedback_signals",
            "internal_validation",
            "access_control",
            "dimensions",
            "evidence",
        }
    )
    for key, fields in _KNOWN_COLUMNS.items()
}


def _validate_structured_fields(
    database_class: str,
    table: str,
    row: Mapping[str, Any],
) -> None:
    missing = object()
    for field in _STRUCTURED_FIELDS.get((database_class, table), frozenset()):
        value = row.get(field)
        if value in {None, ""}:
            continue
        if not isinstance(value, str) or _json_load(value, default=missing) is missing:
            raise RuntimeError(
                f"malformed feedback source JSON: {database_class}.{table}.{field}"
            )


def _database_logical_hash(path: Path) -> str:
    """Hash schema and rows, independent of SQLite page layout."""

    with _connect(Path(path), read_only=True) as conn:
        return _connection_logical_hash(conn)


def _connection_logical_hash(conn: sqlite3.Connection) -> str:
    """Hash the exact snapshot visible through an already locked connection."""

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
    for table in tables:
        columns = _columns(conn, table)
        schema_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        digest.update(
            json.dumps(
                {
                    "table": table,
                    "columns": [list(item) for item in columns],
                    "sql": str(schema_row[0] if schema_row else ""),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        primary = [
            str(name)
            for _, name, _type, _notnull, _default, pk in columns
            if int(pk) > 0
        ]
        order = primary or [str(item[1]) for item in columns]
        query = f'SELECT * FROM "{table}"'  # nosec B608 - sqlite registry
        if order:
            query += " ORDER BY " + ", ".join(f'"{name}"' for name in order)
        for row in conn.execute(query):
            payload = {
                str(item[1]): _normalize_sql_value(row[str(item[1])])
                for item in columns
            }
            digest.update(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
    return "sha256:" + digest.hexdigest()


def _daemon_is_active() -> bool:
    return mnemos_runtime_is_active()


def _schema_fingerprint(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[tuple[Any, ...]],
) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return str(sha256_json(
        {
            "table": table,
            "columns": [list(item) for item in columns],
            "ddl_hash": sha256_json({"sql": str(row[0] if row else "")}),
        }
    ))


def _columns(conn: sqlite3.Connection, table: str) -> tuple[tuple[Any, ...], ...]:
    if not _table_exists(conn, table):
        return ()
    return tuple(tuple(row) for row in conn.execute(f'PRAGMA table_info("{table}")'))


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


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
        conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _secure_create_empty_file(destination)
    with _connect(source, read_only=True) as source_conn, _connect(destination) as target_conn:
        source_conn.backup(target_conn)


def _sqlite_backup_connection(
    source_conn: sqlite3.Connection,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _secure_create_empty_file(destination)
    with _connect(destination) as target_conn:
        source_conn.backup(target_conn)


def _restore_sqlite_backup(
    source: Path,
    destination: Path,
    *,
    expected_logical_hash: str,
) -> dict[str, str]:
    """Atomically replace a target while both old and new inodes are locked."""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    staged = destination.with_name(
        f".{destination.name}.feedback-restore-{os.getpid()}-{stamp}.tmp"
    )
    target_conn = _connect(destination)
    staged_conn: sqlite3.Connection | None = None
    try:
        target_conn.execute("BEGIN EXCLUSIVE")
        _sqlite_backup(source, staged)
        staged_conn = _connect(staged)
        staged_conn.execute("BEGIN EXCLUSIVE")
        integrity = _connection_integrity(staged_conn)
        if integrity != "ok":
            raise RuntimeError("restored cognitive state integrity check failed")
        logical_hash = _connection_logical_hash(staged_conn)
        if logical_hash != expected_logical_hash:
            raise RuntimeError("restored cognitive state logical snapshot mismatch")
        os.replace(staged, destination)
        return {"integrity": integrity, "logical_hash": logical_hash}
    except BaseException:
        raise
    finally:
        if staged_conn is not None:
            staged_conn.rollback()
            staged_conn.close()
        target_conn.rollback()
        target_conn.close()
        staged.unlink(missing_ok=True)


def _secure_create_empty_file(path: Path) -> None:
    """Create one private file without a world-readable permission window."""

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise FileExistsError(f"feedback history backup exists: {path}") from exc
    os.close(fd)


def _write_private_text_file(path: Path, content: str) -> None:
    """Write a new UTF-8 artifact whose mode is private from its first inode."""

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise FileExistsError(f"feedback history backup exists: {path}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)


def _integrity(path: Path) -> str:
    with _connect(path, read_only=True) as conn:
        return _connection_integrity(conn)


def _connection_integrity(conn: sqlite3.Connection) -> str:
    """Return SQLite integrity for an already locked/read-only connection."""

    row = conn.execute("PRAGMA integrity_check").fetchone()
    return str(row[0] if row else "")


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _json_load(value: str, *, default: Any = None) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
