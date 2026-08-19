"""Cross-layer red oracles for the Phase 1 Native -> Raw contract.

These tests deliberately use the real adapters and the real continuous-sync
canonicalization seam.  They are not a synthetic 12-source substitute.
"""

from __future__ import annotations

import ast
import importlib
import json
import hashlib
import os
import sqlite3
import time
from pathlib import Path

import pytest

from core.sync_framework.agent_source import (
    NativeSourceContractError,
    SessionInfo,
    native_session_artifact_evidence_hash,
    parse_discovered_session_result,
)
from core.sync_framework import agent_source as agent_source_contract
from core.sync_framework.native_artifact_inventory import snapshot_native_sources
from daemon.agent_sync_cursor import AgentSyncCursorError, CURSOR_SCHEMA_VERSION
from daemon.raw_sync import _canonical_sessions
from integrations.sources.aider_source import AiderSource
from integrations.sources.base import native_path_kind, stable_path_session_id
from integrations.sources.claude_source import ClaudeSource
from integrations.sources.codex_source import CodexSource
from integrations.sources.cursor_source import CursorSource
from integrations.sources import cursor_source
from integrations.sources.crush_source import CrushSource
from integrations.sources.gemini_cli_source import GeminiCliSource
from integrations.sources.hermes_source import HermesSource
from integrations.sources.kiro_source import KiroSource
from integrations.sources import kimi_payload, openclaw_payload
from integrations.sources.openclaw_payload import read_native_jsonl
from integrations.sources.opencode_source import OpenCodeSource
from integrations.sources.windsurf_source import WindsurfSource
from scripts import reconcile_agent_source_raw_capture as raw_reconciler


def test_native_source_path_inspection_never_folds_unavailable_into_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "native-root"
    assert native_path_kind(target) == "missing"
    original_stat = Path.stat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == target:
            raise PermissionError("sentinel")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    with pytest.raises(
        NativeSourceContractError,
        match="native_path_inspection_unavailable",
    ):
        native_path_kind(target)


def test_phase1_native_file_consumers_have_no_legacy_read_or_copy_bypass() -> None:
    from core.agent_kit.source_support_manifest import (
        get_agent_source_support_manifest,
    )
    from core.sync_framework.registry import SourceRegistry

    SourceRegistry.register_builtin_agents()
    module_names = {
        "core.sync_framework.agent_source",
        "core.sync_framework.native_artifact_inventory",
        "integrations.sources.base",
        "integrations.sources.kimi_payload",
        "integrations.sources.openclaw_payload",
    }
    module_names.update(
        SourceRegistry.get_builtin_source_class(source_name).__module__
        for source_name in get_agent_source_support_manifest().active_source_names
    )
    modules = tuple(importlib.import_module(name) for name in sorted(module_names))
    violations: list[str] = []
    for module in modules:
        module_path = Path(str(module.__file__ or ""))
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "read_bytes"
            ):
                violations.append(f"{module.__name__}:read_bytes:{node.lineno}")
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "open"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "rb"
            ):
                violations.append(f"{module.__name__}:open-rb:{node.lineno}")
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "shutil"
                and node.func.attr in {"copy", "copy2", "copyfile"}
            ):
                violations.append(
                    f"{module.__name__}:shutil-{node.func.attr}:{node.lineno}"
                )

    assert violations == []


@pytest.mark.parametrize(
    "reader",
    (kimi_payload.read_native_jsonl, openclaw_payload.read_native_jsonl),
)
def test_phase1_native_jsonl_readers_reject_leaf_symlinks(
    reader,
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.jsonl"
    artifact.write_text("{}\n", encoding="utf-8")
    alias = tmp_path / "artifact-link.jsonl"
    alias.symlink_to(artifact)

    with pytest.raises(NativeSourceContractError):
        reader(alias)


def test_active_native_source_modules_have_no_lossy_path_predicates() -> None:
    from core.agent_kit.source_support_manifest import (
        get_agent_source_support_manifest,
    )
    from core.sync_framework.registry import SourceRegistry

    SourceRegistry.register_builtin_agents()
    violations: list[str] = []
    module_names = {"integrations.sources.base"}
    for source_name in get_agent_source_support_manifest().active_source_names:
        source_class = SourceRegistry.get_builtin_source_class(source_name)
        module_names.add(source_class.__module__)
    for module_name in sorted(module_names):
        module = importlib.import_module(module_name)
        module_path = Path(str(module.__file__ or ""))
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"exists", "is_file", "is_dir"}
            ):
                violations.append(f"{module_path.name}:{node.lineno}:{node.func.attr}")

    assert violations == []


def test_registered_source_runtime_failure_never_downgrades_to_absent_or_dynamic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.sync_framework.registry import (
        SourceRegistry,
        SourceRegistryUnavailableError,
    )

    SourceRegistry.register_builtin_agents()
    SourceRegistry.reset()

    def unavailable(_self: CodexSource) -> Path:
        raise OSError("sentinel")

    monkeypatch.setattr(CodexSource, "data_dir", property(unavailable))
    with pytest.raises(SourceRegistryUnavailableError, match="source_registry_unavailable:codex"):
        SourceRegistry.get("codex")


