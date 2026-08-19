from __future__ import annotations

from pathlib import Path
import json
import sqlite3

import pytest

from core.access_policy import PrincipalEnvelope
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.feedback_attribution_audit import audit_feedback_attribution
from core.cognitive.feedback_attribution_static_audit import audit_feedback_static
from core.cognitive.feedback_attribution_reaction_audit import (
    independent_reaction_payload_valid,
)
from core.cognitive.feedback_contract import FEEDBACK_TARGETS, reaction_input_hash
from core.cognitive.feedback_entrypoints import record_reflection_feedback
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.reflection.reflection_store import REFLECTION_OBJECT_PURPOSES
from tests.unit.cognitive.feedback_attribution_fixtures import reaction_payload


REPO_ROOT = Path(__file__).resolve().parents[3]


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="user:audit",
        agent="codex",
        host_kind="test",
        capability_id="feedback-audit-test",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _access() -> dict:
    return make_cognitive_access_envelope(
        owner_principal_id=_principal().principal_id,
        owner_agent="codex",
        scope_type="session",
        scope_id="audit-session",
        session_id="audit-session",
        project="mnemos",
        purposes=REFLECTION_OBJECT_PURPOSES,
        consent_provenance_refs=("reflection:audit",),
        sensitivity="sensitive",
        retention_policy="reflection_retention",
        source_acl_lineage=("sha256:" + "1" * 64,),
        visibility="private",
    )


def test_strict_audit_passes_populated_canonical_feedback_graph(tmp_path: Path):
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")
    result = record_reflection_feedback(
        database_dir=tmp_path,
        reflection_id="reflection-audit",
        feedback_type="inaccurate",
        comment="exact correction",
        record_snapshot={"id": "reflection-audit", "summary": "bounded"},
        access_control=_access(),
        principal=_principal(),
    )

    report = audit_feedback_attribution(database_dir=tmp_path, repo_root=REPO_ROOT)

    assert result["terminal_receipt_count"] == 7
    assert report["ok"] is True
    assert set(report["metrics"].values()) == {0}
    assert report["denominators"]["active_reaction_count"] == 1
    assert report["denominators"]["active_attribution_count"] == 1
    assert report["denominators"]["feedback_effect_receipt_count"] == 7
    assert report["denominators"]["formal_user_entrypoint_expected_count"] == 12
    assert report["denominators"]["formal_user_entrypoint_covered_count"] == 12
    assert report["denominators"]["complete_registry_target_command_count"] == 7
    assert report["denominators"]["terminal_disposition_count"] == 7


def test_strict_audit_reports_uncovered_historical_feedback_object(tmp_path: Path):
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")
    with sqlite3.connect(tmp_path / "feedback_signals.db") as conn:
        conn.executescript(
            """
            CREATE TABLE feedback_signals (
                signal_id TEXT PRIMARY KEY,
                source_event_id TEXT NOT NULL,
                target_ref TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            INSERT INTO feedback_signals VALUES (
                'legacy-1', 'feedback-old', 'delivery-old', '{}'
            );
            """
        )

    report = audit_feedback_attribution(database_dir=tmp_path, repo_root=REPO_ROOT)

    assert report["ok"] is False
    assert report["metrics"]["legacy_feedback_object_uncovered"] == 1
    assert report["denominators"]["historical_feedback_object_count"] == 1


def test_strict_audit_counts_command_receipt_denominator_gap(tmp_path: Path):
    ledger = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(ledger)
    result = record_reflection_feedback(
        database_dir=tmp_path,
        reflection_id="reflection-audit-gap",
        feedback_type="inaccurate",
        comment="exact correction",
        record_snapshot={"id": "reflection-audit-gap", "summary": "bounded"},
        access_control=_access(),
        principal=_principal(),
    )
    command_id = result["command_ids"][0]
    with sqlite3.connect(ledger) as conn:
        conn.execute("DROP TRIGGER cognitive_state_effect_receipts_no_delete")
        conn.execute(
            "DELETE FROM cognitive_state_effect_receipts WHERE command_id=?",
            (command_id,),
        )
        conn.execute(
            """
            CREATE TRIGGER cognitive_state_effect_receipts_no_delete
            BEFORE DELETE ON cognitive_state_effect_receipts BEGIN
                SELECT RAISE(ABORT, 'cognitive_state_effect_receipts are immutable');
            END
            """
        )

    report = audit_feedback_attribution(database_dir=tmp_path, repo_root=REPO_ROOT)

    assert report["ok"] is False
    assert report["metrics"]["feedback_command_without_terminal_receipt"] == 1
    assert report["metrics"]["current_target_terminal_gap"] == 1
    assert report["denominators"]["command_without_receipt_count"] == 1


