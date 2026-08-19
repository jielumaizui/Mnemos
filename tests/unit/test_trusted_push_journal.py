import sqlite3
from pathlib import Path

from core.trust.models import JournalEventInput
from core.trust.recovery import TrustedPushRecovery
from core.trust.write_journal import WriteJournal


def test_journal_is_append_only_and_hash_chain_verifies(tmp_path: Path):
    db_path = tmp_path / "trusted.db"
    journal = WriteJournal(db_path)

    journal.append_event(
        JournalEventInput(
            proposal_id="prop1",
            event_type="prepare",
            target_uri=str(tmp_path / "page.md"),
            content_hash="abc",
        )
    )
    journal.append_event(
        JournalEventInput(
            proposal_id="prop1",
            event_type="commit",
            target_uri=str(tmp_path / "page.md"),
            content_hash="abc",
        )
    )

    with sqlite3.connect(db_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(journal_events)")]
        count = conn.execute("SELECT COUNT(*) FROM journal_events").fetchone()[0]

    assert "phase" not in columns
    assert count == 2
    assert journal.verify_hash_chain() is True


def test_recovery_aborts_prepare_without_target(tmp_path: Path):
    db_path = tmp_path / "trusted.db"
    journal = WriteJournal(db_path)
    journal.append_event(
        JournalEventInput(
            proposal_id="prop1",
            event_type="prepare",
            target_uri=str(tmp_path / "missing.md"),
            content_hash="abc",
        )
    )

    dry_run = TrustedPushRecovery(db_path).recover()

    assert dry_run == [
        {
            "proposal_id": "prop1",
            "event": "planned_abort",
            "target_uri": str(tmp_path / "missing.md"),
            "applied": False,
        }
    ]
    assert journal.events_for_proposal("prop1")[-1]["event_type"] == "prepare"

    applied = TrustedPushRecovery(db_path).recover(apply=True)

    assert applied == [
        {
            "proposal_id": "prop1",
            "event": "abort",
            "target_uri": str(tmp_path / "missing.md"),
            "applied": True,
        }
    ]
    assert journal.events_for_proposal("prop1")[-1]["event_type"] == "abort"
