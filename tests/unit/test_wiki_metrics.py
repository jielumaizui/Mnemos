from datetime import datetime, timedelta, timezone


def test_decay_uses_category_specific_half_life(tmp_path):
    from core.wiki_metrics import WikiMetrics

    metrics = WikiMetrics(db_path=str(tmp_path / "metrics.db"), wiki_dir=str(tmp_path))
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    tech = "tech.md"
    practice = "practice.md"
    metrics.upsert_page(tech, heat_score=10, heat_level="hot", tags=["category:technology"], last_updated=old)
    metrics.upsert_page(practice, heat_score=10, heat_level="hot", tags=["category:practice"], last_updated=old)

    metrics.decay_all(decay_days=15)

    assert metrics.get_page(tech).heat_score < 10
    assert metrics.get_page(practice).heat_score == 10


def test_sync_heat_to_frontmatter(tmp_path):
    from core.wiki_metrics import WikiMetrics

    page = tmp_path / "page.md"
    page.write_text("---\ntitle: Page\n---\n# Page\n", encoding="utf-8")
    metrics = WikiMetrics(db_path=str(tmp_path / "metrics.db"), wiki_dir=str(tmp_path))
    metrics.upsert_page(str(page), heat_score=12.34, heat_level="hot")

    assert metrics.sync_heat_to_frontmatter(page) is True
    content = page.read_text(encoding="utf-8")

    assert "热度等级: hot" in content
    assert "热度分: 12.3" in content
    assert "统计更新时间:" in content
    assert "heat_level:" not in content
    assert "heat_score:" not in content


def test_generate_heat_report_can_write_file(tmp_path):
    from core.wiki_metrics import WikiMetrics

    metrics = WikiMetrics(db_path=str(tmp_path / "metrics.db"), wiki_dir=str(tmp_path))
    metrics.upsert_page("hot.md", title="Hot", heat_score=9, heat_level="hot")
    metrics.upsert_page("cold.md", title="Cold", heat_score=1, heat_level="cold")

    report = metrics.generate_heat_report(write=True)

    assert "# 热力地图" in report
    assert "**Hot**" in report
    reports = list((tmp_path / "99-Reports").glob("热力地图-*.md"))
    assert len(reports) == 1
    assert "COLD" in reports[0].read_text(encoding="utf-8")
