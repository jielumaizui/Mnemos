# -*- coding: utf-8 -*-
"""
FeedbackScheduler — 反馈调度器

- 手动反馈：即时处理（权重翻倍）
- 隐式反馈：5 分钟缓冲后批量刷新
- 自观测：即时处理
"""

from __future__ import annotations

import threading
import time
import logging
from typing import Callable, Dict, List, Optional

from core.scoring.adaptive_scorer import Feedback, AdaptiveScorer
from core.scoring.feedback.feedback_fusion import FeedbackFusion
from core.scoring.feedback.fatigue_guard import FeedbackFatigueGuard

logger = logging.getLogger(__name__)


class FeedbackScheduler:
    """反馈调度器"""

    IMPLICIT_FLUSH_INTERVAL = 300  # 5 分钟

    def __init__(self, scorer: AdaptiveScorer):
        self._scorer = scorer
        self._fusion = FeedbackFusion()
        self._fatigue_guard = FeedbackFatigueGuard()
        self._implicit_buffer: List[Feedback] = []
        self._lock = threading.Lock()
        self._running = False
        self._flush_thread: Optional[threading.Thread] = None

    def start(self):
        """启动调度器"""
        self._running = True
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

    def stop(self):
        """停止调度器"""
        self._running = False
        if self._flush_thread:
            self._flush_thread.join(timeout=5)
        self._flush_implicit()

    def submit(self, feedback: Feedback) -> None:
        """提交反馈"""
        if feedback.source == "manual":
            # 手动反馈：疲劳保护 + 即时处理
            if not self._fatigue_guard.can_ask():
                logger.debug("[FeedbackScheduler] 疲劳保护，跳过手动反馈")
                return
            self._fatigue_guard.record_ask()
            # 权重翻倍
            feedback.weight *= 2.0
            self._scorer.feedback(feedback)

        elif feedback.source in ("implicit", "implicit_aggregated"):
            # 隐式反馈：缓冲
            with self._lock:
                self._implicit_buffer.append(feedback)

        elif feedback.source == "self_observation":
            # 自观测：即时处理
            self._scorer.feedback(feedback)

        else:
            self._scorer.feedback(feedback)

    def can_ask_manual(self) -> bool:
        """是否可以发起手动反馈"""
        return self._fatigue_guard.can_ask()

    def record_manual_ignore(self) -> None:
        """记录用户忽略手动反馈"""
        self._fatigue_guard.record_ignore()

    def _flush_loop(self):
        """定时刷新隐式反馈"""
        while self._running:
            time.sleep(self.IMPLICIT_FLUSH_INTERVAL)
            if not self._running:
                break
            self._flush_implicit()

    def _flush_implicit(self):
        """刷新隐式反馈缓冲区"""
        with self._lock:
            if not self._implicit_buffer:
                return
            buffer = self._implicit_buffer[:]
            self._implicit_buffer.clear()

        # 融合
        fused = self._fusion.fuse(buffer)
        for fb in fused.values():
            self._scorer.feedback(fb)

        logger.debug(f"[FeedbackScheduler] 刷新 {len(buffer)} 条隐式反馈 → {len(fused)} 条融合反馈")
