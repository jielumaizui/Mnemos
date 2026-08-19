def _write_page(path, title="Page"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n", encoding="utf-8")


def test_batch_sync_scans_vault_and_excludes_inbox_shadow_and_reports(tmp_path):
    from core.kia.hecate import ShadowPage, ShadowPageManager

    keep_a = tmp_path / "00-Inbox" / "a.md"
    keep_b = tmp_path / "03-Tech" / "b.md"
    excluded_shadow = tmp_path / "07-Shadow" / "a.shadow.md"
    excluded_report = tmp_path / "99-Reports" / "r.md"
    for page in [keep_a, keep_b, excluded_shadow, excluded_report]:
        _write_page(page)

    manager = ShadowPageManager(wiki_base=str(tmp_path))
    # 预先为 keep_a 写入 shadow；00-Inbox 已被默认排除，不应更新它。
    manager._write_shadow(
        keep_a, ShadowPage(shadow_for=str(keep_a), search_date="2026-05-27", content="old")
    )

    seen = []

    def fake_sync(page):
        seen.append(page)
        shadow = ShadowPage(shadow_for=str(page), search_date="2026-05-27", content="fake shadow")
        manager._write_shadow(page, shadow)
        return shadow

    manager.sync_shadow = fake_sync

    stats = manager.batch_sync()

    assert stats["created"] == 1
    assert stats["updated"] == 0
    assert stats["failed"] == 0
    assert stats["status"] == "ok"
    assert stats["total"] == 1
    assert set(seen) == {keep_b}


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


def test_search_tavily_preserves_published_date(tmp_path, monkeypatch):
    """Tavily 返回发布时间时，SearchResult 和 shadow 渲染应保留该字段。"""
    from dataclasses import asdict

    import core.kia.hecate as hecate
    from core.kia.hecate import ShadowPage, ShadowPageManager

    def fake_run(cmd, **kwargs):
        class FakeResult:
            returncode = 0
            stdout = (
                '{"results": [{"title": "Release notes", '
                '"url": "https://example.com/release", '
                '"content": "New version details", '
                '"published_date": "2026-05-27", '
                '"score": 0.9}]}'
            )
            stderr = ""

        return FakeResult()

    monkeypatch.setattr(hecate.subprocess, "run", fake_run)
    monkeypatch.setattr(hecate.shutil, "which", lambda name: "/usr/bin/tvly")

    page = tmp_path / "note.md"
    _write_page(page, "Note")
    manager = ShadowPageManager(wiki_base=str(tmp_path))

    results = manager.search_tavily("release notes")
    assert asdict(results[0])["published_date"] == "2026-05-27"

    shadow = ShadowPage(
        shadow_for=str(page),
        search_date="2026-05-28",
        sources=results,
    )
    rendered = manager._generate_markdown(page, shadow, {"news": results})

    assert "2026-05-27" in rendered
    assert "source_count: 1" in rendered
    assert "evidence_level: single" in rendered
    assert "https://example.com/release" in rendered.split("---", 2)[1]


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


def test_premise_change_report_shows_old_and_new_status(tmp_path):
    """前提变化报告应展示 old_status -> new_status。"""
    from dataclasses import asdict

    from core.kia.hecate import DecisionDependency, PremiseChange, PremiseValidator

    validator = PremiseValidator(wiki_base=tmp_path)
    change = PremiseChange(
        dependency=DecisionDependency(
            dep_type="library_feature",
            raw_text="redis-py-cluster 库不支持 SSL",
            entity="redis-py-cluster",
            condition="SSL",
            supported_at_decision=False,
        ),
        old_status=False,
        new_status=True,
        evidence="https://example.com/changelog",
        confidence=0.85,
    )

    report = validator.format_change_report([change])

    assert "redis-py-cluster" in report
    assert "状态变化: 不支持 -> 支持" in report
    assert "置信度: 0.85" in report
    assert "https://example.com/changelog" in report
    serialized = asdict(change)
    assert serialized["dependency"]["entity"] == "redis-py-cluster"
    assert serialized["dependency"]["condition"] == "SSL"


def test_batch_sync_reports_error_when_most_pages_fail(tmp_path):
    """当失败率超过 50% 时，batch_sync 应返回 status=error。"""
    from core.kia.hecate import ShadowPageManager

    pages = [tmp_path / f"p{i}.md" for i in range(4)]
    for p in pages:
        _write_page(p)

    manager = ShadowPageManager(wiki_base=str(tmp_path))
    manager.sync_shadow = lambda page: None  # 全部失败

    stats = manager.batch_sync(max_workers=2)
    assert stats["failed"] == 4
    assert stats["status"] == "error"


def test_shadow_filename_includes_path_hash_to_avoid_collision(tmp_path):
    """不同目录下同名文件生成的 shadow 文件不应互相覆盖。"""
    from core.kia.hecate import ShadowPageManager, ShadowPage

    a = tmp_path / "dir_a" / "note.md"
    b = tmp_path / "dir_b" / "note.md"
    _write_page(a, "A")
    _write_page(b, "B")

    manager = ShadowPageManager(wiki_base=str(tmp_path))

    def fake_sync(page):
        shadow = ShadowPage(
            shadow_for=str(page),
            search_date="2026-05-27",
            content=f"shadow for {page.parent.name}",
        )
        manager._write_shadow(page, shadow)
        return shadow

    manager.sync_shadow = fake_sync
    manager.batch_sync()
    shadows = manager.list_shadows()
    assert len(shadows) == 2
    assert all(path.name.startswith("shadow-") for path in shadows)
    assert all("note_" not in path.name for path in shadows)


def test_search_all_uses_local_kg_when_no_external_results(tmp_path, monkeypatch):
    """当 Tavily 和 fallback 都不可用时，search_all 应启用本地 KG/Wiki 降级搜索。"""
    from core.kia.hecate import ShadowPageManager
    import core.kia.hecate as hecate

    manager = ShadowPageManager(wiki_base=str(tmp_path), fallback_search=None)
    monkeypatch.setattr(hecate.shutil, "which", lambda name: None)

    # 禁用本地 KG 初始化以避免依赖真实数据库，仅验证调用链
    manager._kg = object()  # 非 None 但不可用，_local_kg_search 会捕获异常

    page = tmp_path / "note.md"
    page.write_text("# Note\n[[Related Page]] content", encoding="utf-8")
    related = tmp_path / "Related Page.md"
    related.write_text("# Related Page\nbody", encoding="utf-8")

    results = manager.search_all(["test query"], page_path=page)

    # 至少应返回本地 Wiki 链接结果
    assert any(r.source == "local_wiki" for r in results)
    assert any("Related Page" in r.title for r in results)


def test_create_shadow_uses_local_fallback_when_tvly_missing(tmp_path, monkeypatch):
    """tvly 不可用时，create_shadow 应使用本地降级生成 shadow 页面。"""
    from core.kia.hecate import ShadowPageManager
    import core.kia.hecate as hecate

    manager = ShadowPageManager(wiki_base=str(tmp_path), fallback_search=None)
    monkeypatch.setattr(hecate.shutil, "which", lambda name: None)
    manager._kg = object()  # 禁用真实 KG

    page = tmp_path / "note.md"
    page.write_text("# Note\n[[Related Page]] content", encoding="utf-8")
    related = tmp_path / "Related Page.md"
    related.write_text("# Related Page\nbody", encoding="utf-8")

    shadow = manager.create_shadow(page)

    assert shadow is not None
    assert "local_wiki" in shadow.content


# ---------------------------------------------------------------------------
# S22: Tavily subprocess 参数注入防护
# ---------------------------------------------------------------------------


def test_tavily_cmd_places_dashdash_before_query(tmp_path, monkeypatch):
    """query 必须位于 `--` 之后，防止以 `-` 开头的 query 被 tvly 解析为选项。

    合规命令形如: ["tvly", "search", "--max-results", "5", "--json", "--", query]
    """
    import core.kia.hecate as hecate
    from core.kia.hecate import ShadowPageManager

    captured_cmd: list = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)

        class FakeResult:
            returncode = 0
            stdout = '{"results": []}'
            stderr = ""
        return FakeResult()

    monkeypatch.setattr(hecate.subprocess, "run", fake_run)
    monkeypatch.setattr(hecate.shutil, "which", lambda name: "/usr/bin/tvly")

    manager = ShadowPageManager(wiki_base=str(tmp_path))
    manager.search_tavily("--inject-option", max_results=3)

    # `--` must appear before the query in the command list
    assert "--" in captured_cmd, "cmd must contain '--' end-of-options separator"
    dashdash_idx = captured_cmd.index("--")
    query_idx = captured_cmd.index("--inject-option")
    assert query_idx > dashdash_idx, (
        f"query at index {query_idx} must come AFTER '--' at index {dashdash_idx}"
    )


def test_tavily_cmd_max_results_is_clamped(tmp_path, monkeypatch):
    """max_results 应限制在合理范围内（1-20），防止异常大值。"""
    import core.kia.hecate as hecate
    from core.kia.hecate import ShadowPageManager

    captured_cmd: list = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)

        class FakeResult:
            returncode = 0
            stdout = '{"results": []}'
            stderr = ""
        return FakeResult()

    monkeypatch.setattr(hecate.subprocess, "run", fake_run)
    monkeypatch.setattr(hecate.shutil, "which", lambda name: "/usr/bin/tvly")

    manager = ShadowPageManager(wiki_base=str(tmp_path))
    manager.search_tavily("normal query", max_results=9999)

    idx = captured_cmd.index("--max-results")
    actual_max = int(captured_cmd[idx + 1])
    assert actual_max <= 20, f"max_results should be clamped to ≤20, got {actual_max}"
