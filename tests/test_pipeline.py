"""端到端测试 — 验证完整数据链路"""

import sys
import os
import json
import tempfile
import shutil
import unittest
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestAgentDiscovery(unittest.TestCase):
    """Agent 检测器测试"""

    def test_detector_returns_status(self):
        from core.helios import AgentDetector
        detector = AgentDetector()
        report = detector.get_status_report()
        self.assertIn("available_count", report)
        self.assertIn("available", report)

    def test_registry_discover_all(self):
        from integrations.olympus import AgentRegistry
        agents = AgentRegistry.discover_all()
        # 至少应发现 Claude Code（因为当前就是在 Claude 中运行）
        self.assertIsInstance(agents, list)


class TestHephaestusWorker(unittest.TestCase):
    """蒸馏 Worker 测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        # Mock 目录
        self.queue_dir = Path(self.temp_dir) / "distill_queue"
        self.output_dir = Path(self.temp_dir) / "distill_output"
        self.inbox_dir = Path(self.temp_dir) / "inbox"
        self.archive_dir = Path(self.temp_dir) / "archive"
        self.queue_dir.mkdir()
        self.output_dir.mkdir()
        self.inbox_dir.mkdir()
        self.archive_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_worker_stats(self):
        from core.hephaestus_worker import HephaestusWorker
        worker = HephaestusWorker()
        stats = worker.get_stats()
        self.assertIn("pending", stats)
        self.assertIn("delegated", stats)

    def test_process_empty_queue(self):
        from core.hephaestus_worker import HephaestusWorker
        from unittest.mock import patch
        # 使用临时队列目录（空），并 mock amphora 返回空列表以隔离真实数据
        worker = HephaestusWorker(queue_dir=self.queue_dir)
        with patch("core.kia.amphora.list_pending", return_value=[]):
            with patch("core.kia.amphora.get_next", return_value=None):
                processed = worker.process_all()
        self.assertEqual(processed, 0)

    def test_collect_completed_no_output(self):
        from core.hephaestus_worker import HephaestusWorker
        from unittest.mock import patch
        # 使用临时输出目录（空），并 mock amphora 以隔离真实数据
        worker = HephaestusWorker(output_dir=self.output_dir)
        with patch("core.kia.amphora.list_processing", return_value=[]):
            collected = worker.collect_completed()
        self.assertEqual(collected, 0)


class TestAgentDelegate(unittest.TestCase):
    """任务委托层测试"""

    def test_delegate_without_agent(self):
        """无可用 Agent 时，委托应返回 False"""
        from core.prometheus_fire import AgentDelegate, DistillTask

        delegate = AgentDelegate()
        # 强制覆盖为无 Agent 状态
        delegate.detector.select_best = lambda: None

        task = DistillTask(
            session_id="test-session-001",
            messages=[{"role": "user", "content": "hello"}],
            meta={"source": "test"},
        )
        # 无 Agent 时，delegate 应返回 False（不崩溃）
        result = delegate.delegate(task, Path("/tmp/test_output.md"))
        self.assertFalse(result)

    def test_build_distill_prompt(self):
        from core.prometheus_fire import AgentDelegate, DistillTask
        delegate = AgentDelegate()
        task = DistillTask(
            session_id="test-001",
            messages=[
                {"role": "user", "content": "How to foo?"},
                {"role": "assistant", "content": "Bar baz."},
            ],
            meta={"source": "test"},
        )
        prompt = delegate.build_distill_prompt(task)
        self.assertIn("test-001", prompt)
        self.assertIn("How to foo?", prompt)
        self.assertIn("Bar baz.", prompt)


class TestFullPipeline(unittest.TestCase):
    """完整链路测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["MNEMOS_DATA_DIR"] = self.temp_dir

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        if "MNEMOS_DATA_DIR" in os.environ:
            del os.environ["MNEMOS_DATA_DIR"]

    def test_distill_task_to_dict(self):
        from core.prometheus_fire import DistillTask
        task = DistillTask(
            session_id="pipe-001",
            messages=[{"role": "user", "content": "test"}],
            meta={"source": "claude", "working_dir": "/tmp"},
        )
        d = task.to_dict()
        self.assertEqual(d["session_id"], "pipe-001")
        self.assertEqual(d["meta"]["source"], "claude")

    def test_config_paths_exist(self):
        from core.config import get_config
        config = get_config()
        # 基本路径应可访问
        self.assertIsNotNone(config.wiki_dir)
        self.assertIsNotNone(config.config_path)


if __name__ == "__main__":
    unittest.main()
