from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import sqlite3
from pathlib import Path

import pytest

from core.access_policy import PrincipalEnvelope
from core.app.outcome_recorder import OutcomeRecorder
from core.application.cognitive_state import CognitiveStateApplicationService
from core.cognitive.feedback_attribution import (
    FeedbackAttributionStore,
    UserReactionInput,
)
from core.cognitive.feedback_attribution_audit import audit_feedback_attribution
from core.cognitive.feedback_proposal_gate import (
    build_gated_feedback_target_adapters,
)
from core.cognitive.prediction_ledger import PredictionRecordStore
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_contract import (
    CognitiveStateRevision,
    LocalConsumerCommand,
    canonical_json,
)
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.training_governance import TrainingGovernanceStore
from core.cognitive.training_governance_audit import audit_training_governance
from core.cognitive.training_history_migration import (
    build_training_history_inventory,
    reconcile_training_history,
)
from core.cognitive.training_contract import (
    FEATURE_NAMES,
    TRAINING_ADMISSION_INTAKE_CONTRACT_HASH,
    training_admission_intake_command_key,
    training_fit_input_hash,
    validate_training_admission_intake_payload,
)
from core.scoring.training_schema import initialize_training_schema
from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2
from tests.cognitive_training_chain_fixtures import (
    build_ready_public_admissions,
)
from tests.unit.cognitive.feedback_attribution_fixtures import access_control
from tests.unit.cognitive.test_prediction_ledger import (
    _objective_outcome_request,
    _record_reaction_exposure,
    _route,
    _router,
)


REPO_ROOT = Path(__file__).resolve().parents[3]


def _initialize_training_projection(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        initialize_training_schema(conn)


def _mature_clock(state: CognitiveStateStore):
    outcome = state.current_revisions(object_type="outcome_measurement")[0]
    matured_at = str(outcome.payload["maturity"]["matured_at"])
    return lambda: matured_at


def _objective_training_command(tmp_path: Path):
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
    attribution_receipt = feedback.record_objective_outcome(outcome, principal)
    command = next(
        item
        for item in state.pending_commands("training_evidence")
        if item["revision_id"] == attribution_receipt.attribution_revision_id
    )
    feedback_receipts = tuple(
        feedback.process_command(command_id)
        for command_id in attribution_receipt.command_ids
    )
    feedback_receipt = next(
        receipt
        for receipt in feedback_receipts
        if receipt.target_id == "training_evidence"
    )
    assert feedback_receipt.disposition == "proposal_committed"
    return state, principal, outcome, command


def _objective_training_intake(
    tmp_path: Path,
    *,
    finalize_prediction: bool = True,
    maturity_delay_hours: int = 0,
    competing_causes: tuple[str, ...] = (),
):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, observed_at, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
        maturity_delay_hours=maturity_delay_hours,
        competing_causes=competing_causes,
    )
    result = CognitiveStateApplicationService(state).apply_outcome(
        request,
        principal=principal,
        source_authority_catalog=catalog,
    )
    assert result["success"] is True
    outcome = state.current_revisions(object_type="outcome_measurement")[0]
    if finalize_prediction:
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
    return state, principal, feedback, attribution


def _reaction_principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="user:feedback-test",
        agent="mnemos",
        host_kind="test",
        capability_id="feedback-test",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _principal_for_access(access: dict) -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id=str(access["owner"]["principal_id"]),
        agent=str(access["owner"]["agent"]),
        host_kind="test",
        capability_id="governed-model-test",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({str(access["scope"]["project"])}),
    )


def _reaction_input() -> UserReactionInput:
    return UserReactionInput(
        source_event_id="source-feedback-training",
        source_revision_id="raw-feedback-training",
        source_content_hash="sha256:" + "1" * 64,
        observed_at="2026-07-19T00:00:00+00:00",
        scope_type="session",
        scope_id="session-feedback",
        source_channel="predictive_push",
        subject_ref={"type": "delivery", "id": "delivery-1"},
        kind="accept",
        evidence_refs=("raw-event:feedback#0:8",),
        evidence_content_hashes=("sha256:" + "2" * 64,),
        access_control=access_control(),
        delivery_ref={
            "state": "available",
            "event_id": "delivery-1",
            "event_payload_hash": "sha256:" + "3" * 64,
            "unavailable_reason": "",
        },
        display_ref={
            "state": "available",
            "display_id": "display-1",
            "content_hash": "sha256:" + "4" * 64,
            "unavailable_reason": "",
        },
        exposure_id="exposure-1",
        interface_id="predictive-push-card",
    )


def test_independent_audit_accepts_one_objective_governed_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.cognitive.training_history_migration._runtime_is_active",
        lambda: False,
    )
    state, principal, _outcome, command = _objective_training_command(tmp_path)
    for name in ("mnemos.db", "rule_weight_optimizer.db", "rule_weights.db"):
        sqlite3.connect(tmp_path / name).close()
    inventory = build_training_history_inventory(tmp_path)
    reconcile_training_history(
        database_dir=tmp_path,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "training-history-backup",
        repo_root=REPO_ROOT,
    )
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    )
    intake = next(
        item
        for item in state.pending_commands("governed_training_admission")
        if item["payload"]["training_target_ref"]["command_id"]
        == command["command_id"]
    )
    receipt = governance.process_admission_intake(
        str(intake["command_id"]),
    )

    report = audit_training_governance(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
    )

    assert receipt.status == "committed"
    assert report["ok"] is True
    assert set(report["metrics"].values()) == {0}
    assert report["denominators"]["admissions"] == 1
    assert report["denominators"]["admitted_samples"] == 1
    assert report["denominators"]["terminal_prediction_expected"] == 1
    assert report["denominators"]["terminal_prediction_verified"] == 1


def test_independent_audit_recomputes_terminal_prediction_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.cognitive.training_history_migration._runtime_is_active",
        lambda: False,
    )
    state, _principal, _outcome, command = _objective_training_command(tmp_path)
    for name in ("mnemos.db", "rule_weight_optimizer.db", "rule_weights.db"):
        sqlite3.connect(tmp_path / name).close()
    inventory = build_training_history_inventory(tmp_path)
    reconcile_training_history(
        database_dir=tmp_path,
        expected_inventory_hash=inventory["inventory_hash"],
        expected_object_manifest_hash=inventory["object_manifest_hash"],
        backup_dir=tmp_path / "training-history-backup",
        repo_root=REPO_ROOT,
    )
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    )
    governance.admit_training_evidence(str(command["command_id"]))
    terminal = state.current_revisions(object_type="prediction_record")[0]
    terminal_command = next(
        item
        for item in state.commands_for_revision(terminal.revision_id)
        if item["command_type"] == "project_prediction_terminal"
    )
    baseline = audit_training_governance(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
    )
    assert baseline["metrics"]["training_terminal_prediction_gap"] == 0

    with sqlite3.connect(state.db_path) as conn:
        conn.execute("DROP TRIGGER cognitive_state_effect_receipts_no_update")
        conn.execute(
            "UPDATE cognitive_state_effect_receipts SET after_hash=? "
            "WHERE command_id=?",
            (
                "sha256:" + "f" * 64,
                terminal_command["command_id"],
            ),
        )
        conn.executescript(
            """
            CREATE TRIGGER cognitive_state_effect_receipts_no_update
            BEFORE UPDATE ON cognitive_state_effect_receipts BEGIN
                SELECT RAISE(ABORT, 'cognitive_state_effect_receipts are immutable');
            END;
            """
        )

    report = audit_training_governance(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
    )

    assert report["metrics"]["training_terminal_prediction_gap"] == 1
    assert report["denominators"]["terminal_prediction_verified"] == 0


def test_objective_command_admits_one_sample_with_reciprocal_receipts(
    tmp_path: Path,
) -> None:
    state, principal, outcome, command = _objective_training_command(tmp_path)
    assert command["payload"]["objective_outcome_ref"] == {
        "state": "available",
        "outcome_id": outcome.object_id,
        "revision_id": outcome.revision_id,
        "payload_hash": outcome.payload_hash,
        "unavailable_reason": "",
    }
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    )

    receipt = governance.admit_training_evidence(
        str(command["command_id"]),
    )
    replay = governance.admit_training_evidence(
        str(command["command_id"]),
    )
    admissions = state.current_revisions(object_type="training_admission_record")
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "governed_training_samples",
                "governed_training_sample_actions",
                "governed_training_sample_receipts",
            )
        }

    assert receipt.status == "committed"
    assert replay == receipt
    assert len(admissions) == 1
    assert admissions[0].revision_id == receipt.admission_revision_id
    assert admissions[0].payload["principal_ref"] == {
        "principal_id": principal.principal_id,
        "authorization_ref": (
            "principal-capability:durable-training-admission-intake"
        ),
    }
    assert admissions[0].payload["label"]["observed_value"] == "useful"
    assert set(admissions[0].payload["feature_snapshot"]["values"]) == {
        "causal_assumption_count",
        "confidence_high",
        "confidence_low",
        "confidence_medium",
        "interruption_cost",
        "predicted_useful",
        "route_deliver",
        "source_snapshot_hash_bucket",
        "task_fit_score",
        "trust_score",
        "window_seconds",
    }
    assert counts == {
        "governed_training_samples": 1,
        "governed_training_sample_actions": 1,
        "governed_training_sample_receipts": 1,
    }
    assert state.effect_receipt(receipt.projection_command_id) is not None


