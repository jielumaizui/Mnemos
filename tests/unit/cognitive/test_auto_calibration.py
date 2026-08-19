"""Unit tests for core.cognitive.auto_calibration validators."""

import hashlib

import pytest

from core.cognitive.auto_calibration import (
    CalibrationEngine,
    ContradictionDetector,
    CrossSourceValidator,
    ValidationResult,
    Validator,
)
from core.cognitive.models import Dimension, Observation, ObservationType
from core.cognitive.sources import SourceItem


def _canonical_raw(revision_id: str, content: str) -> SourceItem:
    return SourceItem(
        source_type="raw",
        file_path=f"raw://{revision_id}",
        content=content,
        raw_revision_id=revision_id,
    )


def _derived_wiki(revision_id: str, content: str, *, raw_content: str) -> SourceItem:
    raw_content_hash = "sha256:" + hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
    return SourceItem(
        source_type="wiki",
        file_path=f"/wiki/{revision_id}.md",
        content=content,
        frontmatter={
            "raw_event_refs": [
                {
                    "revision_id": revision_id,
                    "content_hash": raw_content_hash,
                    "span_start": 0,
                    "span_end": len(raw_content),
                }
            ]
        },
    )

# ───────────────────────────────────────────────
# Validator base class
# ───────────────────────────────────────────────


class DummyValidator(Validator):
    """Concrete validator implementation for testing the abstract base."""

    name = "dummy"

    def validate(self, obs, all_observations, source_items):
        return ValidationResult(
            validator_name=self.name,
            score=1.0,
            verdict="confirmed",
            reason="always confirmed",
        )


class TestValidatorBase:
    """Tests for the Validator abstract base class."""

    def test_validator_subclass_has_name(self):
        assert DummyValidator().name == "dummy"

    def test_validator_returns_validation_result(self):
        obs = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"x": 1},
        )
        result = DummyValidator().validate(obs, [], [])
        assert isinstance(result, ValidationResult)
        assert result.validator_name == "dummy"
        assert result.verdict == "confirmed"

    def test_abstract_validator_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            Validator()


# ───────────────────────────────────────────────
# CrossSourceValidator
# ───────────────────────────────────────────────


@pytest.fixture
def cross_validator():
    return CrossSourceValidator()


class TestCrossSourceValidator:
    """Tests for CrossSourceValidator verdict logic."""

    def test_inconclusive_when_only_wiki_items(self, cross_validator):
        obs = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"concepts": {"ai": 5}, "total_mentions": 5, "dominant": "ai"},
        )
        source_items = [
            SourceItem(source_type="wiki", file_path="/wiki/a.md", content="AI is important.")
        ]
        result = cross_validator.validate(obs, [], source_items)
        assert result.verdict == "inconclusive"
        assert "lineage cluster" in result.reason

    def test_inconclusive_when_only_raw_items(self, cross_validator):
        obs = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"concepts": {"ai": 5}, "total_mentions": 5, "dominant": "ai"},
        )
        source_items = [
            SourceItem(source_type="raw", file_path="/raw/a.md", content="We talk about AI.")
        ]
        result = cross_validator.validate(obs, [], source_items)
        assert result.verdict == "inconclusive"
        assert "lineage cluster" in result.reason

    def test_questionable_when_keywords_missing(self, cross_validator):
        obs = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"concepts": {"ai": 5}, "total_mentions": 5, "dominant": "ai"},
        )
        source_items = [
            _canonical_raw("raw-a", "Nothing relevant here."),
            _canonical_raw("raw-b", "Also nothing."),
        ]
        result = cross_validator.validate(obs, [], source_items)
        assert result.verdict == "questionable"
        assert "均未出现" in result.reason

    def test_same_raw_and_derived_wiki_count_as_one_cluster(self, cross_validator):
        obs = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"concepts": {"ai": 5}, "total_mentions": 5, "dominant": "ai"},
        )
        source_items = [
            _canonical_raw("raw-a", "AI AI"),
            _derived_wiki("raw-a", "AI AI AI", raw_content="AI AI"),
        ]
        result = cross_validator.validate(obs, [], source_items)
        assert result.verdict == "questionable"
        assert len(result.supporting_cluster_ids) == 1
        assert "已去重 1 个派生 L2" in result.reason

    def test_confirmed_only_for_two_independent_raw_roots(self, cross_validator):
        obs = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"concepts": {"ai": 5}, "total_mentions": 5, "dominant": "ai"},
        )
        source_items = [
            _canonical_raw("raw-a", "AI"),
            _canonical_raw("raw-b", "AI AI"),
        ]
        result = cross_validator.validate(obs, [], source_items)
        assert result.verdict == "confirmed"
        assert len(result.supporting_cluster_ids) == 2

    def test_malformed_derived_ref_never_becomes_independent(self, cross_validator):
        obs = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"concepts": {"ai": 5}},
        )
        malformed = SourceItem(
            source_type="wiki",
            file_path="/wiki/bad.md",
            content="AI",
            frontmatter={"raw_event_refs": [{"revision_id": "raw-a"}]},
        )
        result = cross_validator.validate(obs, [], [malformed])
        assert result.verdict == "inconclusive"
        assert malformed.lineage_status == "malformed"


