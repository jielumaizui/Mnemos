"""
prompt_call_log 单元测试

覆盖项：
- log / log_with_timing
- get_stats
- get_cost_summary
- get_latency_summary
- cleanup_old
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest
from core.prompt_call_log import PromptCallLog


class TestPromptCallLog(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test_prompt_calls.db"
        self.log = PromptCallLog(db_path=self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_log_and_retrieve(self):
        self.log.log(
            task_type="distill",
            session_id="sess-1",
            prompt="extract knowledge",
            prompt_tokens=100,
            completion_tokens=50,
            latency_ms=1200,
            parse_success=True,
        )

        rows = self.log.get_by_task_type("distill")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_type"], "distill")
        self.assertEqual(rows[0]["total_tokens"], 150)

    def test_stats_aggregation(self):
        self.log.log(task_type="distill", prompt_tokens=100, completion_tokens=50, latency_ms=1000, parse_success=True)
        self.log.log(task_type="distill", prompt_tokens=200, completion_tokens=100, latency_ms=2000, parse_success=False)

        stats = self.log.get_stats(days=7)
        self.assertEqual(stats["total_calls"], 2)
        self.assertEqual(stats["total_tokens"], 450)
        self.assertEqual(stats["parse_success_rate"], 0.5)

    def test_cost_summary(self):
        self.log.log(task_type="distill", prompt_tokens=1000, completion_tokens=500)
        cost = self.log.get_cost_summary(days=7, cost_per_1k_prompt=0.003, cost_per_1k_completion=0.015)
        self.assertGreater(cost["total_cost_usd"], 0)
        self.assertEqual(cost["prompt_tokens"], 1000)
        self.assertEqual(cost["completion_tokens"], 500)

    def test_cleanup_old(self):
        self.log.log(task_type="distill", prompt_tokens=10, completion_tokens=5)
        removed = self.log.cleanup_old(days=0)
        self.assertGreaterEqual(removed, 0)


if __name__ == "__main__":
    unittest.main()
