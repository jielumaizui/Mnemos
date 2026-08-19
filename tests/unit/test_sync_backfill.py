"""Tests for core.cli.commands.sync._cmd_sync_backfill and helpers."""

from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

from core.cli.commands.sync import (
    _cmd_sync_backfill,
    _compute_missing_turns,
    _compress_ranges,
    _filter_sessions_by_mtime,
    _filter_sources,
    _prepare_backfill_params,
    _process_single_session,
    _init_total_stats,
)


class FakeTurn:
    def __init__(self, turn_number: int):
        self.turn_number = turn_number


class FakeSessionInfo:
    def __init__(self, session_id: str, source_path: Path):
        self.session_id = session_id
        self.source_path = source_path


class FakeSource:
    def __init__(self, name: str, sessions=None, turns=None):
        self.name = name
        self._sessions = sessions or []
        self._turns = turns or []

    def discover_sessions(self):
        return self._sessions

    def parse_turns(self, _path):
        return self._turns


def _make_args(**kwargs) -> Namespace:
    defaults = {
        "source": None,
        "since": 0,
        "max_turns": 0,
        "max_sessions": 0,
        "dry_run": False,
    }
    defaults.update(kwargs)
    return Namespace(**defaults)


def _make_config(tmp_path: Path):
    cfg = Mock()
    cfg.data_dir = tmp_path
    cfg.get = Mock(return_value=0)
    return cfg


def test_compress_ranges():
    assert _compress_ranges([1, 2, 3, 5, 7]) == "1-3,5,7"
    assert _compress_ranges([]) == ""
    assert _compress_ranges([5]) == "5"


def test_prepare_backfill_params(tmp_path):
    args = _make_args(source="kimi", since=24, max_turns=10, max_sessions=5, dry_run=True)
    params = _prepare_backfill_params(args, _make_config(tmp_path))
    assert params.source_filter == "kimi"
    assert params.since_hours == 24
    assert params.max_turns == 10
    assert params.max_sessions == 5
    assert params.dry_run is True


def test_filter_sources():
    a = FakeSource("kimi")
    b = FakeSource("claude")
    assert _filter_sources([a, b], None) == [a, b]
    assert _filter_sources([a, b], "all") == [a, b]
    assert _filter_sources([a, b], "kimi") == [a]


def test_filter_sessions_by_mtime(tmp_path):
    now = 1000000.0
    recent = FakeSessionInfo("recent", tmp_path / "recent")
    old = FakeSessionInfo("old", tmp_path / "old")
    (tmp_path / "recent").write_text("r")
    (tmp_path / "old").write_text("o")

    # Make recent file mtime now, old file mtime 2 hours ago
    import os

    os.utime(tmp_path / "recent", (now, now))
    os.utime(tmp_path / "old", (now - 7200, now - 7200))

    result = _filter_sessions_by_mtime([recent, old], recent_seconds=3600, now=now)
    assert len(result) == 1
    assert result[0][1].session_id == "recent"


def test_compute_missing_turns():
    turns = [FakeTurn(1), FakeTurn(2), FakeTurn(5)]
    to_sync, missing = _compute_missing_turns(turns, [1], max_turns=0)
    assert missing == [2, 5]
    assert [t.turn_number for t in to_sync] == [2, 5]

    to_sync, missing = _compute_missing_turns(turns, [], max_turns=2)
    assert [t.turn_number for t in to_sync] == [2, 5]
    assert missing == [1, 2, 5]


def test_backfill_uses_canonical_session_for_existing_lookup(tmp_path):
    session_file = tmp_path / "session.json"
    session_file.write_text("{}", encoding="utf-8")
    alias = FakeSessionInfo("path-alias", session_file)
    canonical = FakeSessionInfo("canonical-session", session_file)
    source = FakeSource("kimi", turns=[FakeTurn(0)])
    engine = Mock()
    engine.canonicalize_session_info.return_value = canonical
    engine.get_synced_turns_for_session.return_value = [0]
    params = type("Params", (), {"max_turns": 0, "dry_run": True})()

    _process_single_session(source, alias, engine, params, _init_total_stats(), [None])

    engine.get_synced_turns_for_session.assert_called_once_with("kimi", canonical)