def test_strict_audit_rejects_correction_head_with_failed_neutralizations(
    tmp_path: Path,
    monkeypatch,
):
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")
    first = record_reflection_feedback(
        database_dir=tmp_path,
        reflection_id="reflection-audit-failed-neutralization",
        feedback_type="inaccurate",
        comment="first exact correction",
        record_snapshot={"id": "reflection-audit-failed-neutralization"},
        access_control=_access(),
        principal=_principal(),
    )

    class FailingNeutralizationAdapter:
        def apply(self, command):  # pragma: no cover - correction is neutralization-only
            raise AssertionError("replacement must not run before neutralization")

        def neutralize(self, command):
            raise ValueError("domain neutralization rejected")

        def verify(self, effect):  # pragma: no cover - no effect is produced
            return False

    monkeypatch.setattr(
        "core.cognitive.feedback_entrypoints.build_gated_feedback_target_adapters",
        lambda _database_dir: {
            target_id: FailingNeutralizationAdapter()
            for target_id in (
                "belief_correction_proposal",
                "delivery_state",
                "persona_proposal",
                "policy_proposal",
                "reflection_evidence",
                "training_evidence",
                "trust_proposal",
            )
        },
    )
    with pytest.raises(ValueError, match="domain neutralization rejected"):
        record_reflection_feedback(
            database_dir=tmp_path,
            reflection_id="reflection-audit-failed-neutralization",
            feedback_type="inaccurate",
            comment="superseding correction",
            record_snapshot={"id": "reflection-audit-failed-neutralization"},
            access_control=_access(),
            principal=_principal(),
            supersedes_event_id=first["feedback_event_id"],
            correction_target_ref="reflection:reflection-audit-failed-neutralization",
            correction_reason="the first correction was incomplete",
        )

    report = audit_feedback_attribution(database_dir=tmp_path, repo_root=REPO_ROOT)

    assert report["ok"] is False
    assert report["metrics"]["correction_effect_without_neutralization_receipt"] == 1
    assert report["metrics"]["feedback_command_without_terminal_receipt"] >= 1


