# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
from pathlib import Path

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.application.kia import KiaApplicationService
from core.cognitive.policy_patch import (
    POLICY_PATCH_EXECUTOR,
    POLICY_PATCH_FEEDBACK_ACTION,
    POLICY_PATCH_OWNER,
    POLICY_PATCH_PROPOSE_ACTION,
    PolicyPatchStore,
    policy_patch_feedback_binding,
    policy_patch_proposal_binding,
)
from tests.cognitive_decision_fixtures import material_action_authorization


class _FakeConfig:
    def __init__(self, tmp_path: Path):
        self.database_dir = tmp_path
        self.wiki_dir = tmp_path / "wiki"
        self.wiki_dir.mkdir(parents=True, exist_ok=True)

    def get(self, key, default=None):
        values = {
            "policy_patch.db_path": str(self.database_dir / "policy_patches.db"),
            "policy_patch.enabled": True,
            "policy_patch.ttl_days": 14,
            "policy_patch.min_confidence": 0.5,
            "policy_patch.max_active": 5,
            "delivery.db_path": str(self.database_dir / "delivery_events.db"),
            "delivery.preference": "active",
            "delivery.profiles.active": {},
            "delivery.profiles.active.daily_total": 20,
            "delivery.profiles.active.per_task_total": 8,
            "delivery.profiles.active.per_task_hint": 8,
            "delivery.profiles.active.per_task_warn": 4,
            "delivery.profiles.active.force_open_daily": 2,
            "delivery.profiles.active.same_topic_cooldown_hours": 0,
            "delivery.profiles.active.dismiss_cooldown_days": 1,
            "trust.db_path": str(self.database_dir / "trust_decisions.db"),
        }
        return values.get(key, default)


def _patch_config(monkeypatch, tmp_path):
    # Import config consumers before replacing core.config.get_config.  A lazy
    # import while the core symbol is patched would permanently bind the test
    # lambda in that consumer after monkeypatch teardown.
    import core.embeddings.index_manager as embedding_index_manager

    cfg = _FakeConfig(tmp_path)
    monkeypatch.setattr("core.config.get_config", lambda: cfg)
    monkeypatch.setattr(embedding_index_manager, "get_config", lambda: cfg)
    monkeypatch.setattr("core.cognitive.policy_patch.get_config", lambda: cfg)
    monkeypatch.setattr("core.cognitive.delivery_router.get_config", lambda: cfg)
    monkeypatch.setattr("core.cognitive.trust_scorer.get_config", lambda: cfg)
    return cfg


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="mcp:codex:policy-preflight-test",
        agent="codex",
        host_kind="codex",
        capability_id="policy-preflight-test",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"project-a"}),
    )


def _propose(store: PolicyPatchStore, lesson):
    binding = policy_patch_proposal_binding(lesson, store.options)
    assert binding is not None
    authorization = material_action_authorization(
        store.options.database_dir,
        action_type=POLICY_PATCH_PROPOSE_ACTION,
        owner=POLICY_PATCH_OWNER,
        executor=POLICY_PATCH_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
    )
    return store.propose(lesson, material_action=authorization)


def _record_feedback(
    store: PolicyPatchStore,
    patch_id: str,
    *,
    outcome: str,
    evidence=None,
    source_event_id: str = "",
):
    binding = policy_patch_feedback_binding(
        patch_id=patch_id,
        outcome=outcome,
        evidence=evidence,
        source_event_id=source_event_id,
    )
    authorization = material_action_authorization(
        store.options.database_dir,
        action_type=POLICY_PATCH_FEEDBACK_ACTION,
        owner=POLICY_PATCH_OWNER,
        executor=POLICY_PATCH_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
    )
    return store.record_feedback(
        patch_id,
        outcome=outcome,
        evidence=evidence,
        source_event_id=source_event_id,
        material_action=authorization,
    )


def test_preflight_inject_includes_active_policy_patches(monkeypatch, tmp_path):
    _patch_config(monkeypatch, tmp_path)
    store = PolicyPatchStore()
    _propose(
        store,
        {
            "summary": "Run migration rollback checks before editing schema.",
            "task_type": "coding",
            "subtype": "migration",
            "trigger": "schema",
            "confidence": 0.9,
            "evidence_refs": ["raw-1"],
        }
    )

    class _EmptyPreFlightInjector:
        def inject(self, task_type, subtype, time_window, context_text):
            return None

    monkeypatch.setattr("core.kia.prophasis.PreFlightInjector", _EmptyPreFlightInjector)

    result = KiaApplicationService().preflight_inject(
        "coding",
        subtype="migration",
        context_text="schema change",
        principal=_principal(),
    )

    assert result["success"] is True
    assert result["loaded"] is True
    assert result["source"] == "policy_patch"
    assert result["policy_patches"][0]["content"].startswith("Run migration")
    assert result["policy_patches"][0]["match_source"] == "current_context"
    assert result["policy_patches"][0]["task_fit_score"] == 1.0
    assert result["policy_patches"][0]["dedupe_key"]
    assert result["policy_patches"][0]["interruption_budget_ok"] is True


