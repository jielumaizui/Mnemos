"""Declarative persona-dimension rules for behavior prompts."""

from __future__ import annotations

from typing import Any, List, Tuple


# 画像维度映射规则：(维度名, 高阈值文案, 低阈值文案)
_ENERGY_RULES: List[Tuple[str, str, str]] = [
    (
        "focus_depth",
        "- 用户专注深度高：提供结构化、层次化的深度回复，避免碎片化信息",
        "- 用户专注深度低：提供简短、可快速消化的信息，多用列表和要点",
    ),
    (
        "startup_difficulty",
        "- 用户启动难度高：主动提供框架、模板或选项，降低决策成本",
        "- 用户启动容易：可以用开放性问题开场，给用户更多探索空间",
    ),
    (
        "switching_flexibility",
        "- 用户切换弹性高：允许话题自然切换，不必强行锁定当前主题",
        "- 用户切换弹性低：坚持当前主线，切换话题时明确提示和确认",
    ),
]

_COGNITIVE_RULES: List[Tuple[str, str, str]] = [
    (
        "abstraction",
        "- 用户偏抽象思维：先说原理/框架，再用案例佐证",
        "- 用户偏具象思维：先给具体案例，再归纳原理",
    ),
    (
        "system_view",
        "- 用户偏好系统视角：先给全貌和关联，再深入细节",
        "- 用户偏好单点视角：聚焦当前问题，全局背景简要提及",
    ),
    (
        "skepticism",
        "- 用户质疑倾向强：主动展示推理过程、证据和局限性",
        "- 用户信任倾向强：直接给结论和建议，不必过度解释前提",
    ),
]

_VALUE_RULES: List[Tuple[str, str, str]] = [
    (
        "correctness_vs_efficiency",
        "- 用户重视正确性：确保信息准确，不确定时明确说明",
        "- 用户重视效率：快速给出可行方案，不必追求完美",
    ),
    (
        "perfection_vs_completion",
        "- 用户追求完美：提供详尽、完整的方案，考虑边界情况",
        "- 用户追求完成：先给 MVP 方案，细节后续补充",
    ),
    (
        "depth_vs_breadth",
        "- 用户偏好深度：深入一个点，不必面面俱到",
        "- 用户偏好广度：提供多种选择和视角，不必深入每个细节",
    ),
    (
        "action_vs_analysis",
        "- 用户偏行动优先：需求明确时先执行和验证，分析保持必要最小量",
        "- 用户偏分析优先：行动前补足关键证据，但避免重复确认已知事实",
    ),
    (
        "autonomy_vs_collaboration",
        "- 用户偏自主：直接给出可执行结论，减少反复确认和协作式提问",
        "- 用户偏协作：多使用建议性、共创式表达，邀请用户一起决策",
    ),
]


def _append_dimension_lines(
    lines: List[str],
    layer: Any,
    insufficient: set[str],
    rules: List[Tuple[str, str, str]],
) -> None:
    """按规则把单个画像层的维度提示追加到 lines。"""
    for dimension, high_prompt, low_prompt in rules:
        if dimension in insufficient:
            continue
        value = getattr(layer, dimension)
        if value > 0.6:
            lines.append(high_prompt)
        elif value < 0.4:
            lines.append(low_prompt)
