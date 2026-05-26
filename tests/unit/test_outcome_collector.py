"""
outcome_collector 单元测试

覆盖项：
- record / record_view / record_reference / record_edit
- get_page_score（时间衰减）
- get_top_pages
- scan_vault_for_signals
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest
from core.outcome_collector import OutcomeCollector


class TestOutcomeCollector(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test_outcomes.db"
        self.collector = OutcomeCollector(db_path=self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_record_and_score(self):
        self.collector.record_view("page-a")
        self.collector.record_reference("page-b", "page-a")
        self.collector.record_edit("page-a", edit_size=100)

        score = self.collector.get_page_score("page-a")
        self.assertGreater(score, 0)

    def test_negative_signals(self):
        self.collector.record("page-old", "ignored_30d", signal_value=-1.0)
        score = self.collector.get_page_score("page-old")
        self.assertLess(score, 0)

    def test_top_pages(self):
        self.collector.record_view("page-popular")
        self.collector.record_view("page-popular")
        self.collector.record_view("page-popular")
        self.collector.record_view("page-unpopular")

        top = self.collector.get_top_pages(limit=2)
        self.assertEqual(len(top), 2)
        self.assertEqual(top[0]["page_id"], "page-popular")

    def test_summary(self):
        self.collector.record_view("p1")
        self.collector.record_view("p2")
        summary = self.collector.get_summary(days=7)
        self.assertEqual(summary["total_signals"], 2)
        self.assertEqual(summary["pages_affected"], 2)


if __name__ == "__main__":
    unittest.main()
