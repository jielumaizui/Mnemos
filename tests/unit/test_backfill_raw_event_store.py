# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from unittest.mock import Mock, patch

from core.sync_framework.agent_source import SessionInfo, Turn
from core.sync_framework.raw_event_store import RawEventStore
from scripts.backfill_raw_event_store import (
    _backfill_turn,
    _filter_sessions,
    _source_metadata,
    run_backfill,
)


class _Cfg:
    def __init__(self, database_dir: Path):
        self.database_dir = database_dir

    def get(self, key, default=None):  # noqa: ARG002
        return default


class _Source:
    name = "codex"
    model_tag = "codex-cli"

    def completeness_capabilities(self):
        return {
            "visible_text": True,
            "tool_calls": True,
            "tool_results": True,
            "source_fidelity": "full",
            "memory_scope": "fixture",
            "host_memory_default": "host_dependent_unknown",
            "host_memory_effect": "prompt_context_only",
            "transcript_kind": "fixture_jsonl",
            "compression": "none",
            "dedupe_strategy": "canonical_session_id+turn_number+content_hash",
        }


def test_backfill_metadata_preserves_canonical_session_fields(tmp_path: Path):
    session = SessionInfo(
        session_id="alias-session",
        source_path=tmp_path / "alias.jsonl",
        working_dir=str(tmp_path / "project"),
        canonical_session_id="canonical-session",
        session_aliases=["split-part-a", "split-part-b"],
        source_kind="native_jsonl",
    )
    turn = Turn(
        turn_number=0,
        user_content="hello",
        assistant_content="world",
        metadata={
            "canonical_session_id": "stale",
            "source_session_id": "stale",
            "session_aliases": ["stale"],
            "source_kind": "stale",
        },
    )

    metadata = _source_metadata(_Source(), session, turn)

    assert metadata["canonical_session_id"] == "canonical-session"
    assert metadata["source_session_id"] == "alias-session"
    assert metadata["session_aliases"] == ["alias-session", "split-part-a", "split-part-b"]
    assert metadata["source_kind"] == "native_jsonl"
    assert metadata["working_dir"] == str(tmp_path / "project")
    assert metadata["source_fidelity"] == "full"


def test_backfill_turn_writes_canonical_session_id(tmp_path: Path):
    source_file = tmp_path / "alias.jsonl"
    source_file.write_text("{}", encoding="utf-8")
    session = SessionInfo(
        session_id="alias-session",
        source_path=source_file,
        canonical_session_id="canonical-session",
        session_aliases=["split-part-a"],
        source_kind="native_jsonl",
    )
    turn = Turn(
        turn_number=0,
        user_content="hello",
        assistant_content="world",
        tool_calls=[{"name": "read"}],
        tool_results=[{"output": "ok"}],
        completeness={"visible_text": "full", "truncated": False},
    )
    store = RawEventStore(db_path=tmp_path / "raw_events.db", config=_Cfg(tmp_path))
    try:
        event_id = _backfill_turn(store, _Source(), session, turn)
        row = store.get_turn(event_id)
    finally:
        store.close()

    assert row is not None
    assert row["session_id"] == "canonical-session"
    assert row["metadata"]["canonical_session_id"] == "canonical-session"
    assert row["metadata"]["source_session_id"] == "alias-session"
    assert row["metadata"]["session_aliases"] == ["alias-session", "split-part-a"]
    assert row["metadata"]["source_kind"] == "native_jsonl"
    assert row["source_path"] == str(source_file)
    assert row["source_files"] == [str(source_file)]
    assert row["tool_calls"] == [{"name": "read"}]
    assert row["tool_results"] == [{"output": "ok"}]


def test_raw_only_backfill_deduplicates_discovery_aliases_by_shared_resolver(tmp_path: Path):
    older = tmp_path / "older.jsonl"
    newer = tmp_path / "newer.jsonl"
    older.write_text("{}", encoding="utf-8")
    newer.write_text("{}", encoding="utf-8")
    sessions = [
        SessionInfo(
            session_id="alias-a",
            canonical_session_id="canonical-session",
            source_path=older,
            mtime=1.0,
        ),
        SessionInfo(
            session_id="alias-b",
            canonical_session_id="canonical-session",
            source_path=newer,
            mtime=2.0,
        ),
    ]

    selected = _filter_sessions(sessions, since_hours=0, max_sessions=0)

    assert selected == [sessions[1]]


def test_backfill_summary_emits_manifest_bound_native_source_snapshot(tmp_path: Path):
    source_file = tmp_path / "rollout.jsonl"
    source_file.write_text("{}", encoding="utf-8")
    session = SessionInfo(session_id="snapshot-canary", source_path=source_file)
    source = _Source()
    source.data_dir = tmp_path
    source.discover_sessions = lambda: [session]
    source.parse_turns = lambda _path: [
        Turn(turn_number=0, user_content="user", assistant_content="assistant")
    ]
    args = SimpleNamespace(
        source="all",
        db_path=str(tmp_path / "raw_events.db"),
        dry_run=True,
        since_hours=0,
        max_sessions=0,
        max_turns_per_session=0,
    )

    with (
        patch("scripts.backfill_raw_event_store.SourceRegistry.register_builtin_agents"),
        patch("scripts.backfill_raw_event_store.SourceRegistry.auto_discover", return_value=[source]),
    ):
        summary = run_backfill(args)

    snapshot = summary["agents"]["codex"]["native_source_snapshot"]
    assert summary["schema_version"] == "mnemos.agent_source_runtime_report.v2"
    assert summary["report_kind"] == "structural_source_observation"
    assert summary["producer"] == "scripts.backfill_raw_event_store"
    assert summary["report_hash"]
    assert "runtime_receipts" not in summary
    assert summary["support_manifest_hash"] == snapshot["support_manifest_hash"]
    assert snapshot["parser_class"] == "CodexSource"
    assert snapshot["native_denominator"] == {"sessions": 1, "turns": 1}
    assert summary["unmanifested_sources"] == []


def test_backfill_rejects_undeclared_source_before_parser_or_raw_write(tmp_path: Path):
    source = Mock()
    source.name = "undeclared-native"
    args = SimpleNamespace(
        source="all",
        db_path=str(tmp_path / "raw_events.db"),
        dry_run=False,
        since_hours=0,
        max_sessions=0,
        max_turns_per_session=0,
    )

    with (
        patch("scripts.backfill_raw_event_store.SourceRegistry.register_builtin_agents"),
        patch("scripts.backfill_raw_event_store.SourceRegistry.auto_discover", return_value=[source]),
    ):
        summary = run_backfill(args)

    source.discover_sessions.assert_not_called()
    source.parse_turns.assert_not_called()
    assert summary["unmanifested_sources"] == ["undeclared-native"]
    assert summary["agents"]["undeclared-native"]["written"] == 0
    assert summary["agents"]["undeclared-native"]["native_source_snapshot"] is None
