import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.kia.dike import TaskClassifier, TaskLearner, classify_task


class TestDikeTaskClassifier(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "task_classifier.db"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _classifier(self, config_path=None):
        TaskClassifier._cold_start_counts.clear()
        return TaskClassifier(
            config_path=str(config_path) if config_path else None,
            history_db=str(self.db_path),
            feedback_db=str(self.db_path),
        )

    def test_classify_keeps_legacy_fields_and_adds_multilabel_fields(self):
        classifier = self._classifier()

        result = classifier.classify([
            {"role": "user", "content": "帮我写一个数据分析的 Python 脚本"}
        ])

        self.assertEqual(result.task_type, "coding")
        self.assertEqual(result.primary_type, result.task_type)
        self.assertEqual(result.subtype, "python")
        self.assertEqual(result.primary_subtype, result.subtype)
        self.assertGreater(result.confidence, 0)
        self.assertEqual(result.primary_confidence, result.confidence)
        self.assertTrue(result.top_types)
        self.assertIn("coding", result.all_scores)
        self.assertIn("python", result.matched_keywords)
        self.assertIn("coding", result.matched_keywords_by_type)

    def test_mixed_intent_detected(self):
        classifier = self._classifier()

        result = classifier.classify([
            {"role": "user", "content": "写一个 Python 数据分析报表，统计转化率趋势"}
        ])

        types = [task_type for task_type, _ in result.top_types]
        self.assertIn("coding", types)
        self.assertIn("analysis", types)
        self.assertTrue(result.mixed_intent)

    def test_loads_custom_yaml_config(self):
        config_path = Path(self.tmpdir.name) / "task_types.yaml"
        config_path.write_text(
            """
task_types:
  research:
    name: "技术研究"
    keywords: ["调研", "研究", "benchmark"]
    subtypes:
      tool-comparison: ["对比", "选型", "benchmark"]
    expected_goals:
      - "研究目标是什么？"
""",
            encoding="utf-8",
        )
        classifier = self._classifier(config_path=config_path)

        result = classifier.classify([
            {"role": "user", "content": "帮我调研一下向量数据库选型，对比 benchmark"}
        ])

        self.assertEqual(result.task_type, "research")
        self.assertEqual(result.subtype, "tool-comparison")
        self.assertIn("研究目标是什么？", result.expected_goals["_prompts"])

    def test_sqlite_history_contributes_score(self):
        classifier = self._classifier()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                INSERT INTO task_classification_history
                (session_id, task_type, subtype, summary, keywords, confidence, created_at)
                VALUES
                ('s1', 'strategy', 'market-entry', '进入 新市场 渠道 规划', '[]', 0.8, '2026-05-01T00:00:00'),
                ('s2', 'strategy', 'market-entry', '新市场 拓展 渠道 规划', '[]', 0.8, '2026-05-02T00:00:00')
            """)

        result = classifier.classify([
            {"role": "user", "content": "新市场 渠道 规划"}
        ])

        self.assertIn("strategy", result.all_scores)
        self.assertGreater(result.all_scores["strategy"], 0)

    def test_task_learner_feedback_adjusts_keyword_weight(self):
        classifier = self._classifier()
        learner = TaskLearner(str(self.db_path), classifier.task_taxonomy)

        learner.record_feedback("写一份复盘 review", predicted="writing", actual="review")

        self.assertLess(learner.get_adjusted_weight("writing", "写"), 1.0)
        self.assertGreater(learner.get_adjusted_weight("review", "review"), 1.0)

    def test_classify_task_helper_accepts_new_paths(self):
        result = classify_task(
            [{"role": "user", "content": "审查一下这段代码"}],
            history_db=str(self.db_path),
            feedback_db=str(self.db_path),
        )

        self.assertEqual(result.task_type, "review")
        self.assertEqual(result.subtype, "code-review")


if __name__ == "__main__":
    unittest.main()
