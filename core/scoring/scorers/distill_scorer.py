# -*- coding: utf-8 -*-
"""
DistillScorer — 蒸馏层评分器

维度：
  - distill_score: 蒸馏价值（0-1，>0.6 触发提取）
  - falsify_score: 可证伪性（0-1，高=需要验证）
  - evolve_score: 进化潜力（0-1，高=未来价值增长）
  - heat_forecast: 热度预测（0-1，高=热知识）
"""

from __future__ import annotations

import re
from typing import Dict, List

from core.scoring.adaptive_scorer import AdaptiveScorer, ScoreCard
from core.config import get_config


class DistillScorer:
    """蒸馏层评分器"""

    def __init__(self):
        self._config = get_config()
        self._trigger_threshold = self._config.get("distill.trigger_threshold", 0.4)
        self._scorer = AdaptiveScorer(
            domain="distill",
            cold_start_rules={
                "distill_score": self._distill_rule,
                "falsify_score": self._falsify_rule,
                "evolve_score": self._evolve_rule,
                "heat_forecast": self._heat_rule,
            },
        )

    def score(self, content: str, **kwargs) -> List[ScoreCard]:
        return self._scorer.score(content, dimensions=[
            "distill_score", "falsify_score", "evolve_score", "heat_forecast",
        ])

    def should_distill(self, content: str) -> bool:
        """是否应触发蒸馏"""
        cards = self.score(content)
        distill_card = next((c for c in cards if c.dimension == "distill_score"), None)
        if distill_card:
            return distill_card.value > self._trigger_threshold
        return False

    def _distill_rule(self, features: Dict) -> float:
        """蒸馏价值规则"""
        content = features.get("content", "")
        from core.kia.rule_scorer import RuleScorer
        return RuleScorer().score(content)

    def _falsify_rule(self, features: Dict) -> float:
        """可证伪性规则：包含具体断言/数据/指标 = 高可证伪"""
        content = features.get("content", "").lower()
        score = 0.2
        # 具体数字/指标
        if re.search(r'\d+\.?\d*%', content):
            score += 0.2
        if re.search(r'\d+ms|\d+s(?!econd)', content):
            score += 0.1
        # 断言标记
        assert_words = sum(1 for w in ("必须", "一定", "never", "always", "应该", "should", "必须不")
                           if w in content)
        score += min(0.3, assert_words * 0.1)
        return min(1.0, score)

    def _evolve_rule(self, features: Dict) -> float:
        """进化潜力规则：涉及技术选型/架构决策 = 高进化潜力"""
        content = features.get("content", "").lower()
        score = 0.2
        evolve_signals = sum(1 for kw in (
            "架构", "设计", "选型", "迁移", "升级", "重构",
            "architecture", "migration", "upgrade", "refactor",
            "替代", "比较", "对比", "vs",
        ) if kw in content)
        return min(1.0, score + evolve_signals * 0.15)

    def _heat_rule(self, features: Dict) -> float:
        """热度预测规则：近期频繁出现 + 高质量 = 热知识"""
        content = features.get("content", "")
        score = 0.3
        # 代码相关 = 热度偏高
        if features.get("has_code"):
            score += 0.2
        # 包含决策/方案 = 热度偏高
        if any(kw in content for kw in ("决定", "方案", "选择", "decided", "solution")):
            score += 0.2
        return min(1.0, score)
