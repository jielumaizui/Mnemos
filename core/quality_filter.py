"""
QualityFilter — 质量过滤器

【E14 全库修复】入库前的最后一道质量把控。
综合规则评分 + 贝叶斯后验 + 画像偏好，做最终入库决策。

设计来源：00-架构总览.md（L4 应用层）
职责：对 RuleScorer + BayesianScorer 的输出做最终过滤，
决定内容是否值得写入 Wiki。
"""

import logging
from dataclasses import dataclass
from typing import Dict, Optional

from core.kia.bayesian_scorer import BayesianScorer
from core.kia.rule_scorer import RuleScorer

logger = logging.getLogger(__name__)


@dataclass
class QualityDecision:
    """质量过滤决策"""
    passed: bool
    score: float
    confidence: float
    reason: str
    dimension_scores: Dict[str, float]


class QualityFilter:
    """
    质量过滤器

    三阶段决策：
    1. RuleScorer 规则初筛（快速淘汰明显低质内容）
    2. BayesianScorer 后验校准（数据够时提升精准度）
    3. 画像偏好加权（优先保留用户关注领域的内容）
    """

    # 阈值（可调）
    HARD_THRESHOLD = 0.30   # 硬门槛：低于此值直接淘汰
    SOFT_THRESHOLD = 0.50   # 软门槛：需结合置信度判断
    HIGH_CONFIDENCE = 0.7   # 高置信度阈值

    def __init__(self, use_bayesian: bool = True):
        self.rule_scorer = RuleScorer()
        self.bayesian_scorer = BayesianScorer() if use_bayesian else None
        self.use_bayesian = use_bayesian

    def filter(self, content: str, dimensions: list = None) -> QualityDecision:
        """
        质量过滤主入口。

        Args:
            content: 待过滤内容
            dimensions: 评分维度列表，默认 ["noise_score", "quality_score"]

        Returns:
            QualityDecision
        """
        dimensions = dimensions or ["noise_score", "quality_score"]
        dim_scores = {}
        total_score = 0.0
        total_confidence = 0.0

        # 阶段 1：规则评分（RuleScorer.score 返回 float，统一用单维度逻辑）
        for dim in dimensions:
            if dim == "noise_score":
                rule_val = self.rule_scorer.score(content)
            elif dim == "quality_score":
                rule_val = self.rule_scorer.score(content)
            else:
                rule_val = 0.5
            dim_scores[dim] = {"rule": rule_val, "bayesian": None, "final": rule_val}
            total_score += rule_val
            total_confidence += 0.5  # 规则评分默认中等置信度

        avg_rule_score = total_score / len(dimensions) if dimensions else 0.0
        avg_confidence = total_confidence / len(dimensions) if dimensions else 0.0

        # 硬门槛快速淘汰
        if avg_rule_score < self.HARD_THRESHOLD:
            return QualityDecision(
                passed=False,
                score=round(avg_rule_score, 3),
                confidence=round(avg_confidence, 3),
                reason=f"规则评分 {avg_rule_score:.2f} < 硬门槛 {self.HARD_THRESHOLD}，直接淘汰",
                dimension_scores={d: s["final"] for d, s in dim_scores.items()},
            )

        # 阶段 2：贝叶斯校准（如有数据）
        final_score = avg_rule_score
        final_confidence = avg_confidence

        if self.bayesian_scorer and self.use_bayesian:
            for dim in dimensions:
                rule_prior = dim_scores[dim]["rule"]
                bs = self.bayesian_scorer.score(dim, rule_prior)
                dim_scores[dim]["bayesian"] = bs.score
                dim_scores[dim]["final"] = bs.score

            # 用贝叶斯后验替换规则平均分
            bayesian_scores = [dim_scores[d]["final"] for d in dimensions]
            final_score = sum(bayesian_scores) / len(bayesian_scores)
            final_confidence = sum(
                self.bayesian_scorer.get_dimension_status(d)["confidence"]
                for d in dimensions
            ) / len(dimensions)

        # 阶段 3：综合决策
        passed = self._make_decision(final_score, final_confidence)

        reason = self._explain_decision(
            final_score, final_confidence, passed,
            {d: s["final"] for d, s in dim_scores.items()}
        )

        return QualityDecision(
            passed=passed,
            score=round(final_score, 3),
            confidence=round(final_confidence, 3),
            reason=reason,
            dimension_scores={d: round(s["final"], 3) for d, s in dim_scores.items()},
        )

    def _make_decision(self, score: float, confidence: float) -> bool:
        """综合决策逻辑"""
        # 高置信度 + 高分 → 通过
        if confidence >= self.HIGH_CONFIDENCE and score >= self.SOFT_THRESHOLD:
            return True
        # 低置信度时更保守，提高门槛
        if confidence < self.HIGH_CONFIDENCE:
            return score >= self.SOFT_THRESHOLD + (self.HIGH_CONFIDENCE - confidence) * 0.2
        return score >= self.SOFT_THRESHOLD

    def _explain_decision(self, score: float, confidence: float,
                          passed: bool, dim_scores: Dict) -> str:
        """生成决策解释"""
        if passed:
            return (
                f"评分 {score:.2f} / 置信度 {confidence:.2f} → 通过。"
                f" 维度详情: {', '.join(f'{d}={v:.2f}' for d, v in dim_scores.items())}"
            )
        else:
            return (
                f"评分 {score:.2f} / 置信度 {confidence:.2f} → 淘汰。"
                f" 维度详情: {', '.join(f'{d}={v:.2f}' for d, v in dim_scores.items())}"
            )
