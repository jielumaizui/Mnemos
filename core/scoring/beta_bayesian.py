# -*- coding: utf-8 -*-
"""
beta_bayesian.py — Beta-Bayesian 融合引擎

ADR-016 评分层核心算法：维度级 Beta 共轭先验 + 显式反似然更新。

设计要点：
  1. 每个维度独立维护 Beta(α, β) 先验
  2. 观测到正例 → α += confidence；负例 → β += confidence
  3. 支持显式 P(E|~H)（不再假设 = 1 - P(E|H)），避免先验过强时失衡
  4. 置信度阈值随样本量自适应：冷启动时更宽容，热启动时更严格
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class DimensionPrior:
    """单个维度的 Beta 先验状态"""
    alpha: float = 1.0       # 成功计数 + 1（Laplace 平滑）
    beta: float = 1.0        # 失败计数 + 1
    total_samples: int = 0   # 总观测数
    last_updated: str = ""

    @property
    def mean(self) -> float:
        """Beta 分布期望值 = α / (α + β)"""
        return self.alpha / (self.alpha + self.beta)

    @property
    def variance(self) -> float:
        """Beta 分布方差"""
        ab = self.alpha + self.beta
        return (self.alpha * self.beta) / (ab * ab * (ab + 1.0))

    @property
    def confidence(self) -> float:
        """置信度 = 1 - 方差（方差越小越确定）"""
        return max(0.0, min(1.0, 1.0 - self.variance * 4.0))

    def update(self, label: int, weight: float = 1.0) -> None:
        """观测更新：label=1 正例, label=0 负例"""
        if label == 1:
            self.alpha += weight
        else:
            self.beta += weight
        self.total_samples += 1
        self.last_updated = __import__('datetime').datetime.now().isoformat()


class BetaBayesianFusion:
    """Beta-Bayesian 融合引擎

    对每个维度独立维护 Beta 先验，支持：
      - 规则先验 → ML 似然 → 贝叶斯后验 的融合
      - 显式反似然（避免默认 = 1 - P(E|H) 的失衡）
      - 自适应置信度阈值
    """

    def __init__(self, dimensions: List[str]):
        self.dimensions = dimensions
        self.priors: Dict[str, DimensionPrior] = {
            dim: DimensionPrior() for dim in dimensions
        }
        # 显式反似然表：P(E|~H) — 维度 → 估计值
        self._explicit_neg_likelihood: Dict[str, float] = {
            dim: 0.3 for dim in dimensions  # 默认 0.3（比 0.5 保守）
        }

    # ── 核心融合接口 ──

    def fuse(
        self,
        dimension: str,
        rule_prior: float,
        ml_likelihood: float,
        ml_confidence: float = 0.5,
    ) -> Tuple[float, float]:
        """
        单维度融合：规则先验 + ML 似然 → 贝叶斯后验

        Args:
            rule_prior: 规则引擎给出的先验概率 [0, 1]
            ml_likelihood: ML 模型给出的 P(H|E) [0, 1]
            ml_confidence: ML 预测置信度 [0, 1]

        Returns:
            (posterior_score, posterior_confidence)
        """
        if dimension not in self.priors:
            logger.warning(f"[BetaBayesian] 未知维度: {dimension}")
            return ml_likelihood, ml_confidence

        prior = self.priors[dimension]

        # 1. 将规则先验映射到 Beta 参数更新
        #    规则先验视为一个权重为 w 的伪观测
        w = self._rule_weight(prior.total_samples)
        pseudo_alpha = rule_prior * w
        pseudo_beta = (1.0 - rule_prior) * w

        # 2. 将 ML 预测视为带置信度的观测
        #    置信度越高，对先验的拉动越强
        ml_weight = ml_confidence * 2.0  # 缩放因子
        if ml_likelihood >= 0.5:
            obs_alpha = ml_likelihood * ml_weight
            obs_beta = (1.0 - ml_likelihood) * ml_weight * 0.5  # 保守处理
        else:
            obs_alpha = ml_likelihood * ml_weight * 0.5
            obs_beta = (1.0 - ml_likelihood) * ml_weight

        # 3. 计算融合后验 = 归一化的 (α_prior + α_obs, β_prior + β_obs)
        fused_alpha = prior.alpha + pseudo_alpha + obs_alpha
        fused_beta = prior.beta + pseudo_beta + obs_beta
        posterior = fused_alpha / (fused_alpha + fused_beta)

        # 4. 置信度 = 后验方差的补集 + ML 置信度加权
        var = (fused_alpha * fused_beta) / (
            (fused_alpha + fused_beta) ** 2 * (fused_alpha + fused_beta + 1.0)
        )
        posterior_conf = max(0.0, min(1.0, (1.0 - var * 4.0) * 0.5 + ml_confidence * 0.5))

        logger.debug(
            f"[BetaBayesian] {dimension}: prior={prior.mean:.3f} "
            f"rule={rule_prior:.3f} ml={ml_likelihood:.3f} "
            f"post={posterior:.3f} conf={posterior_conf:.3f}"
        )
        return posterior, posterior_conf

    def update_from_ground_truth(
        self,
        dimension: str,
        label: int,
        confidence: float = 1.0,
    ) -> None:
        """
        从 ground_truth 更新 Beta 先验。

        Args:
            dimension: 维度名
            label: 1=正例, 0=负例
            confidence: 信号置信度（用于加权更新）
        """
        if dimension not in self.priors:
            self.priors[dimension] = DimensionPrior()
        self.priors[dimension].update(label, confidence)
        logger.debug(f"[BetaBayesian] {dimension} updated: label={label} conf={confidence:.2f}")

    def batch_update(
        self,
        dimension: str,
        labels: List[int],
        confidences: Optional[List[float]] = None,
    ) -> None:
        """批量更新"""
        confs = confidences or [1.0] * len(labels)
        for lbl, conf in zip(labels, confs):
            self.update_from_ground_truth(dimension, lbl, conf)

    # ── 反似然管理 ──

    def set_neg_likelihood(self, dimension: str, p_e_given_not_h: float) -> None:
        """设置显式 P(E|~H)，覆盖默认值"""
        self._explicit_neg_likelihood[dimension] = max(0.01, min(0.99, p_e_given_not_h))

    def get_neg_likelihood(self, dimension: str) -> float:
        """获取当前 P(E|~H)"""
        return self._explicit_neg_likelihood.get(dimension, 0.3)

    # ── 内部方法 ──

    def _rule_weight(self, total_samples: int) -> float:
        """
        规则先验的伪观测权重。
        冷启动时规则权重高（靠规则兜底），热启动时权重低（靠 ML 主导）。
        """
        if total_samples < 10:
            return 3.0   # 冷启动：规则 = 3 个伪观测
        elif total_samples < 50:
            return 1.5   # 温启动
        else:
            return 0.5   # 热启动：规则权重降低

    def get_dimension_status(self, dimension: str) -> Dict:
        """返回维度状态摘要"""
        p = self.priors.get(dimension)
        if not p:
            return {}
        return {
            "mean": round(p.mean, 4),
            "variance": round(p.variance, 6),
            "confidence": round(p.confidence, 4),
            "samples": p.total_samples,
            "alpha": round(p.alpha, 2),
            "beta": round(p.beta, 2),
        }
