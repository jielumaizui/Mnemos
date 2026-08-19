# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from core.cognitive.policy_patch import (
    PolicyPatchProposeEffectOracle,
    PolicyPatchOptions,
    PolicyPatchStore,
    policy_patch_proposal_binding,
)
from core.cognitive.material_effect_ledger import recover_pending_target_effects
from tests.cognitive_decision_fixtures import policy_patch_proposal_authorization
from tests.cognitive_decision_fixtures import policy_patch_feedback_authorization
from tests.cognitive_decision_fixtures import policy_patch_reconcile_authorization


class _AuthorizedPolicyPatchStore(PolicyPatchStore):
    def propose(self, lesson, *, material_action=None):
        binding = policy_patch_proposal_binding(lesson, self.options)
        authorization = material_action
        if authorization is None and binding is not None:
            authorization = policy_patch_proposal_authorization(
                self.options.database_dir,
                lesson=dict(lesson),
                options=self.options,
            )
        return super().propose(lesson, material_action=authorization)

    def record_feedback(
        self,
        patch_id,
        *,
        outcome,
        evidence=None,
        source_event_id="",
        material_action=None,
    ):
        if source_event_id:
            with sqlite3.connect(str(self.options.db_path)) as conn:
                existing = conn.execute(
                    "SELECT 1 FROM policy_patch_feedback WHERE source_event_id=?",
                    (source_event_id,),
                ).fetchone()
            if existing is not None:
                return super().record_feedback(
                    patch_id,
                    outcome=outcome,
                    evidence=evidence,
                    source_event_id=source_event_id,
                )
        authorization = material_action or policy_patch_feedback_authorization(
            self.options.database_dir,
            patch_id=patch_id,
            outcome=outcome,
            evidence=dict(evidence or {}),
            source_event_id=source_event_id,
        )
        return super().record_feedback(
            patch_id,
            outcome=outcome,
            evidence=evidence,
            source_event_id=source_event_id,
            material_action=authorization,
        )

    def reconcile_trigger_terms(self, *, apply=False, material_action=None):
        authorization = material_action
        if apply and authorization is None:
            preview = super().reconcile_trigger_terms(apply=False)
            if preview["changes"]:
                authorization = policy_patch_reconcile_authorization(
                    self.options.database_dir,
                    changes=preview["changes"],
                )
        return super().reconcile_trigger_terms(
            apply=apply,
            material_action=authorization,
        )


def _store(tmp_path, *, authorized=True, **overrides):
    options = PolicyPatchOptions(
        database_dir=tmp_path,
        db_path=tmp_path / "policy_patches.db",
        ttl_days=14,
        min_confidence=0.5,
        max_active=5,
    )
    if overrides:
        options = replace(options, **overrides)
    store_type = _AuthorizedPolicyPatchStore if authorized else PolicyPatchStore
    return store_type(options=options)


@pytest.mark.no_canonical_material_actions
def test_policy_patch_proposal_fails_closed_without_material_authorization(
    tmp_path,
):
    store = _store(tmp_path, authorized=False)

    with pytest.raises(PermissionError, match="material-action authorization"):
        store.propose(
            {
                "summary": "This patch would change later system behavior.",
                "task_type": "coding",
                "trigger": "decision trace",
                "confidence": 0.9,
            }
        )

    assert store.active_for(task_type="coding", context="decision trace") == []


@pytest.mark.no_canonical_material_actions
def test_policy_patch_feedback_and_reconcile_fail_closed_without_authorization(
    tmp_path,
):
    authorized = _store(tmp_path)
    patch = authorized.propose(
        {
            "source_type": "reflection",
            "source_id": "reflection-material-guard",
            "summary": "Keep only the bounded deployment trigger.",
            "trigger_keywords": ["deploy", "generated explanation"],
            "confidence": 0.9,
            "metadata": {"key_points": ["generated explanation"]},
        }
    )
    raw = PolicyPatchStore(options=authorized.options)

    with pytest.raises(PermissionError, match="material-action authorization"):
        raw.record_feedback(
            patch.patch_id,
            outcome="harmful",
            source_event_id="feedback-without-decision",
        )
    with pytest.raises(PermissionError, match="material-action authorization"):
        raw.reconcile_trigger_terms(apply=True)

    assert raw.get_patch(patch.patch_id)["status"] == "active"


