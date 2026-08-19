from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.privacy.data_ownership import DataOwnershipManager
from core.ops.durable_io import DurableIOError
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn
from core.sync_framework.agent_source_metadata_deletion import (
    delete_agent_source_metadata_subject_scope,
)
from core.sync_framework.sync_engine import SyncEngine


class _Config:
    def __init__(self, root: Path) -> None:
        self.mnemos_dir = root
        self.data_dir = root
        self.database_dir = root / "db"
        self.wiki_dir = root / "wiki"
        self.raw_dir = root / "raw"
        self.obsidian_vault_path = self.raw_dir
        self.database_dir.mkdir(parents=True)

    def get(self, _key: str, default=None):
        return default


class _Source(AgentSource):
    @property
    def name(self) -> str:
        return "codex"

    @property
    def model_tag(self) -> str:
        return "codex"

    def discover_sessions(self):
        return []

    def parse_turns(self, _session_path: Path):
        return []

    def on_session_start(self, _session_id: str, _context: dict):
        return {}

    def on_session_end(self, _session_id: str, _messages: list):
        return None


def _engine(tmp_path: Path) -> tuple[_Config, SyncEngine, Path]:
    config = _Config(tmp_path)
    database = config.database_dir / "sync_log.db"
    engine = SyncEngine(backend=Mock(), db_path=str(database), config=config)
    return config, engine, database


def _seed_current_source_metadata(database: Path) -> None:
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            INSERT INTO sync_log (
                agent_name, session_id, turn_number, content_hash, status
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("codex", "subject-session", 0, "subject-hash", "synced"),
        )
        conn.execute(
            """
            INSERT INTO sync_log (
                agent_name, session_id, turn_number, content_hash, status
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("other", "other-session", 0, "other-hash", "synced"),
        )
        conn.execute(
            """
            INSERT INTO user_signals (
                timestamp, agent, session_id, turn_number, content_length,
                has_code, has_tools, user_questions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-07-16T00:00:00+00:00", "codex", "subject-session", 0, 8, 0, 0, 0),
        )
        conn.execute(
            """
            INSERT INTO sync_audit (source, audit_type, created_at)
            VALUES (?, ?, ?)
            """,
            ("codex", "l1_scan", 1.0),
        )


def test_session_delete_removes_exact_metadata_but_blocks_on_unmapped_audit(tmp_path):
    _config, engine, database = _engine(tmp_path)
    try:
        _seed_current_source_metadata(database)

        result = delete_agent_source_metadata_subject_scope(
            db_path=database,
            request_id="delete-source-session",
            scope_kind="session",
            scope_value="subject-session",
        )
    finally:
        engine.close()

    assert result == {
        "status": "applied",
        "target_count": 2,
        "receipt_count": 1,
        "sync_log_deleted": 1,
        "user_signals_deleted": 1,
        "sync_audit_deleted": 0,
        "after_count": 0,
        "unresolved_sync_audit_count": 1,
        "verified": False,
    }
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sync_log WHERE session_id=?",
                ("subject-session",),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM user_signals WHERE session_id=?",
                ("subject-session",),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sync_log WHERE session_id=?",
                ("other-session",),
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("SELECT COUNT(*) FROM sync_audit").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_source_metadata_deletion_receipts"
        ).fetchone()[0] == 1


