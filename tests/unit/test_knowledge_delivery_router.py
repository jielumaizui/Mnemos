# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timezone

import pytest

from core.access_policy import PrincipalEnvelope
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.decision_trace import MaterialActionAuthorization
from core.cognitive.decision_trace_migration import (
    build_decision_trace_inventory,
    default_source_domains,
    inspect_decision_trace_history_coverage,
)
from core.cognitive.delivery_router import (
    DELIVERY_MATERIAL_ACTION_TYPE,
    DELIVERY_MATERIAL_EXECUTOR,
    DELIVERY_MATERIAL_OWNER,
    DeliveryBudgetPolicy,
    KnowledgeDeliveryRouter,
    _delivery_event_key_column,
    delivery_material_action_binding,
    verify_delivery_nonmaterial_row,
)
from core.cognitive.trust_scorer import (
    KnowledgeTrustOptions,
    KnowledgeTrustScorer,
    TrustDecision,
)
from tests.cognitive_decision_fixtures import material_action_authorization


class _TrustScorer:
    def __init__(self, decision: str = "deliver", reason: str = "ok"):
        self.decision = decision
        self.reason = reason

    def decide(self, **kwargs):
        return TrustDecision(
            decision_id="trust-1",
            source=kwargs["source"],
            subject=kwargs["subject"],
            action=kwargs["action"],
            decision=self.decision,
            reason=self.reason,
            trust_score=0.9,
            task_fit_score=0.9,
            interruption_cost=kwargs["interruption_cost"],
            outcome_score=1.0,
            evidence_refs=list(kwargs.get("evidence_refs") or []),
            metadata=dict(kwargs.get("metadata") or {}),
        )


def _router(tmp_path, *, policy=None, trust=None):
    return KnowledgeDeliveryRouter(
        db_path=tmp_path / "delivery_events.db",
        database_dir=tmp_path,
        policy=policy or DeliveryBudgetPolicy(same_topic_cooldown_hours=24),
        trust_scorer=trust or _TrustScorer(),
    )


def _predictive_authorization(subject):
    normalized = str(subject or "").strip().lower()
    principal = PrincipalEnvelope(
        principal_id="system:delivery-test",
        agent="mnemos",
        host_kind="test",
        capability_id="delivery-test",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )
    access = make_cognitive_access_envelope(
        owner_principal_id=principal.principal_id,
        owner_agent=principal.agent,
        scope_type="topic",
        scope_id=normalized,
        session_id="delivery-test-session",
        project="mnemos",
        purposes=(
            "cognitive_state_read",
            "cognitive_state_write",
            "prediction_read",
        ),
        consent_provenance_refs=(f"wiki:{normalized}",),
        sensitivity="sensitive",
        retention_policy="prediction_source",
        source_acl_lineage=(
            "sha256:" + hashlib.sha256(normalized.encode()).hexdigest(),
        ),
    )
    return principal, access


def _route(router, **kwargs):
    if kwargs.get("channel") == "predictive_push":
        principal, access = _predictive_authorization(kwargs.get("subject"))
        kwargs.setdefault("principal", principal)
        kwargs.setdefault("source_access_control", access)
    return router.route_candidate(**kwargs)


def _authorized_route(router, database_dir, **kwargs):
    if kwargs.get("channel") == "predictive_push":
        return _route(router, **kwargs)
    binding = delivery_material_action_binding(**kwargs)
    authorization = material_action_authorization(
        database_dir,
        action_type=DELIVERY_MATERIAL_ACTION_TYPE,
        owner=DELIVERY_MATERIAL_OWNER,
        executor=DELIVERY_MATERIAL_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
    )
    return router.route_candidate(**kwargs, material_action=authorization)


def test_delivery_event_key_column_rejects_unapproved_sql_identifier():
    assert _delivery_event_key_column("task_key") == "task_key"

    with pytest.raises(ValueError):
        _delivery_event_key_column("task_key; DROP TABLE delivery_events")
    with pytest.raises(ValueError):
        _delivery_event_key_column("subject")


