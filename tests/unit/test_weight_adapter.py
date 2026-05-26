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

    def test_bayesian_warm_when_samples_between_thresholds(self):
        """50~500条 → WARM模式（贝叶斯权重，但未校准）"""
        aswa = AutoSwitchWeightAdapter(db_path=self.db_path)
        for _ in range(60):
            aswa.record_feedback("coding", "count", True)

        status = aswa.get_status("coding")
        self.assertEqual(status["mode"], "bayesian_warm")
        self.assertFalse(status["calibrated"])

    def test_one_time_calibration_at_500_samples(self):
        """≥500条 → 触发一次性基线校准，硬编码基线被更新"""
        aswa = AutoSwitchWeightAdapter(db_path=self.db_path)
        original_baseline = aswa.hardcoded.get_weights("coding").copy()

        # 模拟500条反馈（刻意偏向count维度）
        for i in range(500):
            # 让count维度的success率明显高于其他维度
            dim = ["count", "overlap", "time", "complement"][i % 4]
            # count 80%成功，其他50%成功
            success = (dim == "count" and i % 5 != 0) or (dim != "count" and i % 2 == 0)
            aswa.record_feedback("coding", dim, success)

        # 触发get_weights，内部会检查并执行校准
        weights = aswa.get_weights("coding")

        # 验证：已校准
        status = aswa.get_status("coding")
        self.assertTrue(status["calibrated"])
        self.assertEqual(status["mode"], "bayesian_hot")

        # 验证：硬编码基线被更新了（不是原来的值）
        new_baseline = aswa.hardcoded.get_weights("coding")
        self.assertNotEqual(new_baseline, original_baseline)

        # 验证：count维度被提升了（因为我们模拟了count高成功率）
        self.assertGreater(new_baseline["count"], original_baseline["count"])

    def test_calibration_happens_only_once(self):
        """一次性校准：第二次调用get_weights不再修改基线"""
        aswa = AutoSwitchWeightAdapter(db_path=self.db_path)

        for i in range(500):
            dim = ["count", "overlap", "time", "complement"][i % 4]
            success = i % 2 == 0
            aswa.record_feedback("coding", dim, success)

        # 第一次触发校准
        aswa.get_weights("coding")
        baseline_after_first = aswa.hardcoded.get_weights("coding").copy()

        # 再录100条
        for i in range(100):
            dim = ["count", "overlap", "time", "complement"][i % 4]
            aswa.record_feedback("coding", dim, i % 2 == 0)

        # 第二次调用
        aswa.get_weights("coding")
        baseline_after_second = aswa.hardcoded.get_weights("coding")

        # 基线不应再变
        self.assertEqual(baseline_after_first, baseline_after_second)

    def test_calibration_uses_70_30_blend(self):
        """校准是平滑融合：70%贝叶斯 + 30%原始硬编码"""
        aswa = AutoSwitchWeightAdapter(db_path=self.db_path)
        original = aswa.hardcoded.get_weights("coding")

        # 制造极端反馈让贝叶斯后验严重偏离原始基线
        for _ in range(500):
            aswa.record_feedback("coding", "count", True)   # count全是成功
            aswa.record_feedback("coding", "overlap", False)  # overlap全是失败
            aswa.record_feedback("coding", "time", False)
            aswa.record_feedback("coding", "complement", False)

        aswa.get_weights("coding")
        calibrated = aswa.hardcoded.get_weights("coding")

        # 验证：即使贝叶斯后验极端，校准结果仍保留了30%原始基线
        # count原始=0.20，贝叶斯后验应该接近1.0，校准后应该在0.7~0.8之间
        self.assertGreater(calibrated["count"], original["count"])
        self.assertLess(calibrated["count"], 0.95)  # 不是完全1.0，因为有30%原始基线

        # overlap原始=0.30，贝叶斯后验应该接近0，校准后应该在0.05~0.15之间
        self.assertLess(calibrated["overlap"], original["overlap"])
        self.assertGreater(calibrated["overlap"], 0.05)  # 不是完全0


if __name__ == "__main__":
    unittest.main()
