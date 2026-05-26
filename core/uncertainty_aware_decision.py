"""
UncertaintyAwareDecision — 不确定性感知决策基类

【E14 全库修复】为所有使用贝叶斯的模块提供统一的"阈值调整"框架。
uncertainty 越高 → 阈值越宽松（避免冷启动时过度严格）。
"""

import math
from typing import Dict, Optional, List
from dataclasses import dataclass
from datetime import datetime


@dataclass
class DecisionResult:
    """决策结果"""
    choice: any
    confidence: float
    effective_threshold: float
    base_threshold: float
    uncertainty: float
    reason: str


class UncertaintyAwareDecision:
    """不确定性感知决策基类

    核心公式：
        effective_threshold = base_threshold × relaxation
        relaxation = 1.0 + (max_relaxation - 1.0) × uncertainty

    uncertainty 解释：
        - 0.0：完全确定，使用 base_threshold
        - 0.5：中等不确定，阈值放宽到中间值
        - 1.0：高度不确定，阈值放宽到 max_relaxation 倍
    """

    def __init__(self, base_threshold: float = 0.5, max_relaxation: float = 1.5):
        self.base_threshold = base_threshold
        self.max_relaxation = max_relaxation
        self._history: List[Dict] = []

    def get_effective_threshold(self, uncertainty: float) -> float:
        """
        根据不确定性计算实际使用的阈值

        Args:
            uncertainty: 0.0 ~ 1.0，模型/数据的不确定性

        Returns:
            实际阈值（≥ base_threshold）
        """
        uncertainty = max(0.0, min(1.0, uncertainty))
        relaxation = 1.0 + (self.max_relaxation - 1.0) * uncertainty
        return self.base_threshold * relaxation

    def decide(self, options: list, scores: list,
               uncertainty: float = 0.0) -> DecisionResult:
        """
        基于分数和不确定性做出决策

        Args:
            options: 候选选项列表
            scores: 对应分数列表
            uncertainty: 当前不确定性

        Returns:
            DecisionResult
        """
        if not options:
            return DecisionResult(
                choice=None, confidence=0.0,
                effective_threshold=self.base_threshold,
                base_threshold=self.base_threshold,
                uncertainty=uncertainty,
                reason="No options provided"
            )

        effective_threshold = self.get_effective_threshold(uncertainty)

        # 找到最高分选项
        best_idx = max(range(len(scores)), key=lambda i: scores[i])
        best_score = scores[best_idx]
        best_option = options[best_idx]

        # 计算置信度（相对于阈值的余量）
        if effective_threshold > 0:
            confidence = min(1.0, best_score / effective_threshold)
        else:
            confidence = 1.0

        passed = best_score >= effective_threshold

        result = DecisionResult(
            choice=best_option if passed else None,
            confidence=confidence,
            effective_threshold=effective_threshold,
            base_threshold=self.base_threshold,
            uncertainty=uncertainty,
            reason=(f"Best score {best_score:.3f} >= threshold {effective_threshold:.3f}"
                    if passed else
                    f"Best score {best_score:.3f} < threshold {effective_threshold:.3f}")
        )

        self._history.append({
            "timestamp": datetime.now().isoformat(),
            "options": len(options),
            "best_score": best_score,
            "uncertainty": uncertainty,
            "effective_threshold": effective_threshold,
            "passed": passed,
        })

        return result

    def decide_binary(self, score: float, uncertainty: float = 0.0) -> DecisionResult:
        """二分类决策（通过/不通过）"""
        return self.decide(
            options=[True, False],
            scores=[score, 1.0 - score],
            uncertainty=uncertainty,
        )

    def get_history(self, limit: int = 100) -> List[Dict]:
        """获取决策历史"""
        return self._history[-limit:]

    def estimate_uncertainty_from_samples(self, sample_size: int,
                                          min_samples_for_warm: int = 50) -> float:
        """
        从样本量估计不确定性

        样本越少 → uncertainty 越高
        """
        if sample_size >= min_samples_for_warm * 2:
            return 0.0
        elif sample_size >= min_samples_for_warm:
            return 0.3
        elif sample_size >= min_samples_for_warm // 2:
            return 0.6
        else:
            return 0.9


class BayesianThresholdAdapter(UncertaintyAwareDecision):
    """贝叶斯阈值适配器：根据后验分布调整阈值"""

    def __init__(self, base_threshold: float = 0.5, max_relaxation: float = 1.5,
                 prior_strength: float = 10.0):
        super().__init__(base_threshold, max_relaxation)
        self.prior_strength = prior_strength
        self.successes = 0
        self.failures = 0

    def update_from_feedback(self, success: bool):
        """根据反馈更新先验"""
        if success:
            self.successes += 1
        else:
            self.failures += 1

    def get_posterior_uncertainty(self) -> float:
        """
        计算后验不确定性

        成功率高且样本多 → uncertainty 低
        """
        total = self.successes + self.failures
        if total == 0:
            return 1.0  # 完全不确定

        # Beta 分布的方差作为不确定性度量
        alpha = self.prior_strength + self.successes
        beta = self.prior_strength + self.failures
        variance = (alpha * beta) / ((alpha + beta) ** 2 * (alpha + beta + 1))
        return min(1.0, variance * 10)  # 放大方差使其在 0~1 范围

    def get_effective_threshold(self, uncertainty: float = None) -> float:
        """使用贝叶斯后验不确定性计算阈值"""
        if uncertainty is None:
            uncertainty = self.get_posterior_uncertainty()
        return super().get_effective_threshold(uncertainty)
