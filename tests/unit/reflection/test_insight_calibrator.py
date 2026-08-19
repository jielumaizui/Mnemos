"""Tests for core.reflection.insight_calibrator."""

from unittest.mock import MagicMock

import pytest

from core.reflection.feedback_analytics import DimensionEffectiveness
from core.reflection.insight_calibrator import InsightCalibrator
from core.reflection.insight_generator import InsightResult


def _make_analytics(dim_eff=None):
    analytics = MagicMock()
    analytics.effectiveness_by_dimension.return_value = dim_eff or {}
    return analytics


def test_get_calibration_params_default_when_no_data():
    calibrator = InsightCalibrator(feedback_analytics=_make_analytics({}))
    params = calibrator.get_calibration_params(days=30)

    assert params.confidence_threshold == pytest.approx(0.5)
    assert params.skip_dimensions == []
    assert params.boost_dimensions == []
    assert params.dimension_weights == {
        "attention": 1.0,
        "decisions": 1.0,
        "actions": 1.0,
        "time": 1.0,
        "stress": 1.0,
        "relationships": 1.0,
        "growth": 1.0,
    }
    assert params.calibrated_at


def test_get_calibration_params_adjusts_high_quality_dimension():
    dim_eff = {
        "attention": DimensionEffectiveness(
            dimension="attention",
            total=10,
            positive=9,
            negative=1,
            accuracy_rate=0.9,
            response_rate=1.0,
            trend="stable",
        ),
    }
    calibrator = InsightCalibrator(feedback_analytics=_make_analytics(dim_eff))
    params = calibrator.get_calibration_params(days=30)

    assert params.dimension_weights["attention"] > 1.0
    assert "attention" in params.boost_dimensions
    assert params.skip_dimensions == []


def test_get_calibration_params_adjusts_low_quality_dimension():
    dim_eff = {
        "stress": DimensionEffectiveness(
            dimension="stress",
            total=10,
            positive=2,
            negative=8,
            accuracy_rate=0.2,
            response_rate=1.0,
            trend="stable",
        ),
    }
    calibrator = InsightCalibrator(feedback_analytics=_make_analytics(dim_eff))
    params = calibrator.get_calibration_params(days=30)

    assert params.dimension_weights["stress"] < 1.0
    assert "stress" in params.skip_dimensions
    assert params.boost_dimensions == []


def test_get_calibration_params_applies_smoothing():
    dim_eff = {
        "attention": DimensionEffectiveness(
            dimension="attention",
            total=10,
            positive=9,
            negative=1,
            accuracy_rate=0.9,
            response_rate=1.0,
            trend="stable",
        ),
    }
    calibrator = InsightCalibrator(feedback_analytics=_make_analytics(dim_eff))
    params1 = calibrator.get_calibration_params(days=30)
    params2 = calibrator.get_calibration_params(days=30)

    # Same cache key within the same hour should return the same object
    assert params1 is params2


def test_get_calibration_params_force_refresh_ignores_cache():
    dim_eff = {
        "attention": DimensionEffectiveness(
            dimension="attention",
            total=10,
            positive=9,
            negative=1,
            accuracy_rate=0.9,
            response_rate=1.0,
            trend="stable",
        ),
    }
    calibrator = InsightCalibrator(feedback_analytics=_make_analytics(dim_eff))
    params1 = calibrator.get_calibration_params(days=30)
    params2 = calibrator.get_calibration_params(days=30, force_refresh=True)

    assert params1 is not params2
    assert params1.dimension_weights == params2.dimension_weights


def test_get_calibration_params_confidence_threshold_inverse_to_accuracy():
    low_accuracy = {
        "attention": DimensionEffectiveness(
            dimension="attention",
            total=10,
            positive=2,
            negative=8,
            accuracy_rate=0.2,
            response_rate=1.0,
            trend="stable",
        ),
    }
    high_accuracy = {
        "attention": DimensionEffectiveness(
            dimension="attention",
            total=10,
            positive=9,
            negative=1,
            accuracy_rate=0.9,
            response_rate=1.0,
            trend="stable",
        ),
    }
    low_cal = InsightCalibrator(feedback_analytics=_make_analytics(low_accuracy))
    high_cal = InsightCalibrator(feedback_analytics=_make_analytics(high_accuracy))

    low_threshold = low_cal.get_calibration_params(days=30).confidence_threshold
    high_threshold = high_cal.get_calibration_params(days=30).confidence_threshold

    assert low_threshold > high_threshold


def test_apply_to_insight_result_lowers_confidence_for_skipped_dimensions():
    dim_eff = {
        "stress": DimensionEffectiveness(
            dimension="stress",
            total=10,
            positive=2,
            negative=8,
            accuracy_rate=0.2,
            response_rate=1.0,
            trend="stable",
        ),
    }
    calibrator = InsightCalibrator(feedback_analytics=_make_analytics(dim_eff))
    insight = InsightResult(
        summary="summary",
        key_points=["kp"],
        dimensions_involved=["stress", "attention"],
        confidence=0.8,
    )
    result = calibrator.apply_to_insight_result(insight, days=30)

    assert result.confidence == pytest.approx(0.56)
    assert "stress" in result.calibration_note
    assert "谨慎参考" in result.calibration_note


def test_apply_to_insight_result_unchanged_for_non_skipped_dimensions():
    dim_eff = {
        "stress": DimensionEffectiveness(
            dimension="stress",
            total=10,
            positive=2,
            negative=8,
            accuracy_rate=0.2,
            response_rate=1.0,
            trend="stable",
        ),
    }
    calibrator = InsightCalibrator(feedback_analytics=_make_analytics(dim_eff))
    insight = InsightResult(
        summary="summary",
        key_points=["kp"],
        dimensions_involved=["attention"],
        confidence=0.8,
    )
    result = calibrator.apply_to_insight_result(insight, days=30)

    assert result.confidence == pytest.approx(0.8)
    assert not hasattr(result, "calibration_note") or not result.calibration_note


def test_get_generation_hints_and_weighted_params():
    dim_eff = {
        "attention": DimensionEffectiveness(
            dimension="attention",
            total=10,
            positive=9,
            negative=1,
            accuracy_rate=0.9,
            response_rate=1.0,
            trend="improving",
        ),
    }
    calibrator = InsightCalibrator(feedback_analytics=_make_analytics(dim_eff))

    hints = calibrator.get_generation_hints(days=30)
    assert "attention" in hints
    assert "提升" in hints

    weights = calibrator.get_weighted_mirror_params(days=30)
    assert weights["attention"] > 1.0


def test_calibration_params_to_dict():
    calibrator = InsightCalibrator(feedback_analytics=_make_analytics({}))
    params = calibrator.get_calibration_params(days=30)
    d = params.to_dict()
    assert "dimension_weights" in d
    assert "confidence_threshold" in d
    assert "skip_dimensions" in d
    assert "boost_dimensions" in d
    assert "generation_hints" in d
    assert "calibrated_at" in d
