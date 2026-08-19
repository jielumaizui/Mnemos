# -*- coding: utf-8 -*-
"""Tests for trigger-driven tail acceleration."""

from __future__ import annotations

from pathlib import Path

from core.sync_framework.agent_source import SessionInfo, Turn
from daemon import triggers
from daemon.agent_sync_cursor import AgentSyncCursorStore


class _FakeEngine:
    def __init__(self):
        self.synced = []
        self.closed = False

    @staticmethod
    def canonicalize_session_info(session_info):
        return session_info

    def sync_turns(self, source, session_info, turns, **kwargs):
        assert kwargs == {"incremental": False, "enqueue_distillation": False}
        self.synced.append((source.name, session_info.source_path.name, [turn.turn_number for turn in turns]))
        return [
            type("Result", (), {"turn_number": turn.turn_number, "raw_event_id": f"raw-{turn.turn_number}"})()
            for turn in turns
        ]

    def enqueue_session_for_distillation(self, *_args):
        return {}

    def close(self):
        self.closed = True


class _FakeSource:
    name = "codex"

    def __init__(self, root: Path):
        self._older = root / "older.jsonl"
        self._newer = root / "newer.jsonl"
        self._older.write_text("older", encoding="utf-8")
        self._newer.write_text("newer", encoding="utf-8")

    def discover_sessions(self):
        return [
            SessionInfo(session_id="older", source_path=self._older, mtime=1.0),
            SessionInfo(session_id="newer", source_path=self._newer, mtime=2.0),
        ]

    def parse_turns(self, source_path):
        return [Turn(turn_number=0, user_content="u", assistant_content=str(source_path))]


class _FakeRegistry:
    source = None

    @classmethod
    def list_sources(cls):
        return [cls.source]


def _limits():
    return {
        "tail_sessions_per_source": 1,
        "reconciliation_sessions_per_source": 1,
        "turns_per_session": 10,
    }


def test_sync_dirty_sources_accelerates_tail_without_replacing_reconciliation(tmp_path: Path):
    engine = _FakeEngine()
    _FakeRegistry.source = _FakeSource(tmp_path)

    triggers.sync_dirty_sources(
        ["codex"],
        cfg=object(),
        continuous_sync_limits=_limits,
        cursor_store=AgentSyncCursorStore(tmp_path),
        log_service_error=lambda _service, _exc: None,
        engine_factory=lambda: engine,
        source_registry=_FakeRegistry,
    )

    assert engine.synced == [("codex", "newer.jsonl", [0])]
    assert engine.closed is True


def test_sync_dirty_sources_is_not_gated_by_scheduled_owner(tmp_path: Path):
    """A disabled scheduled scan must not discard a real watcher-triggered change."""
    engine = _FakeEngine()
    _FakeRegistry.source = _FakeSource(tmp_path)

    triggers.sync_dirty_sources(
        ["codex"],
        cfg=object(),
        continuous_sync_limits=_limits,
        cursor_store=AgentSyncCursorStore(tmp_path),
        log_service_error=lambda _service, _exc: None,
        engine_factory=lambda: engine,
        source_registry=_FakeRegistry,
    )

    assert engine.synced == [("codex", "newer.jsonl", [0])]


def test_sync_dirty_sources_logs_source_errors(tmp_path: Path):
    class _BadSource(_FakeSource):
        def discover_sessions(self):
            raise RuntimeError("boom")

    class _Registry:
        @staticmethod
        def list_sources():
            return [_BadSource(tmp_path)]

    errors = []
    engine = _FakeEngine()
    triggers.sync_dirty_sources(
        ["codex"],
        cfg=object(),
        continuous_sync_limits=_limits,
        cursor_store=AgentSyncCursorStore(tmp_path),
        log_service_error=lambda service, exc: errors.append((service, str(exc))),
        engine_factory=lambda: engine,
        source_registry=_Registry,
    )

    assert errors == [("trigger_raw_sync:codex", "boom")]
    assert engine.closed is True
