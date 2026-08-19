# -*- coding: utf-8 -*-
"""Unit tests for core.reflection.reflection_capability."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock


from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.reflection.insight_generator import InsightGenerator
from core.reflection.models import InsightSnapshot, ReflectionRecord, ReflectionTrigger
from core.reflection.reflection_capability import ReflectionCapability
from core.reflection.time_awareness import TemporalContext


class TestReflectionCapability:
    def test_reflect_builds_record_and_insight(self):
        mirror = MagicMock()
        mirror.dimensions_involved = ["decisions"]
        mirror.snapshots = []

        insight_result = MagicMock()
        insight_result.to_snapshot.return_value = InsightSnapshot(
            summary="s", key_points=["k"], dimensions_involved=["decisions"]
        )

        mirror_engine = MagicMock()
        mirror_engine.build_mirror.return_value = mirror

        insight_generator = MagicMock()
        insight_generator.generate.return_value = insight_result

        time_awareness = MagicMock()
        time_awareness.get_temporal_context.return_value = TemporalContext(
            now=datetime.now(),
            now_str="2024-01-01 10:00",
            rhythm="morning",
            rhythm_description="上午",
            last_reflection_ago=3600,
        )

        cap = ReflectionCapability(
            mirror_engine=mirror_engine,
            insight_generator=insight_generator,
            time_awareness=time_awareness,
            use_experience_matcher=False,
        )

        result = cap.reflect(scene="new_project", query="我要重构项目")
        assert result.scene == "new_project"
        assert result.mirror is mirror
        assert result.insight_result is insight_result
        assert result.record.trigger == ReflectionTrigger.MANUAL
        assert result.record.user_query == "我要重构项目"

    def test_store_fills_insight_and_saves(self):
        record = ReflectionRecord(
            trigger=ReflectionTrigger.MANUAL,
            trigger_event="t",
            user_query="q",
        )
        mirror = MagicMock()
        mirror.dimensions_involved = ["decisions"]
        result = MagicMock()
        result.record = record
        result.mirror = mirror
        result.scene = "new_project"

        ref_store = MagicMock()
        cap = ReflectionCapability(reflection_store=ref_store, use_experience_matcher=False)

        stored = cap.store(
            result,
            insight_summary="summary",
            insight_key_points=["point1"],
            evidence_graph=None,
        )
        assert stored.insight.summary == "summary"
        assert stored.insight.key_points == ["point1"]
        ref_store.save_record.assert_called_once()

    def test_store_links_to_evidence_graph(self):
        record = ReflectionRecord(
            trigger=ReflectionTrigger.MANUAL,
            trigger_event="t",
            user_query="q",
        )
        result = MagicMock()
        result.record = record
        result.mirror = MagicMock(dimensions_involved=[])
        result.scene = "x"

        evidence_graph = MagicMock()
        cap = ReflectionCapability(use_experience_matcher=False)
        cap.store(result, "s", [], evidence_graph=evidence_graph)
        evidence_graph.add_reflection_record.assert_called_once()

    def test_get_record(self):
        record = ReflectionRecord(
            id="abc123",
            trigger=ReflectionTrigger.MANUAL,
            trigger_event="t",
            user_query="q",
        )
        ref_store = MagicMock()
        ref_store.authorized_get_by_id.return_value = (record, {"authorized": 1})
        cap = ReflectionCapability(reflection_store=ref_store, use_experience_matcher=False)
        principal = PrincipalEnvelope(
            principal_id="mcp:codex:reflection-capability",
            agent="codex",
            host_kind="test",
            capability_id="reflection-capability",
            capabilities=frozenset({"memory_read"}),
            allowed_projects=frozenset({"mnemos"}),
        )
        narrowing = AccessNarrowing(session_id="session-1", project="mnemos")
        assert cap.get_record("abc123", principal=principal, narrowing=narrowing) is record
        ref_store.authorized_get_by_id.return_value = (None, {"not_found": 1})
        assert cap.get_record("missing", principal=principal, narrowing=narrowing) is None

    def test_serialize_temporal_converts_datetime(self):
        temporal = TemporalContext(
            now=datetime(2024, 1, 1, 12, 0, 0),
            now_str="2024-01-01 12:00",
            rhythm="evening",
            rhythm_description="晚上",
            last_reflection_ago=100,
        )

        serialized = ReflectionCapability._serialize_temporal(temporal)
        assert serialized["rhythm"] == "evening"
        assert serialized["now"] == "2024-01-01T12:00:00"

    def test_use_llm_false_disables_llm_call(self):
        """use_llm=False 时 InsightGenerator 不应调用 LLM。"""
        mirror = MagicMock()
        mirror.dimensions_involved = ["decisions"]
        mirror.snapshots = []
        mirror.to_prompt_context.return_value = "## Mirror（证据链）\n- 证据1"

        mirror_engine = MagicMock()
        mirror_engine.build_mirror.return_value = mirror

        real_generator = InsightGenerator(use_llm=False)
        # 如果 _call_llm 被调用，说明没有遵守 use_llm=False
        real_generator._call_llm = MagicMock(return_value=None)

        time_awareness = MagicMock()
        time_awareness.get_temporal_context.return_value = TemporalContext(
            now=datetime.now(),
            now_str="2024-01-01 10:00",
            rhythm="morning",
            rhythm_description="上午",
        )

        cap = ReflectionCapability(
            mirror_engine=mirror_engine,
            insight_generator=real_generator,
            time_awareness=time_awareness,
            use_experience_matcher=False,
        )

        result = cap.reflect(scene="new_project", query="我要重构项目")
        assert result.insight_result.llm_called is False
        assert result.insight_result.llm_error == ""
        real_generator._call_llm.assert_not_called()
