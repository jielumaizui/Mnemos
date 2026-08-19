import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.trust import CandidateBundle, ProposalQueue, WriteJournal
from core.cognitive.decision_trace import MaterialActionAuthorization
from core.trust.knowledge_vault_writer import KnowledgeVaultWriter
from core.trust.vault_mutation_service import TrustedVaultMutationService
from tests.cognitive_decision_fixtures import (
    knowledge_vault_action_authorization,
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


@pytest.mark.no_canonical_material_actions
def test_writer_fails_closed_without_material_authorization(tmp_path: Path):
    db_path = tmp_path / "trusted.db"
    page = tmp_path / "untraced.md"
    candidate = CandidateBundle.from_payload(
        source="test",
        target_kind="markdown",
        target_path=str(page),
        payload={"content": "# Untraced\n"},
        evidence_refs=["session:missing-decision"],
    )
    queue = ProposalQueue(db_path, wiki_base=tmp_path)
    proposal = queue.submit_candidate(candidate)

    with pytest.raises(PermissionError, match="material-action authorization"):
        KnowledgeVaultWriter(wiki_base=tmp_path, db_path=db_path).write_proposal(
            proposal.proposal_id,
        )

    assert not page.exists()


def test_writer_commits_native_store_and_markdown(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "trusted.db"
    page = tmp_path / "page.md"
    candidate = CandidateBundle.from_payload(
        source="test",
        target_kind="markdown",
        target_path=str(page),
        payload={"content": "# Proposed\n"},
        evidence_refs=["session:1"],
    )
    queue = ProposalQueue(db_path, wiki_base=tmp_path)
    proposal = queue.submit_candidate(candidate)
    resumed = []

    class Bus:
        def resume_deferred(self, proposal_id):
            resumed.append(proposal_id)
            return 1

    monkeypatch.setattr("core.mnemos_bus.get_event_bus", lambda: Bus())

    result = KnowledgeVaultWriter(wiki_base=tmp_path, db_path=db_path).write_proposal(
        proposal.proposal_id,
        material_action=knowledge_vault_action_authorization(
            tmp_path,
            proposal_id=proposal.proposal_id,
            target_uri=str(page),
            content="# Proposed\n",
        ),
    )

    assert result["status"] == "committed"
    assert resumed == [proposal.proposal_id]
    assert page.read_text(encoding="utf-8") == "# Proposed\n"
    assert queue.get(proposal.proposal_id).status == "committed"
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT content_hash FROM native_store WHERE proposal_id = ?",
            (proposal.proposal_id,),
        ).fetchone()
    assert row is not None
    assert [
        event["event_type"]
        for event in WriteJournal(db_path).events_for_proposal(proposal.proposal_id)
    ] == ["prepare", "commit"]


def test_writer_recovers_committed_target_without_duplicate(
    tmp_path: Path,
    monkeypatch,
):
    db_path = tmp_path / "trusted.db"
    page = tmp_path / "crash-safe.md"
    content = "# Crash-safe vault write\n"
    candidate = CandidateBundle.from_payload(
        source="test",
        target_kind="markdown",
        target_path=str(page),
        payload={"content": content},
        evidence_refs=["session:crash-safe-vault"],
    )
    queue = ProposalQueue(db_path, wiki_base=tmp_path)
    proposal = queue.submit_candidate(candidate)
    authorization = knowledge_vault_action_authorization(
        tmp_path,
        proposal_id=proposal.proposal_id,
        target_uri=str(page),
        content=content,
    )

    class Bus:
        def resume_deferred(self, proposal_id):
            return 1

    monkeypatch.setattr("core.mnemos_bus.get_event_bus", lambda: Bus())
    original = MaterialActionAuthorization.record_terminal
    crashed = False

    def crash_after_target(self, terminal):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise OSError("crash after knowledge-vault target commit")
        return original(self, terminal)

    monkeypatch.setattr(
        MaterialActionAuthorization,
        "record_terminal",
        crash_after_target,
    )
    writer = KnowledgeVaultWriter(wiki_base=tmp_path, db_path=db_path)
    with pytest.raises(OSError, match="after knowledge-vault target commit"):
        writer.write_proposal(
            proposal.proposal_id,
            material_action=authorization,
        )

    monkeypatch.setattr(
        MaterialActionAuthorization,
        "record_terminal",
        original,
    )
    result = writer.write_proposal(
        proposal.proposal_id,
        material_action=authorization,
    )

    assert result["status"] == "committed"
    assert page.read_text(encoding="utf-8") == content
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM native_store").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM journal_events WHERE proposal_id=?",
            (proposal.proposal_id,),
        ).fetchone()[0] == 2
    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_effect_receipts"
        ).fetchone()[0] == 1


