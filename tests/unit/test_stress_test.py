import sqlite3


def _write_page(path, frontmatter="", body="# Title\n正文内容\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")


def test_unknown_type_uses_default_challenge_templates(
    tmp_path,
):
    from core.kia.stress_test import StressTestEngine

    page = tmp_path / "03-Tech" / "unknown.md"
    _write_page(page, "类型: 新类型\n置信度: 0.7\n")

    result = StressTestEngine(
        wiki_base=str(tmp_path),
        db_path=tmp_path / "stress.db",
    ).test_page(page)

    assert len(result.challenges) >= 2
    assert {c.challenge_type for c in result.challenges} >= {"boundary", "temporal"}


def test_boundary_extraction_supports_frontmatter_heading_and_inline(tmp_path):
    from core.kia.stress_test import StressTestEngine

    engine = StressTestEngine(wiki_base=str(tmp_path), db_path=tmp_path / "stress.db")

    fm_boundaries = engine._extract_boundaries(
        "",
        {
            "适用条件": ["小团队", "低并发"],
            "不适用场景": "强实时链路",
        },
    )
    assert fm_boundaries["applies"] == "小团队；低并发"
    assert fm_boundaries["not_applies"] == "强实时链路"

    heading_boundaries = engine._extract_boundaries(
        "## 适用边界\n只适用于离线批处理\n## 不适用场景\n不适用于金融交易\n",
        {},
    )
    assert "离线批处理" in heading_boundaries["applies"]
    assert "金融交易" in heading_boundaries["not_applies"]

    inline_boundaries = engine._extract_boundaries(
        "**适用:** 原型验证\n**不适用:** 生产关键路径\n",
        {},
    )
    assert inline_boundaries["applies"] == "原型验证"
    assert inline_boundaries["not_applies"] == "生产关键路径"


def test_anti_pattern_extraction_supports_frontmatter_and_sections(tmp_path):
    from core.kia.stress_test import StressTestEngine

    engine = StressTestEngine(wiki_base=str(tmp_path), db_path=tmp_path / "stress.db")

    from_fm = engine._extract_anti_patterns("", {"反模式": ["无测试上线", "只靠口头约定"]})
    assert from_fm == ["无测试上线", "只靠口头约定"]

    from_section = engine._extract_anti_patterns(
        "## 常见错误\n- 忽略回滚方案\n1. 只验证 happy path\n## 其他\n正文\n",
        {},
    )
    assert from_section == ["忽略回滚方案", "只验证 happy path"]


def test_batch_test_scans_vault_and_excludes_reports(
    tmp_path,
):
    from core.kia.stress_test import StressTestEngine

    keep_inbox = tmp_path / "00-Inbox" / "a.md"
    keep_nested = tmp_path / "03-Tech" / "b.md"
    excluded = tmp_path / "99-Reports" / "r.md"
    for page in [keep_inbox, keep_nested, excluded]:
        _write_page(page, "类型: 新类型\n置信度: 0.7\n")

    results = StressTestEngine(
        wiki_base=str(tmp_path),
        db_path=tmp_path / "stress.db",
    ).batch_test()

    assert {result.page_path for result in results} == {str(keep_inbox), str(keep_nested)}


def test_page_result_updates_frontmatter(
    tmp_path,
):
    from core.kia.stress_test import StressTestEngine

    page = tmp_path / "03-Tech" / "method.md"
    _write_page(page, "类型: 方法论\n置信度: 0.9\n", "## 适用边界\n小团队\n")

    result = StressTestEngine(
        wiki_base=str(tmp_path),
        db_path=tmp_path / "stress.db",
    ).test_page(page)
    content = page.read_text(encoding="utf-8")

    assert "韧性评分:" in content
    assert "上次压力测试:" in content
    assert "盲区清单:" in content
    assert "通过挑战数:" in content
    assert "失败挑战数:" in content
    assert str(result.resilience_score) in content
    assert str(result.passed_challenges) in content
    assert str(result.failed_challenges) in content


def test_two_consecutive_low_scores_mark_reinforcement(
    tmp_path,
):
    from core.kia.stress_test import StressTestEngine

    page = tmp_path / "03-Tech" / "weak.md"
    _write_page(page, "类型: 问题-解决\n置信度: 0.3\n证据级别: 单源\n")
    db_path = tmp_path / "stress.db"
    engine = StressTestEngine(wiki_base=str(tmp_path), db_path=db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO stress_test_results
               (
                   page_path, page_title, resilience_score,
                   challenges_count, blind_spots_count, created_at
               )
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(page), "weak", 3.2, 3, 2, "2026-01-01T00:00:00+00:00"),
        )

    result = engine.test_page(page)
    content = page.read_text(encoding="utf-8")

    assert result.resilience_score < 4.0
    assert "需加固: true" in content


