from __future__ import annotations

import pytest

from core.event_outcome import HandlerOutcome


@pytest.mark.parametrize(
    ("result", "disposition"),
    [
        ({"success": False, "error": "boom"}, "retry"),
        ({"success": True}, "ack"),
        ({"status": "duplicate"}, "noop"),
        ({"status": "skipped", "reason": "none"}, "noop"),
        ({"status": "error", "reason": "down"}, "retry"),
        ("ok", "ack"),
        ("skipped", "noop"),
        ("failed", "retry"),
        (1, "ack"),
    ],
)
def test_handler_outcome_normalization_matrix(result, disposition):
    assert HandlerOutcome.from_result(result, consumer="test").disposition == disposition


def test_reason_is_not_forwarded_twice_as_metadata():
    outcome = HandlerOutcome.from_result(
        {"status": "error", "reason": "down", "detail": 1}, consumer="test"
    )

    assert outcome.reason == "down"
    assert outcome.metadata == {"status": "error", "detail": 1}


def test_defer_requires_a_resumable_key():
    with pytest.raises(ValueError, match="deferred_key"):
        HandlerOutcome.defer(reason="unresumable")