def test_outcome_recorder_closes_objective_targets_and_admits_training(
    tmp_path: Path,
) -> None:
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, _observed_at, catalog = _objective_outcome_request(
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
    _initialize_training_projection(tmp_path / "mnemos.db")

    recorded = OutcomeRecorder(
        database_dir=tmp_path,
        governance_clock=_mature_clock(state),
    ).record_objective_outcome(
        outcome,
        principal=principal,
    )
    replay = OutcomeRecorder(
        database_dir=tmp_path,
        governance_clock=_mature_clock(state),
    ).record_objective_outcome(
        outcome,
        principal=principal,
    )

    assert recorded["success"] is True
    assert recorded["training_admission"]["status"] == "committed"
    assert replay["training_admission"] == recorded["training_admission"]
    assert len(recorded["terminal_receipts"]) == 7
    assert len(state.current_revisions(object_type="training_admission_record")) == 1
    assert state.pending_commands("training_evidence") == []
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM governed_training_samples").fetchone() == (1,)


def test_durable_admission_intake_survives_feedback_closure_and_restart(
    tmp_path: Path,
) -> None:
    state, _principal, feedback, attribution = _objective_training_intake(
        tmp_path
    )
    assert attribution.training_admission_command_id
    assert attribution.training_admission_command_id not in attribution.command_ids
    replay = feedback.replay_pending(limit=3)

    assert replay.processed_count == 7
    assert set(replay.command_ids) == set(attribution.command_ids)
    assert state.pending_commands("training_evidence") == []
    pending_intakes = state.pending_commands("governed_training_admission")
    assert [item["command_id"] for item in pending_intakes] == [
        attribution.training_admission_command_id
    ]
    intake_payload = pending_intakes[0]["payload"]
    validate_training_admission_intake_payload(intake_payload)
    assert intake_payload["contract_hash"] == TRAINING_ADMISSION_INTAKE_CONTRACT_HASH
    assert len(intake_payload["required_feedback_commands"]) == 7
    tampered = deepcopy(intake_payload)
    tampered["training_target_ref"]["payload_hash"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError, match="contract mismatch"):
        validate_training_admission_intake_payload(tampered)
    assert state.current_revisions(object_type="training_admission_record") == ()

    _initialize_training_projection(tmp_path / "mnemos.db")
    restarted = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    )
    report = restarted.reconcile_admission_intakes(10)

    assert report.scanned == report.committed == 1
    assert report.deferred == report.failed == report.remaining == 0
    assert len(state.current_revisions(object_type="training_admission_record")) == 1
    assert state.effect_receipt(attribution.training_admission_command_id) is not None
    assert state.pending_commands("governed_training_admission") == []
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM governed_training_samples").fetchone() == (1,)


def test_admission_waits_for_maturity_and_current_measured_prediction(
    tmp_path: Path,
) -> None:
    state, _principal, feedback, attribution = _objective_training_intake(
        tmp_path,
        finalize_prediction=False,
        maturity_delay_hours=2,
    )
    for command_id in attribution.command_ids:
        feedback.process_command(command_id)
    outcome = state.current_revisions(object_type="outcome_measurement")[0]
    sealed_prediction = state.revision(
        str(outcome.payload["prediction_ref"]["revision_id"])
    )
    assert sealed_prediction is not None
    matured_at = datetime.fromisoformat(
        str(outcome.payload["maturity"]["matured_at"])
    )
    now = {"value": matured_at - timedelta(microseconds=1)}
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=lambda: now["value"].isoformat(),
    )

    immature = governance.reconcile_admission_intakes(10)

    assert immature.scanned == immature.deferred == immature.remaining == 1
    assert immature.committed == immature.failed == 0
    assert state.current_revisions(object_type="training_admission_record") == ()
    assert state.effect_receipt(attribution.training_admission_command_id) is None
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM governed_training_samples").fetchone() == (0,)

    PredictionRecordStore(state).finalize(
        sealed_prediction.object_id,
        {},
        matured_at,
    )
    terminal_prediction = state.current_revision(
        "prediction_record",
        sealed_prediction.object_id,
    )
    assert terminal_prediction is not None
    assert terminal_prediction.payload["terminal"]["state"] == "measured"
    now["value"] = matured_at

    mature = governance.reconcile_admission_intakes(10)
    admission = state.current_revisions(object_type="training_admission_record")[0]

    assert mature.scanned == mature.committed == 1
    assert mature.deferred == mature.failed == mature.remaining == 0
    assert admission.payload["prediction_ref"] == {
        "object_id": sealed_prediction.object_id,
        "revision_id": sealed_prediction.revision_id,
        "payload_hash": sealed_prediction.payload_hash,
        "input_hash": sealed_prediction.payload["prediction_input_hash"],
    }
    assert admission.payload["prediction_terminal_ref"] == {
        "object_id": terminal_prediction.object_id,
        "revision_id": terminal_prediction.revision_id,
        "payload_hash": terminal_prediction.payload_hash,
        "terminal_state": "measured",
        "outcome_revision_id": outcome.revision_id,
        "outcome_payload_hash": outcome.payload_hash,
    }
    assert admission.payload["feature_snapshot"]["source_prediction_input_hash"] == (
        sealed_prediction.payload["prediction_input_hash"]
    )


def test_mature_outcome_stays_deferred_while_prediction_head_is_open(
    tmp_path: Path,
) -> None:
    state, _principal, feedback, attribution = _objective_training_intake(
        tmp_path,
        finalize_prediction=False,
    )
    for command_id in attribution.command_ids:
        feedback.process_command(command_id)
    outcome = state.current_revisions(object_type="outcome_measurement")[0]
    matured_at = datetime.fromisoformat(
        str(outcome.payload["maturity"]["matured_at"])
    )
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=lambda: (matured_at + timedelta(seconds=1)).isoformat(),
    )

    report = governance.reconcile_admission_intakes(10)

    assert report.scanned == report.deferred == report.remaining == 1
    assert report.committed == report.failed == 0
    assert state.current_revisions(object_type="training_admission_record") == ()
    assert state.effect_receipt(attribution.training_admission_command_id) is None


def test_confounded_prediction_head_cannot_admit_training(
    tmp_path: Path,
) -> None:
    state, _principal, feedback, attribution = _objective_training_intake(
        tmp_path,
        competing_causes=("parallel rollout",),
    )
    terminal = state.current_revisions(object_type="prediction_record")[0]
    assert terminal.payload["terminal"]["state"] == "confounded"
    for command_id in attribution.command_ids:
        feedback.process_command(command_id)
    _initialize_training_projection(tmp_path / "mnemos.db")
    report = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    ).reconcile_admission_intakes(10)

    assert report.scanned == report.failed == report.remaining == 1
    assert report.committed == report.deferred == 0
    assert state.current_revisions(object_type="training_admission_record") == ()
    assert state.effect_receipt(attribution.training_admission_command_id) is None


