# -*- coding: utf-8 -*-
"""Decision inbox service and CLI contract tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.application.decision_inbox import DecisionInboxPaths, DecisionInboxService
from core.cli.commands import decision_inbox as decision_inbox_cli
from core.trust import CandidateBundle, ProposalQueue
from tests.cognitive_decision_fixtures import predictive_route_access


def _paths(tmp_path: Path) -> DecisionInboxPaths:
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    return DecisionInboxPaths(
        wiki_base=tmp_path / "wiki",
        database_dir=db_dir,
        proposal_db=db_dir / "trusted_push.db",
        distill_actions_db=db_dir / "distill_actions.db",
        recap_db=db_dir / "recap_tasks.db",
        delivery_db=db_dir / "delivery_events.db",
    )


def _proposal(paths: DecisionInboxPaths):
    paths.wiki_base.mkdir(parents=True)
    candidate = CandidateBundle.from_payload(
        source="hephaestus_distillation",
        target_kind="markdown",
        target_path=str(paths.wiki_base / "decision.md"),
        payload={"title": "Decision", "content": "# Decision\n\nBody"},
        evidence_refs=["session:abc"],
        risk_level="medium",
    )
    return ProposalQueue(paths.proposal_db, wiki_base=paths.wiki_base).submit_candidate(candidate)


def _insert_delivery(paths: DecisionInboxPaths) -> None:
    with sqlite3.connect(str(paths.delivery_db)) as conn:
        conn.execute(
            """
            CREATE TABLE delivery_events (
                event_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '',
                channel TEXT NOT NULL DEFAULT '',
                target TEXT NOT NULL DEFAULT '',
                requested_level TEXT NOT NULL DEFAULT 'hint',
                delivered_level TEXT NOT NULL DEFAULT 'silent',
                decision TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                profile TEXT NOT NULL DEFAULT 'balanced',
                cooldown_key TEXT NOT NULL DEFAULT '',
                task_key TEXT NOT NULL DEFAULT '',
                trust_decision_id TEXT NOT NULL DEFAULT '',
                trust_score REAL NOT NULL DEFAULT 0,
                task_fit_score REAL NOT NULL DEFAULT 0,
                interruption_cost REAL NOT NULL DEFAULT 0,
                feedback TEXT NOT NULL DEFAULT '',
                feedback_at TEXT NOT NULL DEFAULT '',
                outcome_id TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO delivery_events (
                event_id, created_at, source, subject, channel, target,
                delivered_level, decision, reason
            ) VALUES (
                'del-1', '2026-07-09T00:00:00+00:00', 'preflight',
                'Delivery subject', 'predictive_push', 'agent', 'warn',
                'deliver', 'requirements_met'
            )
            """
        )


def _insert_cognitive_action(paths: DecisionInboxPaths) -> None:
    with sqlite3.connect(str(paths.distill_actions_db)) as conn:
        conn.execute(
            """
            CREATE TABLE cognitive_action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cognitive_action_id TEXT NOT NULL UNIQUE,
                distill_action_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                cognitive_action TEXT NOT NULL,
                target_kind TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                artifact_path TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO cognitive_action_log (
                cognitive_action_id, created_at, cognitive_action, target_kind, status
            ) VALUES ('cog-1', '2026-07-09T00:00:01+00:00', 'create_observation', 'observation', 'queued')
            """
        )


def _insert_recap(paths: DecisionInboxPaths) -> None:
    from core.app.forced_retrospective import ForcedRetrospective

    ForcedRetrospective(str(paths.recap_db)).create_system_recap(
        "Recap topic",
        severity="high",
        context="needs review",
    )


def test_decision_inbox_lists_proposal_delivery_cognitive_and_recap(
    tmp_path,
):
    paths = _paths(tmp_path)
    _proposal(paths)
    _insert_delivery(paths)
    _insert_cognitive_action(paths)
    _insert_recap(paths)

    result = DecisionInboxService(paths=paths).list_items(limit=20)

    assert result["schema_version"] == "mnemos.decision_inbox.v1"
    assert result["count"] == 4
    sources = {item["source"] for item in result["items"]}
    assert sources == {"proposal", "delivery", "cognitive_action", "recap"}
    assert all(item["actions"] for item in result["items"])


def test_decision_inbox_act_recap_and_delivery(
    tmp_path,
):
    paths = _paths(tmp_path)
    _insert_delivery(paths)
    _insert_recap(paths)
    service = DecisionInboxService(paths=paths)
    recap_item = next(item for item in service.list_items()["items"] if item["source"] == "recap")

    recap_result = service.act(recap_item["item_id"], "resolve", reason="handled")
    delivery_result = service.act("delivery:del-1", "accept", reason="useful")

    assert recap_result["success"] is True
    assert recap_result["status"] == "resolved"
    assert delivery_result == {
        "success": False,
        "source": "delivery",
        "reason": "canonical_feedback_entrypoint_required",
    }
    with sqlite3.connect(str(paths.delivery_db)) as conn:
        feedback = conn.execute(
            "SELECT feedback FROM delivery_events WHERE event_id='del-1'"
        ).fetchone()[0]
    assert feedback == ""


def test_decision_inbox_authenticated_delivery_uses_canonical_feedback(tmp_path):
    paths = _paths(tmp_path)
    principal = PrincipalEnvelope(
        principal_id="user:decision-inbox-feedback",
        agent="codex",
        host_kind="test",
        capability_id="decision-inbox-feedback",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )
    from core.cognitive.delivery_router import (
        DeliveryBudgetPolicy,
        KnowledgeDeliveryRouter,
    )

    router = KnowledgeDeliveryRouter(
        db_path=paths.delivery_db,
        database_dir=paths.database_dir,
        policy=DeliveryBudgetPolicy(
            daily_total=100,
            per_task_total=100,
            per_task_hint=100,
            same_topic_cooldown_hours=0,
        ),
    )
    delivered = router.route_candidate(
        source="predictive_push",
        subject="delivery subject",
        channel="predictive_push",
        evidence_refs=["03-Tech/delivery.md"],
        task_fit_score=0.9,
        principal=principal,
        source_access_control=predictive_route_access(
            principal,
            subject="delivery subject",
            session_id="decision-inbox-session",
            project="mnemos",
        ),
    )
    router.record_presentation(
        delivered.event_id,
        host_agent=principal.agent,
        rendered_content_hash="sha256:" + "d" * 64,
    )

    result = DecisionInboxService(paths=paths).act(
        f"delivery:{delivered.event_id}",
        "accept",
        principal=principal,
        narrowing=AccessNarrowing(
            project="mnemos",
            session_id="decision-inbox-session",
        ),
    )

    assert result["success"] is True
    assert result["source"] == "delivery"
    assert result["disposition"] == "record_only"
    assert result["required_receipts_complete"] is True
    assert {item["disposition"] for item in result["terminal_receipts"]} == {
        "intentional_skip"
    }


def test_decision_inbox_act_proposal_reject(tmp_path):
    paths = _paths(tmp_path)
    proposal = _proposal(paths)

    result = DecisionInboxService(paths=paths).act(
        f"proposal:{proposal.proposal_id}",
        "reject",
        reason="not useful",
    )

    assert result == {
        "success": False,
        "source": "proposal",
        "reason": "canonical_feedback_entrypoint_required",
    }
    assert ProposalQueue(paths.proposal_db, wiki_base=paths.wiki_base).get(
        proposal.proposal_id
    ).status == proposal.status


def test_decision_inbox_cli_list_uses_service(monkeypatch, capsys):
    class FakeService:
        def list_items(self, *, limit):
            assert limit == 3
            return {
                "schema_version": "mnemos.decision_inbox.v1",
                "count": 1,
                "counts": {"recap": 1},
                "items": [{"item_id": "recap:r1", "source": "recap", "title": "R"}],
            }

    monkeypatch.setattr(decision_inbox_cli, "DecisionInboxService", FakeService)

    rc = decision_inbox_cli.cmd_decision_inbox(
        SimpleNamespace(decision_inbox_cmd="list", limit=3, json=True)
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"][0]["item_id"] == "recap:r1"


def test_decision_inbox_requires_explicit_config_paths():
    with pytest.raises(ValueError, match="configured wiki_dir and database_dir"):
        DecisionInboxService(config=SimpleNamespace(get=lambda *_args: None))
