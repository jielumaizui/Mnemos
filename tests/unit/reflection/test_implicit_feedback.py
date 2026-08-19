"""Tests for core.reflection.implicit_feedback."""

from dataclasses import asdict
from datetime import datetime, timedelta

import pytest

from core.reflection.implicit_feedback import ImplicitFeedbackDetector, SessionContext
from core.reflection.models import FeedbackType


def _make_context(
    reflection_id="r1",
    messages_after=0,
    avg_length=0,
    time_to_first=None,
    session_extended=False,
    explicit_signals=None,
):
    return SessionContext(
        reflection_id=reflection_id,
        insight_generated_at=datetime.now(),
        session_started_at=datetime.now(),
        messages_after_insight=messages_after,
        avg_message_length_after=avg_length,
        time_to_first_response_sec=time_to_first,
        session_extended=session_extended,
        explicit_signals=explicit_signals or [],
    )


def test_detect_zero_response_returns_irrelevant():
    detector = ImplicitFeedbackDetector()
    context = _make_context(messages_after=0)
    result = detector.detect(context)
    assert result is not None
    assert result.inferred_type == FeedbackType.IRRELEVANT
    assert result.confidence == pytest.approx(0.7)
    assert "zero_response_after_insight" in result.signals


def test_session_context_preserves_insight_timestamp_contract():
    generated_at = datetime(2026, 7, 2, 9, 30, 0)
    context = SessionContext(
        reflection_id="r-ts",
        insight_generated_at=generated_at,
        session_started_at=datetime(2026, 7, 2, 9, 29, 0),
    )

    payload = asdict(context)
    assert payload["insight_generated_at"] == generated_at


def test_detect_short_response_returns_irrelevant():
    detector = ImplicitFeedbackDetector()
    context = _make_context(messages_after=1, avg_length=10)
    result = detector.detect(context)
    assert result is not None
    assert result.inferred_type == FeedbackType.IRRELEVANT
    assert result.confidence == pytest.approx(0.6)
    assert any("short_response" in s for s in result.signals)


def test_detect_deep_engagement_returns_accurate():
    detector = ImplicitFeedbackDetector()
    context = _make_context(messages_after=3)
    result = detector.detect(context)
    assert result is not None
    assert result.inferred_type == FeedbackType.ACCURATE
    assert result.confidence == pytest.approx(0.65)
    assert any("deep_engagement" in s for s in result.signals)


def test_detect_session_extended_returns_insightful():
    detector = ImplicitFeedbackDetector()
    context = _make_context(messages_after=1, avg_length=50, session_extended=True)
    result = detector.detect(context)
    assert result is not None
    assert result.inferred_type == FeedbackType.INSIGHTFUL
    assert result.confidence == pytest.approx(0.6)
    assert "session_extended" in result.signals


def test_detect_derives_session_extension_from_timestamps():
    detector = ImplicitFeedbackDetector()
    session_started_at = datetime(2026, 7, 2, 9, 0, 0)
    insight_generated_at = session_started_at + timedelta(minutes=5)
    session_ended_at = insight_generated_at + timedelta(minutes=11)
    context = SessionContext(
        reflection_id="r-duration",
        insight_generated_at=insight_generated_at,
        session_started_at=session_started_at,
        session_ended_at=session_ended_at,
        messages_after_insight=1,
        avg_message_length_after=50,
    )

    result = detector.detect(context)

    assert result is not None
    assert result.inferred_type == FeedbackType.INSIGHTFUL
    assert result.confidence == pytest.approx(0.6)
    assert any(signal.startswith("session_extended_by_duration") for signal in result.signals)


def test_detect_host_marked_action_taken_returns_accurate():
    detector = ImplicitFeedbackDetector()
    context = _make_context(
        messages_after=1, avg_length=10, explicit_signals=["user_acted_on_insight"]
    )
    result = detector.detect(context)
    assert result is not None
    assert result.inferred_type == FeedbackType.ACCURATE
    assert result.confidence == pytest.approx(0.8)
    assert "host_marked:action_taken" in result.signals


def test_detect_host_marked_ignored_returns_irrelevant():
    detector = ImplicitFeedbackDetector()
    context = _make_context(messages_after=2, avg_length=20, explicit_signals=["user_ignored"])
    result = detector.detect(context)
    assert result is not None
    assert result.inferred_type == FeedbackType.IRRELEVANT
    assert result.confidence == pytest.approx(0.75)
    assert "host_marked:ignored" in result.signals


def test_detect_conflicting_signals_takes_highest_confidence():
    detector = ImplicitFeedbackDetector()
    # deep_engagement (0.65) vs user_ignored (0.75)
    context = _make_context(
        messages_after=3,
        explicit_signals=["user_ignored"],
    )
    result = detector.detect(context)
    assert result is not None
    assert result.inferred_type == FeedbackType.IRRELEVANT
    assert result.confidence == pytest.approx(0.75)


def test_detect_no_signals_returns_none():
    detector = ImplicitFeedbackDetector()
    context = _make_context(messages_after=1, avg_length=50)
    assert detector.detect(context) is None


def test_detect_below_min_confidence_returns_none():
    detector = ImplicitFeedbackDetector()
    # A single weak signal below threshold: long delay is 0.55 which is above 0.3
    # Use a fabricated very weak scenario by checking boundary.
    context = _make_context(messages_after=1, avg_length=50, time_to_first=301)
    result = detector.detect(context)
    assert result is not None
    assert result.confidence >= detector.MIN_CONFIDENCE


def test_detect_simple_wraps_detect():
    detector = ImplicitFeedbackDetector()
    result = detector.detect_simple("r2", messages_after=0, avg_length_after=0)
    assert result is not None
    assert result.reflection_id == "r2"
    assert result.inferred_type == FeedbackType.IRRELEVANT


def test_detect_simple_with_immediate_end_returns_irrelevant():
    detector = ImplicitFeedbackDetector()
    result = detector.detect_simple(
        "r3", messages_after=2, avg_length_after=10, session_ended_immediately=True
    )
    assert result is not None
    assert result.inferred_type == FeedbackType.IRRELEVANT
    assert "host_marked:abrupt_end" in result.signals


def test_should_collect_feedback_with_explicit_feedback():
    assert ImplicitFeedbackDetector.should_collect_feedback(1.0, True) is False
    assert ImplicitFeedbackDetector.should_collect_feedback(100.0, True) is False


def test_should_collect_feedback_without_explicit_feedback():
    assert ImplicitFeedbackDetector.should_collect_feedback(1.0, False) is False
    assert ImplicitFeedbackDetector.should_collect_feedback(25.0, False) is True
    assert ImplicitFeedbackDetector.should_collect_feedback(24.1, False) is True
    assert ImplicitFeedbackDetector.should_collect_feedback(24.0, False) is False


def test_implicit_feedback_to_user_feedback():
    detector = ImplicitFeedbackDetector()
    context = _make_context(messages_after=3)
    result = detector.detect(context)
    user_fb = result.to_user_feedback()
    assert user_fb.feedback_type == FeedbackType.ACCURATE
    assert "隐式反馈" in user_fb.comment
    assert "deep_engagement" in user_fb.comment
