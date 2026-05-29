# -*- coding: utf-8 -*-
"""
PersonaDrivenDialogueStrategy — 画像驱动的 Agent 提示注入

根据用户画像数据，将策略句子注入 Agent 系统提示。
每会话 200-500 tokens，仅注入 top 3-5 最相关策略。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional
logger = logging.getLogger(__name__)



class PersonaDrivenDialogueStrategy:
    """画像驱动的对话策略"""

    STRATEGY_TEMPLATES = {
        # 能量维度
        "startup_difficulty_high": {
            "condition": lambda p: p.get("startup_difficulty", 0.5) > 0.6,
            "strategy": "用户启动较慢，提供具体的下一步行动建议而非框架性指导",
        },
        "endurance_mode_burst": {
            "condition": lambda p: p.get("endurance_mode", "") == "burst",
            "strategy": "用户偏好短时高强度工作，将长任务拆解为可独立完成的里程碑",
        },
        # 认知维度
        "abstraction_high": {
            "condition": lambda p: p.get("abstraction", 0.5) > 0.6,
            "strategy": "用户偏好抽象原理，先讲 Why 和原理，再给具体示例",
        },
        "abstraction_low": {
            "condition": lambda p: p.get("abstraction", 0.5) < 0.4,
            "strategy": "用户偏好具体示例，先给可运行的代码/示例，再解释原理",
        },
        "skepticism_high": {
            "condition": lambda p: p.get("skepticism", 0.5) > 0.6,
            "strategy": "用户倾向质疑，主动说明方案的局限性和已知缺陷",
        },
        "system_view_high": {
            "condition": lambda p: p.get("system_view", 0.5) > 0.6,
            "strategy": "用户有系统视角，解释各组件之间的关系和整体影响",
        },
        # 价值维度
        "correctness_high": {
            "condition": lambda p: p.get("correctness_vs_efficiency", 0.5) > 0.6,
            "strategy": "用户重视正确性，提供验证步骤和测试用例",
        },
        "efficiency_high": {
            "condition": lambda p: p.get("correctness_vs_efficiency", 0.5) < 0.4,
            "strategy": "用户重视效率，提供最小可行方案，后续再迭代完善",
        },
        "innovation_high": {
            "condition": lambda p: p.get("innovation_vs_safety", 0.5) > 0.6,
            "strategy": "用户偏好创新方案，提供新思路但标注风险",
        },
        "safety_high": {
            "condition": lambda p: p.get("innovation_vs_safety", 0.5) < 0.4,
            "strategy": "用户偏好稳妥方案，优先推荐经过验证的成熟方案",
        },
    }

    BLINDSPOT_TEMPLATES = {
        "framing_blindspot": {
            "condition": lambda bs: bs.get("framing_rigidity", 0) > 0.6,
            "strategy": "用户可能受问题框架限制，主动挑战问题假设和前提",
        },
        "option_gap_blindspot": {
            "condition": lambda bs: bs.get("option_gap", 0) > 0.6,
            "strategy": "用户可能遗漏选项，主动提供第三种替代方案",
        },
    }

    MAX_STRATEGIES = 5

    def adapt_prompt(self, base_prompt: str, preference_profile: Dict = None,
                     blindspot_profile: Dict = None) -> str:
        """将画像策略注入 Agent 提示

        Args:
            base_prompt: 原始提示
            preference_profile: 行为偏好画像
            blindspot_profile: 盲区画像

        Returns:
            注入策略后的提示
        """
        strategies = []

        # 从偏好画像提取策略
        if preference_profile:
            for key, template in self.STRATEGY_TEMPLATES.items():
                try:
                    if template["condition"](preference_profile):
                        strategies.append(template["strategy"])
                except Exception:
                    logging.getLogger(__name__).warning(f"Caught unexpected error at dialogue_strategy.py", exc_info=True)
                    continue

        # 从盲区画像提取策略
        if blindspot_profile:
            for key, template in self.BLINDSPOT_TEMPLATES.items():
                try:
                    if template["condition"](blindspot_profile):
                        strategies.append(template["strategy"])
                except Exception:
                    logging.getLogger(__name__).warning(f"Caught unexpected error at dialogue_strategy.py", exc_info=True)
                    continue

        if not strategies:
            return base_prompt

        # 限制策略数量
        strategies = strategies[:self.MAX_STRATEGIES]

        # 注入到提示末尾
        strategy_block = "\n---\n[画像驱动策略]\n"
        strategy_block += "\n".join(f"- {s}" for s in strategies)
        strategy_block += "\n---\n"

        return base_prompt + strategy_block
