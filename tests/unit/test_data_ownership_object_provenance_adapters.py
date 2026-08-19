from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.training_governance import TrainingGovernanceStore
from core.ops.action_ledger import (
    ACTION_LEDGER_MATERIAL_EXECUTOR,
    ACTION_LEDGER_MATERIAL_OWNER,
    action_ledger_material_action_input_hash,
)
from core.privacy.data_ownership import DataOwnershipManager, DataSubjectRef
from core.system_contracts import ActionLedger, make_action_record
from tests.cognitive_decision_fixtures import material_action_authorization
from tests.unit.cognitive.test_training_governance_store import (
    _initialize_training_projection,
    _mature_clock,
    _objective_training_command,
    _seed_ready_admissions,
)


class _Config:
    def __init__(self, root: Path):
        self.mnemos_dir = root
        self.database_dir = root
        self.data_dir = root
        self.wiki_dir = root / "wiki"

    def get(self, _key: str, default=None):
        return default

    def vault_dir(self, name: str):
        return self.wiki_dir if name == "mnemos" else self.data_dir / name


def _provenance(session_id: str) -> dict:
    return make_cognitive_access_envelope(
        owner_principal_id="principal:data-ownership-test",
        owner_agent="codex",
        scope_type="session",
        scope_id=session_id,
        session_id=session_id,
        project="mnemos",
        purposes=("data_delete",),
        consent_provenance_refs=("sha256:" + "c" * 64,),
        sensitivity="sensitive",
        retention_policy="test",
        source_acl_lineage=("sha256:" + "d" * 64,),
    )


def _record_material_action(
    ledger: ActionLedger,
    *,
    database_dir: Path,
    target: str,
    provenance: dict,
) -> None:
    record = make_action_record(
        actor="owner-test",
        action_type="data_delete",
        target=target,
        evidence_refs=("sha256:" + "e" * 64,),
        rollback_ref="manual-rollback",
        subject_provenance=provenance,
    )
    authorization = material_action_authorization(
        database_dir,
        action_type="data_delete",
        owner=ACTION_LEDGER_MATERIAL_OWNER,
        executor=ACTION_LEDGER_MATERIAL_EXECUTOR,
        target_ref=target,
        input_hash=action_ledger_material_action_input_hash(record),
    )
    ledger.record(record, material_action=authorization)


def test_data_ownership_adapters_delegate_to_three_object_provenance_owners(
    tmp_path,
    monkeypatch,
):
    from core.mnemos_bus import Event, EventBus

    config = _Config(tmp_path)
    provenance = _provenance("ownership-scope")
    subject = DataSubjectRef("session", "ownership-scope")
    manager = DataOwnershipManager(config)

    ledger = ActionLedger(tmp_path / "action_ledger.db", initialize=True)
    _record_material_action(
        ledger,
        database_dir=tmp_path,
        target="private-action-target",
        provenance=provenance,
    )

    monkeypatch.setattr("core.mnemos_bus.get_config", lambda: config)
    bus = EventBus()
    try:
        bus.publish(
            Event(
                event_type="ownership-test",
                source="test",
                payload={"private": "event"},
                trace_id="ownership-event-trace",
                subject_provenance=provenance,
            )
        )
    finally:
        bus.close()

    state_db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        CognitiveStateStore(state_db),
        database_dir=tmp_path,
    )
    admissions = _seed_ready_admissions(
        governance,
        access_override=provenance,
        scope_override={"type": "session", "id": "ownership-scope"},
    )
    plan = governance.state.plan_subject_tombstone(
        request_id="cog-ownership-scoring-delete",
        scope_kind="session",
        scope_value="ownership-scope",
        snapshot_ref="snapshot://ownership-scoring-delete",
    )
    assert plan.status == "committed"

    event_result = manager._apply_event_metadata_subject_deletion(
        request_id="ownership-event-delete",
        subject=subject,
    )
    action_result = manager._apply_action_ledger_subject_deletion(
        request_id="ownership-action-delete",
        subject=subject,
    )
    scoring_result = manager._apply_scoring_subject_deletion(
        request_id="ownership-scoring-delete",
        subject=subject,
    )

    assert event_result["verified"] is True, "\n".join(
        f"{key}={value}" for key, value in sorted(event_result.items())
    )
    assert action_result["verified"] is True, action_result
    assert scoring_result["verified"] is True, scoring_result
    assert scoring_result["governed_samples_excluded"] == len(admissions)
    assert scoring_result["governed_models_deactivated"] == 0
    assert scoring_result["training_samples_deleted"] == 0
    assert scoring_result["ground_truth_deleted"] == 0


