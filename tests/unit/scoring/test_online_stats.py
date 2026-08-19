# -*- coding: utf-8 -*-
"""Unit tests for core.scoring.online_stats."""

import math

import pytest

from core.scoring.online_stats import DimensionStats, OnlineStats


class TestOnlineStats:
    def test_empty_stats_safe_defaults(self):
        s = OnlineStats()
        assert s.n == 0
        assert s.mean == pytest.approx(0.0)
        assert s.variance == pytest.approx(0.0)
        assert s.std == pytest.approx(0.0)
        assert s.min == pytest.approx(0.0)
        assert s.max == pytest.approx(0.0)

    def test_single_value_variance_is_zero(self):
        s = OnlineStats()
        s.update(42.0)
        assert s.n == 1
        assert s.mean == pytest.approx(42.0)
        assert s.variance == pytest.approx(0.0)
        assert s.min == pytest.approx(42.0)
        assert s.max == pytest.approx(42.0)

    def test_mean_variance_std_multiple_values(self):
        s = OnlineStats()
        for v in [10.0, 20.0, 30.0, 40.0]:
            s.update(v)
        assert s.n == 4
        assert s.mean == pytest.approx(25.0)
        assert s.variance == pytest.approx(500.0 / 3.0, abs=1e-4)
        assert s.std == pytest.approx(math.sqrt(500.0 / 3.0), abs=1e-4)
        assert s.min == pytest.approx(10.0)
        assert s.max == pytest.approx(40.0)

    def test_outlier_detection_requires_minimum_samples(self):
        s = OnlineStats()
        for v in range(9):
            s.update(float(v))
        assert s.n == 9
        assert not s.is_outlier(1e6)

    def test_outlier_detection_flags_extreme_value(self):
        s = OnlineStats()
        for v in range(10):
            s.update(float(v))
        assert s.is_outlier(100.0)
        assert not s.is_outlier(5.0)

    def test_merge_combines_two_stats(self):
        s1 = OnlineStats()
        for v in [1.0, 2.0, 3.0]:
            s1.update(v)
        s2 = OnlineStats()
        for v in [4.0, 5.0, 6.0]:
            s2.update(v)

        merged = s1.merge(s2)
        assert merged.n == 6
        assert merged.mean == pytest.approx(3.5)
        assert merged.variance == pytest.approx(3.5, abs=1e-4)
        assert merged.min == pytest.approx(1.0)
        assert merged.max == pytest.approx(6.0)

    def test_merge_with_empty_stats(self):
        s1 = OnlineStats()
        for v in [1.0, 2.0, 3.0]:
            s1.update(v)
        s2 = OnlineStats()
        merged = s1.merge(s2)
        assert merged.n == 3
        assert merged.mean == pytest.approx(2.0)


class TestDimensionStats:
    def test_update_and_get(self):
        ds = DimensionStats()
        ds.update("quality", 0.8)
        ds.update("quality", 0.9)
        ds.update("speed", 0.5)

        assert set(ds.dimensions) == {"quality", "speed"}
        quality = ds.get("quality")
        assert quality.n == 2
        assert quality.mean == pytest.approx(0.85)
        assert ds.get("missing") is None

    def test_check_drift_flags_outlier(self):
        ds = DimensionStats()
        for v in range(10):
            ds.update("quality", float(v))
        assert ds.check_drift("quality", 100.0)
        assert not ds.check_drift("quality", 5.0)

    def test_check_drift_missing_or_insufficient_data(self):
        ds = DimensionStats()
        ds.update("quality", 1.0)
        assert not ds.check_drift("quality", 100.0)
        assert not ds.check_drift("missing", 100.0)