def test_limited_backfill_never_handoffs_a_partial_session(tmp_path):
    session_file = tmp_path / "session.json"
    session_file.write_text("{}", encoding="utf-8")
    session = FakeSessionInfo("session", session_file)
    source = FakeSource("kimi", turns=[FakeTurn(0), FakeTurn(1), FakeTurn(2)])
    engine = Mock()
    engine.canonicalize_session_info.return_value = session
    engine.get_synced_turns_for_session.return_value = []
    engine.build_backend_duplicate_cache.return_value = {}
    engine.sync_single_turn.return_value = Mock(action="new")
    params = type("Params", (), {"max_turns": 1, "dry_run": False})()
    stats = _init_total_stats()

    _process_single_session(source, session, engine, params, stats, [None])

    assert stats["missing_turns"] == 3
    assert stats["partial_sessions"] == 1
    engine.enqueue_session_for_distillation.assert_not_called()


@patch("core.cli.commands.sync._get_config")
@patch("core.sync_framework.registry.SourceRegistry")
@patch("core.sync_framework.sync_engine.SyncEngine")
@patch("core.cli.commands.sync._write_backfill_status")
def test_backfill_no_sources(_mock_status, mock_engine_cls, mock_registry_cls, mock_get_config, tmp_path, capsys):
    mock_get_config.return_value = _make_config(tmp_path)
    mock_registry_cls.register_builtin_agents = Mock()
    mock_registry_cls.auto_discover.return_value = []

    _cmd_sync_backfill(_make_args())

    captured = capsys.readouterr()
    assert "未发现任何 Agent 源" in captured.out
    mock_engine_cls.assert_not_called()


@patch("core.cli.commands.sync._get_config")
@patch("core.sync_framework.registry.SourceRegistry")
@patch("core.sync_framework.sync_engine.SyncEngine")
@patch("core.cli.commands.sync._write_backfill_status")
def test_backfill_dry_run(mock_status, mock_engine_cls, mock_registry_cls, mock_get_config, tmp_path, capsys):
    mock_get_config.return_value = _make_config(tmp_path)
    mock_registry_cls.register_builtin_agents = Mock()

    session_file = tmp_path / "session.json"
    session_file.write_text("{}")
    import time

    mtime = time.time()
    import os

    os.utime(session_file, (mtime, mtime))

    session = FakeSessionInfo("s1", session_file)
    source = FakeSource(
        "kimi",
        sessions=[session],
        turns=[FakeTurn(1), FakeTurn(2)],
    )
    mock_registry_cls.auto_discover.return_value = [source]

    engine = Mock()
    engine.canonicalize_session_info.side_effect = lambda session_info: session_info
    engine.get_synced_turns_for_session.return_value = []
    mock_engine_cls.return_value = engine

    _cmd_sync_backfill(_make_args(dry_run=True))

    captured = capsys.readouterr()
    assert "[dry-run]" in captured.out
    assert "1-2" in captured.out
    assert "回填统计" in captured.out
    engine.sync_single_turn.assert_not_called()
    assert [call.args[1] for call in mock_status.call_args_list] == ["running", "dry_run"]