# ───────────────────────────────────────────────
# ContradictionDetector
# ───────────────────────────────────────────────


@pytest.fixture
def contradiction_detector():
    return ContradictionDetector()


class TestContradictionDetector:
    """Tests for ContradictionDetector verdict logic."""

    def test_confirmed_when_no_contradiction(self, contradiction_detector):
        obs = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"concepts": {"ai": 5}, "total_mentions": 5, "dominant": "ai"},
        )
        all_observations = [
            obs,
            Observation(
                dimension=Dimension.ACTIONS,
                observation_type=ObservationType.RATIO,
                value={"started": 5, "completed": 5, "blocked": 0, "completion_rate": 1.0},
            ),
        ]
        result = contradiction_detector.validate(obs, all_observations, [])
        assert result.verdict == "confirmed"
        assert "未检测到" in result.reason

    def test_questionable_when_attention_ai_but_low_completion(self, contradiction_detector):
        att_obs = Observation(
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"concepts": {"ai": 5}, "total_mentions": 5, "dominant": "ai"},
        )
        act_obs = Observation(
            dimension=Dimension.ACTIONS,
            observation_type=ObservationType.RATIO,
            value={"started": 5, "completed": 1, "blocked": 4, "completion_rate": 0.1},
        )
        result = contradiction_detector.validate(att_obs, [att_obs, act_obs], [])
        assert result.verdict == "questionable"
        assert "关注但不行动" in result.reason

    def test_questionable_when_priority_high_and_delay_ratio_high(self, contradiction_detector):
        dec_obs = Observation(
            dimension=Dimension.DECISIONS,
            observation_type=ObservationType.PATTERN,
            value={"priority": 60},
        )
        time_obs = Observation(
            dimension=Dimension.TIME,
            observation_type=ObservationType.FREQUENCY,
            value={"estimates": 10, "delays": 5, "delay_ratio": 0.5},
        )
        result = contradiction_detector.validate(dec_obs, [dec_obs, time_obs], [])
        assert result.verdict == "questionable"
        assert "计划多执行差" in result.reason

    def test_questionable_when_high_stress_low_growth(self, contradiction_detector):
        stress_obs = Observation(
            dimension=Dimension.STRESS,
            observation_type=ObservationType.FREQUENCY,
            value={"stress_signals": 60},
        )
        growth_obs = Observation(
            dimension=Dimension.GROWTH,
            observation_type=ObservationType.FREQUENCY,
            value={"growth_signals": 5},
        )
        result = contradiction_detector.validate(stress_obs, [stress_obs, growth_obs], [])
        assert result.verdict == "questionable"
        assert "高压力低成长" in result.reason


# ───────────────────────────────────────────────
# CalibrationEngine integration
# ───────────────────────────────────────────────


class TestCalibrationEngine:
    """Smoke tests for CalibrationEngine."""

    def test_calibrate_batch_updates_confidence(self):
        class SecondDummyValidator(DummyValidator):
            name = "dummy_second"

        engine = CalibrationEngine(
            validators=[DummyValidator(), SecondDummyValidator()]
        )
        obs = Observation(
            id="obs-001",
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"concepts": {"ai": 5}},
            confidence=0.5,
        )
        batch = __import__("core.cognitive.models", fromlist=["ObservationBatch"]).ObservationBatch(
            observations=[obs]
        )
        reports = engine.calibrate_batch(batch, [])

        assert "obs-001" in reports
        assert reports["obs-001"].overall_verdict == "confirmed"
        assert reports["obs-001"].calibrated_confidence == pytest.approx(0.75)
        assert obs.confidence == pytest.approx(0.5)

    def test_calibration_report_contains_validations(self):
        engine = CalibrationEngine(validators=[CrossSourceValidator()])
        obs = Observation(
            id="obs-002",
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"concepts": {"ai": 5}},
            confidence=0.8,
        )
        batch = __import__("core.cognitive.models", fromlist=["ObservationBatch"]).ObservationBatch(
            observations=[obs]
        )
        reports = engine.calibrate_batch(batch, [])

        assert reports["obs-002"].validations
        assert reports["obs-002"].validations[0].validator_name == "cross_source"
