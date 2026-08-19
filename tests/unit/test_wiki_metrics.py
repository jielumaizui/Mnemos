# -*- coding: utf-8 -*-
"""Unit tests for core/wiki_metrics.py"""

import sqlite3
import time
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from core.wiki_metrics import (
    HeatLevel,
    KnowledgeStage,
    PageMetrics,
    QualityLevel,
    WikiMetrics,
    _utcnow,
    compute_evidence_level,
    compute_knowledge_stage,
    compute_heat_level,
    _heat_level_to_display,
    _stage_to_display,
    _status_to_display,
    hash_query,
    quick_quality_score,
    get_default_metrics,
)

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def test_utcnow_is_timezone_aware():
    """_utcnow 应返回带时区的 UTC 时间。"""
    now = _utcnow()
    assert now.tzinfo is not None


def test_compute_evidence_level():
    """compute_evidence_level 应正确分级。"""
    assert compute_evidence_level(0) == 1
    assert compute_evidence_level(1) == 1
    assert compute_evidence_level(2) == 2
    assert compute_evidence_level(3) == 2
    assert compute_evidence_level(4) == 3
    assert compute_evidence_level(5) == 3
    assert compute_evidence_level(6) == 4
    assert compute_evidence_level(10) == 4


def test_compute_knowledge_stage():
    """compute_knowledge_stage 应正确判定阶段。"""
    assert compute_knowledge_stage(0, "draft") == "P3"
    assert compute_knowledge_stage(1, "draft") == "P3"
    assert compute_knowledge_stage(2, "draft") == "P2"
    assert compute_knowledge_stage(6, "merged") == "P1"
    assert compute_knowledge_stage(6, "verified") == "P0"
    assert compute_knowledge_stage(10, "verified") == "P0"


def test_knowledge_stage_enum_matches_compute_contract():
    """KnowledgeStage enum values define the persisted P0-P3 stage contract."""
    assert [stage.value for stage in KnowledgeStage] == ["P0", "P1", "P2", "P3"]

    cases = [
        (6, "verified", KnowledgeStage.P0, "核心"),
        (6, "merged", KnowledgeStage.P1, "成熟"),
        (2, "draft", KnowledgeStage.P2, "发展中"),
        (1, "draft", KnowledgeStage.P3, "原始"),
    ]

    for source_count, status, stage, display in cases:
        assert compute_knowledge_stage(source_count, status) == stage.value
        assert _stage_to_display(stage.value) == display


def test_compute_heat_level():
    """compute_heat_level 应正确分级。"""
    now = _utcnow().isoformat()
    week_ago = (_utcnow() - timedelta(days=5)).isoformat()
    month_ago = (_utcnow() - timedelta(days=20)).isoformat()
    old = (_utcnow() - timedelta(days=60)).isoformat()

    assert compute_heat_level(now) == "hot"
    assert compute_heat_level(week_ago) == "hot"
    assert compute_heat_level(month_ago) == "warm"
    assert compute_heat_level(old) == "cold"

    # with last_accessed
    assert compute_heat_level(old, now) == "hot"
    assert compute_heat_level(old, month_ago) == "warm"


def test_compute_heat_level_bad_date():
    """无效日期应返回 cold。"""
    assert compute_heat_level("not-a-date") == "cold"


def test_display_helpers():
    """显示映射应正确。"""
    assert _heat_level_to_display("hot") == "热"
    assert _heat_level_to_display("warm") == "温"
    assert _heat_level_to_display("cold") == "冷"
    assert _stage_to_display("P0") == "核心"
    assert _stage_to_display("P3") == "原始"
    assert _status_to_display("draft") == "草稿"
    assert _status_to_display("verified") == "已验证"


def test_hash_query():
    """hash_query 应返回 12 位十六进制字符串。"""
    h = hash_query("Hello World")
    assert len(h) == 12
    assert int(h, 16) >= 0  # valid hex
    # 相同查询应产生相同哈希
    assert hash_query("Hello World") == h
    # 大小写和多余空格应归一化
    assert hash_query("  HELLO   WORLD  ") == h


def test_quick_quality_score_empty():
    """空内容应返回 0。"""
    assert quick_quality_score("") == 0.0
    assert quick_quality_score("short") == 0.0