def test_dry_run_does_not_write_db_or_frontmatter(tmp_path):
    """dry_run=True 时不写入 stress_test_results，也不修改页面 frontmatter。"""
    from core.kia.stress_test import StressTestEngine

    page = tmp_path / "03-Tech" / "method.md"
    _write_page(page, "类型: 方法论\n置信度: 0.9\n", "## 适用边界\n小团队\n")
    db_path = tmp_path / "stress.db"
    original_content = page.read_text(encoding="utf-8")

    result = StressTestEngine(
        wiki_base=str(tmp_path),
        db_path=db_path,
        dry_run=True,
    ).test_page(page)

    assert result.challenges
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute("SELECT COUNT(*) FROM stress_test_results").fetchone()
        assert row[0] == 0
    assert page.read_text(encoding="utf-8") == original_content


def test_stress_test_page_helper_accepts_wiki_relative_dry_run(tmp_path):
    """stress_test_page helper 应支持 wiki 相对路径并透传 dry_run。"""
    from core.kia.stress_test import stress_test_page

    page = tmp_path / "03-Tech" / "method.md"
    _write_page(page, "类型: 方法论\n置信度: 0.9\n", "## 适用边界\n小团队\n")
    original_content = page.read_text(encoding="utf-8")

    report = stress_test_page("03-Tech/method.md", wiki_base=str(tmp_path), dry_run=True)

    assert "# 压力测试报告: method" in report
    assert "挑战数量:" in report
    assert page.read_text(encoding="utf-8") == original_content


def test_existing_result_table_gets_challenge_outcome_columns(tmp_path):
    """已有 stress_test_results 旧表会自动补齐挑战结果列。"""
    from core.kia.stress_test import StressTestEngine

    db_path = tmp_path / "stress.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """CREATE TABLE stress_test_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_path TEXT NOT NULL,
                page_title TEXT,
                resilience_score REAL,
                challenges_count INTEGER,
                blind_spots_count INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )

    StressTestEngine(wiki_base=str(tmp_path), db_path=db_path)

    with sqlite3.connect(str(db_path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(stress_test_results)")}

    assert {"passed_challenges", "failed_challenges"} <= columns


def test_save_result_persists_challenges(tmp_path):
    """save_result 应同时写入 stress_test_challenges 表。"""
    from core.kia.stress_test import StressTestEngine, StressTestResult, Challenge

    db_path = tmp_path / "stress.db"
    engine = StressTestEngine(wiki_base=str(tmp_path), db_path=db_path)
    result = StressTestResult(
        page_path=str(tmp_path / "p.md"),
        page_title="p",
        resilience_score=6.0,
        challenges=[Challenge(challenge_type="boundary", question="q1", risk_level="high")],
        passed_challenges=2,
        failed_challenges=1,
        blind_spots=["bs1"],
    )
    engine.save_result(result)

    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute("SELECT question FROM stress_test_challenges").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "q1"
        result_row = conn.execute(
            "SELECT passed_challenges, failed_challenges FROM stress_test_results"
        ).fetchone()
        assert result_row == (2, 1)

    report = engine.generate_report(result)
    assert "挑战通过: 2" in report
    assert "挑战失败: 1" in report


def test_emit_event_publishes_to_bus(
    tmp_path,
    monkeypatch,
):
    """低韧性评分会触发 knowledge_needs_reinforcement 事件。"""
    from core.kia.stress_test import StressTestEngine
    import core.mnemos_bus as bus

    published = []
    monkeypatch.setattr(
        bus,
        "publish_event",
        lambda event_type, agent, payload: published.append((event_type, payload)) or "trace",
    )

    page = tmp_path / "03-Tech" / "weak.md"
    _write_page(page, "类型: 问题-解决\n置信度: 0.3\n证据级别: 单源\n")
    db_path = tmp_path / "stress.db"
    StressTestEngine(wiki_base=str(tmp_path), db_path=db_path).test_page(page)

    assert any(e[0] == "knowledge_needs_reinforcement" for e in published)


def test_reinforcement_event_guard_prevents_re_emit_during_handling(tmp_path, monkeypatch):
    """同一页面处于 knowledge_needs_reinforcement 处理中时，不应再次发布该事件。"""
    from core.kia.stress_test import StressTestEngine, StressTestResult
    import core.mnemos_bus as bus

    published = []
    monkeypatch.setattr(
        bus,
        "publish_event",
        lambda event_type, agent, payload: published.append(event_type) or "trace",
    )

    engine = StressTestEngine(wiki_base=str(tmp_path), db_path=tmp_path / "stress.db")
    page_path = tmp_path / "page.md"
    result = StressTestResult(
        page_path=str(page_path),
        resilience_score=3.0,
        blind_spots=["bs"],
    )

    engine._reinforcement_in_flight.add(str(page_path))
    engine._emit_stress_events(page_path, result)

    assert "knowledge_needs_reinforcement" not in published