def test_policy_patch_propose_and_active_for_task(tmp_path):
    store = _store(tmp_path)
    patch = store.propose(
        {
            "source_type": "layer5_experience",
            "source_id": "exp-1",
            "task_type": "coding",
            "subtype": "fix",
            "trigger_keywords": ["schema migration"],
            "summary": "Schema migrations need a rollback command.",
            "severity": "high",
            "confidence": 0.9,
            "evidence_refs": ["raw-1", "recap-1"],
        }
    )

    active = store.active_for(
        task_type="coding",
        subtype="fix",
        context="working on schema migration",
    )

    assert [item.patch_id for item in active] == [patch.patch_id]
    assert active[0].content == "Schema migrations need a rollback command."
    assert {"raw-1", "recap-1"}.issubset(active[0].evidence_refs)
    assert any(
        ref.startswith("decision-revision:") for ref in active[0].evidence_refs
    )


def test_policy_patch_recovers_target_commit_without_duplicate(
    tmp_path,
    monkeypatch,
):
    import core.cognitive.policy_patch as policy_module

    store = _store(tmp_path, authorized=False)
    lesson = {
        "source_type": "layer5_experience",
        "source_id": "crash-recovery-policy",
        "task_type": "coding",
        "subtype": "fix",
        "trigger_keywords": ["decision trace recovery"],
        "summary": "Recover the exact committed patch without proposing it twice.",
        "confidence": 0.9,
        "evidence_refs": ["raw:crash-recovery-policy"],
    }
    authorization = policy_patch_proposal_authorization(
        tmp_path,
        lesson=lesson,
        options=store.options,
    )
    original = policy_module.recover_recorded_target_effect
    crashed = False

    def crash_after_target(auth, oracle):
        nonlocal crashed
        if not crashed and oracle.observe(auth.permit) is not None:
            crashed = True
            raise OSError("crash after policy target commit")
        return original(auth, oracle)

    monkeypatch.setattr(
        policy_module,
        "recover_recorded_target_effect",
        crash_after_target,
    )
    with pytest.raises(OSError, match="after policy target commit"):
        store.propose(lesson, material_action=authorization)

    monkeypatch.setattr(
        policy_module,
        "recover_recorded_target_effect",
        original,
    )
    recovered = store.propose(lesson, material_action=authorization)

    with sqlite3.connect(store.options.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM policy_patches").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM material_target_effects"
        ).fetchone()[0] == 1
    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_effect_receipts"
        ).fetchone()[0] == 1
    assert recovered.patch_id


def test_policy_patch_replay_rejects_target_journal_receipt_drift(tmp_path):
    store = _store(tmp_path, authorized=False)
    lesson = {
        "source_type": "layer5_experience",
        "source_id": "corrupt-target-journal",
        "task_type": "coding",
        "subtype": "fix",
        "trigger_keywords": ["reciprocal target evidence"],
        "summary": "Reject a target journal that drifted from its receipt.",
        "confidence": 0.9,
        "evidence_refs": ["raw:corrupt-target-journal"],
    }
    authorization = policy_patch_proposal_authorization(
        tmp_path,
        lesson=lesson,
        options=store.options,
    )
    store.propose(lesson, material_action=authorization)

    with sqlite3.connect(store.options.db_path) as conn:
        conn.execute(
            "UPDATE material_target_effects SET after_hash=? WHERE command_id=?",
            ("sha256:" + "f" * 64, authorization.permit.command_id),
        )

    with pytest.raises(RuntimeError, match="does not match its terminal receipt"):
        recover_pending_target_effects(
            state_db_path=tmp_path / "producer_consumer_ledger.db",
            oracle=PolicyPatchProposeEffectOracle(store.options.db_path),
        )

    with pytest.raises(RuntimeError, match="does not match its terminal receipt"):
        store.propose(lesson, material_action=authorization)


def test_policy_patch_matches_any_configured_trigger(tmp_path):
    store = _store(tmp_path)
    patch = store.propose(
        {
            "summary": "Run verify_installation.py after config edits.",
            "task_type": "coding",
            "trigger_keywords": ["配置修改", "verify_installation.py"],
            "confidence": 0.9,
        }
    )

    active = store.active_for(
        task_type="coding",
        context="before final answer, verify_installation.py still needs to run",
    )

    assert [item.patch_id for item in active] == [patch.patch_id]


def test_policy_patch_never_matches_trigger_from_its_own_content(tmp_path):
    store = _store(tmp_path)
    store.propose(
        {
            "summary": "Redis migration guidance that contains redis in the patch body.",
            "task_type": "general",
            "trigger": "redis",
            "confidence": 0.9,
        }
    )

    assert store.active_for(
        task_type="debugging",
        context="unrelated policy relevance regression",
    ) == []


def test_policy_patch_ascii_trigger_requires_token_boundary(tmp_path):
    store = _store(tmp_path)
    store.propose(
        {
            "summary": "Review time allocation before committing.",
            "task_type": "general",
            "trigger": "time",
            "confidence": 0.9,
        }
    )

    assert store.active_for(
        task_type="debugging",
        context="repair the runtime projection",
    ) == []