@patch("core.cli.commands.sync._get_config")
@patch("core.sync_framework.registry.SourceRegistry")
@patch("core.sync_framework.sync_engine.SyncEngine")
@patch("core.cli.commands.sync._write_backfill_status")
def test_complete_handoff_failure_cannot_publish_done(
    mock_status,
    mock_engine_cls,
    mock_registry_cls,
    mock_get_config,
    tmp_path,
    capsys,
):
    mock_get_config.return_value = _make_config(tmp_path)
    mock_registry_cls.register_builtin_agents = Mock()
    session_file = tmp_path / "session.json"
    session_file.write_text("{}", encoding="utf-8")
    turns = [FakeTurn(0), FakeTurn(1)]
    alias = FakeSessionInfo("path-alias", session_file)
    session = FakeSessionInfo("canonical-session", session_file)
    source = FakeSource("kimi", sessions=[alias], turns=turns)
    mock_registry_cls.auto_discover.return_value = [source]
    engine = Mock()
    engine.canonicalize_session_info.return_value = session
    engine.get_synced_turns_for_session.return_value = [0]
    engine.build_backend_duplicate_cache.return_value = {}
    engine.sync_single_turn.return_value = Mock(action="new")
    engine.bind_session_raw_identities.return_value = turns
    engine.enqueue_session_for_distillation.side_effect = ValueError(
        "complete-session handoff requires authoritative Raw revision"
    )
    mock_engine_cls.return_value = engine

    _cmd_sync_backfill(_make_args())

    statuses = [call.args[1] for call in mock_status.call_args_list]
    final_stats = mock_status.call_args_list[-1].args[2]
    assert statuses == ["running", "failed"]
    assert final_stats["failed"] == 1
    assert final_stats["partial_sessions"] == 1
    engine.bind_session_raw_identities.assert_called_once_with(source, session, turns)
    engine.enqueue_session_for_distillation.assert_called_once_with(source, session, turns)
    assert "Failed: 1" in capsys.readouterr().out


@patch("core.cli.commands.sync._get_config")
@patch("core.sync_framework.registry.SourceRegistry")
@patch("core.sync_framework.sync_engine.SyncEngine")
@patch("core.cli.commands.sync._write_backfill_status")
def test_complete_handoff_failure_is_retried_before_replay_can_publish_done(
    mock_status,
    mock_engine_cls,
    mock_registry_cls,
    mock_get_config,
    tmp_path,
):
    mock_get_config.return_value = _make_config(tmp_path)
    mock_registry_cls.register_builtin_agents = Mock()
    session_file = tmp_path / "session.json"
    session_file.write_text("{}", encoding="utf-8")
    turns = [FakeTurn(0), FakeTurn(1)]
    session = FakeSessionInfo("canonical-session", session_file)
    source = FakeSource("kimi", sessions=[session], turns=turns)
    mock_registry_cls.auto_discover.return_value = [source]
    engine = Mock()
    engine.canonicalize_session_info.return_value = session
    engine.get_synced_turns_for_session.side_effect = [[0], [0, 1]]
    engine.build_backend_duplicate_cache.return_value = {}
    engine.sync_single_turn.return_value = Mock(action="new")
    engine.bind_session_raw_identities.return_value = turns
    engine.enqueue_session_for_distillation.side_effect = [
        ValueError("first handoff failed"),
        {"status": "queued", "receipt_id": "handoff-retry"},
    ]
    mock_engine_cls.return_value = engine

    _cmd_sync_backfill(_make_args())
    _cmd_sync_backfill(_make_args())

    statuses = [call.args[1] for call in mock_status.call_args_list]
    first_stats = mock_status.call_args_list[1].args[2]
    second_stats = mock_status.call_args_list[3].args[2]
    assert statuses == ["running", "failed", "running", "done"]
    assert first_stats["failed"] == 1
    assert first_stats["partial_sessions"] == 1
    assert second_stats["failed"] == 0
    assert second_stats["skipped_complete"] == 1
    assert engine.bind_session_raw_identities.call_count == 2
    assert engine.enqueue_session_for_distillation.call_count == 2