@pytest.mark.parametrize("terminal_state", ("censored", "unknown"))
def test_non_measured_prediction_head_cannot_admit_later_outcome(
    tmp_path: Path,
    terminal_state: str,
) -> None:
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    sealed = state.current_revisions(object_type="prediction_record")[0]
    if terminal_state == "unknown":
        _record_reaction_exposure(state, sealed)
    window_end = datetime.fromisoformat(
        str(sealed.payload["evaluation_window"]["ends_at"])
    )
    terminal_receipt = PredictionRecordStore(state).finalize(
        sealed.object_id,
        {},
        window_end,
    )
    assert terminal_receipt.terminal_state == terminal_state
    request, principal, _observed_at, catalog = _objective_outcome_request(
        sealed,
        tmp_path / "raw_events.db",
    )
    result = CognitiveStateApplicationService(state).apply_outcome(
        request,
        principal=principal,
        source_authority_catalog=catalog,
    )
    assert result["success"] is True
    outcome = state.current_revisions(object_type="outcome_measurement")[0]
    feedback = FeedbackAttributionStore(
        state,
        target_adapters=build_gated_feedback_target_adapters(tmp_path),
    )
    attribution = feedback.record_objective_outcome(outcome, principal)
    for command_id in attribution.command_ids:
        feedback.process_command(command_id)
    _initialize_training_projection(tmp_path / "mnemos.db")

    report = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=lambda: window_end.isoformat(),
    ).reconcile_admission_intakes(10)

    assert report.scanned == report.failed == report.remaining == 1
    assert report.committed == report.deferred == 0
    assert state.current_revisions(object_type="training_admission_record") == ()
    assert state.effect_receipt(attribution.training_admission_command_id) is None


def test_training_waits_for_replayed_prediction_correction_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _principal, _feedback, _attribution = _objective_training_intake(
        tmp_path
    )
    old_outcome = state.current_revisions(object_type="outcome_measurement")[0]
    sealed = state.revision(
        str(old_outcome.payload["prediction_ref"]["revision_id"])
    )
    assert sealed is not None
    request, correction_principal, observed_at, catalog = _objective_outcome_request(
        sealed,
        tmp_path / "raw_events.db",
        observed_value="not_useful",
        source_suffix="corrected-terminal-gap",
        observed_hours=2,
        correction_of_revision_id=old_outcome.revision_id,
    )
    service = CognitiveStateApplicationService(state)

    def crash_after_outcome_commit(_outcome_revision_id: str) -> None:
        raise RuntimeError("injected prediction-correction crash")

    monkeypatch.setattr(
        service,
        "_ensure_prediction_correction_receipt",
        crash_after_outcome_commit,
    )
    with pytest.raises(RuntimeError, match="prediction-correction crash"):
        service.apply_outcome(
            request,
            principal=correction_principal,
            source_authority_catalog=catalog,
        )
    corrected = state.current_revision(
        "outcome_measurement",
        old_outcome.object_id,
    )
    assert corrected is not None
    feedback = FeedbackAttributionStore(
        state,
        clock=lambda: observed_at.isoformat(),
        target_adapters=build_gated_feedback_target_adapters(tmp_path),
    )
    attribution = feedback.record_objective_outcome(
        corrected,
        correction_principal,
    )
    for command_id in attribution.command_ids:
        feedback.process_command(command_id)
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=lambda: corrected.payload["maturity"]["matured_at"],
    )

    with pytest.raises(
        ValueError,
        match="current prediction is not exact measured evidence",
    ):
        governance.process_admission_intake(
            attribution.training_admission_command_id
        )

    assert state.current_revisions(object_type="training_admission_record") == ()
    recovery = CognitiveStateApplicationService(
        state
    ).reconcile_outcome_projections()
    assert recovery["committed"] == 1
    assert recovery["failed"] == recovery["remaining"] == 0
    admitted = governance.process_admission_intake(
        attribution.training_admission_command_id
    )
    assert admitted.status == "committed"
    current_terminal = state.current_revision(
        "prediction_record",
        sealed.object_id,
    )
    assert current_terminal is not None
    assert current_terminal.payload["outcome_ref"] == {
        "revision_id": corrected.revision_id,
        "payload_hash": corrected.payload_hash,
    }


def test_tombstoned_prediction_chain_cannot_admit_training(
    tmp_path: Path,
) -> None:
    state, _principal, feedback, attribution = _objective_training_intake(
        tmp_path
    )
    for command_id in attribution.command_ids:
        feedback.process_command(command_id)
    training_target = next(
        command
        for command in state.commands_for_revision(
            attribution.attribution_revision_id
        )
        if command["consumer_id"] == "training_evidence"
    )
    matured_at = _mature_clock(state)()
    plan = state.plan_subject_tombstone(
        request_id="delete-training-prediction-chain",
        scope_kind="session",
        scope_value="prediction-test-session",
        snapshot_ref="snapshot:training-tombstone-test",
    )
    assert plan.status == "committed"
    assert state.current_revisions(object_type="prediction_record") == ()
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=lambda: matured_at,
    )

    with pytest.raises(ValueError, match="source lineage is stale"):
        governance.admit_training_evidence(str(training_target["command_id"]))

    assert state.current_revisions(object_type="training_admission_record") == ()


def test_cog038_audit_excludes_pending_training_admission_intake(
    tmp_path: Path,
) -> None:
    state, _principal, feedback, attribution = _objective_training_intake(
        tmp_path
    )
    for command_id in attribution.command_ids:
        feedback.process_command(command_id)

    report = audit_feedback_attribution(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
    )

    assert report["ok"] is True
    assert set(report["metrics"].values()) == {0}
    assert report["denominators"]["feedback_command_count"] == 7
    assert report["denominators"]["feedback_effect_receipt_count"] == 7
    assert report["denominators"]["pending_feedback_command_count"] == 0
    assert report["denominators"]["current_target_expected_count"] == 7
    assert report["denominators"]["current_target_terminal_count"] == 7
    assert [
        item["command_id"]
        for item in state.pending_commands("governed_training_admission")
    ] == [attribution.training_admission_command_id]


def test_direct_admission_revalidates_intake_provenance(
    tmp_path: Path,
) -> None:
    state, _principal, feedback, attribution = _objective_training_intake(
        tmp_path
    )
    for command_id in attribution.command_ids:
        feedback.process_command(command_id)
    intake = state.command(attribution.training_admission_command_id)
    assert intake is not None
    payload = deepcopy(intake["payload"])
    payload["source_authority_refs"][0] = "source-authority:forged"
    payload["source_authority_refs"] = sorted(payload["source_authority_refs"])
    payload["command_key"] = training_admission_intake_command_key(payload)
    forged = LocalConsumerCommand.create(
        revision_id=str(intake["revision_id"]),
        consumer_id=str(intake["consumer_id"]),
        command_type=str(intake["command_type"]),
        payload=payload,
        created_at=str(intake["created_at"]),
    )
    with sqlite3.connect(state.db_path) as conn:
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='cognitive_state_outbox_no_update'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER cognitive_state_outbox_no_update")
        conn.execute(
            "UPDATE cognitive_state_outbox SET command_id=?, payload_json=?, "
            "payload_hash=? WHERE command_id=?",
            (
                forged.command_id,
                canonical_json(payload),
                forged.payload_hash,
                attribution.training_admission_command_id,
            ),
        )
        conn.execute(str(trigger_sql))

    training_target = next(
        item
        for item in state.commands_for_revision(attribution.attribution_revision_id)
        if item["consumer_id"] == "training_evidence"
    )
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    )

    with pytest.raises(ValueError, match="provenance binding mismatch"):
        governance.admit_training_evidence(str(training_target["command_id"]))

    assert state.current_revisions(object_type="training_admission_record") == ()


def test_intake_receipt_requires_exact_external_projection_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _principal, feedback, attribution = _objective_training_intake(
        tmp_path
    )
    for command_id in attribution.command_ids:
        feedback.process_command(command_id)
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    )
    monkeypatch.setattr(governance, "_apply_projection", lambda _command: None)

    with pytest.raises(RuntimeError, match="projection proof mismatch"):
        governance.process_admission_intake(
            attribution.training_admission_command_id,
        )

    assert state.effect_receipt(attribution.training_admission_command_id) is None
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM governed_training_samples").fetchone() == (0,)


@pytest.mark.parametrize(
    "boundary",
    (
        "before_feedback_target_completion",
        "after_training_evidence_target_receipt",
    ),
)
def test_outcome_entrypoint_crash_keeps_durable_admission_obligation(
    tmp_path: Path,
    boundary: str,
) -> None:
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, _observed_at, catalog = _objective_outcome_request(
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
    _initialize_training_projection(tmp_path / "mnemos.db")

    def crash(name: str) -> None:
        if name == boundary:
            raise RuntimeError("injected outcome-entrypoint crash")

    with pytest.raises(RuntimeError, match="outcome-entrypoint crash"):
        OutcomeRecorder(
            database_dir=tmp_path,
            governance_clock=_mature_clock(state),
        ).record_objective_outcome(
            outcome,
            principal=principal,
            _failpoint=crash,
        )

    pending_intakes = state.pending_commands("governed_training_admission")
    assert len(pending_intakes) == 1
    assert state.current_revisions(object_type="training_admission_record") == ()
    deferred = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    ).reconcile_admission_intakes(10)
    assert deferred.scanned == deferred.deferred == deferred.remaining == 1
    assert deferred.committed == deferred.failed == 0
    feedback = FeedbackAttributionStore(
        state,
        target_adapters=build_gated_feedback_target_adapters(tmp_path),
    )
    for command in tuple(state.pending_commands()):
        if command["command_type"] in {
            "evaluate_feedback_target",
            "neutralize_feedback_effect",
        }:
            feedback.process_command(str(command["command_id"]))

    report = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    ).reconcile_admission_intakes(10)
    assert report.scanned == report.committed == 1
    assert report.deferred == report.failed == report.remaining == 0
    assert len(state.current_revisions(object_type="training_admission_record")) == 1
    assert state.effect_receipt(str(pending_intakes[0]["command_id"])) is not None
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM governed_training_samples").fetchone() == (1,)


