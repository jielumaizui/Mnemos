from pathlib import Path


def _write_page(path, title="Page"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n", encoding="utf-8")


def test_batch_sync_scans_vault_and_excludes_shadow_and_reports(tmp_path):
    from core.kia.hecate import ShadowPage, ShadowPageManager

    keep_a = tmp_path / "00-Inbox" / "a.md"
    keep_b = tmp_path / "03-Tech" / "b.md"
    excluded_shadow = tmp_path / "07-Shadow" / "a.shadow.md"
    excluded_report = tmp_path / "99-Reports" / "r.md"
    for page in [keep_a, keep_b, excluded_shadow, excluded_report]:
        _write_page(page)

    manager = ShadowPageManager(wiki_base=str(tmp_path))
    seen = []

    def fake_sync(page):
        seen.append(page)
        return ShadowPage(shadow_for=str(page), search_date="2026-05-27")

    manager.sync_shadow = fake_sync

    stats = manager.batch_sync()

    assert stats == {"created": 1, "updated": 1, "failed": 0}
    assert set(seen) == {keep_a, keep_b}


def test_search_tavily_fallback_when_tvly_missing(tmp_path, monkeypatch):
    """Tavily CLI 不可用时自动调用 fallback_search"""
    from core.kia.hecate import ShadowPageManager, SearchResult
    import core.kia.hecate as hecate

    fallback_called = []

    def fake_fallback(query, max_results):
        fallback_called.append((query, max_results))
        return [SearchResult(title="fallback result", url="https://example.com", source="fallback")]

    manager = ShadowPageManager(wiki_base=str(tmp_path), fallback_search=fake_fallback)
    monkeypatch.setattr(hecate.shutil, "which", lambda name: None)

    results = manager.search_tavily("test query", max_results=3)

    assert len(results) == 1
    assert results[0].title == "fallback result"
    assert fallback_called == [("test query", 3)]


def test_search_tavily_returns_empty_when_no_fallback_and_no_tvly(tmp_path, monkeypatch):
    """Tavily 不可用且没有 fallback 时返回空列表"""
    from core.kia.hecate import ShadowPageManager
    import core.kia.hecate as hecate

    manager = ShadowPageManager(wiki_base=str(tmp_path), fallback_search=None)
    monkeypatch.setattr(hecate.shutil, "which", lambda name: None)

    results = manager.search_tavily("test query")
    assert results == []


def test_extract_frontmatter_gracefully_handles_missing_yaml(monkeypatch):
    import core.kia.hecate as hecate

    monkeypatch.setattr(hecate, "yaml", None)

    assert hecate.ShadowPageManager._extract_frontmatter("---\na: 1\n---\nbody") == {}


def test_extract_dependencies_library_feature(tmp_path):
    """从页面内容中提取库特性依赖"""
    from core.kia.hecate import PremiseValidator

    validator = PremiseValidator(wiki_base=tmp_path)
    content = "因为 redis-py-cluster 库不支持 SSL 连接，所以未采用该方案。"
    deps = validator.extract_dependencies(content)
    assert len(deps) >= 1
    assert deps[0].dep_type == "library_feature"
    assert deps[0].entity == "redis-py-cluster"
    assert "不支持" in deps[0].raw_text


def test_validate_premises_no_changes(tmp_path):
    """验证无变化时返回空列表"""
    from core.kia.hecate import PremiseValidator

    page = tmp_path / "test.md"
    page.write_text("# Test\n普通内容，无决策依赖。\n", encoding="utf-8")

    validator = PremiseValidator(wiki_base=tmp_path)
    changes = validator.validate_premises(str(page))
    assert changes == []


def test_premise_validator_skips_missing_page(tmp_path):
    """跳过不存在的页面"""
    from core.kia.hecate import PremiseValidator

    validator = PremiseValidator(wiki_base=tmp_path)
    changes = validator.validate_premises("not-exist.md")
    assert changes == []
