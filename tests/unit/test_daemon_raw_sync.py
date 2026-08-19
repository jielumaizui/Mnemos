# -*- coding: utf-8 -*-
"""Tests for continuous AgentSource Raw synchronization."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from core.sync_framework.agent_source import SessionInfo, Turn
from core.sync_framework.raw_event_reader import decode_raw_revision_snapshot
from core.sync_framework.raw_event_store import RawEventStore
from daemon import raw_sync
from daemon.agent_sync_cursor import AgentSyncCursorError, AgentSyncCursorStore
from daemon.raw_only_sync_engine import RawOnlySyncEngine


class _Source:
    name = "codex"

    def __init__(self, root: Path, sessions: list[SessionInfo], turns: dict[str, list[Turn]]):
        self.data_dir = root
        self._sessions = sessions
        self._turns = turns

    def discover_sessions(self):
        return list(self._sessions)

    def parse_turns(self, path: Path):
        return list(self._turns[path.name])


class _Engine:
    def __init__(self, db_path: Path, *, omit_raw_turns: set[int] | None = None):
        self.db_path = db_path
        self.omit_raw_turns = omit_raw_turns or set()
        self.calls: list[tuple[str, list[int]]] = []
        self.handoffs: list[tuple[str, list[int]]] = []
        self.closed = 0

    @staticmethod
    def canonicalize_session_info(session_info: SessionInfo) -> SessionInfo:
        canonical_id = session_info.canonical_session_id or session_info.session_id
        if canonical_id == session_info.session_id:
            return session_info
        return SessionInfo(
            session_id=canonical_id,
            canonical_session_id=canonical_id,
            session_aliases=[session_info.session_id, *session_info.session_aliases],
            source_path=session_info.source_path,
            mtime=session_info.mtime,
        )

    def sync_turns(self, source, session_info, turns, **kwargs):
        assert kwargs == {"incremental": False, "enqueue_distillation": False}
        turn_numbers = [turn.turn_number for turn in turns]
        self.calls.append((session_info.session_id, turn_numbers))
        return [
            type(
                "Result",
                (),
                {
                    "turn_number": turn.turn_number,
                    "raw_event_id": (
                        "" if turn.turn_number in self.omit_raw_turns else f"raw-{turn.turn_number}"
                    ),
                },
            )()
            for turn in turns
        ]

    def enqueue_session_for_distillation(self, _source, session_info, turns):
        self.handoffs.append((session_info.session_id, [turn.turn_number for turn in turns]))

    def close(self):
        self.closed += 1


def _limits(*, sessions: int = 1, turns: int = 50):
    return {
        "tail_sessions_per_source": sessions,
        "reconciliation_sessions_per_source": sessions,
        "turns_per_session": turns,
    }


def _sessions_and_turns(root: Path, count: int, *, turns_per_session: int = 1):
    sessions: list[SessionInfo] = []
    turns: dict[str, list[Turn]] = {}
    for index in range(count):
        path = root / f"session-{index:02d}.jsonl"
        path.write_text("synthetic", encoding="utf-8")
        sessions.append(
            SessionInfo(
                session_id=f"source-{index:02d}",
                canonical_session_id=f"canonical-{index:02d}",
                source_path=path,
                # Session zero is deliberately older than one day.
                mtime=1.0 if index == 0 else float(10_000 + index),
            )
        )
        turns[path.name] = [
            Turn(turn_number=turn_number, user_content="u", assistant_content="a")
            for turn_number in range(turns_per_session)
        ]
    return sessions, turns


def test_continuous_sync_limits_are_throughput_budgets_only():
    assert raw_sync.continuous_sync_limits() == {
        "tail_sessions_per_source": 10,
        "reconciliation_sessions_per_source": 10,
        "turns_per_session": 100,
    }


def test_run_service_accepts_an_explicit_recovery_registry_and_engine(tmp_path: Path):
    root = tmp_path / "native"
    root.mkdir()
    sessions, turns = _sessions_and_turns(root, 1)
    source = _Source(root, sessions, turns)
    engine = _Engine(tmp_path / "sync_log.db")

    class _Registry:
        @staticmethod
        def list_sources():
            return [source]

    report = raw_sync.run_service(
        lambda _service, _error: None,
        continuous_sync_limits_func=lambda: _limits(sessions=1, turns=1),
        cursor_store=AgentSyncCursorStore(tmp_path),
        engine_factory=lambda: engine,
        source_registry=_Registry(),
    )

    assert report["errors"] == 0
    assert engine.calls == [("canonical-00", [0])]
    assert engine.closed == 1


def test_snapshot_binding_failure_is_not_misclassified_as_unmanifested(tmp_path: Path):
    root = tmp_path / "native"
    root.mkdir()
    sessions, turns = _sessions_and_turns(root, 1)
    source = _Source(root, sessions, turns)
    engine = _Engine(tmp_path / "sync_log.db")
    logged: list[Exception] = []

    class _Registry:
        @staticmethod
        def list_sources():
            return [source]

    class _FailingBindCursorStore(AgentSyncCursorStore):
        def bind_native_source_snapshot(
            self,
            source_name: str,
            native_source_snapshot_hash: str,
            *,
            expected_capture_state,
        ) -> None:
            raise AgentSyncCursorError("snapshot generation needs explicit rebuild")

    report = raw_sync.run_service(
        lambda _service, error: logged.append(error),
        continuous_sync_limits_func=lambda: _limits(sessions=1, turns=1),
        cursor_store=_FailingBindCursorStore(tmp_path),
        engine_factory=lambda: engine,
        source_registry=_Registry(),
    )

    assert report["errors"] == 1
    assert report["unmanifested_sources"] == []
    assert any(isinstance(error, AgentSyncCursorError) for error in logged)


def test_runtime_snapshot_contract_failure_is_not_misclassified_as_unmanifested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.agent_kit.source_support_manifest as manifest_module

    root = tmp_path / "native"
    root.mkdir()
    sessions, turns = _sessions_and_turns(root, 1)
    source = _Source(root, sessions, turns)
    engine = _Engine(tmp_path / "sync_log.db")
    logged: list[Exception] = []

    class _Registry:
        @staticmethod
        def list_sources():
            return [source]

    def reject_runtime_snapshot(*_args, **_kwargs):
        raise manifest_module.AgentSourceSupportManifestError("snapshot_capture_cursor_incomplete")

    monkeypatch.setattr(
        manifest_module,
        "build_native_source_snapshot",
        reject_runtime_snapshot,
    )

    report = raw_sync.run_service(
        lambda _service, error: logged.append(error),
        continuous_sync_limits_func=lambda: _limits(sessions=1, turns=1),
        cursor_store=AgentSyncCursorStore(tmp_path),
        engine_factory=lambda: engine,
        source_registry=_Registry(),
    )

    assert report["errors"] == 1
    assert report["unmanifested_sources"] == []
    assert any(
        isinstance(error, manifest_module.AgentSourceSupportManifestError) for error in logged
    )


def test_run_service_reconciles_all_sessions_including_old_history(tmp_path: Path):
    root = tmp_path / "native"
    root.mkdir()
    sessions, turns = _sessions_and_turns(root, 12)
    source = _Source(root, sessions, turns)
    engine = _Engine(tmp_path / "sync_log.db")
    cursor_store = AgentSyncCursorStore(tmp_path)

    with (
        patch("core.sync_framework.registry.SourceRegistry") as registry_cls,
        patch("core.sync_framework.sync_engine.SyncEngine", return_value=engine),
    ):
        registry_cls.return_value.list_sources.return_value = [source]
        for _ in range(12):
            report = raw_sync.run_service(
                lambda _service, _error: None,
                engine_factory=lambda: engine,
                continuous_sync_limits_func=lambda: _limits(sessions=1, turns=1),
                cursor_store=cursor_store,
            )

    captured_ids = {session_id for session_id, _turn_numbers in engine.calls}
    assert captured_ids == {f"canonical-{index:02d}" for index in range(12)}
    assert "canonical-00" in captured_ids  # mtime=1.0 must not be silently excluded.
    assert report["errors"] == 0
    assert report["source_snapshots"]["codex"]["native_denominator"]["sessions"] == 12


def test_snapshot_does_not_claim_turn_denominator_before_full_reconciliation(tmp_path: Path):
    root = tmp_path / "native"
    root.mkdir()
    sessions, turns = _sessions_and_turns(root, 3)
    source = _Source(root, sessions, turns)
    engine = _Engine(tmp_path / "sync_log.db")
    cursor_store = AgentSyncCursorStore(tmp_path)

    with (
        patch("core.sync_framework.registry.SourceRegistry") as registry_cls,
        patch("core.sync_framework.sync_engine.SyncEngine", return_value=engine),
    ):
        registry_cls.return_value.list_sources.return_value = [source]
        first = raw_sync.run_service(
            lambda _service, _error: None,
            engine_factory=lambda: engine,
            continuous_sync_limits_func=lambda: _limits(sessions=1, turns=1),
            cursor_store=cursor_store,
        )
        second = raw_sync.run_service(
            lambda _service, _error: None,
            engine_factory=lambda: engine,
            continuous_sync_limits_func=lambda: _limits(sessions=1, turns=1),
            cursor_store=cursor_store,
        )

    first_cursor = first["source_snapshots"]["codex"]["cursor"]
    assert first_cursor["denominator_complete"] is False
    assert first["source_snapshots"]["codex"]["native_denominator"]["turns"] == 0
    assert second["source_snapshots"]["codex"]["cursor"]["denominator_complete"] is True
    assert second["source_snapshots"]["codex"]["native_denominator"]["turns"] == 3


def test_session_cursor_advances_only_after_raw_receipts_and_resumes(tmp_path: Path):
    root = tmp_path / "native"
    root.mkdir()
    sessions, turns = _sessions_and_turns(root, 1, turns_per_session=3)
    source = _Source(root, sessions, turns)
    cursor_store = AgentSyncCursorStore(tmp_path)
    failing = _Engine(tmp_path / "sync_log.db", omit_raw_turns={1})

    first = raw_sync.sync_source_continuously(
        source,
        failing,
        cursor_store,
        _limits(sessions=1, turns=3),
    )
    cursor = cursor_store.get_session_raw_cursor("codex", "canonical-00")
    assert first["errors"]
    assert cursor.next_turn_number == 1
    assert failing.handoffs == []

    restarted = _Engine(tmp_path / "sync_log.db")
    resumed = raw_sync.sync_source_continuously(
        source,
        restarted,
        AgentSyncCursorStore(tmp_path),
        _limits(sessions=1, turns=3),
    )
    assert resumed["errors"] == []
    assert restarted.calls == [("canonical-00", [1])]
    assert restarted.handoffs == [("canonical-00", [0, 1, 2])]
    assert (
        AgentSyncCursorStore(tmp_path)
        .get_session_raw_cursor("codex", "canonical-00")
        .next_turn_number
        == 3
    )


def test_changed_historical_turn_and_new_tail_both_reconcile_without_cursor_reset(
    tmp_path: Path,
) -> None:
    root = tmp_path / "native"
    root.mkdir()
    sessions, turns = _sessions_and_turns(root, 1, turns_per_session=5)
    source = _Source(root, sessions, turns)
    cursor_store = AgentSyncCursorStore(tmp_path)
    initial = _Engine(tmp_path / "sync_log.db")

    first = raw_sync.sync_source_continuously(
        source,
        initial,
        cursor_store,
        _limits(sessions=1, turns=10),
    )
    assert first["errors"] == []
    assert (
        cursor_store.get_session_raw_cursor(
            "codex",
            "canonical-00",
        ).next_turn_number
        == 5
    )

    turns["session-00.jsonl"][2].user_content = "historical correction"
    turns["session-00.jsonl"].append(
        Turn(
            turn_number=5,
            user_content="new tail",
            assistant_content="new tail answer",
        )
    )
    (root / "session-00.jsonl").write_text(
        "synthetic changed artifact",
        encoding="utf-8",
    )
    repaired = _Engine(tmp_path / "sync_log.db")

    second = raw_sync.sync_source_continuously(
        source,
        repaired,
        AgentSyncCursorStore(tmp_path),
        _limits(sessions=1, turns=1),
    )

    assert second["errors"] == []
    assert repaired.calls == [
        ("canonical-00", [2]),
        ("canonical-00", [5]),
    ]
    restarted = AgentSyncCursorStore(tmp_path)
    assert (
        restarted.pending_session_turn_numbers(
            "codex",
            "canonical-00",
        )
        == []
    )
    assert (
        restarted.get_session_raw_cursor(
            "codex",
            "canonical-00",
        ).next_turn_number
        == 6
    )


def test_real_raw_only_capture_binds_cursor_fingerprint_to_immutable_revision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "native"
    root.mkdir()
    sessions, turns = _sessions_and_turns(root, 1)
    source = _Source(root, sessions, turns)

    class _Config:
        database_dir = tmp_path
        data_dir = tmp_path

        @staticmethod
        def get(_key, default=None):
            return default

    engine = RawOnlySyncEngine(
        raw_store=RawEventStore(
            db_path=tmp_path / "raw_events.db",
            config=_Config(),
        )
    )
    cursor_store = AgentSyncCursorStore(tmp_path)
    try:
        outcome = raw_sync.sync_source_continuously(
            source,
            engine,
            cursor_store,
            _limits(sessions=1, turns=1),
        )
    finally:
        engine.close()

    assert outcome["errors"] == []
    capture_state = cursor_store.source_capture_fingerprint_state("codex")
    assert {
        key: outcome["cursor"][key] for key in capture_state.to_cursor_fields()
    } == capture_state.to_cursor_fields()
    with sqlite3.connect(cursor_store.path) as cursor_connection:
        expected = cursor_connection.execute("""
            SELECT turn_fingerprint
            FROM source_capture_expected_turns
            WHERE source_name='codex' AND canonical_session_id='canonical-00'
              AND turn_number=0
            """).fetchone()
        receipt = cursor_connection.execute("""
            SELECT raw_revision_id, turn_fingerprint
            FROM source_capture_raw_receipts
            WHERE source_name='codex' AND canonical_session_id='canonical-00'
              AND turn_number=0
            """).fetchone()
    assert expected is not None
    assert receipt is not None
    assert receipt[1] == expected[0]
    with sqlite3.connect(tmp_path / "raw_events.db") as raw_connection:
        snapshot_blob = raw_connection.execute(
            """
            SELECT snapshot_blob FROM raw_turn_revisions
            WHERE revision_id=?
            """,
            (receipt[0],),
        ).fetchone()[0]
    snapshot = decode_raw_revision_snapshot(snapshot_blob)
    assert snapshot["metadata"]["native_turn_fingerprint"] == expected[0]


def test_sync_never_mutates_parser_owned_turn_objects(tmp_path: Path) -> None:
    root = tmp_path / "native"
    root.mkdir()
    sessions, turns = _sessions_and_turns(root, 1)
    original_metadata = {"parser_owned": {"nested": ["value"]}}
    turns["session-00.jsonl"][0].metadata = original_metadata
    source = _Source(root, sessions, turns)

    outcome = raw_sync.sync_source_continuously(
        source,
        _Engine(tmp_path / "sync_log.db"),
        AgentSyncCursorStore(tmp_path),
        _limits(sessions=1, turns=1),
    )

    assert outcome["errors"] == []
    assert turns["session-00.jsonl"][0].metadata == {"parser_owned": {"nested": ["value"]}}
    assert turns["session-00.jsonl"][0].metadata is original_metadata


def test_sync_discards_reconciled_native_turn_objects_before_tail_reparse(tmp_path: Path):
    """A large denominator must not retain every parsed transcript in memory."""

    root = tmp_path / "native"
    root.mkdir()
    sessions, turns = _sessions_and_turns(root, 1)

    class _EphemeralSource(_Source):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.parse_calls = 0

        def parse_turns(self, path: Path):
            self.parse_calls += 1
            return super().parse_turns(path)

    source = _EphemeralSource(root, sessions, turns)
    engine = _Engine(tmp_path / "sync_log.db")
    raw_sync.sync_source_continuously(
        source,
        engine,
        AgentSyncCursorStore(tmp_path),
        _limits(sessions=1, turns=1),
    )

    # Reconciliation and tail are independent bounded blocks.  Re-parsing the
    # tail proves the first full native list was released instead of being
    # accumulated with every other session in a large roster.
    assert source.parse_calls == 2


def test_more_than_one_hundred_turns_progress_across_restart(tmp_path: Path):
    root = tmp_path / "native"
    root.mkdir()
    sessions, turns = _sessions_and_turns(root, 1, turns_per_session=205)
    source = _Source(root, sessions, turns)
    cursor_store = AgentSyncCursorStore(tmp_path)
    first_engine = _Engine(tmp_path / "sync_log.db")

    for _ in range(2):
        raw_sync.sync_source_continuously(
            source,
            first_engine,
            cursor_store,
            _limits(sessions=1, turns=50),
        )

    assert cursor_store.get_session_raw_cursor("codex", "canonical-00").next_turn_number == 100

    restarted_engine = _Engine(tmp_path / "sync_log.db")
    restarted_store = AgentSyncCursorStore(tmp_path)
    for _ in range(3):
        raw_sync.sync_source_continuously(
            source,
            restarted_engine,
            restarted_store,
            _limits(sessions=1, turns=50),
        )

    all_calls = first_engine.calls + restarted_engine.calls
    committed = {number for _session_id, batch in all_calls for number in batch}
    assert committed == set(range(205))
    assert restarted_store.get_session_raw_cursor("codex", "canonical-00").next_turn_number == 205


def test_run_service_rejects_undeclared_source_before_discovery(tmp_path: Path):
    class _Undeclared:
        name = "undeclared-native"

        def discover_sessions(self):
            raise AssertionError("must not discover an undeclared source")

    engine = _Engine(tmp_path / "sync_log.db")
    errors = []
    with (
        patch("core.sync_framework.registry.SourceRegistry") as registry_cls,
        patch("core.sync_framework.sync_engine.SyncEngine", return_value=engine),
    ):
        registry_cls.return_value.list_sources.return_value = [_Undeclared()]
        report = raw_sync.run_service(
            lambda service, error: errors.append((service, str(error))),
            engine_factory=lambda: engine,
            cursor_store=AgentSyncCursorStore(tmp_path),
        )

    assert report["unmanifested_sources"] == ["undeclared-native"]
    assert errors == [("raw_sync:undeclared-native", "undeclared native source rejected")]
    assert engine.closed == 1