def test_guard_check_uses_policy_patch_without_system_prompt_edits(monkeypatch, tmp_path):
    _patch_config(monkeypatch, tmp_path)
    store = PolicyPatchStore()
    patch = _propose(
        store,
        {
            "summary": "Pause before running blue-green cutover.",
            "task_type": "ops",
            "trigger": "blue-green cutover",
            "severity": "high",
            "confidence": 0.95,
            "evidence_refs": ["raw-ops-1"],
        }
    )

    class _EmptyPreFlightInjector:
        def inject(self, task_type, subtype, time_window, context_text):
            return None

    monkeypatch.setattr("core.kia.prophasis.PreFlightInjector", _EmptyPreFlightInjector)

    result = KiaApplicationService().guard_check(
        user_message="准备 blue-green cutover",
        task_type="ops",
    )

    assert result["alert"] is True
    assert result["checklist_item"].startswith("策略补丁:")
    assert result["policy_patches"][0]["patch_id"] == patch.patch_id
    assert result["policy_patches"][0]["delivery_mode"] == "preflight_guard_only"
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        row = conn.execute("SELECT source, channel FROM delivery_events").fetchone()
    assert row == ("guard_check", "guard_check")


def test_policy_patch_preflight_guard_and_feedback_suppression(monkeypatch, tmp_path):
    _patch_config(monkeypatch, tmp_path)
    store = PolicyPatchStore()
    patch = _propose(
        store,
        {
            "summary": "Run verify_installation.py before closing config edits.",
            "task_type": "coding",
            "subtype": "config",
            "trigger_keywords": ["config", "verify_installation.py"],
            "severity": "high",
            "confidence": 0.95,
            "evidence_refs": ["recap://retro-1"],
        }
    )

    class _EmptyPreFlightInjector:
        def inject(self, task_type, subtype, time_window, context_text):
            return None

    monkeypatch.setattr("core.kia.prophasis.PreFlightInjector", _EmptyPreFlightInjector)

    service = KiaApplicationService()
    preflight = service.preflight_inject(
        "coding",
        subtype="config",
        context_text="editing config before verify_installation.py",
        principal=_principal(),
    )
    guard = service.guard_check(
        user_message="closing config change before verify_installation.py",
        task_type="coding",
        subtype="config",
    )
    feedback = _record_feedback(
        store,
        patch.patch_id,
        outcome="dismiss",
        evidence={"reason": "not useful for this repo"},
    )
    suppressed = service.preflight_inject(
        "coding",
        subtype="config",
        context_text="editing config before verify_installation.py",
        principal=_principal(),
    )

    assert preflight["loaded"] is True
    assert preflight["policy_patches"][0]["patch_id"] == patch.patch_id
    assert guard["alert"] is True
    assert guard["policy_patches"][0]["patch_id"] == patch.patch_id
    assert feedback["status"] == "dismissed"
    assert suppressed["loaded"] is False
    assert suppressed["policy_patches"] == []


def test_preflight_does_not_inject_patch_that_only_matches_its_own_body(
    monkeypatch, tmp_path
):
    _patch_config(monkeypatch, tmp_path)
    store = PolicyPatchStore()
    _propose(
        store,
        {
            "summary": "Long-term planning advice containing long_term_plan.",
            "task_type": "general",
            "trigger": "long_term_plan",
            "confidence": 0.95,
        }
    )

    class _EmptyPreFlightInjector:
        def inject(self, task_type, subtype, time_window, context_text):
            return None

    monkeypatch.setattr("core.kia.prophasis.PreFlightInjector", _EmptyPreFlightInjector)

    result = KiaApplicationService().preflight_inject(
        "debugging",
        subtype="policy_patch_relevance",
        context_text="reproduce an unrelated matcher bug",
        principal=_principal(),
    )

    assert result["loaded"] is False
    assert result["policy_patches"] == []


def test_preflight_policy_patch_respects_explicit_project_scope(monkeypatch, tmp_path):
    _patch_config(monkeypatch, tmp_path)
    store = PolicyPatchStore()
    patch = _propose(
        store,
        {
            "summary": "Project A schema rollback guidance.",
            "task_type": "coding",
            "scope": "project-a",
            "trigger": "schema rollback",
            "confidence": 0.95,
        }
    )

    class _EmptyPreFlightInjector:
        def inject(self, task_type, subtype, time_window, context_text):
            return None

    monkeypatch.setattr("core.kia.prophasis.PreFlightInjector", _EmptyPreFlightInjector)

    service = KiaApplicationService()
    global_result = service.preflight_inject(
        "coding",
        context_text="schema rollback",
        principal=_principal(),
    )
    project_result = service.preflight_inject(
        "coding",
        context_text="schema rollback",
        principal=_principal(),
        narrowing=AccessNarrowing(project="project-a"),
    )
    global_guard = service.guard_check(
        user_message="schema rollback",
        task_type="coding",
    )
    project_guard = service.guard_check(
        user_message="schema rollback",
        task_type="coding",
        narrowing=AccessNarrowing(project="project-a"),
    )

    assert global_result["policy_patches"] == []
    assert [item["patch_id"] for item in project_result["policy_patches"]] == [
        patch.patch_id
    ]
    assert global_guard["policy_patches"] == []
    assert [item["patch_id"] for item in project_guard["policy_patches"]] == [
        patch.patch_id
    ]
