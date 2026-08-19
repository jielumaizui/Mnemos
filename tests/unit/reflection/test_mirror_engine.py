from dataclasses import asdict
from datetime import datetime
from unittest.mock import MagicMock


from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.models import Dimension, Observation, ObservationType, SourceType
from core.reflection.mirror_engine import DECISION_DIMENSION_MAP, MirrorEngine, TRIGGER_SCENE_MAP
from core.reflection.time_awareness import TemporalContext, TimeAwareness


def _make_obs(dimension: Dimension, value, confidence: float = 0.8):
    return Observation(
        dimension=dimension,
        observation_type=ObservationType.PATTERN,
        value=value,
        unit="",
        confidence=confidence,
        source_type=SourceType.WIKI,
        evidence=["evidence"],
        period_end=datetime(2026, 6, 13, 10, 0, 0),
        period_start=datetime(2026, 6, 12, 10, 0, 0),
    )


def _fake_obs_store(observations_by_dim):
    """observations_by_dim: dict of Dimension -> list of Observation"""
    store = MagicMock()

    def _authorized_query(*, dimension, limit=100, **_kwargs):
        return observations_by_dim.get(dimension, [])[:limit], {"authorized_count": 0}

    store.authorized_query.side_effect = _authorized_query
    return store


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="mcp:codex:mirror-test",
        agent="codex",
        host_kind="test",
        capability_id="mirror-test",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _access_kwargs() -> dict:
    return {
        "principal": _principal(),
        "narrowing": AccessNarrowing(session_id="session-1", project="mnemos"),
    }


def _fixed_time_awareness(weights):
    """weights: dict dim.value -> recency_weight"""
    ta = MagicMock(spec=TimeAwareness)
    ta.get_temporal_context.return_value = TemporalContext(
        now=datetime(2026, 6, 13, 10, 0, 0),
        now_str="2026-06-13 10:00",
        rhythm="normal",
        rhythm_description="常规时间",
        last_reflection_ago=None,
        dimension_freshness={},
        duration_semantics={},
    )
    ta.recency_weight = MagicMock(side_effect=lambda dt, dim, now=None: weights.get(dim, 0.5))
    ta.humanize_duration = MagicMock(return_value="最近")
    return ta


def test_build_mirror_returns_snapshots_for_each_dimension():
    # major_decision maps to decisions, stress, time, growth
    obs_map = {
        Dimension.DECISIONS: [_make_obs(Dimension.DECISIONS, {"choices": 3})],
        Dimension.STRESS: [_make_obs(Dimension.STRESS, 0.4)],
        Dimension.TIME: [_make_obs(Dimension.TIME, 2.5)],
        Dimension.GROWTH: [_make_obs(Dimension.GROWTH, {"role": "lead"})],
    }
    store = _fake_obs_store(obs_map)
    ta = _fixed_time_awareness({d.value: 0.9 for d in obs_map})

    engine = MirrorEngine(observation_store=store, time_awareness=ta)
    result = engine.build_mirror(
        "major_decision", limit_per_dim=3, min_weight=0.2, **_access_kwargs()
    )

    assert len(result.snapshots) == 4
    assert set(result.dimensions_involved) == {"decisions", "stress", "time", "growth"}
    assert result.total_observations_scanned == 4
    assert result.total_weighted_score == 2.88
    assert asdict(result)["total_weighted_score"] == 2.88


def test_trigger_scene_map_exposes_supported_decision_scenes():
    """触发类型映射应只指向 MirrorEngine 支持的决策场景。"""
    assert TRIGGER_SCENE_MAP["new_project"] == "new_project"
    assert TRIGGER_SCENE_MAP["abandon_project"] == "abandon_project"
    assert set(TRIGGER_SCENE_MAP.values()) <= set(DECISION_DIMENSION_MAP)


def test_build_mirror_respects_skip_dimensions():
    obs_map = {
        Dimension.DECISIONS: [_make_obs(Dimension.DECISIONS, {"choices": 3})],
        Dimension.STRESS: [_make_obs(Dimension.STRESS, 0.4)],
        Dimension.TIME: [_make_obs(Dimension.TIME, 2.5)],
        Dimension.GROWTH: [_make_obs(Dimension.GROWTH, {"role": "lead"})],
    }
    store = _fake_obs_store(obs_map)
    ta = _fixed_time_awareness({d.value: 0.9 for d in obs_map})

    engine = MirrorEngine(observation_store=store, time_awareness=ta)
    result = engine.build_mirror(
        "major_decision",
        limit_per_dim=3,
        min_weight=0.2,
        skip_dimensions=["stress"],
        **_access_kwargs(),
    )

    assert "stress" not in result.dimensions_involved
    assert "stress" not in {s.dimension for s in result.snapshots}
    assert len(result.snapshots) == 3


def test_build_mirror_filters_by_min_weight():
    # Both dimensions belong to the major_decision scene.
    obs_map = {
        Dimension.DECISIONS: [_make_obs(Dimension.DECISIONS, {"choices": 3})],
        Dimension.STRESS: [_make_obs(Dimension.STRESS, 0.4)],
    }
    weights = {"decisions": 0.9, "stress": 0.1}
    store = _fake_obs_store(obs_map)
    ta = _fixed_time_awareness(weights)

    engine = MirrorEngine(observation_store=store, time_awareness=ta)
    result = engine.build_mirror(
        "major_decision", limit_per_dim=3, min_weight=0.2, **_access_kwargs()
    )

    assert all(s.dimension != "stress" for s in result.snapshots)
    assert result.total_observations_scanned == 1


def test_build_mirror_enforces_limit_per_dimension():
    obs_map = {
        Dimension.ATTENTION: [
            _make_obs(Dimension.ATTENTION, {"coding": i}, confidence=0.9 - i * 0.05)
            for i in range(5)
        ],
    }
    weights = {"attention": 0.9}
    store = _fake_obs_store(obs_map)
    ta = _fixed_time_awareness(weights)

    engine = MirrorEngine(observation_store=store, time_awareness=ta)
    result = engine.build_mirror(
        "new_project", limit_per_dim=2, min_weight=0.2, **_access_kwargs()
    )

    attention_count = sum(1 for s in result.snapshots if s.dimension == "attention")
    assert attention_count == 2
    # query limit is limit_per_dim * 2, so only 4 candidates are scanned
    assert result.total_observations_scanned == 4


def test_build_mirror_without_obs_store_returns_empty_result():
    engine = MirrorEngine(observation_store=None)
    result = engine.build_mirror("new_project")
    assert result.snapshots == []
    assert "ObservationStore 未配置" in result.temporal_note
