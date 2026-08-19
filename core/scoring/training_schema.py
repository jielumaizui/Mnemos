"""Canonical scoring-side schema authority for governed training.

The complete projection table set is created together only for a fresh projection.
Any partial, drifted, or unregistered live shape fails closed and requires the
explicit COG-048 reconciliation workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
from typing import Any


TRAINING_SCHEMA_VERSION = "mnemos.governed_training_projection.v1"
SCHEMA_COMPONENT = "scoring.governed_training"
REGISTRY_TABLE = "mnemos_schema_registry"
OWNED_TABLES = (
    "governed_training_samples",
    "governed_training_sample_actions",
    "governed_training_sample_receipts",
    "governed_scorer_models",
    "governed_scorer_model_heads",
    "governed_training_run_receipts",
    "governed_training_aux_effects",
    "governed_training_aux_receipts",
)
_SQL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

CANONICAL_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE governed_training_samples (
    sample_id TEXT PRIMARY KEY,
    admission_revision_id TEXT NOT NULL UNIQUE,
    admission_payload_hash TEXT NOT NULL,
    dimension TEXT NOT NULL CHECK(dimension = 'predictive_delivery'),
    metric_id TEXT NOT NULL CHECK(metric_id = 'predictive_delivery_usefulness'),
    feature_snapshot_json TEXT NOT NULL CHECK(
        json_valid(feature_snapshot_json) AND json_type(feature_snapshot_json)='object'
    ),
    feature_snapshot_hash TEXT NOT NULL,
    label_numeric INTEGER NOT NULL CHECK(label_numeric IN (0, 1)),
    label_value TEXT NOT NULL CHECK(label_value IN ('useful', 'not_useful')),
    dataset_group_id TEXT NOT NULL,
    dataset_group_hash TEXT NOT NULL,
    dataset_split TEXT NOT NULL CHECK(dataset_split IN ('train', 'validation', 'holdout')),
    access_control_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(
        (label_numeric=1 AND label_value='useful')
        OR (label_numeric=0 AND label_value='not_useful')
    )
);
CREATE INDEX idx_governed_training_samples_dimension_split
    ON governed_training_samples(dimension, dataset_split, admission_revision_id);
CREATE INDEX idx_governed_training_samples_group
    ON governed_training_samples(dataset_group_hash, dataset_split);

CREATE TABLE governed_training_sample_actions (
    action_id TEXT PRIMARY KEY,
    sample_id TEXT NOT NULL,
    admission_revision_id TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK(action_type IN ('admit', 'exclude', 'correct')),
    reason_code TEXT NOT NULL,
    supersedes_action_id TEXT,
    evidence_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(sample_id) REFERENCES governed_training_samples(sample_id) ON DELETE RESTRICT,
    FOREIGN KEY(admission_revision_id)
        REFERENCES governed_training_samples(admission_revision_id) ON DELETE RESTRICT,
    FOREIGN KEY(supersedes_action_id)
        REFERENCES governed_training_sample_actions(action_id) ON DELETE RESTRICT,
    CHECK(supersedes_action_id IS NULL OR supersedes_action_id != action_id)
);
CREATE INDEX idx_governed_training_sample_actions_sample
    ON governed_training_sample_actions(sample_id, created_at, action_id);

CREATE TABLE governed_training_sample_receipts (
    receipt_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL,
    admission_revision_id TEXT NOT NULL,
    sample_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('committed', 'rejected', 'revoked')),
    before_hash TEXT NOT NULL,
    after_hash TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL CHECK(
        json_valid(evidence_refs_json) AND json_type(evidence_refs_json)='array'
    ),
    receipt_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(admission_revision_id)
        REFERENCES governed_training_samples(admission_revision_id) ON DELETE RESTRICT,
    FOREIGN KEY(sample_id) REFERENCES governed_training_samples(sample_id) ON DELETE RESTRICT,
    FOREIGN KEY(action_id)
        REFERENCES governed_training_sample_actions(action_id) ON DELETE RESTRICT,
    UNIQUE(command_id, sample_id)
);
CREATE INDEX idx_governed_training_sample_receipts_admission
    ON governed_training_sample_receipts(admission_revision_id, created_at);

CREATE TABLE governed_scorer_models (
    model_id TEXT PRIMARY KEY,
    run_revision_id TEXT NOT NULL UNIQUE,
    run_payload_hash TEXT NOT NULL,
    admission_revision_ids_json TEXT NOT NULL CHECK(
        json_valid(admission_revision_ids_json)
        AND json_type(admission_revision_ids_json)='array'
    ),
    dimension TEXT NOT NULL CHECK(dimension = 'predictive_delivery'),
    model_type TEXT NOT NULL CHECK(model_type = 'binary_feature_centroid'),
    model_blob_json TEXT NOT NULL CHECK(
        json_valid(model_blob_json) AND json_type(model_blob_json)='object'
    ),
    model_blob_hash TEXT NOT NULL,
    dataset_manifest_hash TEXT NOT NULL,
    fit_input_hash TEXT NOT NULL,
    validation_report_hash TEXT NOT NULL,
    holdout_report_hash TEXT NOT NULL,
    access_control_hash TEXT NOT NULL,
    parent_model_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(parent_model_id) REFERENCES governed_scorer_models(model_id) ON DELETE RESTRICT
);
CREATE INDEX idx_governed_scorer_models_dimension
    ON governed_scorer_models(dimension, created_at, model_id);

CREATE TABLE governed_scorer_model_heads (
    dimension TEXT PRIMARY KEY CHECK(dimension = 'predictive_delivery'),
    model_id TEXT NOT NULL UNIQUE,
    run_revision_id TEXT NOT NULL UNIQUE,
    activated_at TEXT NOT NULL,
    FOREIGN KEY(model_id) REFERENCES governed_scorer_models(model_id) ON DELETE RESTRICT,
    FOREIGN KEY(run_revision_id)
        REFERENCES governed_scorer_models(run_revision_id) ON DELETE RESTRICT
);

CREATE TABLE governed_training_run_receipts (
    receipt_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE,
    run_revision_id TEXT NOT NULL UNIQUE,
    run_payload_hash TEXT NOT NULL,
    model_id TEXT,
    status TEXT NOT NULL CHECK(
        status IN (
            'model_sealed', 'sealed', 'committed',
            'insufficient_sample', 'failed', 'stale'
        )
    ),
    action_id TEXT NOT NULL,
    effect_id TEXT NOT NULL,
    before_hash TEXT NOT NULL,
    after_hash TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL CHECK(
        json_valid(evidence_refs_json) AND json_type(evidence_refs_json)='array'
    ),
    receipt_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(model_id) REFERENCES governed_scorer_models(model_id) ON DELETE RESTRICT
);
CREATE INDEX idx_governed_training_run_receipts_run
    ON governed_training_run_receipts(run_revision_id, status);

CREATE TABLE governed_training_aux_effects (
    effect_id TEXT PRIMARY KEY,
    effect_kind TEXT NOT NULL CHECK(effect_kind IN ('bayesian_prior', 'rule_optimizer')),
    run_revision_id TEXT NOT NULL,
    run_payload_hash TEXT NOT NULL,
    admission_revision_ids_json TEXT NOT NULL CHECK(
        json_valid(admission_revision_ids_json)
        AND json_type(admission_revision_ids_json)='array'
    ),
    dimension TEXT NOT NULL CHECK(dimension = 'predictive_delivery'),
    input_hash TEXT NOT NULL,
    artifact_json TEXT NOT NULL CHECK(
        json_valid(artifact_json) AND json_type(artifact_json)='object'
    ),
    artifact_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_revision_id, effect_kind)
);
CREATE INDEX idx_governed_training_aux_effects_run
    ON governed_training_aux_effects(run_revision_id, effect_kind);

CREATE TABLE governed_training_aux_receipts (
    receipt_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL,
    run_revision_id TEXT NOT NULL,
    effect_kind TEXT NOT NULL CHECK(effect_kind IN ('bayesian_prior', 'rule_optimizer')),
    effect_id TEXT,
    status TEXT NOT NULL CHECK(
        status IN ('model_sealed', 'sealed', 'committed', 'insufficient_sample', 'stale')
    ),
    before_hash TEXT NOT NULL,
    after_hash TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL CHECK(
        json_valid(evidence_refs_json) AND json_type(evidence_refs_json)='array'
    ),
    receipt_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(effect_id) REFERENCES governed_training_aux_effects(effect_id)
        ON DELETE RESTRICT,
    UNIQUE(command_id, effect_kind),
    UNIQUE(run_revision_id, effect_kind),
    CHECK(status != 'committed' OR effect_id IS NOT NULL)
);
CREATE INDEX idx_governed_training_aux_receipts_run
    ON governed_training_aux_receipts(run_revision_id, effect_kind, status);

CREATE TABLE IF NOT EXISTS mnemos_schema_registry (
    component TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    ddl_hash TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TRIGGER governed_training_samples_no_update
BEFORE UPDATE ON governed_training_samples BEGIN
    SELECT RAISE(ABORT, 'governed_training_samples are append-only');
END;
CREATE TRIGGER governed_training_samples_no_delete
BEFORE DELETE ON governed_training_samples BEGIN
    SELECT RAISE(ABORT, 'governed_training_samples are append-only');
END;
CREATE TRIGGER governed_training_sample_actions_no_update
BEFORE UPDATE ON governed_training_sample_actions BEGIN
    SELECT RAISE(ABORT, 'governed_training_sample_actions are append-only');
END;
CREATE TRIGGER governed_training_sample_actions_no_delete
BEFORE DELETE ON governed_training_sample_actions BEGIN
    SELECT RAISE(ABORT, 'governed_training_sample_actions are append-only');
END;
CREATE TRIGGER governed_training_sample_receipts_no_update
BEFORE UPDATE ON governed_training_sample_receipts BEGIN
    SELECT RAISE(ABORT, 'governed_training_sample_receipts are append-only');
END;
CREATE TRIGGER governed_training_sample_receipts_no_delete
BEFORE DELETE ON governed_training_sample_receipts BEGIN
    SELECT RAISE(ABORT, 'governed_training_sample_receipts are append-only');
END;
CREATE TRIGGER governed_scorer_models_no_update
BEFORE UPDATE ON governed_scorer_models BEGIN
    SELECT RAISE(ABORT, 'governed_scorer_models are append-only');
END;
CREATE TRIGGER governed_scorer_models_no_delete
BEFORE DELETE ON governed_scorer_models BEGIN
    SELECT RAISE(ABORT, 'governed_scorer_models are append-only');
END;
CREATE TRIGGER governed_training_run_receipts_no_update
BEFORE UPDATE ON governed_training_run_receipts BEGIN
    SELECT RAISE(ABORT, 'governed_training_run_receipts are append-only');
END;
CREATE TRIGGER governed_training_run_receipts_no_delete
BEFORE DELETE ON governed_training_run_receipts BEGIN
    SELECT RAISE(ABORT, 'governed_training_run_receipts are append-only');
END;
CREATE TRIGGER governed_training_aux_effects_no_update
BEFORE UPDATE ON governed_training_aux_effects BEGIN
    SELECT RAISE(ABORT, 'governed_training_aux_effects are append-only');
END;
CREATE TRIGGER governed_training_aux_effects_no_delete
BEFORE DELETE ON governed_training_aux_effects BEGIN
    SELECT RAISE(ABORT, 'governed_training_aux_effects are append-only');
END;
CREATE TRIGGER governed_training_aux_receipts_no_update
BEFORE UPDATE ON governed_training_aux_receipts BEGIN
    SELECT RAISE(ABORT, 'governed_training_aux_receipts are append-only');
END;
CREATE TRIGGER governed_training_aux_receipts_no_delete
BEFORE DELETE ON governed_training_aux_receipts BEGIN
    SELECT RAISE(ABORT, 'governed_training_aux_receipts are append-only');
END;
"""