def test_predictive_route_requires_server_resolved_source_access(tmp_path):
    router = _router(tmp_path)
    route = {
        "source": "predictive_push",
        "subject": "redis",
        "channel": "predictive_push",
        "evidence_refs": ["03-Tech/redis.md"],
        "task_fit_score": 0.9,
        "cooldown_key": "redis",
    }

    with pytest.raises(ValueError, match="server-resolved source access_control"):
        router.route_candidate(**route)

    _, access = _predictive_authorization("redis")
    wrong_principal = PrincipalEnvelope(
        principal_id="system:not-the-source-owner",
        agent="mnemos",
        host_kind="test",
        capability_id="delivery-test",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )
    with pytest.raises(PermissionError, match="owner_principal_mismatch"):
        router.route_candidate(
            **route,
            source_access_control=access,
            principal=wrong_principal,
        )


def test_predictive_material_binding_includes_exact_source_acl_hash():
    _, redis_access = _predictive_authorization("redis")
    _, other_access = _predictive_authorization("other")
    route = {
        "source": "predictive_push",
        "subject": "redis",
        "channel": "predictive_push",
        "evidence_refs": ["03-Tech/redis.md"],
        "task_fit_score": 0.9,
        "cooldown_key": "redis",
    }

    redis_binding = delivery_material_action_binding(
        **route,
        source_access_control=redis_access,
    )
    other_binding = delivery_material_action_binding(
        **route,
        source_access_control=other_access,
    )

    assert redis_binding["target_ref"] == other_binding["target_ref"]
    assert redis_binding["input_hash"] != other_binding["input_hash"]


def test_delivery_router_logs_delivery_event(tmp_path):
    router = _router(tmp_path)

    decision = _authorized_route(
        router,
        tmp_path,
        source="predictive_push",
        subject="redis",
        channel="predictive_push",
        target="03-Tech/redis.md",
        evidence_refs=["03-Tech/redis.md"],
        task_fit_score=0.9,
        cooldown_key="redis",
    )

    assert decision.decision == "deliver"
    assert decision.delivered_level == "hint"
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        row = conn.execute(
            "SELECT subject, decision, trust_decision_id FROM delivery_events"
        ).fetchone()
    assert row == ("redis", "deliver", "trust-1")


def test_delivery_presentation_receipt_is_host_bound_immutable_and_not_a_route_claim(tmp_path):
    router = _router(tmp_path)
    decision = _route(
        router,
        source="predictive_push",
        subject="redis",
        channel="predictive_push",
        target="03-Tech/redis.md",
        evidence_refs=["03-Tech/redis.md"],
        task_fit_score=0.9,
        cooldown_key="redis",
        metadata={"principal_agent": "mnemos"},
    )
    rendered_hash = "sha256:" + "a" * 64

    receipt = router.record_presentation(
        decision.event_id,
        host_agent="mnemos",
        rendered_content_hash=rendered_hash,
    )
    replay = router.record_presentation(
        decision.event_id,
        host_agent="mnemos",
        rendered_content_hash=rendered_hash,
    )

    assert receipt["schema_version"] == "mnemos.delivery_presentation_receipt.v1"
    assert receipt["status"] == "recorded"
    assert replay["receipt_hash"] == receipt["receipt_hash"]
    with pytest.raises(PermissionError, match="principal mismatch"):
        router.record_presentation(
            decision.event_id,
            host_agent="other-host",
            rendered_content_hash=rendered_hash,
        )
    with pytest.raises(ValueError, match="immutable"):
        router.record_presentation(
            decision.event_id,
            host_agent="mnemos",
            rendered_content_hash="sha256:" + "b" * 64,
        )


def test_delivery_router_recovers_crash_after_event_without_duplicate(
    tmp_path,
    monkeypatch,
):
    router = _router(tmp_path)
    kwargs = {
        "source": "preflight_inject",
        "subject": "crash-window",
        "channel": "preflight_inject",
        "target": "03-Tech/crash-window.md",
        "evidence_refs": ["03-Tech/crash-window.md"],
        "task_fit_score": 0.9,
        "cooldown_key": "crash-window",
    }
    binding = delivery_material_action_binding(**kwargs)
    authorization = material_action_authorization(
        tmp_path,
        action_type=DELIVERY_MATERIAL_ACTION_TYPE,
        owner=DELIVERY_MATERIAL_OWNER,
        executor=DELIVERY_MATERIAL_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
        nonce="delivery-crash-recovery",
    )
    original = MaterialActionAuthorization.record_terminal
    calls = 0

    def crash_once(self, terminal):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected crash after delivery event commit")
        return original(self, terminal)

    monkeypatch.setattr(
        MaterialActionAuthorization,
        "record_terminal",
        crash_once,
    )
    with pytest.raises(OSError, match="after delivery event commit"):
        router.route_candidate(**kwargs, material_action=authorization)
    replay = router.route_candidate(**kwargs, material_action=authorization)

    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM delivery_events").fetchall()
        metadata = json.loads(str(rows[0]["metadata_json"]))
    assert len(rows) == 1
    assert replay.event_id == str(rows[0]["event_id"])
    assert metadata["material_action"]["command_id"] == (
        authorization.permit.command_id
    )
    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        receipt = conn.execute(
            "SELECT status, target_effect_id FROM cognitive_state_effect_receipts "
            "WHERE command_id=?",
            (authorization.permit.command_id,),
        ).fetchone()
    assert receipt == ("committed", authorization.permit.effect_id)


