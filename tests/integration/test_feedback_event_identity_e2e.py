# -*- coding: utf-8 -*-
from __future__ import annotations

from unittest.mock import MagicMock

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from tests.cognitive_decision_fixtures import predictive_route_access


def test_feedback_binds_exact_delivery_principal_and_scope_end_to_end(
    tmp_path, monkeypatch
):
    fake_cfg = MagicMock()
    fake_cfg.database_dir = tmp_path
    fake_cfg.get.side_effect = lambda _key, default=None: default
    monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)
    monkeypatch.setattr("core.app.application_hub.get_config", lambda: fake_cfg)
    monkeypatch.setattr("core.cognitive.delivery_router.get_config", lambda: fake_cfg)

    from core.application.intelligence import IntelligenceApplicationService
    from core.cognitive.delivery_router import DeliveryBudgetPolicy, KnowledgeDeliveryRouter

    principal = PrincipalEnvelope(
        principal_id="principal-a",
        agent="codex",
        host_kind="codex",
        capability_id="capability-a",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )
    narrowing = AccessNarrowing(project="mnemos", session_id="session-a")
    router = KnowledgeDeliveryRouter(
        db_path=tmp_path / "delivery_events.db",
        database_dir=tmp_path,
        policy=DeliveryBudgetPolicy(
            daily_total=100,
            per_task_total=100,
            per_task_hint=100,
            same_topic_cooldown_hours=0,
        ),
    )
    route_kwargs = {
        "source": "predictive_push",
        "subject": "redis",
        "channel": "predictive_push",
        "evidence_refs": ["03-Tech/redis.md"],
        "task_fit_score": 0.9,
        "metadata": {
            "principal_id": principal.principal_id,
            "principal_agent": principal.agent,
            "project": narrowing.project,
            "session_id": narrowing.session_id,
        },
    }
    delivered = router.route_candidate(
        **route_kwargs,
        principal=principal,
        source_access_control=predictive_route_access(
            principal,
            subject="redis",
            session_id=narrowing.session_id,
            project=narrowing.project,
        ),
    )

    wrong_principal = PrincipalEnvelope(
        principal_id="principal-b",
        agent="codex",
        host_kind="codex",
        capability_id="capability-b",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )
    principal_mismatch = IntelligenceApplicationService().push_feedback(
        "redis",
        "dismiss",
        delivered.event_id,
        principal=wrong_principal,
        narrowing=narrowing,
    )
    assert principal_mismatch == {
        "success": False,
        "reason": "delivery_event_principal_mismatch",
    }

    unshown = IntelligenceApplicationService().push_feedback(
        "redis",
        "dismiss",
        delivered.event_id,
        principal=principal,
        narrowing=narrowing,
    )

    assert unshown == {
        "success": False,
        "reason": "delivery_presentation_not_acknowledged",
    }

    presentation = router.record_presentation(
        delivered.event_id,
        host_agent=principal.agent,
        rendered_content_hash="sha256:" + "a" * 64,
    )
    scope_mismatch = IntelligenceApplicationService().push_feedback(
        "redis",
        "dismiss",
        delivered.event_id,
        principal=principal,
        narrowing=AccessNarrowing(project="other", session_id="session-b"),
    )
    assert scope_mismatch == {
        "success": False,
        "reason": "prediction_scope_mismatch",
    }
    result = IntelligenceApplicationService().push_feedback(
        "redis",
        "dismiss",
        delivered.event_id,
        principal=principal,
        narrowing=narrowing,
    )

    assert result["success"] is True
    assert result["terminal_status"] == "complete"
    assert result["delivery_event_id"] == delivered.event_id
    assert result["principal"]["principal_id"] == principal.principal_id
    assert result["required_receipts_complete"] is True
    assert result["disposition"] == "record_only"
    assert result["effect_delta"] == {
        "direct_domain_updates": 0,
        "proposal_commands": 0,
    }
    assert presentation["delivery_event_id"] == delivered.event_id
    from core.cognitive.state_store import CognitiveStateStore

    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    reaction = state.revision(result["reaction_revision_id"])
    attribution = state.revision(result["attribution_revision_id"])
    assert reaction is not None
    assert reaction.payload["interaction"]["kind"] == "dismiss"
    assert attribution is not None
    assert attribution.payload["outcome_refs"] == []
