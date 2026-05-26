# -*- coding: utf-8 -*-
"""
FeedbackFusion — 反馈融合

加权合并三种来源的反馈：
  - 手动反馈：权重 1.0
  - 隐式反馈：权重 0.6
  - 自观测：权重 0.3
"""

from __future__ import annotations

from typing import Dict, List

from core.scoring.adaptive_scorer import Feedback


class FeedbackFusion:
    """反馈融合"""

    SOURCE_WEIGHTS = {
        "manual": 1.0,
        "implicit": 0.6,
        "implicit_aggregated": 0.6,
        "self_observation": 0.3,
    }

    def fuse(self, feedbacks: List[Feedback]) -> Dict[str, Feedback]:
        """
        融合同一维度的多条反馈

        Returns:
            {dimension: fused_feedback}
        """
        by_dim: Dict[str, List[Feedback]] = {}
        for fb in feedbacks:
            by_dim.setdefault(fb.dimension, []).append(fb)

        result = {}
        for dim, fbs in by_dim.items():
            fused = self._fuse_dimension(fbs)
            result[dim] = fused

        return result

    def _fuse_dimension(self, feedbacks: List[Feedback]) -> Feedback:
        """融合同一维度的反馈"""
        total_weight = 0.0
        weighted_expected = 0.0
        weighted_actual = 0.0

        for fb in feedbacks:
            source_weight = self.SOURCE_WEIGHTS.get(fb.source, 0.5)
            combined_weight = source_weight * fb.weight
            total_weight += combined_weight
            weighted_expected += fb.expected * combined_weight
            weighted_actual += fb.actual * combined_weight

        if total_weight == 0:
            return Feedback(
                dimension=feedbacks[0].dimension if feedbacks else "unknown",
                expected=0.5, actual=0.5, source="fused", weight=0.0,
            )

        return Feedback(
            dimension=feedbacks[0].dimension,
            expected=weighted_expected / total_weight,
            actual=weighted_actual / total_weight,
            source="fused",
            weight=min(1.0, total_weight),
            context={"source_count": len(feedbacks), "sources": list(set(fb.source for fb in feedbacks))},
        )