def test_path_discovery_probe_failure_is_unavailable_only_when_no_concrete_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.sync_framework import registry
    from core.sync_framework.registry import (
        PathDiscover,
        PathDiscoveryUnavailableError,
    )

    monkeypatch.setattr(PathDiscover, "_load_user_config", lambda _name: {})
    monkeypatch.setattr(
        PathDiscover,
        "_root_resolver",
        lambda _name: {
            "environment": [],
            "standard_paths": ["ignored"],
        },
    )
    monkeypatch.setattr(
        PathDiscover,
        "_discover_from_process",
        lambda _name: (_ for _ in ()).throw(OSError("unavailable")),
    )
    monkeypatch.setattr(PathDiscover, "_heuristic_search", lambda _name: None)
    monkeypatch.setattr(registry, "expand_path_templates", lambda _paths: [])

    with pytest.raises(
        PathDiscoveryUnavailableError,
        match="path_discovery_unavailable:codex:process_probe",
    ):
        PathDiscover._do_find("codex")

    concrete = tmp_path / "codex"
    concrete.mkdir()
    monkeypatch.setattr(
        registry,
        "expand_path_templates",
        lambda _paths: [concrete],
    )
    assert PathDiscover._do_find("codex") == concrete


def test_unreadable_path_configuration_never_becomes_source_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from core.sync_framework.registry import (
        PathDiscover,
        PathDiscoveryUnavailableError,
    )

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "agent_paths.json").write_text("{", encoding="utf-8")
    monkeypatch.setattr(
        "core.sync_framework.registry.get_config",
        lambda: SimpleNamespace(data_dir=tmp_path),
    )

    with pytest.raises(
        PathDiscoveryUnavailableError,
        match="path_discovery_unavailable:codex:user_config",
    ):
        PathDiscover._load_user_config("codex")


def test_uninspectable_path_configuration_never_becomes_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from core.sync_framework.registry import (
        PathDiscover,
        PathDiscoveryUnavailableError,
    )

    target = tmp_path / "configs" / "agent_paths.json"
    original_stat = Path.stat

    def guarded_stat(path: Path, *args: object, **kwargs: object):
        if path == target:
            raise PermissionError("configuration metadata unavailable")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(
        "core.sync_framework.registry.get_config",
        lambda: SimpleNamespace(data_dir=tmp_path),
    )

    with pytest.raises(
        PathDiscoveryUnavailableError,
        match="path_discovery_unavailable:codex:user_config",
    ):
        PathDiscover._load_user_config("codex")


def test_uninspectable_configured_source_root_never_falls_back_to_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.sync_framework.registry import (
        PathDiscover,
        PathDiscoveryUnavailableError,
    )

    target = tmp_path / "codex"
    original_stat = Path.stat

    def guarded_stat(path: Path, *args: object, **kwargs: object):
        if path == target:
            raise PermissionError("source root metadata unavailable")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(
        PathDiscover,
        "_load_user_config",
        lambda _name: {"codex": str(target)},
    )

    with pytest.raises(
        PathDiscoveryUnavailableError,
        match="path_discovery_unavailable:codex:user_config_path",
    ):
        PathDiscover._do_find("codex")