def test_metadata_deletion_does_not_certify_unavailable_store_as_uninitialized(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "sync_log.db"
    original_stat = Path.stat

    def denied(path, *args, **kwargs):
        if path == database:
            raise PermissionError("sentinel")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    with pytest.raises(DurableIOError, match="durable_path_inspection_failed"):
        delete_agent_source_metadata_subject_scope(
            db_path=database,
            request_id="delete-unavailable-store",
            scope_kind="all",
            scope_value="all",
        )


def test_incompatible_receipt_schema_blocks_without_hidden_schema_effect(
    tmp_path: Path,
) -> None:
    _config, engine, database = _engine(tmp_path)
    try:
        _seed_current_source_metadata(database)
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                CREATE TABLE agent_source_metadata_deletion_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    scope_kind TEXT NOT NULL,
                    scope_value_hash TEXT NOT NULL,
                    sync_log_deleted INTEGER NOT NULL,
                    user_signals_deleted INTEGER NOT NULL,
                    sync_audit_deleted INTEGER NOT NULL,
                    after_count INTEGER NOT NULL,
                    unresolved_sync_audit_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(request_id, scope_kind, scope_value_hash)
                )
                """
            )
            before_objects = connection.execute(
                """
                SELECT type, name, COALESCE(sql, '')
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            ).fetchall()

        result = delete_agent_source_metadata_subject_scope(
            db_path=database,
            request_id="delete-incompatible-receipt",
            scope_kind="agent",
            scope_value="codex",
        )

        with sqlite3.connect(database) as connection:
            after_objects = connection.execute(
                """
                SELECT type, name, COALESCE(sql, '')
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            ).fetchall()
            subject_rows = connection.execute(
                "SELECT COUNT(*) FROM sync_log WHERE agent_name='codex'"
            ).fetchone()
    finally:
        engine.close()

    assert result["status"] == "blocked"
    assert result["error"] == "agent_source_metadata_receipt_schema_incompatible"
    assert after_objects == before_objects
    assert subject_rows == (1,)


def test_agent_delete_removes_attributable_audit_and_verifies_after_oracle(tmp_path):
    _config, engine, database = _engine(tmp_path)
    try:
        _seed_current_source_metadata(database)

        result = delete_agent_source_metadata_subject_scope(
            db_path=database,
            request_id="delete-source-agent",
            scope_kind="agent",
            scope_value="codex",
        )
    finally:
        engine.close()

    assert result == {
        "status": "applied",
        "target_count": 3,
        "receipt_count": 1,
        "sync_log_deleted": 1,
        "user_signals_deleted": 1,
        "sync_audit_deleted": 1,
        "after_count": 0,
        "unresolved_sync_audit_count": 0,
        "verified": True,
    }
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sync_log WHERE agent_name=?", ("codex",)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM user_signals WHERE agent=?", ("codex",)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sync_audit WHERE source=?", ("codex",)).fetchone()[0] == 0


def test_existing_receipt_rechecks_after_oracle_and_refuses_reinserted_metadata(tmp_path):
    _config, engine, database = _engine(tmp_path)
    try:
        _seed_current_source_metadata(database)
        applied = delete_agent_source_metadata_subject_scope(
            db_path=database,
            request_id="delete-source-recheck",
            scope_kind="agent",
            scope_value="codex",
        )
        assert applied["verified"] is True
        with sqlite3.connect(database) as conn:
            conn.execute(
                """
                INSERT INTO sync_log (
                    agent_name, session_id, turn_number, content_hash, status
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("codex", "unexpected-reinsert", 1, "unexpected-hash", "synced"),
            )

        retry = delete_agent_source_metadata_subject_scope(
            db_path=database,
            request_id="delete-source-recheck",
            scope_kind="agent",
            scope_value="codex",
        )
    finally:
        engine.close()

    assert retry["status"] == "blocked"
    assert retry["verified"] is False
    assert retry["error"] == "agent_source_metadata_after_oracle_nonzero"
    with sqlite3.connect(database) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM sync_log WHERE session_id=?",
                ("unexpected-reinsert",),
            ).fetchone()[0]
            == 1
        )


def test_new_request_resumes_a_nonterminal_scope_receipt_before_creating_another(tmp_path):
    _config, engine, database = _engine(tmp_path)
    try:
        _seed_current_source_metadata(database)
        applied = delete_agent_source_metadata_subject_scope(
            db_path=database,
            request_id="delete-source-incomplete",
            scope_kind="agent",
            scope_value="codex",
        )
        assert applied["status"] == "applied"
        with sqlite3.connect(database) as conn:
            # Simulate a process crash after physical deletion but before the
            # WAL-terminal receipt transition.
            conn.execute(
                """
                UPDATE agent_source_metadata_deletion_receipts
                SET status='flushed', applied_at=''
                WHERE request_id=?
                """,
                ("delete-source-incomplete",),
            )

        resumed = delete_agent_source_metadata_subject_scope(
            db_path=database,
            request_id="delete-source-retry-with-new-id",
            scope_kind="agent",
            scope_value="codex",
        )
    finally:
        engine.close()

    assert resumed["status"] == "applied"
    assert resumed["target_count"] == 3
    assert resumed["verified"] is True
    with sqlite3.connect(database) as conn:
        rows = conn.execute(
            """
            SELECT request_id, status
            FROM agent_source_metadata_deletion_receipts
            ORDER BY created_at
            """
        ).fetchall()
    assert rows == [("delete-source-incomplete", "applied")]


def test_unknown_legacy_source_table_blocks_before_any_delete(tmp_path):
    _config, engine, database = _engine(tmp_path)
    try:
        _seed_current_source_metadata(database)
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE sessions (session_id TEXT, body TEXT)")
            conn.execute("INSERT INTO sessions VALUES (?, ?)", ("subject-session", "legacy body"))

        result = delete_agent_source_metadata_subject_scope(
            db_path=database,
            request_id="delete-source-legacy",
            scope_kind="session",
            scope_value="subject-session",
        )
    finally:
        engine.close()

    assert result["status"] == "blocked"
    assert result["verified"] is False
    assert result["error"] == "unknown_source_metadata_tables"
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sync_log WHERE session_id=?", ("subject-session",)).fetchone()[0] == 1


def test_frozen_scope_blocks_sync_log_reinsertion(tmp_path):
    config, engine, database = _engine(tmp_path)
    try:
        DataOwnershipManager(config).freeze("session:subject-session")

        with pytest.raises(PermissionError, match="data ownership freeze"):
            engine._sync_log.record_sync(  # noqa: SLF001 - exact persistence boundary.
                "codex",
                "subject-session",
                0,
                "subject-hash",
                [],
                "synced",
            )

        source = _Source()
        result = engine.sync_single_turn(
            source,
            SessionInfo(session_id="subject-session", source_path=tmp_path / "subject.json"),
            Turn(turn_number=0, user_content="private", assistant_content="response"),
            incremental=False,
        )
    finally:
        engine.close()

    assert result.action == "blocked"
    assert result.error == "data_ownership_freeze"
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM user_signals").fetchone()[0] == 0
