import sqlite3
from pathlib import Path
from types import SimpleNamespace

from core.cli.commands.proposal import cmd_proposal
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.trust import CandidateBundle, ProposalQueue, WriteJournal


def _proposal(tmp_path: Path, *, name: str):
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")
    target = tmp_path / f"{name}.md"
    candidate = CandidateBundle.from_payload(
        source="hephaestus_distillation",
        source_agent="mnemos-cli",
        source_session_id="cli-test-session",
        target_kind="markdown",
        target_path=str(target),
        payload={"title": f"{name} title", "content": f"# {name}\n\nBody"},
        evidence_refs=["session:abc"],
        risk_level="medium",
    )
    return ProposalQueue(tmp_path / "trusted.db", wiki_base=tmp_path).submit_candidate(
        candidate
    )


def _args(tmp_path: Path, proposal_id: str, proposal_cmd: str, **overrides):
    values = {
        "proposal_cmd": proposal_cmd,
        "proposal_id": proposal_id,
        "db_path": str(tmp_path / "trusted.db"),
        "wiki_base": str(tmp_path),
        "json": True,
        "reason": "",
        "yes": True,
        "allow_high_risk": False,
        "content": None,
        "content_file": "",
        "editor": "",
        "snooze_hours": 24,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _decision_rows(db_path: Path, proposal_id: str) -> list[str]:
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT decision FROM user_decisions WHERE proposal_id = ? ORDER BY rowid",
            (proposal_id,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def test_legacy_reject_cli_uses_dialog_decision_journal(tmp_path: Path, capsys):
    proposal = _proposal(tmp_path, name="reject")
    db_path = tmp_path / "trusted.db"

    status = cmd_proposal(
        _args(tmp_path, proposal.proposal_id, "reject", reason="not useful")
    )

    capsys.readouterr()
    assert status == 0
    assert ProposalQueue(db_path, wiki_base=tmp_path).get(proposal.proposal_id).status == "rejected"
    assert _decision_rows(db_path, proposal.proposal_id) == ["reject"]
    assert [
        event["event_type"]
        for event in WriteJournal(db_path).events_for_proposal(proposal.proposal_id)
    ] == ["reject"]


def test_legacy_edit_cli_uses_dialog_decision_journal(tmp_path: Path, capsys):
    proposal = _proposal(tmp_path, name="edit")
    db_path = tmp_path / "trusted.db"

    status = cmd_proposal(
        _args(
            tmp_path,
            proposal.proposal_id,
            "edit",
            content="# edited\n\nUpdated body",
        )
    )

    capsys.readouterr()
    updated = ProposalQueue(db_path, wiki_base=tmp_path).get(proposal.proposal_id)
    assert status == 0
    assert updated.revision == 1
    assert _decision_rows(db_path, proposal.proposal_id) == ["edit"]
    assert [
        event["event_type"]
        for event in WriteJournal(db_path).events_for_proposal(proposal.proposal_id)
    ] == ["edit"]


def test_legacy_approve_cli_uses_dialog_decision_writer_path(
    tmp_path: Path,
    capsys,
):
    proposal = _proposal(tmp_path, name="approve")
    db_path = tmp_path / "trusted.db"

    status = cmd_proposal(_args(tmp_path, proposal.proposal_id, "approve"))

    capsys.readouterr()
    assert status == 0
    assert (tmp_path / "approve.md").read_text(encoding="utf-8").startswith("# approve")
    assert ProposalQueue(db_path, wiki_base=tmp_path).get(proposal.proposal_id).status == "committed"
    assert _decision_rows(db_path, proposal.proposal_id) == ["approve"]
    assert [
        event["event_type"]
        for event in WriteJournal(db_path).events_for_proposal(proposal.proposal_id)
    ] == ["prepare", "commit"]