def test_aider_same_project_basename_across_roots_has_distinct_canonical_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    for root in (first_root, second_root):
        project = root / "same-project"
        project.mkdir(parents=True)
        (project / ".aider.chat.history.md").write_text(
            "#### /message\nhello\n#### assistant\nworld\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("AIDER_PROJECT_ROOTS", f"{first_root},{second_root}")

    sessions = AiderSource().discover_sessions()

    assert len(sessions) == 2
    assert len({session.canonical_session_id or session.session_id for session in sessions}) == 2


def test_path_identity_is_stable_when_only_discovery_root_changes(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "outer" / "project" / "session.jsonl"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("", encoding="utf-8")

    assert stable_path_session_id(
        "source",
        tmp_path,
        artifact,
        native_id="session",
    ) == stable_path_session_id(
        "source",
        artifact.parent,
        artifact,
        native_id="session",
    )
    assert stable_path_session_id(
        "source",
        tmp_path,
        artifact,
        native_id="first",
    ) != stable_path_session_id(
        "source",
        tmp_path,
        artifact,
        native_id="second",
    )


def test_file_session_state_is_bound_to_bytes_not_only_size_and_mtime(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "history.json"
    artifact.write_text("first", encoding="utf-8")
    original_times = (time.time() - 10, time.time() - 10)
    os.utime(artifact, original_times)
    source = WindsurfSource()
    session = SessionInfo(session_id="state", source_path=artifact)

    before = source.get_session_state(session)
    artifact.write_text("other", encoding="utf-8")
    os.utime(artifact, original_times)
    after = source.get_session_state(session)

    assert before is not None and after is not None
    assert before["size"] == after["size"]
    assert before["mtime"] == after["mtime"]
    assert before["fingerprint"] != after["fingerprint"]


@pytest.mark.parametrize(
    ("source", "container_key"),
    [
        (CursorSource(), "messages"),
        (WindsurfSource(), "history"),
        (OpenCodeSource(), "conversations"),
    ],
)
def test_json_container_residual_is_preserved_once(
    tmp_path: Path,
    source: object,
    container_key: str,
) -> None:
    path = tmp_path / f"{source.name}.json"  # type: ignore[attr-defined]
    payload = {
        container_key: [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ],
        "unprojected": {"sentinel": "container-must-survive"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    turns = source.parse_turns(path)  # type: ignore[attr-defined]
    serialized = json.dumps(
        [ref for turn in turns for ref in turn.raw_event_refs],
        ensure_ascii=False,
        sort_keys=True,
    )

    assert serialized.count("container-must-survive") == 1
    assert serialized.count("native_session_container_residual") == 1


def test_format_and_root_changes_do_not_alias_distinct_native_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hermes_sessions = tmp_path / "hermes" / "sessions"
    hermes_sessions.mkdir(parents=True)
    (hermes_sessions / "same.json").write_text(
        '{"session_id":"same","messages":[]}',
        encoding="utf-8",
    )
    (hermes_sessions / "same.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    hermes = HermesSource().discover_sessions()
    assert len(hermes) == 2
    assert len({item.canonical_session_id for item in hermes}) == 2

    kiro_ids: list[str] = []
    for root_name in ("kiro-a", "kiro-b"):
        root = tmp_path / root_name / "sessions" / "cli"
        root.mkdir(parents=True)
        (root / "same.jsonl").write_text("", encoding="utf-8")
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / root_name))
        session = KiroSource().discover_sessions()[0]
        kiro_ids.append(str(session.canonical_session_id))
    assert len(set(kiro_ids)) == 2

    global_ids: list[str] = []
    for root_name in ("cursor-a", "cursor-b"):
        root = tmp_path / root_name
        global_dir = root / "User" / "globalStorage"
        global_dir.mkdir(parents=True)
        (global_dir / "chat_history.json").write_text("[]", encoding="utf-8")
        monkeypatch.setenv("CURSOR_HOME", str(root))
        session = CursorSource().discover_sessions()[0]
        global_ids.append(str(session.canonical_session_id))
    assert len(set(global_ids)) == 2


def test_continuous_sync_rejects_duplicate_canonical_sessions_instead_of_dropping_one(
    tmp_path: Path,
) -> None:
    first = SessionInfo(
        session_id="duplicate",
        canonical_session_id="duplicate",
        source_path=tmp_path / "first.jsonl",
        mtime=1,
    )
    second = SessionInfo(
        session_id="duplicate",
        canonical_session_id="duplicate",
        source_path=tmp_path / "second.jsonl",
        mtime=2,
    )

    with pytest.raises(AgentSyncCursorError, match="canonical_session_duplicate"):
        _canonical_sessions([first, second])


def test_cursor_sqlite_discovers_each_native_conversation_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "workspaceStorage" / "workspace-a" / "state.vscdb"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for key, user, assistant in (
            ("chat.alpha", "alpha-user", "alpha-assistant"),
            ("chat.beta", "beta-user", "beta-assistant"),
        ):
            connection.execute(
                "INSERT INTO ItemTable VALUES (?, ?)",
                (
                    key,
                    json.dumps(
                        {
                            "messages": [
                                {"role": "user", "content": user},
                                {"role": "assistant", "content": assistant},
                            ]
                        }
                    ),
                ),
            )
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path))

    source = CursorSource()
    sqlite_sessions = [
        session for session in source.discover_sessions() if session.source_path == database
    ]

    assert len(sqlite_sessions) == 2
    assert len({session.session_id for session in sqlite_sessions}) == 2
    parsed = [source.parse_session(session) for session in sqlite_sessions]
    assert sorted(turns[0].user_content for turns in parsed) == [
        "alpha-user",
        "beta-user",
    ]
    assert all([turn.turn_number for turn in turns] == [0] for turns in parsed)


def test_cursor_sqlite_deduplicates_exact_cross_table_mirror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.vscdb"
    payload = json.dumps(
        {
            "messages": [
                {"role": "user", "content": "user"},
                {"role": "assistant", "content": "assistant"},
            ]
        }
    )
    with sqlite3.connect(database) as connection:
        for table in ("ItemTable", "items"):
            connection.execute(
                f'CREATE TABLE "{table}" (key TEXT PRIMARY KEY, value TEXT NOT NULL)'
            )
            connection.execute(
                f'INSERT INTO "{table}" VALUES (?, ?)',
                ("chat.mirrored", payload),
            )
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path))

    sessions = CursorSource().discover_sessions()

    assert len(sessions) == 1
    assert sessions[0].metadata["native_sqlite_table"] == "ItemTable"
    assert sessions[0].metadata["native_sqlite_tables"] == [
        "ItemTable",
        "items",
    ]
    source = CursorSource()
    baseline = native_session_artifact_evidence_hash(source, sessions[0])
    assert baseline.startswith("sha256:")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE items SET value=? WHERE key='chat.mirrored'",
            ('{"messages":[{"role":"user","content":"changed"}]}',),
        )
    with pytest.raises(
        NativeSourceContractError,
        match="native_cursor_sqlite_identity_conflict",
    ):
        native_session_artifact_evidence_hash(source, sessions[0])


def test_cursor_incremental_state_binds_every_cross_table_mirror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.vscdb"
    payload = json.dumps({"messages": [{"role": "user", "content": "stable"}]})
    with sqlite3.connect(database) as connection:
        for table in ("ItemTable", "items"):
            connection.execute(
                f'CREATE TABLE "{table}" (key TEXT PRIMARY KEY, value TEXT NOT NULL)'
            )
            connection.execute(
                f'INSERT INTO "{table}" VALUES (?, ?)',
                ("chat.mirrored", payload),
            )
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path))
    source = CursorSource()
    session = source.discover_sessions()[0]
    before = source.get_session_state(session)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE items SET value=? WHERE key='chat.mirrored'",
            (json.dumps({"messages": [{"role": "user", "content": "drift"}]}),),
        )

    assert before is not None
    with pytest.raises(
        NativeSourceContractError,
        match="native_cursor_sqlite_identity_conflict",
    ):
        source.get_session_state(session)


def test_cursor_ignores_same_named_relational_table_without_key_value_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.vscdb"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO ItemTable VALUES ('chat.valid', '{\"messages\": []}')")
        connection.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, content TEXT)")
        connection.execute("INSERT INTO messages VALUES ('m1', 'not-kv')")
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path))

    sessions = CursorSource().discover_sessions()

    assert len(sessions) == 1
    assert sessions[0].metadata["native_sqlite_table"] == "ItemTable"


