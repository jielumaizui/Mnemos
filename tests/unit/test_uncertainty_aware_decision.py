"""
uncertainty_aware_decision 单元测试

覆盖项：
- get_effective_threshold 公式正确性
- decide 二分类/多选项
- BayesianThresholdAdapter 后验更新
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest
from core.uncertainty_aware_decision import UncertaintyAwareDecision, BayesianThresholdAdapter


class TestUncertaintyAwareDecision(unittest.TestCase):
    def test_threshold_no_uncertainty(self):
        uad = UncertaintyAwareDecision(base_threshold=0.5)
        self.assertEqual(uad.get_effective_threshold(0.0), 0.5)

    def test_threshold_max_uncertainty(self):
        uad = UncertaintyAwareDecision(base_threshold=0.5, max_relaxation=1.5)
        self.assertAlmostEqual(uad.get_effective_threshold(1.0), 0.75)

    def test_decide_binary_pass(self):
        uad = UncertaintyAwareDecision(base_threshold=0.5)
        result = uad.decide_binary(score=0.8, uncertainty=0.0)
        self.assertTrue(result.choice)
        self.assertEqual(result.confidence, 1.0)

    def test_decide_binary_fail(self):
        uad = UncertaintyAwareDecision(base_threshold=0.5)
        result = uad.decide_binary(score=0.3, uncertainty=0.0)
        self.assertFalse(result.choice)

    def test_decide_multi_option(self):
        uad = UncertaintyAwareDecision(base_threshold=0.5)
        result = uad.decide(
            options=["A", "B", "C"],
            scores=[0.2, 0.8, 0.4],
            uncertainty=0.0,
        )
        self.assertEqual(result.choice, "B")


class TestBayesianThresholdAdapter(unittest.TestCase):
    def test_posterior_uncertainty_decreases_with_success(self):
        bta = BayesianThresholdAdapter(base_threshold=0.5)
        # 初始无数据 → 高不确定性
        init_uncertainty = bta.get_posterior_uncertainty()
        self.assertGreater(init_uncertainty, 0.1)

        # 大量成功反馈 → 不确定性降低
        for _ in range(50):
            bta.update_from_feedback(success=True)
        later_uncertainty = bta.get_posterior_uncertainty()
        self.assertLess(later_uncertainty, init_uncertainty)

    def test_effective_threshold_adapts(self):
        bta = BayesianThresholdAdapter(base_threshold=0.5)
        init_threshold = bta.get_effective_threshold()

        # 大量成功 → 阈值应收紧（不确定性降低）
        for _ in range(50):
            bta.update_from_feedback(success=True)
        later_threshold = bta.get_effective_threshold()
        self.assertLess(later_threshold, init_threshold)


if __name__ == "__main__":
    unittest.main()
