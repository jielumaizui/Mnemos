# -*- coding: utf-8 -*-
"""
bayesian_scorer.py — 统一的 Beta-Bayesian 评分器

COG-048 keeps only the stateless fusion algorithm. Historical SQLite priors
and feedback are migration inputs and cannot seed runtime state. All public
learning/state-mutation compatibility methods fail closed; governed model
effects are applied through TrainingGovernanceStore instead.
"""

from __future__ import annotations

import logging
import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, NoReturn, Optional, Tuple

logger = logging.getLogger(__name__)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ==================== 数据模型 ====================


@dataclass
class DimensionPrior:
    """单个维度的 Beta 先验状态"""

    alpha: float = 1.0  # 成功计数 + 1（Laplace 平滑）
    beta: float = 1.0  # 失败计数 + 1
    total_samples: int = 0  # 总观测数（update_from_ground_truth 调用次数）
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
        """基于方差的置信度（方差越小越确定）"""
        return max(0.0, min(1.0, 1.0 - self.variance * 4.0))

    def update(self, label: int, weight: float = 1.0) -> None:
        """观测更新：label=1 正例, label=0 负例"""
        if label == 1:
            self.alpha += weight
        else:
            self.beta += weight
        self.total_samples += 1
        self.last_updated = datetime.now().isoformat()


@dataclass
class DimensionScore:
    """单维度评分结果（旧 API 返回值）"""

    dimension: str
    score: float  # 后验均值 [0.0, 1.0]
    confidence: float  # 后验置信度 [0.0, 1.0]
    prior: float = 0.0  # 规则先验
    likelihood: Optional[float] = 0.0  # ML 似然
    alpha: float = 0.0  # 当前 Beta α
    beta: float = 0.0  # 当前 Beta β
    sample_count: int = 0  # 该维度 ground-truth 样本数


@dataclass
class BayesianScoreCard:
    """多维度评分卡（旧 API 返回值）"""

    scores: Dict[str, DimensionScore]
    timestamp: datetime = field(default_factory=datetime.now)


class BetaDimensionScorer:
    """单维度的 Beta-二项共轭评分器（独立工具类，保留给旧测试/旧 API 使用）。"""

    def __init__(self, dimension: str, alpha: float = 2.0, beta: float = 2.0):
        self.dimension = dimension
        self.alpha = alpha
        self.beta = beta
        self.prior_alpha = alpha
        self.prior_beta = beta

    def observe(self, is_positive: bool, weight: float = 1.0) -> None:
        """记录一次观测反馈"""
        if is_positive:
            self.alpha += weight
        else:
            self.beta += weight

    def observe_rule_prior(self, prior: float, weight: float = 0.3) -> None:
        """融入规则先验作为伪观测"""
        clamped = _clamp(prior)
        self.alpha += clamped * weight
        self.beta += (1.0 - clamped) * weight

    def observe_likelihood(self, likelihood: float, weight: float = 0.7) -> None:
        """融入 ML 似然作为伪观测"""
        clamped = _clamp(likelihood)
        self.alpha += clamped * weight
        self.beta += (1.0 - clamped) * weight

    def posterior_mean(self) -> float:
        """后验均值 = E[p | data]"""
        total = self.alpha + self.beta
        return self.alpha / total if total > 0 else 0.5

    def posterior_confidence(self) -> float:
        """后验置信度 = 1 - 方差（方差越小置信度越高）"""
        total = self.alpha + self.beta
        if total <= 2:
            return 0.0
        variance = (self.alpha * self.beta) / (total**2 * (total + 1))
        return max(0.0, min(1.0, 1.0 - variance * 4.0))

    def reset(self) -> None:
        """重置到先验"""
        self.alpha = self.prior_alpha
        self.beta = self.prior_beta

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension": self.dimension,
            "alpha": self.alpha,
            "beta": self.beta,
            "prior_alpha": self.prior_alpha,
            "prior_beta": self.prior_beta,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BetaDimensionScorer":
        obj = cls(d["dimension"], d.get("prior_alpha", 2.0), d.get("prior_beta", 2.0))
        obj.alpha = d.get("alpha", obj.prior_alpha)
        obj.beta = d.get("beta", obj.prior_beta)
        return obj


# ==================== BayesianScorer 主类 ====================


