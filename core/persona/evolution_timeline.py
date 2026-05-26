# -*- coding: utf-8 -*-
"""
PersonaEvolutionTimeline — 14 维度长期变化可视化

检测画像关键事件（倦怠信号、认知转变、价值翻转），
生成 Mermaid 时序图 + 事件时间线。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class EvolutionEvent:
    """演化事件"""
    date: str
    event_type: str  # burnout_signal / cognitive_shift / value_flip
    dimension: str
    detail: str
    severity: str = "medium"


# 追踪维度定义
TRACKED_DIMENSIONS = [
    ("专注深度", "energy.focus_depth"),
    ("启动难度", "energy.startup_difficulty"),
    ("续航模式", "energy.endurance_mode"),
    ("切换弹性", "energy.switching_flexibility"),
    ("抽象与具象", "cognitive.abstraction"),
    ("系统与单点", "cognitive.system_view"),
    ("质疑与信任", "cognitive.skepticism"),
    ("创造与优化", "cognitive.creativity"),
    ("演绎与归纳", "cognitive.deduction"),
    ("正确性与效率", "value.correctness_vs_efficiency"),
    ("深度与广度", "value.depth_vs_breadth"),
    ("完美与完成", "value.perfection_vs_completion"),
    ("创新与安全", "value.innovation_vs_safety"),
    ("自主与协作", "value.autonomy_vs_collaboration"),
]


class PersonaEvolutionTimeline:
    """画像演化时间线"""

    def __init__(self):
        self._snapshots: List[Dict] = []

    def add_snapshot(self, profile: Dict, date: str = None) -> None:
        """添加画像快照"""
        self._snapshots.append({
            "date": date or datetime.now().strftime("%Y-%m-%d"),
            "profile": profile,
        })

    def generate(self) -> str:
        """生成演化时间线报告

        Returns:
            Markdown 格式的报告（含 Mermaid 图表）
        """
        if len(self._snapshots) < 2:
            return "数据积累中（需要至少 2 个快照）\n"

        lines = ["# 画像演化时间线", ""]

        # 检测事件
        events = self._detect_events()
        if events:
            lines.append("## 关键事件")
            lines.append("")
            for ev in events:
                icon = "🔴" if ev.severity == "high" else "🟡"
                lines.append(f"- {icon} **{ev.date}** [{ev.event_type}] "
                           f"{ev.dimension}: {ev.detail}")
            lines.append("")

        # 维度变化表
        lines.append("## 维度变化")
        lines.append("")
        lines.append("| 维度 | 最早 | 最新 | 变化 |")
        lines.append("|------|------|------|------|")
        for label, attr_path in TRACKED_DIMENSIONS:
            first_val = self._get_value(self._snapshots[0]["profile"], attr_path)
            last_val = self._get_value(self._snapshots[-1]["profile"], attr_path)
            if first_val is not None and last_val is not None:
                delta = last_val - first_val
                arrow = "↑" if delta > 0.05 else "↓" if delta < -0.05 else "→"
                lines.append(f"| {label} | {first_val:.2f} | {last_val:.2f} | {arrow} {abs(delta):.2f} |")
        lines.append("")

        # Mermaid 图表
        lines.append("## 趋势图")
        lines.append("")
        lines.append(self._generate_mermaid_chart())

        return "\n".join(lines)

    def _detect_events(self) -> List[EvolutionEvent]:
        """检测画像关键事件"""
        events = []
        if len(self._snapshots) < 3:
            return events

        for i in range(1, len(self._snapshots)):
            prev = self._snapshots[i - 1]["profile"]
            curr = self._snapshots[i]["profile"]
            date = self._snapshots[i]["date"]

            # 倦怠信号：专注深度下降 >= 0.2
            focus_prev = self._get_value(prev, "energy.focus_depth")
            focus_curr = self._get_value(curr, "energy.focus_depth")
            if focus_prev is not None and focus_curr is not None and (focus_prev - focus_curr) >= 0.2:
                events.append(EvolutionEvent(
                    date=date, event_type="burnout_signal",
                    dimension="专注深度",
                    detail=f"专注深度从 {focus_prev:.2f} 降至 {focus_curr:.2f}",
                    severity="high",
                ))

            # 认知转变：抽象能力连续3版本上升
            if i >= 2:
                abs_vals = [
                    self._get_value(self._snapshots[j]["profile"], "cognitive.abstraction")
                    for j in range(max(0, i - 2), i + 1)
                ]
                if all(v is not None for v in abs_vals):
                    if abs_vals[0] < abs_vals[1] < abs_vals[2]:
                        total_change = abs_vals[2] - abs_vals[0]
                        if total_change > 0.15:
                            events.append(EvolutionEvent(
                                date=date, event_type="cognitive_shift",
                                dimension="抽象与具象",
                                detail=f"抽象能力持续上升 {total_change:.2f}",
                                severity="medium",
                            ))

            # 价值翻转：跨越 0.5 中点
            for label, attr_path in TRACKED_DIMENSIONS:
                if not attr_path.startswith("value."):
                    continue
                val_prev = self._get_value(prev, attr_path)
                val_curr = self._get_value(curr, attr_path)
                if val_prev and val_curr:
                    if (val_prev < 0.5 <= val_curr) or (val_prev >= 0.5 > val_curr):
                        events.append(EvolutionEvent(
                            date=date, event_type="value_flip",
                            dimension=label,
                            detail=f"从 {val_prev:.2f} 翻转为 {val_curr:.2f}",
                            severity="medium",
                        ))

        return events

    def _generate_mermaid_chart(self) -> str:
        """生成 Mermaid xychart-beta 时序图"""
        lines = ["```mermaid", "xychart-beta"]
        lines.append(f'  title "画像演化趋势"')
        dates = ", ".join(f'"{s["date"]}"' for s in self._snapshots[-6:])
        lines.append(f'  x-axis [{dates}]')

        for label, attr_path in TRACKED_DIMENSIONS[:5]:  # 最多5条线
            values = []
            for snap in self._snapshots[-6:]:
                val = self._get_value(snap["profile"], attr_path)
                values.append(f"{val:.2f}" if val is not None else "0.5")
            lines.append(f'  line [{", ".join(values)}]')

        lines.append("```")
        return "\n".join(lines)

    @staticmethod
    def _get_value(profile: Dict, attr_path: str) -> Optional[float]:
        """从嵌套字典中获取属性值"""
        parts = attr_path.split(".")
        current = profile
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        try:
            return float(current)
        except (TypeError, ValueError):
            return None