def test_strict_audit_recomputes_attribution_principal_binding(tmp_path: Path):
    ledger = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(ledger)
    result = record_reflection_feedback(
        database_dir=tmp_path,
        reflection_id="reflection-audit-principal",
        feedback_type="insightful",
        comment="bounded",
        record_snapshot={"id": "reflection-audit-principal"},
        access_control=_access(),
        principal=_principal(),
    )
    with sqlite3.connect(ledger) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM cognitive_state_revisions WHERE revision_id=?",
                (result["attribution_revision_id"],),
            ).fetchone()[0]
        )
        payload["access_control"]["owner"]["principal_id"] = "user:cross-owner"
        conn.execute("DROP TRIGGER cognitive_state_revisions_no_update")
        conn.execute(
            "UPDATE cognitive_state_revisions SET payload_json=? WHERE revision_id=?",
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                result["attribution_revision_id"],
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

    report = audit_feedback_attribution(database_dir=tmp_path, repo_root=REPO_ROOT)

    assert report["ok"] is False
    assert report["metrics"]["attribution_principal_binding_gap"] > 0
    assert (
        report["denominators"]["attribution_principal_binding_verified_count"]
        < report["denominators"]["attribution_principal_binding_expected_count"]
    )


def test_strict_audit_rejects_rehashed_malformed_reaction_causal_context(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(ledger)
    result = record_reflection_feedback(
        database_dir=tmp_path,
        reflection_id="reflection-audit-malformed-context",
        feedback_type="insightful",
        comment="bounded",
        record_snapshot={"id": "reflection-audit-malformed-context"},
        access_control=_access(),
        principal=_principal(),
    )
    with sqlite3.connect(ledger) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM cognitive_state_revisions WHERE revision_id=?",
                (result["reaction_revision_id"],),
            ).fetchone()[0]
        )
        payload["display_ref"]["display_id"] = ""
        payload["reaction_input_hash"] = reaction_input_hash(payload)
        assert not independent_reaction_payload_valid(
            payload,
            feedback_targets=(
                "belief_correction_proposal",
                "delivery_state",
                "persona_proposal",
                "policy_proposal",
                "reflection_evidence",
                "training_evidence",
                "trust_proposal",
            ),
        )
        conn.execute("DROP TRIGGER cognitive_state_revisions_no_update")
        conn.execute(
            "UPDATE cognitive_state_revisions SET payload_json=? WHERE revision_id=?",
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                result["reaction_revision_id"],
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

    report = audit_feedback_attribution(database_dir=tmp_path, repo_root=REPO_ROOT)

    assert report["ok"] is False
    assert report["metrics"]["unknown_action_default_positive"] == 1


def test_independent_reaction_validator_rejects_unresolved_available_entity_ref() -> None:
    payload = reaction_payload()
    payload["decision_ref"] = {
        "state": "available",
        "id": "decision-does-not-exist",
        "revision_id": "cogrev-" + "d" * 32,
        "content_hash": "sha256:" + "e" * 64,
        "unavailable_reason": "",
    }
    payload["reaction_input_hash"] = reaction_input_hash(payload)

    assert not independent_reaction_payload_valid(
        payload,
        feedback_targets=FEEDBACK_TARGETS,
        canonical_revisions_by_id={},
    )


def test_strict_audit_rejects_self_asserted_feedback_domain_receipt(tmp_path: Path):
    ledger = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(ledger)
    result = record_reflection_feedback(
        database_dir=tmp_path,
        reflection_id="reflection-audit-forged-receipt",
        feedback_type="inaccurate",
        comment="exact correction",
        record_snapshot={"id": "reflection-audit-forged-receipt"},
        access_control=_access(),
        principal=_principal(),
    )
    command_id = result["command_ids"][0]
    with sqlite3.connect(ledger) as conn:
        row = conn.execute(
            "SELECT revision_id, consumer_id FROM cognitive_state_effect_receipts "
            "WHERE command_id=?",
            (command_id,),
        ).fetchone()
        conn.execute("DROP TRIGGER cognitive_state_effect_receipts_no_update")
        conn.execute(
            "UPDATE cognitive_state_effect_receipts SET evidence_refs=? " "WHERE command_id=?",
            (
                json.dumps(
                    [
                        f"feedback-command:{command_id}",
                        f"feedback-attribution:{row[0]}",
                        f"domain-feedback-receipt:{row[1]}:self-asserted",
                    ]
                ),
                command_id,
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

    report = audit_feedback_attribution(database_dir=tmp_path, repo_root=REPO_ROOT)

    assert report["ok"] is False
    assert report["metrics"]["target_receipt_reciprocity_gap"] > 0


def test_strict_audit_rejects_an_eligible_target_forged_as_skip(tmp_path: Path):
    ledger = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(ledger)
    result = record_reflection_feedback(
        database_dir=tmp_path,
        reflection_id="reflection-audit-forged-skip",
        feedback_type="inaccurate",
        comment="exact correction",
        record_snapshot={"id": "reflection-audit-forged-skip"},
        access_control=_access(),
        principal=_principal(),
    )
    command_id = result["command_ids"][0]
    with sqlite3.connect(ledger) as conn:
        conn.execute("DROP TRIGGER cognitive_state_effect_receipts_no_update")
        conn.execute(
            "UPDATE cognitive_state_effect_receipts SET status='intentional_skip' "
            "WHERE command_id=?",
            (command_id,),
        )
        conn.executescript(
            """
            CREATE TRIGGER cognitive_state_effect_receipts_no_update
            BEFORE UPDATE ON cognitive_state_effect_receipts BEGIN
                SELECT RAISE(ABORT, 'cognitive_state_effect_receipts are immutable');
            END;
            """
        )

    report = audit_feedback_attribution(database_dir=tmp_path, repo_root=REPO_ROOT)

    assert report["ok"] is False
    assert report["metrics"]["feedback_terminal_disposition_gap"] > 0


def test_static_audit_rejects_a_formal_call_hidden_in_dead_code(tmp_path: Path):
    path = tmp_path / "core/app/outcome_recorder.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "def record_reaction(owner, reaction, principal):\n"
        "    if False:\n"
        "        owner.record_reaction(reaction, principal=principal)\n"
        "    return {}\n",
        encoding="utf-8",
    )

    report = audit_feedback_static(tmp_path)

    assert any(
        item.startswith("core/app/outcome_recorder.py:record_reaction:missing")
        for item in report["formal_user_seam_bypasses"]
    )


def test_static_audit_rejects_unbound_context_search_feedback_mixin(tmp_path: Path):
    helper = tmp_path / "core/app/context_search_feedback.py"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        "class ContextSearchFeedbackMixin:\n"
        "    def record_search_click(self):\n"
        "        return record_context_search_feedback()\n"
        "    def record_search_ignore(self):\n"
        "        return record_context_search_feedback()\n",
        encoding="utf-8",
    )
    owner = tmp_path / "core/app/context_search.py"
    owner.write_text("class ContextAwareSearch:\n    pass\n", encoding="utf-8")

    report = audit_feedback_static(tmp_path)

    assert (
        "core/app/context_search.py:ContextAwareSearch:missing:"
        "ContextSearchFeedbackMixin"
    ) in report["formal_user_seam_bypasses"]


def test_static_audit_rejects_a_dummy_quarantine_name_outside_sql(
    tmp_path: Path,
):
    path = tmp_path / "core/scoring/adaptive_scorer_v2.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "_QUARANTINED_FEEDBACK_QUEUE_SQL = 'blocked'\n"
        "class Scorer:\n"
        "    def _count_ready_samples(self, conn):\n"
        "        _ = _QUARANTINED_FEEDBACK_QUEUE_SQL\n"
        "        return conn.execute(\n"
        "            'SELECT COUNT(*) FROM scorer_training_queue'\n"
        "        ).fetchone()\n",
        encoding="utf-8",
    )

    report = audit_feedback_static(tmp_path)

    assert any(
        "_count_ready_samples:unfiltered_scorer_training_queue_reader" in item
        for item in report["legacy_active_readers"]
    )


