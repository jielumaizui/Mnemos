# -*- coding: utf-8 -*-
"""
KGScorer — 知识图谱层评分器

维度：
  - entity_quality: 实体质量（0-1，>0.5入库，<0.3拒绝）
  - relation_confidence: 关系置信度（0-1，>0.6建立关系）
  - knowledge_freshness: 知识新鲜度（0-1，<0.2标记过时）
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from core.scoring.adaptive_scorer import AdaptiveScorer, ScoreCard
from core.config import get_config


class KGScorer:
    """知识图谱层评分器"""

    def __init__(self):
        self._config = get_config()
        self._entity_threshold = self._config.get("knowledge_graph.entity_quality_threshold", 0.3)
        self._relation_strong = self._config.get("knowledge_graph.relation_confidence_strong", 0.7)
        self._relation_weak = self._config.get("knowledge_graph.relation_confidence_weak", 0.4)
        self._freshness_half_life = self._config.get("knowledge_graph.freshness_decay_half_life_days", 30)
        self._deprecated_threshold = self._config.get("knowledge_graph.freshness_deprecated_threshold", 0.2)

        self._scorer = AdaptiveScorer(
            domain="kg",
            cold_start_rules={
                "entity_quality": self._entity_quality_rule,
                "relation_confidence": self._relation_confidence_rule,
                "knowledge_freshness": self._freshness_rule,
            },
        )

    def score(self, content: str, **kwargs) -> List[ScoreCard]:
        return self._scorer.score(content, dimensions=[
            "entity_quality", "relation_confidence", "knowledge_freshness",
        ])

    def entity_decision(self, quality_score: float) -> str:
        """实体入库决策"""
        if quality_score >= 0.5:
            return "accept"
        elif quality_score >= self._entity_threshold:
            return "tentative"
        else:
            return "reject"

    def relation_level(self, confidence: float) -> str:
        """关系置信度等级"""
        if confidence >= self._relation_strong:
            return "strong"
        elif confidence >= self._relation_weak:
            return "weak"
        else:
            return "suspect"

    def _entity_quality_rule(self, features: Dict) -> float:
        content = features.get("content", "")
        from core.kia.rule_scorer import entity_density_score
        return entity_density_score(content).score

    def _relation_confidence_rule(self, features: Dict) -> float:
        """关系置信度规则：有明确关联标记 = 高置信"""
        content = features.get("content", "")
        score = 0.3
        # Wiki 引用 = 明确关联
        import re
        wiki_refs = re.findall(r'\[\[([^\]]+)\]\]', content)
        if wiki_refs:
            score += min(0.3, len(wiki_refs) * 0.1)
        # 明确关联词
        relation_words = sum(1 for kw in ("依赖", "基于", "使用", "depends", "uses", "related")
                             if kw in content.lower())
        score += min(0.2, relation_words * 0.1)
        return min(1.0, score)

    def _freshness_rule(self, features: Dict) -> float:
        """知识新鲜度规则：基于时间衰减"""
        # 默认假设新内容是新鲜的
        # 真正的时间衰减在 update_freshness 中用贝叶斯更新
        return 0.8

    def update_freshness(
        self,
        current_freshness: float,
        evidence_type: str,  # "confirm" | "contradict" | "neutral"
        days_since_last: int = 0,
    ) -> float:
        """
        贝叶斯更新知识新鲜度

        Args:
            current_freshness: 当前新鲜度
            evidence_type: 证据类型
            days_since_last: 距上次更新的天数
        """
        import math
        # 时间衰减
        half_life = self._freshness_half_life
        decay = math.exp(-0.693 * days_since_last / half_life) if days_since_last > 0 else 1.0
        fresh = current_freshness * decay

        # 贝叶斯更新
        if evidence_type == "confirm":
            fresh = min(1.0, fresh + 0.2)
        elif evidence_type == "contradict":
            fresh *= 0.5

        return max(0.0, min(1.0, fresh))
