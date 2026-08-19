from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3

from core.application.cognitive_state import CognitiveStateApplicationService
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.belief_revision import BeliefRevisionCommand, BeliefRevisionStore
from core.cognitive.state_contract import canonical_json, sha256_json
from core.cognitive.training_governance import TrainingGovernanceStore
from core.cognitive.training_contract import training_admission_input_hash
from scripts import run_local_gates
import scripts.audit_phase3_cognitive_chain as chain_audit
from scripts.audit_phase3_cognitive_chain import (
    ZERO_BUDGET_METRICS,
    audit_phase3_chain_state,
    audit_phase3_cognitive_chain_static,
)
from tests.unit.cognitive.test_decision_trace import (
    HASH_B,
    _decision_request,
    _principal,
    _record_decision,
    _source,
)
from tests.unit.cognitive.test_phase3_training_intake_reconciliation import (
    _remove_post_contract_intake,
)
from tests.unit.cognitive.test_prediction_ledger import _objective_outcome_request
from tests.unit.cognitive.test_training_governance_store import (
    _initialize_training_projection,
    _mature_clock,
    _objective_training_command,
)


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_phase3_chain_static_audit_owns_required_cross_domain_denominator() -> None:
    report = audit_phase3_cognitive_chain_static(repo_root=REPO_ROOT)

    assert report["schema_version"] == "mnemos.phase3_cognitive_chain_audit.v1"
    assert report["audit_mode"] == "static_only"
    assert report["ok"] is True
    assert set(report["metrics"]) == set(ZERO_BUDGET_METRICS)
    assert set(report["metrics"].values()) == {0}
    assert report["denominators"]["required_chain_tests"] >= 8
    assert report["denominators"]["registered_gate_surfaces"] == 4


def test_phase3_chain_audit_is_required_by_all_gate_surfaces() -> None:
    gate_commands = {name: command for name, command in run_local_gates.GATES}
    assert gate_commands["phase3 cognitive chain"] == [
        "python",
        "scripts/audit_phase3_cognitive_chain.py",
        "--strict",
        "--json",
    ]
    precommit = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    full_score = (REPO_ROOT / "scripts" / "run_full_score_gates.py").read_text(
        encoding="utf-8"
    )
    static_command = (
        "scripts/audit_phase3_cognitive_chain.py --static-only --strict --json"
    )
    assert static_command in precommit
    assert static_command in ci
    assert '"contracts.phase3_cognitive_chain"' in full_score


def test_phase3_chain_state_accepts_one_real_canonical_admission(tmp_path: Path) -> None:
    state, _principal, outcome, target = _objective_training_command(tmp_path)
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    )
    intake = next(
        item
        for item in state.pending_commands("governed_training_admission")
        if item["payload"]["training_target_ref"]["command_id"]
        == target["command_id"]
    )
    governance.process_admission_intake(str(intake["command_id"]))

    report = audit_phase3_chain_state(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
        now=datetime.fromisoformat(outcome.payload["maturity"]["matured_at"]),
    )

    assert report["ok"] is True, report
    assert set(report["metrics"].values()) == {0}
    assert report["denominators"]["objective_attributions"] == 1
    assert report["denominators"]["eligible_training_targets"] == 1
    assert report["denominators"]["admission_intakes"] == 1
    assert report["denominators"]["admitted_samples"] == 1


def test_phase3_chain_static_audit_rejects_belief_purpose_contract_drift(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        chain_audit,
        "DECISION_SNAPSHOT_SOURCE_PURPOSES",
        {"belief_revision": "cognitive_state_read"},
    )

    report = audit_phase3_cognitive_chain_static(repo_root=REPO_ROOT)

    assert report["metrics"]["decision_source_purpose_contract_gap"] == 1
    assert report["ok"] is False


