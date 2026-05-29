# -*- coding: utf-8 -*-
"""
DistillScorerV2 — 蒸馏层评分器（V2 桥接）

阶段二：将蒸馏层接入 AdaptiveScorerV2，实现评分闭环。
维度：
  - distill:   蒸馏价值（0-1，>0.6 触发提取）
  - memos:     Memos 质量（复用 V2 通用维度）
  - sync:      同步紧迫度
  - kg:        知识图谱关联度
  - profile:   画像匹配度
  - ops:       运维异常度

与 DistillScorer（V1）并行存在，由 ValuePrejudgment._get_scorer_v2() 优先选用。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2, ScoreCardV2
from core.config import get_config


class DistillScorerV2:
    """蒸馏层 V2 评分器"""

    # 默认触发蒸馏的维度与阈值
    DEFAULT_DIMENSIONS = ["distill", "memos", "sync", "kg", "profile", "ops"]
    DEFAULT_TRIGGER_DIM = "distill"
    DEFAULT_TRIGGER_THRESHOLD = 0.6

    def __init__(self, config: Dict = None):
        self._config = config or {}
        self._trigger_threshold = self._config.get(
            "trigger_threshold",
            get_config().get("distill.trigger_threshold", self.DEFAULT_TRIGGER_THRESHOLD),
        )
        self._scorer = AdaptiveScorerV2(
            domain="distill",
            config=config,
        )

    def score(self, content: str, dimensions: List[str] = None) -> ScoreCardV2:
        """对内容执行多维度 V2 评分。

        Args:
            content: 待评分文本
            dimensions: 评分维度列表（默认六域全开）

        Returns:
            ScoreCardV2
        """
        dims = dimensions or self.DEFAULT_DIMENSIONS
        item = {"content": content, "frontmatter": {}}
        return self._scorer.score(item, dimensions=dims)

    def should_distill(self, content: str, threshold: float = None) -> bool:
        """是否应触发蒸馏。

        Args:
            content: 待判断文本
            threshold: 自定义阈值（覆盖默认值）

        Returns:
            True 当且仅当 distill 维度得分超过阈值
        """
        card = self.score(content, dimensions=[self.DEFAULT_TRIGGER_DIM])
        score = card.scores.get(self.DEFAULT_TRIGGER_DIM, 0.0)
        return score > (threshold or self._trigger_threshold)

    def score_with_sources(self, content: str) -> Dict:
        """返回带明细的评分结果（用于调试和审计）。

        Returns:
            {
                "scores": {"distill": 0.72, ...},
                "confidences": {"distill": 0.85, ...},
                "features": {...},
                "model_version": "v2-...",
                "should_distill": True,
            }
        """
        card = self.score(content)
        return {
            "scores": card.scores,
            "confidences": card.confidences,
            "features": card.features,
            "model_version": card.model_version,
            "should_distill": card.scores.get(self.DEFAULT_TRIGGER_DIM, 0.0)
            > self._trigger_threshold,
        }
