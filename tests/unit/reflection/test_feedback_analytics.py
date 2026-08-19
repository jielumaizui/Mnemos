"""Legacy reflection feedback must never remain active calibration evidence."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from core.reflection.feedback_analytics import FeedbackAnalytics
from core.reflection.models import (
    FeedbackType,
    ImplicitFeedbackRecord,
    InsightSnapshot,
    ReflectionRecord,
    ReflectionTrigger,
    UserFeedback,
)


def _record(
    *,
    user_feedback=None,
    implicit_feedback=None,
    internal_validation=None,
) -> ReflectionRecord:
    record = ReflectionRecord(
        id="legacy-reflection",
        created_at=datetime.now(),
        trigger=ReflectionTrigger.NEW_PROJECT,
        mirror_dimensions=["attention"],
        insight=InsightSnapshot(
            summary="summary",
            key_points=["key point"],
            dimensions_involved=["attention"],
        ),
    )
    record.user_feedback = user_feedback
    record.implicit_feedback = implicit_feedback
    record.internal_validation = internal_validation
    return record


@pytest.mark.parametrize(
    "legacy_record",
    [
        _record(
            user_feedback=UserFeedback(feedback_type=FeedbackType.ACCURATE),
        ),
        _record(
            user_feedback=UserFeedback(feedback_type=FeedbackType.INACCURATE),
        ),
        _record(
            implicit_feedback=ImplicitFeedbackRecord(
                inferred_type=FeedbackType.IRRELEVANT,
                confidence=0.99,
                signals=["legacy implicit signal"],
            ),
        ),
        _record(internal_validation={"is_valid": True, "confidence": 1.0}),
    ],
    ids=(
        "legacy-explicit-positive",
        "legacy-explicit-negative",
        "legacy-implicit",
        "legacy-internal-validation",
    ),
)
def test_all_legacy_reflection_feedback_sources_are_quarantined(legacy_record):
    store = MagicMock()
    store.get_latest.return_value = [legacy_record]
    analytics = FeedbackAnalytics(store)

    assert analytics.effectiveness_by_dimension(days=30, min_samples=1) == {}
    assert analytics.effectiveness_by_trigger(days=30, min_samples=1) == {}
    assert analytics.trend_over_time(days=30) == []
    assert store.get_latest.call_count == 0


def test_legacy_feedback_quality_report_is_explicitly_empty():
    store = MagicMock()
    store.get_latest.return_value = [
        _record(
            user_feedback=UserFeedback(feedback_type=FeedbackType.ACCURATE),
            implicit_feedback=ImplicitFeedbackRecord(
                inferred_type=FeedbackType.IRRELEVANT,
                confidence=0.99,
                signals=["legacy implicit signal"],
            ),
            internal_validation={"is_valid": True, "confidence": 1.0},
        )
    ]

    report = FeedbackAnalytics(store).get_insight_quality_report(days=30)

    assert report["period_days"] == 30
    assert report["overall"] == {
        "total_with_feedback": 0,
        "positive": 0,
        "negative": 0,
        "accuracy_rate": 0.0,
    }
    assert report["dimensions"] == {
        "ranked": [],
        "problematic": [],
        "high_value": [],
    }
    assert report["triggers"] == {"ranked": []}
    assert report["trend"] == {"direction": "stable", "weekly_windows": []}
    assert store.get_latest.call_count == 0
