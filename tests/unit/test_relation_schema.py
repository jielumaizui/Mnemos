"""
Tests for core.kia.relation_schema

Covers: RelationType, Relation, RelationEvidence, convenience functions,
        suggest_relation_type, serialization round-trip.
"""

import pytest

from core.kia.relation_schema import (
    RelationType,
    CORE_RELATION_TYPES,
    RELATION_META,
    Relation,
    RelationEvidence,
    get_relation_description,
    get_relation_example,
    infer_symmetric_type,
    get_all_relation_types,
    suggest_relation_type,
)


class TestRelationType:
    def test_core_types_present(self):
        """蓝图要求的 9 种核心类型必须存在"""
        expected = {
            "contains",
            "related_to",
            "contradicts",
            "supercedes",
            "derives_from",
            "prerequisite",
            "co_occurs",
            "sequential",
            "similar_to",
        }
        actual = {rt.value for rt in CORE_RELATION_TYPES}
        assert actual == expected

    def test_relation_type_is_str_enum(self):
        """RelationType 是 str Enum，可直接比较字符串"""
        assert RelationType.CONTAINS == "contains"
        assert isinstance(RelationType.RELATED_TO, str)

    def test_reverse_mapping_exists_for_core(self):
        """每种核心类型都有反向关系定义"""
        for rt in CORE_RELATION_TYPES:
            meta = RELATION_META.get(rt, {})
            assert "reverse" in meta
            assert meta["reverse"]


class TestRelation:
    def test_default_init(self):
        r = Relation(source="A", target="B", relation_type=RelationType.CONTAINS)
        assert r.source == "A"
        assert r.target == "B"
        assert r.strength == 0.5
        assert r.confidence == 0.5
        assert r.evidence == []
        assert r.confidence_history == []
        assert r.status == "active"
        assert r.created_at
        assert r.updated_at

    def test_strength_recalc_on_explicit_base_dynamic(self):
        """显式设置 base_strength / dynamic_strength 时 strength 应重新计算"""
        r = Relation(
            source="A",
            target="B",
            relation_type=RelationType.RELATED_TO,
            base_strength=0.8,
            dynamic_strength=0.5,
        )
        assert r.strength == round(0.8 * 0.5, 3)  # 0.4

    def test_is_symmetric(self):
        assert (
            Relation(source="A", target="B", relation_type=RelationType.RELATED_TO).is_symmetric
            is True
        )
        assert (
            Relation(source="A", target="B", relation_type=RelationType.CONTAINS).is_symmetric
            is False
        )

    def test_reverse_type(self):
        assert (
            Relation(source="A", target="B", relation_type=RelationType.CONTAINS).reverse_type
            == "is_contained_by"
        )

    def test_is_transitive(self):
        assert (
            Relation(source="A", target="B", relation_type=RelationType.CONTAINS).is_transitive
            is True
        )
        assert (
            Relation(source="A", target="B", relation_type=RelationType.RELATED_TO).is_transitive
            is False
        )

    def test_update_confidence_ewma(self):
        r = Relation(source="A", target="B", relation_type=RelationType.CONTAINS)
        r.update_confidence(0.9)
        assert len(r.confidence_history) == 1
        assert r.confidence_history[0] == 0.5
        # EWMA: 0.3 * 0.9 + 0.7 * 0.5 = 0.27 + 0.35 = 0.62
        assert r.confidence == pytest.approx(0.62, abs=0.01)

    def test_to_dict_round_trip(self):
        r = Relation(
            source="A",
            target="B",
            relation_type=RelationType.DERIVES_FROM,
            base_strength=0.7,
            dynamic_strength=0.6,
            confidence=0.8,
            evidence=[RelationEvidence(evidence_type="quote", content="test")],
        )
        d = r.to_dict()
        assert d["source"] == "A"
        assert d["relation_type"] == "derives_from"
        assert d["evidence"][0]["type"] == "quote"

    def test_contrasts_with_relation_type_round_trip(self):
        relation = Relation(
            source="expected-outcome",
            target="actual-outcome",
            relation_type=RelationType.CONTRASTS_WITH,
        )

        payload = relation.to_dict()
        assert payload["relation_type"] == "contrasts_with"
        restored = Relation.from_dict(payload)
        assert restored.relation_type == RelationType.CONTRASTS_WITH

    def test_extends_relation_type_round_trip(self):
        relation = Relation(
            source="plugin-api-v2",
            target="plugin-api-v1",
            relation_type=RelationType.EXTENDS,
        )

        payload = relation.to_dict()
        assert payload["relation_type"] == "extends"
        restored = Relation.from_dict(payload)
        assert restored.relation_type == RelationType.EXTENDS

    def test_implements_relation_type_round_trip(self):
        relation = Relation(
            source="storage-backend",
            target="storage-interface",
            relation_type=RelationType.IMPLEMENTS,
        )

        payload = relation.to_dict()
        assert payload["relation_type"] == "implements"
        restored = Relation.from_dict(payload)
        assert restored.relation_type == RelationType.IMPLEMENTS

    def test_from_dict_backward_compat(self):
        """旧数据只有 strength 字段时，应映射为 base_strength"""
        old = {
            "source": "A",
            "target": "B",
            "relation_type": "contains",
            "strength": 0.8,
        }
        r = Relation.from_dict(old)
        assert r.base_strength == 0.8
        assert r.dynamic_strength == 1.0

    def test_from_dict_with_base_dynamic(self):
        data = {
            "source": "A",
            "target": "B",
            "relation_type": "contains",
            "base_strength": 0.7,
            "dynamic_strength": 0.6,
            "strength": 0.42,
        }
        r = Relation.from_dict(data)
        assert r.base_strength == 0.7
        assert r.dynamic_strength == 0.6


class TestConvenienceFunctions:
    def test_get_relation_description(self):
        desc = get_relation_description(RelationType.CONTAINS)
        assert "包含" in desc

    def test_get_relation_example(self):
        ex = get_relation_example(RelationType.CONTRADICTS)
        assert ex  # 非空

    def test_infer_symmetric_type(self):
        assert infer_symmetric_type(RelationType.RELATED_TO) is True
        assert infer_symmetric_type(RelationType.CONTAINS) is False

    def test_get_all_relation_types(self):
        all_types = get_all_relation_types()
        assert len(all_types) == len(RelationType)
        for value, desc, ex in all_types:
            assert value
        # 核心类型必须有描述
        core_values = {rt.value for rt in CORE_RELATION_TYPES}
        for value, desc, ex in all_types:
            if value in core_values:
                assert desc, f"核心类型 {value} 缺少描述"


class TestSuggestRelationType:
    def test_keyword_match_contains(self):
        results = suggest_relation_type(["包含", "涵盖"])
        assert any(rt == RelationType.CONTAINS for rt, score in results)

    def test_keyword_match_contradicts(self):
        results = suggest_relation_type(["矛盾", "冲突"])
        assert any(rt == RelationType.CONTRADICTS for rt, score in results)

    def test_no_match_returns_empty(self):
        results = suggest_relation_type(["xyz_unknown"])
        assert results == []

    def test_scores_sorted_descending(self):
        results = suggest_relation_type(["包含", "相关", "矛盾"])
        scores = [s for _, s in results]
        assert scores == sorted(scores, reverse=True)

    def test_returns_non_empty_list_for_known_keywords(self):
        """已知关键词必须返回非空列表，确保函数不会隐式返回 None"""
        results = suggest_relation_type(["包含", "扩展"])
        assert isinstance(results, list)
        assert len(results) > 0
        for rel_type, score in results:
            assert isinstance(rel_type, RelationType)
            assert 0 <= score <= 1
