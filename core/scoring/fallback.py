# -*- coding: utf-8 -*-
"""
fallback.py — 评分降级策略

当 ML 模型失败（未训练/异常/准确率不足）时，自动降级到规则评分，
并记录降级事件供运维分析。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class DegradationEvent:
    """降级事件记录"""
    dimension: str
    reason: str                      # 降级原因
    rule_score: float                # 规则评分兜底值
    ml_error: Optional[str] = None   # ML 异常信息
    timestamp: datetime = field(default_factory=datetime.now)


class ScorerFallback:
    """评分降级管理器"""

    def __init__(self):
        self._events: list = []
        self._consecutive_failures: Dict[str, int] = {}
        self._max_consecutive = 3       # 连续失败 3 次后锁定降级

    @contextmanager
    def guard(
        self,
        dimension: str,
        rule_fallback_fn: Callable[[], float],
    ):
        """
        降级保护上下文管理器。

        用法：
            with fallback.guard("memos", lambda: rule_scorer.score(item)) as score_fn:
                result = score_fn()  # 尝试 ML 评分
        """
        ml_failed = False
        ml_error = None
        rule_score = rule_fallback_fn()

        def _try_ml(ml_fn: Callable[[], Any]):
            nonlocal ml_failed, ml_error
            try:
                return ml_fn()
            except Exception as e:
                ml_failed = True
                ml_error = str(e)
                self._record_failure(dimension)
                logger.warning(
                    f"[ScorerFallback] {dimension} ML failed, "
                    f"falling back to rule={rule_score:.3f}: {e}"
                )
                return rule_score

        yield _try_ml

        if ml_failed:
            self._events.append(DegradationEvent(
                dimension=dimension,
                reason="ml_exception",
                rule_score=rule_score,
                ml_error=ml_error,
            ))

    def should_degrade(self, dimension: str) -> bool:
        """判断是否应降级（连续失败过多）"""
        return self._consecutive_failures.get(dimension, 0) >= self._max_consecutive

    def reset_failure(self, dimension: str) -> None:
        """重置失败计数（ML 恢复成功时调用）"""
        self._consecutive_failures[dimension] = 0

    def get_events(self, since: Optional[datetime] = None) -> list:
        """获取降级事件历史"""
        if since is None:
            return self._events[:]
        return [e for e in self._events if e.timestamp >= since]

    def _record_failure(self, dimension: str) -> None:
        self._consecutive_failures[dimension] = self._consecutive_failures.get(dimension, 0) + 1