class BayesianScorer:
    """
    统一的贝叶斯评分器。

    ``fuse()`` is stateless and uses code-owned cold priors. Pre-cutover persistence
    arguments remain signature-compatible but never enable database access.
    """

    def __init__(
        self,
        dimensions: Optional[List[str]] = None,
        db_path: Optional[Path] = None,
        prior_alpha: float = 1.0,
        prior_beta: float = 1.0,
        neg_likelihood: float = 0.3,
        rule_weight_cold: float = 3.0,
        rule_weight_hot: float = 0.5,
        decay_half_life: float = 30.0,
        enable_lock: bool = True,
        persistent: bool = False,
    ):
        self._db_path = Path(db_path) if db_path is not None else None
        self.prior_alpha = prior_alpha
        self.prior_beta = prior_beta
        self._default_neg_likelihood = _clamp(neg_likelihood, 0.01, 0.99)
        self._rule_weight_cold = rule_weight_cold
        self._rule_weight_hot = rule_weight_hot
        self._decay_half_life = decay_half_life
        self._lock = threading.RLock() if enable_lock else None
        # Compatibility input only: COG-048 removed caller-selectable Bayesian
        # persistence. Historical rows are inventoried by migration tooling.
        self._persistent = False
        del persistent

        # V2 主路径使用的先验表
        self.priors: Dict[str, DimensionPrior] = {}
        if dimensions:
            for dim in dimensions:
                self.priors[dim] = self._fresh_prior()

        # 显式反似然表：dimension -> P(E|~H)
        self._explicit_neg_likelihood: Dict[str, float] = {}

    # ── 锁工具 ──

    @contextmanager
    def _locked(self):
        if self._lock:
            with self._lock:
                yield
        else:
            yield

    def _fresh_prior(self) -> DimensionPrior:
        return DimensionPrior(alpha=self.prior_alpha, beta=self.prior_beta)

    @staticmethod
    def _retired(operation: str) -> NoReturn:
        raise PermissionError(f"training_admission_receipt_required:{operation}")

    # ── V2 / BetaBayesianFusion 兼容接口 ──

    def fuse(
        self,
        dimension: str,
        rule_prior: float,
        ml_likelihood: float,
        ml_confidence: float = 0.5,
    ) -> Tuple[float, float]:
        """
        单维度融合：规则先验 + ML 似然 → 贝叶斯后验。

        注意：本方法是 stateless 的，不会修改持久化先验。
        """
        with self._locked():
            if dimension not in self.priors:
                logger.warning("[BayesianScorer] 未知维度: %s", dimension)
                return ml_likelihood, ml_confidence

            prior = self.priors[dimension]

            # 1. 规则先验 → 伪观测
            w = self._rule_weight(prior.total_samples)
            pseudo_alpha = rule_prior * w
            pseudo_beta = (1.0 - rule_prior) * w

            # 2. 通过显式 P(E|~H) 计算证据后验
            neg_likelihood = self.get_neg_likelihood(dimension)
            p_h = _clamp(rule_prior, 0.01, 0.99)
            p_e_given_h = _clamp(ml_likelihood, 0.01, 0.99)
            p_e_given_not_h = _clamp(neg_likelihood, 0.01, 0.99)
            evidence = p_e_given_h * p_h + p_e_given_not_h * (1.0 - p_h)
            evidence_posterior = (p_e_given_h * p_h) / evidence if evidence > 1e-12 else p_h
            evidence_posterior = _clamp(evidence_posterior)

            # 3. 证据后验 → 带置信度的观测（非对称 tail down-weight）
            ml_weight = ml_confidence * 2.0
            if evidence_posterior >= 0.5:
                obs_alpha = evidence_posterior * ml_weight
                obs_beta = (1.0 - evidence_posterior) * ml_weight * 0.5
            else:
                obs_alpha = evidence_posterior * ml_weight * 0.5
                obs_beta = (1.0 - evidence_posterior) * ml_weight

            # 4. 融合后验
            fused_alpha = prior.alpha + pseudo_alpha + obs_alpha
            fused_beta = prior.beta + pseudo_beta + obs_beta
            posterior = fused_alpha / (fused_alpha + fused_beta)

            # 5. 置信度
            var = (fused_alpha * fused_beta) / (
                (fused_alpha + fused_beta) ** 2 * (fused_alpha + fused_beta + 1.0)
            )
            posterior_conf = _clamp((1.0 - var * 4.0) * 0.5 + ml_confidence * 0.5)

            logger.debug(
                "[BayesianScorer] %s: prior_mean=%.3f rule=%.3f ml=%.3f neg=%.3f "
                "evidence_post=%.3f post=%.3f conf=%.3f",
                dimension,
                prior.mean,
                rule_prior,
                ml_likelihood,
                neg_likelihood,
                evidence_posterior,
                posterior,
                posterior_conf,
            )
            return posterior, posterior_conf

    def update_from_ground_truth(
        self,
        dimension: str,
        label: int,
        confidence: float = 1.0,
        source_refs: Tuple[Tuple[str, str], ...] = (),
    ) -> None:
        """Reject caller-provided labels outside canonical admission."""

        del dimension, label, confidence, source_refs
        self._retired("bayesian_update_from_ground_truth")

    def batch_update(
        self,
        dimension: str,
        labels: List[int],
        confidences: Optional[List[float]] = None,
        source_refs: Tuple[Tuple[str, str], ...] = (),
    ) -> None:
        """Reject caller-provided label batches outside canonical admission."""

        del dimension, labels, confidences, source_refs
        self._retired("bayesian_batch_update")

    def set_neg_likelihood(self, dimension: str, p_e_given_not_h: float) -> None:
        """Reject runtime prior tuning outside a governed run."""

        del dimension, p_e_given_not_h
        self._retired("bayesian_set_neg_likelihood")

    def get_neg_likelihood(self, dimension: str) -> float:
        """获取当前 P(E|~H)。"""
        return self._explicit_neg_likelihood.get(dimension, self._default_neg_likelihood)

    def get_dimension_status(self, dimension: str) -> Dict[str, Any]:
        """返回维度状态摘要。"""
        with self._locked():
            prior = self.priors.get(dimension)
            if not prior:
                return {}
            return {
                "mean": round(prior.mean, 4),
                "variance": round(prior.variance, 6),
                "confidence": round(prior.confidence, 4),
                "samples": prior.total_samples,
                "alpha": round(prior.alpha, 2),
                "beta": round(prior.beta, 2),
            }

    def _rule_weight(self, total_samples: int) -> float:
        """
        规则先验的伪观测权重。
        冷启动时规则权重高，热启动时权重低，使用指数衰减避免阈值处突变。
        """
        if total_samples <= 0:
            return self._rule_weight_cold
        span = self._rule_weight_cold - self._rule_weight_hot
        weight = self._rule_weight_hot + span * math.exp(-total_samples / self._decay_half_life)
        return round(weight, 2)

    # ── 状态导出/恢复（供 AdaptiveScorerV2 模型快照使用） ──

    def state_to_dict(self) -> Dict[str, Dict[str, Any]]:
        """导出所有先验状态为可 JSON 序列化的字典。"""
        with self._locked():
            state: Dict[str, Dict[str, Any]] = {}
            for dim, prior in self.priors.items():
                state[dim] = {
                    "alpha": prior.alpha,
                    "beta": prior.beta,
                    "total_samples": prior.total_samples,
                    "last_updated": prior.last_updated,
                    "neg_likelihood": self.get_neg_likelihood(dim),
                }
            return state

    def restore_state(self, state: Dict[str, Dict[str, Any]]) -> None:
        """Reject loading caller-created or historical prior snapshots."""

        del state
        self._retired("bayesian_restore_state")

    # ── 旧 API（保留给非 V2 调用方） ──

    def score(
        self,
        dimension: str,
        rule_prior: float,
        ml_likelihood: Optional[float] = None,
    ) -> DimensionScore:
        """单维度评分（stateless，不修改先验）。"""
        with self._locked():
            if dimension not in self.priors:
                self.priors[dimension] = self._fresh_prior()
            ml = ml_likelihood if ml_likelihood is not None else 0.5
            post, conf = self.fuse(dimension, rule_prior, ml, ml_confidence=0.5)
            prior = self.priors[dimension]
            return DimensionScore(
                dimension=dimension,
                score=round(post, 4),
                confidence=round(conf, 4),
                prior=round(rule_prior, 4),
                likelihood=round(ml_likelihood, 4) if ml_likelihood is not None else None,
                alpha=round(prior.alpha, 2),
                beta=round(prior.beta, 2),
                sample_count=prior.total_samples,
            )

    def score_multi(
        self,
        dimensions: List[str],
        rule_priors: Dict[str, float],
        ml_likelihoods: Optional[Dict[str, float]] = None,
    ) -> BayesianScoreCard:
        """多维度批量评分。"""
        ml = ml_likelihoods or {}
        scores = {}
        for dim in dimensions:
            scores[dim] = self.score(dim, rule_priors.get(dim, 0.5), ml.get(dim))
        return BayesianScoreCard(scores=scores)

    def feedback(
        self,
        dimension: str,
        is_positive: bool,
        weight: float = 1.0,
        context: Optional[Dict[str, Any]] = None,
        subject_provenance: Mapping[str, Any] | None = None,
    ) -> None:
        """Reject raw feedback as Bayesian ground truth."""

        del dimension, is_positive, weight, context, subject_provenance
        self._retired("bayesian_feedback")

    def reset_dimension(self, dimension: str) -> None:
        """Reject direct state mutation; governed effects own lifecycle."""

        del dimension
        self._retired("bayesian_reset_dimension")

    def list_dimensions(self) -> List[str]:
        """列出所有已初始化的维度。"""
        with self._locked():
            return list(self.priors.keys())
