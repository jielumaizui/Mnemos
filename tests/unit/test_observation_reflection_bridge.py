"""
Observation → Reflection 事件驱动桥接测试

验证 observation.updated 事件能驱动 L4 Reflection，
补齐 L3 → L4 的自动流转链路。
"""

from unittest.mock import MagicMock, patch

import pytest

from core.cognitive.models import Dimension, Observation, ObservationType
from core.cognitive.observation_store import ObservationStore
from core.mnemos_bus import Event
from core.reflection.models import ReflectionTrigger


@pytest.fixture
def sample_observations():
    """构造几条高置信度观察。"""
    return [
        Observation(
            id="obs-1",
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.TREND,
            value="对 Redis 的关注显著上升",
            confidence=0.85,
            source_path="wiki/session.md",
        ),
        Observation(
            id="obs-2",
            dimension=Dimension.STRESS,
            observation_type=ObservationType.DEVIATION,
            value="估算偏差从 1.2 倍扩大到 2.1 倍",
            confidence=0.75,
            source_path="wiki/session.md",
        ),
    ]


def _make_event(observation_ids):
    return Event(
        event_type="observation.updated",
        source="observation_engine",
        payload={"observation_ids": observation_ids, "wiki_path": "/tmp/wiki/L3-Observations"},
    )


def _fake_config(enabled=True, trigger_enabled=True):
    fake_config = MagicMock()
    fake_config.get = MagicMock(
        side_effect=lambda key, default=None: {
            "reflection.enabled": enabled,
            "reflection.observation_trigger_enabled": trigger_enabled,
            "reflection.observation_trigger_confidence": 0.7,
        }.get(key, default)
    )
    return fake_config


# ========== ObservationStore.get_by_id ==========


def test_observation_store_get_by_id_round_trip(tmp_path):
    """ObservationStore.get_by_id 应能按 id 查询已保存的观察。"""
    db_path = tmp_path / "observations.db"
    store = ObservationStore(str(db_path))

    obs = Observation(
        id="obs-get",
        dimension=Dimension.GROWTH,
        observation_type=ObservationType.PATTERN,
        value="成长型思维",
        confidence=0.9,
        source_path="wiki/growth.md",
    )
    store.save_batch([obs])

    got = store.get_by_id("obs-get")
    assert got is not None
    assert got.id == "obs-get"
    assert got.dimension == Dimension.GROWTH
    assert got.observation_type == ObservationType.PATTERN
    assert got.value == "成长型思维"
    assert got.confidence == pytest.approx(0.9)

    missing = store.get_by_id("not-exist")
    assert missing is None


# ========== observation.updated → Reflection ==========


def test_on_observation_updated_triggers_reflection(sample_observations):
    """高置信度观察应触发 Reflection 并发布 reflection.completed。"""
    mock_store = MagicMock()
    mock_store.get_by_id = MagicMock(
        side_effect=lambda oid: next((o for o in sample_observations if o.id == oid), None)
    )

    mock_record = MagicMock()
    mock_record.id = "ref-1"
    mock_insight = MagicMock()
    mock_insight.summary = "Redis 与估算偏差需要关注"
    mock_result = MagicMock()
    mock_result.triggered = True
    mock_result.record = mock_record
    mock_result.insight = mock_insight

    mock_engine = MagicMock()
    mock_engine.reflect_on_user_input = MagicMock(return_value=mock_result)

    mock_bus = MagicMock()

    with (
        patch("core.config.get_config", return_value=_fake_config()),
        patch("core.cognitive.observation_store.ObservationStore", return_value=mock_store),
        patch("mnemos_daemon._get_reflection_engine", return_value=mock_engine),
        patch("mnemos_daemon._event_bus_instance", mock_bus),
    ):
        from mnemos_daemon import _on_observation_updated

        event = _make_event(["obs-1", "obs-2"])
        _on_observation_updated(event)

    mock_engine.reflect_on_user_input.assert_called_once()
    query = mock_engine.reflect_on_user_input.call_args[0][0]
    assert "Redis" in query
    assert "估算偏差" in query

    mock_bus.publish.assert_called_once()
    args, kwargs = mock_bus.publish.call_args
    assert args[0] == "reflection.completed"
    payload = kwargs["payload"]
    assert payload["triggered"] is True
    assert payload["trigger_source"] == "observation.updated"
    assert payload["record_id"] == "ref-1"


