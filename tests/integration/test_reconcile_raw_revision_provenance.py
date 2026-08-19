from __future__ import annotations

import sqlite3

from core.sync_framework.raw_event_store import RawEventStore
from scripts.reconcile_raw_revision_provenance import reconcile


def test_reconcile_backs_up_edges_provable_pages_and_marks_legacy_gaps(tmp_path):
    db_path = tmp_path / "raw_events.db"
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    store = RawEventStore(db_path=db_path)
    revision_id = store.upsert_turn(
        source_agent="codex",
        session_id="session-1",
        turn_number=0,
        user_content="abc",
        assistant_content="defgh",
    )
    store.close()
    (wiki_dir / "provable.md").write_text(
        "---\n"
        "source: codex\n"
        "source_session: session-1\n"
        "raw_event_refs:\n"
        f"  - revision_id: {revision_id}\n"
        "    span_start: 0\n"
        "    span_end: 8\n"
        "---\n# Provable\n",
        encoding="utf-8",
    )
    (wiki_dir / "legacy.md").write_text(
        "---\nsource: codex\nsource_session: session-legacy\n---\n# Legacy\n",
        encoding="utf-8",
    )

    dry_run = reconcile(
        db_path=db_path,
        wiki_dir=wiki_dir,
        apply=False,
        backup_root=tmp_path / "backups",
    )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_provenance_edges").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM raw_provenance_gaps").fetchone()[0] == 0
    assert dry_run["provable_edges"] == 1
    assert dry_run["provenance_gaps"] == 1

    applied = reconcile(
        db_path=db_path,
        wiki_dir=wiki_dir,
        apply=True,
        backup_root=tmp_path / "backups",
    )

    assert applied["integrity_check"] == "ok"
    assert applied["edges_recorded"] == 1
    assert applied["gaps_recorded"] == 1
    assert applied["gap_status"] == {"pending_rebuild": 1}
    assert list((tmp_path / "backups").glob("root004-*/raw_events.db"))
    store = RawEventStore(db_path=db_path)
    try:
        assert store.list_provenance_edges(revision_id)[0]["consumer_type"] == "wiki_page"
        assert store.get_metrics(revision_id)["reference_count"] == 1
    finally:
        store.close()