def test_cursor_sqlite_rejects_conflicting_cross_table_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.vscdb"
    with sqlite3.connect(database) as connection:
        for table, value in (
            ("ItemTable", '{"messages": [{"role": "user", "content": "first"}]}'),
            ("items", '{"messages": [{"role": "user", "content": "second"}]}'),
        ):
            connection.execute(
                f'CREATE TABLE "{table}" (key TEXT PRIMARY KEY, value TEXT NOT NULL)'
            )
            connection.execute(
                f'INSERT INTO "{table}" VALUES (?, ?)',
                ("chat.conflict", value),
            )
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path))

    with pytest.raises(
        NativeSourceContractError,
        match="native_cursor_sqlite_identity_conflict",
    ):
        CursorSource().discover_sessions()


def test_cursor_sqlite_preserves_non_utf8_blob_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.vscdb"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
        connection.execute(
            "INSERT INTO ItemTable VALUES (?, ?)",
            ("chat.binary", sqlite3.Binary(b"\xff\x00native")),
        )
    monkeypatch.setenv("CURSOR_HOME", str(tmp_path))

    source = CursorSource()
    session = source.discover_sessions()[0]
    result = parse_discovered_session_result(source, session)

    assert result.disposition == "parsed"
    assert any(
        ref.get("raw_encoding") == "base64" and ref.get("raw_base64") == "/wBuYXRpdmU="
        for turn in result.turns
        for ref in turn.raw_event_refs
    )


