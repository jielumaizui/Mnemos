# -*- coding: utf-8 -*-
"""
SelfObservation — 系统自观测反馈

零打扰，自动收集内部质量信号：
  - on_sync_completed: 同步失败→health_score 降低
  - on_search_failed: 搜索碎片→fragmentation_score 升高
  - on_cluster_quality_low: 聚类内离散度高→触发重训练
"""

from __future__ import annotations

import logging
from typing import Optional

from core.scoring.adaptive_scorer import Feedback

logger = logging.getLogger(__name__)


class SelfObservation:
    """系统自观测"""

    def __init__(self):
        self._sync_failures = 0
        self._sync_total = 0
        self._search_failures = 0
        self._search_total = 0

    def on_sync_completed(self, success: bool, latency_ms: float = 0) -> Optional[Feedback]:
        """同步完成事件"""
        self._sync_total += 1
        if not success:
            self._sync_failures += 1

        if self._sync_total >= 10:
            fail_rate = self._sync_failures / self._sync_total
            if fail_rate > 0.2:
                return Feedback(
                    dimension="health_score",
                    expected=0.8,
                    actual=max(0.0, 1.0 - fail_rate),
                    source="self_observation",
                    context={"fail_rate": fail_rate, "total": self._sync_total},
                    weight=0.3,
                )
        return None

    def on_search_failed(self, query: str, partial_results: int = 0) -> Optional[Feedback]:
        """搜索失败事件"""
        self._search_total += 1
        if partial_results == 0:
            self._search_failures += 1

        if self._search_total >= 5 and self._search_failures / self._search_total > 0.3:
            return Feedback(
                dimension="fragmentation_score",
                expected=0.3,  # 期望低碎片
                actual=0.7,   # 实际高碎片
                source="self_observation",
                context={"query": query, "failure_rate": self._search_failures / self._search_total},
                weight=0.3,
            )
        return None

    def on_cluster_quality_low(self, dimension: str, silhouette_score: float) -> Optional[Feedback]:
        """聚类质量低事件"""
        if silhouette_score < 0.2:
            return Feedback(
                dimension=dimension,
                expected=0.7,
                actual=silhouette_score,
                source="self_observation",
                context={"silhouette_score": silhouette_score},
                weight=0.3,
            )
        return None

    @property
    def stats(self) -> dict:
        return {
            "sync_failures": self._sync_failures,
            "sync_total": self._sync_total,
            "search_failures": self._search_failures,
            "search_total": self._search_total,
        }
