# -*- coding: utf-8 -*-
"""
FeedbackSignalProcessor — 反馈信号处理器

5 事件滑动窗口聚合，去除极值。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from core.scoring.adaptive_scorer import Feedback


class FeedbackSignalProcessor:
    """反馈信号处理器"""

    WINDOW_SIZE = 5

    def __init__(self):
        self._windows: Dict[str, List[Feedback]] = {}

    def add(self, feedback: Feedback) -> Optional[Feedback]:
        """添加反馈信号，窗口满时返回聚合结果"""
        dim = feedback.dimension
        if dim not in self._windows:
            self._windows[dim] = []

        self._windows[dim].append(feedback)

        if len(self._windows[dim]) >= self.WINDOW_SIZE:
            return self._aggregate(dim)
        return None

    def _aggregate(self, dimension: str) -> Feedback:
        """聚合窗口内的反馈信号"""
        window = self._windows[dimension]

        # 去除极值：去掉最高和最低的 expected
        if len(window) > 3:
            sorted_by_expected = sorted(window, key=lambda f: f.expected)
            trimmed = sorted_by_expected[1:-1]
        else:
            trimmed = window

        # 加权平均
        total_weight = sum(f.weight for f in trimmed)
        avg_expected = sum(f.expected * f.weight for f in trimmed) / total_weight if total_weight > 0 else 0.5
        avg_actual = sum(f.actual * f.weight for f in trimmed) / total_weight if total_weight > 0 else 0.5

        # 清空窗口
        self._windows[dimension] = []

        return Feedback(
            dimension=dimension,
            expected=avg_expected,
            actual=avg_actual,
            source="implicit_aggregated",
            weight=0.6,
            context={"window_size": len(window), "trimmed_size": len(trimmed)},
        )
