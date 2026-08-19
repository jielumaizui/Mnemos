"""Tests for core.reflection.models."""

from datetime import datetime

import pytest

from core.reflection.models import (
    CognitiveShift,
    CognitiveTrajectory,
    FeedbackType,
    InsightSnapshot,
    MirrorSnapshot,
    ReflectionRecord,
    ReflectionTrigger,
    UserFeedback,
)


def test_reflection_trigger_enum_round_trip():
    for trigger in ReflectionTrigger:
        assert ReflectionTrigger(trigger.value) == trigger


def test_feedback_type_enum_round_trip():
    for fb in FeedbackType:
        assert FeedbackType(fb.value) == fb


def test_reflection_record_to_dict_round_trip():
    now = datetime(2026, 6, 10, 10, 0, 0)
    record = ReflectionRecord(
        id="r1",
        created_at=now,
        trigger=ReflectionTrigger.MAJOR_DECISION,
        trigger_event="启动项目",
        user_query="我要启动新项目",
        mirror_dimensions=["attention", "decisions"],
        mirror_snapshots=[
            MirrorSnapshot(
                observation_id="obs-1",
                dimension="attention",
                value_summary="关注分布",
                evidence_summary="证据1",
                confidence=0.8,
                recency_weight=0.9,
                period_end=now,
            ),
        ],
        insight=InsightSnapshot(
            summary="洞察摘要",
            key_points=["要点1"],
            dimensions_involved=["attention", "decisions"],
        ),
        temporal_context={"rhythm": "workday_morning"},
        user_feedback=UserFeedback(
            feedback_type=FeedbackType.ACCURATE,
            comment="准",
            given_at=now,
        ),
        fed_back_to_observations=True,
        fed_back_to_knowledge=False,
    )

    d = record.to_dict()
    assert d["id"] == "r1"
    assert d["created_at"] == now.isoformat()
    assert d["trigger"] == "major_decision"
    assert d["trigger_event"] == "启动项目"
    assert d["user_query"] == "我要启动新项目"
    assert d["mirror_dimensions"] == ["attention", "decisions"]
    assert len(d["mirror_snapshots"]) == 1
    assert d["mirror_snapshots"][0]["period_end"] == now.isoformat()
    assert d["insight"]["summary"] == "洞察摘要"
    assert d["insight"]["key_points"] == ["要点1"]
    assert d["temporal_context"] == {"rhythm": "workday_morning"}
    assert d["user_feedback"]["feedback_type"] == "accurate"
    assert d["user_feedback"]["comment"] == "准"
    assert d["user_feedback"]["given_at"] == now.isoformat()
    assert d["fed_back_to_observations"] is True
    assert d["fed_back_to_knowledge"] is False


def test_cognitive_shift_to_dict():
    first_seen = datetime(2026, 1, 1, 0, 0, 0)
    detected = datetime(2026, 6, 10, 0, 0, 0)
    shift = CognitiveShift(
        dimension="growth",
        shift_type="role_change",
        from_state="开发者",
        to_state="技术负责人",
        confidence=0.85,
        evidence=["ev1", "ev2"],
        first_seen_at=first_seen,
        shift_detected_at=detected,
    )
    d = shift.to_dict()
    assert d["dimension"] == "growth"
    assert d["shift_type"] == "role_change"
    assert d["from_state"] == "开发者"
    assert d["to_state"] == "技术负责人"
    assert d["confidence"] == pytest.approx(0.85)
    assert d["evidence"] == ["ev1", "ev2"]
    assert d["first_seen_at"] == first_seen.isoformat()
    assert d["shift_detected_at"] == detected.isoformat()


def test_cognitive_trajectory_tracks_state_history():
    detected1 = datetime(2026, 1, 1, 0, 0, 0)
    detected2 = datetime(2026, 6, 1, 0, 0, 0)
    trajectory = CognitiveTrajectory(dimension="growth")

    shift1 = CognitiveShift(
        dimension="growth",
        shift_type="role_change",
        from_state="开发者",
        to_state="技术负责人",
        confidence=0.8,
        evidence=["ev1"],
        first_seen_at=detected1,
        shift_detected_at=detected1,
    )
    shift2 = CognitiveShift(
        dimension="growth",
        shift_type="role_change",
        from_state="技术负责人",
        to_state="管理者",
        confidence=0.9,
        evidence=["ev2"],
        first_seen_at=detected1,
        shift_detected_at=detected2,
    )

    trajectory.add_shift(shift1)
    assert trajectory.current_state == "技术负责人"
    assert len(trajectory.shifts) == 1
    assert len(trajectory.state_history) == 1
    assert trajectory.state_history[0]["state"] == "技术负责人"

    trajectory.add_shift(shift2)
    assert trajectory.current_state == "管理者"
    assert len(trajectory.shifts) == 2
    assert len(trajectory.state_history) == 2
    assert trajectory.state_history[1]["state"] == "管理者"
    assert trajectory.state_history[1]["confidence"] == pytest.approx(0.9)


def test_reflection_record_defaults_have_id_and_created_at():
    record = ReflectionRecord()
    assert record.id
    assert isinstance(record.id, str)
    assert record.created_at is not None
    assert record.trigger == ReflectionTrigger.MANUAL
    assert record.mirror_snapshots == []
    assert record.mirror_dimensions == []
    assert record.insight is None
