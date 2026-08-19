# -*- coding: utf-8 -*-
"""Unit tests for core.reflection.feedback_loop."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock


from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.models import Dimension, ObservationType
from core.reflection.feedback_loop import FeedbackLoop
from core.reflection.models import (
    MirrorSnapshot,
    ReflectionRecord,
    ReflectionTrigger,
)


def _make_snapshot(dimension: str, value_summary: str, days_ago: int = 0):
    return MirrorSnapshot(
        observation_id="obs-1",
        dimension=dimension,
        value_summary=value_summary,
        evidence_summary="evidence",
        confidence=0.8,
        recency_weight=1.0,
        period_end=datetime.now() - timedelta(days=days_ago),
    )


def _make_record(snapshot, trigger=ReflectionTrigger.MANUAL):
    return ReflectionRecord(
        trigger=trigger,
        trigger_event="test",
        user_query="q",
        mirror_snapshots=[snapshot],
        mirror_dimensions=[snapshot.dimension],
    )


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="mcp:codex:feedback-loop",
        agent="codex",
        host_kind="test",
        capability_id="feedback-loop",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _narrowing() -> AccessNarrowing:
    return AccessNarrowing(session_id="session-1", project="mnemos")


class TestFeedbackLoop:
    def test_process_reflection_no_store(self):
        loop = FeedbackLoop()
        record = _make_record(_make_snapshot("growth", "工程师"))
        result = loop.process_reflection(record)
        assert result.messages == ["ReflectionStore 未配置，跳过反哺"]

    def test_process_reflection_detects_shift(self):
        ref_store = MagicMock()
        obs_store = MagicMock()
        loop = FeedbackLoop(reflection_store=ref_store, observation_store=obs_store)

        old_snapshot = _make_snapshot("growth", "工程师", days_ago=60)
        old_record = _make_record(old_snapshot)
        # created_at 是 now - 60 days
        old_record.created_at = datetime.now() - timedelta(days=60)

        new_snapshot = _make_snapshot("growth", "团队管理者", days_ago=0)
        new_record = _make_record(new_snapshot)

        ref_store.authorized_get_by_trigger.return_value = ([old_record], {})

        result = loop.process_reflection(
            new_record,
            principal=_principal(),
            narrowing=_narrowing(),
        )

        assert len(result.shifts_detected) == 1
        assert result.shifts_detected[0].dimension == "growth"
        assert result.shifts_detected[0].shift_type == "role_change_to_manager"
        ref_store.save_shift.assert_called_once()
        obs_store.save.assert_called_once()
        ref_store.mark_fed_back.assert_called_once()

    def test_process_reflection_no_shift_same_summary(self):
        ref_store = MagicMock()
        loop = FeedbackLoop(reflection_store=ref_store)

        snapshot = _make_snapshot("growth", "工程师", days_ago=60)
        old_record = _make_record(snapshot)
        old_record.created_at = datetime.now() - timedelta(days=60)

        new_record = _make_record(_make_snapshot("growth", "工程师"))
        ref_store.authorized_get_by_trigger.return_value = ([old_record], {})

        result = loop.process_reflection(
            new_record,
            principal=_principal(),
            narrowing=_narrowing(),
        )
        assert result.shifts_detected == []

    def test_process_reflection_interval_too_short(self):
        ref_store = MagicMock()
        loop = FeedbackLoop(reflection_store=ref_store)

        old_snapshot = _make_snapshot("growth", "工程师", days_ago=5)
        old_record = _make_record(old_snapshot)
        old_record.created_at = datetime.now() - timedelta(days=5)

        new_snapshot = _make_snapshot("growth", "团队管理者")
        new_record = _make_record(new_snapshot)
        ref_store.authorized_get_by_trigger.return_value = ([old_record], {})

        result = loop.process_reflection(
            new_record,
            principal=_principal(),
            narrowing=_narrowing(),
        )
        assert result.shifts_detected == []

    def test_shift_to_observation(self):
        loop = FeedbackLoop()
        shift = MagicMock()
        shift.dimension = "attention"
        shift.shift_type = "focus_shift"
        shift.from_state = "a"
        shift.to_state = "b"
        shift.confidence = 0.7
        shift.shift_detected_at = datetime.now()
        shift.first_seen_at = datetime.now() - timedelta(days=30)

        obs = loop._shift_to_observation(shift)
        assert obs is not None
        assert obs.dimension == Dimension.ATTENTION
        assert obs.observation_type == ObservationType.TREND
        assert obs.value["shift_type"] == "focus_shift"

    def test_shift_to_knowledge_update_known_type(self):
        loop = FeedbackLoop()
        shift = MagicMock()
        shift.shift_type = "focus_shift"
        shift.dimension = "attention"
        shift.confidence = 0.7
        shift.shift_detected_at = datetime.now()

        update = loop._shift_to_knowledge_update(shift)
        assert update["suggestion"] == "用户关注重心已转移，建议更新 MOC 和项目优先级"

    def test_shift_to_knowledge_update_unknown_type(self):
        loop = FeedbackLoop()
        shift = MagicMock()
        shift.shift_type = "unknown_shift"
        shift.dimension = "growth"
        shift.confidence = 0.5
        shift.shift_detected_at = datetime.now()

        update = loop._shift_to_knowledge_update(shift)
        assert "用户认知发生变化" in update["suggestion"]