@pytest.mark.parametrize(
    "boundary",
    (
        "after_admission_revision_commit",
        "after_scoring_sample_projection",
        "before_admission_intake_terminal_receipt",
    ),
)
def test_admission_intake_crash_replays_only_missing_effects(
    tmp_path: Path,
    boundary: str,
) -> None:
    state, _principal, feedback, attribution = _objective_training_intake(
        tmp_path
    )
    for command_id in attribution.command_ids:
        feedback.process_command(command_id)
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    )

    def crash(name: str) -> None:
        if name == boundary:
            raise RuntimeError("injected admission-intake crash")

    with pytest.raises(RuntimeError, match="admission-intake crash"):
        governance.process_admission_intake(
            attribution.training_admission_command_id,
            _failpoint=crash,
        )

    restarted = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    )
    report = restarted.reconcile_admission_intakes(10)
    assert report.scanned == report.committed == 1
    assert report.deferred == report.failed == report.remaining == 0
    assert len(state.current_revisions(object_type="training_admission_record")) == 1
    assert state.effect_receipt(attribution.training_admission_command_id) is not None
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "governed_training_samples",
                "governed_training_sample_actions",
                "governed_training_sample_receipts",
            )
        }
    assert counts == {
        "governed_training_samples": 1,
        "governed_training_sample_actions": 1,
        "governed_training_sample_receipts": 1,
    }


def test_reaction_only_training_command_is_rejected_without_projection(
    tmp_path: Path,
) -> None:
    state_db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    state = CognitiveStateStore(state_db)
    principal = _reaction_principal()
    feedback = FeedbackAttributionStore(
        state,
        clock=lambda: "2026-07-19T00:00:01+00:00",
        target_adapters=build_gated_feedback_target_adapters(tmp_path),
    )
    recorded = feedback.record_reaction(_reaction_input(), principal)
    command = next(
        item
        for item in state.pending_commands("training_evidence")
        if item["revision_id"] == recorded.attribution_revision_id
    )
    feedback.process_command(str(command["command_id"]))
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
    )

    with pytest.raises(ValueError, match="durable admission intake"):
        governance.admit_training_evidence(str(command["command_id"]))

    assert state.current_revisions(object_type="training_admission_record") == ()
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM governed_training_samples").fetchone() == (0,)


def test_reconcile_pending_recovers_projection_after_post_commit_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, principal, _outcome, command = _objective_training_command(tmp_path)
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    )

    def crash_after_canonical_commit(_command: LocalConsumerCommand) -> None:
        raise OSError("injected projection crash")

    monkeypatch.setattr(governance, "_apply_projection", crash_after_canonical_commit)
    with pytest.raises(OSError, match="projection crash"):
        governance.admit_training_evidence(str(command["command_id"]))

    assert len(state.current_revisions(object_type="training_admission_record")) == 1
    assert len(state.pending_commands("governed_training_projection")) == 1
    recovered = TrainingGovernanceStore(state, database_dir=tmp_path)
    report = recovered.reconcile_pending(10)
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        sample_count = conn.execute("SELECT COUNT(*) FROM governed_training_samples").fetchone()[0]

    assert report.scanned == report.projected == 1
    assert report.failed == report.remaining == 0
    assert sample_count == 1
    assert state.pending_commands("governed_training_projection") == []


def _seed_ready_admissions(
    governance: TrainingGovernanceStore,
    *,
    access_override: dict | None = None,
    scope_override: dict[str, str] | None = None,
) -> tuple[CognitiveStateRevision, ...]:
    return build_ready_public_admissions(
        governance,
        access_override=access_override,
        scope_override=scope_override,
        subject_prefix="unit-training-chain",
    )


def _tamper_state_effect_after_hash(
    state_db: Path,
    command_id: str,
) -> None:
    with sqlite3.connect(state_db) as conn:
        conn.execute("DROP TRIGGER cognitive_state_effect_receipts_no_update")
        conn.execute(
            "UPDATE cognitive_state_effect_receipts SET after_hash=? "
            "WHERE command_id=?",
            ("sha256:" + "f" * 64, command_id),
        )
        conn.executescript(
            """
            CREATE TRIGGER cognitive_state_effect_receipts_no_update
            BEFORE UPDATE ON cognitive_state_effect_receipts BEGIN
                SELECT RAISE(ABORT, 'cognitive_state_effect_receipts are immutable');
            END;
            """
        )


@pytest.mark.parametrize(
    "tamper_kind",
    (
        "intake_receipt",
        "feedback_target_command",
        "feedback_target_receipt",
        "attribution_head",
        "outcome_head",
        "prediction_terminal_head",
        "prediction_terminal_receipt",
        "material_effect_receipt",
        "scoring_projection_receipt",
    ),
)
def test_build_ready_run_revalidates_complete_admission_upstream_before_write(
    tmp_path: Path,
    tamper_kind: str,
) -> None:
    state, _principal, outcome, target_command = _objective_training_command(
        tmp_path
    )
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    )
    intake = next(
        command
        for command in state.pending_commands("governed_training_admission")
        if command["payload"]["training_target_ref"]["command_id"]
        == target_command["command_id"]
    )
    governance.process_admission_intake(str(intake["command_id"]))
    admission = state.current_revisions(
        object_type="training_admission_record"
    )[0]

    if tamper_kind == "intake_receipt":
        _tamper_state_effect_after_hash(
            state.db_path,
            str(intake["command_id"]),
        )
    elif tamper_kind == "feedback_target_command":
        with sqlite3.connect(state.db_path) as conn:
            conn.execute("DROP TRIGGER cognitive_state_outbox_no_update")
            conn.execute(
                "UPDATE cognitive_state_outbox SET payload_hash=? "
                "WHERE command_id=?",
                (
                    "sha256:" + "f" * 64,
                    str(target_command["command_id"]),
                ),
            )
            conn.executescript(
                """
                CREATE TRIGGER cognitive_state_outbox_no_update
                BEFORE UPDATE ON cognitive_state_outbox BEGIN
                    SELECT RAISE(ABORT, 'cognitive_state_outbox is immutable');
                END;
                """
            )
    elif tamper_kind == "feedback_target_receipt":
        _tamper_state_effect_after_hash(
            state.db_path,
            str(target_command["command_id"]),
        )
    elif tamper_kind == "attribution_head":
        with sqlite3.connect(state.db_path) as conn:
            conn.execute(
                "DELETE FROM cognitive_state_heads "
                "WHERE object_type='feedback_attribution_record' "
                "AND revision_id=?",
                (str(target_command["revision_id"]),),
            )
    elif tamper_kind == "outcome_head":
        with sqlite3.connect(state.db_path) as conn:
            conn.execute(
                "DELETE FROM cognitive_state_heads "
                "WHERE object_type='outcome_measurement' AND revision_id=?",
                (outcome.revision_id,),
            )
    elif tamper_kind == "prediction_terminal_head":
        with sqlite3.connect(state.db_path) as conn:
            conn.execute(
                "DELETE FROM cognitive_state_heads "
                "WHERE object_type='prediction_record' AND object_id=?",
                (admission.payload["prediction_terminal_ref"]["object_id"],),
            )
    elif tamper_kind == "prediction_terminal_receipt":
        terminal = state.revision(
            str(admission.payload["prediction_terminal_ref"]["revision_id"])
        )
        assert terminal is not None
        terminal_command = next(
            command
            for command in state.commands_for_revision(terminal.revision_id)
            if command["command_type"] == "project_prediction_terminal"
        )
        _tamper_state_effect_after_hash(
            state.db_path,
            str(terminal_command["command_id"]),
        )
    elif tamper_kind == "material_effect_receipt":
        with sqlite3.connect(state.db_path) as conn:
            row = conn.execute(
                "SELECT command_id FROM cognitive_state_effect_receipts "
                "WHERE target_effect_id=?",
                (admission.payload["material_effect_ref"]["effect_id"],),
            ).fetchone()
        assert row is not None
        _tamper_state_effect_after_hash(state.db_path, str(row[0]))
    else:
        with sqlite3.connect(tmp_path / "mnemos.db") as conn:
            conn.execute(
                "DROP TRIGGER governed_training_sample_receipts_no_update"
            )
            conn.execute(
                "UPDATE governed_training_sample_receipts SET receipt_hash=? "
                "WHERE admission_revision_id=?",
                ("sha256:" + "f" * 64, admission.revision_id),
            )
            conn.executescript(
                """
                CREATE TRIGGER governed_training_sample_receipts_no_update
                BEFORE UPDATE ON governed_training_sample_receipts BEGIN
                    SELECT RAISE(ABORT, 'governed_training_sample_receipts are append-only');
                END;
                """
            )

    with pytest.raises((ValueError, RuntimeError)):
        governance.build_ready_run("predictive_delivery")

    assert state.current_revisions(object_type="training_run_record") == ()
    audit = audit_training_governance(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
    )
    assert audit["metrics"]["training_admission_upstream_gap"] == 1
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM governed_scorer_models"
        ).fetchone() == (0,)


