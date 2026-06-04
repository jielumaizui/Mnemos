"""
ConnectionDiagnostics 单元测试

覆盖：
- check_memos / check_wiki / check_agents
- generate_task_list 优先级排序
- full_report / quick_status 数据结构
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class TestDiagnosticsDataclasses(unittest.TestCase):
    def test_memos_status_defaults(self):
        from core.diagnostics import MemosStatus
        s = MemosStatus()
        self.assertFalse(s.enabled)
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

    def test_connection_task_sorting(self):
        from core.diagnostics import ConnectionTask
        tasks = [
            ConnectionTask(priority="low", task="z"),
            ConnectionTask(priority="high", task="a"),
            ConnectionTask(priority="medium", task="m"),
        ]
        tasks.sort(key=lambda t: ({"high": 0, "medium": 1, "low": 2}.get(t.priority, 99), t.completed))
        self.assertEqual([t.priority for t in tasks], ["high", "medium", "low"])


class TestCheckMemos(unittest.TestCase):
    def test_memos_not_configured(self):
        from core.diagnostics import ConnectionDiagnostics
        mock_config = MagicMock()
        mock_config.memos_enabled = False
        mock_config.memos_token = ""
        mock_config.memos_api_url = ""

        status = ConnectionDiagnostics.check_memos(mock_config)
        self.assertFalse(status.enabled)
        self.assertFalse(status.configured)
        self.assertIsNone(status.reachable)

    def test_memos_configured_but_unreachable(self):
        from core.diagnostics import ConnectionDiagnostics
        mock_config = MagicMock()
        mock_config.memos_enabled = True
        mock_config.memos_token = "test-token"
        mock_config.memos_api_url = "http://localhost:5230"

        with patch("integrations.styx.MemosClient") as MockClient:
            instance = MockClient.return_value
            instance.list_all_memos.side_effect = Exception("Connection refused")
            status = ConnectionDiagnostics.check_memos(mock_config)

        self.assertTrue(status.configured)
        self.assertFalse(status.reachable)
        self.assertIn("Connection refused", status.error)


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


class TestGenerateTaskList(unittest.TestCase):
    def test_all_incomplete(self):
        from core.diagnostics import ConnectionDiagnostics, MemosStatus, WikiStatus

        memos = MemosStatus(enabled=False, configured=False)
        wiki = WikiStatus(path="/tmp/wiki", exists=False, writable=False)
        agents = []

        tasks = ConnectionDiagnostics.generate_task_list(memos, wiki, agents)
        self.assertGreater(len(tasks), 0)
        # 高优先级任务在最前
        self.assertEqual(tasks[0].priority, "high")
        self.assertIn("Memos", tasks[0].task)

    def test_all_complete(self):
        from core.diagnostics import ConnectionDiagnostics, MemosStatus, WikiStatus, AgentStatus

        memos = MemosStatus(enabled=True, configured=True, reachable=True)
        wiki = WikiStatus(path="/tmp/wiki", exists=True, writable=True)
        agents = [AgentStatus(name="claude", available=True, hooks_installed=True)]

        tasks = ConnectionDiagnostics.generate_task_list(memos, wiki, agents)
        # Memos 和 Wiki 的任务应该是 completed=True
        memos_tasks = [t for t in tasks if "Memos" in t.task]
        wiki_tasks = [t for t in tasks if "Wiki" in t.task]
        self.assertTrue(all(t.completed for t in memos_tasks))
        self.assertTrue(all(t.completed for t in wiki_tasks))

    def test_agent_hooks_pending(self):
        from core.diagnostics import ConnectionDiagnostics, MemosStatus, WikiStatus, AgentStatus

        memos = MemosStatus(enabled=True, configured=True, reachable=True)
        wiki = WikiStatus(path="/tmp/wiki", exists=True, writable=True)
        agents = [AgentStatus(name="claude", available=True, hooks_installed=False)]

        tasks = ConnectionDiagnostics.generate_task_list(memos, wiki, agents)
        hook_tasks = [t for t in tasks if "hooks" in t.task.lower()]
        self.assertEqual(len(hook_tasks), 1)
        self.assertEqual(hook_tasks[0].priority, "medium")
        self.assertFalse(hook_tasks[0].completed)


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
        self.assertIn("memos", status)
        self.assertIn("wiki", status)
        self.assertIn("agents", status)

        self.assertIn("configured", status["memos"])
        self.assertIn("reachable", status["memos"])
        self.assertIn("exists", status["wiki"])
        self.assertIn("writable", status["wiki"])
        self.assertIn("total", status["agents"])
        self.assertIn("hooked", status["agents"])
        self.assertIn("mcp", status["agents"])
        self.assertIn("policy", status["agents"])
        self.assertIn("active", status["agents"])


if __name__ == "__main__":
    unittest.main()