def test_policy_patch_rejects_generated_explanation_as_trigger_term(tmp_path):
    store = _store(tmp_path)
    explanation = (
        "证据显示当前决策信号很多，同时 assistant output contains common fix words, "
        "but this generated explanation is not a bounded task trigger and must not match."
    )
    store.propose(
        {
            "summary": "Unrelated behavioral guidance.",
            "task_type": "general",
            "trigger_keywords": ["long_term_plan", explanation],
            "confidence": 0.9,
        }
    )

    assert store.active_for(
        task_type="debugging",
        subtype="policy_patch_relevance",
        context="reproduce and fix an unrelated matcher bug",
    ) == []


def test_policy_patch_proposal_sanitizes_generated_trigger_explanations(tmp_path):
    store = _store(tmp_path)
    patch = store.propose(
        {
            "summary": "Use rollback for schema changes.",
            "task_type": "coding",
            "trigger_keywords": [
                "schema migration",
                "This generated explanation is intentionally much longer than a stable "
                "activation term and therefore cannot become retrieval input for a patch.",
                "schema migration",
            ],
            "confidence": 0.9,
        }
    )

    assert patch.trigger == "schema migration"


def test_policy_patch_rejects_short_generated_sentence_trigger(tmp_path):
    store = _store(tmp_path)

    assert store.propose(
        {
            "summary": "Generated guidance.",
            "trigger": "This is a generated explanation, not a trigger.",
            "confidence": 0.9,
        }
    ) is None


def test_policy_patch_project_scope_requires_explicit_matching_scope(tmp_path):
    store = _store(tmp_path)
    patch = store.propose(
        {
            "source_id": "project-a-policy",
            "summary": "Project A schema rollback guidance.",
            "task_type": "coding",
            "scope": "project-a",
            "trigger": "schema rollback",
            "confidence": 0.9,
        }
    )

    assert store.active_for(
        task_type="coding", context="schema rollback"
    ) == []
    assert store.active_for(
        task_type="coding", context="schema rollback", scope="project-b"
    ) == []
    assert [
        item.patch_id
        for item in store.active_for(
            task_type="coding", context="schema rollback", scope="project-a"
        )
    ] == [patch.patch_id]


def test_policy_patch_reconciles_legacy_trigger_terms_without_inventing_terms(tmp_path):
    store = _store(tmp_path)
    keep = store.propose(
        {
            "source_id": "keep",
            "summary": "Keep a bounded trigger.",
            "trigger": "attention",
            "confidence": 0.9,
        }
    )
    review = store.propose(
        {
            "source_id": "review",
            "summary": "Legacy record that will lose every invalid trigger.",
            "trigger": "manual",
            "confidence": 0.9,
        }
    )
    explanation = "x" * 80
    with sqlite3.connect(tmp_path / "policy_patches.db") as conn:
        conn.execute(
            "UPDATE policy_patches SET trigger=? WHERE patch_id=?",
            (json.dumps(["attention", explanation]), keep.patch_id),
        )
        conn.execute(
            "UPDATE policy_patches SET trigger=? WHERE patch_id=?",
            (explanation, review.patch_id),
        )

    preview = store.reconcile_trigger_terms(apply=False)
    with sqlite3.connect(tmp_path / "policy_patches.db") as conn:
        before = conn.execute(
            "SELECT trigger, status FROM policy_patches WHERE patch_id=?",
            (review.patch_id,),
        ).fetchone()
    applied = store.reconcile_trigger_terms(apply=True)

    assert preview["changed"] == 2
    assert before == (explanation, "active")
    assert applied["changed"] == 2
    assert applied["moved_to_review"] == 1
    assert store.get_patch(keep.patch_id)["trigger"] == "attention"
    assert store.get_patch(review.patch_id)["status"] == "review"
    assert store.active_for(task_type="general", context="attention")[0].patch_id == keep.patch_id


def test_policy_patch_reconciliation_removes_reflection_key_points(tmp_path):
    store = _store(tmp_path)
    generated_key_point = "决策前验证回滚路径"
    patch = store.propose(
        {
            "source_type": "reflection",
            "source_id": "reflection-with-generated-key-point",
            "summary": "Keep stable dimensions only.",
            "trigger_keywords": ["decisions", generated_key_point],
            "confidence": 0.9,
            "metadata": {"key_points": [generated_key_point]},
        }
    )

    preview = store.reconcile_trigger_terms(apply=False)
    applied = store.reconcile_trigger_terms(apply=True)

    assert preview["changed"] == 1
    assert applied["removed_term_count"] == 1
    assert store.get_patch(patch.patch_id)["trigger"] == "decisions"


