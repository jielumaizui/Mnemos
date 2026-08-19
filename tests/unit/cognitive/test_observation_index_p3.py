"""P3 audit unit tests for core.cognitive.observation_index CLI helpers."""

import argparse
from unittest.mock import MagicMock


from core.cognitive import observation_index

# ---------------------------------------------------------------------------
# core/cognitive/observation_index.py::cmd_rebuild
# ---------------------------------------------------------------------------


class TestCmdRebuild:
    def test_rebuild(self, monkeypatch, tmp_path, capsys):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()

        fake_config = MagicMock()
        fake_config.database_dir = tmp_path / "db"
        fake_config.wiki_dir = wiki_dir
        monkeypatch.setattr(observation_index, "get_config", lambda: fake_config)

        index_mock = MagicMock()
        index_mock.rebuild_from_sources.return_value = {"added": 5}
        monkeypatch.setattr(observation_index, "ObservationIndex", lambda: index_mock)

        args = argparse.Namespace(wiki_dir=str(wiki_dir), raw_events_db=None, no_backup=False)
        rc = observation_index.cmd_rebuild(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert "重建完成" in captured.out
        assert '"added": 5' in captured.out
        index_mock.rebuild_from_sources.assert_called_once_with(
            raw_events_db=str(tmp_path / "db" / "raw_events.db"),
            wiki_dir=str(wiki_dir),
            backup=True,
        )

    def test_rebuild_missing_wiki(self, monkeypatch, tmp_path, capsys):
        fake_config = MagicMock()
        fake_config.database_dir = tmp_path / "db"
        fake_config.wiki_dir = tmp_path / "wiki"
        monkeypatch.setattr(observation_index, "get_config", lambda: fake_config)

        args = argparse.Namespace(
            wiki_dir="/does/not/exist",
            raw_events_db=None,
            no_backup=False,
        )
        rc = observation_index.cmd_rebuild(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert "未找到 Wiki 目录" in captured.err


# ---------------------------------------------------------------------------
# core/cognitive/observation_index.py::cmd_stats
# ---------------------------------------------------------------------------


class TestCmdStats:
    def test_stats(self, monkeypatch, capsys):
        index_mock = MagicMock()
        index_mock.get_stats.return_value = {"total_observations": 9}
        monkeypatch.setattr(observation_index, "ObservationIndex", lambda: index_mock)

        args = argparse.Namespace(wiki_dir=None, raw_events_db=None)
        rc = observation_index.cmd_stats(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert '"total_observations": 9' in captured.out
        index_mock.get_stats.assert_called_once()


# ---------------------------------------------------------------------------
# core/cognitive/observation_index.py::cmd_check
# ---------------------------------------------------------------------------


class TestCmdCheck:
    def test_check_healthy(self, monkeypatch, tmp_path, capsys):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()

        obs_mock = MagicMock()
        obs_mock.id = "obs-1"
        obs_mock.source_id = "src-1"
        obs_mock.source_path = ""
        obs_mock.confidence = 0.8

        index_mock = MagicMock()
        index_mock.query.return_value = [obs_mock]
        index_mock.get_stats.return_value = {"total_observations": 1, "by_dimension": {}}
        monkeypatch.setattr(observation_index, "ObservationIndex", lambda: index_mock)

        args = argparse.Namespace(wiki_dir=str(wiki_dir), raw_events_db=None)
        rc = observation_index.cmd_check(args)

        assert rc == 0
        captured = capsys.readouterr()
        assert '"healthy": true' in captured.out

    def test_check_unhealthy(self, monkeypatch, tmp_path, capsys):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()

        obs_mock = MagicMock()
        obs_mock.id = "obs-1"
        obs_mock.source_id = ""
        obs_mock.source_path = ""
        obs_mock.confidence = 1.5

        index_mock = MagicMock()
        index_mock.query.return_value = [obs_mock]
        index_mock.get_stats.return_value = {"total_observations": 1, "by_dimension": {}}
        monkeypatch.setattr(observation_index, "ObservationIndex", lambda: index_mock)

        args = argparse.Namespace(wiki_dir=str(wiki_dir), raw_events_db=None)
        rc = observation_index.cmd_check(args)

        assert rc == 1
        captured = capsys.readouterr()
        assert '"healthy": false' in captured.out


# ---------------------------------------------------------------------------
# core/cognitive/observation_index.py::ObservationIndexIntegrityCheck.run
# Tests for ObservationIndexIntegrityCheck.run
# ---------------------------------------------------------------------------


class FakeObservation:
    def __init__(self, oid, source_id=True, source_path=True, confidence=0.5):
        self.id = oid
        self.source_id = source_id
        self.source_path = source_path
        self.confidence = confidence


class TestObservationIndexIntegrityCheckRun:
    def test_run_healthy(self, monkeypatch, tmp_path):
        index_mock = MagicMock()
        index_mock.query.return_value = [
            FakeObservation("o1"),
            FakeObservation("o2", confidence=0.99),
        ]
        index_mock.get_stats.return_value = {
            "total_observations": 2,
            "by_dimension": {"attention": 1},
        }

        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        obs_dir = wiki_dir / "L3-Observations"
        obs_dir.mkdir()
        (obs_dir / "attention.md").write_text("# attention")

        checker = observation_index.ObservationIndexIntegrityCheck(
            index=index_mock,
            wiki_dir=str(wiki_dir),
        )
        report = checker.run()

        assert report["healthy"] is True
        assert report["issues"] == []
        assert report["total_observations"] == 2

    def test_run_empty_index_when_source_exists(self, monkeypatch, tmp_path):
        index_mock = MagicMock()
        index_mock.query.return_value = []
        index_mock.get_stats.return_value = {
            "total_observations": 0,
            "by_dimension": {},
        }

        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()

        checker = observation_index.ObservationIndexIntegrityCheck(
            index=index_mock,
            wiki_dir=str(wiki_dir),
        )
        report = checker.run()

        assert report["healthy"] is False
        assert any("Observation Index 为空" in i for i in report["issues"])

    def test_run_invalid_confidence(self, monkeypatch, tmp_path):
        index_mock = MagicMock()
        index_mock.query.return_value = [
            FakeObservation("o1", confidence=1.5),
        ]
        index_mock.get_stats.return_value = {
            "total_observations": 1,
            "by_dimension": {},
        }

        checker = observation_index.ObservationIndexIntegrityCheck(
            index=index_mock,
            wiki_dir=None,
        )
        report = checker.run()

        assert report["healthy"] is False
        assert any("置信度越界" in i for i in report["issues"])

    def test_run_missing_source(self, monkeypatch, tmp_path):
        index_mock = MagicMock()
        index_mock.query.return_value = [
            FakeObservation("o1", source_id=False, source_path=False),
        ]
        index_mock.get_stats.return_value = {
            "total_observations": 1,
            "by_dimension": {},
        }

        checker = observation_index.ObservationIndexIntegrityCheck(
            index=index_mock,
            wiki_dir=None,
        )
        report = checker.run()

        assert report["healthy"] is False
        assert any("缺少 source_id" in i for i in report["issues"])

    def test_run_wiki_projection_mismatch(self, monkeypatch, tmp_path):
        index_mock = MagicMock()
        index_mock.query.return_value = []
        index_mock.get_stats.return_value = {
            "total_observations": 0,
            "by_dimension": {"attention": 0},
        }

        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        obs_dir = wiki_dir / "L3-Observations"
        obs_dir.mkdir()
        (obs_dir / "missing.md").write_text("# missing")

        checker = observation_index.ObservationIndexIntegrityCheck(
            index=index_mock,
            wiki_dir=str(wiki_dir),
        )
        report = checker.run()

        assert report["healthy"] is False
        assert any("Index 维度未在 Wiki 投影" in i for i in report["issues"])
        assert any("Wiki 投影包含未知维度" in i for i in report["issues"])

    def test_run_wiki_projection_part_shards_are_not_unknown_dimensions(
        self, monkeypatch, tmp_path
    ):
        index_mock = MagicMock()
        index_mock.query.return_value = []
        index_mock.get_stats.return_value = {
            "total_observations": 0,
            "by_dimension": {"attention": 0},
        }

        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        obs_dir = wiki_dir / "L3-Observations"
        obs_dir.mkdir()
        (obs_dir / "attention.md").write_text("# index")
        (obs_dir / "attention.part-001.md").write_text("# shard")
        (obs_dir / "attention.part-002.md").write_text("# shard")

        checker = observation_index.ObservationIndexIntegrityCheck(
            index=index_mock,
            wiki_dir=str(wiki_dir),
        )
        report = checker.run()

        assert not any("Wiki 投影包含未知维度" in i for i in report["issues"])
        assert not any("Index 维度未在 Wiki 投影" in i for i in report["issues"])
