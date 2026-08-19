# -*- coding: utf-8 -*-
"""
lightweight_nb.py — 纯 Python Complement Naive Bayes

ADR-016 要求的轻量评分器，用于：
  1. 树莓派/嵌入式环境（无 sklearn）
  2. 快速冷启动（无需编译依赖）
  3. V2 的 fallback 降级路径

算法：ComplementNB（Rennie et al., 2003）
  - 用补集类（非目标类）的 TF 估计参数
  - 对不平衡数据更鲁棒
  - 支持 partial_fit 增量更新
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class LightweightComplementNB:
    """
    纯 Python Complement Naive Bayes。

    特征格式：Dict[str, float]（稀疏特征，如词频或元数据）
    """

    def __init__(self, alpha: float = 1.0):
        """
        Args:
            alpha: Laplace 平滑参数
        """
        self.alpha = alpha
        self._class_count: Counter[int] = Counter()  # 类样本数
        self._feature_count: Dict[int, Dict[str, float]] = defaultdict(
            Counter  # type: ignore[arg-type]
        )  # type: ignore[arg-type]
        self._feature_log_prob: Dict[int, Dict[str, float]] = {}
        self._class_log_prior: Dict[int, float] = {}
        self._classes: set = set()
        self._n_features: int = 0
        self.is_fitted = False

    # ── 训练接口 ──

    def fit(self, X: List[Dict[str, float]], y: List[int]) -> "LightweightComplementNB":
        """
        批量训练。

        Args:
            X: 特征列表，每个特征是 {feature_name: count} 字典
            y: 标签列表，0 或 1
        """
        self._reset()
        self.partial_fit(X, y, classes=[0, 1])
        return self

    def partial_fit(
        self,
        X: List[Dict[str, float]],
        y: List[int],
        classes: Optional[List[int]] = None,
    ) -> "LightweightComplementNB":
        """
        增量训练（支持 online learning）。

        Args:
            X: 新增样本特征
            y: 新增样本标签
            classes: 所有可能的类标签（首次调用时必须提供）
        """
        if classes is not None:
            self._classes.update(classes)

        if not self._classes:
            raise ValueError("classes must be provided on first call to partial_fit()")

        for features, label in zip(X, y):
            if not features:
                features = {"__bias__": 1.0}
            self._class_count[label] += 1
            for feat, val in features.items():
                self._feature_count[label][feat] += val

        # 一次性更新特征总数（避免 O(n²×m)）
        all_features = set()  # type: ignore[var-annotated]
        for counter in self._feature_count.values():
            all_features.update(counter.keys())
        self._n_features = max(self._n_features, len(all_features))

        self._update_log_prob()
        self.is_fitted = True
        logger.debug(
            "[LightweightNB] partial_fit: %s samples, classes=%s", len(X), dict(self._class_count)
        )
        return self

    # ── 预测接口 ──

    def predict_proba(self, X: List[Dict[str, float]]) -> List[Dict[int, float]]:
        """
        预测概率。

        Returns:
            [{0: p0, 1: p1}, ...]
        """
        if not self.is_fitted:
            # 未训练时返回均匀分布
            return [{0: 0.5, 1: 0.5} for _ in X]

        # 预计算每个类别的补集统计量（避免 O(C²×F)）
        total_all = sum(self._class_count.values())
        complement_cache = {}
        for cls in sorted(self._classes):
            comp_count = Counter()  # type: ignore[var-annotated]
            for other_cls, feat_counter in self._feature_count.items():
                if other_cls != cls:
                    comp_count += feat_counter  # type: ignore[arg-type]
            total_comp = total_all - self._class_count.get(cls, 0)
            complement_cache[cls] = (comp_count, total_comp)

        results = []
        for features in X:
            if not features:
                features = {"__bias__": 1.0}
            scores = {}
            for cls in sorted(self._classes):
                log_prob = self._class_log_prior.get(cls, math.log(0.5))
                comp_count, total_comp = complement_cache[cls]
                denom = total_comp + self.alpha * max(self._n_features, 1)
                for feat, val in features.items():
                    count = comp_count.get(feat, 0.0)
                    log_w = math.log((count + self.alpha) / denom)
                    log_prob += val * log_w
                scores[cls] = log_prob

            results.append(self._softmax(scores))

        return results

    def predict(self, X: List[Dict[str, float]]) -> List[int]:
        """预测标签"""
        probs = self.predict_proba(X)
        return [max(p, key=p.get) for p in probs]  # type: ignore[arg-type]

    def score(self, X: List[Dict[str, float]], y: List[int]) -> float:
        """准确率"""
        preds = self.predict(X)
        correct = sum(1 for p, g in zip(preds, y) if p == g)
        return correct / len(y) if y else 0.0

    # ── 序列化接口 ──

    def to_dict(self) -> Dict:
        """导出为字典（便于 JSON 序列化）"""
        return {
            "alpha": self.alpha,
            "class_count": dict(self._class_count),
            "feature_count": {k: dict(v) for k, v in self._feature_count.items()},
            "classes": sorted(self._classes),
            "n_features": self._n_features,
            "is_fitted": self.is_fitted,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "LightweightComplementNB":
        """从字典恢复"""
        inst = cls(alpha=data.get("alpha", 1.0))
        inst._class_count = Counter(data.get("class_count", {}))  # type: ignore[assignment]
        inst._feature_count = defaultdict(Counter)  # type: ignore[arg-type]
        for k, v in data.get("feature_count", {}).items():
            inst._feature_count[int(k)] = Counter(v)  # type: ignore[assignment]
        inst._classes = set(data.get("classes", [0, 1]))
        inst._n_features = data.get("n_features", 0)
        inst.is_fitted = data.get("is_fitted", False)
        if inst.is_fitted:
            inst._update_log_prob()
        return inst

    # ── 内部方法 ──

    def _reset(self) -> None:
        self._class_count.clear()
        self._feature_count.clear()
        self._feature_log_prob.clear()
        self._class_log_prior.clear()
        self._classes.clear()
        self._n_features = 0
        self.is_fitted = False

    def _update_log_prob(self) -> None:
        """更新对数概率缓存"""
        total = sum(self._class_count.values())
        if total == 0:
            return
        class_count = max(len(self._classes), 1)
        for cls in self._classes:
            self._class_log_prior[cls] = math.log(
                (self._class_count[cls] + self.alpha) / (total + self.alpha * class_count)
            )

    @staticmethod
    def _softmax(scores: Dict[int, float]) -> Dict[int, float]:
        """softmax 归一化"""
        # 数值稳定性：减去最大值
        max_score = max(scores.values())
        exp_scores = {k: math.exp(v - max_score) for k, v in scores.items()}
        total = sum(exp_scores.values())
        if total == 0:
            return {k: 1.0 / len(scores) for k in scores}
        return {k: v / total for k, v in exp_scores.items()}
