"""Tests for core.reflection.internal_validator."""

from core.reflection.insight_generator import InsightResult
from core.reflection.internal_validator import InternalValidator
from core.reflection.mirror_engine import MirrorResult
from core.reflection.models import MirrorSnapshot


def _make_mirror(snapshots):
    return MirrorResult(
        snapshots=snapshots,
        dimensions_involved=list({s.dimension for s in snapshots}),
    )


def _make_insight(dims, confidence=0.8):
    return InsightResult(
        summary="summary",
        key_points=["kp"],
        dimensions_involved=dims,
        confidence=confidence,
    )


def test_validate_passes_with_strong_evidence():
    validator = InternalValidator()
    snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="attention",
            value_summary="value",
            evidence_summary="evidence",
            confidence=0.8,
            recency_weight=0.8,
        ),
        MirrorSnapshot(
            observation_id="obs-2",
            dimension="attention",
            value_summary="value2",
            evidence_summary="evidence2",
            confidence=0.85,
            recency_weight=0.85,
        ),
        MirrorSnapshot(
            observation_id="obs-3",
            dimension="decisions",
            value_summary="value3",
            evidence_summary="evidence3",
            confidence=0.8,
            recency_weight=0.8,
        ),
        MirrorSnapshot(
            observation_id="obs-4",
            dimension="decisions",
            value_summary="value4",
            evidence_summary="evidence4",
            confidence=0.82,
            recency_weight=0.82,
        ),
    ]
    mirror = _make_mirror(snapshots)
    insight = _make_insight(["attention", "decisions"], confidence=0.8)
    result = validator.validate(mirror, insight)

    assert result.passed is True
    assert result.overall_score >= 0.75
    statuses = {f.check_name: f.status for f in result.findings}
    assert statuses["evidence_coverage"] == "pass"
    assert statuses["recency_validity"] == "pass"
    assert statuses["confidence_consistency"] == "pass"
    assert statuses["dimension_independence"] == "pass"
    assert statuses["evidence_quantity"] == "pass"


def test_validate_reports_low_recency_fail():
    validator = InternalValidator()
    snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="attention",
            value_summary="value",
            evidence_summary="evidence",
            confidence=0.8,
            recency_weight=0.1,
        ),
        MirrorSnapshot(
            observation_id="obs-2",
            dimension="attention",
            value_summary="value2",
            evidence_summary="evidence2",
            confidence=0.8,
            recency_weight=0.1,
        ),
    ]
    mirror = _make_mirror(snapshots)
    insight = _make_insight(["attention"], confidence=0.8)
    result = validator.validate(mirror, insight)

    recency_finding = next(f for f in result.findings if f.check_name == "recency_validity")
    assert recency_finding.status == "fail"
    assert recency_finding.score < InternalValidator.MIN_AVG_RECENCY


def test_validate_warns_on_uncovered_dimensions():
    validator = InternalValidator()
    snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="attention",
            value_summary="value",
            evidence_summary="evidence",
            confidence=0.8,
            recency_weight=0.8,
        ),
    ]
    mirror = _make_mirror(snapshots)
    insight = _make_insight(["attention", "stress"], confidence=0.8)
    result = validator.validate(mirror, insight)

    coverage = next(f for f in result.findings if f.check_name == "evidence_coverage")
    assert coverage.status == "warn"
    assert "stress" in coverage.message


def test_validate_warns_on_insight_confidence_too_high():
    validator = InternalValidator()
    snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="attention",
            value_summary="value",
            evidence_summary="evidence",
            confidence=0.5,
            recency_weight=0.8,
        ),
    ]
    mirror = _make_mirror(snapshots)
    insight = _make_insight(["attention"], confidence=0.9)
    result = validator.validate(mirror, insight)

    consistency = next(f for f in result.findings if f.check_name == "confidence_consistency")
    assert consistency.status == "warn"


def test_validate_warns_on_dimension_confidence_gap():
    validator = InternalValidator()
    snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="attention",
            value_summary="value",
            evidence_summary="evidence",
            confidence=0.95,
            recency_weight=0.8,
        ),
        MirrorSnapshot(
            observation_id="obs-2",
            dimension="attention",
            value_summary="value2",
            evidence_summary="evidence2",
            confidence=0.95,
            recency_weight=0.8,
        ),
        MirrorSnapshot(
            observation_id="obs-3",
            dimension="decisions",
            value_summary="value3",
            evidence_summary="evidence3",
            confidence=0.3,
            recency_weight=0.8,
        ),
        MirrorSnapshot(
            observation_id="obs-4",
            dimension="decisions",
            value_summary="value4",
            evidence_summary="evidence4",
            confidence=0.3,
            recency_weight=0.8,
        ),
    ]
    mirror = _make_mirror(snapshots)
    insight = _make_insight(["attention", "decisions"], confidence=0.6)
    result = validator.validate(mirror, insight)

    independence = next(f for f in result.findings if f.check_name == "dimension_independence")
    assert independence.status == "warn"


def test_validate_warns_on_insufficient_evidence_per_dimension():
    validator = InternalValidator()
    snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="attention",
            value_summary="value",
            evidence_summary="evidence",
            confidence=0.8,
            recency_weight=0.8,
        ),
    ]
    mirror = _make_mirror(snapshots)
    insight = _make_insight(["attention"], confidence=0.8)
    result = validator.validate(mirror, insight)

    quantity = next(f for f in result.findings if f.check_name == "evidence_quantity")
    assert quantity.status == "warn"


def test_to_feedback_equivalent():
    validator = InternalValidator()
    snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="attention",
            value_summary="value",
            evidence_summary="evidence",
            confidence=0.8,
            recency_weight=0.8,
        ),
        MirrorSnapshot(
            observation_id="obs-2",
            dimension="attention",
            value_summary="value2",
            evidence_summary="evidence2",
            confidence=0.8,
            recency_weight=0.8,
        ),
    ]
    mirror = _make_mirror(snapshots)

    high = validator.validate(mirror, _make_insight(["attention"], confidence=0.8))
    assert high.to_feedback_equivalent() == "accurate"

    empty_mirror = _make_mirror([])
    low = validator.validate(empty_mirror, _make_insight(["attention"], confidence=0.1))
    assert low.to_feedback_equivalent() == "inaccurate"

    # 1 snapshot with low recency and insufficient quantity -> overall ~0.74
    weak_snapshots = [
        MirrorSnapshot(
            observation_id="obs-1",
            dimension="attention",
            value_summary="value",
            evidence_summary="evidence",
            confidence=0.5,
            recency_weight=0.2,
        ),
    ]
    weak_mirror = _make_mirror(weak_snapshots)
    mid = validator.validate(weak_mirror, _make_insight(["attention"], confidence=0.5))
    assert mid.to_feedback_equivalent() is None
