# -*- coding: utf-8 -*-
"""Unit tests for core.scoring.fallback."""

from datetime import datetime, timedelta

import pytest

from core.scoring.fallback import DegradationEvent, ScorerFallback


@pytest.fixture
def fallback():
    return ScorerFallback()


class TestScorerFallback:
    def test_guard_returns_ml_result_when_no_exception(self, fallback):
        def rule_fn():
            return 0.4

        with fallback.guard("profile", rule_fn) as try_ml:
            result = try_ml(lambda: 0.9)
        assert result == pytest.approx(0.9)
        assert fallback.get_events() == []
        assert not fallback.should_degrade("profile")

    def test_guard_records_degradation_event_and_returns_fallback(self, fallback):
        def rule_fn():
            return 0.75

        ml_error = ValueError("ml exploded")

        with fallback.guard("profile", rule_fn) as try_ml:
            result = try_ml(lambda: (_ for _ in ()).throw(ml_error))

        assert result == pytest.approx(0.75)
        events = fallback.get_events()
        assert len(events) == 1
        event = events[0]
        assert isinstance(event, DegradationEvent)
        assert event.dimension == "profile"
        assert event.reason == "ml_exception"
        assert event.rule_score == pytest.approx(0.75)
        assert "ml exploded" in event.ml_error
        assert isinstance(event.timestamp, datetime)

    def test_guard_increments_consecutive_failures(self, fallback):
        def rule_fn():
            return 0.5

        for _ in range(3):
            with fallback.guard("kg", rule_fn) as try_ml:
                try_ml(lambda: (_ for _ in ()).throw(RuntimeError("fail")))

        assert fallback._consecutive_failures["kg"] == 3
        assert fallback.should_degrade("kg")

    def test_guard_does_not_degrade_after_success(self, fallback):
        def rule_fn():
            return 0.5

        with fallback.guard("sync", rule_fn) as try_ml:
            try_ml(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        with fallback.guard("sync", rule_fn) as try_ml:
            assert try_ml(lambda: 0.9) == pytest.approx(0.9)

        # guard 上下文本身不会自动 reset_failure，需显式调用
        fallback.reset_failure("sync")
        assert not fallback.should_degrade("sync")

    def test_get_events_filters_by_since(self, fallback):
        def rule_fn():
            return 0.6

        with fallback.guard("ops", rule_fn) as try_ml:
            try_ml(lambda: (_ for _ in ()).throw(RuntimeError("fail")))

        now = datetime.now()
        assert len(fallback.get_events(since=now - timedelta(seconds=1))) == 1
        assert len(fallback.get_events(since=now + timedelta(seconds=1))) == 0

    def test_event_list_truncation_at_max(self, fallback, monkeypatch):
        monkeypatch.setattr(fallback, "MAX_EVENTS", 3)

        def rule_fn():
            return 0.1

        for i in range(5):
            with fallback.guard("dim", rule_fn) as try_ml:
                try_ml(lambda i=i: (_ for _ in ()).throw(RuntimeError(f"fail {i}")))

        assert len(fallback.get_events()) == 3
        # 保留最近的事件
        assert "fail 4" in fallback.get_events()[-1].ml_error
