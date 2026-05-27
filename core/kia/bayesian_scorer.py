"""
BayesianScorer — 贝叶斯评分器

【E14 全库修复】基于 Beta-二项共轭先验的轻量级评分器。
无需 sklearn，纯 Python，O(1) 更新，树莓派友好。

设计来源：ADR-016 §2.2 Beta-二项共轭先验修复方案
数学：p ~ Beta(α, β)，观测后 α += success，β += failure
后验均值 = α / (α + β)，作为最终评分。
"""

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from math import log, exp
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


# ==================== 数据模型 ====================

@dataclass
class DimensionScore:
    """单维度评分结果"""
    dimension: str
    score: float              # 后验均值 [0.0, 1.0]
    confidence: float         # 后验置信度 [0.0, 1.0]
    prior: float = 0.0        # 规则先验
    likelihood: float = 0.0   # ML 似然
    alpha: float = 0.0        # Beta α
    beta: float = 0.0         # Beta β
    sample_count: int = 0     # 该维度样本数


@dataclass
class BayesianScoreCard:
    """贝叶斯评分卡"""
    scores: Dict[str, DimensionScore]
    timestamp: datetime = field(default_factory=datetime.now)


# ==================== Beta 共轭评分器 ====================

class BetaDimensionScorer:
    """单维度的 Beta-二项共轭评分器"""

    def __init__(self, dimension: str, alpha: float = 2.0, beta: float = 2.0):
        self.dimension = dimension
        self.alpha = alpha
        self.beta = beta
        self.prior_alpha = alpha
        self.prior_beta = beta

    def observe(self, is_positive: bool, weight: float = 1.0):
        """记录一次观测反馈"""
        if is_positive:
            self.alpha += weight
        else:
            self.beta += weight

    def observe_rule_prior(self, prior: float, weight: float = 0.3):
        """融入规则先验作为伪观测"""
        self.alpha += prior * weight
        self.beta += (1.0 - prior) * weight

    def observe_likelihood(self, likelihood: float, weight: float = 0.7):
        """融入 ML 似然作为伪观测"""
        self.alpha += likelihood * weight
        self.beta += (1.0 - likelihood) * weight

    def posterior_mean(self) -> float:
        """后验均值 = E[p | data]"""
        total = self.alpha + self.beta
        return self.alpha / total if total > 0 else 0.5

    def posterior_confidence(self) -> float:
        """后验置信度 = 1 - 方差（方差越小置信度越高）"""
        total = self.alpha + self.beta
        if total <= 4:
            return 0.0
        variance = (self.alpha * self.beta) / (total ** 2 * (total + 1))
        return max(0.0, min(1.0, 1.0 - variance * 10))  # 放大方差使其在 0~1 范围

    def reset(self):
        """重置到先验"""
        self.alpha = self.prior_alpha
        self.beta = self.prior_beta

    def to_dict(self) -> Dict:
        return {
            "dimension": self.dimension,
            "alpha": self.alpha,
            "beta": self.beta,
            "prior_alpha": self.prior_alpha,
            "prior_beta": self.prior_beta,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "BetaDimensionScorer":
        obj = cls(d["dimension"], d.get("prior_alpha", 2.0), d.get("prior_beta", 2.0))
        obj.alpha = d.get("alpha", obj.prior_alpha)
        obj.beta = d.get("beta", obj.prior_beta)
        return obj


# ==================== BayesianScorer 主类 ====================

class BayesianScorer:
    """
    贝叶斯评分器（热启动阶段）。

    与 V1 RuleScorer 的关系：
    - 冷启动：RuleScorer 给出先验，BayesianScorer 不启用
    - 数据积累后：BayesianScorer 融合规则先验 + 历史反馈，输出后验评分
    - V2 阶段：BayesianScorer 主导，RuleScorer 作为 sanity check
    """

    def __init__(self, db_path: Path = None,
                 prior_alpha: float = 2.0, prior_beta: float = 2.0):
        self._db_path = db_path or Path.home() / ".mnemos" / "bayesian_scorer.db"
        self.prior_alpha = prior_alpha
        self.prior_beta = prior_beta
        self._scorers: Dict[str, BetaDimensionScorer] = {}
        self._init_db()
        self._load_scorers()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @db_path.setter
    def db_path(self, value: Path):
        self._db_path = value
        self._init_db()
        self._load_scorers()

    def _init_db(self):
        """初始化 SQLite 持久化"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bayesian_scorer_state (
                    dimension TEXT PRIMARY KEY,
                    alpha REAL NOT NULL,
                    beta REAL NOT NULL,
                    prior_alpha REAL NOT NULL,
                    prior_beta REAL NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bayesian_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dimension TEXT NOT NULL,
                    is_positive INTEGER NOT NULL,
                    weight REAL DEFAULT 1.0,
                    context_json TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.commit()

    def _load_scorers(self):
        """从数据库加载 scorer 状态"""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                rows = conn.execute(
                    "SELECT dimension, alpha, beta, prior_alpha, prior_beta FROM bayesian_scorer_state"
                ).fetchall()
                for dim, alpha, beta, pa, pb in rows:
                    scorer = BetaDimensionScorer(dim, pa, pb)
                    scorer.alpha = alpha
                    scorer.beta = beta
                    self._scorers[dim] = scorer
        except Exception as e:
            logger.warning(f"加载贝叶斯 scorer 状态失败: {e}")

    def _save_scorer(self, dimension: str):
        """保存 scorer 状态到数据库"""
        scorer = self._scorers.get(dimension)
        if not scorer:
            return
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO bayesian_scorer_state
                    (dimension, alpha, beta, prior_alpha, prior_beta, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (dimension, scorer.alpha, scorer.beta,
                  scorer.prior_alpha, scorer.prior_beta,
                  datetime.now().isoformat()))
            conn.commit()

    # ── 核心评分接口 ──

    def score(self, dimension: str, rule_prior: float,
              ml_likelihood: float = None) -> DimensionScore:
        """
        单维度贝叶斯评分。

        流程：
        1. 获取/创建维度 scorer
        2. 融入规则先验（30% 权重）
        3. 如有 ML 似然，融入（70% 权重）
        4. 返回后验均值 + 置信度

        Args:
            dimension: 评分维度，如 "quality_score" / "distill_score"
            rule_prior: 规则给出的先验评分 [0.0, 1.0]
            ml_likelihood: ML 模型给出的似然 [0.0, 1.0]，None 则只使用规则

        Returns:
            DimensionScore
        """
        scorer = self._get_or_create_scorer(dimension)

        # 融入规则先验
        scorer.observe_rule_prior(rule_prior, weight=0.3)

        # 融入 ML 似然（如有）
        if ml_likelihood is not None:
            scorer.observe_likelihood(ml_likelihood, weight=0.7)

        result = DimensionScore(
            dimension=dimension,
            score=round(scorer.posterior_mean(), 4),
            confidence=round(scorer.posterior_confidence(), 4),
            prior=round(rule_prior, 4),
            likelihood=round(ml_likelihood, 4) if ml_likelihood is not None else None,
            alpha=round(scorer.alpha, 2),
            beta=round(scorer.beta, 2),
            sample_count=int(scorer.alpha + scorer.beta - scorer.prior_alpha - scorer.prior_beta),
        )

        # 保存状态
        self._save_scorer(dimension)

        return result

    def score_multi(self, dimensions: List[str],
                    rule_priors: Dict[str, float],
                    ml_likelihoods: Dict[str, float] = None) -> BayesianScoreCard:
        """多维度批量评分"""
        ml = ml_likelihoods or {}
        scores = {}
        for dim in dimensions:
            scores[dim] = self.score(dim, rule_priors.get(dim, 0.5), ml.get(dim))
        return BayesianScoreCard(scores=scores)

    # ── 反馈接口 ──

    def feedback(self, dimension: str, is_positive: bool,
                 weight: float = 1.0, context: Dict = None):
        """
        接收外部真实反馈，更新后验。

        这是贝叶斯学习的核心入口：每次观测（成功/失败）直接更新 α/β。
        """
        scorer = self._get_or_create_scorer(dimension)
        scorer.observe(is_positive, weight)

        # 持久化反馈记录
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                INSERT INTO bayesian_feedback (dimension, is_positive, weight, context_json, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (dimension, int(is_positive), weight,
                  json.dumps(context or {}, ensure_ascii=False, default=str),
                  datetime.now().isoformat()))
            conn.commit()

        self._save_scorer(dimension)
        logger.debug(f"[BayesianScorer] {dimension} 反馈: {'+' if is_positive else '-'} (weight={weight})")

    # ── 辅助方法 ──

    def _get_or_create_scorer(self, dimension: str) -> BetaDimensionScorer:
        if dimension not in self._scorers:
            self._scorers[dimension] = BetaDimensionScorer(
                dimension, self.prior_alpha, self.prior_beta
            )
        return self._scorers[dimension]

    def get_dimension_status(self, dimension: str) -> Dict:
        """获取维度的学习状态（未初始化视为 cold）"""
        scorer = self._scorers.get(dimension)
        if not scorer:
            return {
                "dimension": dimension,
                "status": "cold",
                "sample_count": 0,
                "posterior_mean": 0.5,
                "confidence": 0.0,
                "alpha": 0.0,
                "beta": 0.0,
            }
        total = scorer.alpha + scorer.beta
        samples = total - scorer.prior_alpha - scorer.prior_beta
        return {
            "dimension": dimension,
            "sample_count": int(samples),
            "posterior_mean": round(scorer.posterior_mean(), 4),
            "confidence": round(scorer.posterior_confidence(), 4),
            "alpha": round(scorer.alpha, 2),
            "beta": round(scorer.beta, 2),
            "status": "hot" if samples >= 50 else "warm" if samples >= 10 else "cold",
        }

    def reset_dimension(self, dimension: str):
        """重置某个维度的学习到先验"""
        scorer = self._scorers.get(dimension)
        if scorer:
            scorer.reset()
            self._save_scorer(dimension)

    def list_dimensions(self) -> List[str]:
        """列出所有已学习的维度"""
        return list(self._scorers.keys())
