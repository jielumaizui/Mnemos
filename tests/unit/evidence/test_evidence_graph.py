# -*- coding: utf-8 -*-
"""Unit tests for core/evidence/evidence_graph.py"""

import json
from datetime import datetime, timezone

import pytest

from core.evidence.evidence_graph import (
    EvidenceEdge,
    EvidenceGraph,
    EvidenceNode,
    EvidenceNodeType,
    EvidenceRelationType,
)
from core.cognitive.sources import SourceItem
from core.reflection.models import (
    InsightSnapshot,
    MirrorSnapshot,
    ReflectionRecord,
    ReflectionTrigger,
)


@pytest.fixture
def graph(tmp_path):
    """提供一个基于临时目录的 EvidenceGraph 实例。"""
    return EvidenceGraph(db_path=str(tmp_path / "evidence_graph.db"))


class TestEvidenceGraphCrud:
    """EvidenceGraph 基础 CRUD  roundtrip 测试"""

    def test_ensure_node_and_get_node_roundtrip(self, graph):
        """ensure_node 写入的节点应能被 get_node 读出。"""
        node = EvidenceNode(
            id="node-1",
            node_type=EvidenceNodeType.OBSERVATION,
            title="Test Node",
            source_path="/tmp/test.md",
            content="hello world",
            metadata={"foo": "bar"},
        )
        assert graph.ensure_node(node) is True

        loaded = graph.get_node("node-1")
        assert loaded is not None
        assert loaded.id == "node-1"
        assert loaded.node_type == EvidenceNodeType.OBSERVATION
        assert loaded.title == "Test Node"
        assert loaded.source_path == "/tmp/test.md"
        assert loaded.content == "hello world"
        assert loaded.metadata == {"foo": "bar"}

    def test_cognitive_shift_node_type_is_serialized_contract(self, graph):
        """Cognitive shift nodes should keep their public JSON/DB contract."""
        node = EvidenceNode(
            id="shift-1",
            node_type=EvidenceNodeType.COGNITIVE_SHIFT,
            title="Strategy changed",
            content="The user moved from reactive debugging to planned remediation.",
            metadata={"before": "reactive", "after": "planned"},
        )

        payload = json.loads(json.dumps(node.to_dict()))
        assert payload["node_type"] == "cognitive_shift"

        assert graph.ensure_node(node) is True
        loaded = graph.get_node("shift-1")
        assert loaded is not None
        assert loaded.node_type == EvidenceNodeType.COGNITIVE_SHIFT
        assert loaded.to_dict()["node_type"] == "cognitive_shift"

    def test_add_edge_and_get_edges_roundtrip(self, graph):
        """add_edge 写入的边应能被 get_edges 查询。"""
        graph.ensure_node(
            EvidenceNode(id="src", node_type=EvidenceNodeType.INSIGHT, title="Source")
        )
        graph.ensure_node(
            EvidenceNode(id="tgt", node_type=EvidenceNodeType.OBSERVATION, title="Target")
        )

        edge = EvidenceEdge(
            source_id="src",
            target_id="tgt",
            relation_type=EvidenceRelationType.DERIVED_FROM,
            confidence=0.9,
            evidence=["e1", "e2"],
        )
        assert graph.add_edge(edge) is True

        edges = graph.get_edges(source_id="src")
        assert len(edges) == 1
        loaded = edges[0]
        assert loaded.source_id == "src"
        assert loaded.target_id == "tgt"
        assert loaded.relation_type == EvidenceRelationType.DERIVED_FROM
        assert loaded.confidence == pytest.approx(0.9)
        assert loaded.evidence == ["e1", "e2"]

    def test_sensitive_literals_are_narrowly_redacted_at_persistence_boundary(self, graph):
        private_value = "do-not-" + "store"
        provider_value = "sk-" + "1234567890abcdefghijkl"
        graph.ensure_node(
            EvidenceNode(
                id="private-node",
                node_type=EvidenceNodeType.OBSERVATION,
                content=("email=person@example.com " + "pass" + "word=" + private_value),
            )
        )
        graph.add_edge(
            EvidenceEdge(
                source_id="private-node",
                target_id="target",
                relation_type=EvidenceRelationType.DERIVED_FROM,
                evidence=["api_" + "key=" + provider_value],
            )
        )

        node = graph.get_node("private-node")
        edge = graph.get_edges(source_id="private-node")[0]
        assert node is not None
        assert "person@example.com" not in node.content
        assert private_value not in node.content
        assert provider_value not in edge.evidence[0]
        assert "[REDACTED:" in node.content
        assert "[REDACTED:CREDENTIAL]" in edge.evidence[0]

    def test_feedback_on_relation_type_is_serialized_contract(self, graph):
        """Feedback edges should keep their public JSON/DB contract."""
        feedback_node = EvidenceNode(
            id="feedback-1",
            node_type=EvidenceNodeType.USER_FEEDBACK,
            title="User feedback",
        )
        node_payload = json.loads(json.dumps(feedback_node.to_dict()))
        assert node_payload["node_type"] == "user_feedback"

        graph.ensure_node(feedback_node)
        graph.ensure_node(EvidenceNode(id="insight-1", node_type=EvidenceNodeType.INSIGHT))

        edge = EvidenceEdge(
            source_id="feedback-1",
            target_id="insight-1",
            relation_type=EvidenceRelationType.FEEDBACK_ON,
            evidence=["user corrected the insight"],
        )

        payload = json.loads(json.dumps(edge.to_dict()))
        assert payload["relation_type"] == "feedback_on"

        assert graph.add_edge(edge) is True
        loaded = graph.get_edges(
            source_id="feedback-1",
            relation_type=EvidenceRelationType.FEEDBACK_ON,
        )
        assert len(loaded) == 1
        assert loaded[0].relation_type == EvidenceRelationType.FEEDBACK_ON
        assert loaded[0].to_dict()["relation_type"] == "feedback_on"
        assert graph.get_node("feedback-1").node_type == EvidenceNodeType.USER_FEEDBACK

    def test_get_edges_filters_by_relation_type(self, graph):
        """get_edges 应支持按 relation_type 过滤。"""
        graph.ensure_node(EvidenceNode(id="a", node_type=EvidenceNodeType.INSIGHT))
        graph.ensure_node(EvidenceNode(id="b", node_type=EvidenceNodeType.OBSERVATION))
        graph.ensure_node(EvidenceNode(id="c", node_type=EvidenceNodeType.KNOWLEDGE))

        graph.add_edge(
            EvidenceEdge(
                source_id="a",
                target_id="b",
                relation_type=EvidenceRelationType.DERIVED_FROM,
            )
        )
        graph.add_edge(
            EvidenceEdge(
                source_id="a",
                target_id="c",
                relation_type=EvidenceRelationType.SUPPORTS,
            )
        )

        derived = graph.get_edges(source_id="a", relation_type=EvidenceRelationType.DERIVED_FROM)
        assert len(derived) == 1
        assert derived[0].target_id == "b"

        all_edges = graph.get_edges(source_id="a")
        assert len(all_edges) == 2


