"""P3 audit unit tests for core.cli.commands.* command handlers."""

import argparse
from unittest.mock import MagicMock

import pytest


class FakeConfig:
    """Lightweight config stub for CLI command tests."""

    def __init__(self, tmp_path):
        self._tmp = tmp_path
        self.config_path = tmp_path / "configs" / "main.json"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir = tmp_path / ".mnemos"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_dir = self.data_dir
        self.wiki_dir = tmp_path / "wiki"
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir = tmp_path / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.obsidian_vault_path = self.raw_dir

    def vault_dir(self, name: str):
        if name == "mnemos":
            return self.wiki_dir
        if name == "raw":
            return self.raw_dir
        raise KeyError(name)


@pytest.fixture
def fake_config(tmp_path):
    return FakeConfig(tmp_path)


# ---------------------------------------------------------------------------
# core/cli/commands/cognitive_graph.py::cmd_cognitive_graph
# ---------------------------------------------------------------------------


class TestCmdCognitiveGraph:
    def test_stats(self, monkeypatch, fake_config, capsys):
        from core.cli.commands import cognitive_graph

        monkeypatch.setattr(cognitive_graph, "_get_config", lambda: fake_config)

        store_mock = MagicMock()
        store_mock.get_stats.return_value = {"nodes": 3, "relations": 5}
        updater_mock = MagicMock()

        monkeypatch.setattr("core.cognitive_graph.CognitiveGraphStore", lambda: store_mock)
        monkeypatch.setattr(
            "core.cognitive_graph.CognitiveGraphUpdater",
            lambda store: updater_mock,
        )

        args = argparse.Namespace(cg_cmd="stats", session_id=None, page_path=None, event_type=None)
        cognitive_graph.cmd_cognitive_graph(args)

        captured = capsys.readouterr()
        assert "跨层认知图统计" in captured.out
        assert '"nodes": 3' in captured.out
        store_mock.get_stats.assert_called_once()

    def test_reconcile(self, monkeypatch, fake_config, capsys):
        from core.cli.commands import cognitive_graph

        monkeypatch.setattr(cognitive_graph, "_get_config", lambda: fake_config)

        store_mock = MagicMock()
        updater_mock = MagicMock()
        updater_mock.reconcile.return_value = {"done": True}

        monkeypatch.setattr("core.cognitive_graph.CognitiveGraphStore", lambda: store_mock)
        monkeypatch.setattr(
            "core.cognitive_graph.CognitiveGraphUpdater",
            lambda store: updater_mock,
        )

        args = argparse.Namespace(
            cg_cmd="reconcile", session_id=None, page_path=None, event_type=None
        )
        cognitive_graph.cmd_cognitive_graph(args)

        captured = capsys.readouterr()
        assert "开始认知图 reconciliation" in captured.out
        assert '"done": true' in captured.out
        updater_mock.reconcile.assert_called_once()

    def test_ingest(self, monkeypatch, fake_config, capsys):
        from core.cli.commands import cognitive_graph
        from core.mnemos_bus import Event

        monkeypatch.setattr(cognitive_graph, "_get_config", lambda: fake_config)

        store_mock = MagicMock()
        updater_mock = MagicMock()

        monkeypatch.setattr("core.cognitive_graph.CognitiveGraphStore", lambda: store_mock)
        monkeypatch.setattr(
            "core.cognitive_graph.CognitiveGraphUpdater",
            lambda store: updater_mock,
        )

        bus_mock = MagicMock()
        bus_mock._dispatch_thread = None
        monkeypatch.setattr(cognitive_graph, "get_event_bus", lambda: bus_mock)

        args = argparse.Namespace(
            cg_cmd="ingest",
            session_id="s1",
            page_path="/wiki/page.md",
            event_type="wiki.page_updated",
        )
        cognitive_graph.cmd_cognitive_graph(args)

        captured = capsys.readouterr()
        assert "已发布事件: wiki.page_updated" in captured.out
        bus_mock.publish.assert_called_once()
        event = bus_mock.publish.call_args[0][0]
        assert isinstance(event, Event)
        assert event.payload["session_id"] == "s1"
        bus_mock._submit_event.assert_called_once_with(event)


# ---------------------------------------------------------------------------
# core/cli/commands/distill.py::_cmd_distill_audit
# ---------------------------------------------------------------------------


class TestCmdDistillAudit:
    def test_audit_counts(self, monkeypatch, fake_config, capsys):
        from core.cli.commands import distill

        monkeypatch.setattr(distill, "_get_config", lambda: fake_config)

        fake_config.wiki_dir.mkdir(parents=True, exist_ok=True)
        page = fake_config.wiki_dir / "page.md"
        page.write_text(
            "---\nsource_session: s1\ntruncated: true\nsource_coverage: 0.8\n---\ncontent\n",
            encoding="utf-8",
        )

        args = argparse.Namespace(distill_cmd="audit")
        distill._cmd_distill_audit(args)

        captured = capsys.readouterr()
        assert "蒸馏完整性审计结果" in captured.out
        assert "Wiki 页面总数: 1" in captured.out
        assert "截断输入页面: 1" in captured.out


