# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
from pathlib import Path

from core.cognitive.trust_scorer import (
    KnowledgeTrustOptions,
    KnowledgeTrustScorer,
)


def _options(tmp_path: Path) -> KnowledgeTrustOptions:
    return KnowledgeTrustOptions(
        database_dir=tmp_path,
        db_path=tmp_path / "trust_decisions.db",
        base_trust_score=0.55,
        evidence_ref_bonus=0.1,
        min_merge_score=0.72,
        min_delivery_score=0.55,
        min_delivery_task_fit=0.45,
        min_guard_score=0.75,
        min_guard_task_fit=0.7,
        ignore_penalty=0.12,
        dismiss_penalty=0.22,
        no_click_penalty=0.08,
        contradicted_penalty=0.35,
        harmful_penalty=1.0,
        harmful_cooldown_days=30,
    )


def test_guard_block_requires_evidence_active_risk_and_task_fit(tmp_path):
    scorer = KnowledgeTrustScorer(options=_options(tmp_path))

    missing_evidence = scorer.decide(
        source="guard_check",
        subject="rm -rf risk",
        action="guard_block",
        evidence_refs=[],
        task_fit_score=0.95,
        active_risk=True,
    )
    assert missing_evidence.decision == "observe"
    assert missing_evidence.reason == "missing_evidence_refs"

    valid = scorer.decide(
        source="guard_check",
        subject="rm -rf risk",
        action="guard_block",
        evidence_refs=["raw-1", "raw-2", "kg-1"],
        task_fit_score=0.95,
        active_risk=True,
    )
    assert valid.decision == "block"
    assert scorer.get_decision(valid.decision_id)["decision"] == "block"


def test_project_scoped_dismiss_only_lowers_matching_delivery_task_fit(tmp_path):
    scorer = KnowledgeTrustScorer(options=_options(tmp_path))
    scorer.record_negative_evidence(
        source="push_feedback",
        subject="docker",
        signal_type="dismiss",
        scope_type="project",
        scope_value="/repo/a",
    )

    other_project = scorer.decide(
        source="delivery",
        subject="docker",
        action="predictive_push",
        evidence_refs=["raw-1"],
        task_fit_score=0.6,
        scope_type="project",
        scope_value="/repo/b",
    )
    same_project = scorer.decide(
        source="delivery",
        subject="docker",
        action="predictive_push",
        evidence_refs=["raw-1"],
        task_fit_score=0.6,
        scope_type="project",
        scope_value="/repo/a",
    )

    assert other_project.decision == "deliver"
    assert same_project.decision == "suppress"
    assert same_project.reason == "low_task_fit"
    assert same_project.trust_score == other_project.trust_score


def test_harmful_negative_evidence_blocks_delivery_and_reduces_trust(tmp_path):
    scorer = KnowledgeTrustScorer(options=_options(tmp_path))
    row = scorer.record_negative_evidence(
        source="outcome",
        subject="dangerous-shell",
        signal_type="harmful",
        scope_type="global",
        severity=1.0,
        metadata={"reason": "deleted user data"},
    )

    decision = scorer.decide(
        source="delivery",
        subject="dangerous-shell",
        action="predictive_push",
        evidence_refs=["raw-1", "raw-2"],
        task_fit_score=0.95,
        scope_type="project",
        scope_value="/repo/a",
    )

    assert row["cooldown_until"]
    assert decision.decision == "block"
    assert decision.reason == "harmful_negative_evidence"
    assert decision.trust_score == 0.0


def test_merge_decision_records_trust_ledger(tmp_path):
    scorer = KnowledgeTrustScorer(options=_options(tmp_path))

    decision = scorer.decide(
        source="distill_action_router",
        subject="03-Tech/redis.md",
        action="merge_into_page",
        evidence_refs=["raw-1", "raw-2"],
        task_fit_score=0.9,
        scope_type="wiki_page",
        scope_value="03-Tech/redis.md",
        metadata={"claim_id": "claim-1"},
    )

    assert decision.decision == "apply"
    with sqlite3.connect(tmp_path / "trust_decisions.db") as conn:
        row = conn.execute(
            "SELECT action, decision, reason FROM trust_decisions WHERE decision_id=?",
            (decision.decision_id,),
        ).fetchone()
    assert row == ("merge_into_page", "apply", "merge_requirements_met")


def test_extraction_decision_requires_evidence_refs(tmp_path):
    scorer = KnowledgeTrustScorer(options=_options(tmp_path))

    missing_evidence = scorer.decide(
        source="distill_action_router",
        subject="00-Inbox/redis.md",
        action="extract",
        evidence_refs=[],
        task_fit_score=0.9,
    )
    accepted = scorer.decide(
        source="distill_action_router",
        subject="00-Inbox/redis.md",
        action="extract",
        evidence_refs=["raw-1", "raw-2"],
        task_fit_score=0.9,
    )
    low_task_fit = scorer.decide(
        source="distill_action_router",
        subject="00-Inbox/redis.md",
        action="extract",
        evidence_refs=["raw-1", "raw-2"],
        task_fit_score=0.1,
    )

    assert missing_evidence.decision == "review"
    assert missing_evidence.reason == "missing_evidence_refs"
    assert low_task_fit.decision == "review"
    assert low_task_fit.reason == "low_task_fit"
    assert accepted.decision == "accept"
    assert accepted.reason == "extraction_requirements_met"


def test_preview_decision_does_not_create_or_write_trust_ledger(tmp_path):
    scorer = KnowledgeTrustScorer(options=_options(tmp_path), ensure_db=False)

    decision = scorer.decide(
        source="cognitive_consolidator",
        subject="06-Retrospectives/method.md",
        action="extract",
        evidence_refs=["raw-1"],
        task_fit_score=0.9,
        persist=False,
    )

    assert decision.decision == "accept"
    assert not (tmp_path / "trust_decisions.db").exists()


def test_repeated_decisions_are_not_overwritten(tmp_path):
    scorer = KnowledgeTrustScorer(options=_options(tmp_path))

    first = scorer.decide(
        source="predictive_push",
        subject="redis",
        action="predictive_push",
        evidence_refs=["03-Tech/redis.md"],
        task_fit_score=0.9,
    )
    second = scorer.decide(
        source="predictive_push",
        subject="redis",
        action="predictive_push",
        evidence_refs=["03-Tech/redis.md"],
        task_fit_score=0.9,
    )

    assert first.decision_id != second.decision_id
    with sqlite3.connect(tmp_path / "trust_decisions.db") as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM trust_decisions WHERE subject=?",
            ("redis",),
        ).fetchone()[0]
    assert count == 2
