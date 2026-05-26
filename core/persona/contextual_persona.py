# -*- coding: utf-8 -*-
"""
ContextualPersona — 工作/个人/学习情境隔离

根据 working_dir 和 session_tags 分离信号，为每个情境生成独立画像。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# 情境检测模式
CONTEXT_PATTERNS = {
    "work": {
        "dir_patterns": ["/work/", "/company/", "/project/", "/src/"],
        "tag_patterns": ["work", "company", "meeting", "review"],
    },
    "personal": {
        "dir_patterns": ["/personal/", "/side-project/", "/hobby/"],
        "tag_patterns": ["personal", "side-project", "hobby", "experiment"],
    },
    "study": {
        "dir_patterns": ["/learning/", "/course/", "/book/", "/tutorial/"],
        "tag_patterns": ["learning", "study", "course", "reading"],
    },
}


class ContextualPersona:
    """情境隔离画像"""

    # 价值维度用于跨情境对比
    COMPARISON_DIMENSIONS = [
        "correctness_vs_efficiency",
        "depth_vs_breadth",
        "perfection_vs_completion",
        "innovation_vs_safety",
    ]

    def __init__(self):
        self._context_signals: Dict[str, List[Dict]] = {
            "work": [], "personal": [], "study": [], "default": [],
        }

    def detect_context(self, working_dir: str = "", session_tags: List[str] = None) -> str:
        """检测当前情境

        Args:
            working_dir: 工作目录路径
            session_tags: 会话标签列表

        Returns:
            "work" / "personal" / "study" / "default"
        """
        session_tags = session_tags or []
        dir_lower = (working_dir or "").lower()
        tags_lower = [t.lower() for t in session_tags]

        for context_name, patterns in CONTEXT_PATTERNS.items():
            # 目录匹配
            for pattern in patterns["dir_patterns"]:
                if pattern.lower() in dir_lower:
                    return context_name
            # 标签匹配
            for pattern in patterns["tag_patterns"]:
                if pattern.lower() in tags_lower:
                    return context_name

        return "default"

    def add_signal(self, signal: Dict, context: str = None) -> None:
        """添加信号到指定情境"""
        if context is None:
            context = self.detect_context(
                signal.get("working_dir", ""),
                signal.get("tags", []),
            )
        self._context_signals.setdefault(context, []).append(signal)

    def get_profile(self, context: str = "default", days: int = 90) -> Dict:
        """获取指定情境的画像

        如果该情境信号 < 10，回退到 default 画像。
        """
        signals = self._context_signals.get(context, [])
        if len(signals) < 10:
            signals = self._context_signals.get("default", [])
            context = "default"

        if not signals:
            return {"context": context, "signal_count": 0, "insufficient": True}

        # 简单画像分析
        profile = {
            "context": context,
            "signal_count": len(signals),
            "insufficient": False,
        }

        # 统计价值维度
        for dim in self.COMPARISON_DIMENSIONS:
            values = [s.get(dim) for s in signals if dim in s]
            if values:
                profile[dim] = sum(values) / len(values)
            else:
                profile[dim] = 0.5

        return profile

    def generate_comparison(self) -> str:
        """生成跨情境对比报告"""
        lines = ["## 情境画像对比", ""]
        lines.append("| 维度 | 工作 | 个人 | 学习 |")
        lines.append("|------|------|------|------|")

        has_difference = False
        for dim in self.COMPARISON_DIMENSIONS:
            values = []
            for ctx in ["work", "personal", "study"]:
                profile = self.get_profile(ctx)
                val = profile.get(dim, 0.5)
                values.append(val)

            lines.append(f"| {dim} | {values[0]:.2f} | {values[1]:.2f} | {values[2]:.2f} |")

            # 检测差异 > 0.2
            if max(values) - min(values) > 0.2:
                has_difference = True

        if has_difference:
            lines.append("")
            lines.append("> 不同情境间存在显著差异（>0.2），建议针对性调整工作方式。")

        return "\n".join(lines)
