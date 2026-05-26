"""
weight_adapter 单元测试

覆盖项：
- HardcodedWeightAdapter 预设权重
- BayesianWeightAdapter 后验计算
- AutoSwitchWeightAdapter 自动切换
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest
from core.weight_adapter import (
    HardcodedWeightAdapter,
    BayesianWeightAdapter,
    AutoSwitchWeightAdapter,
)


class TestHardcodedWeightAdapter(unittest.TestCase):
    def test_default_weights_sum_to_one(self):
        hwa = HardcodedWeightAdapter()
        for domain, weights in hwa.DEFAULT_WEIGHTS.items():
            self.assertAlmostEqual(sum(weights.values()), 1.0, places=2,
                                   msg=f"{domain} weights do not sum to 1.0")

    def test_get_weights_unknown_domain(self):
        hwa = HardcodedWeightAdapter()
        weights = hwa.get_weights("nonexistent_domain")
        self.assertEqual(weights, hwa.DEFAULT_WEIGHTS["general"])

    def test_custom_weights(self):
        hwa = HardcodedWeightAdapter()
        hwa.set_weights("custom", {"count": 0.5, "overlap": 0.5, "time": 0.0, "complement": 0.0})
        weights = hwa.get_weights("custom")
        self.assertEqual(weights["count"], 0.5)


class TestBayesianWeightAdapter(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test_weights.db"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_posterior_weights_normalized(self):
        bwa = BayesianWeightAdapter(db_path=self.db_path)
        bwa.record_feedback("coding", "count", True)
        bwa.record_feedback("coding", "overlap", True)
        weights = bwa.get_posterior_weights("coding")
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=2)

    def test_uncertainty_decreases_with_feedback(self):
        bwa = BayesianWeightAdapter(db_path=self.db_path)
        init_uncertainty = bwa.get_uncertainty("coding")

        for _ in range(20):
            bwa.record_feedback("coding", "count", True)

        later_uncertainty = bwa.get_uncertainty("coding")
        self.assertLess(later_uncertainty["count"], init_uncertainty["count"])


class TestAutoSwitchWeightAdapter(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test_weights.db"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_hardcoded_when_few_samples(self):
        aswa = AutoSwitchWeightAdapter(db_path=self.db_path)
        weights = aswa.get_weights("coding")
        # 样本不足 → 硬编码权重
        self.assertEqual(weights, aswa.hardcoded.get_weights("coding"))

    def test_bayesian_when_many_samples(self):
        aswa = AutoSwitchWeightAdapter(db_path=self.db_path)
        for _ in range(60):
            aswa.record_feedback("coding", "count", True)

        status = aswa.get_status("coding")
        self.assertEqual(status["mode"], "bayesian")


if __name__ == "__main__":
    unittest.main()