def test_hermes_provider_request_failure_is_not_credited_as_empty_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    artifact = sessions_dir / "request-failure.json"
    artifact.write_text(
        json.dumps(
            {
                "error": "provider request failed",
                "reason": "transport",
                "request": {
                    "method": "POST",
                    "url": "https://provider.invalid/v1",
                    "headers": {"authorization": "sensitive-test-value"},
                    "body": {"messages": [{"role": "user", "content": "private"}]},
                },
                "session_id": "request-failure",
                "timestamp": "2026-07-28T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    source = HermesSource()
    session = source.discover_sessions()[0]

    result_parser = getattr(source, "parse_session_result", None)
    assert callable(result_parser)
    result = result_parser(session)
    assert result.disposition == "evidence_excluded"
    assert result.reason_code == "provider_request_failure_artifact"
    assert list(result.turns) == []


def test_hermes_custom_disposition_uses_framework_owned_snapshot_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "session.jsonl").write_text(
        '{"role":"user","content":"u"}\n' '{"role":"assistant","content":"a"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    with snapshot_native_sources([HermesSource()]) as snapshot:
        source = snapshot.sources[0]
        session = source.discover_sessions()[0]
        result = parse_discovered_session_result(source, session)

    assert result.disposition == "parsed"
    assert result.turns[0].user_content == "u"
    assert result.artifact_evidence_hash.startswith("sha256:")


def test_opencode_discovers_sqlite_and_legacy_json_in_one_denominator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "opencode.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE session (
                id TEXT PRIMARY KEY, title TEXT, directory TEXT,
                time_created INTEGER, time_updated INTEGER
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY, session_id TEXT,
                time_created INTEGER, time_updated INTEGER, data TEXT
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                time_created INTEGER, data TEXT
            );
            INSERT INTO session VALUES ('sqlite-session', '', '', 1, 1);
            """)
    legacy_dir = tmp_path / "sessions"
    legacy_dir.mkdir()
    legacy = legacy_dir / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "legacy-user"},
                    {"role": "assistant", "content": "legacy-assistant"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_DB_PATH", str(database))

    sessions = OpenCodeSource().discover_sessions()

    assert {session.source_kind for session in sessions} == {
        "sqlite",
        "json_fallback",
    }
    assert any(session.source_path == legacy for session in sessions)


def test_opencode_sqlite_binds_and_preserves_complete_owned_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "opencode.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE session (
                id TEXT PRIMARY KEY, title TEXT, directory TEXT,
                time_created INTEGER, time_updated INTEGER,
                permission TEXT, metadata TEXT
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY, session_id TEXT,
                time_created INTEGER, time_updated INTEGER, data TEXT
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                time_created INTEGER, time_updated INTEGER, data TEXT
            );
            """)
        connection.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "session",
                "title",
                "/project",
                1,
                2,
                "permission-sentinel",
                '{"future":"session-metadata-sentinel"}',
            ),
        )
        connection.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
            ("message", "session", 3, 4, '{"role":"user"}'),
        )
        connection.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            (
                "part",
                "message",
                "session",
                5,
                6,
                '{"type":"text","text":"visible"}',
            ),
        )
    monkeypatch.setenv("OPENCODE_DB_PATH", str(database))
    source = OpenCodeSource()
    session = source.discover_sessions()[0]

    before = native_session_artifact_evidence_hash(source, session)
    result = parse_discovered_session_result(source, session)
    serialized = json.dumps(
        [
            {
                "metadata": turn.metadata,
                "raw_event_refs": turn.raw_event_refs,
            }
            for turn in result.turns
        ],
        sort_keys=True,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE session SET permission='permission-drift' WHERE id='session'")
        connection.execute("UPDATE part SET time_updated=7 WHERE id='part'")
    after = native_session_artifact_evidence_hash(source, session)

    assert "permission-sentinel" in serialized
    assert "session-metadata-sentinel" in serialized
    assert '"time_updated": 6' in serialized
    assert before != after


def test_crush_sqlite_binds_and_preserves_complete_owned_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "crush.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, parent_session_id TEXT, title TEXT,
                message_count INTEGER, prompt_tokens INTEGER,
                completion_tokens INTEGER, cost REAL, updated_at INTEGER,
                created_at INTEGER, summary_message_id TEXT, todos TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, session_id TEXT, role TEXT, parts TEXT,
                model TEXT, created_at INTEGER, updated_at INTEGER,
                finished_at INTEGER, provider TEXT, is_summary_message INTEGER
            );
            CREATE TABLE read_files (
                session_id TEXT, path TEXT, read_at INTEGER
            );
            """)
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "session",
                "",
                "title",
                1,
                11,
                22,
                0.5,
                2,
                1,
                "summary",
                '["todo-sentinel"]',
            ),
        )
        connection.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "message",
                "session",
                "user",
                '[{"type":"text","text":"visible"}]',
                "model",
                3,
                4,
                5,
                "provider-sentinel",
                0,
            ),
        )
        connection.execute(
            "INSERT INTO read_files VALUES (?, ?, ?)",
            ("session", "/project/file", 6),
        )
    source = CrushSource()
    source._override_data_dirs = [tmp_path]  # noqa: SLF001
    session = source.discover_sessions()[0]

    before = native_session_artifact_evidence_hash(source, session)
    result = parse_discovered_session_result(source, session)
    serialized = json.dumps(
        [
            {
                "metadata": turn.metadata,
                "raw_event_refs": turn.raw_event_refs,
            }
            for turn in result.turns
        ],
        sort_keys=True,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE messages SET provider='provider-drift' WHERE id='message'")
    after = native_session_artifact_evidence_hash(source, session)

    assert "todo-sentinel" in serialized
    assert "provider-sentinel" in serialized
    assert '"read_at": 6' in serialized
    assert before != after


def test_claude_recognized_message_preserves_every_unconsumed_native_field(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "session.jsonl"
    artifact.write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-07-28T00:00:00Z",
                "future_outer": "outer-sentinel",
                "message": {
                    "id": "message",
                    "role": "user",
                    "future_message": "message-sentinel",
                    "content": [
                        {
                            "type": "text",
                            "text": "visible",
                            "future_text": "text-block-sentinel",
                        },
                        {
                            "type": "tool_use",
                            "id": "tool",
                            "name": "bash",
                            "input": {"cmd": "true"},
                            "future_tool": "tool-block-sentinel",
                        },
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = parse_discovered_session_result(
        ClaudeSource(),
        SessionInfo(session_id="session", source_path=artifact),
    )
    serialized = json.dumps(
        [turn.raw_event_refs for turn in result.turns],
        sort_keys=True,
    )

    assert "outer-sentinel" in serialized
    assert "message-sentinel" in serialized
    assert "text-block-sentinel" in serialized
    assert "tool-block-sentinel" in serialized


def test_aider_raw_preserves_exact_markdown_and_nonempty_unknown_history(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / ".aider.chat.history.md"
    native = (
        "#### /message\n"
        "  user keeps edges  \n\n"
        "#### assistant\n"
        "answer\n\n"
        "#### future-native-section\n"
        "opaque-sentinel\n"
    )
    artifact.write_text(native, encoding="utf-8")

    result = parse_discovered_session_result(
        AiderSource(),
        SessionInfo(session_id="aider", source_path=artifact),
    )

    assert result.disposition == "parsed"
    assert any(
        ref.get("event_type") == "native_markdown_history" and ref.get("raw") == native
        for turn in result.turns
        for ref in turn.raw_event_refs
    )

    artifact.write_text(
        "#### future-only\nopaque-only-sentinel\n",
        encoding="utf-8",
    )
    unknown_only = parse_discovered_session_result(
        AiderSource(),
        SessionInfo(session_id="aider", source_path=artifact),
    )

    assert unknown_only.disposition == "parsed"
    assert unknown_only.turns
    assert "opaque-only-sentinel" in json.dumps(
        [turn.raw_event_refs for turn in unknown_only.turns]
    )


@pytest.mark.parametrize(
    ("source_cls", "environment_name", "nested_relative"),
    (
        (
            CursorSource,
            "CURSOR_HOME",
            Path("User/globalStorage/vendor/chat_history.json"),
        ),
        (
            WindsurfSource,
            "WINDSURF_HOME",
            Path("extensions/vendor/history.json"),
        ),
    ),
)
def test_vscode_sources_cover_recursive_manifest_declared_json_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_cls,
    environment_name: str,
    nested_relative: Path,
) -> None:
    artifact = tmp_path / nested_relative
    artifact.parent.mkdir(parents=True)
    artifact.write_text("[]", encoding="utf-8")
    monkeypatch.setenv(environment_name, str(tmp_path))

    sessions = source_cls().discover_sessions()

    assert [session.source_path for session in sessions] == [artifact]


def test_cursor_schema_has_explicit_session_disposition_generation() -> None:
    assert CURSOR_SCHEMA_VERSION == "mnemos.agent_sync_cursor.v5"


def test_normal_daemon_empty_session_has_content_bound_disposition_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    artifact = project / ".aider.chat.history.md"
    artifact.write_text("", encoding="utf-8")
    monkeypatch.setenv("AIDER_PROJECT_ROOTS", str(tmp_path))
    source = AiderSource()
    session = source.discover_sessions()[0]

    result = parse_discovered_session_result(source, session)

    assert result.disposition == "typed_empty"
    assert result.reason_code == "valid_empty_native_session"
    assert result.artifact_evidence_hash.startswith("sha256:")


def test_parse_fails_closed_when_native_artifact_changes_during_parse(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "session.jsonl"
    artifact.write_text("", encoding="utf-8")

    class MutatingSource:
        name = "mutation-oracle"

        def native_artifact_paths(self, _session: SessionInfo) -> list[Path]:
            return [artifact]

        def parse_session(self, _session: SessionInfo) -> list[object]:
            artifact.write_text("changed", encoding="utf-8")
            return []

    session = SessionInfo(session_id="mutation", source_path=artifact)

    with pytest.raises(
        NativeSourceContractError,
        match="native_session_artifact_changed_during_parse",
    ):
        parse_discovered_session_result(MutatingSource(), session)


def test_custom_parser_cannot_self_sign_a_noncanonical_evidence_hash(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "session.json"
    artifact.write_text("{}", encoding="utf-8")

    class SelfSigningSource:
        name = "self-signing-oracle"

        def native_artifact_paths(self, _session: SessionInfo) -> list[Path]:
            return [artifact]

        def parse_session_result(self, _session: SessionInfo):
            from core.sync_framework.agent_source import SessionParseResult

            return SessionParseResult(
                turns=(),
                disposition="typed_empty",
                reason_code="valid_empty_native_session",
                artifact_evidence_hash="sha256:" + ("0" * 64),
            )

    with pytest.raises(
        NativeSourceContractError,
        match="native_session_self_signed_evidence_mismatch",
    ):
        parse_discovered_session_result(
            SelfSigningSource(),
            SessionInfo(session_id="session", source_path=artifact),
        )


def test_sqlite_session_evidence_is_exact_and_does_not_dump_sibling_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor_db = tmp_path / "cursor.vscdb"
    with sqlite3.connect(cursor_db) as connection:
        connection.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO ItemTable VALUES (?, ?)",
            [
                ("chat.target", '{"messages": []}'),
                ("chat.sibling", '{"messages": []}'),
            ],
        )
    cursor_session = SessionInfo(
        session_id="cursor-target",
        source_path=cursor_db,
        source_kind="sqlite_conversation",
        metadata={
            "native_sqlite_table": "ItemTable",
            "native_sqlite_key": "chat.target",
        },
    )

    opencode_db = tmp_path / "opencode.db"
    with sqlite3.connect(opencode_db) as connection:
        connection.executescript("""
            CREATE TABLE session (
                id TEXT PRIMARY KEY, title TEXT, directory TEXT,
                time_created INTEGER, time_updated INTEGER
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY, session_id TEXT,
                time_created INTEGER, time_updated INTEGER, data TEXT
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                time_created INTEGER, data TEXT
            );
            """)
        for session_id in ("target", "sibling"):
            connection.execute(
                "INSERT INTO session VALUES (?, ?, ?, 1, 1)",
                (session_id, session_id, "/project"),
            )
            connection.execute(
                "INSERT INTO message VALUES (?, ?, 1, 1, ?)",
                (
                    f"message-{session_id}",
                    session_id,
                    '{"role":"user"}',
                ),
            )
            connection.execute(
                "INSERT INTO part VALUES (?, ?, ?, 1, ?)",
                (
                    f"part-{session_id}",
                    f"message-{session_id}",
                    session_id,
                    '{"type":"text","text":"hello"}',
                ),
            )
    opencode_session = SessionInfo(
        session_id="target",
        source_path=opencode_db,
        source_kind="sqlite",
        metadata={"native_session_id": "target"},
    )

    crush_db = tmp_path / "crush.db"
    with sqlite3.connect(crush_db) as connection:
        connection.executescript("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, title TEXT, parent_session_id TEXT,
                message_count INTEGER, updated_at INTEGER, created_at INTEGER
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY, session_id TEXT, role TEXT, parts TEXT,
                model TEXT, created_at INTEGER, updated_at INTEGER,
                finished_at INTEGER
            );
            CREATE TABLE read_files (
                session_id TEXT, path TEXT, read_at INTEGER
            );
            """)
        for session_id in ("target", "sibling"):
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, '', 1, 1, 1)",
                (session_id, session_id),
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, 'user', ?, '', 1, 1, NULL)",
                (
                    f"message-{session_id}",
                    session_id,
                    '[{"type":"text","data":{"text":"hello"}}]',
                ),
            )
    crush_session = SessionInfo(
        session_id="target",
        source_path=crush_db,
        source_kind="sqlite",
        metadata={"native_session_id": "target"},
    )

    cases = [
        (
            CursorSource(),
            cursor_session,
            cursor_db,
            "UPDATE ItemTable SET value=? WHERE key='chat.sibling'",
            ('{"messages":[{"role":"user","content":"changed"}]}',),
            "UPDATE ItemTable SET value=? WHERE key='chat.target'",
            ('{"messages":[{"role":"user","content":"changed"}]}',),
        ),
        (
            OpenCodeSource(),
            opencode_session,
            opencode_db,
            "UPDATE part SET data=? WHERE session_id='sibling'",
            ('{"type":"text","text":"changed"}',),
            "UPDATE part SET data=? WHERE session_id='target'",
            ('{"type":"text","text":"changed"}',),
        ),
        (
            CrushSource(),
            crush_session,
            crush_db,
            "UPDATE messages SET parts=? WHERE session_id='sibling'",
            ('[{"type":"text","data":{"text":"changed"}}]',),
            "UPDATE messages SET parts=? WHERE session_id='target'",
            ('[{"type":"text","data":{"text":"changed"}}]',),
        ),
    ]

    def reject_generic_dump(_path: Path) -> dict[str, object]:
        raise AssertionError("generic whole-database evidence must not run")

    monkeypatch.setattr(
        agent_source_contract,
        "_artifact_content_evidence",
        reject_generic_dump,
    )
    for source, session, database, sibling_sql, sibling_args, target_sql, target_args in cases:
        baseline = native_session_artifact_evidence_hash(source, session)
        assert (
            parse_discovered_session_result(
                source,
                session,
            ).artifact_evidence_hash
            == baseline
        )
        with sqlite3.connect(database) as connection:
            connection.execute(sibling_sql, sibling_args)
        assert native_session_artifact_evidence_hash(source, session) == baseline
        with sqlite3.connect(database) as connection:
            connection.execute(target_sql, target_args)
        assert native_session_artifact_evidence_hash(source, session) != baseline


