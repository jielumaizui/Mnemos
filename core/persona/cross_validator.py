# -*- coding: utf-8 -*-
"""
ProfileCrossValidator — 行为×知识双画像交叉验证

发现"言行不一"的矛盾，生成改进建议。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Contradiction:
    """交叉验证发现的矛盾"""
    type: str
    insight: str
    suggestion: str
    severity: str = "medium"  # low / medium / high


class ProfileCrossValidator:
    """行为×知识双画像交叉验证器"""

    CONTRADICTION_RULES = [
        {
            "type": "perfection_trap",
            "behavior_check": lambda p: p.get("perfection_vs_completion", 0.5) > 0.6,
            "knowledge_check": lambda k: k.get("update_ratio", 0) > 0.5,
            "insight": "追求完美但知识频繁更新，产出与交付效率之间存在拉扯",
            "suggestion": "设置 WIP 上限，完成优先于完美",
        },
        {
            "type": "depth_breadth_gap",
            "behavior_check": lambda p: p.get("depth_vs_breadth", 0.5) > 0.6,
            "knowledge_check": lambda k: k.get("domain_entropy", 0) > 0.7,
            "insight": "行为偏好深度但知识领域分散，深度与广度之间存在拉扯",
            "suggestion": "确定 2-3 个核心领域，集中深度建设",
        },
        {
            "type": "learning_style_gap",
            "behavior_check": lambda p: p.get("deduction", 0.5) > 0.6,
            "knowledge_check": lambda k: k.get("simple_mode", "") == "inductive",
            "insight": "行为上偏好演绎但知识积累偏归纳，可能存在学习方式不匹配",
            "suggestion": "尝试从案例出发学习，再归纳为原则",
        },
        {
            "type": "system_fragmentation",
            "behavior_check": lambda p: p.get("system_view", 0.5) > 0.6,
            "knowledge_check": lambda k: k.get("frontmatter_completeness", 0) < 0.4,
            "insight": "有系统视角但知识库结构化程度低，可能影响知识检索效率",
            "suggestion": "补充关键词标签和边界条件，提高知识可发现性",
        },
    ]

    def validate(self, behavior_profile: Dict, knowledge_profile: Dict) -> List[Contradiction]:
        """执行交叉验证，返回发现的矛盾列表"""
        contradictions = []

        for rule in self.CONTRADICTION_RULES:
            try:
                b_match = rule["behavior_check"](behavior_profile)
                k_match = rule["knowledge_check"](knowledge_profile)
                if b_match and k_match:
                    contradictions.append(Contradiction(
                        type=rule["type"],
                        insight=rule["insight"],
                        suggestion=rule["suggestion"],
                        severity="medium",
                    ))
            except Exception:
                logging.getLogger(__name__).warning(f"Caught unexpected error at cross_validator.py", exc_info=True)
                continue

        return contradictions
