import json
import sqlite3
from dataclasses import dataclass


def _write_page(path, frontmatter, body="# Title\n正文\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")


class FakeTrail:
    def __init__(self, stats):
        self.stats = stats

    def get_page_stats(self, page_path):
        return self.stats.get(page_path, {})


@dataclass
class FakeIssue:
    issue_type: str
    severity: str


class FakeReport:
    health_score = 82.0
    issues = [FakeIssue("outdated", "high"), FakeIssue("low_confidence", "critical")]


class FakeImmune:
    def full_scan(self):
        return FakeReport()


def test_generate_scans_vault_and_persists_profile(tmp_path):
    from core.kia.metis import ProfileGenerator

    keep_a = tmp_path / "00-Inbox" / "a.md"
    keep_b = tmp_path / "03-Tech" / "b.md"
    excluded = tmp_path / "99-Archive" / "old.md"
    _write_page(keep_a, "领域: 技术\n类型: 问题-解决\n复杂度: 入门\n置信度: 0.9\n时效性: 版本绑定\n创建日期: 2026-01-01\n")
    _write_page(keep_b, "领域: 产品\n类型: 方法论\n复杂度: 中级\n置信度: 0.7\n时效性: 上下文相关\n创建日期: 2026-02-01\n")
    _write_page(excluded, "领域: 管理\n类型: 决策记录\n")

    db_path = tmp_path / "profiles.db"
    profile = ProfileGenerator(
        wiki_base=str(tmp_path),
        db_path=db_path,
        trail=FakeTrail({}),
        immune=FakeImmune(),
    ).generate()

    assert profile.total_knowledge == 2
    assert profile.domain_distribution == {"技术": 1, "产品": 1}
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT total_knowledge, domain_distribution FROM knowledge_profiles").fetchone()
    assert row[0] == 2
    assert json.loads(row[1]) == {"技术": 1, "产品": 1}


def test_growth_tracks_created_and_updated_months(tmp_path):
    from core.kia.metis import ProfileGenerator

    page = tmp_path / "03-Tech" / "growth.md"
    _write_page(
        page,
        "领域: 技术\n类型: 方法论\n复杂度: 中级\n置信度: 0.8\n时效性: 稳定\n创建日期: 2026-01-02\n修改日期: 2026-03-04\n",
    )

    profile = ProfileGenerator(
        wiki_base=str(tmp_path),
        db_path=tmp_path / "profiles.db",
        trail=FakeTrail({}),
        immune=FakeImmune(),
    ).generate()

    assert {"month": "2026-01", "created": 1, "updated": 0, "active": False} in profile.growth_trend
    assert {"month": "2026-03", "created": 0, "updated": 1, "active": True} in profile.growth_trend


def test_learning_mode_includes_conversion_paths_and_effect_mode(tmp_path):
    from core.kia.metis import ProfileGenerator

    problem = tmp_path / "03-Tech" / "problem.md"
    method = tmp_path / "03-Tech" / "method.md"
    _write_page(problem, "领域: 技术\n类型: 问题-解决\n复杂度: 中级\n置信度: 0.8\n时效性: 稳定\n创建日期: 2026-01-01\n", "[[method]]\n")
    _write_page(method, "领域: 技术\n类型: 方法论\n复杂度: 中级\n置信度: 0.8\n时效性: 稳定\n创建日期: 2026-01-01\n")

    profile = ProfileGenerator(
        wiki_base=str(tmp_path),
        db_path=tmp_path / "profiles.db",
        trail=FakeTrail({str(problem): {"total_queries": 2, "effect_score": 0.9}}),
        immune=FakeImmune(),
    ).generate()

    assert profile.learning_mode["conversion_paths"] == 1
    assert profile.learning_mode["effect_driven_mode"] == "解决效果驱动型"


def test_multisource_dimensions_and_completeness(tmp_path):
    from core.kia.metis import ProfileGenerator

    page = tmp_path / "03-Tech" / "stats.md"
    _write_page(page, "领域: 技术\n类型: 问题-解决\n复杂度: 高级\n置信度: 0.9\n时效性: 稳定\n创建日期: 2026-01-01\n")

    profile = ProfileGenerator(
        wiki_base=str(tmp_path),
        db_path=tmp_path / "profiles.db",
        trail=FakeTrail({str(page): {"total_queries": 4, "total_modifications": 2, "effect_score": 0.75}}),
        immune=FakeImmune(),
    ).generate()

    assert profile.activity_heatmap["stats"]["query_count"] == 4
    assert profile.effect_distribution["高效果"] == 1
    assert profile.health_trend["current_score"] == 82.0
    assert profile.blindspot_distribution == {"outdated": 1, "low_confidence": 1}
    assert profile.frontmatter_completeness == 1.0


def test_quality_score_is_domain_aware_for_version_bound_tech(tmp_path):
    from core.kia.metis import ProfileGenerator

    generator = ProfileGenerator(wiki_base=str(tmp_path), db_path=tmp_path / "profiles.db", trail=FakeTrail({}), immune=FakeImmune())
    tech_score = generator._calculate_quality_score(
        [0.8],
        [{"领域": "技术", "时效性": "版本绑定", "关键词": {"核心概念": ["a"], "场景标签": ["b"], "工具实体": ["c"]}}],
    )
    generic_score = generator._calculate_quality_score(
        [0.8],
        [{"领域": "其他", "时效性": "版本绑定", "关键词": {"核心概念": ["a"], "场景标签": ["b"], "工具实体": ["c"]}}],
    )

    assert tech_score > generic_score