@patch("core.cli.commands.sync._get_config")
@patch("core.sync_framework.registry.SourceRegistry")
@patch("core.sync_framework.sync_engine.SyncEngine")
@patch("core.cli.commands.sync._write_backfill_status")
def test_backfill_sync_turns(_mock_status, mock_engine_cls, mock_registry_cls, mock_get_config, tmp_path, capsys):
    mock_get_config.return_value = _make_config(tmp_path)
    mock_registry_cls.register_builtin_agents = Mock()

    session_file = tmp_path / "session.json"
    session_file.write_text("{}")
    import time

    mtime = time.time()
    import os

    os.utime(session_file, (mtime, mtime))

    session = FakeSessionInfo("s1", session_file)
    source = FakeSource(
        "kimi",
        sessions=[session],
        turns=[FakeTurn(1), FakeTurn(2)],
    )
    mock_registry_cls.auto_discover.return_value = [source]

    engine = Mock()
    engine.canonicalize_session_info.side_effect = lambda session_info: session_info
    engine.get_synced_turns_for_session.return_value = []
    engine.sync_single_turn.side_effect = [
        Mock(action="new"),
        Mock(action="skipped"),
    ]
    engine.build_backend_duplicate_cache.return_value = {}
    mock_engine_cls.return_value = engine

    _cmd_sync_backfill(_make_args())

    captured = capsys.readouterr()
    assert "sync missing" in captured.out
    assert "Synced(new): 1" in captured.out
    assert "Skipped: 1" in captured.out
    engine.sync_single_turn.assert_called()


@patch("core.cli.commands.sync._get_config")
@patch("core.sync_framework.registry.SourceRegistry")
@patch("core.sync_framework.sync_engine.SyncEngine")
@patch("core.cli.commands.sync._write_backfill_status")
def test_limited_backfill_persists_partial_not_done(
    mock_status, mock_engine_cls, mock_registry_cls, mock_get_config, tmp_path, capsys
):
    mock_get_config.return_value = _make_config(tmp_path)
    mock_registry_cls.register_builtin_agents = Mock()
    session_file = tmp_path / "session.json"
    session_file.write_text("{}", encoding="utf-8")
    session = FakeSessionInfo("s1", session_file)
    source = FakeSource("kimi", sessions=[session], turns=[FakeTurn(0), FakeTurn(1)])
    mock_registry_cls.auto_discover.return_value = [source]
    engine = Mock()
    engine.canonicalize_session_info.side_effect = lambda session_info: session_info
    engine.get_synced_turns_for_session.return_value = []
    engine.build_backend_duplicate_cache.return_value = {}
    engine.sync_single_turn.return_value = Mock(action="new")
    mock_engine_cls.return_value = engine

    _cmd_sync_backfill(_make_args(max_turns=1))

    assert [call.args[1] for call in mock_status.call_args_list] == ["running", "partial"]
    assert mock_status.call_args_list[-1].args[2]["full_history_scope"] is False
    assert "不能声明历史回填完成" in capsys.readouterr().out


@patch("core.cli.commands.sync._get_config")
@patch("core.sync_framework.registry.SourceRegistry")
@patch("core.sync_framework.sync_engine.SyncEngine")
@patch("core.cli.commands.sync._write_backfill_status")
def test_backfill_parse_failure(_mock_status, mock_engine_cls, mock_registry_cls, mock_get_config, tmp_path, capsys):
    mock_get_config.return_value = _make_config(tmp_path)
    mock_registry_cls.register_builtin_agents = Mock()

    session_file = tmp_path / "session.json"
    session_file.write_text("{}")
    import time

    mtime = time.time()
    import os

    os.utime(session_file, (mtime, mtime))

    session = FakeSessionInfo("s1", session_file)
    source = FakeSource("kimi", sessions=[session])
    source.parse_turns = Mock(side_effect=ValueError("bad"))
    mock_registry_cls.auto_discover.return_value = [source]

    engine = Mock()
    mock_engine_cls.return_value = engine

    _cmd_sync_backfill(_make_args())

    captured = capsys.readouterr()
    assert "✗" in captured.out
    assert "Failed: 1" in captured.out