def test_static_audit_rejects_governed_metric_without_active_admission(
    tmp_path: Path,
):
    path = tmp_path / "daemon/adaptive_service.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "def collect_metrics(adaptive_config, result):\n"
        "    _record_single_metric(\n"
        "        adaptive_config, result, None,\n"
        "        queries=['SELECT COUNT(*) FROM governed_training_samples'],\n"
        "        feature='scoring', metric='feedback_rate',\n"
        "        transform=lambda rows: 0.0, log_debug=None,\n"
        "    )\n",
        encoding="utf-8",
    )

    report = audit_feedback_static(tmp_path)

    assert (
        "daemon/adaptive_service.py:collect_metrics:"
        "unfiltered_training_feedback_rate" in report["legacy_active_readers"]
    )


def test_static_audit_rejects_a_formal_call_after_return(tmp_path: Path):
    path = tmp_path / "core/app/outcome_recorder.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "def record_reaction(owner, reaction, principal):\n"
        "    return {}\n"
        "    owner.record_reaction(reaction, principal=principal)\n",
        encoding="utf-8",
    )

    report = audit_feedback_static(tmp_path)

    assert any(
        item.startswith("core/app/outcome_recorder.py:record_reaction:missing")
        for item in report["formal_user_seam_bypasses"]
    )


def test_static_audit_rejects_a_constant_false_sql_predicate(tmp_path: Path):
    path = tmp_path / "core/kia/rule_scorer.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "ACTIVE_RULE_OUTCOME_SQL = ' AND protected = 1'\n"
        "class Rules:\n"
        "    def get_total_samples(self, conn):\n"
        "        query = 'SELECT COUNT(*) FROM rule_outcomes' + (\n"
        "            ACTIVE_RULE_OUTCOME_SQL if False else ''\n"
        "        )\n"
        "        return conn.execute(query).fetchone()\n",
        encoding="utf-8",
    )

    report = audit_feedback_static(tmp_path)

    assert any(
        "get_total_samples:unfiltered_rule_outcomes" in item
        for item in report["legacy_active_readers"]
    )


def test_static_audit_propagates_termination_from_constant_branch(tmp_path: Path):
    path = tmp_path / "core/app/outcome_recorder.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "def record_reaction(owner, reaction, principal):\n"
        "    if True:\n"
        "        return {}\n"
        "    owner.record_reaction(reaction, principal=principal)\n",
        encoding="utf-8",
    )

    report = audit_feedback_static(tmp_path)

    assert any(
        item.startswith("core/app/outcome_recorder.py:record_reaction:missing")
        for item in report["formal_user_seam_bypasses"]
    )


def test_static_audit_folds_constant_compare_in_sql_predicate(tmp_path: Path):
    path = tmp_path / "core/kia/rule_scorer.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "ACTIVE_RULE_OUTCOME_SQL = ' AND protected = 1'\n"
        "class Rules:\n"
        "    def get_total_samples(self, conn):\n"
        "        query = 'SELECT COUNT(*) FROM rule_outcomes' + (\n"
        "            ACTIVE_RULE_OUTCOME_SQL if 1 == 0 else ''\n"
        "        )\n"
        "        return conn.execute(query).fetchone()\n",
        encoding="utf-8",
    )

    report = audit_feedback_static(tmp_path)

    assert any(
        "get_total_samples:unfiltered_rule_outcomes" in item
        for item in report["legacy_active_readers"]
    )
