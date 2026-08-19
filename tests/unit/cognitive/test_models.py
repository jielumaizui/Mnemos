"""Unit tests for core.cognitive.models."""

from datetime import datetime
import pytest

from core.cognitive.models import (
    Dimension,
    Observation,
    ObservationBatch,
    ObservationType,
    SourceType,
)
from core.cognitive.sources import ContentSource, UserIntent


@pytest.fixture
def sample_observation() -> Observation:
    """Return a single Observation with deterministic fields."""
    return Observation(
        id="obs-001",
        dimension=Dimension.ATTENTION,
        observation_type=ObservationType.FREQUENCY,
        value={"concepts": {"ai": 5}, "total_mentions": 5, "dominant": "ai"},
        unit="mentions",
        confidence=0.85,
        source_type=SourceType.WIKI,
        source_path="/wiki/attention.md",
        source_id="session-1",
        evidence=["AI mentioned 5 times"],
        observed_at=datetime(2026, 6, 1, 10, 0, 0),
        period_start=datetime(2026, 6, 1, 0, 0, 0),
        period_end=datetime(2026, 6, 2, 0, 0, 0),
        content_source=ContentSource.NATIVE_DIALOGUE,
        user_intent_signal=UserIntent.SHARING_INFORMATION,
        user_notes="",
        created_at=datetime(2026, 6, 1, 12, 0, 0),
        updated_at=datetime(2026, 6, 1, 12, 0, 0),
        version=1,
    )


@pytest.fixture
def observation_batch(sample_observation) -> ObservationBatch:
    """Return a batch with two observations of different dimensions and types."""
    obs2 = Observation(
        id="obs-002",
        dimension=Dimension.TIME,
        observation_type=ObservationType.TREND,
        value={"estimates": 10, "delays": 2},
        unit="mentions",
        confidence=0.7,
        source_type=SourceType.RAW,
        source_path="/raw/session-2.md",
        source_id="session-2",
        evidence=["time trend evidence"],
        observed_at=datetime(2026, 6, 2, 10, 0, 0),
        period_start=datetime(2026, 6, 2, 0, 0, 0),
        period_end=datetime(2026, 6, 3, 0, 0, 0),
        content_source=ContentSource.USER_NOTE,
        user_intent_signal=UserIntent.UNKNOWN,
        created_at=datetime(2026, 6, 2, 12, 0, 0),
        updated_at=datetime(2026, 6, 2, 12, 0, 0),
        version=2,
    )
    return ObservationBatch(
        observations=[sample_observation, obs2],
        period_start=datetime(2026, 6, 1, 0, 0, 0),
        period_end=datetime(2026, 6, 3, 0, 0, 0),
        source_count=2,
        dimension_counts={"attention": 1, "time": 1},
    )