def test_ready_run_seals_then_applies_one_governed_model_without_holdout_fit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    _initialize_training_projection(tmp_path / "mnemos.db")
    state = CognitiveStateStore(state_db)
    clock_values = iter(
        (
            "2026-07-19T04:00:01+00:00",
            "2026-07-19T04:00:02+00:00",
        )
    )
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=lambda: next(clock_values),
    )
    _seed_ready_admissions(governance)
    original_evaluation = governance._evaluation_report  # noqa: SLF001
    durable_seals: list[CognitiveStateRevision] = []

    def evaluate_after_durable_seal(*args, **kwargs):
        if kwargs.get("split") == "holdout":
            current = state.current_revisions(object_type="training_run_record")
            assert len(current) == 1
            assert current[0].payload["state"] == "model_sealed"
            durable_seals.append(current[0])
        return original_evaluation(*args, **kwargs)

    monkeypatch.setattr(governance, "_evaluation_report", evaluate_after_durable_seal)

    sealed = governance.build_ready_run(
        "predictive_delivery",
        "2026-07-19T04:00:00+00:00",
    )
    sealed_revision = governance.state.revision(sealed.run_revision_id)
    assert sealed_revision is not None
    assert sealed_revision.payload["state"] == "sealed"
    assert len(durable_seals) == 1
    assert sealed_revision.supersedes_revision_id == durable_seals[0].revision_id
    assert sealed_revision.payload["dataset_manifest"]["counts"] == {
        "train": 20,
        "validation": 2,
        "holdout": 2,
    }
    assert sealed_revision.payload["fit_input_hash"] == training_fit_input_hash(
        sealed_revision.payload["admission_refs"]
    )
    assert sealed_revision.payload["algorithm"]["selection_input_hash"] == (
        sealed_revision.payload["fit_input_hash"]
    )
    assert datetime.fromisoformat(
        sealed_revision.payload["holdout_report"]["evaluated_after_model_sealed_at"]
    ) > datetime.fromisoformat(sealed_revision.payload["model_artifact"]["sealed_at"])
    assert sealed_revision.payload["bayesian_prior_artifact"]["total_samples"] == 20
    assert sealed_revision.payload["rule_optimizer_artifact"]["sample_count"] == 20
    assert sealed_revision.payload["bayesian_prior_artifact"]["admission_revision_ids"] == [
        item["revision_id"]
        for item in sealed_revision.payload["admission_refs"]
        if item["split"] == "train"
    ]
    assert (
        sealed_revision.payload["rule_optimizer_artifact"]["admission_revision_ids"]
        == sealed_revision.payload["bayesian_prior_artifact"]["admission_revision_ids"]
    )

    applied = governance.apply_run(sealed.run_revision_id)
    replay = governance.apply_run(sealed.run_revision_id)
    batch_replay = governance.build_ready_run("predictive_delivery")
    current = governance.state.current_revision("training_run_record", sealed.run_id)
    assert current is not None
    loaded = governance.load_applied_model(
        applied.run_revision_id,
        _principal_for_access(current.payload["access_control"]),
    )
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        model_count = conn.execute("SELECT COUNT(*) FROM governed_scorer_models").fetchone()[0]
        head = conn.execute(
            "SELECT dimension, model_id, run_revision_id " "FROM governed_scorer_model_heads"
        ).fetchone()
        receipt_count = conn.execute(
            "SELECT COUNT(*) FROM governed_training_run_receipts"
        ).fetchone()[0]
        aux_effects = conn.execute(
            "SELECT effect_kind, COUNT(*) FROM governed_training_aux_effects "
            "GROUP BY effect_kind ORDER BY effect_kind"
        ).fetchall()
        aux_receipts = conn.execute(
            "SELECT effect_kind, status, COUNT(*) FROM governed_training_aux_receipts "
            "GROUP BY effect_kind, status ORDER BY effect_kind, status"
        ).fetchall()

    assert applied.status == "applied"
    assert replay == applied
    assert batch_replay == applied
    assert current.payload["state"] == "applied"
    assert loaded.model_id == applied.model_id
    assert loaded.model_blob_hash == current.payload["model_artifact"]["blob_hash"]
    assert model_count == 1
    assert head == (
        "predictive_delivery",
        applied.model_id,
        applied.run_revision_id,
    )
    assert receipt_count == 3
    assert aux_effects == [("bayesian_prior", 1), ("rule_optimizer", 1)]
    assert aux_receipts == [
        ("bayesian_prior", "committed", 1),
        ("bayesian_prior", "model_sealed", 1),
        ("bayesian_prior", "sealed", 1),
        ("rule_optimizer", "committed", 1),
        ("rule_optimizer", "model_sealed", 1),
        ("rule_optimizer", "sealed", 1),
    ]


