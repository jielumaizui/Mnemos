"""
ingest_helpers layer2_value_prejudge 单元测试

覆盖项：
- direct_distill (>=70)
- skip (<=30)
- llm_judge (30-70)
- 自动评分路径
- 带 rule_score 路径
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest
from core.kia.ingest_helpers import layer2_value_prejudge, score_message_quality


class TestLayer2ValuePrejudge(unittest.TestCase):
    def test_direct_distill_high_score(self):
        result = layer2_value_prejudge("Python asyncio is a powerful concurrency framework for building scalable network applications.")
        self.assertEqual(result["decision"], "direct_distill")
        self.assertGreaterEqual(result["score"], 70)
        self.assertGreater(result["confidence"], 0.5)

    def test_skip_low_score(self):
        result = layer2_value_prejudge("a")
        self.assertEqual(result["decision"], "skip")
        self.assertLessEqual(result["score"], 30)

    def test_llm_judge_middle(self):
        result = layer2_value_prejudge("hello world")
        self.assertEqual(result["decision"], "llm_judge")
        self.assertGreaterEqual(result["score"], 30)
        self.assertLessEqual(result["score"], 70)

    def test_with_explicit_rule_score(self):
        rule_score = {"total_score": 85.0, "length_score": 25, "density_score": 30, "richness_score": 30}
        result = layer2_value_prejudge("some content", rule_score=rule_score)
        self.assertEqual(result["decision"], "direct_distill")
        self.assertEqual(result["score"], 85.0)

    def test_threshold_boundary_70(self):
        rule_score = {"total_score": 70.0}
        result = layer2_value_prejudge("x", rule_score=rule_score)
        self.assertEqual(result["decision"], "direct_distill")

    def test_threshold_boundary_30(self):
        rule_score = {"total_score": 30.0}
        result = layer2_value_prejudge("x", rule_score=rule_score)
        self.assertEqual(result["decision"], "skip")


if __name__ == "__main__":
    unittest.main()
