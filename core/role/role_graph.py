"""
Role Graph — 当前激活的"自我"

不是用户静态身份，而是当前会话/查询中激活的 role。
用于 Reflection Router：同一个 query，Builder 模式 vs Parent 模式
需要不同的 Reflection Capability 配置。

初始角色严格控制：
- builder（建造/重构/项目）
- parent（家庭/育儿/关系）
- career_explorer（职业/成长）
- learner（学习/读书/技能）
- default（日常对话）
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


class Role(str, Enum):
    """预定义角色集合"""

    BUILDER = "builder"
    PARENT = "parent"
    CAREER_EXPLORER = "career_explorer"
    LEARNER = "learner"
    DEFAULT = "default"


@dataclass
class RoleActivation:
    """
    角色激活结果

    - role: 激活的角色
    - confidence: 激活置信度 0-1
    - signals: 触发该角色的信号列表
    - scene_hint: 对 reflection router 的场景提示
    """

    role: Role = Role.DEFAULT
    confidence: float = 0.0
    signals: List[str] = field(default_factory=list)
    scene_hint: Optional[str] = None
    activated_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict:
        return {
            "role": self.role.value,
            "confidence": self.confidence,
            "signals": self.signals,
            "scene_hint": self.scene_hint,
            "activated_at": self.activated_at.isoformat(),
        }


class RoleGraph:
    """
    角色图 — 基于规则 + 近期上下文推断当前激活角色

    设计原则：
    - 轻量、可解释、不依赖外部 embedding API
    - 优先从 query 本身推断，再结合 recent_context 修正
    - default 角色置信度低，不触发 reflection
    """

    # 每个角色的关键词信号
    # 键：role，值：[(pattern, weight, signal_label), ...]
    ROLE_SIGNALS: Dict[Role, List[tuple]] = {
        Role.BUILDER: [
            # 中文关键词不使用 \b（Python 中文字符边界行为不一致），直接匹配
            (
                r"(?:重构|重写|架构|设计|实现|开发|代码|项目|系统|工具|自动化|部署|上线|迭代|产品|功能|模块|组件|API|服务)",
                1.0,
                "builder_keyword",
            ),
            (
                r"\b(?:refactor|rebuild|architecture|implement|develop|code|project|system|tool|deploy|product|feature|module|component)\b",  # noqa: E501
                1.0,
                "builder_keyword_en",
            ),
        ],
        Role.PARENT: [
            (
                r"(?:孩子|儿子|女儿|老婆|老公|妻子|丈夫|父母|爸妈|父亲|母亲|家庭|育儿|教育|作业|学校|老师|考试|叛逆|陪伴|家务)",
                1.0,
                "parent_keyword",
            ),
            (
                r"\b(?:kid|child|children|son|daughter|wife|husband|parent|family|school|homework|education)\b",  # noqa: E501
                1.0,
                "parent_keyword_en",
            ),
        ],
        Role.CAREER_EXPLORER: [
            (
                r"(?:职业|工作|晋升|管理|领导|团队|老板|同事|面试|跳槽|薪资|绩效|成长|发展|规划|行业|公司|业务|创业|副业)",
                1.0,
                "career_keyword",
            ),
            (
                r"\b(?:career|job|promotion|management|leader|team|interview|salary|performance|growth|planning|company|business|startup)\b",  # noqa: E501
                1.0,
                "career_keyword_en",
            ),
        ],
        Role.LEARNER: [
            (
                r"(?:学习|读书|课程|知识|技能|技术|语言|证书|考试|研究方法|论文|教材|笔记|理解|掌握|入门|进阶)",
                1.0,
                "learner_keyword",
            ),
            (
                r"\b(?:learn|study|read|book|course|knowledge|skill|technology|language|certification|exam|thesis|textbook)\b",  # noqa: E501
                1.0,
                "learner_keyword_en",
            ),
        ],
    }

    # 场景提示词，供 Reflection Router 参考
    ROLE_SCENE_HINTS: Dict[Role, str] = {
        Role.BUILDER: "project_or_build",
        Role.PARENT: "family_or_relationship",
        Role.CAREER_EXPLORER: "career_or_growth",
        Role.LEARNER: "learning_or_skill",
        Role.DEFAULT: "default",
    }

    def __init__(self):
        self._history: List[RoleActivation] = []

    def infer_role(
        self,
        query: str,
        recent_context: Optional[List[str]] = None,
    ) -> RoleActivation:
        """
        推断当前激活角色

        Args:
            query: 用户当前查询
            recent_context: 近期用户消息/观察摘要列表

        Returns:
            RoleActivation
        """
        recent_context = recent_context or []
        scores: Dict[Role, float] = {role: 0.0 for role in Role if role != Role.DEFAULT}
        signals: Dict[Role, List[str]] = {role: [] for role in Role if role != Role.DEFAULT}

        # 1. 从 query 中提取信号
        for role, patterns in self.ROLE_SIGNALS.items():
            for pattern, weight, label in patterns:
                matches = re.findall(pattern, query, re.IGNORECASE)
                if matches:
                    scores[role] += weight * len(matches)
                    signals[role].append(f"{label}: {matches[0]}")

        # 2. 从 recent_context 中累积信号（降低权重）
        context_weight = 0.3
        for ctx in recent_context[-10:]:
            if not ctx:
                continue
            for role, patterns in self.ROLE_SIGNALS.items():
                for pattern, weight, label in patterns:
                    matches = re.findall(pattern, ctx, re.IGNORECASE)
                    if matches:
                        scores[role] += weight * len(matches) * context_weight
                        if len(signals[role]) < 5:
                            signals[role].append(f"context_{label}: {matches[0]}")

        if not scores or max(scores.values()) <= 0:
            activation = RoleActivation(
                role=Role.DEFAULT,
                confidence=0.0,
                signals=[],
                scene_hint=self.ROLE_SCENE_HINTS[Role.DEFAULT],
            )
            self._history.append(activation)
            return activation

        # 3. 选择最高分角色
        best_role = max(scores, key=scores.get)  # type: ignore[arg-type]
        best_score = scores[best_role]

        # 4. 计算置信度（归一化）
        total_score = sum(scores.values())
        confidence = best_score / total_score if total_score > 0 else 0.0

        # 如果最高分没有显著领先，降级为 default
        if confidence < 0.4:
            activation = RoleActivation(
                role=Role.DEFAULT,
                confidence=round(confidence, 2),
                signals=signals[best_role][:3],
                scene_hint=self.ROLE_SCENE_HINTS[Role.DEFAULT],
            )
        else:
            activation = RoleActivation(
                role=best_role,
                confidence=round(min(confidence, 1.0), 2),
                signals=signals[best_role][:5],
                scene_hint=self.ROLE_SCENE_HINTS[best_role],
            )

        self._history.append(activation)
        return activation

    def get_history(self, limit: int = 20) -> List[RoleActivation]:
        """获取最近的激活历史"""
        return self._history[-limit:]

    def get_dominant_role(self, window: int = 10) -> Role:
        """最近 window 次激活中的主导角色"""
        recent = self._history[-window:]
        if not recent:
            return Role.DEFAULT
        counts: Dict[Role, int] = {}
        for act in recent:
            counts[act.role] = counts.get(act.role, 0) + 1
        return max(counts, key=counts.get)  # type: ignore[arg-type]