def test_on_observation_updated_skips_low_confidence():
    """低置信度且非突变/趋势/对比的观察不应触发 Reflection。"""
    low_conf = Observation(
        id="obs-low",
        dimension=Dimension.ATTENTION,
        observation_type=ObservationType.FREQUENCY,
        value="某关键词出现 3 次",
        confidence=0.3,
        source_path="wiki/session.md",
    )

    mock_store = MagicMock()
    mock_store.get_by_id = MagicMock(return_value=low_conf)

    mock_engine = MagicMock()
    mock_bus = MagicMock()

    with (
        patch("core.config.get_config", return_value=_fake_config()),
        patch("core.cognitive.observation_store.ObservationStore", return_value=mock_store),
        patch("mnemos_daemon._get_reflection_engine", return_value=mock_engine),
        patch("mnemos_daemon._event_bus_instance", mock_bus),
    ):
        from mnemos_daemon import _on_observation_updated

        event = _make_event(["obs-low"])
        _on_observation_updated(event)

    mock_engine.reflect_on_user_input.assert_not_called()
    mock_bus.publish.assert_not_called()


def test_on_observation_updated_disabled_by_config():
    """reflection.observation_trigger_enabled=False 时不触发。"""
    mock_engine = MagicMock()
    mock_bus = MagicMock()

    with (
        patch("core.config.get_config", return_value=_fake_config(trigger_enabled=False)),
        patch("mnemos_daemon._get_reflection_engine", return_value=mock_engine),
        patch("mnemos_daemon._event_bus_instance", mock_bus),
    ):
        from mnemos_daemon import _on_observation_updated

        event = _make_event(["obs-1"])
        _on_observation_updated(event)

    mock_engine.reflect_on_user_input.assert_not_called()


def test_on_observation_updated_fallback_to_manual(sample_observations):
    """reflect_on_user_input 未触发时 fallback 到手动反射。"""
    mock_store = MagicMock()
    mock_store.get_by_id = MagicMock(
        side_effect=lambda oid: next((o for o in sample_observations if o.id == oid), None)
    )

    mock_record = MagicMock()
    mock_record.id = "ref-2"
    mock_insight = MagicMock()
    mock_insight.summary = "手动反射洞察"
    manual_result = MagicMock()
    manual_result.triggered = True
    manual_result.record = mock_record
    manual_result.insight = mock_insight

    auto_result = MagicMock()
    auto_result.triggered = False
    auto_result.record = None
    auto_result.insight = None

    mock_engine = MagicMock()
    mock_engine.reflect_on_user_input = MagicMock(return_value=auto_result)
    mock_engine.reflect_manually = MagicMock(return_value=manual_result)

    mock_bus = MagicMock()

    with (
        patch("core.config.get_config", return_value=_fake_config()),
        patch("core.cognitive.observation_store.ObservationStore", return_value=mock_store),
        patch("mnemos_daemon._get_reflection_engine", return_value=mock_engine),
        patch("mnemos_daemon._event_bus_instance", mock_bus),
    ):
        from mnemos_daemon import _on_observation_updated

        event = _make_event(["obs-1"])
        _on_observation_updated(event)

    mock_engine.reflect_manually.assert_called_once()
    trigger = mock_engine.reflect_manually.call_args.kwargs["trigger"]
    assert trigger == ReflectionTrigger.OBSERVATION_UPDATED

    mock_bus.publish.assert_called_once()
    args, kwargs = mock_bus.publish.call_args
    assert kwargs["payload"]["record_id"] == "ref-2"


def test_on_observation_updated_skips_feedback_loop_observations():
    """由反思反哺机制生成的 Observation（source_id == 'feedback_loop'）不应再触发 Reflection，
    以防止 Observation → Reflection → Observation 的级联放大。"""
    feedback_obs = Observation(
        id="obs-feedback",
        dimension=Dimension.ATTENTION,
        observation_type=ObservationType.TREND,
        value="认知变迁反哺",
        confidence=0.95,
        source_path="reflection_feedback:focus_shift",
        source_id="feedback_loop",
    )

    mock_store = MagicMock()
    mock_store.get_by_id = MagicMock(return_value=feedback_obs)

    mock_engine = MagicMock()
    mock_bus = MagicMock()

    with (
        patch("core.config.get_config", return_value=_fake_config()),
        patch("core.cognitive.observation_store.ObservationStore", return_value=mock_store),
        patch("mnemos_daemon._get_reflection_engine", return_value=mock_engine),
        patch("mnemos_daemon._event_bus_instance", mock_bus),
    ):
        from mnemos_daemon import _on_observation_updated

        event = _make_event(["obs-feedback"])
        _on_observation_updated(event)

    mock_engine.reflect_on_user_input.assert_not_called()
    mock_engine.reflect_manually.assert_not_called()
    mock_bus.publish.assert_not_called()
