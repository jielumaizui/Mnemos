# -*- coding: utf-8 -*-
"""
OnlineStats — Welford 算法增量均值/方差

O(1) 内存，增量更新。
用于特征漂移检测（3-sigma 规则）和模型监控。
"""

from __future__ import annotations

import math
from typing import Dict, Optional


class OnlineStats:
    """增量统计量（Welford 算法）"""

    def __init__(self):
        self._n: int = 0
        self._mean: float = 0.0
        self._m2: float = 0.0
        self._min: float = float("inf")
        self._max: float = float("-inf")

    def update(self, value: float) -> None:
        """增量更新（Welford）"""
        self._n += 1
        delta = value - self._mean
        self._mean += delta / self._n
        delta2 = value - self._mean
        self._m2 += delta * delta2
        self._min = min(self._min, value)
        self._max = max(self._max, value)

    @property
    def n(self) -> int:
        return self._n

    @property
    def mean(self) -> float:
        return self._mean

    @property
    def variance(self) -> float:
        if self._n < 2:
            return 0.0
        return self._m2 / (self._n - 1)

    @property
    def std(self) -> float:
        return math.sqrt(self.variance)

    @property
    def min(self) -> float:
        return self._min if self._n > 0 else 0.0

    @property
    def max(self) -> float:
        return self._max if self._n > 0 else 0.0

    def is_outlier(self, value: float, sigma: float = 3.0) -> bool:
        """3-sigma 异常检测"""
        if self._n < 10:
            return False
        lower = self._mean - sigma * self.std
        upper = self._mean + sigma * self.std
        return value < lower or value > upper

    def merge(self, other: OnlineStats) -> OnlineStats:
        """合并两个统计量（并行计算用）"""
        result = OnlineStats()
        result._n = self._n + other._n
        if result._n == 0:
            return result
        delta = other._mean - self._mean
        result._mean = (self._n * self._mean + other._n * other._mean) / result._n
        result._m2 = self._m2 + other._m2 + delta * delta * self._n * other._n / result._n
        result._min = min(self._min, other._min)
        result._max = max(self._max, other._max)
        return result

    def to_dict(self) -> Dict:
        return {
            "n": self._n, "mean": self._mean, "variance": self.variance,
            "std": self.std, "min": self._min, "max": self._max,
        }


class DimensionStats:
    """多维度统计量管理"""

    def __init__(self):
        self._dims: Dict[str, OnlineStats] = {}

    def update(self, dimension: str, value: float) -> None:
        if dimension not in self._dims:
            self._dims[dimension] = OnlineStats()
        self._dims[dimension].update(value)

    def get(self, dimension: str) -> Optional[OnlineStats]:
        return self._dims.get(dimension)

    def check_drift(self, dimension: str, value: float, sigma: float = 3.0) -> bool:
        """检测特征漂移"""
        stats = self._dims.get(dimension)
        if not stats or stats.n < 10:
            return False
        return stats.is_outlier(value, sigma)

    @property
    def dimensions(self) -> list:
        return list(self._dims.keys())