def test_sqlite_source_adapters_preserve_typed_transient_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "native.db"
    artifact.write_bytes(b"SQLite format 3\x00")
    failure = sqlite3.OperationalError("sensitive native database detail")
    failure.sqlite_errorcode = sqlite3.SQLITE_BUSY
    failure.sqlite_errorname = "SQLITE_BUSY"

    def fail_connect(_path: Path):
        raise failure

    opencode = OpenCodeSource()
    crush = CrushSource()
    monkeypatch.setattr(opencode, "_connect", fail_connect)
    monkeypatch.setattr(crush, "_connect", fail_connect)
    monkeypatch.setattr(
        cursor_source,
        "connect_native_sqlite_readonly",
        fail_connect,
    )
    cases = [
        (
            opencode,
            SessionInfo(
                session_id="target",
                source_path=artifact,
                source_kind="sqlite",
                metadata={"native_session_id": "target"},
            ),
        ),
        (
            crush,
            SessionInfo(
                session_id="target",
                source_path=artifact,
                source_kind="sqlite",
                metadata={"native_session_id": "target"},
            ),
        ),
        (
            CursorSource(),
            SessionInfo(
                session_id="target",
                source_path=artifact,
                source_kind="sqlite_conversation",
                metadata={
                    "native_sqlite_table": "ItemTable",
                    "native_sqlite_key": "target",
                },
            ),
        ),
    ]

    for source, session in cases:
        with pytest.raises(NativeSourceContractError) as raised:
            source.session_artifact_evidence_hash(session)
        assert raised.value.retryable is True
        assert raised.value.details == {
            "failure_class": "sqlite_transient",
            "retryable": True,
            "sqlite_errorcode": sqlite3.SQLITE_BUSY,
            "sqlite_errorname": "SQLITE_BUSY",
        }
        assert "sensitive" not in repr(raised.value.details)