class TestAddReflectionRecord:
    """add_reflection_record 测试"""

    @pytest.fixture
    def record(self):
        """构造一个带 Insight 的 ReflectionRecord。"""
        return ReflectionRecord(
            id="refl-001",
            trigger=ReflectionTrigger.MANUAL,
            trigger_event="user asked for reflection",
            user_query="what do you think?",
            mirror_dimensions=["focus", "energy"],
            mirror_snapshots=[
                MirrorSnapshot(
                    observation_id="obs-1",
                    dimension="focus",
                    value_summary="high focus",
                    evidence_summary="evidence for focus",
                    confidence=0.8,
                    recency_weight=0.9,
                ),
                MirrorSnapshot(
                    observation_id="obs-2",
                    dimension="energy",
                    value_summary="low energy",
                    evidence_summary="evidence for energy",
                    confidence=0.6,
                    recency_weight=0.7,
                ),
            ],
            insight=InsightSnapshot(
                summary="insight summary",
                key_points=["p1", "p2"],
                dimensions_involved=["focus"],
            ),
        )

    def test_creates_expected_nodes_and_edges(self, graph, record):
        """add_reflection_record 应创建 Reflection / Mirror / Insight / Observation 节点及边。"""
        result = graph.add_reflection_record(record)

        assert result["reflection_id"] == "refl-001"
        assert result["mirror_id"] == "refl-001:mirror"
        assert result["insight_id"] == "refl-001:insight"
        assert set(result["observation_ids"]) == {"obs-1", "obs-2"}

        # Reflection 节点
        reflection = graph.get_node("refl-001")
        assert reflection is not None
        assert reflection.node_type == EvidenceNodeType.REFLECTION
        assert reflection.content == "user asked for reflection"
        assert reflection.metadata["trigger"] == ReflectionTrigger.MANUAL.value

        # Mirror 节点
        mirror = graph.get_node("refl-001:mirror")
        assert mirror is not None
        assert mirror.node_type == EvidenceNodeType.MIRROR

        # Insight 节点
        insight = graph.get_node("refl-001:insight")
        assert insight is not None
        assert insight.node_type == EvidenceNodeType.INSIGHT
        assert insight.content == "insight summary"

        # Observation 节点
        obs1 = graph.get_node("obs-1")
        assert obs1 is not None
        assert obs1.node_type == EvidenceNodeType.OBSERVATION
        assert obs1.metadata["dimension"] == "focus"

        # Reflection -> Mirror
        edges = graph.get_edges(source_id="refl-001", target_id="refl-001:mirror")
        assert len(edges) == 1
        assert edges[0].relation_type == EvidenceRelationType.USED_IN

        # Mirror -> Observation
        edges = graph.get_edges(source_id="refl-001:mirror", target_id="obs-1")
        assert len(edges) == 1
        assert edges[0].relation_type == EvidenceRelationType.USED_IN

        # Canonical lineage direction: generated object -> generating evidence.
        edges = graph.get_edges(source_id="refl-001:insight", target_id="refl-001")
        assert len(edges) == 1
        assert edges[0].relation_type == EvidenceRelationType.GENERATED_FROM

        # Insight -> Mirror
        edges = graph.get_edges(source_id="refl-001:insight", target_id="refl-001:mirror")
        assert len(edges) == 1
        assert edges[0].relation_type == EvidenceRelationType.DERIVED_FROM

        # Insight -> Observation
        edges = graph.get_edges(source_id="refl-001:insight", target_id="obs-2")
        assert len(edges) == 1
        assert edges[0].relation_type == EvidenceRelationType.DERIVED_FROM

    def test_no_insight_skips_insight_nodes(self, graph):
        """当 ReflectionRecord 没有 insight 时，不应创建 Insight 节点。"""
        record = ReflectionRecord(
            id="refl-no-insight",
            mirror_snapshots=[
                MirrorSnapshot(
                    observation_id="obs-1",
                    dimension="d1",
                    value_summary="v1",
                    evidence_summary="e1",
                    confidence=1.0,
                    recency_weight=1.0,
                )
            ],
        )
        result = graph.add_reflection_record(record)
        assert result["insight_id"] is None
        assert graph.get_node("refl-no-insight:insight") is None

        edges = graph.get_edges(
            source_id="refl-no-insight",
            target_id="refl-no-insight:mirror",
        )
        assert len(edges) == 1
        assert edges[0].relation_type == EvidenceRelationType.USED_IN

    def test_invalid_record_raises_type_error(self, graph):
        """非 ReflectionRecord 应抛出 TypeError。"""
        with pytest.raises(TypeError, match="ReflectionRecord"):
            graph.add_reflection_record({})


