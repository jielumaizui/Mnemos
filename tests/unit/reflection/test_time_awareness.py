from dataclasses import asdict
from datetime import datetime, timedelta


from core.reflection.time_awareness import TemporalContext, TimeAwareness


def test_recency_weight_returns_one_for_none_period_end():
    ta = TimeAwareness()
    assert ta.recency_weight(None, "attention") == 1.0


def test_recency_weight_returns_one_for_future_period():
    ta = TimeAwareness()
    now = datetime(2026, 6, 13, 10, 0, 0)
    future = now + timedelta(days=5)
    assert ta.recency_weight(future, "attention", as_of=now) == 1.0


def test_recency_weight_boosts_recent_observations():
    ta = TimeAwareness()
    now = datetime(2026, 6, 13, 10, 0, 0)

    # Within 7 days the weight is floored to at least 0.9
    three_days_ago = now - timedelta(days=3)
    assert ta.recency_weight(three_days_ago, "stress", as_of=now) >= 0.9

    # Far in the past decays according to dimension half-life
    # growth half-life is 365 days; 365 days ago still yields ~0.368
    old = now - timedelta(days=365)
    weight = ta.recency_weight(old, "growth", as_of=now)
    assert 0 < weight < 0.5


def test_recency_weight_uses_dimension_specific_half_life():
    ta = TimeAwareness()
    now = datetime(2026, 6, 13, 10, 0, 0)
    period = now - timedelta(days=60)

    # stress half-life is 14 days -> strong decay
    stress_weight = ta.recency_weight(period, "stress", as_of=now)
    # growth half-life is 365 days -> mild decay
    growth_weight = ta.recency_weight(period, "growth", as_of=now)

    assert stress_weight < growth_weight


def test_freshness_status_edge_cases():
    ta = TimeAwareness()

    # actions has a 30-day half-life
    # Exactly at half-life boundary is fresh
    assert ta.freshness_status(30, "actions") == "fresh"
    # Just beyond half-life is stale
    assert ta.freshness_status(31, "actions") == "stale"
    # Exactly at 3x half-life boundary is stale
    assert ta.freshness_status(90, "actions") == "stale"
    # Beyond 3x half-life is expired
    assert ta.freshness_status(91, "actions") == "expired"


def test_freshness_status_defaults_to_30_day_half_life():
    ta = TimeAwareness()
    # Unknown dimension uses default half-life of 30 days
    assert ta.freshness_status(30, "unknown_dimension") == "fresh"
    assert ta.freshness_status(90, "unknown_dimension") == "stale"
    assert ta.freshness_status(91, "unknown_dimension") == "expired"


def test_duration_semantics_uses_configured_buckets(monkeypatch):
    import core.reflection.time_awareness as time_awareness

    monkeypatch.setattr(
        time_awareness,
        "DURATION_SEMANTICS",
        {
            "two_weeks": (0, 14),
            "open_ended": (14, float("inf")),
        },
    )
    ta = TimeAwareness()

    context = ta.get_temporal_context(as_of=datetime(2026, 6, 13, 10, 0, 0))

    assert context.duration_semantics["14d"] == ta.humanize_duration(14)


def test_detect_rhythm_uses_configured_periods(monkeypatch):
    import core.reflection.time_awareness as time_awareness

    periods = dict(time_awareness.RHYTHM_PERIODS)
    periods["year_start"] = ((1, 1), (1, 7))
    monkeypatch.setattr(time_awareness, "RHYTHM_PERIODS", periods)

    ta = TimeAwareness()

    in_period = ta.get_temporal_context(as_of=datetime(2026, 1, 6, 10, 0, 0))
    outside_period = ta.get_temporal_context(as_of=datetime(2026, 1, 15, 10, 0, 0))

    assert in_period.rhythm == "year_start"
    assert outside_period.rhythm != "year_start"


def test_temporal_context_preserves_last_reflection_trigger_contract():
    context = TemporalContext(
        now=datetime(2026, 6, 13, 10, 0, 0),
        now_str="2026-06-13 10:00",
        rhythm="normal",
        rhythm_description="正常节律",
        last_reflection_ago=3,
        last_reflection_trigger="major_decision",
    )

    assert asdict(context)["last_reflection_trigger"] == "major_decision"
