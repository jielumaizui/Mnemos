from __future__ import annotations

import sqlite3

import pytest

from core.scoring.training_schema import (
    CANONICAL_DDL_HASH,
    SCHEMA_COMPONENT,
    TRAINING_SCHEMA_VERSION,
    TrainingSchemaError,
    initialize_training_schema,
    inspect_training_schema,
)


OWNED_TABLES = {
    "governed_training_samples",
    "governed_training_sample_actions",
    "governed_training_sample_receipts",
    "governed_scorer_models",
    "governed_scorer_model_heads",
    "governed_training_run_receipts",
    "governed_training_aux_effects",
    "governed_training_aux_receipts",
}


def test_fresh_training_schema_is_registered_and_exact() -> None:
    with sqlite3.connect(":memory:") as conn:
        assert inspect_training_schema(conn).classification == "absent"

        initialize_training_schema(conn)
        state = inspect_training_schema(conn)
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        registry = conn.execute(
            "SELECT schema_version, ddl_hash FROM mnemos_schema_registry " "WHERE component=?",
            (SCHEMA_COMPONENT,),
        ).fetchone()

    assert state.ok is True
    assert state.classification == "canonical"
    assert state.ddl_hash == CANONICAL_DDL_HASH
    assert OWNED_TABLES <= tables
    assert registry == (TRAINING_SCHEMA_VERSION, CANONICAL_DDL_HASH)
    assert (
        not {
            "ground_truth_signals",
            "scorer_training_queue",
            "scorer_feedback_events",
            "scorer_models",
            "bayesian_scorer_state",
            "bayesian_feedback",
        }
        & tables
    )


def test_append_only_training_projection_rows_reject_update_and_delete() -> None:
    with sqlite3.connect(":memory:") as conn:
        initialize_training_schema(conn)
        conn.execute(
            """
            INSERT INTO governed_training_samples (
                sample_id, admission_revision_id, admission_payload_hash,
                dimension, metric_id, feature_snapshot_json,
                feature_snapshot_hash, label_numeric, label_value,
                dataset_group_id, dataset_group_hash, dataset_split,
                access_control_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "training-sample-001",
                "cogrev-" + "1" * 32,
                "sha256:" + "1" * 64,
                "predictive_delivery",
                "predictive_delivery_usefulness",
                "{}",
                "sha256:" + "2" * 64,
                1,
                "useful",
                "training-group-001",
                "sha256:" + "3" * 64,
                "train",
                "sha256:" + "4" * 64,
                "2026-07-19T00:00:00+00:00",
            ),
        )

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE governed_training_samples SET label_numeric=0 "
                "WHERE sample_id='training-sample-001'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM governed_training_samples " "WHERE sample_id='training-sample-001'"
            )


def test_partial_or_tampered_training_schema_fails_closed() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE governed_training_samples (sample_id TEXT PRIMARY KEY)")
        with pytest.raises(TrainingSchemaError, match="reconciliation"):
            initialize_training_schema(conn)

    with sqlite3.connect(":memory:") as conn:
        initialize_training_schema(conn)
        conn.execute("DROP TRIGGER governed_training_samples_no_update")
        state = inspect_training_schema(conn)

        assert state.classification == "unknown_or_partial"
        assert state.ok is False
        with pytest.raises(TrainingSchemaError, match="reconciliation"):
            initialize_training_schema(conn)


def test_training_schema_registry_corruption_fails_closed() -> None:
    with sqlite3.connect(":memory:") as conn:
        initialize_training_schema(conn)
        conn.execute(
            "UPDATE mnemos_schema_registry SET ddl_hash=? WHERE component=?",
            ("sha256:" + "f" * 64, SCHEMA_COMPONENT),
        )
        state = inspect_training_schema(conn)

    assert state.classification == "registry_mismatch"
    assert state.ok is False