class TestObservationSerialization:
    """Tests for Observation.to_dict and Observation.from_dict."""

    def test_to_dict_structure(self, sample_observation):
        d = sample_observation.to_dict()
        assert d["id"] == "obs-001"
        assert d["dimension"] == "attention"
        assert d["observation_type"] == "frequency"
        assert d["value"] == {"concepts": {"ai": 5}, "total_mentions": 5, "dominant": "ai"}
        assert d["unit"] == "mentions"
        assert d["confidence"] == pytest.approx(0.85)
        assert d["source_type"] == "wiki"
        assert d["source_path"] == "/wiki/attention.md"
        assert d["source_id"] == "session-1"
        assert d["evidence"] == '["AI mentioned 5 times"]'
        assert d["observed_at"] == "2026-06-01T10:00:00"
        assert d["period_start"] == "2026-06-01T00:00:00"
        assert d["period_end"] == "2026-06-02T00:00:00"
        assert d["content_source"] == "native_dialogue"
        assert d["user_intent_signal"] == "sharing_information"
        assert d["user_notes"] == ""
        assert d["version"] == 1

    def test_from_dict_roundtrip(self, sample_observation):
        d = sample_observation.to_dict()
        restored = Observation.from_dict(d)

        assert restored.id == sample_observation.id
        assert restored.dimension == sample_observation.dimension
        assert restored.observation_type == sample_observation.observation_type
        assert restored.value == sample_observation.value
        assert restored.unit == sample_observation.unit
        assert restored.confidence == pytest.approx(sample_observation.confidence)
        assert restored.source_type == sample_observation.source_type
        assert restored.source_path == sample_observation.source_path
        assert restored.source_id == sample_observation.source_id
        assert restored.evidence == sample_observation.evidence
        assert restored.observed_at == sample_observation.observed_at
        assert restored.period_start == sample_observation.period_start
        assert restored.period_end == sample_observation.period_end
        assert restored.content_source == sample_observation.content_source
        assert restored.user_intent_signal == sample_observation.user_intent_signal
        assert restored.user_notes == sample_observation.user_notes
        assert restored.version == sample_observation.version

    def test_contrast_observation_type_roundtrip(self):
        obs = Observation(
            dimension=Dimension.DECISIONS,
            observation_type=ObservationType.CONTRAST,
            value={"expected": "fast", "actual": "delayed"},
            source_type=SourceType.RAW,
        )

        payload = obs.to_dict()
        assert payload["observation_type"] == "contrast"
        assert Observation.from_dict(payload).observation_type == ObservationType.CONTRAST

    def test_from_dict_defaults(self):
        d = {
            "id": "obs-003",
            "dimension": "growth",
            "observation_type": "pattern",
            "value": {"signals": 3},
            "source_type": "raw",
            "created_at": "2026-06-01T00:00:00",
            "updated_at": "2026-06-01T00:00:00",
        }
        obs = Observation.from_dict(d)
        assert obs.dimension == Dimension.GROWTH
        assert obs.observation_type == ObservationType.PATTERN
        assert obs.unit == ""
        assert obs.confidence == pytest.approx(1.0)
        assert obs.source_path == ""
        assert obs.source_id == ""
        assert obs.evidence == []
        assert obs.observed_at is None
        assert obs.period_start is None
        assert obs.period_end is None
        assert obs.content_source == ContentSource.UNKNOWN
        assert obs.user_intent_signal == UserIntent.UNKNOWN
        assert obs.user_notes == ""
        assert obs.version == 1


class TestObservationBatch:
    """Tests for ObservationBatch helpers."""

    def test_add_updates_counts_and_list(self):
        batch = ObservationBatch()
        obs = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"x": 1},
        )
        batch.add(obs)
        assert len(batch.observations) == 1
        assert batch.dimension_counts == {"attention": 1}

        obs2 = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.PATTERN,
            value={"y": 2},
        )
        batch.add(obs2)
        assert len(batch.observations) == 2
        assert batch.dimension_counts == {"attention": 2}

    def test_total_observations_prefers_exact_paginated_total(self, sample_observation):
        paged = ObservationBatch(observation_total=9, observations=[sample_observation])

        assert paged.total_observations == 9

    def test_by_dimension(self, observation_batch):
        attention = observation_batch.by_dimension(Dimension.ATTENTION)
        assert len(attention) == 1
        assert attention[0].id == "obs-001"

        time_obs = observation_batch.by_dimension(Dimension.TIME)
        assert len(time_obs) == 1
        assert time_obs[0].id == "obs-002"

        empty = observation_batch.by_dimension(Dimension.STRESS)
        assert empty == []

    def test_by_type(self, observation_batch):
        freq = observation_batch.by_type(ObservationType.FREQUENCY)
        assert len(freq) == 1
        assert freq[0].id == "obs-001"

        trend = observation_batch.by_type(ObservationType.TREND)
        assert len(trend) == 1
        assert trend[0].id == "obs-002"

        empty = observation_batch.by_type(ObservationType.DEVIATION)
        assert empty == []
