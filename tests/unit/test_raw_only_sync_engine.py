# -*- coding: utf-8 -*-
"""Regression tests for the bounded Raw-only reconciliation adapter."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn
from core.sync_framework.raw_event_store import RawEventStore
from daemon.raw_only_sync_engine import RawOnlySyncEngine


class _Config:
    def __init__(self, database_dir: Path):
        self.database_dir = database_dir
        self.data_dir = database_dir

    def get(self, _key: str, default=None):
        return default


class _Source(AgentSource):
    name = "codex"
    model_tag = "synthetic-codex"

    def discover_sessions(self):
        return []

    def parse_turns(self, _session_path: Path):
        return []

    def completeness_capabilities(self):
        return {
            "visible_text": True,
            "tool_calls": True,
            "tool_results": True,
            "reasoning": True,
            "attachments": True,
            "raw_files": True,
            "source_fidelity": "full",
        }


def test_raw_only_engine_commits_raw_receipts_without_sync_log_or_downstream_writes(tmp_path: Path):
    config = _Config(tmp_path)
    raw_db = tmp_path / "raw_events.db"
    sync_db = tmp_path / "sync_log.db"
    source_path = tmp_path / "native.jsonl"
    source_path.write_text("synthetic-safe", encoding="utf-8")
    engine = RawOnlySyncEngine(
        raw_store=RawEventStore(db_path=raw_db, config=config),
    )
    session = SessionInfo(
        session_id="alias-session",
        canonical_session_id="canonical-session",
        source_path=source_path,
    )
    turn = Turn(
        turn_number=0,
        user_content="synthetic-safe user",
        assistant_content="synthetic-safe assistant",
        native_event_id="synthetic-native-0",
        tool_calls=[{"id": "safe-call", "name": "health_check", "arguments": {}}],
        tool_results=[{"tool_call_id": "safe-call", "status": "ok"}],
    )

    try:
        results = engine.sync_turns(
            _Source(),
            session,
            [turn],
            incremental=False,
            enqueue_distillation=False,
        )
    finally:
        engine.close()

    assert len(results) == 1
    assert results[0].action == "raw_committed"
    assert results[0].raw_event_id
    assert not sync_db.exists()
    with sqlite3.connect(raw_db) as conn:
        row = conn.execute(
            "SELECT source_agent, session_id, current_revision_id FROM raw_turns"
        ).fetchone()
    assert row == ("codex", "canonical-session", results[0].raw_event_id)


def test_raw_only_engine_rejects_incremental_or_downstream_modes(tmp_path: Path):
    config = _Config(tmp_path)
    engine = RawOnlySyncEngine(raw_store=RawEventStore(db_path=tmp_path / "raw.db", config=config))
    session = SessionInfo(session_id="session", source_path=tmp_path / "native")
    turn = Turn(turn_number=0, user_content="u", assistant_content="a")
    try:
        with pytest.raises(ValueError, match="full raw-only batches"):
            engine.sync_turns(
                _Source(), session, [turn], incremental=True, enqueue_distillation=False
            )
        with pytest.raises(ValueError, match="full raw-only batches"):
            engine.sync_turns(
                _Source(), session, [turn], incremental=False, enqueue_distillation=True
            )
    finally:
        engine.close()
