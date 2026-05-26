"""
WeightAdapter — 权重适配器

【E14 全库修复】蒸馏评分四维权重（count/overlap/time/complement）的自适应调整。
支持三阶段演进：Hardcoded → AutoSwitch → Bayesian。
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List
import logging

logger = logging.getLogger(__name__)


class HardcodedWeightAdapter:
    """硬编码权重适配器（冷启动阶段）"""

    DEFAULT_WEIGHTS = {
        "general": {"count": 0.25, "overlap": 0.25, "time": 0.25, "complement": 0.25},
        "coding": {"count": 0.20, "overlap": 0.30, "time": 0.15, "complement": 0.35},
        "marketing": {"count": 0.30, "overlap": 0.20, "time": 0.30, "complement": 0.20},
        "analysis": {"count": 0.25, "overlap": 0.25, "time": 0.30, "complement": 0.20},
        "strategy": {"count": 0.20, "overlap": 0.20, "time": 0.20, "complement": 0.40},
        "writing": {"count": 0.35, "overlap": 0.25, "time": 0.25, "complement": 0.15},
        "review": {"count": 0.25, "overlap": 0.35, "time": 0.15, "complement": 0.25},
    }

    def __init__(self):
        self.weights = dict(self.DEFAULT_WEIGHTS)

    def get_weights(self, domain: str, context: Dict = None) -> Dict[str, float]:
        """获取指定领域的权重"""
        return self.weights.get(domain, self.DEFAULT_WEIGHTS["general"])

    def set_weights(self, domain: str, weights: Dict[str, float]):
        """手动设置领域权重"""
        self.weights[domain] = weights


class BayesianWeightAdapter:
    """贝叶斯权重适配器（热启动阶段）

    核心思想：
    - 每个维度的权重视为 Beta 分布的参数
    - 根据蒸馏结果的反馈（成功/失败）更新后验
    - 后验均值作为实际使用的权重
    """

    def __init__(self, db_path: Path = None, prior_strength: float = 10.0):
        self.db_path = db_path or Path.home() / ".mnemos" / "weight_adapter.db"
        self.prior_strength = prior_strength
        self._init_db()

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS weight_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL,
                    dimension TEXT NOT NULL,
                    success BOOLEAN NOT NULL,
                    outcome_score REAL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.commit()

    def record_feedback(self, domain: str, dimension: str,
                        success: bool, outcome_score: float = None):
        """记录某个维度在某个领域上的反馈"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                INSERT INTO weight_feedback (domain, dimension, success, outcome_score, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (domain, dimension, int(success), outcome_score,
                  datetime.now().isoformat()))
            conn.commit()

    def get_posterior_weights(self, domain: str) -> Dict[str, float]:
        """
        计算后验权重

        对每个维度，使用 Beta 分布的后验均值：
            alpha = prior_strength + successes
            beta = prior_strength + failures
            weight = alpha / (alpha + beta)
        然后归一化到和为 1.0
        """
        dimensions = ["count", "overlap", "time", "complement"]
        raw_weights = {}

        with sqlite3.connect(str(self.db_path)) as conn:
            for dim in dimensions:
                row = conn.execute("""
                    SELECT SUM(CASE WHEN success THEN 1 ELSE 0 END) as successes,
                           SUM(CASE WHEN success THEN 0 ELSE 1 END) as failures
                    FROM weight_feedback
                    WHERE domain = ? AND dimension = ?
                """, (domain, dim)).fetchone()

                successes = row[0] or 0
                failures = row[1] or 0

                alpha = self.prior_strength + successes
                beta = self.prior_strength + failures
                raw_weights[dim] = alpha / (alpha + beta)

        # 归一化
        total = sum(raw_weights.values())
        if total > 0:
            return {k: round(v / total, 4) for k, v in raw_weights.items()}
        return HardcodedWeightAdapter.DEFAULT_WEIGHTS["general"]

    def get_weights(self, domain: str, context: Dict = None) -> Dict[str, float]:
        """获取贝叶斯后验权重"""
        return self.get_posterior_weights(domain)

    def get_uncertainty(self, domain: str) -> Dict[str, float]:
        """获取各维度权重的不确定性（Beta 分布方差）"""
        dimensions = ["count", "overlap", "time", "complement"]
        uncertainties = {}

        with sqlite3.connect(str(self.db_path)) as conn:
            for dim in dimensions:
                row = conn.execute("""
                    SELECT SUM(CASE WHEN success THEN 1 ELSE 0 END) as successes,
                           SUM(CASE WHEN success THEN 0 ELSE 1 END) as failures
                    FROM weight_feedback
                    WHERE domain = ? AND dimension = ?
                """, (domain, dim)).fetchone()

                successes = row[0] or 0
                failures = row[1] or 0

                alpha = self.prior_strength + successes
                beta = self.prior_strength + failures
                # Beta 分布方差
                variance = (alpha * beta) / ((alpha + beta) ** 2 * (alpha + beta + 1))
                uncertainties[dim] = round(variance, 4)

        return uncertainties


class AutoSwitchWeightAdapter:
    """自动切换权重适配器

    根据样本量自动选择：
    - 样本少 → HardcodedWeightAdapter
    - 样本足够 → BayesianWeightAdapter
    """

    SWITCH_THRESHOLD = 50  # 每个领域最少反馈数才切换到贝叶斯

    def __init__(self, db_path: Path = None):
        self.hardcoded = HardcodedWeightAdapter()
        self.bayesian = BayesianWeightAdapter(db_path)
        self.switch_threshold = self.SWITCH_THRESHOLD

    def get_weights(self, domain: str, context: Dict = None) -> Dict[str, float]:
        """自动选择适配器获取权重"""
        sample_count = self._get_sample_count(domain)

        if sample_count < self.switch_threshold:
            logger.debug(f"[WeightAdapter] 领域 '{domain}' 样本不足 ({sample_count} < {self.switch_threshold})，使用硬编码权重")
            return self.hardcoded.get_weights(domain, context)
        else:
            logger.debug(f"[WeightAdapter] 领域 '{domain}' 样本充足 ({sample_count})，使用贝叶斯权重")
            return self.bayesian.get_weights(domain, context)

    def record_feedback(self, domain: str, dimension: str,
                        success: bool, outcome_score: float = None):
        """记录反馈（传递给贝叶斯适配器）"""
        self.bayesian.record_feedback(domain, dimension, success, outcome_score)

    def _get_sample_count(self, domain: str) -> int:
        """获取领域的反馈样本数"""
        with sqlite3.connect(str(self.bayesian.db_path)) as conn:
            row = conn.execute("""
                SELECT COUNT(*) FROM weight_feedback WHERE domain = ?
            """, (domain,)).fetchone()
            return row[0] if row else 0

    def get_status(self, domain: str) -> Dict:
        """获取适配器状态"""
        sample_count = self._get_sample_count(domain)
        return {
            "domain": domain,
            "sample_count": sample_count,
            "switch_threshold": self.switch_threshold,
            "mode": "bayesian" if sample_count >= self.switch_threshold else "hardcoded",
            "uncertainty": self.bayesian.get_uncertainty(domain) if sample_count > 0 else {},
        }


# 默认导出 AutoSwitch（推荐用法）
WeightAdapter = AutoSwitchWeightAdapter