@pytest.mark.parametrize(
    ("tamper_kind", "metric"),
    (
        ("model_blob", "model_manifest_hash_mismatch"),
        ("sample_receipt", "training_effect_without_receipt"),
        ("admission_state_receipt_id", "training_effect_without_receipt"),
        ("admission_consumption_metadata", "training_effect_without_receipt"),
        ("run_receipt", "training_effect_without_receipt"),
        ("state_receipt", "training_effect_without_receipt"),
    ),
)
def test_independent_audit_recomputes_model_and_reciprocal_receipts(
    tmp_path: Path,
    tamper_kind: str,
    metric: str,
) -> None:
    state_db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        CognitiveStateStore(state_db),
        database_dir=tmp_path,
        clock=lambda: "2026-07-19T04:05:00+00:00",
    )
    _seed_ready_admissions(governance)
    sealed = governance.build_ready_run("predictive_delivery")
    applied = governance.apply_run(sealed.run_revision_id)
    baseline = audit_training_governance(database_dir=tmp_path, repo_root=REPO_ROOT)
    assert baseline["metrics"][metric] == 0

    if tamper_kind == "model_blob":
        with sqlite3.connect(tmp_path / "mnemos.db") as conn:
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
    elif tamper_kind == "sample_receipt":
        with sqlite3.connect(tmp_path / "mnemos.db") as conn:
            conn.executescript(
                """
                DROP TRIGGER governed_training_sample_receipts_no_update;
                UPDATE governed_training_sample_receipts
                SET receipt_hash='sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'
                WHERE status='committed';
                CREATE TRIGGER governed_training_sample_receipts_no_update
                BEFORE UPDATE ON governed_training_sample_receipts BEGIN
                    SELECT RAISE(ABORT, 'governed_training_sample_receipts are append-only');
                END;
                """
            )
    elif tamper_kind == "admission_state_receipt_id":
        with sqlite3.connect(state_db) as conn:
            conn.execute("DROP TRIGGER cognitive_state_effect_receipts_no_update")
            conn.execute(
                "UPDATE cognitive_state_effect_receipts SET receipt_id=? "
                "WHERE command_id=("
                "SELECT command_id FROM cognitive_state_outbox "
                "WHERE command_type='project_governed_training_sample' "
                "ORDER BY command_id LIMIT 1)",
                ("cogeffect-" + "f" * 32,),
            )
            conn.executescript(
                """
                CREATE TRIGGER cognitive_state_effect_receipts_no_update
                BEFORE UPDATE ON cognitive_state_effect_receipts BEGIN
                    SELECT RAISE(ABORT, 'cognitive_state_effect_receipts are immutable');
                END;
                """
            )
    elif tamper_kind == "admission_consumption_metadata":
        with sqlite3.connect(state_db) as conn:
            conn.execute("DROP TRIGGER cognitive_data_consumptions_no_update")
            conn.execute(
                "UPDATE cognitive_data_consumptions SET metadata='{}' "
                "WHERE consumption_id=("
                "SELECT receipt.consumption_id "
                "FROM cognitive_state_effect_receipts AS receipt "
                "JOIN cognitive_state_outbox AS command "
                "ON command.command_id=receipt.command_id "
                "WHERE command.command_type='project_governed_training_sample' "
                "ORDER BY command.command_id LIMIT 1)",
            )
            conn.executescript(
                """
                CREATE TRIGGER cognitive_data_consumptions_no_update
                BEFORE UPDATE ON cognitive_data_consumptions BEGIN
                    SELECT RAISE(ABORT, 'cognitive_data_consumptions are immutable');
                END;
                """
            )
    elif tamper_kind == "run_receipt":
        with sqlite3.connect(tmp_path / "mnemos.db") as conn:
            conn.executescript(
                """
                DROP TRIGGER governed_training_run_receipts_no_update;
                UPDATE governed_training_run_receipts
                SET receipt_hash='sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'
                WHERE status='committed';
                CREATE TRIGGER governed_training_run_receipts_no_update
                BEFORE UPDATE ON governed_training_run_receipts BEGIN
                    SELECT RAISE(ABORT, 'governed_training_run_receipts are append-only');
                END;
                """
            )
    else:
        with sqlite3.connect(state_db) as conn:
            conn.execute("DROP TRIGGER cognitive_state_effect_receipts_no_update")
            conn.execute(
                "UPDATE cognitive_state_effect_receipts SET after_hash=? WHERE command_id=?",
                (
                    "sha256:" + "f" * 64,
                    applied.projection_command_id,
                ),
            )
            conn.executescript(
                """
                CREATE TRIGGER cognitive_state_effect_receipts_no_update
                BEFORE UPDATE ON cognitive_state_effect_receipts BEGIN
                    SELECT RAISE(ABORT, 'cognitive_state_effect_receipts are immutable');
                END;
                """
            )

    report = audit_training_governance(database_dir=tmp_path, repo_root=REPO_ROOT)

    assert report["metrics"][metric] > baseline["metrics"][metric]
    if tamper_kind == "state_receipt":
        applied_revision = governance.state.revision(applied.run_revision_id)
        assert applied_revision is not None
        with pytest.raises(RuntimeError, match="reciprocal state receipt"):
            governance.load_applied_model(
                applied.run_revision_id,
                _principal_for_access(applied_revision.payload["access_control"]),
            )
    if tamper_kind in {
        "sample_receipt",
        "admission_state_receipt_id",
        "admission_consumption_metadata",
    }:
        applied_revision = governance.state.revision(applied.run_revision_id)
        assert applied_revision is not None
        with pytest.raises(
            RuntimeError,
            match="training admission (projection|consumption) proof",
        ):
            governance.load_applied_model(
                applied.run_revision_id,
                _principal_for_access(applied_revision.payload["access_control"]),
            )


def test_independent_audit_detects_deleted_run_and_auxiliary_receipts(
    tmp_path: Path,
) -> None:
    state_db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        CognitiveStateStore(state_db),
        database_dir=tmp_path,
        clock=lambda: "2026-07-19T04:06:00+00:00",
    )
    _seed_ready_admissions(governance)
    sealed = governance.build_ready_run("predictive_delivery")
    governance.apply_run(sealed.run_revision_id)

    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        conn.executescript(
            """
            DROP TRIGGER governed_training_run_receipts_no_delete;
            DROP TRIGGER governed_training_aux_effects_no_delete;
            DROP TRIGGER governed_training_aux_receipts_no_delete;
            DELETE FROM governed_training_run_receipts;
            DELETE FROM governed_training_aux_effects;
            DELETE FROM governed_training_aux_receipts;
            CREATE TRIGGER governed_training_run_receipts_no_delete
            BEFORE DELETE ON governed_training_run_receipts BEGIN
                SELECT RAISE(ABORT, 'governed_training_run_receipts are append-only');
            END;
            CREATE TRIGGER governed_training_aux_effects_no_delete
            BEFORE DELETE ON governed_training_aux_effects BEGIN
                SELECT RAISE(ABORT, 'governed_training_aux_effects are append-only');
            END;
            CREATE TRIGGER governed_training_aux_receipts_no_delete
            BEFORE DELETE ON governed_training_aux_receipts BEGIN
                SELECT RAISE(ABORT, 'governed_training_aux_receipts are append-only');
            END;
            """
        )

    report = audit_training_governance(database_dir=tmp_path, repo_root=REPO_ROOT)

    assert report["metrics"]["training_effect_without_receipt"] > 0
    assert report["metrics"]["bayesian_update_without_admission"] > 0
    assert report["metrics"]["optimizer_update_without_admission"] > 0


def test_independent_audit_rejects_extra_action_and_receipt_rows(
    tmp_path: Path,
) -> None:
    state_db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        CognitiveStateStore(state_db),
        database_dir=tmp_path,
        clock=lambda: "2026-07-19T04:08:00+00:00",
    )
    _seed_ready_admissions(governance)
    baseline = audit_training_governance(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
    )
    assert baseline["metrics"]["training_effect_without_receipt"] == 0

    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        sample_id, admission_revision_id = conn.execute(
            "SELECT sample_id, admission_revision_id "
            "FROM governed_training_samples ORDER BY sample_id LIMIT 1"
        ).fetchone()
        prior_action_id = conn.execute(
            "SELECT action_id FROM governed_training_sample_actions "
            "WHERE sample_id=? ORDER BY created_at, action_id LIMIT 1",
            (sample_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO governed_training_sample_actions VALUES " "(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "forged-extra-training-action",
                sample_id,
                admission_revision_id,
                "correct",
                "forged_noncanonical_reason",
                prior_action_id,
                "sha256:" + "d" * 64,
                "2000-01-01T00:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO governed_training_sample_receipts VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "forged-extra-training-receipt",
                "forged-noncanonical-command",
                admission_revision_id,
                sample_id,
                "forged-extra-training-action",
                "committed",
                "sha256:" + "a" * 64,
                "sha256:" + "b" * 64,
                '["forged-evidence"]',
                "sha256:" + "c" * 64,
                "2000-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()

    report = audit_training_governance(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
    )

    assert report["metrics"]["training_effect_without_receipt"] > 0


def test_empty_manifest_records_reproducible_insufficient_sample_without_model(
    tmp_path: Path,
) -> None:
    state_db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        CognitiveStateStore(state_db),
        database_dir=tmp_path,
        clock=lambda: "2026-07-19T04:10:00+00:00",
    )

    receipt = governance.build_ready_run("predictive_delivery")
    replay = governance.build_ready_run("predictive_delivery")
    revision = governance.state.revision(receipt.run_revision_id)
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        model_count = conn.execute("SELECT COUNT(*) FROM governed_scorer_models").fetchone()[0]
        head_count = conn.execute("SELECT COUNT(*) FROM governed_scorer_model_heads").fetchone()[0]
        stored_status = conn.execute("SELECT status FROM governed_training_run_receipts").fetchone()

    assert receipt.status == "insufficient_sample"
    assert replay == receipt
    assert revision is not None
    assert revision.payload["dataset_manifest"]["counts"] == {
        "train": 0,
        "validation": 0,
        "holdout": 0,
    }
    assert model_count == head_count == 0
    assert stored_status == ("insufficient_sample",)


def test_project_tombstone_excludes_samples_deactivates_model_and_replays(
    tmp_path: Path,
) -> None:
    state_db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    _initialize_training_projection(tmp_path / "mnemos.db")
    state = CognitiveStateStore(state_db)
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=lambda: "2026-07-19T04:30:00+00:00",
    )
    admissions = _seed_ready_admissions(governance)
    sealed = governance.build_ready_run("predictive_delivery")
    applied = governance.apply_run(sealed.run_revision_id)
    plan = state.plan_subject_tombstone(
        request_id="cog-training-project-delete",
        scope_kind="project",
        scope_value="mnemos",
        snapshot_ref="snapshot://training-project-delete",
    )
    command_id = next(
        command_id
        for command_id in plan.command_ids
        if state.command(command_id)["consumer_id"] == "governed_training_projection"
    )

    result = governance.apply_tombstone_command(command_id)
    replay = governance.apply_tombstone_command(command_id)

    assert replay == result
    assert result["status"] == "applied"
    assert result["sample_count"] == len(admissions)
    assert result["model_count"] == 1
    assert result["deactivated_model_ids"] == (applied.model_id,)
    assert result["remaining_model_head_count"] == 0
    assert len(result["receipt_ids"]) == len(admissions)
    assert state.current_revisions(object_type="training_admission_record") == ()
    assert state.current_revisions(object_type="training_run_record") == ()
    assert state.effect_receipt(command_id) is not None
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM governed_training_sample_actions "
            "WHERE action_type='exclude' AND reason_code='subject_tombstone'"
        ).fetchone() == (len(admissions),)
        assert conn.execute(
            "SELECT COUNT(*) FROM governed_training_sample_receipts "
            "WHERE command_id=? AND status='revoked'",
            (command_id,),
        ).fetchone() == (len(admissions),)
        assert conn.execute("SELECT COUNT(*) FROM governed_scorer_model_heads").fetchone() == (0,)
    with pytest.raises(ValueError, match="not current and applied"):
        governance.load_applied_model(
            applied.run_revision_id,
            _principal_for_access(admissions[0].payload["access_control"]),
        )

    baseline = audit_training_governance(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
    )
    assert baseline["metrics"]["training_effect_without_receipt"] == 0
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        conn.executescript(
            """
            DROP TRIGGER governed_training_sample_receipts_no_update;
            UPDATE governed_training_sample_receipts
            SET receipt_hash='sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'
            WHERE command_id=(
                SELECT command_id FROM governed_training_sample_receipts
                WHERE receipt_id LIKE 'training-sample-tombstone-receipt-%'
                ORDER BY command_id LIMIT 1
            );
            CREATE TRIGGER governed_training_sample_receipts_no_update
            BEFORE UPDATE ON governed_training_sample_receipts BEGIN
                SELECT RAISE(ABORT, 'governed_training_sample_receipts are append-only');
            END;
            """
        )
    tampered = audit_training_governance(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
    )

    assert tampered["metrics"]["training_effect_without_receipt"] > 0


