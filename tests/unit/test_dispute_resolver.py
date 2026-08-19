# -*- coding: utf-8 -*-
"""Unit tests for core.app.dispute_resolver."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from core.app.dispute_resolver import DisputeAssertion, DisputePage, DisputeResolver
from core.trust.proposal_queue import ProposalQueue
from tests.knowledge_graph_decision_fixtures import authorized_knowledge_graph


@pytest.fixture
def resolver(tmp_path):
    return DisputeResolver(wiki_base=str(tmp_path))


class TestDisputePage:
    def test_severity_extreme(self):
        page = DisputePage(
            topic="x",
            new_assertion=DisputeAssertion("a", "t", "c", 1),
            existing_assertions=[],
            conflict_strength=0.95,
            is_core_knowledge=True,
        )
        assert page.severity == "extreme"

    def test_severity_high(self):
        page = DisputePage(
            topic="x",
            new_assertion=DisputeAssertion("a", "t", "c", 1),
            existing_assertions=[],
            conflict_strength=0.8,
            is_core_knowledge=True,
        )
        assert page.severity == "high"

    def test_severity_medium(self):
        page = DisputePage(
            topic="x",
            new_assertion=DisputeAssertion("a", "t", "c", 1),
            existing_assertions=[],
            conflict_strength=0.5,
            is_core_knowledge=True,
        )
        assert page.severity == "medium"


def test_create_dispute_page_enforce_submits_proposal_without_writing_file(
    tmp_path, monkeypatch
):
    db = tmp_path / ".mnemos" / "trusted.db"
    fake_config = SimpleNamespace(
        wiki_dir=tmp_path,
        database_dir=tmp_path / ".mnemos",
        get=lambda key, default=None: {
            "trusted_push.mode": "enforce",
            "trusted_push.db_path": str(db),
        }.get(key, default),
    )
    monkeypatch.setattr("core.trust.config.get_config", lambda: fake_config)
    resolver = DisputeResolver(wiki_base=str(tmp_path))

    page = resolver.create_dispute_page(
        new_assertion=DisputeAssertion(
            page_path="new.md",
            title="Redis conflict",
            content="new",
            reference_count=1,
            relation_evidence=["raw-1"],
        ),
        conflicts=[
            DisputeAssertion(
                page_path="old.md",
                title="Redis old",
                content="old",
                reference_count=1,
            )
        ],
        conflict_strength=0.8,
    )

    assert page.page_path.startswith("08-Disputes/")
    assert not list(tmp_path.rglob("*.md"))
    proposals = ProposalQueue(db, wiki_base=tmp_path).list()
    assert proposals[0].candidate.source == "dispute_resolver"


class TestDisputeResolver:
    def test_create_dispute_page_writes_markdown(self, resolver, tmp_path):
        new_assertion = DisputeAssertion(
            page_path="note.md", title="New Assertion", content="New content", reference_count=3
        )
        existing = [
            DisputeAssertion(
                page_path="old.md", title="Old Assertion", content="Old content", reference_count=5
            )
        ]
        page = resolver.create_dispute_page(
            new_assertion, existing, conflict_strength=0.85, is_core_knowledge=True
        )

        assert page.topic == "New Assertion"
        assert page.severity == "high"
        assert page.page_path.startswith("08-Disputes/")
        full_path = tmp_path / page.page_path
        assert full_path.exists()
        content = full_path.read_text(encoding="utf-8")
        assert "# 争议仲裁：New Assertion" in content
        assert "- [ ] **采纳新断言**" in content
        assert "受影响页面总数：8" in content
        state_db = (
            resolver.wiki_base / ".mnemos" / "producer_consumer_ledger.db"
        )
        if not state_db.exists():
            from core.trust.vault_mutation_service import TrustedVaultMutationService

            state_db = (
                TrustedVaultMutationService(
                    wiki_base=resolver.wiki_base
                ).config.db_path.parent
                / "producer_consumer_ledger.db"
            )
        with sqlite3.connect(state_db) as conn:
            assert conn.execute(
                "SELECT status FROM cognitive_state_effect_receipts"
            ).fetchone() == ("committed",)

    def test_get_unresolved_disputes(self, resolver, tmp_path):
        new_assertion = DisputeAssertion(
            page_path="n.md", title="Unresolved", content="c", reference_count=1
        )
        resolver.create_dispute_page(new_assertion, [], 0.5)

        disputes = resolver.get_unresolved_disputes()
        assert len(disputes) == 1
        assert disputes[0]["needs_escalation"] is False

    def test_resolve_dispute_updates_page(self, resolver, tmp_path):
        new_assertion = DisputeAssertion(
            page_path="n.md", title="Resolve Me", content="c", reference_count=1
        )
        page = resolver.create_dispute_page(new_assertion, [], 0.5)
        resolver.resolve_dispute(page.page_path, "adopt_new", context="some context")

        full_path = tmp_path / page.page_path
        content = full_path.read_text(encoding="utf-8")
        assert "- [x] **采纳新断言**" in content
        assert "**解决方案**: 采纳新断言" in content
        assert "**上下文**: some context" in content

    def test_find_relation_invalid_type_returns_none(self, resolver, tmp_path):
        """非法关系类型应返回 None 且不抛异常"""
        kg = authorized_knowledge_graph(wiki_base=str(tmp_path))
        assert resolver._find_relation(kg, "a", "b", "not_a_real_type") is None

    def test_find_relation_missing_relation_returns_none(self, resolver, tmp_path):
        """关系中不存在时返回 None"""
        from core.kia.relation_schema import RelationType

        kg = authorized_knowledge_graph(wiki_base=str(tmp_path))
        assert resolver._find_relation(kg, "a", "b", RelationType.BUILDS_ON.value) is None

    def test_get_unresolved_disputes_empty_when_none(self, resolver):
        assert resolver.get_unresolved_disputes() == []

    def test_create_dispute_page_includes_score_breakdown(self, resolver, tmp_path):
        """生成的争议页应包含评分明细表格。"""
        from core.app.dispute_scorer import RelationFeatures

        new_assertion = DisputeAssertion(
            page_path="note.md", title="New Assertion", content="New content", reference_count=3
        )
        existing = [
            DisputeAssertion(
                page_path="old.md", title="Old Assertion", content="Old content", reference_count=5
            )
        ]
        features_a = RelationFeatures(
            confidence=0.8, freshness=0.7, citation=0.6, quality=0.5, source=0.9, core=0.4
        )
        features_b = RelationFeatures(
            confidence=0.5, freshness=0.6, citation=0.7, quality=0.4, source=0.8, core=0.3
        )
        page = resolver.create_dispute_page(
            new_assertion,
            existing,
            conflict_strength=0.85,
            is_core_knowledge=True,
            features_a=features_a,
            features_b=features_b,
        )

        full_path = tmp_path / page.page_path
        content = full_path.read_text(encoding="utf-8")
        assert "## 评分明细" in content
        assert "| 维度 | 权重 | 新断言 | 现有断言 | 加权差 |" in content
        assert "confidence" in content
        assert "综合分" in content
        assert "建议动作" in content


class TestDisputeResolverScan:
    """Tests for DisputeResolver.scan() automatic conflict detection."""

    @pytest.fixture
    def patched_dispute_config(self, patched_get_config):
        """启用争议扫描并配置保守阈值。"""
        cfg = patched_get_config
        cfg._values["dispute_scan"] = {
            "enabled": True,
            "interval_seconds": 3600,
            "max_daily_disputes": 10,
            "max_pages_per_scan": 500,
            "min_conflict_strength": 0.5,
            "auto_resolve_min_gap": 0.30,
            "merge_min_gap": 0.15,
            "freshness_half_life_days": 30,
            "citation_max_reference": 20,
            "weights": {
                "confidence": 0.25,
                "freshness": 0.25,
                "citation": 0.20,
                "quality": 0.15,
                "source": 0.10,
                "core": 0.05,
            },
            "adaptive_learning": {"enabled": False},
        }
        return cfg

    @pytest.fixture
    def conflict_kg(self, tmp_path):
        """构造一个包含关系冲突的临时 KnowledgeGraph。"""
        from core.kia.relation_schema import Relation, RelationType

        db_path = tmp_path / "kg.db"
        kg = authorized_knowledge_graph(
            wiki_base=str(tmp_path),
            db_path=str(db_path),
        )

        # 创建两个互相矛盾的页面
        (tmp_path / "old.md").write_text("Old approach", encoding="utf-8")
        (tmp_path / "new.md").write_text("New approach", encoding="utf-8")

        # A builds_on B 但又 contradicts B -> 冲突
        kg.add_relation(
            Relation(
                source="old.md",
                target="new.md",
                relation_type=RelationType.BUILDS_ON,
                strength=0.6,
                confidence=0.9,
            )
        )
        kg.add_relation(
            Relation(
                source="old.md",
                target="new.md",
                relation_type=RelationType.CONTRADICTS,
                strength=0.7,
                confidence=0.3,
            )
        )
        return kg

    def test_scan_finds_conflict_and_creates_dispute(
        self, patched_dispute_config, tmp_path, conflict_kg
    ):
        patched_dispute_config.wiki_dir = tmp_path
        resolver = DisputeResolver(wiki_base=str(tmp_path), db_path=str(conflict_kg.db_path))
        report = resolver.scan()

        assert report["conflicts_found"] >= 1
        assert report["disputes_created"] >= 1
        dispute_dir = tmp_path / "08-Disputes"
        assert any(dispute_dir.glob("*.md"))

    def test_scan_respects_max_daily_disputes(self, patched_dispute_config, tmp_path, conflict_kg):
        patched_dispute_config.wiki_dir = tmp_path
        patched_dispute_config._values["dispute_scan"]["max_daily_disputes"] = 0
        resolver = DisputeResolver(wiki_base=str(tmp_path), db_path=str(conflict_kg.db_path))
        report = resolver.scan()

        assert report["conflicts_found"] >= 1
        assert report["disputes_created"] == 0
        assert report["skipped"] >= 1

    def test_scan_respects_min_conflict_strength_config(
        self, patched_dispute_config, tmp_path, conflict_kg
    ):
        patched_dispute_config.wiki_dir = tmp_path
        patched_dispute_config._values["dispute_scan"]["min_conflict_strength"] = 0.99
        resolver = DisputeResolver(wiki_base=str(tmp_path), db_path=str(conflict_kg.db_path))
        report = resolver.scan()

        assert report["conflicts_found"] >= 1
        assert report["disputes_created"] == 0
        assert report["auto_resolved"] == 0
        assert report["merged"] == 0
        assert report["skipped"] >= 1

    def test_scan_respects_max_pages_per_scan_config(
        self, patched_dispute_config, tmp_path, conflict_kg
    ):
        patched_dispute_config.wiki_dir = tmp_path
        patched_dispute_config._values["dispute_scan"]["max_pages_per_scan"] = 0
        resolver = DisputeResolver(wiki_base=str(tmp_path), db_path=str(conflict_kg.db_path))
        report = resolver.scan()

        assert report["conflicts_found"] >= 1
        assert report["scanned_relations"] == 0
        assert report["disputes_created"] == 0
        assert report["skipped"] >= report["conflicts_found"]

    def test_scan_does_not_duplicate_dispute(self, patched_dispute_config, tmp_path, conflict_kg):
        patched_dispute_config.wiki_dir = tmp_path
        resolver = DisputeResolver(wiki_base=str(tmp_path), db_path=str(conflict_kg.db_path))
        r1 = resolver.scan()
        r2 = resolver.scan()

        assert r1["disputes_created"] >= 1
        # 第二次扫描不应重复创建同一冲突的争议页
        assert r2["disputes_created"] == 0

    def test_scan_auto_resolves_large_confidence_gap(self, patched_dispute_config, tmp_path):
        from core.kia.relation_schema import Relation, RelationType

        patched_dispute_config.wiki_dir = tmp_path
        db_path = tmp_path / "kg.db"
        kg = authorized_knowledge_graph(
            wiki_base=str(tmp_path),
            db_path=str(db_path),
        )

        (tmp_path / "a.md").write_text("A", encoding="utf-8")
        (tmp_path / "b.md").write_text("B", encoding="utf-8")

        kg.add_relation(
            Relation(
                source="a.md",
                target="b.md",
                relation_type=RelationType.BUILDS_ON,
                strength=0.6,
                confidence=1.0,
                source_method="manual",
            )
        )
        kg.add_relation(
            Relation(
                source="a.md",
                target="b.md",
                relation_type=RelationType.CONTRADICTS,
                strength=0.6,
                confidence=0.0,
                source_method="auto",
            )
        )

        resolver = DisputeResolver(wiki_base=str(tmp_path), db_path=str(db_path))
        report = resolver.scan()

        assert report["conflicts_found"] >= 1
        assert report["auto_resolved"] >= 1
        assert report["disputes_created"] == 0

    def test_resolve_dispute_records_feedback(self, patched_dispute_config, tmp_path):
        """手动解决争议时应记录反馈，供自适应权重学习使用。"""
        from core.app.dispute_scorer import RelationFeatures

        patched_dispute_config._values["mnemos_dir"] = str(tmp_path)
        patched_dispute_config._values["dispute_scan"]["adaptive_learning"] = {
            "enabled": True,
            "min_samples_before_update": 1,
            "learning_rate": 0.5,
            "max_weight": 0.60,
            "min_weight": 0.05,
        }

        resolver = DisputeResolver(wiki_base=str(tmp_path))
        new_assertion = DisputeAssertion(
            page_path="n.md", title="Feedback", content="c", reference_count=1
        )
        features_a = RelationFeatures(confidence=1.0, freshness=0.0)
        features_b = RelationFeatures(confidence=0.0, freshness=1.0)
        page = resolver.create_dispute_page(
            new_assertion,
            [],
            0.8,
            pair_key="s|t|builds_on#s|t|contradicts",
            features_a=features_a,
            features_b=features_b,
        )

        resolver.resolve_dispute(page.page_path, "adopt_new")

        feedback_path = tmp_path / "state" / "dispute_adaptive_weights.feedback.jsonl"
        assert feedback_path.exists()
        assert '"actual_winner": "a"' in feedback_path.read_text(encoding="utf-8")


class TestDisputeEvidenceContext:
    """Tests for dispute page evidence context synchronization."""

    def test_dispute_page_contains_evidence_context(self, resolver, tmp_path):
        from core.kia.relation_schema import RelationEvidence

        new_assertion = DisputeAssertion(
            page_path="new.md",
            title="New Assertion",
            content="New content",
            reference_count=3,
            relation_context="new context",
            relation_evidence=[RelationEvidence(evidence_type="quote", content="evidence text")],
            source_method="manual",
            confidence=0.9,
            strength=0.8,
        )
        existing = [
            DisputeAssertion(
                page_path="old.md",
                title="Old Assertion",
                content="Old content",
                reference_count=5,
                relation_context="old context",
                relation_evidence=[
                    RelationEvidence(evidence_type="llm_inference", content="llm says so")
                ],
                source_method="auto",
                confidence=0.4,
                strength=0.5,
            )
        ]
        page = resolver.create_dispute_page(
            new_assertion, existing, conflict_strength=0.85, is_core_knowledge=True
        )

        full_path = tmp_path / page.page_path
        content = full_path.read_text(encoding="utf-8")
        assert "## 证据上下文" in content
        assert "### 新断言证据" in content
        assert "### 现有断言 1证据" in content
        assert "来源方法：manual" in content
        assert "来源方法：auto" in content
        assert "置信度：0.900" in content
        assert "置信度：0.400" in content
        assert "new context" in content
        assert "old context" in content
        assert "quote: evidence text" in content
        assert "llm_inference: llm says so" in content

    def test_keep_both_context_sync_is_traceable_idempotent_and_rollbackable(
        self, resolver, tmp_path
    ):
        """keep_both 应把上下文同步到双方页面，并可按 marker 回滚。"""
        (tmp_path / "new.md").write_text("New page body\n", encoding="utf-8")
        (tmp_path / "old.md").write_text("Old page body\n", encoding="utf-8")
        new_assertion = DisputeAssertion(
            page_path="new.md", title="New Assertion", content="New content", reference_count=1
        )
        existing = [
            DisputeAssertion(
                page_path="old.md", title="Old Assertion", content="Old content", reference_count=1
            )
        ]
        page = resolver.create_dispute_page(new_assertion, existing, 0.7)

        resolver.resolve_dispute(page.page_path, "keep_both", context="仅适用于 v2 场景")
        resolver.resolve_dispute(page.page_path, "keep_both", context="仅适用于 v2 场景")

        for original in (tmp_path / "new.md", tmp_path / "old.md"):
            content = original.read_text(encoding="utf-8")
            assert content.count("mnemos-dispute-context:") == 2
            assert content.count("mnemos-dispute-context:start") == 1
            assert "争议仲裁补充" in content
            assert "仅适用于 v2 场景" in content

        assert resolver.rollback_resolution_context(page.page_path) == 2

        for original in (tmp_path / "new.md", tmp_path / "old.md"):
            content = original.read_text(encoding="utf-8")
            assert "mnemos-dispute-context:" not in content
            assert "仅适用于 v2 场景" not in content

        dispute_content = (tmp_path / page.page_path).read_text(encoding="utf-8")
        assert "争议上下文回滚" in dispute_content


class TestDisputeResolutionUpdatesKG:
    """Tests that resolving a dispute updates the knowledge graph relations."""

    @pytest.fixture
    def resolver_with_pair(self, tmp_path):
        from core.kia.relation_schema import Relation, RelationType

        db_path = tmp_path / "kg.db"
        kg = authorized_knowledge_graph(
            wiki_base=str(tmp_path),
            db_path=str(db_path),
        )
        (tmp_path / "a.md").write_text("A", encoding="utf-8")
        (tmp_path / "b.md").write_text("B", encoding="utf-8")

        kg.add_relation(
            Relation(
                source="a.md",
                target="b.md",
                relation_type=RelationType.BUILDS_ON,
                strength=0.8,
                confidence=0.9,
                source_method="manual",
            )
        )
        kg.add_relation(
            Relation(
                source="a.md",
                target="b.md",
                relation_type=RelationType.CONTRADICTS,
                strength=0.7,
                confidence=0.6,
                source_method="auto",
            )
        )

        resolver = DisputeResolver(wiki_base=str(tmp_path), db_path=str(db_path))
        return resolver, kg

    def _get_relation(self, kg, rel_type):
        from core.kia.relation_schema import RelationType

        for rel in kg.get_relations("a.md", relation_type=RelationType(rel_type)):
            if rel.target == "b.md":
                return rel
        return None

    def test_resolve_adopt_new_deprecates_old_relation(self, resolver_with_pair):
        resolver, kg = resolver_with_pair
        new_assertion = DisputeAssertion(
            page_path="a.md", title="A", content="c", reference_count=1
        )
        existing = [DisputeAssertion(page_path="b.md", title="B", content="c", reference_count=1)]
        page = resolver.create_dispute_page(
            new_assertion,
            existing,
            conflict_strength=0.85,
            pair_key="a.md|b.md|builds_on#a.md|b.md|contradicts",
        )

        resolver.resolve_dispute(page.page_path, "adopt_new")

        old_rel = self._get_relation(kg, "contradicts")
        new_rel = self._get_relation(kg, "builds_on")
        assert old_rel.confidence == 0
        assert old_rel.strength == 0
        assert "deprecated by dispute resolution" in (old_rel.context or "")
        assert new_rel.confidence != 0
        assert new_rel.strength != 0

    def test_resolve_keep_old_deprecates_new_relation(self, resolver_with_pair):
        resolver, kg = resolver_with_pair
        new_assertion = DisputeAssertion(
            page_path="a.md", title="A", content="c", reference_count=1
        )
        existing = [DisputeAssertion(page_path="b.md", title="B", content="c", reference_count=1)]
        page = resolver.create_dispute_page(
            new_assertion,
            existing,
            conflict_strength=0.85,
            pair_key="a.md|b.md|builds_on#a.md|b.md|contradicts",
        )

        resolver.resolve_dispute(page.page_path, "keep_old")

        new_rel = self._get_relation(kg, "builds_on")
        old_rel = self._get_relation(kg, "contradicts")
        assert new_rel.confidence == 0
        assert new_rel.strength == 0
        assert "deprecated by dispute resolution" in (new_rel.context or "")
        assert old_rel.confidence != 0
        assert old_rel.strength != 0

    def test_resolve_without_pair_key_does_not_crash(self, resolver, tmp_path):
        new_assertion = DisputeAssertion(
            page_path="n.md", title="No Pair", content="c", reference_count=1
        )
        page = resolver.create_dispute_page(new_assertion, [], 0.5)
        resolver.resolve_dispute(page.page_path, "adopt_new")

        full_path = tmp_path / page.page_path
        content = full_path.read_text(encoding="utf-8")
        assert "- [x] **采纳新断言**" in content