class TrainingSchemaError(RuntimeError):
    """The governed training projection cannot be opened safely."""


@dataclass(frozen=True)
class TrainingSchemaState:
    """Read-only classification and evidence for the projection schema."""

    classification: str
    schema_version: str
    ddl_hash: str
    canonical_ddl_hash: str
    registry_version: str
    registry_ddl_hash: str
    tables: tuple[str, ...]
    row_counts: dict[str, int]
    migration_required: bool
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Return whether the live schema is canonical and needs no migration."""

        return not self.errors and not self.migration_required

    def as_dict(self) -> dict[str, Any]:
        """Serialize inspection evidence for CLI and audit reports."""

        return {
            "classification": self.classification,
            "schema_version": self.schema_version,
            "ddl_hash": self.ddl_hash,
            "canonical_ddl_hash": self.canonical_ddl_hash,
            "registry_version": self.registry_version,
            "registry_ddl_hash": self.registry_ddl_hash,
            "tables": list(self.tables),
            "row_counts": dict(self.row_counts),
            "migration_required": self.migration_required,
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


def _registry_row(conn: sqlite3.Connection) -> tuple[str, str]:
    if not _table_exists(conn, REGISTRY_TABLE):
        return "", ""
    try:
        row = conn.execute(
            "SELECT schema_version, ddl_hash FROM mnemos_schema_registry WHERE component=?",
            (SCHEMA_COMPONENT,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise TrainingSchemaError("invalid governed training schema registry") from exc
    return (str(row[0]), str(row[1])) if row else ("", "")


def _schema_signature(conn: sqlite3.Connection) -> str:
    objects: list[dict[str, Any]] = []
    for object_type, name, sql in conn.execute(
        "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger') "
        "AND name LIKE 'governed_%' ORDER BY type, name"
    ).fetchall():
        item: dict[str, Any] = {
            "type": str(object_type),
            "name": str(name),
            "sql": " ".join(str(sql).split()),
        }
        if object_type == "table":
            quoted_name = _quote_owned_table(str(name))
            item["columns"] = [
                tuple(row) for row in conn.execute(f"PRAGMA table_xinfo({quoted_name})").fetchall()
            ]
            item["foreign_keys"] = [
                tuple(row)
                for row in conn.execute(f"PRAGMA foreign_key_list({quoted_name})").fetchall()
            ]
        objects.append(item)
    raw = json.dumps(objects, ensure_ascii=False, sort_keys=True, default=str).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _quote_owned_table(name: str) -> str:
    if name not in OWNED_TABLES or _SQL_IDENTIFIER.fullmatch(name) is None:
        raise TrainingSchemaError("unknown governed training table identifier")
    return f'"{name}"'


def _execute_canonical_ddl(conn: sqlite3.Connection) -> None:
    statement = ""
    for line in CANONICAL_DDL.splitlines():
        if line.strip().upper().startswith("PRAGMA FOREIGN_KEYS"):
            continue
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            statement = ""
            if sql:
                conn.execute(sql)
    if statement.strip():
        raise TrainingSchemaError("governed training DDL is incomplete")


def _canonical_hash() -> str:
    with sqlite3.connect(":memory:") as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _execute_canonical_ddl(conn)
        return _schema_signature(conn)


CANONICAL_DDL_HASH = _canonical_hash()


def inspect_training_schema(conn: sqlite3.Connection) -> TrainingSchemaState:
    """Inspect the exact registered schema without creating or altering it."""

    tables = tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    )
    present = set(OWNED_TABLES) & set(tables)
    registry_version, registry_hash = _registry_row(conn)
    if not present and not registry_version and not registry_hash:
        return TrainingSchemaState(
            classification="absent",
            schema_version=TRAINING_SCHEMA_VERSION,
            ddl_hash="",
            canonical_ddl_hash=CANONICAL_DDL_HASH,
            registry_version="",
            registry_ddl_hash="",
            tables=tables,
            row_counts={},
            migration_required=False,
            errors=(),
        )

    row_counts = {
        # `_quote_owned_table` accepts exact registry-owned identifiers only.
        table: int(
            conn.execute(  # nosec B608
                f"SELECT COUNT(*) FROM {_quote_owned_table(table)}"  # nosec B608
            ).fetchone()[0]
        )
        for table in OWNED_TABLES
        if table in present
    }
    ddl_hash = _schema_signature(conn) if present else ""
    all_present = set(OWNED_TABLES) <= set(tables)
    if (
        all_present
        and ddl_hash == CANONICAL_DDL_HASH
        and registry_version == TRAINING_SCHEMA_VERSION
        and registry_hash == CANONICAL_DDL_HASH
    ):
        classification = "canonical"
        migration_required = False
        errors: tuple[str, ...] = ()
    elif all_present and ddl_hash == CANONICAL_DDL_HASH:
        classification = "registry_mismatch"
        migration_required = True
        errors = ("governed training schema registry mismatch",)
    elif not present and (registry_version or registry_hash):
        classification = "orphan_registry"
        migration_required = True
        errors = ("governed training registry exists without owned tables",)
    else:
        classification = "unknown_or_partial"
        migration_required = True
        errors = ("unknown or partial governed training schema",)
    return TrainingSchemaState(
        classification=classification,
        schema_version=TRAINING_SCHEMA_VERSION,
        ddl_hash=ddl_hash,
        canonical_ddl_hash=CANONICAL_DDL_HASH,
        registry_version=registry_version,
        registry_ddl_hash=registry_hash,
        tables=tables,
        row_counts=row_counts,
        migration_required=migration_required,
        errors=errors,
    )


def _write_registry_row(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO mnemos_schema_registry(component, schema_version, ddl_hash, applied_at) "
        "VALUES (?, ?, ?, ?)",
        (
            SCHEMA_COMPONENT,
            TRAINING_SCHEMA_VERSION,
            CANONICAL_DDL_HASH,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def initialize_training_schema(conn: sqlite3.Connection) -> None:
    """Create all owned tables for a fresh projection or validate exact current state."""

    conn.execute("PRAGMA foreign_keys = ON")
    state = inspect_training_schema(conn)
    if state.classification == "absent":
        _execute_canonical_ddl(conn)
        _write_registry_row(conn)
        verified = inspect_training_schema(conn)
        if not verified.ok:
            raise TrainingSchemaError("fresh governed training schema verification failed")
        return
    if not state.ok:
        raise TrainingSchemaError(
            "governed training schema requires explicit reconciliation: "
            f"classification={state.classification}"
        )


def validate_existing_training_schema(conn: sqlite3.Connection) -> None:
    """Reject unsafe existing shapes before any constructor-side DDL."""

    state = inspect_training_schema(conn)
    if state.classification == "absent" or state.ok:
        return
    raise TrainingSchemaError(
        "governed training schema requires explicit reconciliation: "
        f"classification={state.classification}"
    )