def test_simple_and_sqlite_adapters_preserve_unknown_native_payloads(
    tmp_path: Path,
) -> None:
    unknown = {
        "role": "future_role",
        "content": {"future_visible": "must survive"},
        "future_field": {"nested": True},
    }
    for source in (CursorSource(), WindsurfSource(), OpenCodeSource()):
        artifact = tmp_path / f"{source.name}.json"
        artifact.write_text(json.dumps([unknown]), encoding="utf-8")

        result = parse_discovered_session_result(
            source,
            SessionInfo(
                session_id=source.name,
                source_path=artifact,
                source_kind="json_fallback",
            ),
        )

        assert result.disposition == "parsed"
        refs = [ref for turn in result.turns for ref in turn.raw_event_refs]
        assert any(ref.get("raw") == unknown for ref in refs)
        assert json.dumps(refs, ensure_ascii=False).count("must survive") == 1


def test_jsonl_adapters_preserve_valid_non_object_native_records(
    tmp_path: Path,
) -> None:
    opaque = "opaque-valid-json-native-record"
    for source in (
        CodexSource(),
        ClaudeSource(),
        HermesSource(),
        GeminiCliSource(),
        KiroSource(),
    ):
        artifact = tmp_path / f"{source.name}.jsonl"
        artifact.write_text(json.dumps(opaque) + "\n", encoding="utf-8")
        result = parse_discovered_session_result(
            source,
            SessionInfo(
                session_id=source.name,
                source_path=artifact,
                source_kind="jsonl",
            ),
        )

        assert result.disposition == "parsed"
        refs = [ref for turn in result.turns for ref in turn.raw_event_refs]
        assert json.dumps(refs, ensure_ascii=False).count(opaque) == 1


def test_opencode_state_uses_exact_session_path_and_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_db = tmp_path / "configured" / "opencode.db"
    configured_db.parent.mkdir()
    snapshot_db = tmp_path / "snapshot" / "opencode.db"
    snapshot_db.parent.mkdir()
    for database, text in (
        (configured_db, "configured"),
        (snapshot_db, "snapshot"),
    ):
        with sqlite3.connect(database) as connection:
            connection.executescript("""
                CREATE TABLE session (
                    id TEXT PRIMARY KEY, title TEXT, directory TEXT,
                    time_created INTEGER, time_updated INTEGER
                );
                CREATE TABLE message (
                    id TEXT PRIMARY KEY, session_id TEXT,
                    time_created INTEGER, time_updated INTEGER, data TEXT
                );
                CREATE TABLE part (
                    id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                    time_created INTEGER, data TEXT
                );
                """)
            connection.execute("INSERT INTO session VALUES ('target', '', '', 1, 1)")
            connection.execute(
                "INSERT INTO message VALUES ('m', 'target', 1, 1, ?)",
                ('{"role":"user"}',),
            )
            connection.execute(
                "INSERT INTO part VALUES ('p', 'm', 'target', 1, ?)",
                (json.dumps({"type": "text", "text": text}),),
            )
    monkeypatch.setenv("OPENCODE_DB_PATH", str(configured_db))
    source = OpenCodeSource()
    session = SessionInfo(
        session_id="target",
        source_path=snapshot_db,
        source_kind="sqlite",
        metadata={"native_session_id": "target"},
    )

    before = source.get_session_state(session)
    with sqlite3.connect(snapshot_db) as connection:
        connection.execute(
            "UPDATE part SET data=? WHERE id='p'",
            (json.dumps({"type": "text", "text": "changed!"}),),
        )
    after = source.get_session_state(session)

    assert before is not None and after is not None
    assert before["fingerprint"] != after["fingerprint"]