def test_quick_quality_score_with_structure():
    """有结构的内容应获得更高分。"""
    content = (
        "# Title\n\nSome text here.\n\n## Section\n- item1\n- item2\n\n```python\nprint(1)\n```\n"
    )
    score = quick_quality_score(content)
    assert score > 0.0
    assert score <= 100.0


# ---------------------------------------------------------------------------
# PageMetrics dataclass
# ---------------------------------------------------------------------------


def test_page_metrics_defaults():
    """PageMetrics 默认值应正确。"""
    pm = PageMetrics(wiki_path="test.md")
    assert pm.wiki_path == "test.md"
    assert pm.knowledge_stage == "P3"
    assert pm.evidence_level == 1
    assert pm.heat_level == "cold"
    assert pm.quality_level == "acceptable"
    assert pm.page_role == "knowledge"
    assert pm.source_refs == []
    assert pm.tags == []


def test_quality_level_enum_matches_score_contract():
    """QualityLevel enum values define the persisted quality_level contract."""
    assert [level.value for level in QualityLevel] == [
        "excellent",
        "good",
        "acceptable",
        "poor",
    ]
    assert PageMetrics(wiki_path="test.md").quality_level == QualityLevel.ACCEPTABLE.value
    assert WikiMetrics._compute_quality_level(80) == QualityLevel.EXCELLENT.value
    assert WikiMetrics._compute_quality_level(60) == QualityLevel.GOOD.value
    assert WikiMetrics._compute_quality_level(40) == QualityLevel.ACCEPTABLE.value
    assert WikiMetrics._compute_quality_level(39.9) == QualityLevel.POOR.value


# ---------------------------------------------------------------------------
# WikiMetrics init
# ---------------------------------------------------------------------------


