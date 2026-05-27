"""
quality_filter 单元测试

覆盖项：
- QualityFilter 基本过滤流程
- 硬门槛淘汰
- 贝叶斯校准（如有）
- 决策解释
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest
from core.quality_filter import QualityFilter


class TestQualityFilter(unittest.TestCase):
    def setUp(self):
        self.filter = QualityFilter(use_bayesian=False)

    def test_high_quality_content_passes(self):
        """高质量内容通过过滤"""
        content = (
            "Python asyncio 生产环境最佳实践。\n\n"
            "步骤1：安装 uvloop 替换默认事件循环。\n"
            "步骤2：配置 `asyncio.get_event_loop().set_debug(True)`。\n"
            "步骤3：使用 `asyncio.gather()` 并发处理 I/O。\n\n"
            "关键实体：asyncio, uvloop, aiohttp。\n"
            "```bash\npip install uvloop aiohttp\n```\n"
        )
        decision = self.filter.filter(content)
        self.assertTrue(decision.passed)
        self.assertGreater(decision.score, 0.5)

    def test_noise_content_fails(self):
        """噪音内容被淘汰"""
        content = "ok ok ok thanks yes good"
        decision = self.filter.filter(content)
        self.assertFalse(decision.passed)

    def test_empty_content_fails(self):
        """空内容被淘汰"""
        decision = self.filter.filter("")
        self.assertFalse(decision.passed)

    def test_decision_contains_dimension_scores(self):
        """决策包含维度评分详情"""
        content = "Implementing a Redis cluster with Sentinel for high availability"
        decision = self.filter.filter(content)
        self.assertIn("dimension_scores", dir(decision))
        self.assertGreater(len(decision.dimension_scores), 0)

    def test_reason_is_human_readable(self):
        """决策理由是可读的"""
        content = "Detailed technical discussion about Kubernetes operators"
        decision = self.filter.filter(content)
        self.assertIn("评分", decision.reason)
        self.assertIn("置信度", decision.reason)

    def test_with_bayesian(self):
        """启用贝叶斯评分器"""
        tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(tmpdir.name) / "test_quality.db"
        qf = QualityFilter(use_bayesian=True)
        qf.bayesian_scorer.db_path = db_path
        decision = qf.filter("Python asyncio event loop deep dive")
        self.assertIsNotNone(decision.score)
        tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()