def test_phase3_chain_detects_authorized_belief_missing_from_decision(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    from core.cognitive.state_schema import initialize_cognitive_state_schema

    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    source = _source()
    access = make_cognitive_access_envelope(
        owner_principal_id="mcp:codex:test",
        owner_agent="codex",
        scope_type="project",
        scope_id="mnemos",
        project="mnemos",
        purposes=("belief_read", "cognitive_state_read", "cognitive_state_write"),
        consent_provenance_refs=(str(source["source_id"]),),
        sensitivity="sensitive",
        retention_policy="cognitive_state",
        source_acl_lineage=(HASH_B,),
    )
    BeliefRevisionStore(service.store).revise(
        BeliefRevisionCommand(
            claim="Phase 3 aggregate closure requires a connected cognitive chain.",
            claim_kind="fact",
            scope_type="project",
            scope_id="mnemos",
            source_id=str(source["source_id"]),
            source_revision_id=str(source["source_revision_id"]),
            source_content_hash=str(source["content_hash"]),
            source_access_control=access,
            supporting_evidence=("raw-event-cog036-1#0:64",),
            valid_from="2026-07-17T09:00:00+00:00",
            invalidation_conditions=("Phase 3 chain contract changes",),
            created_at="2026-07-17T09:00:00+00:00",
        ),
        principal=_principal(),
    )
    sealed = _record_decision(service, _decision_request())
    decision_id = str(sealed["decision"]["revision_id"])
    with sqlite3.connect(db_path) as conn:
        payload = json.loads(
            str(
                conn.execute(
                    "SELECT payload_json FROM cognitive_state_revisions "
                    "WHERE revision_id=?",
                    (decision_id,),
                ).fetchone()[0]
            )
        )
        payload["belief_revision_refs"] = []
        conn.execute("DROP TRIGGER cognitive_state_revisions_no_update")
        conn.execute(
            "UPDATE cognitive_state_revisions SET payload_json=?, payload_hash=? "
            "WHERE revision_id=?",
            (canonical_json(payload), sha256_json(payload), decision_id),
        )
        conn.executescript(
            """
            CREATE TRIGGER cognitive_state_revisions_no_update
            BEFORE UPDATE ON cognitive_state_revisions BEGIN
                SELECT RAISE(ABORT, 'cognitive_state_revisions are immutable');
            END;
            """
        )

    report = audit_phase3_chain_state(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
    )

    assert report["metrics"]["decision_belief_candidate_gap"] == 1


def test_phase3_chain_detects_terminal_feedback_without_durable_intake(
    tmp_path: Path,
) -> None:
    state, _principal_value, outcome, _target = _objective_training_command(tmp_path)
    _remove_post_contract_intake(state.db_path)

    report = audit_phase3_chain_state(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
        now=datetime.fromisoformat(outcome.payload["maturity"]["matured_at"]),
    )

    assert report["metrics"]["eligible_feedback_without_admission_intake"] == 1
    assert report["metrics"]["terminal_training_target_without_admission"] == 1
    assert report["metrics"]["mature_training_intake_pending_without_reason"] == 1


def test_phase3_chain_detects_fail_open_immature_admission(tmp_path: Path) -> None:
    state, _principal_value, outcome, target = _objective_training_command(tmp_path)
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    )
    intake = next(
        item
        for item in state.pending_commands("governed_training_admission")
        if item["payload"]["training_target_ref"]["command_id"]
        == target["command_id"]
    )
    receipt = governance.process_admission_intake(str(intake["command_id"]))
    admission = state.revision(receipt.admission_revision_id)
    assert admission is not None
    payload = json.loads(canonical_json(admission.payload))
    payload["temporal_proof"]["outcome_matured_at"] = "2099-01-01T00:00:00+00:00"
    proof = dict(payload["temporal_proof"])
    proof.pop("proof_hash", None)
    payload["temporal_proof"]["proof_hash"] = sha256_json(proof)
    payload["input_set_hash"] = training_admission_input_hash(payload)
    with sqlite3.connect(state.db_path) as conn:
        conn.execute("DROP TRIGGER cognitive_state_revisions_no_update")
        conn.execute(
            "UPDATE cognitive_state_revisions SET payload_json=?, payload_hash=? "
            "WHERE revision_id=?",
            (
                canonical_json(payload),
                sha256_json(payload),
                admission.revision_id,
            ),
        )
        conn.executescript(
            """
            CREATE TRIGGER cognitive_state_revisions_no_update
            BEFORE UPDATE ON cognitive_state_revisions BEGIN
                SELECT RAISE(ABORT, 'cognitive_state_revisions are immutable');
            END;
            """
        )

    report = audit_phase3_chain_state(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
        now=datetime.fromisoformat(outcome.payload["maturity"]["matured_at"]),
    )

    assert report["metrics"]["immature_or_open_prediction_admitted"] == 1