@pytest.mark.no_canonical_material_actions
def test_delivery_router_seals_decision_before_outward_delivery(
    tmp_path,
):
    router = _router(tmp_path)

    decision = _route(
        router,
        source="predictive_push",
        subject="redis",
        channel="predictive_push",
        evidence_refs=["03-Tech/redis.md"],
        task_fit_score=0.9,
        cooldown_key="redis",
    )

    assert decision.decision == "deliver"
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM delivery_events").fetchone() == (1,)
    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        assert conn.execute(
            "SELECT status FROM cognitive_state_effect_receipts"
        ).fetchone() == ("committed",)


def test_same_topic_cooldown_suppresses_repeat_delivery(tmp_path):
    router = _router(tmp_path)
    kwargs = {
        "source": "predictive_push",
        "subject": "redis",
        "channel": "predictive_push",
        "evidence_refs": ["03-Tech/redis.md"],
        "task_fit_score": 0.9,
        "cooldown_key": "redis",
    }

    first = _authorized_route(router, tmp_path, **kwargs)
    second = _route(router, **kwargs)

    assert first.decision == "deliver"
    assert second.decision == "suppress"
    assert second.reason == "same_topic_cooldown"
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM delivery_events WHERE decision='suppress'"
        ).fetchone()
    assert row is not None
    assert verify_delivery_nonmaterial_row(row) is True
    forged = dict(row)
    metadata = json.loads(str(forged["metadata_json"]))
    metadata.pop("_nonmaterial_suppression")
    forged["metadata_json"] = json.dumps(metadata)
    assert verify_delivery_nonmaterial_row(forged) is False

    with sqlite3.connect(tmp_path / "action_ledger.db") as conn:
        conn.execute(
            """
            CREATE TABLE action_ledger (
                action_id TEXT PRIMARY KEY,
                evidence_refs_json TEXT NOT NULL,
                verification_json TEXT NOT NULL,
                quality_decision_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
    with sqlite3.connect(tmp_path / "trusted_push.db") as conn:
        conn.execute(
            """
            CREATE TABLE formal_cognitive_mutations (
                event_id TEXT PRIMARY KEY,
                evidence_refs TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
    domains = default_source_domains(database_dir=tmp_path)
    inventory = build_decision_trace_inventory(domains)
    delivery_report = next(
        value for value in inventory.domains
        if value["domain"] == "delivery_events"
    )
    coverage = inspect_decision_trace_history_coverage(
        domains,
        tmp_path / "producer_consumer_ledger.db",
    )

    assert delivery_report["source_row_count"] == 2
    assert delivery_report["row_count"] == 1
    assert delivery_report["nonmaterial_suppression_count"] == 1
    assert coverage["expected_by_source"]["delivery_events.delivery_events"] == 1


def test_silent_delivery_is_logged_without_consuming_visible_budget_or_cooldown(tmp_path):
    policy = DeliveryBudgetPolicy(
        daily_total=1,
        per_task_total=1,
        per_task_hint=1,
        same_topic_cooldown_hours=24,
    )
    router = _router(tmp_path, policy=policy)

    silent = _authorized_route(
        router,
        tmp_path,
        source="preflight_inject",
        subject="redis",
        channel="preflight_inject",
        evidence_refs=["06-Retrospectives/redis.md"],
        task_fit_score=0.9,
        requested_level="silent",
        task_key="coding",
        cooldown_key="redis",
    )
    visible = _authorized_route(
        router,
        tmp_path,
        source="predictive_push",
        subject="redis",
        channel="predictive_push",
        evidence_refs=["03-Tech/redis.md"],
        task_fit_score=0.9,
        requested_level="hint",
        task_key="coding",
        cooldown_key="redis",
    )
    silent_after_visible_budget = _authorized_route(
        router,
        tmp_path,
        source="preflight_inject",
        subject="redis",
        channel="preflight_inject",
        evidence_refs=["06-Retrospectives/redis.md"],
        task_fit_score=0.9,
        requested_level="silent",
        task_key="coding",
        cooldown_key="redis",
    )

    assert silent.decision == "deliver"
    assert silent.delivered_level == "silent"
    assert visible.decision == "deliver"
    assert visible.delivered_level == "hint"
    assert silent_after_visible_budget.decision == "deliver"
    assert silent_after_visible_budget.delivered_level == "silent"


def test_topic_negative_evidence_suppresses_predictive_push(tmp_path):
    scorer = KnowledgeTrustScorer(
        options=KnowledgeTrustOptions(
            database_dir=tmp_path,
            db_path=tmp_path / "trust_decisions.db",
        )
    )
    scorer.record_negative_evidence(
        source="push_feedback",
        subject="redis",
        signal_type="dismiss",
        scope_type="topic",
        scope_value="redis",
    )
    router = _router(tmp_path, trust=scorer)

    decision = _route(
        router,
        source="predictive_push",
        subject="redis",
        channel="predictive_push",
        evidence_refs=["03-Tech/redis.md"],
        task_fit_score=0.5,
        cooldown_key="redis",
    )

    assert decision.decision == "suppress"
    assert decision.reason == "trust_gate:low_task_fit"


def test_legacy_feedback_column_no_longer_drives_delivery_cooldown(tmp_path):
    policy = DeliveryBudgetPolicy(
        same_topic_cooldown_hours=0,
        dismiss_cooldown_days=14,
    )
    router = _router(tmp_path, policy=policy)
    kwargs = {
        "source": "predictive_push",
        "subject": "redis",
        "channel": "predictive_push",
        "evidence_refs": ["03-Tech/redis.md"],
        "task_fit_score": 0.9,
        "cooldown_key": "redis",
    }

    first = _authorized_route(router, tmp_path, **kwargs)
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        conn.execute("ALTER TABLE delivery_events ADD COLUMN feedback TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE delivery_events ADD COLUMN feedback_at TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "UPDATE delivery_events SET feedback='dismiss', feedback_at=? WHERE event_id=?",
            (datetime.now(timezone.utc).isoformat(), first.event_id),
        )
    second = _route(router, **kwargs)

    assert second.decision == "deliver"
    assert second.reason != "dismiss_cooldown"


def test_quiet_profile_downgrades_warn_without_active_risk(tmp_path):
    policy = DeliveryBudgetPolicy(preference="quiet", per_task_warn=1)
    router = _router(tmp_path, policy=policy)

    decision = _authorized_route(
        router,
        tmp_path,
        source="guard_check",
        subject="risky command",
        channel="guard_check",
        evidence_refs=["raw-1"],
        task_fit_score=0.9,
        requested_level="warn",
        active_risk=False,
    )

    assert decision.decision == "deliver"
    assert decision.delivered_level == "hint"
    assert decision.reason == "quiet_profile_downgrade"


def test_active_profile_does_not_bypass_trust_gate(tmp_path):
    policy = DeliveryBudgetPolicy(preference="active", force_open_daily=1)
    router = _router(tmp_path, policy=policy, trust=_TrustScorer(decision="suppress", reason="low_task_fit"))

    decision = router.route_candidate(
        source="guard_check",
        subject="risky command",
        channel="guard_check",
        evidence_refs=["raw-1"],
        task_fit_score=0.9,
        requested_level="force_open",
        active_risk=True,
    )

    assert decision.decision == "suppress"
    assert decision.delivered_level == "silent"
    assert decision.reason == "trust_gate:low_task_fit"


def test_delivery_router_does_not_expose_feedback_or_outcome_mutation_apis(tmp_path):
    router = _router(tmp_path)
    decision = _authorized_route(
        router,
        tmp_path,
        source="predictive_push",
        subject="redis",
        channel="predictive_push",
        evidence_refs=["03-Tech/redis.md"],
        task_fit_score=0.9,
        cooldown_key="redis",
    )

    assert not hasattr(router, "record_feedback")
    assert not hasattr(router, "record_outcome")
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(delivery_events)")}
        outcome_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cognitive_outcomes'"
        ).fetchone()
    assert decision.event_id
    assert {"feedback", "feedback_at", "outcome_id"}.isdisjoint(columns)
    assert outcome_table is None
    assert not (tmp_path / "feedback_signals.db").exists()
