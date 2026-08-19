"""
ConnectionDiagnostics 单元测试

覆盖：
- check_storage / check_wiki / check_agents
- generate_task_list 优先级排序
- full_report / quick_status 数据结构
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestDiagnosticsDataclasses(unittest.TestCase):
    def test_storage_status_defaults(self):
        from core.diagnostics import StorageStatus

        s = StorageStatus()
        self.assertEqual(s.backend, "obsidian")
        self.assertFalse(s.configured)
        self.assertIsNone(s.reachable)

    def test_wiki_status_defaults(self):
        from core.diagnostics import WikiStatus

        s = WikiStatus()
        self.assertEqual(s.path, "")
        self.assertFalse(s.exists)

    def test_agent_status_defaults(self):
        from core.diagnostics import AgentStatus

        s = AgentStatus()
        self.assertEqual(s.name, "")
        self.assertFalse(s.available)
        self.assertFalse(s.hooks_installed)
        self.assertFalse(s.mcp_configured)
        self.assertFalse(s.policy_installed)
        self.assertFalse(s.active_ready)
        self.assertEqual(s.passive_source_state, "unknown")
        self.assertEqual(s.passive_source_error_code, "")

    def test_connection_task_sorting(self):
        from core.diagnostics import ConnectionTask

        tasks = [
            ConnectionTask(priority="low", task="z"),
            ConnectionTask(priority="high", task="a"),
            ConnectionTask(priority="medium", task="m"),
        ]
        tasks.sort(
            key=lambda t: ({"high": 0, "medium": 1, "low": 2}.get(t.priority, 99), t.completed)
        )
        self.assertEqual([t.priority for t in tasks], ["high", "medium", "low"])


class TestCheckStorage(unittest.TestCase):
    def test_storage_not_configured(self):
        from core.diagnostics import ConnectionDiagnostics

        mock_config = MagicMock()
        mock_config.storage_backend = "obsidian"
        mock_config.obsidian_vault_path = Path("/nonexistent/vault")

        status = ConnectionDiagnostics.check_storage(mock_config)
        self.assertEqual(status.backend, "obsidian")
        self.assertFalse(status.reachable)
        self.assertFalse(Path("/nonexistent/vault").exists())

    def test_storage_configured_and_reachable(self):
        from core.diagnostics import ConnectionDiagnostics

        with tempfile.TemporaryDirectory() as td:
            mock_config = MagicMock()
            mock_config.storage_backend = "obsidian"
            mock_config.obsidian_vault_path = Path(td)

            status = ConnectionDiagnostics.check_storage(mock_config)
            self.assertTrue(status.configured)
            self.assertTrue(status.reachable)

    def test_default_storage_check_is_read_only(self):
        from core.diagnostics import ConnectionDiagnostics

        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            before = sorted(path.iterdir())
            mock_config = MagicMock()
            mock_config.storage_backend = "obsidian"
            mock_config.obsidian_vault_path = path

            status = ConnectionDiagnostics.check_storage(mock_config)

            self.assertTrue(status.reachable)
            self.assertEqual(sorted(path.iterdir()), before)

    def test_explicit_storage_write_probe_preserves_same_name_user_file(self):
        from core.diagnostics import ConnectionDiagnostics

        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            sentinel = path / ".mnemos_write_test"
            sentinel.write_text("user-owned", encoding="utf-8")
            mock_config = MagicMock()
            mock_config.storage_backend = "obsidian"
            mock_config.obsidian_vault_path = path

            status = ConnectionDiagnostics.check_storage(
                mock_config, probe_writable=True
            )

            self.assertTrue(status.reachable)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "user-owned")
            self.assertEqual(sorted(path.iterdir()), [sentinel])

    def test_explicit_storage_probe_cleans_up_after_write_failure(self):
        from core.diagnostics import ConnectionDiagnostics

        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            mock_config = MagicMock()
            mock_config.storage_backend = "obsidian"
            mock_config.obsidian_vault_path = path

            with patch("core.diagnostics.os.write", side_effect=OSError("disk full")):
                status = ConnectionDiagnostics.check_storage(
                    mock_config, probe_writable=True
                )

            self.assertFalse(status.reachable)
            self.assertIn("disk full", status.error)
            self.assertEqual(list(path.iterdir()), [])

    def test_storage_reports_obsidian_raw_vault(self):
        from core.diagnostics import ConnectionDiagnostics

        with tempfile.TemporaryDirectory() as td:
            mock_config = MagicMock()
            mock_config.storage_backend = "obsidian"
            mock_config.obsidian_vault_path = Path(td)

            status = ConnectionDiagnostics.check_storage(mock_config)
            self.assertEqual(status.backend, "obsidian")
            self.assertEqual(status.path, td)
            self.assertTrue(status.configured)
            self.assertTrue(status.reachable)


class TestCheckWiki(unittest.TestCase):
    def test_wiki_not_exists(self):
        from core.diagnostics import ConnectionDiagnostics

        mock_config = MagicMock()
        mock_config.wiki_dir = Path("/nonexistent/wiki")

        status = ConnectionDiagnostics.check_wiki(mock_config)
        self.assertFalse(status.exists)
        self.assertFalse(status.writable)

    def test_wiki_exists_and_writable(self):
        from core.diagnostics import ConnectionDiagnostics

        with tempfile.TemporaryDirectory() as td:
            mock_config = MagicMock()
            mock_config.wiki_dir = Path(td)

            status = ConnectionDiagnostics.check_wiki(mock_config)
            self.assertTrue(status.exists)
            self.assertTrue(status.writable)


class TestCheckAgents(unittest.TestCase):
    def setUp(self):
        from core.diagnostics import clear_agent_status_providers

        clear_agent_status_providers()

    def tearDown(self):
        from core.diagnostics import clear_agent_status_providers

        clear_agent_status_providers()

    def test_check_agents_uses_registered_status_provider(self):
        from core.diagnostics import (
            AgentStatus,
            ConnectionDiagnostics,
            register_agent_status_provider,
        )

        class FakeProvider:
            def list_agent_statuses(self):
                return [
                    AgentStatus(
                        name="claude",
                        available=True,
                        hooks_installed=True,
                        active_ready=True,
                    )
                ]

        register_agent_status_provider(FakeProvider(), key="fake")

        with patch(
            "core.sync_framework.registry.SourceRegistry.list_registered",
            return_value=[],
        ):
            agents = ConnectionDiagnostics.check_agents(load_default_providers=False)

        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].name, "claude")
        self.assertTrue(agents[0].active_ready)

    def test_check_agents_can_use_isolated_default_providers_without_registration(self):
        from core.diagnostics import AgentStatus, ConnectionDiagnostics

        class FakeProvider:
            def list_agent_statuses(self):
                return [AgentStatus(name="codex", available=True, active_ready=True)]

        with patch(
            "core.diagnostics._isolated_default_agent_status_providers",
            return_value=[FakeProvider()],
        ):
            agents = ConnectionDiagnostics.check_agents(
                load_default_providers=False,
                isolated_default_providers=True,
            )

        self.assertEqual([(agent.name, agent.active_ready) for agent in agents], [("codex", True)])
        self.assertEqual(
            ConnectionDiagnostics.check_agents(load_default_providers=False),
            [],
        )

    def test_registered_passive_source_failure_is_reported_unavailable(self):
        from core.diagnostics import ConnectionDiagnostics
        from core.sync_framework.registry import SourceRegistryUnavailableError

        with (
            patch(
                "core.sync_framework.registry.SourceRegistry.list_registered",
                return_value=["codex"],
            ),
            patch(
                "core.sync_framework.registry.SourceRegistry.get",
                side_effect=SourceRegistryUnavailableError("codex", "get"),
            ),
        ):
            agents = ConnectionDiagnostics.check_agents(load_default_providers=True)

        codex = next(agent for agent in agents if agent.name == "codex")
        self.assertFalse(codex.passive_source_available)
        self.assertEqual(codex.passive_source_state, "unavailable")
        self.assertEqual(
            codex.passive_source_error_code,
            "source_registry_unavailable:codex:get",
        )


class TestGenerateTaskList(unittest.TestCase):
    def test_all_incomplete_obsidian(self):
        from core.diagnostics import ConnectionDiagnostics, WikiStatus, StorageStatus

        wiki = WikiStatus(path="/tmp/wiki", exists=False, writable=False)
        agents = []
        storage = StorageStatus(backend="obsidian", configured=False, reachable=False)

        tasks = ConnectionDiagnostics.generate_task_list(wiki, agents, storage)
        self.assertGreater(len(tasks), 0)
        # 高优先级任务在最前
        self.assertEqual(tasks[0].priority, "high")
        self.assertIn("Obsidian", tasks[0].task)

    def test_all_complete(self):
        from core.diagnostics import ConnectionDiagnostics, WikiStatus, AgentStatus, StorageStatus

        wiki = WikiStatus(path="/tmp/wiki", exists=True, writable=True)
        agents = [AgentStatus(name="claude", available=True, hooks_installed=True)]
        storage = StorageStatus(backend="obsidian", configured=True, reachable=True)

        tasks = ConnectionDiagnostics.generate_task_list(wiki, agents, storage)
        # Storage 和 Wiki 的任务应该是 completed=True
        storage_tasks = [t for t in tasks if "Obsidian" in t.task]
        wiki_tasks = [t for t in tasks if "Wiki" in t.task]
        self.assertTrue(all(t.completed for t in storage_tasks))
        self.assertTrue(all(t.completed for t in wiki_tasks))

    def test_agent_hooks_pending(self):
        from core.diagnostics import ConnectionDiagnostics, WikiStatus, AgentStatus, StorageStatus

        wiki = WikiStatus(path="/tmp/wiki", exists=True, writable=True)
        agents = [AgentStatus(name="claude", available=True, hooks_installed=False)]
        storage = StorageStatus(backend="obsidian", configured=True, reachable=True)

        tasks = ConnectionDiagnostics.generate_task_list(wiki, agents, storage)
        hook_tasks = [t for t in tasks if "hooks" in t.task.lower()]
        self.assertEqual(len(hook_tasks), 1)
        self.assertEqual(hook_tasks[0].priority, "medium")
        self.assertFalse(hook_tasks[0].completed)

    def test_unavailable_source_is_not_reported_as_not_installed(self):
        from core.diagnostics import (
            AgentStatus,
            ConnectionDiagnostics,
            StorageStatus,
            WikiStatus,
        )

        tasks = ConnectionDiagnostics.generate_task_list(
            WikiStatus(path="/tmp/wiki", exists=True, writable=True),
            [
                AgentStatus(
                    name="codex",
                    passive_source_state="unavailable",
                    passive_source_error_code="source_registry_unavailable:codex:get",
                )
            ],
            StorageStatus(backend="obsidian", configured=True, reachable=True),
        )

        assert any("恢复 codex 被动数据源诊断" == task.task for task in tasks)
        assert not any("未检测到任何支持的 Agent" in task.action for task in tasks)

    def test_task_list_reuses_one_agent_snapshot_without_second_path_probe(self):
        from core.diagnostics import (
            AgentStatus,
            ConnectionDiagnostics,
            StorageStatus,
            WikiStatus,
        )
        from core.sync_framework.registry import PathDiscover

        agents = [
            AgentStatus(
                name="aider",
                passive_source_available=True,
                passive_source_state="available",
                data_dir="/isolated/aider",
            )
        ]
        with patch.object(
            PathDiscover,
            "find",
            side_effect=AssertionError(
                "diagnostics must not probe source paths twice"
            ),
        ):
            tasks = ConnectionDiagnostics.generate_task_list(
                WikiStatus(path="/wiki", exists=True, writable=True),
                agents,
                StorageStatus(
                    backend="obsidian",
                    configured=True,
                    reachable=True,
                    path="/vault",
                ),
            )

        aider = next(task for task in tasks if task.task == "发现 aider 数据源")
        self.assertTrue(aider.completed)
        self.assertEqual(aider.action, "路径: /isolated/aider")


class TestFullReport(unittest.TestCase):
    def test_report_structure(self):
        from core.diagnostics import ConnectionDiagnostics

        report = ConnectionDiagnostics.full_report()
        self.assertIn("connections", report)
        self.assertIn("agents", report)
        self.assertIn("missing", report)
        self.assertIn("tasks", report)
        self.assertIn("host_agent", report)
        self.assertIn("mnemos_version", report)

        # tasks 应该是字典列表
        for task in report["tasks"]:
            self.assertIn("priority", task)
            self.assertIn("task", task)
            self.assertIn("action", task)
            self.assertIn("completed", task)


class TestQuickStatus(unittest.TestCase):
    def test_quick_status_structure(self):
        from core.diagnostics import ConnectionDiagnostics

        status = ConnectionDiagnostics.quick_status()
        self.assertIn("ready", status)
        self.assertIn("storage", status)
        self.assertIn("wiki", status)
        self.assertIn("agents", status)

        self.assertIn("backend", status["storage"])
        self.assertIn("configured", status["storage"])
        self.assertIn("reachable", status["storage"])
        self.assertIn("exists", status["wiki"])
        self.assertIn("writable", status["wiki"])
        self.assertIn("total", status["agents"])
        self.assertIn("hooked", status["agents"])
        self.assertIn("mcp", status["agents"])
        self.assertIn("policy", status["agents"])
        self.assertIn("active", status["agents"])


if __name__ == "__main__":
    unittest.main()