def test_phase3_chain_detects_corrected_outcome_with_old_sample_still_active(
    tmp_path: Path,
) -> None:
    state, _principal_value, old_outcome, target = _objective_training_command(tmp_path)
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    )
    intake = next(
        item
        for item in state.pending_commands("governed_training_admission")
        if item["payload"]["training_target_ref"]["command_id"]
        == target["command_id"]
    )
    admission_receipt = governance.process_admission_intake(
        str(intake["command_id"])
    )
    prediction = state.revision(
        str(old_outcome.payload["prediction_ref"]["revision_id"])
    )
    assert prediction is not None
    request, principal, _observed_at, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
        observed_value="not_useful",
        source_suffix="aggregate-correction",
        observed_hours=2,
        correction_of_revision_id=old_outcome.revision_id,
    )
    corrected = CognitiveStateApplicationService(state).apply_outcome(
        request,
        principal=principal,
        source_authority_catalog=catalog,
    )
    assert corrected["success"] is True
    corrected_outcome = state.current_revision(
        "outcome_measurement",
        old_outcome.object_id,
    )
    assert corrected_outcome is not None

    report = audit_phase3_chain_state(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
        now=datetime.fromisoformat(
            corrected_outcome.payload["maturity"]["matured_at"]
        ),
    )

    assert report["metrics"]["corrected_sample_still_active"] == 1

    run_payload = {
        "state": "applied",
        "admission_refs": [
            {"revision_id": admission_receipt.admission_revision_id}
        ],
    }
    run_revision_id = "cogrev-phase3-dependent-run"
    with sqlite3.connect(state.db_path) as conn:
        source = conn.execute(
            "SELECT source_event_id, source_revision_id, source_content_hash, "
            "redaction_policy, redaction_counts FROM cognitive_state_revisions "
            "WHERE revision_id=?",
            (admission_receipt.admission_revision_id,),
        ).fetchone()
        assert source is not None
        conn.execute(
            """
            INSERT INTO cognitive_state_revisions (
                revision_id, object_type, object_id, schema_version, revision_no,
                source_event_id, source_revision_id, source_content_hash,
                scope_type, scope_id, evidence_refs, evidence_hash,
                payload_json, payload_hash, supersedes_revision_id,
                correction_of_revision_id, admission_state, redaction_policy,
                redaction_counts, created_at
            ) VALUES (?, 'training_run_record', ?, ?, 1, ?, ?, ?,
                      'project', 'mnemos', ?, ?, ?, ?, NULL, NULL,
                      'active', ?, ?, ?)
            """,
            (
                run_revision_id,
                "training-run-phase3-dependent",
                "mnemos.training_run_record.v1",
                source[0],
                source[1],
                source[2],
                canonical_json([admission_receipt.admission_revision_id]),
                sha256_json([admission_receipt.admission_revision_id]),
                canonical_json(run_payload),
                sha256_json(run_payload),
                source[3],
                source[4],
                corrected_outcome.created_at,
            ),
        )
        conn.execute(
            "INSERT INTO cognitive_state_heads("
            "object_type, object_id, revision_id, updated_at"
            ") VALUES ('training_run_record', ?, ?, ?)",
            (
                "training-run-phase3-dependent",
                run_revision_id,
                corrected_outcome.created_at,
            ),
        )

    dependent = audit_phase3_chain_state(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
        now=datetime.fromisoformat(
            corrected_outcome.payload["maturity"]["matured_at"]
        ),
    )
    assert (
        dependent["metrics"]["correction_dependent_run_or_model_not_stale"]
        == 1
    )


def test_phase3_chain_revalidates_intake_receipt_for_existing_admission(
    tmp_path: Path,
) -> None:
    state, _principal_value, outcome, target = _objective_training_command(tmp_path)
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        state,
        database_dir=tmp_path,
        clock=_mature_clock(state),
    )
    intake = next(
        item
        for item in state.pending_commands("governed_training_admission")
        if item["payload"]["training_target_ref"]["command_id"]
        == target["command_id"]
    )
    governance.process_admission_intake(str(intake["command_id"]))
    with sqlite3.connect(state.db_path) as conn:
        conn.execute("DROP TRIGGER cognitive_state_effect_receipts_no_delete")
        conn.execute(
            "DELETE FROM cognitive_state_effect_receipts WHERE command_id=?",
            (intake["command_id"],),
        )
        conn.executescript(
            """
            CREATE TRIGGER cognitive_state_effect_receipts_no_delete
            BEFORE DELETE ON cognitive_state_effect_receipts BEGIN
                SELECT RAISE(ABORT, 'cognitive_state_effect_receipts are immutable');
            END;
            """
        )

    report = audit_phase3_chain_state(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
        now=datetime.fromisoformat(outcome.payload["maturity"]["matured_at"]),
    )

    assert report["metrics"]["training_intake_without_terminal_receipt"] == 1
    assert report["metrics"]["admission_upstream_revision_gap"] == 1


def test_phase3_chain_detects_active_model_head_without_current_applied_run(
    tmp_path: Path,
) -> None:
    _state, _principal_value, _outcome, _target = _objective_training_command(
        tmp_path
    )
    _initialize_training_projection(tmp_path / "mnemos.db")
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        conn.execute(
            "INSERT INTO governed_scorer_model_heads("
            "dimension, model_id, run_revision_id, activated_at"
            ") VALUES ('predictive_delivery', 'model-orphan', "
            "'run-revision-orphan', '2026-07-19T00:00:00+00:00')"
        )

    report = audit_phase3_chain_state(
        database_dir=tmp_path,
        repo_root=REPO_ROOT,
    )

    assert report["metrics"]["stale_model_head_active"] == 1