def test_opencode_rejects_cross_session_part_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "opencode.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE session (
                id TEXT PRIMARY KEY, title TEXT, directory TEXT,
                time_created INTEGER, time_updated INTEGER
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY, session_id TEXT,
                time_created INTEGER, time_updated INTEGER, data TEXT
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                time_created INTEGER, data TEXT
            );
            """)
        connection.execute("INSERT INTO session VALUES ('target', '', '', 1, 1)")
        connection.execute(
            "INSERT INTO message VALUES ('m-target', 'target', 1, 1, ?)",
            ('{"role":"user"}',),
        )
        connection.execute(
            "INSERT INTO part VALUES ('p-cross', 'm-target', 'sibling', 1, ?)",
            ('{"type":"text","text":"wrong-session"}',),
        )
    source = OpenCodeSource()
    session = SessionInfo(
        session_id="target",
        source_path=database,
        source_kind="sqlite",
        metadata={"native_session_id": "target"},
    )

    with pytest.raises(
        NativeSourceContractError,
        match="native_opencode_part_session_identity_conflict",
    ):
        parse_discovered_session_result(source, session)


def test_native_read_failure_does_not_log_path_or_exception_body(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_path = tmp_path / "private-project-name" / "missing.jsonl"

    with pytest.raises(
        NativeSourceContractError,
        match="native_openclaw_jsonl_read_failed",
    ):
        read_native_jsonl(sensitive_path)

    assert "private-project-name" not in caplog.text
    assert "missing.jsonl" not in caplog.text
    assert "Traceback" not in caplog.text


def test_untyped_generation_error_evidence_does_not_hash_sensitive_message() -> None:
    sensitive_message = "runtime-error-body-sentinel"

    evidence = raw_reconciler._safe_sync_error_evidence(  # noqa: SLF001
        [("raw_sync:codex", RuntimeError(sensitive_message))]
    )

    assert evidence == [
        {
            "source": "codex",
            "error_type": "RuntimeError",
            "error_code": "",
            "message_hash": hashlib.sha256(b"RuntimeError").hexdigest(),
            "count": 1,
        }
    ]
    assert (
        hashlib.sha256(sensitive_message.encode("utf-8")).hexdigest()
        not in json.dumps(evidence)
    )


@pytest.mark.parametrize("guard_kind", ("raw_generation", "challenger"))
def test_raw_guard_accepts_only_helper_owned_exact_native_sqlite_read(
    tmp_path: Path,
    guard_kind: str,
) -> None:
    snapshot_root = tmp_path / "snapshot"
    worker_root = tmp_path / "worker"
    snapshot_root.mkdir()
    worker_root.mkdir()
    database = snapshot_root / "opencode.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE session (id TEXT)")
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_fd)
            blocked: set[str] = set()
            if guard_kind == "raw_generation":
                raw_reconciler._install_raw_generation_write_guard(
                    database_dir=worker_root / "target",
                    allowed_names={"raw_events.db"},
                    allowed_write_roots=(worker_root,),
                    allowed_read_roots=(snapshot_root,),
                    blocked_name_hashes=blocked,
                )
            else:
                raw_reconciler._install_challenger_read_only_guard(
                    allowed_write_roots=(worker_root,),
                    allowed_read_paths=(database,),
                    blocked_name_hashes=blocked,
                )
            helper_ok = False
            forged_blocked = False
            source = OpenCodeSource()
            connection = source._connect(database)
            try:
                helper_ok = connection.execute("SELECT COUNT(*) FROM session").fetchone() == (0,)
            finally:
                connection.close()
            try:
                sqlite3.connect(
                    f"{database.resolve().as_uri()}?mode=ro",
                    uri=True,
                )
            except PermissionError:
                forged_blocked = True
            os.write(
                write_fd,
                json.dumps(
                    {
                        "helper_ok": helper_ok,
                        "forged_blocked": forged_blocked,
                    }
                ).encode("utf-8"),
            )
            os._exit(0)
        except BaseException:
            os._exit(91)
    os.close(write_fd)
    encoded = os.read(read_fd, 4096)
    os.close(read_fd)
    _waited, status = os.waitpid(pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert json.loads(encoded) == {
        "helper_ok": True,
        "forged_blocked": True,
    }


def test_parser_grandchild_write_violation_survives_fork_copy_on_write(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "violations.log"
    marker.touch(mode=0o600)
    forbidden = tmp_path.parent / "forbidden-native-write.txt"
    pid = os.fork()
    if pid == 0:
        try:
            blocked: set[str] = set()
            raw_reconciler._install_raw_generation_write_guard(
                database_dir=tmp_path / "target",
                allowed_names={"raw_events.db"},
                allowed_write_roots=(tmp_path,),
                blocked_name_hashes=blocked,
                violation_marker=marker,
            )
            grandchild = os.fork()
            if grandchild == 0:
                try:
                    try:
                        forbidden.write_text("blocked", encoding="utf-8")
                    except PermissionError:
                        pass
                    os._exit(0)
                except BaseException:
                    os._exit(91)
            _waited_grandchild, grandchild_status = os.waitpid(grandchild, 0)
            os._exit(
                0 if os.waitstatus_to_exitcode(grandchild_status) == 0 and blocked == set() else 92
            )
        except BaseException:
            os._exit(91)
    _waited, status = os.waitpid(pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert raw_reconciler._read_guard_violations(marker)
    assert not forbidden.exists()