def test_public_corrected_outcome_automatically_corrects_prediction_and_training(
    tmp_path: Path,
) -> None:
    state, _principal, old_outcome, _command = _objective_training_command(tmp_path)
    _initialize_training_projection(tmp_path / "mnemos.db")
    old_intake = state.pending_commands("governed_training_admission")[0]
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    )
    old_admission = governance.process_admission_intake(
        str(old_intake["command_id"]),
    )
    prediction = state.revision(
        str(old_outcome.payload["prediction_ref"]["revision_id"])
    )
    assert prediction is not None
    correction_request, correction_principal, _observed_at, catalog = (
        _objective_outcome_request(
            prediction,
            tmp_path / "raw_events.db",
            observed_value="not_useful",
            source_suffix="public-correction",
            observed_hours=2,
            correction_of_revision_id=old_outcome.revision_id,
        )
    )

    corrected_result = CognitiveStateApplicationService(state).apply_outcome(
        correction_request,
        principal=correction_principal,
        source_authority_catalog=catalog,
    )
    corrected_outcome = state.revision(corrected_result["revision_ids"][0])
    assert corrected_outcome is not None
    terminal = state.current_revision("prediction_record", prediction.object_id)
    assert terminal is not None
    assert terminal.payload["outcome_ref"] == {
        "revision_id": corrected_outcome.revision_id,
        "payload_hash": corrected_outcome.payload_hash,
    }

    recorded = OutcomeRecorder(
        state_db=state.db_path,
        governance_clock=lambda: str(
            corrected_outcome.payload["maturity"]["matured_at"]
        ),
    ).record_objective_outcome(
        corrected_outcome,
        principal=correction_principal,
    )

    assert recorded["training_admission"]["status"] == "committed"
    old_current = state.current_revision(
        "training_admission_record",
        old_admission.admission_id,
    )
    assert old_current is not None
    assert old_current.payload["lifecycle_state"] == "excluded"
    active = [
        revision
        for revision in state.current_revisions(
            object_type="training_admission_record"
        )
        if revision.payload["lifecycle_state"] == "admitted"
    ]
    assert len(active) == 1
    assert active[0].payload["outcome_ref"]["revision_id"] == (
        corrected_outcome.revision_id
    )


def test_correction_without_prior_admission_supersedes_old_pending_work(
    tmp_path: Path,
) -> None:
    state, _principal, _feedback, old_attribution = _objective_training_intake(
        tmp_path
    )
    _initialize_training_projection(tmp_path / "mnemos.db")
    old_outcome = state.current_revisions(
        object_type="outcome_measurement"
    )[0]
    prediction = state.revision(
        str(old_outcome.payload["prediction_ref"]["revision_id"])
    )
    assert prediction is not None
    old_intake_id = old_attribution.training_admission_command_id
    correction_request, correction_principal, _observed_at, catalog = (
        _objective_outcome_request(
            prediction,
            tmp_path / "raw_events.db",
            observed_value="not_useful",
            source_suffix="zero-prior-admission",
            observed_hours=2,
            correction_of_revision_id=old_outcome.revision_id,
        )
    )
    corrected_result = CognitiveStateApplicationService(state).apply_outcome(
        correction_request,
        principal=correction_principal,
        source_authority_catalog=catalog,
    )
    corrected_outcome = state.revision(corrected_result["revision_ids"][0])
    assert corrected_outcome is not None

    recorded = OutcomeRecorder(
        state_db=state.db_path,
        governance_clock=lambda: str(
            corrected_outcome.payload["maturity"]["matured_at"]
        ),
    ).record_objective_outcome(
        corrected_outcome,
        principal=correction_principal,
    )

    assert recorded["training_admission"]["status"] == "committed"
    superseded = state.effect_receipt(old_intake_id)
    assert superseded is not None
    assert superseded["status"] == "rejected"
    assert superseded["reason_code"] == (
        "training_admission_intake_superseded_by_outcome_correction"
    )
    assert state.pending_commands("governed_training_admission") == []
    feedback_audit = audit_feedback_attribution(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
    )
    assert feedback_audit["ok"] is True, {
        key: value
        for key, value in feedback_audit["metrics"].items()
        if value
    }
    active = [
        revision
        for revision in state.current_revisions(
            object_type="training_admission_record"
        )
        if revision.payload["lifecycle_state"] == "admitted"
    ]
    assert len(active) == 1
    assert active[0].payload["outcome_ref"]["revision_id"] == (
        corrected_outcome.revision_id
    )