class TestWikiMetricsInit:
    """WikiMetrics 初始化测试"""

    def test_custom_db_path(self, tmp_path):
        """应支持自定义数据库路径。"""
        db = tmp_path / "metrics.db"
        wm = WikiMetrics(db_path=str(db))
        assert wm.db_path == db

    def test_custom_wiki_without_db_override_uses_local_metrics(self, tmp_path):
        wiki = tmp_path / "custom-wiki"

        wm = WikiMetrics(wiki_dir=str(wiki))

        assert wm.db_path == wiki / ".kg" / "wiki_metrics.db"

    def test_close_closes_persistent_connection(self, tmp_path):
        """close() 应关闭普通持久连接。"""
        wm = WikiMetrics(db_path=str(tmp_path / "metrics.db"))
        conn = wm._get_conn()
        wm.close()
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_legacy_encrypted_artifact_does_not_change_connection_mode(self, tmp_path):
        """旧加密 artifact 文件不再改变 WikiMetrics 连接模式。"""
        db = tmp_path / "wiki_metrics.db"
        db.with_suffix(db.suffix + ".enc").touch()
        wm = WikiMetrics(db_path=str(db))
        assert wm._transient_sqlite is False
        assert getattr(wm._local, "transient_conns", []) == []
        assert wm._transient_conns == set()

        wm.upsert_page("concepts/test.md", title="test")

        assert getattr(wm._local, "transient_conns", []) == []
        assert wm._transient_conns == set()

    def test_lazy_path_fallback(self, monkeypatch, tmp_path):
        """未提供 db_path 时应使用 LazyPath。"""
        fake_cfg = MagicMock()
        fake_cfg.database_dir = tmp_path / "db"
        monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)
        wm = WikiMetrics()
        assert "wiki_metrics.db" in str(wm.db_path)

    def test_migrate_old_schema_missing_columns(self, tmp_path):
        """旧版 page_metrics 表应自动补齐缺失列（source_refs/tags/knowledge_stage 等）。"""
        db = tmp_path / "old_metrics.db"
        conn = sqlite3.connect(str(db))
        # 模拟只包含早期列的旧表
        conn.execute("""
            CREATE TABLE page_metrics (
                wiki_path TEXT PRIMARY KEY,
                title TEXT,
                heat_score REAL DEFAULT 0.0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

        wm = WikiMetrics(db_path=str(db))
        # 使用新列 upsert 不应抛 schema 错误
        wm.upsert_page(
            "concepts/test.md",
            title="test",
            source_refs=["src1", "src2"],
            tags=["t1"],
            knowledge_stage="P2",
            quality_level="good",
        )

        columns = {
            row[1]
            for row in sqlite3.connect(str(db))
            .execute("PRAGMA table_info(page_metrics)")
            .fetchall()
        }
        for col in (
            "source_refs",
            "tags",
            "knowledge_stage",
            "quality_level",
            "created_at",
            "last_accessed",
            "status",
            "page_role",
        ):
            assert col in columns, f"missing column {col}"


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


@pytest.fixture
def wm(tmp_path):
    """提供已初始化的 WikiMetrics 实例（临时数据库）。"""
    db_path = tmp_path / "test_metrics.db"
    return WikiMetrics(db_path=str(db_path))


class TestUpsertPage:
    """upsert_page 测试"""

    def test_insert_new_page(self, wm):
        """插入新页面应创建记录。"""
        wm.upsert_page("test.md", title="Test", quality_score=85.0)
        page = wm.get_page("test.md")
        assert page is not None
        assert page.title == "Test"
        assert page.quality_score == 85.0
        assert page.page_role == "knowledge"

    def test_scan_all_pages_records_generated_page_role(self, tmp_path):
        """扫描 Wiki 时应把生成占位页角色写入 page_metrics。"""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        page = wiki_dir / "placeholder.md"
        page.write_text(
            "---\n名称: 占位页\n---\n\n"
            "# placeholder\n\n"
            "该页面为自动创建的占位/消歧页，用于修复悬空链接。需要后续补充实质内容。\n",
            encoding="utf-8",
        )
        wm = WikiMetrics(db_path=str(tmp_path / "wiki_metrics.db"), wiki_dir=str(wiki_dir))

        wm.scan_all_pages()

        metrics = wm.get_page("placeholder.md")
        assert metrics is not None
        assert metrics.page_role == "generated_placeholder"

    def test_update_existing_page(self, wm):
        """更新已有页面应修改字段。"""
        wm.upsert_page("test.md", title="Old")
        wm.upsert_page("test.md", title="New", heat_score=5.0)
        page = wm.get_page("test.md")
        assert page.title == "New"
        assert page.heat_score == 5.0

    def test_preserve_last_updated(self, wm):
        """_preserve_last_updated 应阻止 last_updated 更新。"""
        wm.upsert_page("test.md", title="Test")
        old_page = wm.get_page("test.md")
        old_time = old_page.last_updated
        time.sleep(0.01)
        wm.upsert_page("test.md", heat_score=1.0, _preserve_last_updated=True)
        new_page = wm.get_page("test.md")
        assert new_page.last_updated == old_time

    def test_json_list_fields(self, wm):
        """source_refs 和 tags 应被序列化为 JSON。"""
        wm.upsert_page("test.md", source_refs=["ref1", "ref2"], tags=["a", "b"])
        page = wm.get_page("test.md")
        assert page.source_refs == ["ref1", "ref2"]
        assert page.tags == ["a", "b"]

    def test_canonical_path_resolution(self, wm):
        """不同路径形式应解析为同一记录。"""
        wm.upsert_page("concepts/test.md", title="Test")
        page = wm.get_page("concepts/test")
        assert page is not None
        assert page.title == "Test"


class TestGetPage:
    """get_page 测试"""

    def test_get_nonexistent(self, wm):
        """不存在的页面应返回 None。"""
        assert wm.get_page("nonexistent.md") is None

    def test_get_by_various_paths(self, wm):
        """多种路径形式应能找到同一页面。"""
        wm.upsert_page("dir/page.md", title="Page")
        assert wm.get_page("dir/page.md") is not None
        assert wm.get_page("dir/page") is not None


class TestListPages:
    """list_pages 测试"""

    def test_list_all(self, wm):
        """无过滤应返回所有页面。"""
        wm.upsert_page("a.md", title="A", knowledge_stage="P3")
        wm.upsert_page("b.md", title="B", knowledge_stage="P2")
        pages = wm.list_pages()
        assert len(pages) == 2

    def test_filter_by_stage(self, wm):
        """按 stage 过滤。"""
        wm.upsert_page("a.md", knowledge_stage="P3")
        wm.upsert_page("b.md", knowledge_stage="P2")
        pages = wm.list_pages(stage="P2")
        assert len(pages) == 1
        assert pages[0].knowledge_stage == "P2"

    def test_filter_by_quality(self, wm):
        """按质量分数过滤。"""
        wm.upsert_page("a.md", quality_score=30.0)
        wm.upsert_page("b.md", quality_score=70.0)
        pages = wm.list_pages(min_quality=50.0)
        assert len(pages) == 1
        assert pages[0].quality_score == 70.0

    def test_filter_by_freshness(self, wm):
        """按新鲜度过滤。"""
        wm.upsert_page("a.md", freshness_days=5)
        wm.upsert_page("b.md", freshness_days=50)
        pages = wm.list_pages(max_freshness=10)
        assert len(pages) == 1
        assert pages[0].freshness_days == 5


# ---------------------------------------------------------------------------
# Quality & Heat
# ---------------------------------------------------------------------------


class TestAssessQuality:
    """assess_quality 测试"""

    def test_assess_and_update(self, wm):
        """应评估并更新质量分数。"""
        score = wm.assess_quality("page.md", "# Title\n\nSome content here.")
        assert score > 0.0
        page = wm.get_page("page.md")
        assert page.quality_score == round(score, 1)
        assert page.quality_level in ("excellent", "good", "acceptable", "poor")


class TestUpdateHeat:
    """update_heat 测试"""

    def test_create_new_on_heat_update(self, wm):
        """页面不存在时应创建记录。"""
        wm.update_heat("new.md", access_type="read")
        page = wm.get_page("new.md")
        assert page is not None
        assert page.heat_score == 1.0
        assert page.last_accessed != ""

    def test_increment_heat_score(self, wm):
        """应增加热力分数。"""
        wm.upsert_page("page.md", heat_score=5.0, last_updated=_utcnow().isoformat())
        wm.update_heat("page.md", access_type="read")
        page = wm.get_page("page.md")
        assert page.heat_score == 6.0

    def test_citation_boost(self, wm):
        """citation 应有更高加分。"""
        wm.upsert_page("page.md", heat_score=0.0, last_updated=_utcnow().isoformat())
        wm.update_heat("page.md", access_type="citation")
        page = wm.get_page("page.md")
        assert page.heat_score == 5.0


class TestDecayAll:
    """decay_all 测试"""

    def test_decay_old_pages(self, wm):
        """旧页面应被衰减。"""
        old_time = (_utcnow() - timedelta(days=30)).isoformat()
        wm.upsert_page("old.md", heat_score=10.0, last_updated=old_time)
        decayed = wm.decay_all(decay_days=15)
        assert decayed >= 1
        page = wm.get_page("old.md")
        assert page.heat_score < 10.0

    def test_no_decay_for_fresh_pages(self, wm):
        """新页面不应被衰减。"""
        wm.upsert_page("fresh.md", heat_score=10.0, last_updated=_utcnow().isoformat())
        decayed = wm.decay_all(decay_days=15)
        assert decayed == 0


# ---------------------------------------------------------------------------
# Page queries by level
# ---------------------------------------------------------------------------


class TestGetPagesByLevel:
    """get_pages_by_level 测试"""

    def test_filter_by_heat_level(self, wm):
        """按热力等级过滤。"""
        wm.upsert_page("hot.md", heat_level="hot", heat_score=10.0)
        wm.upsert_page("cold.md", heat_level="cold", heat_score=0.0)
        hot_pages = wm.get_pages_by_level(HeatLevel.HOT)
        assert len(hot_pages) == 1
        assert hot_pages[0].wiki_path == "hot.md"

    def test_filter_by_string(self, wm):
        """字符串参数也应工作。"""
        wm.upsert_page("warm.md", heat_level="warm", heat_score=3.0)
        pages = wm.get_pages_by_level("warm")
        assert len(pages) == 1


class TestGetColdPages:
    """get_cold_pages 测试"""

    def test_returns_cold_pages(self, wm):
        """应返回冷页面。"""
        wm.upsert_page("cold.md", heat_level="cold", quality_score=10.0, freshness_days=100)
        wm.upsert_page("hot.md", heat_level="hot", quality_score=90.0, freshness_days=1)
        pages = wm.get_cold_pages()
        assert len(pages) == 1
        assert pages[0].wiki_path == "cold.md"


# ---------------------------------------------------------------------------
# Frontmatter sync
# ---------------------------------------------------------------------------


class TestSyncHeatToFrontmatter:
    """sync_heat_to_frontmatter 测试"""

    def test_sync_updates_frontmatter(self, wm, tmp_path):
        """应更新 frontmatter 中的热力字段。"""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        wm._wiki_dir = wiki_dir
        page = wiki_dir / "test.md"
        page.write_text(
            "---\ntitle: Test\n热度等级: 冷\n---\n\nBody.\n", encoding="utf-8"
        )

        wm.upsert_page("test.md", heat_level="hot", heat_score=8.5, quality_score=75.0)
        result = wm.sync_heat_to_frontmatter(page)
        assert result is True

        content = page.read_text(encoding="utf-8")
        assert "热度等级: 热" in content
        assert "heat_level:" not in content
        assert wm.sync_heat_to_frontmatter(page) is False
        assert page.read_text(encoding="utf-8") == content

    def test_sync_missing_page_returns_false(self, wm, tmp_path):
        """页面不存在时应返回 False。"""
        missing = tmp_path / "missing.md"
        result = wm.sync_heat_to_frontmatter(missing)
        assert result is False

    def test_sync_no_metrics_returns_false(self, wm, tmp_path):
        """无 metrics 时应返回 False。"""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        wm._wiki_dir = wiki_dir
        page = wiki_dir / "test.md"
        page.write_text("---\n---\n\nBody.\n", encoding="utf-8")
        result = wm.sync_heat_to_frontmatter(page)
        assert result is False

    def test_sync_preserves_producer_provenance_fields(self, wm, tmp_path):
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        wm._wiki_dir = wiki_dir
        page = wiki_dir / "report.md"
        page.write_text(
            "---\n"
            "report_id: immutable-report-id\n"
            "source_db: /evidence/source.db\n"
            "source_payload_digest: abc123\n"
            "来源:\n"
            "- event:abc123\n"
            "来源数量: 1\n"
            "---\n\n# Report\n\nEvidence-backed report.\n",
            encoding="utf-8",
        )
        wm.upsert_page(
            "report.md",
            heat_level="hot",
            heat_score=3.0,
            quality_score=72.0,
            source_count=1,
            source_refs=["event:abc123"],
        )

        assert wm.sync_heat_to_frontmatter(page) is True
        content = page.read_text(encoding="utf-8")
        assert "report_id: immutable-report-id" in content
        assert "source_db: /evidence/source.db" in content
        assert "source_payload_digest: abc123" in content
        assert "event:abc123" in content

    def test_sync_does_not_rewrite_producer_owned_projection_pages(self, wm, tmp_path):
        wiki_dir = tmp_path / "wiki"
        projection = wiki_dir / "L2.4-KG" / "Entities" / "kg-python.md"
        projection.parent.mkdir(parents=True)
        original = (
            "---\n"
            "类型: kg_entity_projection\n"
            "名称: Python\n"
            "来源数量: 2\n"
            "---\n\n# Python\n\nDeterministic KG projection.\n"
        )
        projection.write_text(original, encoding="utf-8")
        wm._wiki_dir = wiki_dir
        wm.upsert_page(
            "L2.4-KG/Entities/kg-python.md",
            heat_level="hot",
            heat_score=3.0,
            quality_score=72.0,
            source_count=2,
        )

        assert wm.sync_heat_to_frontmatter(projection) is False
        assert projection.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


class TestGenerateHeatReport:
    """generate_heat_report 测试"""

    def test_generates_markdown(self, wm):
        """应生成 Markdown 报告。"""
        wm.upsert_page("hot.md", heat_level="hot", title="Hot Page")
        report = wm.generate_heat_report()
        assert "report_type: heatmap" in report
        assert "data_reliability: db_backed" in report
        assert "source_count: 1" in report
        assert "#page_metrics" in report.split("---", 2)[1]
        assert "# 热力地图" in report
        assert "HOT: 1" in report
        assert "Hot Page" in report

    def test_write_to_file(self, wm, tmp_path):
        """write=True 时应写入文件。"""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        wm._wiki_dir = wiki_dir
        wm.upsert_page("hot.md", heat_level="hot", title="Hot Page")
        _ = wm.generate_heat_report(write=True, wiki_dir=str(wiki_dir))
        report_dir = wiki_dir / "99-Reports"
        assert report_dir.exists()
        files = list(report_dir.glob("*.md"))
        assert len(files) == 1
        assert "热力地图" in files[0].read_text(encoding="utf-8")

    def test_write_skips_when_no_metrics(self, wm, tmp_path):
        """没有来源页面时不写入 Obsidian 报告文件。"""
        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir()
        report = wm.generate_heat_report(write=True, wiki_dir=str(wiki_dir))

        assert "data_reliability: unavailable" in report
        assert not (wiki_dir / "99-Reports").exists()


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------


class TestRelations:
    """页面关系测试"""

    def test_add_relation(self, wm):
        """应能添加关系。"""
        wm.add_relation("a.md", "b.md", relation_type="link", strength=2.0)
        rels = wm.get_relations("a.md")
        assert len(rels) == 1
        assert rels[0]["to"] == "b.md"
        assert rels[0]["type"] == "link"
        assert rels[0]["strength"] == 2.0

    def test_backlink_count_updated(self, wm):
        """添加关系应更新反向链接计数。"""
        wm.upsert_page("a.md", title="A")
        wm.upsert_page("b.md", title="B")
        wm.add_relation("a.md", "b.md")
        page_b = wm.get_page("b.md")
        assert page_b.backlink_count == 1

    def test_multiple_relations(self, wm):
        """应支持多个关系。"""
        wm.add_relation("a.md", "b.md")
        wm.add_relation("a.md", "c.md")
        rels = wm.get_relations("a.md")
        assert len(rels) == 2


# ---------------------------------------------------------------------------
# Merge candidates
# ---------------------------------------------------------------------------


class TestGetMergeCandidates:
    """get_merge_candidates 测试"""

    def test_finds_candidates(self, wm):
        """应找到合并候选。"""
        old_time = (_utcnow() - timedelta(days=40)).isoformat()
        for i in range(5):
            wm.upsert_page(
                f"topic-{i}.md",
                title=f"Topic {i}",
                knowledge_stage="P3",
                freshness_days=40,
                last_updated=old_time,
            )
        candidates = wm.get_merge_candidates(min_pages=3, max_freshness=30)
        assert len(candidates) >= 1

    def test_no_candidates_when_fresh(self, wm):
        """无冷页面时不应返回候选。"""
        wm.upsert_page("a.md", title="A", knowledge_stage="P3", freshness_days=1)
        candidates = wm.get_merge_candidates(min_pages=3)
        assert candidates == []


# ---------------------------------------------------------------------------
# Mark deprecated/merged
# ---------------------------------------------------------------------------


class TestMarkOperations:
    """mark_deprecated / mark_merged 测试"""

    def test_mark_deprecated(self, wm):
        """应标记为废弃。"""
        wm.upsert_page("page.md", title="Page")
        wm.mark_deprecated("page.md", reason="outdated")
        page = wm.get_page("page.md")
        assert page.status == "deprecated"
        assert "outdated" in page.tags

    def test_mark_merged(self, wm):
        """应标记为已合并。"""
        wm.upsert_page("page.md", title="Page")
        wm.mark_merged("page.md", merged_into="target.md")
        page = wm.get_page("page.md")
        assert page.status == "deprecated"
        assert any("target.md" in str(t) for t in page.tags)


# ---------------------------------------------------------------------------
# Summary & report
# ---------------------------------------------------------------------------


class TestGetSummary:
    """get_summary 测试"""

    def test_empty_summary(self, wm):
        """空数据库应返回零值。"""
        summary = wm.get_summary()
        assert summary["total_pages"] == 0
        assert summary["avg_quality"] == 0.0

    def test_summary_with_pages(self, wm):
        """有页面时应返回统计。"""
        wm.upsert_page("a.md", knowledge_stage="P3", quality_score=60.0, status="draft")
        wm.upsert_page("b.md", knowledge_stage="P2", quality_score=80.0, status="draft")
        summary = wm.get_summary()
        assert summary["total_pages"] == 2
        assert summary["by_stage"]["P3"] == 1
        assert summary["by_stage"]["P2"] == 1
        assert summary["avg_quality"] == 70.0


class TestGenerateReport:
    """generate_report 测试"""

    def test_report_contains_sections(self, wm):
        """报告应包含各节。"""
        wm.upsert_page("page.md", title="Page", knowledge_stage="P3")
        report = wm.generate_report()
        assert "Wiki Metrics Report" in report
        assert "总页面" in report
        assert "知识阶段分布" in report
        assert "P3" in report


# ---------------------------------------------------------------------------
# Static helpers
# ---------------------------------------------------------------------------


class TestSplitJoinFrontmatter:
    """_split_frontmatter / _join_frontmatter 测试"""

    def test_split_with_frontmatter(self):
        """应正确分离 frontmatter 和 body。"""
        content = "---\ntitle: Test\n---\n\nBody text."
        fm, body = WikiMetrics._split_frontmatter(content)
        assert fm.get("title") == "Test"
        assert "Body text." in body

    def test_split_without_frontmatter(self):
        """无 frontmatter 应返回空字典。"""
        content = "Just body text."
        fm, body = WikiMetrics._split_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_join_frontmatter(self):
        """应正确拼接 frontmatter 和 body。"""
        result = WikiMetrics._join_frontmatter({"title": "Test"}, "Body.")
        assert result.startswith("---\n")
        assert "title: Test" in result
        assert result.endswith("Body.")


class TestDecayDaysFor:
    """_decay_days_for 测试"""

    def test_technology_decay(self, wm):
        """technology 分类应有 7 天衰减期。"""
        assert wm._decay_days_for("tech/page.md", tags_json='["category:technology"]') == 7

    def test_default_decay(self, wm):
        """未知分类应使用默认值。"""
        assert wm._decay_days_for("other/page.md", default=15) == 15


# ---------------------------------------------------------------------------
# Category detection
# ---------------------------------------------------------------------------


class TestCategoryForPath:
    """_category_for_path 测试"""

    def test_from_frontmatter(self, wm, tmp_path):
        """应从 frontmatter 中读取 category。"""
        page = tmp_path / "test.md"
        page.write_text("---\ncategory: technology\n---\n\nBody.\n", encoding="utf-8")
        assert wm._category_for_path(str(page)) == "technology"

    def test_from_tags(self, wm):
        """应从 tags 中提取 category。"""
        assert (
            wm._category_for_path("page.md", tags_json='["category:methodology"]') == "methodology"
        )

    def test_fallback_empty(self, wm):
        """无匹配时应返回空字符串。"""
        assert wm._category_for_path("page.md", tags_json="[]") == ""


# ---------------------------------------------------------------------------
# Canonical path
# ---------------------------------------------------------------------------


class TestCanonicalPath:
    """_canonical_metric_path / _path_candidates 测试"""

    def test_canonical_adds_md(self, wm):
        """无后缀时应添加 .md。"""
        assert wm._canonical_metric_path("dir/page") == "dir/page.md"

    def test_canonical_keeps_md(self, wm):
        """有 .md 后缀应保持。"""
        assert wm._canonical_metric_path("dir/page.md") == "dir/page.md"

    def test_path_candidates_variants(self, wm):
        """应返回多种路径变体。"""
        candidates = wm._path_candidates("dir/page")
        assert "dir/page" in candidates
        assert "dir/page.md" in candidates


def test_reconcile_page_lifecycle_create_move_delete(tmp_path):
    wiki = tmp_path / "wiki"
    old = wiki / "00-Inbox" / "page.md"
    new = wiki / "04-Concepts" / "page.md"
    old.parent.mkdir(parents=True)
    new.parent.mkdir(parents=True)
    old.write_text("---\n标题: Page\n---\n# Page\ncontent", encoding="utf-8")
    metrics = WikiMetrics(db_path=str(tmp_path / "metrics.db"), wiki_dir=str(wiki))
    created = metrics.reconcile_page_lifecycle(page_path=str(old), mutation_type="create")
    assert created["inserted"] == 1
    old.rename(new)
    moved = metrics.reconcile_page_lifecycle(
        page_path=str(new), previous_path=str(old), mutation_type="move"
    )
    assert moved["deleted"] == 1
    assert metrics.get_page("04-Concepts/page.md") is not None
    new.unlink()
    deleted = metrics.reconcile_page_lifecycle(page_path=str(new), mutation_type="delete")
    assert deleted["deleted"] == 1
    assert metrics.get_page("04-Concepts/page.md") is None
    metrics.close()


@pytest.mark.parametrize("failure", ["missing", "outside", "scoring"])
def test_metrics_move_failure_preserves_old_projection(tmp_path, failure, monkeypatch):
    wiki = tmp_path / "wiki"
    old = wiki / "old.md"
    new = wiki / "new.md"
    wiki.mkdir()
    old.write_text("# Old\n\ncontent", encoding="utf-8")
    metrics = WikiMetrics(db_path=str(tmp_path / "metrics.db"), wiki_dir=str(wiki))
    metrics.reconcile_page_lifecycle(page_path=str(old), mutation_type="create")

    if failure == "missing":
        target = new
    elif failure == "outside":
        target = tmp_path / "outside.md"
        target.write_text("# Outside", encoding="utf-8")
    else:
        target = new
        old.rename(new)
        monkeypatch.setattr(metrics, "_score_content", lambda _content: (_ for _ in ()).throw(ValueError("bad score")))

    if failure == "scoring":
        with pytest.raises(ValueError, match="bad score"):
            metrics.reconcile_page_lifecycle(
                page_path=str(target), previous_path=str(old), mutation_type="move"
            )
    else:
        result = metrics.reconcile_page_lifecycle(
            page_path=str(target), previous_path=str(old), mutation_type="move"
        )
        assert result["status"] in {"page_not_found", "invalid_path"}

    assert metrics.get_page("old.md") is not None
    assert metrics.get_page("new.md") is None
    metrics.close()


def test_scan_converges_without_metrics_frontmatter_feedback(tmp_path):
    wiki = tmp_path / "wiki"
    page = wiki / "page.md"
    wiki.mkdir()
    page.write_text(
        "---\n标题: Page\n状态: 活跃\n知识阶段: 成熟\n来源数量: 1\n---\n"
        "# Page\n\nStable body with [[another-page]] and useful detail.\n",
        encoding="utf-8",
    )
    metrics = WikiMetrics(db_path=str(tmp_path / "metrics.db"), wiki_dir=str(wiki))
    metrics.scan_all_pages()
    after_first = page.read_bytes()
    first = metrics.get_page("page.md")
    metrics.scan_all_pages()
    after_second = page.read_bytes()
    second = metrics.get_page("page.md")
    assert after_second == after_first
    assert second.title == first.title
    assert second.knowledge_stage == first.knowledge_stage
    assert second.quality_score == first.quality_score
    assert second.completeness == first.completeness
    metrics.close()


# ---------------------------------------------------------------------------
# Global instance
# ---------------------------------------------------------------------------


class TestGetDefaultMetrics:
    """get_default_metrics 测试"""

    @pytest.fixture(autouse=True)
    def isolate_default_metrics(self, monkeypatch, tmp_path):
        import core.wiki_metrics as wiki_metrics_module

        existing = wiki_metrics_module._default_metrics
        if existing is not None:
            existing.close()
        monkeypatch.setattr(wiki_metrics_module, "_default_metrics", None)
        monkeypatch.setattr(wiki_metrics_module, "DB_PATH", tmp_path / "wiki_metrics.db")
        monkeypatch.setattr(wiki_metrics_module, "WIKI_DIR", tmp_path / "wiki")
        yield
        current = wiki_metrics_module._default_metrics
        if current is not None:
            current.close()
        monkeypatch.setattr(wiki_metrics_module, "_default_metrics", None)

    def test_returns_wiki_metrics(self):
        """应返回 WikiMetrics 实例。"""
        m = get_default_metrics()
        assert isinstance(m, WikiMetrics)

    def test_singleton(self):
        """多次调用应返回同一实例。"""
        m1 = get_default_metrics()
        m2 = get_default_metrics()
        assert m1 is m2
