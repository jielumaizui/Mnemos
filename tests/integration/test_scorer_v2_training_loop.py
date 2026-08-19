# -*- coding: utf-8 -*-
"""COG-048 governed scorer training and runtime activation integration."""

from __future__ import annotations

from datetime import datetime, timedelta
import sqlite3
from pathlib import Path

import pytest

from core.access_policy import PrincipalEnvelope
from core.application.cognitive_state import CognitiveStateApplicationService
from core.cognitive.feedback_attribution import FeedbackAttributionStore
from core.cognitive.feedback_proposal_gate import (
    build_gated_feedback_target_adapters,
)
from core.cognitive.prediction_ledger import PredictionRecordStore
from core.cognitive.state_contract import (
    CognitiveStateRevision,
)
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.training_contract import (
    FEATURE_NAMES,
)
from core.cognitive.training_governance import TrainingGovernanceStore
from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
from core.scoring.training_schema import initialize_training_schema
from tests.cognitive_training_chain_fixtures import (
    build_ready_public_admissions,
)
from tests.unit.cognitive.test_prediction_ledger import (
    _objective_outcome_request,
    _route,
    _router,
)


def _initialize_training_projection(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        initialize_training_schema(conn)


def _objective_training_command(
    tmp_path: Path,
) -> tuple[CognitiveStateStore, PrincipalEnvelope, CognitiveStateRevision, dict]:
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, observed_at, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
    )
    result = CognitiveStateApplicationService(state).apply_outcome(
        request,
        principal=principal,
        source_authority_catalog=catalog,
    )
    assert result["success"] is True
    outcome = state.current_revisions(object_type="outcome_measurement")[0]
    PredictionRecordStore(state).finalize(
        prediction.object_id,
        {},
        outcome.payload["maturity"]["matured_at"],
    )
    feedback = FeedbackAttributionStore(
        state,
        clock=lambda: observed_at.isoformat(),
        target_adapters=build_gated_feedback_target_adapters(tmp_path),
    )
    attribution = feedback.record_objective_outcome(outcome, principal)
    command = next(
        item
        for item in state.pending_commands("training_evidence")
        if item["revision_id"] == attribution.attribution_revision_id
    )
    receipts = tuple(
        feedback.process_command(command_id)
        for command_id in attribution.command_ids
    )
    closed = next(
        receipt for receipt in receipts if receipt.target_id == "training_evidence"
    )
    assert closed.disposition == "proposal_committed"
    return state, principal, outcome, command


def _principal_for_access(access: dict) -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id=str(access["owner"]["principal_id"]),
        agent=str(access["owner"]["agent"]),
        host_kind="test",
        capability_id="governed-training-integration",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({str(access["scope"]["project"])}),
    )


def _seed_ready_governed_admissions(
    governance: TrainingGovernanceStore,
    *,
    access: dict,
    scope: dict[str, str],
) -> tuple[CognitiveStateRevision, ...]:
    return build_ready_public_admissions(
        governance,
        access_override=access,
        scope_override=scope,
        subject_prefix="integration-training-chain",
    )


def test_objective_outcome_trains_and_activates_exact_governed_model(
    tmp_path: Path,
) -> None:
    state, _source_principal, outcome, training_command = _objective_training_command(
        tmp_path
    )
    _initialize_training_projection(tmp_path / "mnemos.db")
    now = {
        "value": datetime.fromisoformat(
            str(outcome.payload["maturity"]["matured_at"])
        )
    }

    def clock() -> str:
        now["value"] += timedelta(seconds=1)
        return now["value"].isoformat()

    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=clock,
    )
    objective_intake = next(
        command
        for command in state.pending_commands("governed_training_admission")
        if command["payload"]["training_target_ref"]["command_id"]
        == training_command["command_id"]
    )
    objective_receipt = governance.process_admission_intake(
        str(objective_intake["command_id"]),
    )
    objective_revision = state.revision(objective_receipt.admission_revision_id)
    assert objective_revision is not None
    seeded = _seed_ready_governed_admissions(
        governance,
        access=objective_revision.payload["access_control"],
        scope=objective_revision.payload["scope"],
    )

    sealed = governance.build_ready_run("predictive_delivery")
    applied = governance.apply_run(sealed.run_revision_id)
    applied_replay = governance.apply_run(sealed.run_revision_id)
    batch_replay = governance.build_ready_run("predictive_delivery")
    applied_revision = state.revision(applied.run_revision_id)
    assert applied_revision is not None
    model_principal = _principal_for_access(applied_revision.payload["access_control"])
    snapshot = governance.load_applied_model(
        applied.run_revision_id,
        model_principal,
    )

    scorer = AdaptiveScorerV2(
        db_path=str(tmp_path / "mnemos.db"),
        governance_state_store=state,
        governance_principal=model_principal,
    )
    assert scorer.apply_governed_run(applied.run_revision_id) == applied.model_id
    prediction = state.revision(str(outcome.payload["prediction_ref"]["revision_id"]))
    assert prediction is not None
    scored = scorer.score(prediction.payload, ["predictive_delivery"])
    assert scored.confidences["predictive_delivery"] > 0
    assert {name: scored.features[name] for name in FEATURE_NAMES} == (
        objective_revision.payload["feature_snapshot"]["values"]
    )
    feature_names = tuple(str(value) for value in snapshot.model_blob["feature_names"])
    positive = dict(zip(feature_names, snapshot.model_blob["positive_centroid"]))
    negative = dict(zip(feature_names, snapshot.model_blob["negative_centroid"]))
    positive_score, positive_confidence = scorer._ml_score(  # noqa: SLF001
        "predictive_delivery",
        positive,
    )
    negative_score, negative_confidence = scorer._ml_score(  # noqa: SLF001
        "predictive_delivery",
        negative,
    )

    assert applied.status == "applied"
    assert applied_replay == batch_replay == applied
    assert positive_score > negative_score
    assert positive_confidence > 0 and negative_confidence > 0
    assert len(seeded) == 24
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM governed_training_samples").fetchone() == (25,)
        assert conn.execute("SELECT COUNT(*) FROM governed_scorer_models").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM governed_scorer_model_heads").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM governed_training_run_receipts").fetchone() == (
            3,
        )

        conn.executescript(
            """
            DROP TRIGGER governed_scorer_models_no_update;
            UPDATE governed_scorer_models
            SET model_blob_json='{"feature_names":[]}'
            WHERE run_revision_id=(
                SELECT run_revision_id FROM governed_scorer_model_heads
            );
            CREATE TRIGGER governed_scorer_models_no_update
            BEFORE UPDATE ON governed_scorer_models BEGIN
                SELECT RAISE(ABORT, 'governed_scorer_models are append-only');
            END;
            """
        )

    restarted = AdaptiveScorerV2(
        db_path=str(tmp_path / "mnemos.db"),
        governance_state_store=state,
        governance_principal=model_principal,
    )
    with pytest.raises(RuntimeError, match="governed model projection proof mismatch"):
        restarted.apply_governed_run(applied.run_revision_id)


def test_fresh_integration_schema_contains_no_pre_cutover_training_tables(
    tmp_path: Path,
) -> None:
    state_db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    _initialize_training_projection(tmp_path / "mnemos.db")
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

    assert "governed_training_samples" in tables
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