class TestSemanticEvidenceApis:
    """EvidenceGraph 高层语义接口测试"""

    def test_add_mirror_observations_creates_mirror_edges(self, graph):
        """add_mirror_observations 应创建 Mirror 节点和 Mirror -> Observation 边。"""
        graph.ensure_node(
            EvidenceNode(
                id="obs-1",
                node_type=EvidenceNodeType.OBSERVATION,
                title="Observation 1",
            )
        )

        edges = graph.add_mirror_observations(
            "mirror-1",
            ["obs-1"],
            mirror_summary="mirror summary",
            mirror_metadata={"dimensions": ["focus"]},
            evidence_by_observation={"obs-1": "focus evidence"},
        )

        assert len(edges) == 1
        assert edges[0].source_id == "mirror-1"
        assert edges[0].target_id == "obs-1"
        assert edges[0].relation_type == EvidenceRelationType.USED_IN
        assert edges[0].evidence == ["focus evidence"]

        mirror = graph.get_node("mirror-1")
        assert mirror is not None
        assert mirror.node_type == EvidenceNodeType.MIRROR
        assert mirror.content == "mirror summary"
        assert mirror.metadata == {"dimensions": ["focus"]}

    def test_add_observation_sources_orders_mixed_timestamp_forms(self, graph):
        """Legacy-naive and canonical-aware SourceItems may share one page."""
        items = []
        for index in range(21):
            item = SourceItem(
                source_type="wiki",
                file_path=f"/wiki/{index}.md",
                content="source",
            )
            item.timestamp = (
                datetime(2026, 7, 14, 12, index % 60)
                if index % 2
                else datetime(2026, 7, 14, 12, index % 60, tzinfo=timezone.utc)
            )
            items.append(item)

        edges = graph.add_observation_sources("obs-mixed-time", items)

        assert len(edges) == 21

    def test_add_insight_derivation_records_full_lineage(self, graph):
        """add_insight_derivation 应写入 Insight 的 Mirror/Observation/Wiki/Reflection 血缘。"""
        graph.ensure_node(EvidenceNode(id="mirror-1", node_type=EvidenceNodeType.MIRROR))
        graph.ensure_node(EvidenceNode(id="obs-1", node_type=EvidenceNodeType.OBSERVATION))

        result = graph.add_insight_derivation(
            "ins-1",
            "mirror-1",
            ["obs-1"],
            knowledge_refs=["03-Tech/redis.md"],
            reflection_id="refl-1",
            insight_summary="insight summary",
            insight_metadata={"dimensions": ["focus"]},
        )

        assert len(result["insight_to_mirror"]) == 1
        assert len(result["insight_to_observations"]) == 1
        assert len(result["mirror_to_observations"]) == 1
        assert len(result["insight_to_knowledge"]) == 1
        assert len(result["reflection_to_insight"]) == 2

        insight = graph.get_node("ins-1")
        assert insight is not None
        assert insight.content == "insight summary"
        assert insight.metadata == {"dimensions": ["focus"]}

        knowledge = graph.get_node("knowledge:03-Tech/redis.md")
        assert knowledge is not None
        assert knowledge.node_type == EvidenceNodeType.KNOWLEDGE

        edge_types = {edge.relation_type for edge in graph.get_edges(source_id="ins-1")}
        assert edge_types == {
            EvidenceRelationType.DERIVED_FROM,
            EvidenceRelationType.GENERATED_FROM,
            EvidenceRelationType.SUPPORTS,
        }

    def test_get_evidence_chain_for_insight_returns_direct_evidence(self, graph):
        """get_evidence_chain_for_insight 应返回 Insight 指出的直接证据边。"""
        graph.ensure_node(EvidenceNode(id="mirror-1", node_type=EvidenceNodeType.MIRROR))
        graph.ensure_node(EvidenceNode(id="obs-1", node_type=EvidenceNodeType.OBSERVATION))

        graph.add_insight_derivation(
            "ins-1",
            "mirror-1",
            ["obs-1"],
            knowledge_refs=["03-Tech/redis.md"],
            insight_summary="insight summary",
        )

        chain = graph.get_evidence_chain_for_insight("ins-1")
        chain_edges = {(source.id, relation, target.id) for source, relation, target in chain}

        assert chain_edges == {
            ("ins-1", EvidenceRelationType.DERIVED_FROM, "mirror-1"),
            ("ins-1", EvidenceRelationType.DERIVED_FROM, "obs-1"),
            ("ins-1", EvidenceRelationType.SUPPORTS, "knowledge:03-Tech/redis.md"),
        }


