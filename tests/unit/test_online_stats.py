"""
OnlineStats — 增量统计量测试（Welford 算法）

覆盖:
- 空状态查询
- 单值更新
- 多值更新（均值、方差、标准差）
- min/max 追踪
- DimensionStats 字典封装
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.scoring.online_stats import OnlineStats, DimensionStats


def test_empty_stats():
    """空统计量应返回安全默认值"""
    s = OnlineStats()
    assert s.n == 0
    assert s.mean == 0.0
    assert s.variance == 0.0
    assert s.std == 0.0
    assert s.min == 0.0
    assert s.max == 0.0


def test_single_value():
    """单值更新后方差为零"""
    s = OnlineStats()
    s.update(42.0)
    assert s.n == 1
    assert s.mean == 42.0
    assert s.variance == 0.0
    assert s.min == 42.0
    assert s.max == 42.0


def test_multiple_values():
    """多值更新的均值和方差正确"""
    s = OnlineStats()
    values = [10.0, 20.0, 30.0, 40.0]
    for v in values:
        s.update(v)

    assert s.n == 4
    assert s.mean == 25.0
    # 样本方差 = sum((x-mean)^2) / (n-1) = 500/3 ≈ 166.67
    assert abs(s.variance - 166.6667) < 0.001
    assert abs(s.std - 12.9099) < 0.001
    assert s.min == 10.0
    assert s.max == 40.0


def test_dimension_stats_dict():
    """DimensionStats 字典封装正确"""
    ds = DimensionStats()
    ds.update("quality", 0.8)
    ds.update("quality", 0.9)
    ds.update("speed", 0.5)

    assert set(ds.dimensions) == {"quality", "speed"}
    assert ds.get("quality").n == 2
    assert abs(ds.get("quality").mean - 0.85) < 0.001
    assert ds.get("speed").n == 1
    assert ds.get("nonexistent") is None


def test_welford_numerical_stability():
    """Welford 算法数值稳定性：大数加小数不应丢失精度"""
    s = OnlineStats()
    s.update(1e9 + 1)
    s.update(1e9 + 2)
    s.update(1e9 + 3)
    assert abs(s.mean - (1e9 + 2)) < 0.1
    assert s.variance > 0
