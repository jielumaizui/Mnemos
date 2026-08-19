import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.trust import CandidateBundle, DialogDecisionPush, ProposalQueue, WriteJournal
from core.trust.vault_mutation_service import TrustedVaultMutationService
from tests.cognitive_decision_fixtures import (
    trusted_markdown_action_authorization,
)


def _trusted_config(wiki: Path, db: Path, mode: str) -> SimpleNamespace:
    return SimpleNamespace(
        wiki_dir=wiki,
        database_dir=db.parent,
        get=lambda key, default=None: {
            "trusted_push.mode": mode,
            "trusted_push.db_path": str(db),
        }.get(key, default),
    )


def _proposal(
    tmp_path: Path,
    *,
    name: str = "page",
    risk_level: str = "medium",
    source_session_id: str | None = "dialog-session",
    source_agent: str | None = "codex",
):
    page = tmp_path / f"{name}.md"
    candidate = CandidateBundle.from_payload(
        source="hephaestus_distillation",
        target_kind="markdown",
        target_path=str(page),
        payload={"title": f"{name} title", "content": f"# {name}\n\nBody"},
        evidence_refs=["session:abc"],
        risk_level=risk_level,
        source_agent=source_agent,
        source_session_id=source_session_id,
    )
    return ProposalQueue(tmp_path / "trusted.db", wiki_base=tmp_path).submit_candidate(candidate)


def _event_count(db_path: Path) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute("SELECT COUNT(*) FROM dialog_push_events").fetchone()
    return int(row[0])


def _decision_access(tmp_path: Path) -> dict:
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")
    return {
        "principal": PrincipalEnvelope(
            principal_id="user:dialog-feedback",
            agent="codex",
            host_kind="test",
            capability_id="dialog-feedback",
            capabilities=frozenset({"memory_read", "memory_write"}),
            allowed_projects=frozenset({"mnemos"}),
        ),
        "narrowing": AccessNarrowing(
            session_id="dialog-session",
            project="mnemos",
        ),
    }


def test_dialog_push_delivers_whitebox_card_without_duplicate_events(tmp_path: Path):
    proposal = _proposal(tmp_path)
    db_path = tmp_path / "trusted.db"
    push = DialogDecisionPush(db_path, wiki_base=tmp_path)

    first = push.push(limit=5)
    second = push.push(limit=5)

    assert first["surface"] == "whitebox"
    assert first["cards"][0]["proposal_id"] == proposal.proposal_id
    assert {action["id"] for action in first["cards"][0]["actions"]} == {
        "approve",
        "reject",
        "snooze",
        "edit",
    }
    assert second["cards"][0]["card_id"] == first["cards"][0]["card_id"]
    assert _event_count(db_path) == 1


def test_manual_dialog_push_delivers_medium_card_during_quiet_hours(tmp_path: Path):
    proposal = _proposal(tmp_path)
    db_path = tmp_path / "trusted.db"
    push = DialogDecisionPush(db_path, wiki_base=tmp_path)
    quiet_now = datetime(2026, 7, 9, 6, 40, tzinfo=timezone.utc)

    result = push.push(limit=5, now=quiet_now)

    assert result["surface"] == "whitebox"
    assert result["cards"][0]["proposal_id"] == proposal.proposal_id
    assert _event_count(db_path) == 1


def test_dialog_push_can_respect_quiet_hours_when_requested(tmp_path: Path):
    _proposal(tmp_path)
    db_path = tmp_path / "trusted.db"
    push = DialogDecisionPush(db_path, wiki_base=tmp_path)
    quiet_now = datetime(2026, 7, 9, 6, 40, tzinfo=timezone.utc)

    result = push.push(limit=5, now=quiet_now, respect_quiet_hours=True)

    assert result == {"surface": "none", "fallback_reason": "", "cards": []}
    assert _event_count(db_path) == 0


def test_dialog_push_reports_none_surface_when_no_cards_for_agent_adapter(tmp_path: Path):
    db_path = tmp_path / "trusted.db"
    delivered = []

    class Adapter:
        def deliver(self, cards):
            delivered.extend(cards)

    result = DialogDecisionPush(db_path, wiki_base=tmp_path).push(
        agent_adapter=Adapter()
    )

    assert result == {"surface": "none", "fallback_reason": "", "cards": []}
    assert delivered == []


def test_dialog_push_falls_back_to_whitebox_when_agent_adapter_fails(tmp_path: Path):
    _proposal(tmp_path)
    db_path = tmp_path / "trusted.db"

    class FailingAdapter:
        def deliver(self, cards):
            raise TimeoutError("agent unavailable")

    result = DialogDecisionPush(db_path, wiki_base=tmp_path).push(
        agent_adapter=FailingAdapter()
    )

    assert result["surface"] == "whitebox"
    assert "TimeoutError" in result["fallback_reason"]
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute("SELECT surface, error_message FROM dialog_push_events").fetchone()
    assert row == ("whitebox", "TimeoutError: agent unavailable")


