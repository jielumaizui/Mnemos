from dataclasses import asdict
from datetime import datetime, timedelta

from core.cognitive.models import Dimension, Observation, ObservationType
from core.reflection.models import ReflectionTrigger
from core.reflection.trigger_detector import TriggerContext, TriggerDetector


def test_detect_text_matches_new_project_keyword():
    detector = TriggerDetector()
    event = detector.detect_text("我要启动一个新项目")
    assert event is not None
    assert event.trigger == ReflectionTrigger.NEW_PROJECT
    assert event.source == "keyword"
    assert event.confidence > 0


def test_detect_text_matches_major_decision_keyword():
    detector = TriggerDetector()
    event = detector.detect_text("我纠结要不要跳槽，这是一个重大决定")
    assert event is not None
    assert event.trigger == ReflectionTrigger.MAJOR_DECISION
    assert event.source == "keyword"


def test_detect_text_matches_long_term_plan_keyword():
    detector = TriggerDetector()
    event = detector.detect_text("我需要做一个三年计划")
    assert event is not None
    assert event.trigger == ReflectionTrigger.LONG_TERM_PLAN


def test_detect_text_returns_none_for_neutral_input():
    detector = TriggerDetector()
    assert detector.detect_text("今天天气不错") is None
    assert detector.detect_text("帮我查一下快递") is None
    assert detector.detect_text("好的") is None


def test_detect_text_short_input_returns_none():
    detector = TriggerDetector()
    assert detector.detect_text("创业") is None


def test_get_observations_in_range_passes_time_window_to_store():
    """P119: _get_observations_in_range 应将时间范围下传给 ObservationStore.query。"""
    from datetime import datetime, timedelta
    from unittest.mock import MagicMock
    from core.cognitive.models import Dimension, Observation, ObservationType

    detector = TriggerDetector()
    mock_store = MagicMock()
    mock_store.query.return_value = [
        Observation(
            id="obs-1",
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={},
            period_start=datetime.now() - timedelta(days=2),
            period_end=datetime.now() - timedelta(days=1),
        )
    ]
    detector.obs_store = mock_store

    start = datetime.now() - timedelta(days=3)
    end = datetime.now()
    result = detector._get_observations_in_range("attention", start, end)

    assert len(result) == 1
    mock_store.query.assert_called_once()
    call_kwargs = mock_store.query.call_args.kwargs
    assert call_kwargs.get("dimension") == Dimension.ATTENTION
    assert call_kwargs.get("period_start") == start
    assert call_kwargs.get("period_end") == end
    assert call_kwargs.get("limit") == 1000


def test_trigger_context_preserves_last_trigger_age_contract():
    context = TriggerContext(last_trigger_ago_days=5, user_text="我要启动一个新项目")

    assert asdict(context)["last_trigger_ago_days"] == 5


def test_detect_uses_context_recent_observations_for_mutation():
    now = datetime.now()
    recent_observations = [
        Observation(
            dimension=Dimension.STRESS,
            observation_type=ObservationType.TREND,
            value={"level": 0.95},
            period_start=now - timedelta(days=2),
            period_end=now - timedelta(days=1),
        ),
        Observation(
            dimension=Dimension.STRESS,
            observation_type=ObservationType.TREND,
            value={"level": 1.0},
            period_start=now - timedelta(days=1),
            period_end=now,
        ),
    ]
    compare_observations = [
        Observation(
            dimension=Dimension.STRESS,
            observation_type=ObservationType.TREND,
            value={"level": 0.2},
            period_start=now - timedelta(days=10),
            period_end=now - timedelta(days=9),
        ),
        Observation(
            dimension=Dimension.STRESS,
            observation_type=ObservationType.TREND,
            value={"level": 0.25},
            period_start=now - timedelta(days=8),
            period_end=now - timedelta(days=7),
        ),
    ]

    class CompareOnlyStore:
        def query(self, **kwargs):
            if kwargs["dimension"] == Dimension.STRESS and kwargs["period_end"] < now - timedelta(days=1):
                return compare_observations
            return []

    detector = TriggerDetector(observation_store=CompareOnlyStore())
    event = detector.detect(TriggerContext(recent_observations=recent_observations))

    assert event is not None
    assert event.source == "observation_mutation"
    assert event.trigger == ReflectionTrigger.MAJOR_DECISION
    assert "近期样本: 2, 对比样本: 2" in event.matched_signals
