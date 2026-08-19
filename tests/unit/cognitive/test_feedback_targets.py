from __future__ import annotations

import json
import sqlite3

import pytest

from core.cognitive.feedback_migration_barrier import (
    FeedbackMigrationInProgress,
    activate_feedback_migration_barrier,
    assert_feedback_writes_enabled,
    deactivate_feedback_migration_barrier,
)
from core.cognitive.feedback_domain_proposal import DomainFeedbackProposalStore
from core.cognitive.feedback_targets import (
    TARGET_DOMAIN_TABLES,
)
from core.cognitive.feedback_proposal_gate import (
    build_gated_feedback_target_adapters,
)
from core.trust.push_decision_gate import GateDecision, GateResult


def _apply_command(target_id: str) -> dict:
    return {
        "schema_version": "mnemos.feedback_target_command.v1",
        "attribution_revision_id": "cogrev-" + "1" * 32,
        "attribution_payload_hash": "sha256:" + "2" * 64,
        "input_set_hash": "sha256:" + "3" * 64,
        "target_id": target_id,
        "eligible": True,
        "exclusion_reason": "",
        "command_key": "feedback-target:" + "4" * 32,
        "effect_kind": "proposal",
        "required_target_ids": [],
    }


def test_target_adapter_commits_only_an_idempotent_proposal_receipt(tmp_path):
    adapter = build_gated_feedback_target_adapters(tmp_path)["training_evidence"]

    first = adapter.apply(_apply_command("training_evidence"))
    replay = adapter.apply(_apply_command("training_evidence"))

    assert first == replay
    assert first.disposition == "proposal_committed"
    assert len(first.decision_trace_refs) == 1
    assert len(first.action_refs) == 1
    assert adapter.verify(first)
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        row = conn.execute(
            "SELECT r.disposition, p.payload_json, r.material_command_id, "
            "r.decision_trace_refs_json, r.action_refs_json "
            "FROM training_feedback_proposal_receipts AS r "
            "JOIN training_feedback_proposals AS p ON p.proposal_id=r.state_id"
        ).fetchone()
    assert row[0] == "proposal_committed"
    assert '"direct_domain_update":false' in row[1]
    assert '"training_admitted":false' in row[1]
    assert row[2].startswith("cogcmd-")
    assert len(json.loads(row[3])) == 1
    assert len(json.loads(row[4])) == 1
    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_revisions "
            "WHERE object_type='decision_trace'"
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_effect_receipts AS e "
            "JOIN cognitive_state_outbox AS o ON o.command_id=e.command_id "
            "WHERE o.command_type='execute_material_action' AND e.status='committed'"
        ).fetchone() == (1,)
    assert (tmp_path / "mnemos.db").is_file()
    assert not (tmp_path / "trust_decisions.db").exists()
    assert not (tmp_path / "reflections.db").exists()


def test_target_adapter_replay_does_not_reauthorize_after_clock_advances(
    tmp_path,
    monkeypatch,
):
    ticks = iter(
        (
            "2026-07-18T00:00:00+00:00",
            "2026-07-18T00:00:01+00:00",
        )
    )
    monkeypatch.setattr(
        "core.cognitive.feedback_proposal_gate."
        "DecisionTraceFeedbackProposalGate._now",
        staticmethod(lambda: next(ticks)),
    )
    adapter = build_gated_feedback_target_adapters(tmp_path)["training_evidence"]

    first = adapter.apply(_apply_command("training_evidence"))
    replay = adapter.apply(_apply_command("training_evidence"))

    assert replay == first


def test_target_adapter_fails_before_state_when_trusted_gate_rejects(
    tmp_path,
    monkeypatch,
):
    adapter = build_gated_feedback_target_adapters(tmp_path)["trust_proposal"]
    monkeypatch.setattr(
        "core.cognitive.feedback_proposal_gate.PushDecisionGate.evaluate",
        lambda _self, _candidate: GateResult(
            decision=GateDecision.REJECT,
            risk_level="high",
            reasons=["injected rejection"],
        ),
    )

    with pytest.raises(PermissionError, match="trusted push gate rejected"):
        adapter.apply(_apply_command("trust_proposal"))

    assert not (tmp_path / "trust_decisions.db").exists()
    assert not (tmp_path / "producer_consumer_ledger.db").exists()


def test_target_adapter_neutralizes_prior_proposal_with_reciprocal_receipt(tmp_path):
    adapter = build_gated_feedback_target_adapters(tmp_path)["policy_proposal"]
    prior = adapter.apply(_apply_command("policy_proposal"))
    command = {
        "schema_version": "mnemos.feedback_neutralization_command.v1",
        "attribution_revision_id": "cogrev-" + "5" * 32,
        "attribution_payload_hash": "sha256:" + "6" * 64,
        "target_id": "policy_proposal",
        "command_key": "feedback-target:" + "7" * 32,
        "prior_target_receipt_ref": prior.target_receipt_ref,
        "prior_after_hash": prior.after_hash,
        "neutralization_kind": "suppress",
    }

    effect = adapter.neutralize(command)

    assert effect.disposition == "suppressed"
    assert effect.before_hash == prior.after_hash
    assert adapter.verify(effect)
    with sqlite3.connect(tmp_path / "policy_patches.db") as conn:
        proposal_table, action_table, receipt_table = TARGET_DOMAIN_TABLES[
            "policy_proposal"
        ]
        assert conn.execute(f"SELECT COUNT(*) FROM {proposal_table}").fetchone() == (1,)
        assert conn.execute(f"SELECT COUNT(*) FROM {action_table}").fetchone() == (1,)
        assert conn.execute(f"SELECT COUNT(*) FROM {receipt_table}").fetchone() == (2,)


def test_feedback_migration_barrier_is_exclusive_hashed_and_owner_bound(tmp_path):
    barrier = activate_feedback_migration_barrier(
        tmp_path,
        inventory_hash="sha256:" + "8" * 64,
        activated_at="2026-07-18T00:00:00+00:00",
    )

    with pytest.raises(FeedbackMigrationInProgress, match="feedback_migration_in_progress"):
        assert_feedback_writes_enabled(tmp_path)
    with pytest.raises(FileExistsError):
        activate_feedback_migration_barrier(
            tmp_path,
            inventory_hash="sha256:" + "8" * 64,
        )
    with pytest.raises(PermissionError, match="owner mismatch"):
        deactivate_feedback_migration_barrier(tmp_path, owner_id="not-active-owner")

    deactivate_feedback_migration_barrier(tmp_path, owner_id=barrier.owner_id)
    assert_feedback_writes_enabled(tmp_path)


def test_domain_feedback_store_rejects_unregistered_sql_identifiers(tmp_path):
    with pytest.raises(ValueError, match="journal is not registered"):
        DomainFeedbackProposalStore(
            database_dir=tmp_path,
            db_file="arbitrary.db",
            target_id="belief_correction_proposal",
            owner_id="arbitrary_owner",
            proposal_table="arbitrary_proposals",
            action_table="arbitrary_actions",
            receipt_table="arbitrary_receipts",
            gate_contract_id="mnemos.arbitrary.v1",
        )
