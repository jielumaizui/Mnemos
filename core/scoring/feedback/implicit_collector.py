# -*- coding: utf-8 -*-
"""
ImplicitFeedbackCollector — 隐式反馈采集器

可采集信号：
  - on_search: 搜索查询（查询词+结果数）
  - on_reuse: 后续对话引用已有知识
  - on_dwell_time: 页面停留时间（Obsidian 限制，仅记录访问）
  - on_edit: Wiki 页面编辑（通过 git diff 检测）

不可采集（Obsidian 限制）：
  - 复制检测、星标/收藏、停留时间、快速关闭、跨 Agent 分享
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.scoring.adaptive_scorer import Feedback
from core.config import get_config

logger = logging.getLogger(__name__)


class ImplicitFeedbackCollector:
    """隐式反馈采集器"""

    def __init__(self):
        self._signals: List[Dict] = []

    def on_search(self, query: str, results_count: int, context: Optional[Dict] = None) -> Optional[Feedback]:
        """搜索事件"""
        # 搜索无结果 = 盲点信号
        if results_count == 0:
            return Feedback(
                dimension="blind_spot_score",
                expected=0.8,  # 高盲点
                actual=0.2,   # 当前低检测
                source="implicit",
                context={"query": query, "results_count": results_count, **(context or {})},
                weight=0.6,
            )
        # 搜索有结果但少 = 部分盲点
        elif results_count <= 2:
            return Feedback(
                dimension="blind_spot_score",
                expected=0.5,
                actual=0.3,
                source="implicit",
                context={"query": query, "results_count": results_count},
                weight=0.4,
            )

        self._signals.append({
            "type": "search", "query": query, "results_count": results_count,
            "timestamp": datetime.now().isoformat(),
        })
        return None

    def on_reuse(self, knowledge_id: str, session_id: str, context: Optional[Dict] = None) -> Feedback:
        """知识复用事件 — 被复用的知识质量确认"""
        return Feedback(
            dimension="distill_score",
            expected=0.8,  # 被复用 = 高价值
            actual=0.5,   # 默认
            source="implicit",
            context={"knowledge_id": knowledge_id, "session_id": session_id, **(context or {})},
            weight=0.6,
        )

    def on_edit(self, page_path: str, edit_type: str = "minor", context: Optional[Dict] = None) -> Optional[Feedback]:
        """Wiki 页面编辑事件"""
        weight = 0.8 if edit_type == "major" else 0.4
        return Feedback(
            dimension="quality_score",
            expected=0.9 if edit_type == "major" else 0.7,
            actual=0.5,
            source="implicit",
            context={"page_path": page_path, "edit_type": edit_type, **(context or {})},
            weight=weight,
        )

    def get_signals(self, limit: int = 100) -> List[Dict]:
        """获取采集的信号"""
        return self._signals[-limit:]
