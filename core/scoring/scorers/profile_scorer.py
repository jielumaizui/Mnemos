# -*- coding: utf-8 -*-
"""
ProfileScorer — 画像层评分器

维度：
  - behavior_pattern: 行为模式强度（0-1）
  - blind_spot_score: 盲点分数（0-1，高=知识盲区大）
  - preference_stability: 偏好稳定性（0-1，高=偏好明确稳定）
"""

from __future__ import annotations

from typing import Dict, List

from core.scoring.adaptive_scorer import AdaptiveScorer, ScoreCard


class ProfileScorer:
    """画像层评分器"""

    def __init__(self):
        self._scorer = AdaptiveScorer(
            domain="profile",
            cold_start_rules={
                "behavior_pattern": self._behavior_rule,
                "blind_spot_score": self._blind_spot_rule,
                "preference_stability": self._stability_rule,
            },
        )

    def score(self, content: str, **kwargs) -> List[ScoreCard]:
        return self._scorer.score(content, dimensions=[
            "behavior_pattern", "blind_spot_score", "preference_stability",
        ])

    def _behavior_rule(self, features: Dict) -> float:
        """行为模式规则：重复性模式 = 高行为模式分数"""
        content = features.get("content", "")
        score = 0.3
        # 技术栈偏好
        if features.get("has_code"):
            score += 0.2
        # 时间模式
        hour = features.get("hour_of_day", 0)
        if 9 <= hour <= 18:
            score += 0.1  # 工作时间
        return min(1.0, score)

    def _blind_spot_rule(self, features: Dict) -> float:
        """盲点规则：搜索无结果/低置信 = 高盲点分数"""
        content = features.get("content", "").lower()
        score = 0.2
        # 提问型（不知道的领域）
        question_marks = content.count("?") + content.count("？")
        if question_marks > 2:
            score += 0.3
        # 探索型关键词
        explore_words = sum(1 for kw in (
            "怎么", "如何", "什么", "为什么", "how", "what", "why",
            "不了解", "不清楚", "没见过", "第一次",
        ) if kw in content)
        score += min(0.3, explore_words * 0.1)
        return min(1.0, score)

    def _stability_rule(self, features: Dict) -> float:
        """偏好稳定性规则：重复选择相同技术/方案 = 高稳定性"""
        # COLD 阶段给中间值
        return 0.5
