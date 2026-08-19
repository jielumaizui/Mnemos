"""Tests for the narrow current Raw-to-Observation edge reconciler."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from core.cognitive.models import Dimension, Observation, ObservationType, SourceType
from core.cognitive.observation_store import ObservationStore
from core.ops.cognitive_readiness_lineage import observation_lineage_metric
from core.sync_framework.raw_event_store import (
    RawEventStore,
    canonical_observation_text,
)
from scripts.reconcile_observation_provenance_edges import (
    ObservationProvenanceReconcileError,
    inspect,
    reconcile,
)


def _visible_length(user_content: str, assistant_content: str) -> int:
    return len(
        canonical_observation_text(
            {
                "user_content": user_content,
                "assistant_content": assistant_content,
            }
        )
    )


def test_reconciler_removes_only_invalid_current_edges_and_repairs_metrics(tmp_path):
    raw_db = tmp_path / "raw_events.db"
    observations_db = tmp_path / "observations.db"
    store = RawEventStore(db_path=raw_db)
    try:
        old_revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="cog-027-reconcile",
            turn_number=1,
            user_content="old user",
            assistant_content="old assistant",
        )
        # This historical edge is deliberately invalid, but is outside the
        # current revision denominator and must remain immutable history.
        store.record_provenance_edge(
            source_revision_id=old_revision_id,
            span_start=0,
            span_end=1,
            consumer_type="observation",
            consumer_id="stale-missing-observation",
        )
        current_user = "current user"
        current_assistant = "current assistant"
        current_revision_id = store.upsert_turn(
            source_agent="codex",
            session_id="cog-027-reconcile",
            turn_number=1,
            user_content=current_user,
            assistant_content=current_assistant,
        )
        assert current_revision_id != old_revision_id

        observation_store = ObservationStore(db_path=str(observations_db))
        observation = Observation(
            id="current-valid-observation",
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"state": "valid"},
            source_type=SourceType.RAW,
            source_id=current_revision_id,
        )
        assert observation_store.save(observation) == "inserted"
        visible_length = _visible_length(current_user, current_assistant)
        store.record_provenance_edge(
            source_revision_id=current_revision_id,
            span_start=0,
            span_end=visible_length,
            consumer_type="observation",
            consumer_id=observation.id,
        )
        store.record_provenance_edge(
            source_revision_id=current_revision_id,
            span_start=0,
            span_end=visible_length,
            consumer_type="observation",
            consumer_id="discarded-batch-candidate",
        )

        dry_run = inspect(raw_db, observations_db)
        assert dry_run["status"] == "reconciliation_required"
        assert dry_run["invalid_current_edge_count"] == 1
        assert dry_run["current_observation_edge_count"] == 2
        assert dry_run["ok"] is True
        assert "discarded-batch-candidate" not in str(dry_run)
        with sqlite3.connect(raw_db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM raw_provenance_edges").fetchone()[0] == 3

        with pytest.raises(ObservationProvenanceReconcileError, match="backup_directory_required"):
            reconcile(raw_db, observations_db, apply=True)

        result = reconcile(
            raw_db,
            observations_db,
            apply=True,
            backup_dir=tmp_path / "backups",
        )

        assert result["status"] == "clean"
        assert result["deleted_edges"] == 1
        assert result["recomputed_metrics"] == 1
        assert result["after"]["invalid_current_edge_count"] == 0
        assert list((tmp_path / "backups").glob("*.sqlite"))
        with sqlite3.connect(raw_db) as conn:
            retained = conn.execute(
                """
                SELECT source_revision_id, consumer_id
                FROM raw_provenance_edges
                ORDER BY source_revision_id, consumer_id
                """
            ).fetchall()
            assert retained == [
                (current_revision_id, observation.id),
                (old_revision_id, "stale-missing-observation"),
            ]
            reference_count = conn.execute(
                "SELECT reference_count FROM raw_metrics"
            ).fetchone()[0]
        assert reference_count == 2

        metric = observation_lineage_metric(
            raw_db,
            observations_db,
            freshness_window_seconds=3600,
            now=datetime.now(timezone.utc),
        )
        assert metric["coverage_ratio"] == 1.0
        assert metric["invalid_edge_count"] == 0
    finally:
        store.close()
