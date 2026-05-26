# -*- coding: utf-8 -*-
"""
MemosQualityScorer — 记忆层评分器

维度：
  - quality_score: 内容质量（0-1）
  - sensitivity_score: 敏感度（0-1，1=必须私有）
  - fragmentation_score: 碎片化程度（0-1，1=高度碎片）
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from core.scoring.adaptive_scorer import AdaptiveScorer, ScoreCard


class MemosQualityScorer:
    """记忆层评分器"""

    # 确定性敏感模式（命中即 PRIVATE）
    _SENSITIVE_PATTERNS = [
        re.compile(r'sk-[a-zA-Z0-9]{20,}'),
        re.compile(r'gh[pousr]_[A-Za-z0-9_]{36,}'),
        re.compile(r'AKID[0-9a-zA-Z]{10,}'),
        re.compile(r'password[:=]\s*\S+', re.I),
        re.compile(r'secret[:=]\s*\S+', re.I),
        re.compile(r'token[:=]\s*\S+', re.I),
        re.compile(r'private\s?key', re.I),
        re.compile(r'身份证|社保号|银行卡号'),
    ]

    def __init__(self):
        self._scorer = AdaptiveScorer(
            domain="memos",
            cold_start_rules={
                "quality_score": self._quality_rule,
                "sensitivity_score": self._sensitivity_rule,
                "fragmentation_score": self._fragmentation_rule,
            },
        )

    def score(self, content: str, **kwargs) -> List[ScoreCard]:
        """评分入口"""
        return self._scorer.score(content, dimensions=[
            "quality_score", "sensitivity_score", "fragmentation_score",
        ])

    def _quality_rule(self, features: Dict) -> float:
        content = features.get("content", "")
        from core.kia.rule_scorer import quality_score
        return quality_score(content).score

    def _sensitivity_rule(self, features: Dict) -> float:
        """敏感度规则：确定性模式命中 = 1.0，概率模型辅助"""
        content = features.get("content", "")
        if not content:
            return 0.0
        for pattern in self._SENSITIVE_PATTERNS:
            if pattern.search(content):
                return 1.0
        # 概率辅助：包含隐私相关词
        privacy_signals = sum(1 for kw in ("密码", "密钥", "凭证", "私钥", "credential", "secret")
                              if kw in content.lower())
        return min(0.5, privacy_signals * 0.15)

    def _fragmentation_rule(self, features: Dict) -> float:
        """碎片化规则：内容越短 + 分片标记越多 = 越碎片化"""
        content = features.get("content", "")
        if not content:
            return 0.0
        score = 0.0
        length = len(content)
        if length < 200:
            score += 0.3
        elif length < 500:
            score += 0.1
        # 分片标记
        if "segment=" in content or "type=chunk" in content:
            score += 0.4
        # 截断标记
        if "[truncated]" in content.lower() or "已截断" in content:
            score += 0.2
        return min(1.0, score)
