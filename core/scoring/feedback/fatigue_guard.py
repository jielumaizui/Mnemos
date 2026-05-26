# -*- coding: utf-8 -*-
"""
FeedbackFatigueGuard — 反馈疲劳保护

- 每天最多 3 次手动反馈
- 最少 30 分钟间隔
- 忽略后 2 小时冷却
"""

from __future__ import annotations

import time
from typing import Optional

from core.config import get_config


class FeedbackFatigueGuard:
    """反馈疲劳保护"""

    def __init__(self):
        self._config = get_config()
        self._max_daily = self._config.get("scoring.feedback_fatigue_max_daily", 3)
        self._min_interval_minutes = self._config.get("scoring.feedback_fatigue_min_interval_minutes", 30)
        self._ignore_cooldown_hours = self._config.get("scoring.feedback_fatigue_ignore_cooldown_hours", 2)

        self._daily_count = 0
        self._last_ask_time: float = 0
        self._last_ignore_time: float = 0
        self._day_start: float = time.time()

    def can_ask(self) -> bool:
        """是否可以发起手动反馈请求"""
        now = time.time()

        # 重置日计数
        if now - self._day_start > 86400:
            self._daily_count = 0
            self._day_start = now

        # 每日上限
        if self._daily_count >= self._max_daily:
            return False

        # 最小间隔
        if now - self._last_ask_time < self._min_interval_minutes * 60:
            return False

        # 忽略冷却
        if now - self._last_ignore_time < self._ignore_cooldown_hours * 3600:
            return False

        return True

    def record_ask(self) -> None:
        """记录发起反馈请求"""
        self._daily_count += 1
        self._last_ask_time = time.time()

    def record_ignore(self) -> None:
        """记录用户忽略反馈"""
        self._last_ignore_time = time.time()

    @property
    def daily_count(self) -> int:
        return self._daily_count

    @property
    def remaining_today(self) -> int:
        return max(0, self._max_daily - self._daily_count)
