# -*- coding: utf-8 -*-
"""Unit tests for core.reflection.reflection_engine."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from core.reflection.experience_matcher import ExperienceMatch
from core.reflection.models import (
    CognitiveShift,
    FeedbackType,
    InsightSnapshot,
    ReflectionRecord,
    ReflectionTrigger,
)
from core.reflection.reflection_engine import ReflectionEngine, ReflectionResult
from core.reflection.reflection_exporter import ReflectionExporter
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
    insight.to_snapshot.return_value = InsightSnapshot(
        summary="insight summary", key_points=["k"], dimensions_involved=["decisions"]
    )
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
    feedback_collector.get_feedback_history.return_value = []

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


class TestReflectOnUserInput:
    def test_no_trigger_returns_not_triggered(self):
        engine = _make_engine()
        result = engine.reflect_on_user_input("hello")
        assert isinstance(result, ReflectionResult)
        assert result.triggered is False
        assert result.listening is False

    def test_triggered_saves_record(self):
        engine = _make_engine()
        trigger_event = MagicMock()
        trigger_event.trigger = ReflectionTrigger.NEW_PROJECT
        trigger_event.raw_text = "我要启动新项目"
        trigger_event.to_dict.return_value = {"trigger": "new_project"}
        engine.trigger_detector.detect.return_value = trigger_event

        result = engine.reflect_on_user_input("我要启动新项目")
        assert result.triggered is True
        assert result.mirror is engine.mirror_engine.build_mirror.return_value
        assert result.insight is engine.insight_generator.generate.return_value
        engine.ref_store.save_record.assert_called_once()
        engine.feedback_loop.process_reflection.assert_called_once()

    def test_triggered_uses_experience_matcher(self):
        engine = _make_engine()
        trigger_event = MagicMock()
        trigger_event.trigger = ReflectionTrigger.NEW_PROJECT
        trigger_event.raw_text = "我要启动新项目"
        trigger_event.to_dict.return_value = {"trigger": "new_project"}
        engine.trigger_detector.detect.return_value = trigger_event

        matched = ExperienceMatch(
            source_type="reflection",
            source_id="r-old",
            title="旧 Reflection",
            summary="曾经启动过类似项目",
            score=0.85,
        )
        matcher = MagicMock()
        matcher.find_similar.return_value = [matched]
        engine.experience_matcher = matcher

        engine.reflect_on_user_input("我要启动新项目")

        matcher.find_similar.assert_called_once()
        call_kwargs = engine.insight_generator.generate.call_args.kwargs
        assert call_kwargs.get("experiences") == [matched]

    def test_reflect_manually_passes_use_llm_to_capability(self):
        """reflect_manually 应将 ReflectionEngine 的 use_llm 传给 ReflectionCapability。"""
        from unittest.mock import patch

        engine = _make_engine()
        engine.use_llm = False

        fake_capability_result = MagicMock()
        fake_capability_result.insight_result = MagicMock()
        fake_capability_result.insight_result.summary = "summary"
        fake_capability_result.insight_result.key_points = []
        fake_capability_result.mirror = MagicMock()
        fake_capability_result.scene = "default"

        fake_record = MagicMock()
        fake_record.id = "record-123"

        with patch("core.reflection.reflection_capability.ReflectionCapability") as MockCap:
            fake_cap = MagicMock()
            fake_cap.reflect.return_value = fake_capability_result
            fake_cap.store.return_value = fake_record
            MockCap.return_value = fake_cap

            result = engine.reflect_manually("分析最近状态")

            assert result.triggered is True
            MockCap.assert_called_once()
            call_kwargs = MockCap.call_args.kwargs
            assert call_kwargs.get("use_llm") is False


class TestListeningSession:
    def test_start_listening_no_trigger_returns_none(self):
        engine = _make_engine()
        assert engine.start_listening("sid", "普通对话") is None

    def test_start_listening_success(self):
        engine = _make_engine()
        trigger_event = MagicMock()
        trigger_event.trigger = ReflectionTrigger.NEW_PROJECT
        trigger_event.raw_text = "我想策划活动"
        engine.trigger_detector.detect.return_value = trigger_event

        session = MagicMock()
        session.is_expired = False
        engine.deviation_detector.start_listening.return_value = session

        result = engine.start_listening("sid", "我想策划一场活动")
        assert result is session
        engine.deviation_detector.start_listening.assert_called_once()

    def test_process_message_no_session(self):
        engine = _make_engine()
        result = engine.process_message_in_session("sid", "msg")
        assert result.triggered is False
        assert result.listening is False

    def test_process_message_no_deviation(self):
        engine = _make_engine()
        session = MagicMock()
        session.id = "sid"
        session.is_expired = False
        engine.deviation_detector.get_session.return_value = session
        engine.deviation_detector.add_user_message.return_value = None

        result = engine.process_message_in_session("sid", "继续")
        assert result.triggered is False
        assert result.listening is True

    def test_process_message_with_deviation(self):
        engine = _make_engine()
        session = MagicMock()
        session.id = "sid"
        session.user_messages = ["预计2周完成"]
        signal = MagicMock()
        signal.suggestion = "时间估算可能过于乐观"
        signal.severity = "medium"
        signal.deviation_type = "time_estimation"
        engine.deviation_detector.get_session.return_value = session
        engine.deviation_detector.add_user_message.return_value = signal

        result = engine.process_message_in_session("sid", "预计2周完成")
        assert result.triggered is True
        engine.deviation_detector.close_session.assert_called_once()


class TestConsumersAndHelpers:
    def test_register_and_notify_consumer(self):
        engine = _make_engine()
        consumer = MagicMock()
        engine.register_consumer(consumer)
        record = MagicMock()
        engine._notify_consumers("insight_generated", record)
        consumer.on_insight_generated.assert_called_once_with(record)

    def test_reflect_on_user_input_notifies_consumers(self):
        engine = _make_engine()
        consumer = MagicMock()
        engine.register_consumer(consumer)

        trigger_event = MagicMock()
        trigger_event.trigger = ReflectionTrigger.NEW_PROJECT
        trigger_event.raw_text = "我要启动新项目"
        trigger_event.to_dict.return_value = {"trigger": "new_project"}
        engine.trigger_detector.detect.return_value = trigger_event

        shift = CognitiveShift(
            dimension="decisions",
            shift_type="style_evolution",
            from_state="犹豫",
            to_state="果断",
            confidence=0.8,
            evidence=["e1"],
            first_seen_at=datetime.now(),
        )
        fb_result = MagicMock()
        fb_result.messages = []
        fb_result.shifts_detected = [shift]
        engine.feedback_loop.process_reflection.return_value = fb_result

        engine.reflect_on_user_input("我要启动新项目")

        consumer.on_insight_generated.assert_called_once()
        consumer.on_cognitive_shift.assert_called_once_with(shift)
        consumer.flush.assert_called_once()

    def test_submit_feedback_is_retired_and_does_not_notify_consumers(self):
        engine = _make_engine()
        consumer = MagicMock()
        engine.register_consumer(consumer)

        with pytest.raises(RuntimeError, match="legacy_reflection_feedback_write_retired"):
            engine.submit_feedback("rid", FeedbackType.ACCURATE, "good")

        consumer.on_feedback_collected.assert_not_called()
        consumer.flush.assert_not_called()

    def test_submit_feedback_does_not_delegate(self):
        engine = _make_engine()
        with pytest.raises(RuntimeError, match="legacy_reflection_feedback_write_retired"):
            engine.submit_feedback("rid", FeedbackType.ACCURATE, "good")
        engine.feedback_collector.submit_feedback.assert_not_called()

    def test_get_pending_feedback_delegates(self):
        engine = _make_engine()
        engine.get_pending_feedback(hours_since=12, limit=5)
        engine.feedback_collector.get_pending_feedback.assert_called_once_with(12, 5)

    def test_get_feedback_history_delegates(self):
        engine = _make_engine()
        engine.get_feedback_history(limit=7, feedback_type=FeedbackType.ACCURATE)
        engine.feedback_collector.get_feedback_history.assert_called_once_with(
            7, FeedbackType.ACCURATE
        )

    def test_get_cognitive_trajectory(self):
        engine = _make_engine()
        shift = MagicMock()
        shift.dimension = "growth"
        shift.to_state = "manager"
        engine.ref_store.get_shifts.return_value = [shift]
        trajectory = engine.get_cognitive_trajectory("growth")
        assert trajectory is not None
        assert trajectory.dimension == "growth"

    def test_get_cognitive_trajectory_empty(self):
        engine = _make_engine()
        engine.ref_store.get_shifts.return_value = []
        assert engine.get_cognitive_trajectory("growth") is None

    def test_submit_session_context_inferred(self):
        engine = _make_engine()
        implicit = MagicMock()
        implicit.inferred_type = FeedbackType.ACCURATE
        implicit.confidence = 0.8
        implicit.signals = ["extended"]
        implicit.inferred_at = datetime.now()
        engine.implicit_detector.detect.return_value = implicit

        record = ReflectionRecord(
            id="rid",
            trigger=ReflectionTrigger.MANUAL,
            trigger_event="t",
            user_query="q",
        )
        engine.ref_store.get_latest.return_value = [record]

        context = MagicMock()
        context.reflection_id = "rid"
        result = engine.submit_session_context(context)
        assert result["inferred"] is True
        assert result["recorded"] is False
        engine.ref_store.save_record.assert_not_called()

    def test_get_stats(self):
        engine = _make_engine()
        engine.ref_store.get_stats.return_value = {"total": 5}
        stats = engine.get_stats()
        assert stats["total"] == 5
        assert "temporal_context" in stats


class TestDeviationInsightGeneration:
    """偏差检测触发模式下 Insight 生成的回归测试。

    覆盖 reflection_engine.py:_generate_insight_from_deviation 中
    `generation_hints` 与偏差提示的合并逻辑，确保不会出现 `str + list` TypeError。
    """

    def test_deviation_generation_with_string_hints(self):
        """generation_hints 为非空字符串时，偏差提示应被追加到末尾。"""
        engine = _make_engine()
        engine.calibrator.get_calibration_params.return_value.generation_hints = (
            "历史反馈：时间维度需谨慎"
        )

        signal = MagicMock()
        signal.suggestion = "预计2周可能过短"
        signal.severity = 0.8
        signal.deviation_type = "numeric"

        session = MagicMock()
        session.id = "sid"
        session.mirror = MagicMock()
        session.mirror.snapshots = []
        session.user_messages = ["预计2周完成"]

        result = engine._generate_insight_from_deviation(signal, session)

        assert result.triggered is True
        assert engine.deviation_detector.close_session.call_count == 1
        call_kwargs = engine.insight_generator.generate.call_args.kwargs
        calibration_hints = call_kwargs["calibration_hints"]
        assert "历史反馈：时间维度需谨慎" in calibration_hints
        assert "额外偏差信号" in calibration_hints
        assert "预计2周可能过短" in calibration_hints

    def test_deviation_generation_with_empty_hints(self):
        """generation_hints 为空字符串时，只保留偏差提示。"""
        engine = _make_engine()
        engine.calibrator.get_calibration_params.return_value.generation_hints = ""

        signal = MagicMock()
        signal.suggestion = "时间估算可能过于乐观"
        signal.severity = "medium"
        signal.deviation_type = "time_estimation"

        session = MagicMock()
        session.id = "sid"
        session.mirror = MagicMock()
        session.mirror.snapshots = []
        session.user_messages = ["预计2周完成"]

        result = engine._generate_insight_from_deviation(signal, session)

        assert result.triggered is True
        call_kwargs = engine.insight_generator.generate.call_args.kwargs
        calibration_hints = call_kwargs["calibration_hints"]
        assert "偏差信号:" in calibration_hints
        assert "时间估算可能过于乐观" in calibration_hints

    def test_deviation_generation_with_list_hints_backward_compat(self):
        """旧代码/测试可能把 generation_hints 传成列表，合并逻辑应兼容。"""
        engine = _make_engine()
        engine.calibrator.get_calibration_params.return_value.generation_hints = ["提示1", "提示2"]

        signal = MagicMock()
        signal.suggestion = "频率自评与实际不符"
        signal.severity = 0.7
        signal.deviation_type = "frequency"

        session = MagicMock()
        session.id = "sid"
        session.mirror = MagicMock()
        session.mirror.snapshots = []
        session.user_messages = ["我很少加班"]

        result = engine._generate_insight_from_deviation(signal, session)

        assert result.triggered is True
        call_kwargs = engine.insight_generator.generate.call_args.kwargs
        calibration_hints = call_kwargs["calibration_hints"]
        assert "提示1" in calibration_hints
        assert "提示2" in calibration_hints
        assert "频率自评与实际不符" in calibration_hints


class TestKnowledgeUpdateExport:
    """P110: Reflection 产生的知识更新建议应写入 Wiki 并触发 wiki_page_updated 事件。"""

    def test_export_reflection_projection_publishes_wiki_page_updated(self, tmp_path):
        engine = _make_engine()
        # 启用 Wiki 导出
        engine.export_to_wiki = True
        engine._reflection_exporter = ReflectionExporter(str(tmp_path))

        record = ReflectionRecord(
            id="rec-p110",
            created_at=datetime.now(),
            trigger=ReflectionTrigger.MAJOR_DECISION,
            trigger_event="角色转变",
            user_query="我要转管理",
        )
        feedback_result = MagicMock()
        feedback_result.knowledge_updates = [
            {
                "dimension": "growth",
                "shift_type": "role_change",
                "suggestion": "更新职业路径笔记",
                "confidence": 0.8,
                "from_state": "开发者",
                "to_state": "经理",
                "detected_at": datetime.now().isoformat(),
            }
        ]
        engine.ref_store.get_by_id.return_value = record
        engine.ref_store.get_all_shifts_for_projection.return_value = []
        engine.ref_store.get_all_for_projection.return_value = [record]

        engine._export_reflection_projection(record, feedback_result)

        pages = list((tmp_path / "L4-Reflections" / "KnowledgeUpdates").rglob("*.md"))
        assert len(pages) == 1
        binding = engine._reflection_exporter.lifecycle.binding_for_path(pages[0])
        assert binding is not None
        assert binding["page_role"] == "formal_derived:reflection_knowledge_update"
        assert binding["status"] == "published"
        assert binding["mutation_id"]
        assert binding["event_trace_id"] == binding["mutation_id"]