# ---------------------------------------------------------------------------
# core/cli/commands/feedback.py::cmd_feedback
# ---------------------------------------------------------------------------


class TestCmdFeedback:
    def test_stats(self, monkeypatch, fake_config, capsys):
        from core.cli.commands import feedback

        monkeypatch.setattr(feedback, "_get_config", lambda: fake_config)

        engine_cls = MagicMock()
        engine_inst = MagicMock()
        engine_inst.get_feedback_summary.return_value = {"count": 7}
        engine_cls.return_value = engine_inst

        monkeypatch.setattr(
            "core.reflection.reflection_engine.ReflectionEngine",
            engine_cls,
        )

        args = argparse.Namespace(feedback_cmd="stats", days=14)
        feedback.cmd_feedback(args)

        captured = capsys.readouterr()
        assert "最近 14 天反馈统计" in captured.out
        assert '"count": 7' in captured.out
        engine_inst.get_feedback_summary.assert_called_once_with(days=14)


# ---------------------------------------------------------------------------
# core/cli/commands/mcp.py::_install_mcp_only_agent & _mcp_only_agent_status
# ---------------------------------------------------------------------------


class TestMcpOnlyAgent:
    def test_install_codex(self, monkeypatch, capsys):
        from core.cli.commands import mcp as mcp_cmd

        active_mock = MagicMock()
        active_mock.upsert_codex_mcp_server.return_value = True
        active_mock.install_agent_policy.return_value = True

        monkeypatch.setattr("integrations.active", active_mock)

        result = mcp_cmd._install_mcp_only_agent("codex")
        assert result is True
        captured = capsys.readouterr()
        assert "✓ MCP 主动工具" in captured.out

    def test_status_hermes(self, monkeypatch):
        from core.cli.commands import mcp as mcp_cmd

        active_mock = MagicMock()
        active_mock.yaml_mcp_configured.return_value = True
        active_mock.marked_block_installed.return_value = False

        monkeypatch.setattr("integrations.active", active_mock)

        status = mcp_cmd._mcp_only_agent_status("hermes")
        assert status == {"mcp": True, "policy": False}
        active_mock.yaml_mcp_configured.assert_called_once()

    def test_install_kiro(self, monkeypatch):
        from core.cli.commands import mcp as mcp_cmd

        active_mock = MagicMock()
        active_mock.upsert_kiro_mcp_server.return_value = True
        active_mock.install_agent_policy.return_value = True

        monkeypatch.setattr("integrations.active", active_mock)

        assert mcp_cmd._install_mcp_only_agent("kiro") is True
        active_mock.kiro_mcp_config_path.assert_called_once()
        active_mock.upsert_kiro_mcp_server.assert_called_once_with(
            active_mock.kiro_mcp_config_path.return_value
        )
        active_mock.install_agent_policy.assert_called_once_with("kiro")

    def test_status_kiro(self, monkeypatch):
        from core.cli.commands import mcp as mcp_cmd

        active_mock = MagicMock()
        active_mock.kiro_mcp_configured.return_value = True
        active_mock.marked_block_installed.return_value = True

        monkeypatch.setattr("integrations.active", active_mock)

        assert mcp_cmd._mcp_only_agent_status("kiro") == {"mcp": True, "policy": True}
        active_mock.kiro_mcp_config_path.assert_called_once()
        active_mock.kiro_mcp_configured.assert_called_once_with(
            active_mock.kiro_mcp_config_path.return_value
        )

    def test_install_unknown_agent_returns_false(self):
        from core.cli.commands import mcp as mcp_cmd

        assert mcp_cmd._install_mcp_only_agent("unknown") is False

    def test_status_unknown_agent_returns_none(self):
        from core.cli.commands import mcp as mcp_cmd

        assert mcp_cmd._mcp_only_agent_status("unknown") is None


# ---------------------------------------------------------------------------
# core/cli/commands/observe.py::cmd_observe
# ---------------------------------------------------------------------------


