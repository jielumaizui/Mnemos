# -*- coding: utf-8 -*-
"""
DistillScorerV2 — 蒸馏层评分器（V2 桥接）

将蒸馏层接入 AdaptiveScorerV2，实现评分闭环。
维度：
  - distill:   蒸馏价值（0-1，>0.6 触发提取）
  - falsify:   可证伪性
  - evolve:    进化潜力
  - heat:      热度预测
  - l1:        L1 storage 质量（复用 V2 通用维度）
  - sync:      同步紧迫度
  - kg:        知识图谱关联度
  - profile:   画像匹配度
  - ops:       运维异常度
"""

from __future__ import annotations

from typing import Dict, List

from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2, ScoreCardV2
from core.config import get_config
from core.kia.ingest_helpers import is_noise_message
from core.kia.policy import get_effective_policy


class DistillScorerV2:
    """蒸馏层 V2 评分器"""

    # 默认触发蒸馏的维度与阈值
    # "l1" 是 "l1_storage" 的别名，由 AdaptiveScorerV2.normalize_dimension 统一映射。
    DEFAULT_DIMENSIONS = [
        "distill",
        "falsify",
        "evolve",
        "heat",
        "l1",
        "sync",
        "kg",
        "profile",
        "ops",
    ]
    DEFAULT_TRIGGER_DIM = "distill"
    DEFAULT_TRIGGER_THRESHOLD = 0.6

    def __init__(self, config: Dict | None = None):
        self._config = config or {}
        # 优先使用 EffectivePolicy 的 shadow / 全局 Config，最后才是传入的 config 字典
        self._trigger_threshold = self._config.get(
            "trigger_threshold",
            get_effective_policy().get(
                "distill.trigger_threshold",
                get_config().get("distill.trigger_threshold", self.DEFAULT_TRIGGER_THRESHOLD),
            ),
        )
        self._scorer = AdaptiveScorerV2(
            domain="distill",
            config=config,
        )
        self._dimension_weights: Dict[str, float] = {}

    def _apply_dimension_weights(self, card: ScoreCardV2) -> ScoreCardV2:
        """根据 Layer5 维度权重调整各维度得分。"""
        if not self._dimension_weights:
            return card
        new_scores = dict(card.scores)
        for dim, weight in self._dimension_weights.items():
            if dim in new_scores:
                new_scores[dim] = max(0.0, min(1.0, new_scores[dim] * weight))
        return ScoreCardV2(
            scores=new_scores,
            confidences=card.confidences,
            features=card.features,
            model_version=card.model_version,
            timestamp=card.timestamp,
        )

    def score(self, content: str, dimensions: List[str] | None = None) -> ScoreCardV2:
        """对内容执行多维度 V2 评分。

        Args:
            content: 待评分文本
            dimensions: 评分维度列表（默认六域全开）

        Returns:
            ScoreCardV2
        """
        dims = dimensions or self.DEFAULT_DIMENSIONS
        item = {"content": content, "frontmatter": {}}
        card = self._scorer.score(item, dimensions=dims)
        return self._apply_dimension_weights(card)

    def should_distill(self, content: str, threshold: float | None = None) -> bool:
        """是否应触发蒸馏。

        Args:
            content: 待判断文本
            threshold: 自定义阈值（覆盖默认值）

        Returns:
            True 当且仅当 distill 维度得分超过阈值且内容不是低价值噪声
        """
        if is_noise_message(content):
            return False
        card = self.score(content, dimensions=[self.DEFAULT_TRIGGER_DIM])
        score = card.scores.get(self.DEFAULT_TRIGGER_DIM, 0.0)
        return score > (threshold or self._trigger_threshold)
