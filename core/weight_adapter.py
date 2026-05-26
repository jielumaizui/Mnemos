"""
WeightAdapter — 权重适配器

蒸馏评分四维权重（count/overlap/time/complement）的自适应调整。

四阶段生命周期（用户要求的折中设计）：
  COLD   (0~50条):   纯硬编码基线
  WARM   (50~500条): 贝叶斯调阈值松紧（保守路线）
  CALIBRATE (≥500条): 一次性基线校准 → 贝叶斯后验写回硬编码（只执行一次）
  HOT    (≥500条):   保守路线，但基线已被数据校准过

核心原则：贝叶斯可以修正硬编码基线，但只修正一次。
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

    三阶段生命周期：
    1. COLD（0~50条）: 纯硬编码
    2. WARM（50~500条）: 贝叶斯调阈值松紧（保守路线）
    3. CALIBRATE（≥500条且未校准）: 一次性基线校准 → 贝叶斯后验写回硬编码
    4. HOT（≥500条且已校准）: 保守路线，但基线已被数据校准过

    核心原则：贝叶斯可以修正硬编码基线，但只修正一次。
    """

    SWITCH_THRESHOLD = 50      # COLD → WARM
    CALIBRATE_THRESHOLD = 500  # WARM → CALIBRATE（一次性基线校准）

    def __init__(self, db_path: Path = None):
        self.hardcoded = HardcodedWeightAdapter()
        self.bayesian = BayesianWeightAdapter(db_path)
        self.switch_threshold = self.SWITCH_THRESHOLD
        self.calibrate_threshold = self.CALIBRATE_THRESHOLD
        self._calibrated_domains: set = set()  # 已校准的领域（内存标记，重启后重新评估）

    def get_weights(self, domain: str, context: Dict = None) -> Dict[str, float]:
        """自动选择适配器获取权重，触发一次性校准"""
        sample_count = self._get_sample_count(domain)

        # 阶段3: 一次性基线校准（≥500条且未校准过）
        if sample_count >= self.calibrate_threshold and domain not in self._calibrated_domains:
            self._calibrate_hardcoded(domain, sample_count)
            self._calibrated_domains.add(domain)

        # 阶段1: COLD → 纯硬编码
        if sample_count < self.switch_threshold:
            logger.debug(
                f"[WeightAdapter] 领域 '{domain}' 样本不足 ({sample_count} < {self.switch_threshold})，"
                f"使用硬编码权重"
            )
            return self.hardcoded.get_weights(domain, context)

        # 阶段2/4: WARM/HOT → 贝叶斯后验权重（含阈值松紧逻辑由调用方处理）
        logger.debug(
            f"[WeightAdapter] 领域 '{domain}' 样本充足 ({sample_count})，"
            f"使用贝叶斯权重{'（基线已校准）' if domain in self._calibrated_domains else ''}"
        )
        return self.bayesian.get_weights(domain, context)

    def _calibrate_hardcoded(self, domain: str, sample_count: int):
        """
        一次性基线校准：将贝叶斯后验均值写回硬编码基线。

        触发条件：样本数 ≥ CALIBRATE_THRESHOLD 且该领域从未校准过。
        行为：
        1. 计算当前贝叶斯后验权重
        2. 与硬编码基线做平滑融合（70%贝叶斯 + 30%硬编码，防止过拟合）
        3. 写回 hardcoded.weights[domain]
        4. 记录日志
        """
        posterior = self.bayesian.get_posterior_weights(domain)
        baseline = self.hardcoded.get_weights(domain)

        # 平滑融合：不完全替换，保留30%原始基线防止过拟合
        calibrated = {
            dim: round(0.7 * posterior.get(dim, baseline[dim]) + 0.3 * baseline[dim], 4)
            for dim in baseline
        }

        self.hardcoded.set_weights(domain, calibrated)
        logger.info(
            f"[WeightAdapter] 领域 '{domain}' 一次性基线校准完成（样本={sample_count}）。"
            f"硬编码基线已更新: {baseline} → {calibrated}"
        )

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
        calibrated = domain in self._calibrated_domains
        mode = "hardcoded"
        if sample_count >= self.calibrate_threshold:
            mode = "bayesian_hot" if calibrated else "bayesian_warm"
        elif sample_count >= self.switch_threshold:
            mode = "bayesian_warm"

        return {
            "domain": domain,
            "sample_count": sample_count,
            "switch_threshold": self.switch_threshold,
            "calibrate_threshold": self.calibrate_threshold,
            "mode": mode,
            "calibrated": calibrated,
            "uncertainty": self.bayesian.get_uncertainty(domain) if sample_count > 0 else {},
        }


# 默认导出 AutoSwitch（推荐用法）
WeightAdapter = AutoSwitchWeightAdapter