class TestLineageAndExplain:
    """explain_why 与 get_lineage 遍历测试"""

    @pytest.fixture
    def chain_graph(self, graph):
        """构建一条 derived -> evidence 的规范血缘链。"""
        memory = EvidenceNode(
            id="mem-1",
            node_type=EvidenceNodeType.MEMORY,
            title="Memory 1",
            source_path="/raw/note.md",
        )
        observation = EvidenceNode(
            id="obs-1",
            node_type=EvidenceNodeType.OBSERVATION,
            title="Observation 1",
        )
        insight = EvidenceNode(
            id="ins-1",
            node_type=EvidenceNodeType.INSIGHT,
            title="Insight 1",
        )
        graph.ensure_node(memory)
        graph.ensure_node(observation)
        graph.ensure_node(insight)

        # insight -> observation -> memory，upstream 始终沿出边找证据。
        graph.add_edge(
            EvidenceEdge(
                source_id="obs-1",
                target_id="mem-1",
                relation_type=EvidenceRelationType.OBSERVED_IN,
            )
        )
        graph.add_edge(
            EvidenceEdge(
                source_id="ins-1",
                target_id="obs-1",
                relation_type=EvidenceRelationType.DERIVED_FROM,
            )
        )
        return graph

    def test_get_lineage_upstream(self, chain_graph):
        """从 insight 向上游遍历应包含 observation 与 memory。"""
        lineage = chain_graph.get_lineage("ins-1", direction="upstream", depth=5)

        assert set(lineage["nodes"].keys()) == {"ins-1", "obs-1", "mem-1"}
        assert len(lineage["edges"]) == 2

    def test_get_lineage_downstream(self, chain_graph):
        """从 memory 向下游遍历应包含 observation 与 insight。"""
        lineage = chain_graph.get_lineage("mem-1", direction="downstream", depth=5)

        assert set(lineage["nodes"].keys()) == {"mem-1", "obs-1", "ins-1"}
        assert len(lineage["edges"]) == 2

    def test_get_lineage_both(self, chain_graph):
        """双向遍历应包含全部节点。"""
        lineage = chain_graph.get_lineage("obs-1", direction="both", depth=5)

        assert set(lineage["nodes"].keys()) == {"obs-1", "mem-1", "ins-1"}
        assert len(lineage["edges"]) == 2

    def test_get_lineage_unknown_node(self, graph):
        """未知节点应返回空结果。"""
        lineage = graph.get_lineage("missing")
        assert lineage == {"nodes": {}, "edges": []}

    def test_explain_why(self, chain_graph):
        """explain_why 应返回 insight 的上游 observation 与 memory。"""
        explanation = chain_graph.explain_why("ins-1")

        assert explanation["insight_id"] == "ins-1"
        assert {item["target_id"] for item in explanation["direct_evidence_chain"]} == {"obs-1"}
        assert len(explanation["observations"]) == 1
        assert explanation["observations"][0]["id"] == "obs-1"
        assert len(explanation["memories"]) == 1
        assert explanation["memories"][0]["id"] == "mem-1"
        assert len(explanation["knowledges"]) == 0
        assert explanation["node_count"] == 3
        assert explanation["edge_count"] == 2

    def test_explain_why_includes_direct_evidence_chain(self, graph):
        """Reflection 写入的 Insight 下游证据应进入 explain_why。"""
        record = ReflectionRecord(
            id="refl-001",
            trigger=ReflectionTrigger.MANUAL,
            trigger_event="user asked for reflection",
            mirror_dimensions=["focus", "energy"],
            mirror_snapshots=[
                MirrorSnapshot(
                    observation_id="obs-1",
                    dimension="focus",
                    value_summary="high focus",
                    evidence_summary="evidence for focus",
                    confidence=0.8,
                    recency_weight=0.9,
                ),
                MirrorSnapshot(
                    observation_id="obs-2",
                    dimension="energy",
                    value_summary="low energy",
                    evidence_summary="evidence for energy",
                    confidence=0.6,
                    recency_weight=0.7,
                ),
            ],
            insight=InsightSnapshot(
                summary="insight summary",
                key_points=["p1", "p2"],
                dimensions_involved=["focus"],
            ),
        )
        graph.add_reflection_record(record)

        explanation = graph.explain_why("refl-001:insight")
        direct_targets = {item["target_id"]: item for item in explanation["direct_evidence_chain"]}

        assert set(direct_targets) == {
            "refl-001",
            "refl-001:mirror",
            "obs-1",
            "obs-2",
        }
        assert direct_targets["obs-1"]["relation"] == EvidenceRelationType.DERIVED_FROM.value
        assert direct_targets["obs-1"]["target_type"] == EvidenceNodeType.OBSERVATION.value
        assert {item["id"] for item in explanation["observations"]} == {"obs-1", "obs-2"}
        assert explanation["node_count"] == 5
        assert explanation["edge_count"] == 7

    def test_explain_why_unknown(self, graph):
        """explain_why 对未知 insight 返回空证据列表。"""
        explanation = graph.explain_why("missing")
        assert explanation["direct_evidence_chain"] == []
        assert explanation["observations"] == []
        assert explanation["memories"] == []
        assert explanation["knowledges"] == []
        assert explanation["node_count"] == 0
        assert explanation["edge_count"] == 0
