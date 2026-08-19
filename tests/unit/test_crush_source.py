# -*- coding: utf-8 -*-
"""Tests for the Crush Agent source adapter."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.sync_framework.agent_source import (
    SessionInfo,
    native_session_artifact_evidence_hash,
)
from core.sync_framework.raw_event_store import RawEventStore
from core.sync_framework.sync_engine import SyncEngine
from integrations.sources.crush_source import CrushSource


@pytest.fixture
def crush_db(tmp_path: Path) -> Path:
    """Create a minimal Crush SQLite database in a temp directory."""
    db_path = tmp_path / "crush.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            parent_session_id TEXT,
            title TEXT NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            cost REAL NOT NULL DEFAULT 0.0,
            updated_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            summary_message_id TEXT,
            todos TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            parts TEXT NOT NULL DEFAULT '[]',
            model TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            finished_at INTEGER,
            provider TEXT,
            is_summary_message INTEGER DEFAULT 0 NOT NULL
        );
        CREATE TABLE read_files (
            session_id TEXT NOT NULL,
            path TEXT NOT NULL,
            read_at INTEGER NOT NULL,
            PRIMARY KEY (path, session_id)
        );
        """
    )

    conn.execute(
        """
        INSERT INTO sessions (id, parent_session_id, title, message_count, updated_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "sess-1",
            "parent-1",
            "test session",
            5,
            1_700_000_000_000,
            1_700_000_000_000,
        ),
    )

    messages = [
        (
            "msg-1",
            "sess-1",
            "user",
            json.dumps([{"type": "text", "data": {"text": "hello crush"}}]),
            1_700_000_000_100,
        ),
        (
            "msg-2",
            "sess-1",
            "assistant",
            json.dumps(
                [
                    {"type": "text", "data": {"text": "hi there"}},
                    {
                        "type": "tool_call",
                        "data": {
                            "id": "tc-1",
                            "name": "ls",
                            "input": '{"path": "/tmp"}',
                        },
                    },
                ]
            ),
            1_700_000_000_200,
        ),
        (
            "msg-3",
            "sess-1",
            "tool",
            json.dumps(
                [
                    {
                        "type": "tool_result",
                        "data": {
                            "tool_call_id": "tc-1",
                            "name": "ls",
                            "content": "a.txt b.txt",
                            "metadata": '{"files": 2}',
                        },
                    }
                ]
            ),
            1_700_000_000_300,
        ),
        (
            "msg-4",
            "sess-1",
            "user",
            json.dumps([{"type": "text", "data": {"text": "next turn"}}]),
            1_700_000_000_400,
        ),
        (
            "msg-5",
            "sess-1",
            "assistant",
            json.dumps([{"type": "text", "data": {"text": "got it"}}]),
            1_700_000_000_500,
        ),
    ]
    conn.executemany(
        """
        INSERT INTO messages
        (id, session_id, role, parts, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [(m[0], m[1], m[2], m[3], m[4], m[4]) for m in messages],
    )

    conn.execute(
        "INSERT INTO read_files (session_id, path, read_at) VALUES (?, ?, ?)",
        ("sess-1", "/tmp/a.txt", 1_700_000_000),
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def source(crush_db: Path) -> CrushSource:
    """Create a CrushSource pointed at the temp database."""
    s = CrushSource()
    s._override_data_dir = crush_db.parent
    return s


class _CrushRawConfig:
    """Hermetic config for multi-root Crush Native-to-Raw verification."""

    def __init__(self, root: Path):
        self.data_dir = root
        self.database_dir = root
        self.wiki_dir = root / "wiki"
        self.raw_dir = root / "raw"
        self.obsidian_vault_path = self.raw_dir

    def get(self, key, default=None):  # noqa: ANN001
        values = {
            "storage.max_content_bytes": 200_000,
            "capture.reasoning_mode": "artifact_summary",
            "raw_event_store.enabled": True,
            "raw_projection.enabled": True,
        }
        return values.get(key, default)


class _NoConfiguredCrushRoot:
    def get(self, _key, default=None):  # noqa: ANN001
        return default


class _ConfiguredCrushRoot:
    def __init__(self, root: Path):
        self.root = root

    def get(self, key, default=None):  # noqa: ANN001
        if key == "integrations.crush.home":
            return str(self.root)
        return default


def _create_multi_session_crush_db(
    db_path: Path,
    sessions: list[tuple[str, str, str]],
) -> Path:
    """Create a minimal native Crush database with deterministic content IDs."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            parent_session_id TEXT,
            title TEXT NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            parts TEXT NOT NULL,
            model TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            finished_at INTEGER
        );
        CREATE TABLE read_files (
            session_id TEXT NOT NULL,
            path TEXT NOT NULL,
            read_at INTEGER NOT NULL,
            PRIMARY KEY (path, session_id)
        );
        """
    )
    for index, (session_id, user_text, assistant_text) in enumerate(sessions):
        created_at = 1_700_100_000_000 + index * 10
        conn.execute(
            """
            INSERT INTO sessions (id, parent_session_id, title, message_count, updated_at, created_at)
            VALUES (?, '', ?, 2, ?, ?)
            """,
            (session_id, f"title-{session_id}", created_at + 2, created_at),
        )
        conn.executemany(
            """
            INSERT INTO messages
            (id, session_id, role, parts, model, created_at, updated_at, finished_at)
            VALUES (?, ?, ?, ?, 'crush-test', ?, ?, ?)
            """,
            [
                (
                    f"{session_id}-user",
                    session_id,
                    "user",
                    json.dumps([{"type": "text", "data": {"text": user_text}}]),
                    created_at,
                    created_at,
                    created_at,
                ),
                (
                    f"{session_id}-assistant",
                    session_id,
                    "assistant",
                    json.dumps([{"type": "text", "data": {"text": assistant_text}}]),
                    created_at + 1,
                    created_at + 1,
                    created_at + 1,
                ),
            ],
        )
    conn.commit()
    conn.close()
    return db_path


def _session_inventory(sessions: list[SessionInfo]) -> dict[str, dict[str, object]]:
    """Compare the complete canonical discovery projection, not a reduced subset."""
    return {
        session.session_id: {
            "source_path": str(session.source_path),
            "working_dir": session.working_dir,
            "mtime": session.mtime,
            "canonical_session_id": session.canonical_session_id,
            "session_aliases": session.session_aliases,
            "source_kind": session.source_kind,
            "metadata": session.metadata,
        }
        for session in sessions
    }


def _raw_inventory(
    runtime_root: Path,
    sessions: list[SessionInfo],
) -> dict[tuple[str, int], dict[str, object]]:
    """Project discovered sessions through the real SyncEngine-to-Raw seam."""
    config = _CrushRawConfig(runtime_root)
    backend = Mock()
    backend.list_by_tags.return_value = []
    backend.save.return_value = []
    raw_store = RawEventStore(
        db_path=config.database_dir / "raw_events.db",
        config=config,
    )
    engine = SyncEngine(
        backend=backend,
        db_path=str(config.database_dir / "sync_log.db"),
        config=config,
        raw_store=raw_store,
    )
    try:
        result = engine.sync_batch(CrushSource(), sessions, incremental=False)
        assert result.failed == []
        rows = raw_store._pool.get_conn().execute(  # noqa: SLF001
            """
            SELECT session_id, turn_number, current_revision_id
            FROM raw_turns WHERE source_agent='crush'
            ORDER BY session_id, turn_number
            """
        ).fetchall()
        inventory: dict[tuple[str, int], dict[str, object]] = {}
        for session_id, turn_number, revision_id in rows:
            stored = raw_store.get_turn(revision_id)
            assert stored is not None
            metadata = dict(stored["metadata"])
            metadata.pop("support_latest_native_contract_observation_id", None)
            metadata.pop("support_latest_native_contract_observed_at", None)
            inventory[(session_id, turn_number)] = {
                "user_content": stored["user_content"],
                "assistant_content": stored["assistant_content"],
                "content_hash": stored["content_hash"],
                "source_path": stored["source_path"],
                "source_files": stored["source_files"],
                "metadata": metadata,
                "tool_calls": stored["tool_calls"],
                "tool_results": stored["tool_results"],
                "attachments": stored["attachments"],
                "raw_event_refs": stored["raw_event_refs"],
                "completeness": stored["completeness"],
            }
        return inventory
    finally:
        engine.close()


class TestCrushSource:
    def test_name_and_model_tag(self, source: CrushSource):
        assert source.name == "crush"
        assert source.model_tag == "crush"

    def test_data_dir_resolves_to_db_parent(self, source: CrushSource, crush_db: Path):
        assert source.data_dir == crush_db.parent
        assert source.db_path == crush_db

    def test_data_dir_prefers_project_local_crush_db(self, tmp_path: Path, monkeypatch):
        project = tmp_path / "project"
        db_dir = project / ".crush"
        db_dir.mkdir(parents=True)
        (db_dir / "crush.db").write_text("", encoding="utf-8")
        monkeypatch.chdir(project)

        source = CrushSource()

        assert source.data_dir == db_dir
        assert source.db_path == db_dir / "crush.db"

    def test_multi_root_discovery_is_cwd_stable_and_preserves_raw_provenance(
        self, tmp_path: Path, monkeypatch
    ):
        """Every declared root is complete and projects to cwd-stable canonical Raw."""
        home = tmp_path / "home"
        project = tmp_path / "project"
        nested = project / "src" / "nested"
        installation = tmp_path / "installed-crush"
        environment_home = tmp_path / "environment-home"
        environment_data = tmp_path / "environment-data"
        nested.mkdir(parents=True)
        project_db = _create_multi_session_crush_db(
            project / ".crush" / "crush.db",
            [
                ("project-only", "project user", "project assistant"),
                ("shared", "same user", "same assistant"),
                ("conflict", "project conflict", "project answer"),
                ("clone-conflict", "clone user", "clone answer"),
            ],
        )
        global_db = _create_multi_session_crush_db(
            home / ".crush" / "crush.db",
            [
                ("global-only", "global user", "global assistant"),
                ("shared", "same user", "same assistant"),
                ("conflict", "global conflict", "global answer"),
                ("clone-conflict", "clone user", "clone answer"),
            ],
        )
        installation_db = _create_multi_session_crush_db(
            installation / "crush.db",
            [
                ("installation-only", "installation user", "installation assistant"),
                ("shared", "same user", "same assistant"),
                ("conflict", "installation conflict", "installation answer"),
                ("clone-conflict", "divergent user", "divergent answer"),
            ],
        )
        environment_home_db = _create_multi_session_crush_db(
            environment_home / "crush.db",
            [("environment-home-only", "environment home user", "environment home answer")],
        )
        environment_data_db = _create_multi_session_crush_db(
            environment_data / "crush.db",
            [("environment-data-only", "environment data user", "environment data answer")],
        )
        standard_config_db = _create_multi_session_crush_db(
            home / ".config" / "crush" / "crush.db",
            [("standard-config-only", "standard config user", "standard config answer")],
        )
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("CRUSH_HOME", str(environment_home))
        monkeypatch.setenv("CRUSH_DATA_DIR", str(environment_data))
        monkeypatch.setattr(
            "integrations.sources.crush_source.get_config",
            lambda: _ConfiguredCrushRoot(installation),
        )

        monkeypatch.chdir(project)
        project_source = CrushSource()
        from_project = project_source.discover_sessions()
        monkeypatch.chdir(nested)
        from_nested = CrushSource().discover_sessions()

        assert _session_inventory(from_project) == _session_inventory(from_nested)
        expected_databases = {
            project_db,
            global_db,
            installation_db,
            environment_home_db,
            environment_data_db,
            standard_config_db,
        }
        assert {session.source_path for session in from_project} <= expected_databases
        assert set(project_source.db_paths) == expected_databases
        assert set(project_source.observed_roots()) == {
            path.parent for path in expected_databases
        }
        assert {session.session_id for session in from_project} >= {
            "project-only",
            "global-only",
            "installation-only",
            "environment-home-only",
            "environment-data-only",
            "standard-config-only",
            "shared",
        }
        conflict_sessions = [
            session for session in from_project if session.metadata["native_session_id"] == "conflict"
        ]
        assert len(conflict_sessions) == 3
        assert len({session.session_id for session in conflict_sessions}) == 3
        shared = next(session for session in from_project if session.session_id == "shared")
        assert shared.metadata["source_database_count"] == 3
        assert len(shared.metadata["source_database_ids"]) == 3

        project_raw = _raw_inventory(tmp_path / "runtime-project", from_project)
        nested_raw = _raw_inventory(tmp_path / "runtime-nested", from_nested)
        assert project_raw == nested_raw
        assert set(project_raw) == {
            (session.session_id, 0) for session in from_project
        }

    def test_discovered_non_latest_session_reaches_its_own_canonical_raw(
        self, tmp_path: Path
    ):
        """Session-aware parsing must never fall back to the newest database row."""
        db_path = _create_multi_session_crush_db(
            tmp_path / "native" / "crush.db",
            [
                ("older-session", "older unique user", "older unique answer"),
                ("latest-session", "latest unique user", "latest unique answer"),
            ],
        )
        source = CrushSource()
        source._override_data_dir = db_path.parent
        older = next(
            session
            for session in source.discover_sessions()
            if session.session_id == "older-session"
        )

        inventory = _raw_inventory(tmp_path / "runtime-non-latest", [older])

        stored = inventory[("older-session", 0)]
        assert stored["user_content"] == "older unique user"
        assert stored["assistant_content"] == "older unique answer"
        assert stored["metadata"]["native_session_id"] == "older-session"

    def test_same_native_messages_with_divergent_session_metadata_do_not_collapse(
        self,
        tmp_path: Path,
    ):
        first = _create_multi_session_crush_db(
            tmp_path / "first" / "crush.db",
            [("same", "user", "assistant")],
        )
        second = _create_multi_session_crush_db(
            tmp_path / "second" / "crush.db",
            [("same", "user", "assistant")],
        )
        with sqlite3.connect(second) as connection:
            connection.execute(
                "UPDATE sessions SET title='different-title' WHERE id='same'"
            )
        source = CrushSource()
        source._override_data_dirs = [first, second]

        sessions = source.discover_sessions()

        assert len(sessions) == 2
        assert {
            session.metadata["canonical_identity_mode"]
            for session in sessions
        } == {"divergent_database"}

    def test_clone_session_evidence_binds_every_represented_database(
        self,
        tmp_path: Path,
    ):
        first = _create_multi_session_crush_db(
            tmp_path / "first" / "crush.db",
            [("same", "user", "assistant")],
        )
        second = _create_multi_session_crush_db(
            tmp_path / "second" / "crush.db",
            [("same", "user", "assistant")],
        )
        source = CrushSource()
        source._override_data_dirs = [first, second]
        session = source.discover_sessions()[0]

        before = native_session_artifact_evidence_hash(source, session)
        with sqlite3.connect(second) as connection:
            connection.execute(
                "UPDATE sessions SET title='changed-after-discovery' WHERE id='same'"
            )
        after = native_session_artifact_evidence_hash(source, session)

        assert session.metadata["source_database_count"] == 2
        assert len(session.source_paths) == 2
        assert before != after

    def test_discover_sessions(self, source: CrushSource, crush_db: Path):
        sessions = source.discover_sessions()
        assert len(sessions) == 1
        assert sessions[0].session_id == "sess-1"
        assert sessions[0].source_path == crush_db
        assert sessions[0].mtime > 0
        assert sessions[0].metadata["parent_session_id"] == "parent-1"
        assert sessions[0].metadata["title"] == "test session"
        assert sessions[0].metadata["message_count"] == 5
        assert sessions[0].metadata["created_at"] == "2023-11-14T22:13:20+00:00"
        assert sessions[0].metadata["updated_at"] == "2023-11-14T22:13:20+00:00"

    def test_parse_turns(self, source: CrushSource, crush_db: Path):
        source.discover_sessions()
        turns = source.parse_turns(crush_db)

        assert len(turns) == 2

        t0 = turns[0]
        assert t0.turn_number == 0
        assert t0.user_content == "hello crush"
        assert t0.assistant_content == "hi there"
        assert len(t0.tool_calls) == 1
        assert t0.tool_calls[0]["name"] == "ls"
        assert t0.tool_calls[0]["arguments"] == {"path": "/tmp"}
        assert len(t0.tool_results) == 1
        assert t0.tool_results[0]["output"] == "a.txt b.txt"
        assert t0.tool_results[0]["metadata"] == {"files": 2}
        assert "/tmp/a.txt" in t0.source_files
        assert t0.attachments == [{"type": "read_file", "path": "/tmp/a.txt"}]
        assert t0.completeness["attachments"] == "full"

        t1 = turns[1]
        assert t1.turn_number == 1
        assert t1.user_content == "next turn"
        assert t1.assistant_content == "got it"
        assert not t1.tool_calls

    def test_parse_session_preserves_consecutive_assistant_text(
        self, source: CrushSource, crush_db: Path
    ):
        conn = sqlite3.connect(crush_db)
        conn.execute(
            """
            INSERT INTO messages
            (id, session_id, role, parts, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "msg-2b",
                "sess-1",
                "assistant",
                json.dumps([{"type": "text", "data": {"text": "continued"}}]),
                1_700_000_000_250,
                1_700_000_000_250,
            ),
        )
        conn.commit()
        conn.close()

        turns = source.parse_session(
            SessionInfo(
                session_id="sess-1",
                source_path=crush_db,
                metadata={"native_session_id": "sess-1"},
            )
        )

        assert turns[0].assistant_content == "hi there\ncontinued"
        assert turns[0].tool_calls[0]["name"] == "ls"
        assert turns[0].tool_results[0]["output"] == "a.txt b.txt"

    def test_standalone_nontext_messages_reach_canonical_raw(
        self, source: CrushSource, crush_db: Path, tmp_path: Path
    ):
        conn = sqlite3.connect(crush_db)
        conn.execute(
            """
            INSERT INTO sessions
            (id, parent_session_id, title, message_count, updated_at, created_at)
            VALUES ('sess-meta', '', 'metadata only', 11, ?, ?)
            """,
            (1_700_000_001_000, 1_700_000_001_000),
        )
        conn.executemany(
            """
            INSERT INTO messages
            (id, session_id, role, parts, created_at, updated_at)
            VALUES (?, 'sess-meta', ?, ?, ?, ?)
            """,
            [
                (
                    "meta-tool-call",
                    "assistant",
                    json.dumps(
                        [
                            {
                                "type": "tool_call",
                                "data": {
                                    "id": "tc-meta",
                                    "name": "pwd",
                                    "input": "{}",
                                },
                            },
                            {
                                "type": "future_assistant_part",
                                "data": {"value": "assistant-opaque"},
                            },
                        ]
                    ),
                    1_700_000_001_100,
                    1_700_000_001_100,
                ),
                (
                    "meta-tool-result",
                    "tool",
                    json.dumps(
                        [
                            {
                                "type": "tool_result",
                                "data": {
                                    "tool_call_id": "tc-meta",
                                    "name": "pwd",
                                    "content": "/workspace",
                                    "metadata": "{}",
                                },
                            }
                        ]
                    ),
                    1_700_000_001_200,
                    1_700_000_001_200,
                ),
                (
                    "meta-unknown",
                    "system",
                    json.dumps(
                        [{"type": "future_native_part", "data": {"value": "opaque"}}]
                    ),
                    1_700_000_001_300,
                    1_700_000_001_300,
                ),
                (
                    "meta-malformed",
                    "assistant",
                    "{invalid-native-parts",
                    1_700_000_001_400,
                    1_700_000_001_400,
                ),
                (
                    "meta-malformed-known-type",
                    "assistant",
                    json.dumps(
                        [{"type": "text", "data": "opaque-malformed-text"}]
                    ),
                    1_700_000_001_500,
                    1_700_000_001_500,
                ),
                (
                    "meta-invalid-utf8",
                    "assistant",
                    sqlite3.Binary(b"{\xffbroken-native-parts"),
                    1_700_000_001_600,
                    1_700_000_001_600,
                ),
                (
                    "meta-malformed-tool-call",
                    "assistant",
                    json.dumps(
                        [{"type": "tool_call", "data": "opaque-call-data"}]
                    ),
                    1_700_000_001_700,
                    1_700_000_001_700,
                ),
                (
                    "meta-malformed-tool-result",
                    "tool",
                    json.dumps(
                        [{"type": "tool_result", "data": "opaque-result-data"}]
                    ),
                    1_700_000_001_800,
                    1_700_000_001_800,
                ),
                (
                    "meta-lossy-tool-call-value",
                    "assistant",
                    json.dumps(
                        [
                            {
                                "type": "tool_call",
                                "data": {"id": "lossy-call", "name": "bad", "input": 0},
                            }
                        ]
                    ),
                    1_700_000_001_900,
                    1_700_000_001_900,
                ),
                (
                    "meta-lossy-tool-result-value",
                    "tool",
                    json.dumps(
                        [
                            {
                                "type": "tool_result",
                                "data": {
                                    "tool_call_id": "lossy-call",
                                    "name": "bad",
                                    "content": "result",
                                    "metadata": 0,
                                    "is_error": "false",
                                },
                            }
                        ]
                    ),
                    1_700_000_002_000,
                    1_700_000_002_000,
                ),
                (
                    "meta-non-array-json",
                    "assistant",
                    json.dumps({"opaque": "non-array-native-parts"}),
                    1_700_000_002_100,
                    1_700_000_002_100,
                ),
            ],
        )
        conn.commit()
        conn.close()
        session = SessionInfo(
            session_id="sess-meta",
            source_path=crush_db,
            canonical_session_id="sess-meta",
            source_kind="sqlite",
            metadata={
                "native_session_id": "sess-meta",
                "source_database_id": "crush-db-meta",
                "source_database_ids": ["crush-db-meta"],
            },
        )
        turns = source.parse_session(session)
        assert len(turns) == 1

        config = _CrushRawConfig(tmp_path / "runtime-metadata")
        raw_store = RawEventStore(
            db_path=config.database_dir / "raw_events.db",
            config=config,
        )
        backend = Mock()
        backend.list_by_tags.return_value = []
        backend.save.return_value = []
        engine = SyncEngine(
            backend=backend,
            db_path=str(config.database_dir / "sync_log.db"),
            config=config,
            raw_store=raw_store,
        )
        try:
            result = engine.sync_single_turn(
                source,
                session,
                turns[0],
                incremental=False,
            )
            assert result.action != "failed"
            assert result.raw_event_id
            stored = raw_store.get_turn(result.raw_event_id)
            assert stored is not None
            assert stored["tool_calls"][0]["name"] == "pwd"
            assert stored["tool_results"][0]["output"] == "/workspace"
            assert {
                ref["role"]
                for ref in stored["raw_event_refs"]
                if "role" in ref
            } == {
                "assistant",
                "system",
                "tool",
            }
            assert any(
                ref.get("event_type") == "native_session"
                for ref in stored["raw_event_refs"]
            )
            assert sum(
                ref.get("event_type") == "native_message"
                for ref in stored["raw_event_refs"]
            ) == 11
            malformed = next(
                ref
                for ref in stored["raw_event_refs"]
                if ref.get("message_id") == "meta-malformed"
            )
            assert malformed["parts"][0]["data"]["raw"] == "{invalid-native-parts"
            assert malformed["parts"][0]["data"]["decode_error"] == "invalid_json"
            malformed_known = next(
                ref
                for ref in stored["raw_event_refs"]
                if ref.get("message_id") == "meta-malformed-known-type"
            )
            assert malformed_known["parts"] == [
                {"type": "text", "data": "opaque-malformed-text"}
            ]
            invalid_utf8 = next(
                ref
                for ref in stored["raw_event_refs"]
                if ref.get("message_id") == "meta-invalid-utf8"
            )
            assert invalid_utf8["parts"][0]["data"] == {
                "raw_base64": "e/9icm9rZW4tbmF0aXZlLXBhcnRz",
                "raw_encoding": "base64",
                "decode_error": "invalid_utf8",
            }
            malformed_call = next(
                ref
                for ref in stored["raw_event_refs"]
                if ref.get("message_id") == "meta-malformed-tool-call"
            )
            assert malformed_call["parts"][0]["data"] == "opaque-call-data"
            malformed_result = next(
                ref
                for ref in stored["raw_event_refs"]
                if ref.get("message_id") == "meta-malformed-tool-result"
            )
            assert malformed_result["parts"][0]["data"] == "opaque-result-data"
            lossy_call = next(
                ref
                for ref in stored["raw_event_refs"]
                if ref.get("message_id") == "meta-lossy-tool-call-value"
            )
            assert lossy_call["parts"][0]["data"]["input"] == 0
            lossy_result = next(
                ref
                for ref in stored["raw_event_refs"]
                if ref.get("message_id") == "meta-lossy-tool-result-value"
            )
            assert lossy_result["parts"][0]["data"]["metadata"] == 0
            assert lossy_result["parts"][0]["data"]["is_error"] == "false"
            non_array = next(
                ref
                for ref in stored["raw_event_refs"]
                if ref.get("message_id") == "meta-non-array-json"
            )
            assert non_array["parts"][0]["data"] == {
                "raw": '{"opaque": "non-array-native-parts"}',
                "decode_error": "non_array_json",
            }
        finally:
            engine.close()

    def test_get_session_state(self, source: CrushSource, crush_db: Path):
        info = SessionInfo(session_id="sess-1", source_path=crush_db)
        state = source.get_session_state(info)

        assert state is not None
        assert state["size"] == 5
        assert state["file_count"] == 5
        assert state["mtime"] > 0
        assert state["fingerprint"].startswith("sha256:")
        assert state["fingerprint_contract"] == "crush-exact-session-rows-sha256-v1"

    def test_parse_turns_fallback_session_id(self, source: CrushSource, crush_db: Path):
        # Without discover_sessions(), parse_turns should still resolve the latest session.
        turns = source.parse_turns(crush_db)
        assert len(turns) == 2
        assert turns[0].user_content == "hello crush"