@pytest.mark.parametrize(
    "boundary",
    (
        "after_correction_pending_commit",
        "after_correction_exclusion_commit",
        "after_training_run_stale_commit",
        "after_training_correction_stale",
        "before_admission_intake_terminal_receipt",
    ),
)
def test_public_correction_crash_replays_each_missing_effect_once(
    tmp_path: Path,
    boundary: str,
) -> None:
    state, _principal, old_outcome, _command = _objective_training_command(
        tmp_path
    )
    _initialize_training_projection(tmp_path / "mnemos.db")
    now = {"value": str(old_outcome.payload["maturity"]["matured_at"])}
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=lambda: now["value"],
    )
    old_intake = state.pending_commands("governed_training_admission")[0]
    old_admission = governance.process_admission_intake(
        str(old_intake["command_id"]),
    )
    old_run = governance.build_ready_run("predictive_delivery")
    assert old_run.status == "insufficient_sample"
    prediction = state.revision(
        str(old_outcome.payload["prediction_ref"]["revision_id"])
    )
    assert prediction is not None
    correction_request, correction_principal, _observed_at, catalog = (
        _objective_outcome_request(
            prediction,
            tmp_path / "raw_events.db",
            observed_value="not_useful",
            source_suffix=boundary,
            observed_hours=2,
            correction_of_revision_id=old_outcome.revision_id,
        )
    )
    corrected_result = CognitiveStateApplicationService(state).apply_outcome(
        correction_request,
        principal=correction_principal,
        source_authority_catalog=catalog,
    )
    corrected_outcome = state.revision(corrected_result["revision_ids"][0])
    assert corrected_outcome is not None
    now["value"] = str(corrected_outcome.payload["maturity"]["matured_at"])
    crashed = False

    def crash(name: str) -> None:
        nonlocal crashed
        if name == boundary and not crashed:
            crashed = True
            raise RuntimeError("injected public correction crash")

    recorder = OutcomeRecorder(
        state_db=state.db_path,
        governance_clock=lambda: now["value"],
    )
    with pytest.raises(RuntimeError, match="public correction crash"):
        recorder.record_objective_outcome(
            corrected_outcome,
            principal=correction_principal,
            _failpoint=crash,
        )
    assert crashed is True

    recovered = OutcomeRecorder(
        state_db=state.db_path,
        governance_clock=lambda: now["value"],
    ).record_objective_outcome(
        corrected_outcome,
        principal=correction_principal,
    )
    replay = recorder.record_objective_outcome(
        corrected_outcome,
        principal=correction_principal,
    )

    assert replay["training_admission"] == recovered["training_admission"]
    old_current = state.current_revision(
        "training_admission_record",
        old_admission.admission_id,
    )
    assert old_current is not None
    assert old_current.payload["lifecycle_state"] == "excluded"
    assert [
        revision.payload["lifecycle_state"]
        for revision in state.revision_chain(
            "training_admission_record",
            old_admission.admission_id,
        )
    ] == ["admitted", "correction_pending", "excluded"]
    active = [
        revision
        for revision in state.current_revisions(
            object_type="training_admission_record"
        )
        if revision.payload["lifecycle_state"] == "admitted"
    ]
    assert len(active) == 1
    assert active[0].payload["outcome_ref"]["revision_id"] == (
        corrected_outcome.revision_id
    )
    stale_run = state.current_revision("training_run_record", old_run.run_id)
    assert stale_run is not None
    assert stale_run.payload["state"] == "stale"
    assert len(
        state.revision_chain("training_run_record", old_run.run_id)
    ) == 2
    assert state.pending_commands("governed_training_admission") == []
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        action_counts = conn.execute(
            "SELECT action_type, COUNT(*) FROM governed_training_sample_actions "
            "GROUP BY action_type ORDER BY action_type"
        ).fetchall()
        sample_count = conn.execute(
            "SELECT COUNT(*) FROM governed_training_samples"
        ).fetchone()[0]
    assert action_counts == [("admit", 2), ("exclude", 1)]
    assert sample_count == 2


def test_corrected_outcome_excludes_old_sample_stales_model_and_requires_rebuild(
    tmp_path: Path,
) -> None:
    state, _principal, old_outcome, command = _objective_training_command(tmp_path)
    _initialize_training_projection(tmp_path / "mnemos.db")
    outcome_time = datetime.fromisoformat(old_outcome.created_at)
    now = {"value": (outcome_time + timedelta(hours=1)).isoformat()}
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=lambda: now["value"],
    )
    intake = next(
        item
        for item in state.pending_commands("governed_training_admission")
        if item["payload"]["training_target_ref"]["command_id"]
        == command["command_id"]
    )
    old_admission = governance.process_admission_intake(
        str(intake["command_id"]),
    )
    old_admission_revision = state.revision(old_admission.admission_revision_id)
    assert old_admission_revision is not None
    _seed_ready_admissions(
        governance,
        access_override=old_admission_revision.payload["access_control"],
        scope_override=old_admission_revision.payload["scope"],
    )
    sealed = governance.build_ready_run("predictive_delivery")
    applied = governance.apply_run(sealed.run_revision_id)
    applied_revision = state.revision(applied.run_revision_id)
    assert applied_revision is not None
    model_principal = _principal_for_access(applied_revision.payload["access_control"])
    assert (
        governance.load_applied_model(
            applied.run_revision_id,
            model_principal,
        ).model_id
        == applied.model_id
    )
    scorer = AdaptiveScorerV2(
        db_path=str(tmp_path / "mnemos.db"),
        governance_state_store=state,
        governance_principal=model_principal,
    )
    assert scorer.apply_governed_run(applied.run_revision_id) == applied.model_id
    assert scorer._bayesian.priors["predictive_delivery"].total_samples == (  # noqa: SLF001
        applied_revision.payload["dataset_manifest"]["counts"]["train"]
    )
    assert set(scorer._governed_rule_weights) == {"predictive_delivery"}  # noqa: SLF001
    prediction = state.revision(str(old_outcome.payload["prediction_ref"]["revision_id"]))
    assert prediction is not None
    first_feature_values = old_admission_revision.payload["feature_snapshot"]["values"]
    initial_score = scorer.score(prediction.payload, ["predictive_delivery"])
    assert initial_score.confidences["predictive_delivery"] > 0
    assert {name: initial_score.features[name] for name in FEATURE_NAMES} == first_feature_values
    correction_request, correction_principal, _observed_at, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
        observed_value="not_useful",
        source_suffix="2",
        observed_hours=2,
        correction_of_revision_id=old_outcome.revision_id,
    )
    corrected_result = CognitiveStateApplicationService(state).apply_outcome(
        correction_request,
        principal=correction_principal,
        source_authority_catalog=catalog,
    )
    assert corrected_result["success"] is True
    corrected_outcome = state.current_revision(
        "outcome_measurement",
        old_outcome.object_id,
    )
    assert corrected_outcome is not None
    now["value"] = (outcome_time + timedelta(hours=2)).isoformat()
    recorder = OutcomeRecorder(
        state_db=state.db_path,
        governance_clock=lambda: now["value"],
    )
    recorded = recorder.record_objective_outcome(
        corrected_outcome,
        principal=correction_principal,
    )
    replay = recorder.record_objective_outcome(
        corrected_outcome,
        principal=correction_principal,
    )
    replacement = recorded["training_admission"]
    old_current = state.current_revision(
        "training_admission_record",
        old_admission.admission_id,
    )
    replacement_revision = state.revision(
        str(replacement["admission_revision_id"])
    )
    stale_run = state.current_revision("training_run_record", applied.run_id)
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        old_actions = conn.execute(
            "SELECT action_type FROM governed_training_sample_actions "
            "WHERE sample_id=? ORDER BY created_at, action_id",
            (old_admission.sample_id,),
        ).fetchall()
        old_model_count = conn.execute("SELECT COUNT(*) FROM governed_scorer_models").fetchone()[0]
        stale_head_count = conn.execute(
            "SELECT COUNT(*) FROM governed_scorer_model_heads"
        ).fetchone()[0]

    assert replay["training_admission"] == replacement
    assert old_current is not None
    assert old_current.payload["lifecycle_state"] == "excluded"
    assert old_current.payload["correction_of_revision_id"] == (old_admission.admission_revision_id)
    pending_revision = state.revision(old_current.supersedes_revision_id)
    assert pending_revision is not None
    assert pending_revision.payload["lifecycle_state"] == "correction_pending"
    assert pending_revision.supersedes_revision_id == old_admission.admission_revision_id
    assert replacement_revision is not None
    assert replacement_revision.payload["lifecycle_state"] == "admitted"
    assert replacement_revision.payload["outcome_ref"]["revision_id"] == (
        corrected_outcome.revision_id
    )
    assert old_actions == [("admit",), ("exclude",)]
    assert stale_run is not None and stale_run.payload["state"] == "stale"
    assert old_model_count == 1
    assert stale_head_count == 0
    with pytest.raises(ValueError, match="not current and applied"):
        governance.load_applied_model(applied.run_revision_id, model_principal)
    assert scorer._ml_score(  # noqa: SLF001
        "predictive_delivery",
        first_feature_values,
    ) == (0.5, 0.0)
    assert "predictive_delivery" not in scorer._models  # noqa: SLF001

    rebuilt = governance.rebuild_stale_dimension("predictive_delivery")
    rebuilt_replay = governance.rebuild_stale_dimension("predictive_delivery")
    rebuilt_run = state.revision(rebuilt.run_revision_id)
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        active_head = conn.execute(
            "SELECT model_id, run_revision_id FROM governed_scorer_model_heads"
        ).fetchone()
        model_count = conn.execute("SELECT COUNT(*) FROM governed_scorer_models").fetchone()[0]

    assert rebuilt.status == "applied"
    assert rebuilt_replay == rebuilt
    assert rebuilt_run is not None
    rebuilt_ids = set(rebuilt_run.payload["dataset_manifest"]["admission_revision_ids"])
    assert old_admission.admission_revision_id not in rebuilt_ids
    assert replacement["admission_revision_id"] in rebuilt_ids
    assert active_head == (rebuilt.model_id, rebuilt.run_revision_id)
    assert model_count == 2
    assert (
        governance.load_applied_model(
            rebuilt.run_revision_id,
            model_principal,
        ).model_id
        == rebuilt.model_id
    )
    assert scorer.apply_governed_run(rebuilt.run_revision_id) == rebuilt.model_id
