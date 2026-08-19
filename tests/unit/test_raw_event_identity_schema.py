"""Legacy Raw identity migration tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.sync_framework.raw_event_identity_schema import (
    RawEventIdentitySchemaMigrationError,
    apply,
    inspect,
)
from core.sync_framework.raw_event_store import (
    RawEventIdentitySchemaMigrationRequired,
    RawEventStore,
)


class _Config:
    def __init__(self, database_dir):
        self.database_dir = database_dir

    def get(self, key, default=None):  # noqa: ARG002
        return default


def test_identity_schema_inspection_does_not_label_unavailable_uninitialized(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "raw_events.db"
    original_stat = Path.stat

    def denied(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("sentinel")
        return original_stat(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    assert inspect(path)["status"] == "unreadable"
    with pytest.raises(
        RawEventIdentitySchemaMigrationError,
        match="schema is unavailable",
    ):
        apply(path)


def test_identity_schema_immutable_inspection_rejects_uninspectable_wal(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "raw_events.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sentinel(value INTEGER)")
    wal_path = Path(f"{path}-wal")
    original_stat = Path.stat

    def denied(candidate, *args, **kwargs):
        if candidate == wal_path:
            raise PermissionError("sentinel")
        return original_stat(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    result = inspect(path)

    assert result["status"] == "unreadable"
    assert result["error"] == "raw_identity_wal_unavailable"


def _legacy_raw_schema(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE raw_turns (
                event_id TEXT PRIMARY KEY,
                current_revision_id TEXT,
                source_agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_number INTEGER NOT NULL,
                model_tag TEXT,
                conversation_at TEXT,
                captured_at TEXT NOT NULL,
                origin TEXT NOT NULL,
                source_path TEXT,
                source_files_json TEXT,
                content_hash TEXT NOT NULL,
                full_content_hash TEXT,
                completeness_status TEXT NOT NULL,
                completeness_json TEXT,
                metadata_json TEXT,
                tool_calls_json TEXT,
                tool_results_json TEXT,
                attachments_json TEXT,
                raw_event_refs_json TEXT,
                reasoning_blob BLOB,
                user_content_blob BLOB NOT NULL,
                assistant_content_blob BLOB NOT NULL,
                compression TEXT NOT NULL DEFAULT 'zlib',
                raw_bytes INTEGER NOT NULL DEFAULT 0,
                quality_rank INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                UNIQUE(source_agent, session_id, turn_number)
            );
            CREATE TABLE raw_turn_revisions (
                revision_id TEXT PRIMARY KEY,
                logical_event_id TEXT NOT NULL,
                revision_number INTEGER NOT NULL,
                supersedes_revision_id TEXT,
                content_hash TEXT NOT NULL,
                full_content_hash TEXT,
                snapshot_blob BLOB NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(logical_event_id) REFERENCES raw_turns(event_id)
            );
            CREATE TABLE raw_provenance_edges (
                edge_id TEXT PRIMARY KEY,
                source_revision_id TEXT NOT NULL,
                span_start INTEGER NOT NULL,
                span_end INTEGER NOT NULL,
                consumer_type TEXT NOT NULL,
                consumer_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(source_revision_id)
                    REFERENCES raw_turn_revisions(revision_id) ON DELETE RESTRICT
            );
            CREATE TABLE raw_provenance_gaps (
                gap_id TEXT PRIMARY KEY,
                consumer_type TEXT NOT NULL,
                consumer_id TEXT NOT NULL,
                source_agent TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_rebuild',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                UNIQUE(consumer_type, consumer_id, reason)
            );
            """
        )
        conn.execute(
            """
            INSERT INTO raw_turns (
                event_id, current_revision_id, source_agent, session_id, turn_number,
                captured_at, origin, content_hash, completeness_status,
                reasoning_blob, user_content_blob, assistant_content_blob, updated_at
            ) VALUES ('legacy-event', 'legacy-revision', 'codex', 'session', 0,
                      '2026-07-12T00:00:00', 'sync_engine', 'hash', 'complete',
                      x'', x'78', x'79', '2026-07-12T00:00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO raw_turn_revisions (
                revision_id, logical_event_id, revision_number, content_hash, snapshot_blob, created_at
            ) VALUES ('legacy-revision', 'legacy-event', 0, 'hash', x'789c030000000001',
                      '2026-07-12T00:00:00')
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_turn_unique_requires_explicit_migration(tmp_path):
    path = tmp_path / "raw_events.db"
    _legacy_raw_schema(path)

    assert inspect(path)["status"] == "migration_required"
    with pytest.raises(RawEventIdentitySchemaMigrationRequired):
        RawEventStore(db_path=path, config=_Config(tmp_path))

    result = apply(path)
    assert result["status"] == "current"
    store = RawEventStore(db_path=path, config=_Config(tmp_path))
    try:
        first = store.upsert_turn(
            source_agent="codex",
            session_id="session",
            turn_number=0,
            user_content="one",
            assistant_content="one",
            metadata={"native_event_id": "native-1"},
        )
        second = store.upsert_turn(
            source_agent="codex",
            session_id="session",
            turn_number=0,
            user_content="two",
            assistant_content="two",
            metadata={"native_event_id": "native-2"},
        )
        assert first != second
        assert (
            store._pool.get_conn().execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM raw_turns WHERE event_id='legacy-event'"
            ).fetchone()[0]
            == 1
        )
    finally:
        store.close()


def test_identity_migration_receipts_orphan_provenance_before_rebuilding(tmp_path):
    path = tmp_path / "raw_events.db"
    _legacy_raw_schema(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO raw_provenance_edges (
                edge_id, source_revision_id, span_start, span_end,
                consumer_type, consumer_id, created_at
            ) VALUES (
                'orphan-edge', 'missing-revision', 0, 9,
                'wiki_page', 'legacy-page', '2026-07-12T00:00:00'
            )
            """
        )

    before = inspect(path)
    assert before["status"] == "migration_required"
    assert before["orphan_provenance_edges"] == 1

    result = apply(path)

    assert result["status"] == "current"
    assert result["orphan_provenance_edges"] == 0
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_provenance_edges").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        receipt = conn.execute(
            """
            SELECT edge_id, source_revision_id, span_start, span_end,
                   consumer_type, consumer_id
            FROM raw_provenance_orphan_receipts
            """
        ).fetchone()
        assert receipt == (
            "orphan-edge",
            "missing-revision",
            0,
            9,
            "wiki_page",
            "legacy-page",
        )
        gap = conn.execute(
            """
            SELECT consumer_type, consumer_id, status
            FROM raw_provenance_gaps
            WHERE consumer_type='wiki_page' AND consumer_id='legacy-page'
            """
        ).fetchone()
        assert gap == ("wiki_page", "legacy-page", "pending_rebuild")
