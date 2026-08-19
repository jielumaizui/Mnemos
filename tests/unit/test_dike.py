import json
import sqlite3
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path


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

        result = classifier.classify(
            [{"role": "user", "content": "帮我写一个数据分析的 Python 脚本"}]
        )

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
        self.assertEqual(
            result.expected_goals["_prompts"],
            classifier.get_expected_goal_prompts("coding"),
        )

    def test_mixed_intent_detected(self):
        classifier = self._classifier()

        result = classifier.classify(
            [{"role": "user", "content": "写一个 Python 数据分析报表，统计转化率趋势"}]
        )

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

        result = classifier.classify(
            [{"role": "user", "content": "帮我调研一下向量数据库选型，对比 benchmark"}]
        )

        self.assertEqual(result.task_type, "research")
        self.assertEqual(result.subtype, "tool-comparison")
        self.assertIn("研究目标是什么？", result.expected_goals["_prompts"])

    def test_get_expected_goal_prompts_uses_config_and_unknown_fallback(self):
        config_path = Path(self.tmpdir.name) / "task_types.yaml"
        config_path.write_text(
            """
task_types:
  research:
    name: "技术研究"
    keywords: ["调研"]
    expected_goals:
      - "研究目标是什么？"
      - "验收标准是什么？"
""",
            encoding="utf-8",
        )
        classifier = self._classifier(config_path=config_path)

        self.assertEqual(
            classifier.get_expected_goal_prompts("research"),
            ["研究目标是什么？", "验收标准是什么？"],
        )
        self.assertEqual(
            classifier.get_expected_goal_prompts("unknown"),
            ["预期目标是什么？", "期望达成什么效果？"],
        )

    def test_get_task_type_label_formats_known_and_unknown_types(self):
        classifier = self._classifier()

        self.assertEqual(
            classifier.get_task_type_label("coding", "python"),
            "编程开发/Python",
        )
        self.assertEqual(
            classifier.get_task_type_label("research", "deep-dive"),
            "research/deep-dive",
        )
        self.assertEqual(classifier.get_task_type_label("review"), "审查评审")

    def test_sqlite_history_contributes_score(self):
        classifier = self._classifier()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                INSERT INTO task_classification_history
                (session_id, task_type, subtype, summary, keywords, confidence, created_at)
                VALUES
                ('s1', 'strategy', 'market-entry', '进入 新市场 渠道 规划',
                 '[]', 0.8, '2026-05-01T00:00:00'),
                ('s2', 'strategy', 'market-entry', '新市场 拓展 渠道 规划',
                 '[]', 0.8, '2026-05-02T00:00:00')
            """)

        result = classifier.classify([{"role": "user", "content": "新市场 渠道 规划"}])

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

    def test_classify_task_helper_runs_optional_confirmation_callback(self):
        calls = []

        def confirm(task_type, subtype, messages):
            calls.append((task_type, subtype, messages))
            return True, 0.95

        result = classify_task(
            [{"role": "user", "content": "写代码"}],
            history_db=str(self.db_path),
            feedback_db=str(self.db_path),
            llm_confirm_callback=confirm,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "coding")
        self.assertTrue(result.confirmed)
        self.assertGreater(result.confidence, 0.8)

    def test_classification_result_context_summary_serializes(self):
        result = classify_task(
            [{"role": "user", "content": "请复盘这次项目交付并输出行动项"}],
            history_db=str(self.db_path),
            feedback_db=str(self.db_path),
        )

        self.assertEqual(result.context_summary, "请复盘这次项目交付并输出行动项")
        payload = json.loads(json.dumps(asdict(result), ensure_ascii=False))
        self.assertEqual(payload["context_summary"], "请复盘这次项目交付并输出行动项")

    def test_classification_result_suggested_confirmation_serializes(self):
        result = classify_task(
            [{"role": "user", "content": "帮我写一个数据分析的 Python 脚本"}],
            history_db=str(self.db_path),
            feedback_db=str(self.db_path),
        )

        payload = json.loads(json.dumps(asdict(result), ensure_ascii=False))

        self.assertEqual(payload["suggested_confirmation"], result.suggested_confirmation)
        self.assertIn(payload["suggested_confirmation"], {"silent", "hint", "ask"})
        self.assertEqual(payload["confirmed"], result.suggested_confirmation == "silent")

    def test_confirmed_classification_records_history_for_future_matching(self):
        config_path = Path(self.tmpdir.name) / "task_types.yaml"
        config_path.write_text(
            """
task_types:
  custom:
    name: "自定义任务"
    keywords: ["mnemosseed"]
    subtypes: {}
""",
            encoding="utf-8",
        )

        def confirm(_task_type, _subtype, _messages):
            return True, 0.95

        classify_task(
            [{"role": "user", "content": "mnemosseed alpha beta"}],
            config_path=str(config_path),
            history_db=str(self.db_path),
            feedback_db=str(self.db_path),
            llm_confirm_callback=confirm,
        )
        classify_task(
            [{"role": "user", "content": "mnemosseed alpha gamma"}],
            config_path=str(config_path),
            history_db=str(self.db_path),
            feedback_db=str(self.db_path),
            llm_confirm_callback=confirm,
        )

        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT task_type, summary
                FROM task_classification_history
                ORDER BY id
                """
            ).fetchall()

        self.assertEqual(
            rows,
            [
                ("custom", "mnemosseed alpha beta"),
                ("custom", "mnemosseed alpha gamma"),
            ],
        )

        result = classify_task(
            [{"role": "user", "content": "alpha beta gamma"}],
            config_path=str(config_path),
            history_db=str(self.db_path),
            feedback_db=str(self.db_path),
        )

        self.assertEqual(result.task_type, "custom")
        self.assertGreater(result.all_scores["custom"], 0)


if __name__ == "__main__":
    unittest.main()