@pytest.mark.no_canonical_material_actions
def test_inline_approve_writes_wiki_and_marks_card_acted(tmp_path: Path):
    proposal = _proposal(tmp_path, name="approve")
    db_path = tmp_path / "trusted.db"
    push = DialogDecisionPush(db_path, wiki_base=tmp_path)
    push.push()

    result = push.decide(proposal.proposal_id, "approve", **_decision_access(tmp_path))

    assert result["status"] == "committed"
    assert (tmp_path / "approve.md").read_text(encoding="utf-8").startswith("# approve")
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute("SELECT status FROM dialog_push_events").fetchone()
    assert row == ("acted",)
    assert not (tmp_path / "feedback_signals.db").exists()
    with sqlite3.connect(str(tmp_path / "producer_consumer_ledger.db")) as conn:
        receipt_statuses = {
            str(row[0])
            for row in conn.execute(
                "SELECT status FROM cognitive_state_effect_receipts"
            ).fetchall()
        }
    assert receipt_statuses == {"committed", "intentional_skip"}


def test_inline_reject_without_principal_fails_closed(tmp_path: Path):
    proposal = _proposal(tmp_path, name="reject")
    db_path = tmp_path / "trusted.db"

    with pytest.raises(PermissionError, match="authenticated principal"):
        DialogDecisionPush(db_path, wiki_base=tmp_path).decide(
            proposal.proposal_id,
            "reject",
            reason="not useful",
        )

    assert (
        ProposalQueue(db_path, wiki_base=tmp_path).get(proposal.proposal_id).status
        == proposal.status
    )
    assert not (tmp_path / "feedback_signals.db").exists()
    assert not (tmp_path / "trust_decisions.db").exists()


def test_dialog_decision_rejects_cross_session_principal_before_feedback(
    tmp_path: Path,
) -> None:
    proposal = _proposal(
        tmp_path,
        name="session-bound",
        source_session_id="session-a",
    )
    access = _decision_access(tmp_path)
    access["narrowing"] = AccessNarrowing(
        session_id="session-b",
        project="mnemos",
    )

    with pytest.raises(PermissionError, match="proposal source"):
        DialogDecisionPush(
            tmp_path / "trusted.db",
            wiki_base=tmp_path,
        ).decide(
            proposal.proposal_id,
            "reject",
            reason="cross-session attempt",
            **access,
        )

    assert ProposalQueue(
        tmp_path / "trusted.db",
        wiki_base=tmp_path,
    ).get(proposal.proposal_id).status == proposal.status
    with sqlite3.connect(str(tmp_path / "producer_consumer_ledger.db")) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_revisions"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("source_session_id", "source_agent", "message"),
    (
        (None, "codex", "source session is unavailable"),
        ("dialog-session", None, "principal agent does not match"),
        ("dialog-session", "other-agent", "principal agent does not match"),
    ),
)
def test_dialog_decision_fails_closed_for_unbound_source_scope(
    tmp_path: Path,
    source_session_id: str | None,
    source_agent: str | None,
    message: str,
) -> None:
    proposal = _proposal(
        tmp_path,
        name="unbound-source",
        source_session_id=source_session_id,
        source_agent=source_agent,
    )

    with pytest.raises(PermissionError, match=message):
        DialogDecisionPush(
            tmp_path / "trusted.db",
            wiki_base=tmp_path,
        ).decide(
            proposal.proposal_id,
            "reject",
            reason="must not mutate",
            **_decision_access(tmp_path),
        )

    assert ProposalQueue(
        tmp_path / "trusted.db",
        wiki_base=tmp_path,
    ).get(proposal.proposal_id).status == proposal.status
    with sqlite3.connect(str(tmp_path / "producer_consumer_ledger.db")) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_revisions"
        ).fetchone()[0] == 0