def test_writer_closes_origin_markdown_command_after_enforced_approval(
    tmp_path: Path,
    monkeypatch,
):
    db_path = tmp_path / "trusted.db"
    page = tmp_path / "approved.md"
    content = "# Approved\n"
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
        evidence_refs=("test:trusted-origin",),
        material_action=origin_action,
    )
    assert submitted.intercepted

    writer_action = knowledge_vault_action_authorization(
        tmp_path,
        proposal_id=submitted.proposal_id,
        target_uri=str(page),
        content=content,
    )
    result = KnowledgeVaultWriter(
        wiki_base=tmp_path,
        db_path=db_path,
    ).write_proposal(
        submitted.proposal_id,
        allow_high_risk=True,
        material_action=writer_action,
    )

    assert result["status"] == "committed"
    assert page.read_text(encoding="utf-8") == content
    with sqlite3.connect(str(tmp_path / "producer_consumer_ledger.db")) as conn:
        rows = conn.execute(
            """
            SELECT command_id, status FROM cognitive_state_effect_receipts
            WHERE command_id IN (?, ?)
            ORDER BY command_id
            """,
            (submitted.material_command_id, writer_action.permit.command_id),
        ).fetchall()
    assert rows == sorted(
        [
            (submitted.material_command_id, "committed"),
            (writer_action.permit.command_id, "committed"),
        ]
    )


def test_writer_recovers_origin_command_after_approval_crash(
    tmp_path: Path,
    monkeypatch,
):
    import core.trust.vault_mutation_service as mutation_module

    db_path = tmp_path / "trusted.db"
    page = tmp_path / "origin-recovery.md"
    content = "# Origin recovery\n"
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
        material_action=origin_action,
    )
    writer_action = knowledge_vault_action_authorization(
        tmp_path,
        proposal_id=submitted.proposal_id,
        target_uri=str(page),
        content=content,
    )
    original = mutation_module.record_trusted_markdown_observed_terminal
    crashed = False

    def crash_before_origin_terminal(*args, **kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise OSError("crash before origin Markdown terminal")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        mutation_module,
        "record_trusted_markdown_observed_terminal",
        crash_before_origin_terminal,
    )
    writer = KnowledgeVaultWriter(wiki_base=tmp_path, db_path=db_path)
    with pytest.raises(OSError, match="before origin Markdown terminal"):
        writer.write_proposal(
            submitted.proposal_id,
            allow_high_risk=True,
            material_action=writer_action,
        )

    monkeypatch.setattr(
        mutation_module,
        "record_trusted_markdown_observed_terminal",
        original,
    )
    result = writer.write_proposal(
        submitted.proposal_id,
        allow_high_risk=True,
        material_action=writer_action,
    )

    assert result["status"] == "committed"
    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        rows = conn.execute(
            """SELECT command_id, status FROM cognitive_state_effect_receipts
               WHERE command_id IN (?, ?) ORDER BY command_id""",
            (origin_action.permit.command_id, writer_action.permit.command_id),
        ).fetchall()
    assert rows == sorted(
        [
            (origin_action.permit.command_id, "committed"),
            (writer_action.permit.command_id, "committed"),
        ]
    )


def test_writer_rolls_back_native_store_on_markdown_conflict(tmp_path: Path):
    db_path = tmp_path / "trusted.db"
    page = tmp_path / "page.md"
    page.write_text("# User edit\n", encoding="utf-8")
    candidate = CandidateBundle.from_payload(
        source="test",
        target_kind="markdown",
        target_path=str(page),
        payload={"content": "# Proposed\n"},
        evidence_refs=["session:1"],
        risk_level="high",
    )
    queue = ProposalQueue(db_path, wiki_base=tmp_path)
    proposal = queue.submit_candidate(candidate)

    result = KnowledgeVaultWriter(wiki_base=tmp_path, db_path=db_path).write_proposal(
        proposal.proposal_id,
        allow_high_risk=True,
        material_action=knowledge_vault_action_authorization(
            tmp_path,
            proposal_id=proposal.proposal_id,
            target_uri=str(page),
            content="# Proposed\n",
        ),
    )

    assert result["status"] == "failed"
    assert result["reason"] == "markdown_conflict"
    assert queue.get(proposal.proposal_id).status == "failed"
    assert page.read_text(encoding="utf-8") == "# User edit\n"
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT proposal_id FROM native_store WHERE proposal_id = ?",
            (proposal.proposal_id,),
        ).fetchone()
    assert row is None
    assert [
        event["event_type"]
        for event in WriteJournal(db_path).events_for_proposal(proposal.proposal_id)
    ] == ["prepare", "rollback"]
