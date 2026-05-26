# -*- coding: utf-8 -*-
"""
SyncScorer — 同步层评分器

维度：
  - noise_score: 噪声程度（0-1，高=噪声大应跳过）
  - urgency_score: 紧急程度（0-1，高=需立即同步）
  - sync_priority: 同步优先级（0-1，高=实时同步）
"""

from __future__ import annotations

import re
from typing import Dict, List

from core.scoring.adaptive_scorer import AdaptiveScorer, ScoreCard


class SyncScorer:
    """同步层评分器"""

    def __init__(self):
        self._scorer = AdaptiveScorer(
            domain="sync",
            cold_start_rules={
                "noise_score": self._noise_rule,
                "urgency_score": self._urgency_rule,
                "sync_priority": self._priority_rule,
            },
        )

    def score(self, content: str, **kwargs) -> List[ScoreCard]:
        return self._scorer.score(content, dimensions=[
            "noise_score", "urgency_score", "sync_priority",
        ])

    def _noise_rule(self, features: Dict) -> float:
        content = features.get("content", "")
        from core.kia.rule_scorer import noise_penalty
        return noise_penalty(content).score

    def _urgency_rule(self, features: Dict) -> float:
        """紧急程度：包含错误/异常/崩溃等关键词则高分"""
        content = features.get("content", "").lower()
        if not content:
            return 0.0
        urgent_signals = sum(1 for kw in (
            "崩溃", "异常", "error", "crash", "fatal", "紧急",
            "生产环境", "线上", "outage", "down", "故障",
        ) if kw in content)
        return min(1.0, urgent_signals * 0.25 + 0.1)

    def _priority_rule(self, features: Dict) -> float:
        """同步优先级：高质量+有代码 = 高优先级"""
        content = features.get("content", "")
        score = 0.3
        if features.get("has_code"):
            score += 0.3
        if features.get("length", 0) > 200:
            score += 0.2
        if features.get("has_list"):
            score += 0.1
        return min(1.0, score)
