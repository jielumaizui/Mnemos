from datetime import timedelta

import pytest


def test_legacy_feedback_signal_cannot_derive_expected_or_actual_label():
    from core.scoring.feedback_channel import FeedbackSignal

    signal = FeedbackSignal(
        subject="topic-a",
        action="accept",
        dimension="profile",
    )

    assert not hasattr(signal, "to_feedback_v2")
    assert not hasattr(signal, "score")


def test_fatigue_guard_persists_across_instances(tmp_path):
    from core.scoring.feedback_channel import FeedbackFatigueGuard

    db_path = tmp_path / "feedback.db"
    first = FeedbackFatigueGuard(db_path=db_path)
    second = FeedbackFatigueGuard(db_path=db_path)

    assert first.allow_prompt("topic", cooldown=timedelta(hours=1)) is True
    first.record_prompt("topic")
    assert second.allow_prompt("topic", cooldown=timedelta(hours=1)) is False
    assert b"topic" not in db_path.read_bytes()


def test_direct_feedback_training_bridge_fails_closed_without_calling_scorer():
    from core.scoring.feedback_channel import FeedbackSignal, record_feedback_signal

    calls = []

    class ForbiddenScorer:
        def feedback(self, feedback):
            calls.append(feedback)

    with pytest.raises(RuntimeError, match="direct_feedback_training_retired"):
        record_feedback_signal(
            FeedbackSignal(
                subject="topic-a",
                action="dismiss",
                dimension="profile",
            ),
            scorer=ForbiddenScorer(),
        )

    assert calls == []