def test_data_ownership_delete_verifies_only_after_three_object_owners_apply(
    tmp_path,
    monkeypatch,
):
    """The public delete seam must expose terminal physical/tombstone effects."""

    from core.mnemos_bus import Event, EventBus

    config = _Config(tmp_path)
    provenance = _provenance("ownership-terminal-scope")
    manager = DataOwnershipManager(config)

    ledger = ActionLedger(tmp_path / "action_ledger.db", initialize=True)
    _record_material_action(
        ledger,
        database_dir=tmp_path,
        target="terminal-action-target",
        provenance=provenance,
    )

    monkeypatch.setattr("core.mnemos_bus.get_config", lambda: config)
    bus = EventBus()
    try:
        bus.publish(
            Event(
                event_type="ownership-terminal-test",
                source="test",
                payload={"private": "event"},
                trace_id="ownership-terminal-event-trace",
                subject_provenance=provenance,
            )
        )
    finally:
        bus.close()

    state_db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    _initialize_training_projection(tmp_path / "mnemos.db")
    governance = TrainingGovernanceStore(
        CognitiveStateStore(state_db),
        database_dir=tmp_path,
    )
    admissions = _seed_ready_admissions(
        governance,
        access_override=provenance,
        scope_override={"type": "session", "id": "ownership-terminal-scope"},
    )

    manager.freeze("session:ownership-terminal-scope")
    snapshot_ref = manager.create_delete_snapshot(
        "session:ownership-terminal-scope", retention_days=1
    ).snapshot_id
    proof = manager.delete(
        "session:ownership-terminal-scope",
        dry_run=False,
        apply=True,
        confirm=True,
        snapshot_ref=snapshot_ref,
    )

    assert proof.verification_results["remaining_unimplemented_domains"] == (), (
        proof.verification_results["cognitive_state"],
        proof.verification_results["scoring"],
        proof.verification_results["observation"],
    )
    assert proof.status == "verified", proof.verification_results
    for domain in ("metadata", "action_ledger", "scoring"):
        assert proof.verification_results[domain]["verified"] is True
    assert proof.verification_results["scoring"]["governed_samples_excluded"] == len(admissions)

    from core.cognitive.tombstone_consumer_coordinator import (
        apply_receipt_only_cognitive_tombstones,
    )

    state = CognitiveStateStore(state_db)
    with sqlite3.connect(state_db) as conn:
        request_id = str(
            conn.execute(
                "SELECT json_extract(payload_json, '$.request_id') "
                "FROM cognitive_state_outbox "
                "WHERE command_type='tombstone_cognitive_state' LIMIT 1"
            ).fetchone()[0]
        )
    replay = apply_receipt_only_cognitive_tombstones(
        state,
        request_id=request_id,
    )
    assert replay["status"] == "existing"
    assert replay["verified"] is True
    assert replay["terminal_count"] == replay["required_count"] == 11


def test_receipt_only_tombstone_consumer_rejects_tampered_source_proof(
    tmp_path,
):
    from core.cognitive.tombstone_consumer_coordinator import (
        apply_receipt_only_cognitive_tombstones,
    )

    state, _principal, _outcome, target = _objective_training_command(
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
        == target["command_id"]
    )
    governance.process_admission_intake(str(intake["command_id"]))
    plan = state.plan_subject_tombstone(
        request_id="cog-receipt-only-tamper",
        scope_kind="session",
        scope_value="prediction-test-session",
        snapshot_ref="snapshot://receipt-only-tamper",
    )
    training_tombstone = next(
        command_id
        for command_id in plan.command_ids
        if state.command(command_id)["consumer_id"]
        == "governed_training_projection"
    )
    governance.apply_tombstone_command(training_tombstone)
    with sqlite3.connect(state.db_path) as conn:
        conn.execute("DROP TRIGGER cognitive_state_effect_receipts_no_update")
        conn.execute(
            "UPDATE cognitive_state_effect_receipts SET after_hash=? "
            "WHERE command_id=?",
            ("sha256:" + "f" * 64, str(target["command_id"])),
        )
        conn.executescript(
            """
            CREATE TRIGGER cognitive_state_effect_receipts_no_update
            BEFORE UPDATE ON cognitive_state_effect_receipts BEGIN
                SELECT RAISE(ABORT, 'cognitive_state_effect_receipts are immutable');
            END;
            """
        )

    with pytest.raises(ValueError, match="independently valid"):
        apply_receipt_only_cognitive_tombstones(
            state,
            request_id=plan.request_id,
        )

    target_tombstone = next(
        command_id
        for command_id in plan.command_ids
        if state.command(command_id)["consumer_id"] == "training_evidence"
    )
    assert state.effect_receipt(target_tombstone) is None
    assert state.tombstone_status(plan.request_id)["verified"] is False