def test_policy_patch_context_match_is_explained_deduped_and_budgeted(tmp_path):
    store = _store(tmp_path, max_active=1)
    first = store.propose(
        {
            "source_id": "reflection-1",
            "summary": "First paraphrase about a schema migration rollback.",
            "task_type": "coding",
            "subtype": "migration",
            "trigger_keywords": ["schema migration", "rollback"],
            "severity": "high",
            "confidence": 0.95,
        }
    )
    store.propose(
        {
            "source_id": "reflection-2",
            "summary": "Second paraphrase about migration safety.",
            "task_type": "coding",
            "subtype": "migration",
            "trigger_keywords": ["rollback", "schema migration", "database"],
            "severity": "medium",
            "confidence": 0.9,
        }
    )

    active = store.active_for(
        task_type="coding",
        subtype="migration",
        context="prepare schema migration rollback",
    )

    assert [item.patch_id for item in active] == [first.patch_id]
    assert active[0].metadata["match_source"] == "current_context"
    assert active[0].metadata["matched_triggers"] == ["schema migration", "rollback"]
    assert active[0].metadata["task_fit_score"] == 1.0
    assert active[0].metadata["dedupe_key"]
    assert active[0].metadata["interruption_budget_ok"] is True


def test_policy_patch_budget_does_not_starve_relevant_lower_ranked_patch(tmp_path):
    store = _store(tmp_path, max_active=1)
    relevant = store.propose(
        {
            "source_id": "relevant",
            "summary": "Relevant schema rollback guidance.",
            "task_type": "coding",
            "trigger": "schema rollback",
            "severity": "low",
            "confidence": 0.8,
        }
    )
    for index in range(5):
        store.propose(
            {
                "source_id": f"irrelevant-{index}",
                "summary": f"High-priority unrelated guidance {index}.",
                "task_type": "coding",
                "trigger": f"unrelated-{index}",
                "severity": "critical",
                "confidence": 0.99,
            }
        )

    active = store.active_for(
        task_type="coding",
        context="prepare a schema rollback",
    )

    assert [item.patch_id for item in active] == [relevant.patch_id]


def test_policy_patch_prefers_task_specific_match_over_generic_duplicate(tmp_path):
    store = _store(tmp_path)
    specific = store.propose(
        {
            "source_id": "specific",
            "summary": "Coding-specific schema rollback guidance.",
            "task_type": "coding",
            "trigger": "schema rollback",
            "severity": "low",
            "confidence": 0.8,
        }
    )
    store.propose(
        {
            "source_id": "generic",
            "summary": "Generic schema rollback guidance.",
            "task_type": "general",
            "trigger": "schema rollback",
            "severity": "critical",
            "confidence": 0.99,
        }
    )

    active = store.active_for(
        task_type="coding",
        context="prepare a schema rollback",
    )

    assert [item.patch_id for item in active] == [specific.patch_id]
    assert active[0].metadata["task_fit_score"] == 0.9


def test_policy_patch_respects_confidence_expiry_and_feedback(tmp_path):
    store = _store(tmp_path, min_confidence=0.8)

    assert (
        store.propose(
            {"summary": "too weak", "trigger": "weak trigger", "confidence": 0.2}
        )
        is None
    )

    expired = store.propose(
        {
            "summary": "expired patch",
            "task_type": "coding",
            "trigger": "expired patch",
            "confidence": 0.9,
            "expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        }
    )
    assert expired is not None
    assert store.active_for(task_type="coding") == []


def test_policy_patch_feedback_source_event_is_idempotent(tmp_path):
    store = _store(tmp_path)
    patch = store.propose(
        {
            "summary": "verify before deploy",
            "trigger": "deploy",
            "confidence": 0.9,
        }
    )
    assert patch is not None

    first = store.record_feedback(
        patch.patch_id,
        outcome="contradicted",
        source_event_id="recap-correction-one",
    )
    duplicate = store.record_feedback(
        patch.patch_id,
        outcome="contradicted",
        source_event_id="recap-correction-one",
    )

    assert duplicate == first
    with sqlite3.connect(str(tmp_path / "policy_patches.db")) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM policy_patch_feedback WHERE source_event_id=?",
            ("recap-correction-one",),
        ).fetchone()[0] == 1

    assert (
        store.propose({"summary": "active patch", "task_type": "coding", "confidence": 0.9})
        is None
    )

    active = store.propose(
        {
            "summary": "active patch",
            "task_type": "coding",
            "trigger": "active patch",
            "confidence": 0.9,
        }
    )
    assert active is not None
    assert store.active_for(task_type="coding", context="active patch")

    feedback = store.record_feedback(
        active.patch_id,
        outcome="dismiss",
        evidence={"reason": "not useful for this project"},
    )

    assert feedback["outcome"] == "dismiss"
    assert store.get_patch(active.patch_id)["status"] == "dismissed"
    assert store.active_for(task_type="coding") == []