class TestCmdObserve:
    def test_run_full(self, monkeypatch, fake_config, capsys):
        from core.cli.commands import observe

        monkeypatch.setattr(observe, "_get_config", lambda: fake_config)

        batch_mock = MagicMock()
        batch_mock.observations = []
        batch_mock.total_observations = 0
        batch_mock.dimension_counts = {"attention": 2}

        engine_mock = MagicMock()
        engine_mock.reader.get_stats.return_value = {
            "raw_dir": str(fake_config.raw_dir),
            "wiki_dir": str(fake_config.wiki_dir),
            "raw_files": 1,
            "wiki_files": 2,
            "total_files": 3,
        }
        engine_mock.run.return_value = batch_mock

        monkeypatch.setattr(
            "core.cognitive.observation_engine.ObservationEngine", lambda **kw: engine_mock
        )

        args = argparse.Namespace(observe_cmd="run", full=True, since=None)
        observe.cmd_observe(args)

        captured = capsys.readouterr()
        assert "Observation 提取完成: 0 条观察" in captured.out
        engine_mock.run.assert_called_once_with(persist=True)

    def test_search(self, monkeypatch, capsys):
        from core.cli.commands import observe
        from core.cognitive.models import Dimension, SourceType

        obs_mock = MagicMock()
        obs_mock.dimension = Dimension.ATTENTION
        obs_mock.observation_type = MagicMock()
        obs_mock.observation_type.value = "frequency"
        obs_mock.evidence = ["evidence"]
        obs_mock.value = "value"

        index_mock = MagicMock()
        index_mock.query.return_value = [obs_mock]

        monkeypatch.setattr("core.cognitive.observation_store.ObservationIndex", lambda: index_mock)

        args = argparse.Namespace(
            observe_cmd="search",
            dimension="attention",
            source_type="wiki",
            limit=10,
        )
        observe.cmd_observe(args)

        captured = capsys.readouterr()
        assert "找到 1 条观察" in captured.out
        index_mock.query.assert_called_once_with(
            dimension=Dimension.ATTENTION,
            source_type=SourceType.WIKI,
            limit=10,
        )

    def test_search_dimension_only_uses_dimension_api(self, monkeypatch, capsys):
        from core.cli.commands import observe
        from core.cognitive.models import Dimension

        obs_mock = MagicMock()
        obs_mock.dimension = Dimension.ATTENTION
        obs_mock.observation_type = MagicMock()
        obs_mock.observation_type.value = "frequency"
        obs_mock.evidence = ["evidence"]
        obs_mock.value = "value"

        index_mock = MagicMock()
        index_mock.get_by_dimension.return_value = [obs_mock]

        monkeypatch.setattr("core.cognitive.observation_store.ObservationIndex", lambda: index_mock)

        args = argparse.Namespace(
            observe_cmd="search",
            dimension="attention",
            source_type=None,
            limit=10,
        )
        observe.cmd_observe(args)

        captured = capsys.readouterr()
        assert "找到 1 条观察" in captured.out
        index_mock.get_by_dimension.assert_called_once_with(Dimension.ATTENTION, limit=10)
        index_mock.query.assert_not_called()

    def test_stats(self, monkeypatch, capsys):
        from core.cli.commands import observe

        index_mock = MagicMock()
        index_mock.get_stats.return_value = {
            "total_observations": 5,
            "by_dimension": {"attention": 3},
            "by_source": {"wiki": 5},
            "latest_update": "2026-01-01",
        }

        monkeypatch.setattr("core.cognitive.observation_store.ObservationIndex", lambda: index_mock)

        args = argparse.Namespace(observe_cmd="stats")
        observe.cmd_observe(args)

        captured = capsys.readouterr()
        assert "Observation Index 统计" in captured.out
        assert "总观察数: 5" in captured.out


# ---------------------------------------------------------------------------
# core/cli/commands/vaults.py::cmd_vaults
# ---------------------------------------------------------------------------


class TestCmdVaults:
    def test_sync(self, monkeypatch, fake_config, capsys):
        from core.cli.commands import vaults

        monkeypatch.setattr(vaults, "_get_config", lambda: fake_config)

        result = {
            "vault_dir": str(fake_config.wiki_dir),
            "kg": {"pages": 1},
            "observation": {"pages": 2},
            "reflection": {"pages": 3},
            "persona": {"pages": 4},
            "git": {"committed": True, "output": "ok"},
        }
        monkeypatch.setattr(vaults, "sync_all_projections", lambda commit: result)

        args = argparse.Namespace(vaults_cmd="sync", no_commit=False, apply=True, dry_run=False)
        vaults.cmd_vaults(args)

        captured = capsys.readouterr()
        assert "开始重建认知 Vault Markdown 投影" in captured.out
        assert "KG 投影" in captured.out

    def test_status(self, monkeypatch, fake_config, capsys):
        from core.cli.commands import vaults

        fake_config.wiki_dir.mkdir(parents=True, exist_ok=True)
        fake_config.raw_dir.mkdir(parents=True, exist_ok=True)

        def list_vaults():
            return ["mnemos"]

        fake_config.list_vaults = list_vaults
        monkeypatch.setattr(vaults, "_get_config", lambda: fake_config)
        monkeypatch.setattr(
            vaults,
            "_print_vault_status",
            lambda cfg: ("status line", ["warning one"]),
        )

        args = argparse.Namespace(vaults_cmd="status")
        vaults.cmd_vaults(args)

        captured = capsys.readouterr()
        assert "status line" in captured.out
        assert "warning one" in captured.out
