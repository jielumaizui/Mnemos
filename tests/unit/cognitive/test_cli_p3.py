"""P3 audit unit tests for core.cognitive.cli command handlers."""

import argparse
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from core.cognitive import cli as cognitive_cli
from core.cognitive.models import Dimension, ObservationType

# ---------------------------------------------------------------------------
# core/cognitive/cli.py::_parse_since
# ---------------------------------------------------------------------------


class TestParseSince:
    def test_date_format(self):
        dt = cognitive_cli._parse_since("2026-05-01")
        assert dt.year == 2026
        assert dt.month == 5
        assert dt.day == 1

    def test_days_format(self):
        dt = cognitive_cli._parse_since("3d")
        assert (datetime.now() - dt).days == pytest.approx(3, abs=1)

    def test_hours_format(self):
        dt = cognitive_cli._parse_since("2hours")
        delta = datetime.now() - dt
        assert delta.total_seconds() / 3600 == pytest.approx(2, abs=1)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            cognitive_cli._parse_since("not-a-date")


# ---------------------------------------------------------------------------
# core/cognitive/cli.py::cmd_run
# ---------------------------------------------------------------------------


class TestCmdRun:
    def test_run_with_zero_files(self, monkeypatch, capsys):
        engine_mock = MagicMock()
        engine_mock.reader.get_stats.return_value = {
            "raw_events_db": None,
            "wiki_dir": None,
            "raw_items": 0,
            "wiki_files": 0,
            "total_items": 0,
        }

        monkeypatch.setattr(cognitive_cli, "ObservationEngine", lambda **kw: engine_mock)

        args = argparse.Namespace(raw_events_db=None, wiki_dir=None, since=None, dry_run=False)
        cognitive_cli.cmd_run(args)

        captured = capsys.readouterr()
        assert "没有找到任何数据源" in captured.out
        engine_mock.run.assert_not_called()

    def test_run_full(self, monkeypatch, capsys):
        batch_mock = MagicMock()
        batch_mock.observations = []
        batch_mock.source_count = 5
        batch_mock.period_start = datetime(2026, 1, 1)
        batch_mock.period_end = datetime(2026, 1, 2)
        batch_mock.dimension_counts = {}

        engine_mock = MagicMock()
        engine_mock.reader.get_stats.return_value = {
            "raw_events_db": "/data/raw_events.db",
            "wiki_dir": "/wiki",
            "raw_items": 2,
            "wiki_files": 3,
            "total_items": 5,
        }
        engine_mock.run.return_value = batch_mock
        engine_mock.get_store_stats.return_value = {
            "total_observations": 0,
            "latest_update": "2026-01-01",
        }

        monkeypatch.setattr(cognitive_cli, "ObservationEngine", lambda **kw: engine_mock)

        args = argparse.Namespace(
            raw_events_db="/data/raw_events.db",
            wiki_dir="/wiki",
            since=None,
            dry_run=False,
        )
        cognitive_cli.cmd_run(args)

        captured = capsys.readouterr()
        assert "全量提取" in captured.out
        assert "扫描文件: 5" in captured.out
        engine_mock.run.assert_called_once_with(persist=True)


# ---------------------------------------------------------------------------
# core/cognitive/cli.py::cmd_stats
# ---------------------------------------------------------------------------


class TestCmdStats:
    def test_stats(self, monkeypatch, capsys):
        store_mock = MagicMock()
        store_mock.get_stats.return_value = {"total_observations": 7}
        monkeypatch.setattr(cognitive_cli, "ObservationStore", lambda db_path: store_mock)

        args = argparse.Namespace(db=":memory:")
        cognitive_cli.cmd_stats(args)

        captured = capsys.readouterr()
        assert '"total_observations": 7' in captured.out
        store_mock.get_stats.assert_called_once()


# ---------------------------------------------------------------------------
# core/cognitive/cli.py::cmd_query
# ---------------------------------------------------------------------------


class TestCmdQuery:
    def test_query_by_dimension(self, monkeypatch, capsys):
        obs_mock = MagicMock()
        obs_mock.dimension = Dimension.ATTENTION
        obs_mock.observation_type = ObservationType.FREQUENCY
        obs_mock.value = {"x": 1}
        obs_mock.confidence = 0.8
        obs_mock.version = 1

        store_mock = MagicMock()
        store_mock.query.return_value = [obs_mock]
        monkeypatch.setattr(cognitive_cli, "ObservationStore", lambda db_path: store_mock)

        args = argparse.Namespace(db=":memory:", dimension="attention", limit=10)
        cognitive_cli.cmd_query(args)

        captured = capsys.readouterr()
        assert "查询到 1 条观察" in captured.out
        store_mock.query.assert_called_once_with(dimension=Dimension.ATTENTION, limit=10)

    def test_query_invalid_dimension(self, monkeypatch, capsys):
        store_mock = MagicMock()
        monkeypatch.setattr(cognitive_cli, "ObservationStore", lambda db_path: store_mock)

        args = argparse.Namespace(db=":memory:", dimension="not-a-dim", limit=10)
        cognitive_cli.cmd_query(args)

        captured = capsys.readouterr()
        assert "无效维度" in captured.out
        store_mock.query.assert_not_called()


# ---------------------------------------------------------------------------
# core/cognitive/cli.py::cmd_clear
# ---------------------------------------------------------------------------


class TestCmdClear:
    def test_clear_force(self, monkeypatch, capsys):
        store_mock = MagicMock()
        monkeypatch.setattr(cognitive_cli, "ObservationStore", lambda db_path: store_mock)

        args = argparse.Namespace(db=":memory:", force=True)
        cognitive_cli.cmd_clear(args)

        captured = capsys.readouterr()
        assert "已清空所有 Observation 数据" in captured.out
        store_mock.clear_all.assert_called_once()

    def test_clear_cancel(self, monkeypatch, capsys):
        store_mock = MagicMock()
        monkeypatch.setattr(cognitive_cli, "ObservationStore", lambda db_path: store_mock)
        monkeypatch.setattr("builtins.input", lambda prompt: "no")

        args = argparse.Namespace(db=":memory:", force=False)
        cognitive_cli.cmd_clear(args)

        captured = capsys.readouterr()
        assert "已取消" in captured.out
        store_mock.clear_all.assert_not_called()
