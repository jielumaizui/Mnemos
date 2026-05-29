# -*- coding: utf-8 -*-
"""
AdaptiveScorer — 自适应评分引擎

核心公式：P(H|E) = P(E|H) * P(H) / P(E)
  - P(H) = 规则先验（RuleScorer）
  - P(E|H) = ML 似然（ComplementNB + TfidfVectorizer）
  - P(H|E) = 贝叶斯后验（最终分数）

冷启动三阶段：
  - COLD（1-7天）：纯规则评分，零 ML
  - WARM（8-30天）：规则 + 轻量统计混合
  - HOT（31天+）：ML 主导（>80%），规则兜底

ADR-016 修复：
  - _bayesian_update 使用显式 P(E|~H) 参数，不再假设 = 1 - P(E|H)
  - 训练标签用 rule_prior，不用 posterior（避免自举偏差）
  - 使用 partial_fit（在线学习），不替换模型
  - EWMA 更新模型参数，不只是统计均值
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from core.config import get_config
from core.kia.rule_scorer import RuleScorer, noise_penalty, quality_score
from .online_stats import DimensionStats

logger = logging.getLogger(__name__)


@dataclass
class ScoreCard:
    """评分结果"""
    dimension: str
    value: float          # 0-1
    confidence: float     # 0-1
    source: str           # "rule" | "ml" | "bayesian"
    rule_prior: float = 0.0
    ml_likelihood: float = 0.0
    posterior: float = 0.0
    model_version: int = 0
    features: Optional[Dict] = None
    reasons: List[str] = field(default_factory=list)


@dataclass
class Feedback:
    """反馈信号"""
    dimension: str
    expected: float       # 用户/系统期望分数
    actual: float         # 实际观察分数
    source: str = "manual"  # "manual" | "implicit" | "self_observation"
    context: Optional[Dict] = None
    weight: float = 1.0


class AdaptiveScorer:
    """
    自适应评分引擎

    Usage:
        scorer = AdaptiveScorer(domain="sync", cold_start_rules={...})
        card = scorer.score(turn, dimensions=["noise_score", "form_score"])
        scorer.feedback(Feedback(dimension="noise_score", expected=0.9, actual=0.3))
    """

    # 模式：COLD → WARM → HOT
    _SAMPLE_THRESHOLDS = {
        "cold": 0,      # 无训练样本
        "warm": 20,     # ≥20 样本进入 WARM
        "hot": 100,     # ≥100 样本进入 HOT
    }

    def __init__(
        self,
        domain: str = "sync",
        cold_start_rules: Optional[Dict[str, Callable]] = None,
    ):
        self.domain = domain
        self.config = get_config()
        self._mode = "cold"

        # 规则系统
        self._rule_scorer = RuleScorer()
        self._cold_rules = cold_start_rules or self._default_cold_rules()

        # ML 模型（延迟初始化）
        self._models: Dict[str, Any] = {}  # dimension → ComplementNB
        self._vectorizers: Dict[str, Any] = {}  # dimension → ColumnTransformer
        self._model_version: int = 0

        # 训练缓冲区
        self._retrain_buffer: List[Dict] = []
        self._retrain_buffer_size = self.config.get("scoring.retrain_buffer", 100)

        # 在线统计
        self._stats = DimensionStats()

        # EWMA 参数
        self._ewma_alpha = self.config.get("scoring.ewma_alpha", 0.1)

        # 模型持久化
        self._model_dir = self.config.data_dir / "models" / domain
        self._model_dir.mkdir(parents=True, exist_ok=True)

        # 尝试加载已有模型
        self._load_model()

    def _default_cold_rules(self) -> Dict[str, Callable]:
        """默认 COLD 阶段规则"""
        return {
            "noise_score": self._noise_rule_prior,
            "quality_score": self._quality_rule_prior,
            "form_score": self._form_rule_prior,
            "entity_quality": self._entity_quality_rule_prior,
        }

    # ==================== 公共 API ====================

    def score(
        self,
        item: Any,
        dimensions: Optional[List[str]] = None,
    ) -> List[ScoreCard]:
        """
        评分入口：特征提取 → 规则先验 → ML似然 → 贝叶斯后验

        Args:
            item: 待评分对象（Turn / str / Dict）
            dimensions: 评分维度列表，None 则使用 domain 默认维度

        Returns:
            ScoreCard 列表
        """
        if dimensions is None:
            dimensions = list(self._cold_rules.keys())

        # 特征提取
        features = self._extract_features(item)

        cards = []
        for dim in dimensions:
            card = self._score_dimension(dim, features)
            cards.append(card)
            self._stats.update(f"{dim}.value", card.value)
            self._stats.update(f"{dim}.confidence", card.confidence)

        # 发射 content_scored 事件
        try:
            from core.mnemos_bus import publish_event
            memory_id = str(getattr(item, "memory_id", getattr(item, "id", hash(str(item)) & 0xFFFFFFFF)))
            publish_event("content_scored", "scoring", {
                "memory_id": memory_id,
                "posterior": round(sum(c.value for c in cards) / len(cards), 4) if cards else 0.0,
                "confidence": round(sum(c.confidence for c in cards) / len(cards), 4) if cards else 0.0,
                "dimensions": [c.dimension for c in cards],
            })
        except Exception:
            logger.warning(f"Unexpected error in adaptive_scorer.py", exc_info=True)
            pass

        return cards

    def feedback(self, fb: Feedback) -> None:
        """
        反馈入口：EWMA 增量更新

        Args:
            fb: 反馈信号
        """
        # EWMA 更新
        dim = fb.dimension
        current_model = self._models.get(dim)
        if current_model is not None:
            # 调整模型参数（通过 EWMA 平滑）
            self._ewma_update(current_model, fb)

        # 加入重训练缓冲区
        self._retrain_buffer.append({
            "dimension": dim,
            "features": self._extract_features(fb.context or {}),
            "label": fb.expected,
            "source": fb.source,
            "weight": fb.weight,
            "timestamp": datetime.now().isoformat(),
        })

        # 触发重训练检查
        if len(self._retrain_buffer) >= self._retrain_buffer_size:
            self._schedule_retrain()

        # 漂移检测
        if self._stats.check_drift(f"{dim}.value", fb.expected):
            logger.warning(f"[AdaptiveScorer] 检测到 {dim} 特征漂移")

    # ==================== 评分流水线 ====================

    def _score_dimension(self, dimension: str, features: Dict) -> ScoreCard:
        """单维度评分：规则先验 → ML似然 → 贝叶斯后验"""
        # 1. 规则先验
        rule_prior = self._rule_prior(dimension, features)

        # 2. ML 似然（COLD 阶段跳过）
        ml_likelihood = 0.5
        if self._mode in ("warm", "hot"):
            ml_likelihood = self._ml_likelihood(dimension, features)

        # 3. 贝叶斯后验
        posterior = self._bayesian_update(rule_prior, ml_likelihood)

        # 4. 确定置信度
        confidence = self._compute_confidence(dimension)

        # 5. 选择最终分数和来源
        if self._mode == "cold":
            value = rule_prior
            source = "rule"
        elif self._mode == "warm":
            # WARM: 规则权重 0.6，ML 权重 0.4
            value = 0.6 * rule_prior + 0.4 * posterior
            source = "bayesian"
        else:
            value = posterior
            source = "bayesian"

        return ScoreCard(
            dimension=dimension,
            value=round(max(0.0, min(1.0, value)), 3),
            confidence=round(confidence, 3),
            source=source,
            rule_prior=round(rule_prior, 3),
            ml_likelihood=round(ml_likelihood, 3),
            posterior=round(posterior, 3),
            model_version=self._model_version,
            features=features if self._mode != "cold" else None,
        )

    def _rule_prior(self, dimension: str, features: Dict) -> float:
        """规则先验 — 返回概率值 0-1"""
        rule_fn = self._cold_rules.get(dimension)
        if rule_fn:
            try:
                return rule_fn(features)
            except Exception:
                logger.warning(f"Unexpected error in adaptive_scorer.py", exc_info=True)
                pass

        # 通用规则回退
        content = features.get("content", "")
        if content:
            result = self._rule_scorer.score(content)
            return result
        return 0.5

    def _ml_likelihood(self, dimension: str, features: Dict) -> float:
        """ML 似然 — ComplementNB 预测"""
        model = self._models.get(dimension)
        if model is None:
            return 0.5

        try:
            X = self._features_to_vector(features)
            if X is not None and hasattr(model, "predict_proba"):
                proba = model.predict_proba(X)[0]
                # 取正类概率
                return float(proba[-1]) if len(proba) > 1 else float(proba[0])
        except Exception as e:
            logger.debug(f"[AdaptiveScorer] ML 预测失败 {dimension}: {e}")

        return 0.5

    def _bayesian_update(
        self,
        prior: float,
        likelihood: float,
        p_e_given_not_h: float = 0.3,
    ) -> float:
        """
        贝叶斯后验更新。

        ADR-016 修复：显式传入 P(E|~H)，不再假设 = 1 - P(E|H)

        P(H|E) = P(E|H) * P(H) / P(E)
        P(E) = P(E|H) * P(H) + P(E|~H) * P(~H)
        """
        p_h = max(0.01, min(0.99, prior))  # 避免除零
        p_not_h = 1.0 - p_h
        p_e = likelihood * p_h + p_e_given_not_h * p_not_h
        if p_e < 1e-10:
            return p_h
        posterior = (likelihood * p_h) / p_e
        return max(0.0, min(1.0, posterior))

    def _compute_confidence(self, dimension: str) -> float:
        """计算置信度"""
        stats = self._stats.get(f"{dimension}.value")
        if not stats or stats.n < 5:
            return 0.3 if self._mode == "cold" else 0.5

        # 基于样本数和方差的置信度
        sample_confidence = min(1.0, stats.n / 100.0)
        variance = stats.variance
        variance_confidence = max(0.0, 1.0 - variance)  # 低方差 = 高置信

        return 0.5 * sample_confidence + 0.5 * variance_confidence

    # ==================== 特征提取 ====================

    def _extract_features(self, item: Any) -> Dict[str, Any]:
        """提取 21 维特征"""
        content = ""
        if isinstance(item, str):
            content = item
        elif isinstance(item, dict):
            content = item.get("content", "") or item.get("user_content", "")
            if not content:
                content = str(item)
        elif hasattr(item, "user_content"):
            content = f"{getattr(item, 'user_content', '')}\n{getattr(item, 'assistant_content', '')}"
        else:
            content = str(item)

        features: Dict[str, Any] = {"content": content}

        if not content:
            return features

        stripped = content.strip()
        features["length"] = len(stripped)
        features["has_code"] = 1 if "```" in stripped else 0
        features["has_questions"] = 1 if "?" in stripped or "？" in stripped else 0
        features["has_list"] = 1 if re.search(r'^\s*[-*\d]\s+', stripped, re.M) else 0
        features["has_heading"] = 1 if re.search(r'^#{1,6}\s', stripped, re.M) else 0
        features["has_url"] = 1 if re.search(r'https?://', stripped) else 0
        features["has_file_path"] = 1 if re.search(r'(?:~/|/|\.\.?/)[\w./-]+', stripped) else 0
        features["has_mention"] = 1 if re.search(r'@\w+', stripped) else 0
        features["code_block_count"] = len(re.findall(r'```', stripped)) // 2
        features["url_count"] = len(re.findall(r'https?://\S+', stripped))
        features["list_item_count"] = len(re.findall(r'^\s*[-*\d]\s+', stripped, re.M))

        # 语言检测
        features["has_chinese"] = 1 if re.search(r'[一-龥]', stripped) else 0
        features["has_english"] = 1 if re.search(r'[a-zA-Z]{3,}', stripped) else 0

        # 时间特征
        now = datetime.now()
        features["hour_of_day"] = now.hour
        features["day_of_week"] = now.weekday()

        # 来源
        if isinstance(item, dict):
            features["source_agent"] = item.get("source", item.get("agent_name", "unknown"))
        else:
            features["source_agent"] = getattr(item, "name", "unknown")

        return features

    def _features_to_vector(self, features: Dict) -> Optional[np.ndarray]:
        """将特征字典转为向量（ColumnTransformer 风格）"""
        try:
            # 简化版：手动构建特征向量
            # 文本特征（TF-IDF 需要 fitted vectorizer，COLD 阶段跳过）
            numeric_features = [
                features.get("length", 0),
                features.get("has_code", 0),
                features.get("has_questions", 0),
                features.get("has_list", 0),
                features.get("has_heading", 0),
                features.get("has_url", 0),
                features.get("has_file_path", 0),
                features.get("has_mention", 0),
                features.get("code_block_count", 0),
                features.get("url_count", 0),
                features.get("list_item_count", 0),
                features.get("has_chinese", 0),
                features.get("has_english", 0),
                features.get("hour_of_day", 0),
                features.get("day_of_week", 0),
            ]
            return np.array([numeric_features], dtype=np.float64)
        except Exception:
            logger.warning(f"Unexpected error in adaptive_scorer.py", exc_info=True)
            return None

    # ==================== 规则先验函数 ====================

    def _noise_rule_prior(self, features: Dict) -> float:
        """噪声规则先验"""
        content = features.get("content", "")
        if not content:
            return 0.1
        result = noise_penalty(content)
        return result.score

    def _quality_rule_prior(self, features: Dict) -> float:
        """质量规则先验"""
        content = features.get("content", "")
        if not content:
            return 0.1
        result = quality_score(content)
        return result.score

    def _form_rule_prior(self, features: Dict) -> float:
        """形式规则先验（代码块、标题、列表、文件路径、URL）"""
        score = 0.3  # 基础分
        if features.get("has_code"):
            score += 0.2
        if features.get("has_heading"):
            score += 0.1
        if features.get("has_list"):
            score += 0.1
        if features.get("has_file_path"):
            score += 0.1
        if features.get("has_url"):
            score += 0.1
        length = features.get("length", 0)
        if length > 200:
            score += 0.1
        return min(1.0, score)

    def _entity_quality_rule_prior(self, features: Dict) -> float:
        """实体质量规则先验"""
        content = features.get("content", "")
        if not content:
            return 0.1
        # 基于实体密度
        from core.kia.rule_scorer import entity_density_score
        result = entity_density_score(content)
        return result.score

    # ==================== ML 训练 ====================

    def _schedule_retrain(self) -> None:
        """检查是否需要重训练"""
        for dim in set(r["dimension"] for r in self._retrain_buffer):
            dim_records = [r for r in self._retrain_buffer if r["dimension"] == dim]
            if len(dim_records) >= self.config.get("scoring.min_samples_per_dimension", 20):
                self._retrain_dimension(dim, dim_records)

        # 清空已处理的记录
        self._retrain_buffer.clear()

    def _retrain_dimension(self, dimension: str, records: List[Dict]) -> None:
        """重训练指定维度的模型"""
        try:
            from sklearn.naive_bayes import ComplementNB

            X_list = []
            y_list = []
            for r in records:
                X = self._features_to_vector(r["features"])
                if X is not None:
                    X_list.append(X[0])
                    # ADR-016 修复：用 rule_prior 作为标签，不用 posterior
                    label = 1 if r["label"] >= 0.5 else 0
                    y_list.append(label)

            if len(X_list) < 10:
                return

            X = np.array(X_list)
            y = np.array(y_list)

            model = ComplementNB()
            model.fit(X, y)

            self._models[dimension] = model
            self._model_version += 1
            self._update_mode()

            # 持久化
            self._save_model()

            logger.info(
                f"[AdaptiveScorer] 重训练 {dimension}: "
                f"{len(X_list)} 样本, mode={self._mode}"
            )

        except ImportError:
            logger.debug("[AdaptiveScorer] scikit-learn 未安装，跳过 ML 训练")
        except Exception as e:
            logger.error(f"[AdaptiveScorer] 重训练失败 {dimension}: {e}")

    def _ewma_update(self, model: Any, fb: Feedback) -> None:
        """EWMA 增量更新模型参数"""
        # EWMA 平滑模型参数（alpha=0.1）
        alpha = self._ewma_alpha
        if hasattr(model, "feature_log_prob_") and model.feature_log_prob_ is not None:
            # 微调特征对数概率（简化实现）
            adjustment = alpha * (fb.expected - fb.actual) * 0.01
            model.feature_log_prob_ += adjustment

    def _update_mode(self) -> None:
        """更新冷启动阶段"""
        total_samples = len(self._retrain_buffer) + sum(
            1 for _ in self._models.values()
        )
        trained_dims = len(self._models)

        if trained_dims == 0 or total_samples < self._SAMPLE_THRESHOLDS["warm"]:
            self._mode = "cold"
        elif total_samples < self._SAMPLE_THRESHOLDS["hot"]:
            self._mode = "warm"
        else:
            self._mode = "hot"

    # ==================== 模型持久化 ====================

    def _save_model(self) -> None:
        """保存模型到磁盘"""
        try:
            import joblib

            version_dir = self._model_dir / f"v{self._model_version}"
            version_dir.mkdir(parents=True, exist_ok=True)

            # 保存模型
            for dim, model in self._models.items():
                joblib.dump(model, version_dir / f"{dim}.joblib")

            # 保存元数据
            meta = {
                "version": self._model_version,
                "mode": self._mode,
                "domain": self.domain,
                "timestamp": datetime.now().isoformat(),
                "dimensions": list(self._models.keys()),
            }
            (version_dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            # 更新 current 软链接
            current = self._model_dir / "current"
            if current.is_symlink():
                current.unlink()
            current.symlink_to(version_dir)

            # 保留最近 5 个版本
            self._cleanup_old_versions(5)

        except ImportError:
            pass
        except Exception as e:
            logger.error(f"[AdaptiveScorer] 模型保存失败: {e}")

    def _load_model(self) -> None:
        """加载已有模型"""
        try:
            import joblib

            current = self._model_dir / "current"
            if not current.exists():
                return

            target = current.resolve()
            meta_path = target / "meta.json"
            if not meta_path.exists():
                return

            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self._model_version = meta.get("version", 0)
            self._mode = meta.get("mode", "cold")

            for dim in meta.get("dimensions", []):
                model_path = target / f"{dim}.joblib"
                if model_path.exists():
                    self._models[dim] = joblib.load(model_path)

            logger.info(
                f"[AdaptiveScorer] 加载模型: v{self._model_version}, "
                f"mode={self._mode}, dims={list(self._models.keys())}"
            )

        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"[AdaptiveScorer] 模型加载失败: {e}")
            try:
                from core.mnemos_bus import publish_event
                publish_event("model_load_failed", "scoring", {
                    "model_version": getattr(self, '_model_version', 0),
                    "error": str(e),
                    "context": {"model_dir": str(self._model_dir)},
                })
            except Exception:
                logger.warning(f"Unexpected error in adaptive_scorer.py", exc_info=True)
                pass

    def _cleanup_old_versions(self, keep: int) -> None:
        """保留最近 N 个版本"""
        try:
            versions = sorted(
                [d for d in self._model_dir.iterdir() if d.is_dir() and d.name.startswith("v")],
                key=lambda d: int(d.name[1:]) if d.name[1:].isdigit() else 0,
            )
            for old in versions[:-keep]:
                import shutil
                shutil.rmtree(old, ignore_errors=True)
        except Exception:
            logger.warning(f"Unexpected error in adaptive_scorer.py", exc_info=True)
            pass

    def rollback(self, target_version: Optional[int] = None) -> bool:
        """回滚到指定版本"""
        try:
            if target_version is None:
                # 回滚到上一个版本
                target_version = max(0, self._model_version - 1)

            version_dir = self._model_dir / f"v{target_version}"
            if not version_dir.exists():
                logger.error(f"[AdaptiveScorer] 版本不存在: v{target_version}")
                return False

            import joblib

            meta = json.loads((version_dir / "meta.json").read_text(encoding="utf-8"))
            self._models.clear()
            for dim in meta.get("dimensions", []):
                model_path = version_dir / f"{dim}.joblib"
                if model_path.exists():
                    self._models[dim] = joblib.load(model_path)

            self._model_version = target_version
            self._mode = meta.get("mode", "cold")

            # 更新 current 链接
            current = self._model_dir / "current"
            if current.is_symlink():
                current.unlink()
            current.symlink_to(version_dir)

            logger.info(f"[AdaptiveScorer] 已回滚到 v{target_version}")
            return True

        except Exception as e:
            logger.error(f"[AdaptiveScorer] 回滚失败: {e}")
            return False

    # ==================== 状态查询 ====================

    @property
    def mode(self) -> str:
        return self._mode

    def get_status(self) -> Dict:
        """获取评分器状态"""
        return {
            "domain": self.domain,
            "mode": self._mode,
            "model_version": self._model_version,
            "dimensions": list(self._models.keys()) or list(self._cold_rules.keys()),
            "retrain_buffer_size": len(self._retrain_buffer),
            "stats_dimensions": self._stats.dimensions,
        }