def test_authenticated_dialog_reject_records_one_canonical_reaction(tmp_path: Path):
    proposal = _proposal(tmp_path, name="authenticated-reject")
    result = DialogDecisionPush(
        tmp_path / "trusted.db",
        wiki_base=tmp_path,
    ).decide(
        proposal.proposal_id,
        "reject",
        reason="not useful",
        **_decision_access(tmp_path),
    )

    canonical = result["canonical_feedback"]
    assert canonical["disposition"] == "record_only"
    assert len(canonical["terminal_receipts"]) == 7
    assert {item["disposition"] for item in canonical["terminal_receipts"]} == {
        "intentional_skip"
    }
    assert all(
        item["schema_version"]
        == "mnemos.feedback_cognitive_update_receipt.v1"
        and item["target_command_hash"].startswith("sha256:")
        and item["attribution_payload_hash"].startswith("sha256:")
        and "decision_trace_refs" in item
        and "action_refs" in item
        and "reciprocal_receipt_refs" in item
        and "superseded_effect_refs" in item
        and "neutralized_effect_refs" in item
        for item in canonical["terminal_receipts"]
    )
    assert not (tmp_path / "feedback_signals.db").exists()


def test_inline_reject_closes_intercepted_origin_command(
    tmp_path: Path,
    monkeypatch,
):
    db_path = tmp_path / "trusted.db"
    page = tmp_path / "reject-origin.md"
    content = "# Reject origin\n"
    monkeypatch.setattr(
        "core.trust.config.get_config",
        lambda: _trusted_config(tmp_path, db_path, "enforce"),
    )
    origin_action = trusted_markdown_action_authorization(
        tmp_path,
        target_path=page,
        content=content,
        proposed_action="update_markdown",
    )
    submitted = TrustedVaultMutationService(wiki_base=tmp_path).submit_markdown(
        target_path=page,
        content=content,
        source="unit_test",
        evidence_refs=("test:reject-origin",),
        actor="codex",
        source_session_id="dialog-session",
        material_action=origin_action,
    )

    result = DialogDecisionPush(db_path, wiki_base=tmp_path).decide(
        submitted.proposal_id,
        "reject",
        reason="not wanted",
        **_decision_access(tmp_path),
    )

    assert result["status"] == "rejected"
    assert not page.exists()
    with sqlite3.connect(str(tmp_path / "producer_consumer_ledger.db")) as conn:
        row = conn.execute(
            """
            SELECT status, before_hash, after_hash
            FROM cognitive_state_effect_receipts WHERE command_id=?
            """,
            (submitted.material_command_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "rejected"
    assert row[1] == row[2]


def test_inline_edit_revokes_origin_command_and_removes_stale_binding(
    tmp_path: Path,
    monkeypatch,
):
    db_path = tmp_path / "trusted.db"
    page = tmp_path / "edit-origin.md"
    content = "# Original\n"
    monkeypatch.setattr(
        "core.trust.config.get_config",
        lambda: _trusted_config(tmp_path, db_path, "enforce"),
    )
    origin_action = trusted_markdown_action_authorization(
        tmp_path,
        target_path=page,
        content=content,
        proposed_action="update_markdown",
    )
    submitted = TrustedVaultMutationService(wiki_base=tmp_path).submit_markdown(
        target_path=page,
        content=content,
        source="unit_test",
        evidence_refs=("test:edit-origin",),
        actor="codex",
        source_session_id="dialog-session",
        material_action=origin_action,
    )

    result = DialogDecisionPush(db_path, wiki_base=tmp_path).decide(
        submitted.proposal_id,
        "edit",
        content="# Revised\n",
        reason="change content",
        **_decision_access(tmp_path),
    )

    assert result["status"] in {"validated", "needs_manual_review"}
    revised = ProposalQueue(db_path, wiki_base=tmp_path).get(submitted.proposal_id)
    assert "material_action" not in revised.candidate.payload
    with sqlite3.connect(str(tmp_path / "producer_consumer_ledger.db")) as conn:
        row = conn.execute(
            "SELECT status FROM cognitive_state_effect_receipts WHERE command_id=?",
            (submitted.material_command_id,),
        ).fetchone()
    assert row == ("revoked",)


def test_inline_snooze_and_edit_are_audited_and_edit_rechecks_gate(tmp_path: Path):
    proposal = _proposal(tmp_path, name="edit")
    db_path = tmp_path / "trusted.db"
    push = DialogDecisionPush(db_path, wiki_base=tmp_path)

    access = _decision_access(tmp_path)
    snooze = push.decide(
        proposal.proposal_id,
        "snooze",
        reason="later",
        **access,
    )
    assert snooze["status"] == "snoozed"
    assert push.push()["cards"] == []

    edited = push.decide(
        proposal.proposal_id,
        "edit",
        content="# edited\n\napi_key=REDACT_ME_1234567890",
        reason="add details",
        supersedes_event_id=snooze["canonical_feedback"]["feedback_event_id"],
        **access,
    )

    assert edited["status"] == "needs_manual_review"
    assert [
        event["event_type"]
        for event in WriteJournal(db_path).events_for_proposal(proposal.proposal_id)
    ] == ["snooze", "edit"]
