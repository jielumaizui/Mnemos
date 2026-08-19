# -*- coding: utf-8 -*-
"""Additional edge-path tests for core.reflection.reflection_engine."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from core.reflection.models import (
    FeedbackType,
    ReflectionRecord,
    ReflectionTrigger,
)
from core.reflection.reflection_engine import ReflectionEngine, ReflectionResult
from core.reflection.time_awareness import TemporalContext


def _make_engine():
    obs_store = MagicMock()
    ref_store = MagicMock()
    ref_store.save_record.return_value = None

    temporal = TemporalContext(
        now=datetime.now(),
        now_str="",
        rhythm="normal",
        rhythm_description="",
    )
    time_awareness = MagicMock()
    time_awareness.get_temporal_context.return_value = temporal

    trigger_detector = MagicMock()
    trigger_detector.detect.return_value = None

    mirror_engine = MagicMock()
    mirror = MagicMock()
    mirror.dimensions_involved = ["decisions"]
    mirror.snapshots = []
    mirror_engine.build_mirror.return_value = mirror

    insight = MagicMock()
    insight.summary = "insight summary"
    insight.confidence = 0.8
    insight.to_snapshot.return_value = MagicMock()
    insight_generator = MagicMock()
    insight_generator.generate.return_value = insight

    calibrator = MagicMock()
    cal_params = MagicMock()
    cal_params.dimension_weights = {}
    cal_params.skip_dimensions = []
    cal_params.generation_hints = []
    cal_params.confidence_threshold = 0.5
    calibrator.get_calibration_params.return_value = cal_params
    calibrator.apply_to_insight_result.return_value = insight

    validator = MagicMock()
    validation = MagicMock()
    validation.overall_score = 0.8
    validation.passed = True
    validation.findings = []
    validation.to_feedback_equivalent.return_value = "accurate"
    validator.validate.return_value = validation

    feedback_collector = MagicMock()
    fb_result = MagicMock()
    fb_result.messages = []
    feedback_collector.submit_feedback.return_value = fb_result
    feedback_collector.get_pending_feedback.return_value = []
    feedback_collector.get_feedback_summary.return_value = {}

    feedback_loop = MagicMock()
    feedback_loop.process_reflection.return_value = fb_result

    implicit_detector = MagicMock()
    implicit_detector.detect.return_value = None

    deviation_detector = MagicMock()
    deviation_detector.get_session.return_value = None

    engine = ReflectionEngine(
        observation_store=obs_store,
        reflection_store=ref_store,
        evidence_graph=None,
        reflection_router=None,
        wiki_dir=None,
        export_to_wiki=False,
    )
    engine.time_awareness = time_awareness
    engine.trigger_detector = trigger_detector
    engine.mirror_engine = mirror_engine
    engine.insight_generator = insight_generator
    engine.calibrator = calibrator
    engine.internal_validator = validator
    engine.feedback_collector = feedback_collector
    engine.feedback_loop = feedback_loop
    engine.implicit_detector = implicit_detector
    engine.deviation_detector = deviation_detector
    return engine


class TestReflectManuallyAndQuality:
    def test_reflect_manually_uses_reflection_capability(self):
        engine = _make_engine()
        route = MagicMock()
        route.scene = "new_project"
        route.role.value = "builder"
        engine.reflection_router = MagicMock()
        engine.reflection_router.route.return_value = route

        cap_result = MagicMock()
        cap_result.mirror = MagicMock(dimensions_involved=["decisions"])
        cap_result.insight_result = MagicMock()
        cap_result.record = ReflectionRecord(
            id="rid",
            trigger=ReflectionTrigger.MANUAL,
            trigger_event="new_project",
            user_query="q",
        )

        mock_cap_class = MagicMock()
        mock_cap_instance = MagicMock()
        mock_cap_instance.reflect.return_value = cap_result
        mock_cap_instance.store.return_value = cap_result.record
        mock_cap_class.return_value = mock_cap_instance

        with patch("core.reflection.reflection_capability.ReflectionCapability", mock_cap_class):
            result = engine.reflect_manually("帮我分析最近的决策模式")

        assert isinstance(result, ReflectionResult)
        assert result.triggered is True
        assert result.record is cap_result.record
        engine.feedback_loop.process_reflection.assert_called_once()

    def test_reflect_manually_defaults_scene_to_trigger(self):
        engine = _make_engine()
        route = MagicMock()
        route.scene = "default"
        route.role.value = "default"
        engine.reflection_router = MagicMock()
        engine.reflection_router.route.return_value = route

        cap_result = MagicMock()
        cap_result.mirror = MagicMock(dimensions_involved=[])
        cap_result.insight_result = MagicMock()
        cap_result.record = ReflectionRecord(
            id="rid",
            trigger=ReflectionTrigger.MANUAL,
            trigger_event="manual",
            user_query="q",
        )

        mock_cap_instance = MagicMock()
        mock_cap_instance.reflect.return_value = cap_result
        mock_cap_instance.store.return_value = cap_result.record

        with patch(
            "core.reflection.reflection_capability.ReflectionCapability",
            return_value=mock_cap_instance,
        ):
            engine.reflect_manually("分析")

        call_kwargs = mock_cap_instance.reflect.call_args.kwargs
        assert call_kwargs["scene"] == ReflectionTrigger.MANUAL.value

    def test_reflect_manually_passes_experience_matcher(self):
        engine = _make_engine()
        route = MagicMock()
        route.scene = "new_project"
        route.role.value = "builder"
        engine.reflection_router = MagicMock()
        engine.reflection_router.route.return_value = route

        cap_result = MagicMock()
        cap_result.mirror = MagicMock(dimensions_involved=[])
        cap_result.insight_result = MagicMock()
        cap_result.record = ReflectionRecord(
            id="rid",
            trigger=ReflectionTrigger.MANUAL,
            trigger_event="new_project",
            user_query="q",
        )

        mock_cap_class = MagicMock()
        mock_cap_instance = MagicMock()
        mock_cap_instance.reflect.return_value = cap_result
        mock_cap_instance.store.return_value = cap_result.record
        mock_cap_class.return_value = mock_cap_instance

        with patch("core.reflection.reflection_capability.ReflectionCapability", mock_cap_class):
            engine.reflect_manually("帮我分析最近的决策模式")

        assert (
            mock_cap_class.call_args.kwargs.get("experience_matcher") is engine.experience_matcher
        )

    def test_get_insight_quality_report(self):
        engine = _make_engine()
        with patch("core.reflection.feedback_analytics.FeedbackAnalytics") as analytics_cls:
            analytics_instance = MagicMock()
            analytics_instance.get_insight_quality_report.return_value = {"score": 0.9}
            analytics_cls.return_value = analytics_instance

            report = engine.get_insight_quality_report(days=7)

        assert report == {"score": 0.9}
        analytics_instance.get_insight_quality_report.assert_called_once_with(7)


class TestValidationAndConsumers:
    def test_reflect_on_user_input_appends_validation_warning(self):
        engine = _make_engine()
        trigger_event = MagicMock()
        trigger_event.trigger = ReflectionTrigger.NEW_PROJECT
        trigger_event.raw_text = "新项目"
        trigger_event.to_dict.return_value = {}
        engine.trigger_detector.detect.return_value = trigger_event

        validation = MagicMock()
        validation.overall_score = 0.3
        validation.passed = False
        validation.findings = []
        validation.to_feedback_equivalent.return_value = "inaccurate"
        engine.internal_validator.validate.return_value = validation

        result = engine.reflect_on_user_input("我要启动新项目")
        assert any("系统自检" in msg for msg in result.feedback_messages)

    def test_notify_consumer_exception_swallowed(self):
        engine = _make_engine()
        bad_consumer = MagicMock()
        bad_consumer.on_insight_generated.side_effect = RuntimeError("boom")
        good_consumer = MagicMock()

        engine.register_consumer(bad_consumer)
        engine.register_consumer(good_consumer)
        record = MagicMock()
        engine._notify_consumers("insight_generated", record)

        good_consumer.on_insight_generated.assert_called_once_with(record)
        bad_consumer.on_insight_generated.assert_called_once_with(record)

    def test_feedback_collected_event_routing(self):
        engine = _make_engine()
        consumer = MagicMock()
        engine.register_consumer(consumer)
        shift = MagicMock()
        engine._notify_consumers("cognitive_shift", shift)
        consumer.on_cognitive_shift.assert_called_once_with(shift)


class TestSubmitSessionContext:
    def test_no_implicit_feedback(self):
        engine = _make_engine()
        context = MagicMock()
        context.reflection_id = "rid"
        result = engine.submit_session_context(context)
        assert result["inferred"] is False

    def test_target_record_not_found(self):
        engine = _make_engine()
        implicit = MagicMock()
        implicit.inferred_type = FeedbackType.ACCURATE
        implicit.confidence = 0.8
        implicit.signals = []
        implicit.inferred_at = datetime.now()
        engine.implicit_detector.detect.return_value = implicit
        engine.ref_store.get_by_id.return_value = None

        context = MagicMock()
        context.reflection_id = "rid"
        result = engine.submit_session_context(context)
        assert result["inferred"] is False
        assert "不存在" in result["reason"]


class TestExportReflectionProjection:
    def test_export_with_wiki_dir(self, tmp_path):
        engine = _make_engine()
        engine.wiki_dir = str(tmp_path)
        engine.export_to_wiki = True

        exporter = MagicMock()
        exporter.week_start.return_value = datetime(2026, 7, 20)
        exporter.shifts_for_week.return_value = []
        engine._reflection_exporter = exporter

        record = ReflectionRecord(
            id="rid",
            trigger=ReflectionTrigger.NEW_PROJECT,
            trigger_event="t",
            user_query="q",
        )
        engine.ref_store.get_by_id.return_value = record
        engine.ref_store.get_all_shifts_for_projection.return_value = []
        engine.ref_store.get_all_for_projection.return_value = [record]
        engine._export_reflection_projection(record)

        exporter.export_record.assert_called_once_with(record)
        exporter.export_shifts.assert_called_once()
        exporter.export_weekly_report.assert_called_once()

    def test_export_exception_propagates(self, tmp_path):
        engine = _make_engine()
        engine.wiki_dir = str(tmp_path)
        engine.export_to_wiki = True

        exporter = MagicMock()
        exporter.export_record.side_effect = RuntimeError("disk full")
        engine._reflection_exporter = exporter

        record = ReflectionRecord(
            id="rid",
            trigger=ReflectionTrigger.NEW_PROJECT,
            trigger_event="t",
            user_query="q",
        )
        engine.ref_store.get_by_id.return_value = record
        with pytest.raises(RuntimeError, match="disk full"):
            engine._export_reflection_projection(record)
        exporter.export_record.assert_called_once()
