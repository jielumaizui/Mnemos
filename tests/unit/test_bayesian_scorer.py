"""
bayesian_scorer 单元测试

覆盖项：
- BetaDimensionScorer 后验计算
- BayesianScorer 评分 + 反馈闭环
- 置信度随样本增加而提升
- 数据库持久化
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest
from core.kia.bayesian_scorer import (
    BetaDimensionScorer, BayesianScorer, DimensionScore,
)


class TestBetaDimensionScorer(unittest.TestCase):
    def test_posterior_mean_uniform_prior(self):
        """均匀先验 α=β=2，无观测时后验均值为 0.5"""
        scorer = BetaDimensionScorer("test", alpha=2.0, beta=2.0)
        self.assertAlmostEqual(scorer.posterior_mean(), 0.5)

    def test_posterior_after_positive_observation(self):
        """正例观测后后验均值上升"""
        scorer = BetaDimensionScorer("test", alpha=2.0, beta=2.0)
        scorer.observe(True)
        self.assertGreater(scorer.posterior_mean(), 0.5)

    def test_posterior_after_negative_observation(self):
        """负例观测后后验均值下降"""
        scorer = BetaDimensionScorer("test", alpha=2.0, beta=2.0)
        scorer.observe(False)
        self.assertLess(scorer.posterior_mean(), 0.5)

    def test_confidence_increases_with_samples(self):
        """样本越多，置信度越高"""
        scorer = BetaDimensionScorer("test", alpha=2.0, beta=2.0)
        low_conf = scorer.posterior_confidence()
        for _ in range(20):
            scorer.observe(True)
        high_conf = scorer.posterior_confidence()
        self.assertGreater(high_conf, low_conf)

    def test_reset(self):
        """重置后回到先验"""
        scorer = BetaDimensionScorer("test", alpha=2.0, beta=2.0)
        scorer.observe(True)
        scorer.observe(True)
        scorer.reset()
        self.assertAlmostEqual(scorer.posterior_mean(), 0.5)


class TestBayesianScorer(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test_bayesian.db"
        self.scorer = BayesianScorer(db_path=self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_score_returns_dimension_score(self):
        """评分返回 DimensionScore"""
        result = self.scorer.score("quality", rule_prior=0.6)
        self.assertIsInstance(result, DimensionScore)
        self.assertEqual(result.dimension, "quality")
        self.assertAlmostEqual(result.prior, 0.6)

    def test_score_with_ml_likelihood(self):
        """融合 ML 似然后评分"""
        result = self.scorer.score("quality", rule_prior=0.5, ml_likelihood=0.8)
        self.assertIsInstance(result, DimensionScore)
        self.assertIsNotNone(result.likelihood)

    def test_feedback_updates_posterior(self):
        """反馈后后验更新"""
        before = self.scorer.score("quality", rule_prior=0.5)
        self.scorer.feedback("quality", is_positive=True, weight=1.0)
        after = self.scorer.score("quality", rule_prior=0.5)
        self.assertNotEqual(before.score, after.score)

    def test_persistence(self):
        """状态持久化到数据库"""
        self.scorer.feedback("quality", is_positive=True, weight=5.0)

        # 新建实例，应该加载之前的状态
        scorer2 = BayesianScorer(db_path=self.db_path)
        status = scorer2.get_dimension_status("quality")
        self.assertEqual(status["sample_count"], 5)

    def test_dimension_status_cold_warm_hot(self):
        """维度状态随样本数变化"""
        # COLD
        status = self.scorer.get_dimension_status("new_dim")
        self.assertEqual(status["status"], "cold")

        # WARM
        for _ in range(10):
            self.scorer.feedback("new_dim", is_positive=True)
        status = self.scorer.get_dimension_status("new_dim")
        self.assertEqual(status["status"], "warm")

        # HOT
        for _ in range(50):
            self.scorer.feedback("new_dim", is_positive=True)
        status = self.scorer.get_dimension_status("new_dim")
        self.assertEqual(status["status"], "hot")

    def test_score_multi(self):
        """多维度批量评分"""
        card = self.scorer.score_multi(
            dimensions=["noise", "quality"],
            rule_priors={"noise": 0.2, "quality": 0.8},
        )
        self.assertIn("noise", card.scores)
        self.assertIn("quality", card.scores)


if __name__ == "__main__":
    unittest.main()
