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
    excluded = tmp_path / ".obsidian" / "old.md"
    _write_page(
        keep_a,
        "领域: 技术\n类型: 问题-解决\n复杂度: 入门\n置信度: 0.9\n时效性: 版本绑定\n创建日期: 2026-01-01\n",
    )
    _write_page(
        keep_b,
        "领域: 产品\n类型: 方法论\n复杂度: 中级\n置信度: 0.7\n时效性: 上下文相关\n创建日期: 2026-02-01\n",
    )
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
        row = conn.execute(
            "SELECT total_knowledge, domain_distribution FROM knowledge_profiles"
        ).fetchone()
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
    _write_page(
        problem,
        "领域: 技术\n类型: 问题-解决\n复杂度: 中级\n置信度: 0.8\n时效性: 稳定\n创建日期: 2026-01-01\n",
        "[[method]]\n",
    )
    _write_page(
        method,
        "领域: 技术\n类型: 方法论\n复杂度: 中级\n置信度: 0.8\n时效性: 稳定\n创建日期: 2026-01-01\n",
    )

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
    _write_page(
        page,
        "领域: 技术\n类型: 问题-解决\n复杂度: 高级\n置信度: 0.9\n时效性: 稳定\n创建日期: 2026-01-01\n",
    )

    profile = ProfileGenerator(
        wiki_base=str(tmp_path),
        db_path=tmp_path / "profiles.db",
        trail=FakeTrail(
            {str(page): {"total_queries": 4, "total_modifications": 2, "effect_score": 0.75}}
        ),
        immune=FakeImmune(),
    ).generate()

    assert profile.activity_heatmap["stats"]["query_count"] == 4
    assert profile.effect_distribution["高效果"] == 1
    assert profile.health_trend["current_score"] == 82.0
    assert profile.blindspot_distribution == {"outdated": 1, "low_confidence": 1}
    assert profile.frontmatter_completeness == 1.0


def test_quality_score_is_domain_aware_for_version_bound_tech(tmp_path):
    from core.kia.metis import ProfileGenerator

    generator = ProfileGenerator(
        wiki_base=str(tmp_path),
        db_path=tmp_path / "profiles.db",
        trail=FakeTrail({}),
        immune=FakeImmune(),
    )
    tech_score = generator._calculate_quality_score(
        [0.8],
        [
            {
                "领域": "技术",
                "时效性": "版本绑定",
                "关键词": {"核心概念": ["a"], "场景标签": ["b"], "工具实体": ["c"]},
            }
        ],
    )
    generic_score = generator._calculate_quality_score(
        [0.8],
        [
            {
                "领域": "其他",
                "时效性": "版本绑定",
                "关键词": {"核心概念": ["a"], "场景标签": ["b"], "工具实体": ["c"]},
            }
        ],
    )

    assert tech_score > generic_score


def test_generate_profile_delegates_to_generator_report(monkeypatch):
    from core.kia import metis

    calls = []

    class FakeProfileGenerator:
        def __init__(self, wiki_base=None):
            calls.append(("init", wiki_base))

        def generate_and_report(self):
            calls.append(("generate_and_report", None))
            return "# profile"

    monkeypatch.setattr(metis, "ProfileGenerator", FakeProfileGenerator)

    assert metis.generate_profile("/tmp/wiki") == "# profile"
    assert calls == [("init", "/tmp/wiki"), ("generate_and_report", None)]


def test_sync_profile_update_applies_created_wiki_page_payload(tmp_path):
    from core.kia.metis import ProfileGenerator, sync_profile_update

    existing = tmp_path / "00-Inbox" / "existing.md"
    created = tmp_path / "00-Inbox" / "created.md"
    _write_page(
        existing,
        "领域: 技术\n类型: 方法论\n复杂度: 中级\n置信度: 0.8\n时效性: 稳定\n创建日期: 2026-01-01\n",
    )
    db_path = tmp_path / "profiles.db"
    ProfileGenerator(
        wiki_base=str(tmp_path),
        db_path=db_path,
        trail=FakeTrail({}),
        immune=FakeImmune(),
    ).generate()
    _write_page(
        created,
        "领域: 产品\n类型: 问题-解决\n复杂度: 入门\n置信度: 0.7\n时效性: 上下文相关\n创建日期: 2026-02-01\n",
    )

    result = sync_profile_update(
        {"wiki_pages": [str(created)]},
        wiki_base=str(tmp_path),
        db_path=db_path,
        operation="create",
    )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT total_knowledge, domain_distribution FROM knowledge_profiles"
        ).fetchone()

    assert result == {"status": "ok", "updated": 1}
    assert row[0] == 2
    assert json.loads(row[1]) == {"技术": 1, "产品": 1}


def test_wiki_page_updated_publishes_durable_mutation_for_profile_consumer(monkeypatch, tmp_path):
    from core.hephaestus.distillation_failure import publish_wiki_page_updated

    events = []

    def fake_publish_event(
        event_type,
        source,
        payload,
        *,
        trace_id="",
        subject_provenance=None,
    ):
        del subject_provenance
        events.append((event_type, source, payload, trace_id))
        return trace_id

    monkeypatch.setattr("core.mnemos_bus.publish_event", fake_publish_event)
    monkeypatch.setattr(
        "core.wiki_projection_lifecycle._default_db_path",
        lambda: tmp_path / "wiki_projection.db",
    )

    page = tmp_path / "00-Inbox" / "created.md"
    page.parent.mkdir(parents=True)
    page.write_text("# created\n", encoding="utf-8")
    receipt = publish_wiki_page_updated(page, update_type="create")

    assert len(events) == 1
    event_type, source, payload, trace_id = events[0]
    assert (event_type, source) == ("wiki_page_updated", "wiki_mutation")
    assert payload["page_path"] == str(page.resolve())
    assert payload["mutation_type"] == "create"
    assert payload["page_id"] == receipt["page_id"]
    assert payload["page_revision"] == receipt["page_revision"]
    assert payload["mutation_id"] == receipt["mutation_id"]
    assert trace_id == receipt["mutation_id"]
    assert receipt["event_trace_id"] == receipt["mutation_id"]
