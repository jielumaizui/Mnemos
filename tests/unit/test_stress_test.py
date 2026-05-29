import sqlite3


def _write_page(path, frontmatter="", body="# Title\n正文内容\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")


def test_unknown_type_uses_default_challenge_templates(tmp_path):
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

    fm_boundaries = engine._extract_boundaries("", {
        "适用条件": ["小团队", "低并发"],
        "不适用场景": "强实时链路",
    })
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


def test_batch_test_scans_vault_and_excludes_reports(tmp_path):
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


def test_page_result_updates_frontmatter(tmp_path):
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
    assert str(result.resilience_score) in content


def test_two_consecutive_low_scores_mark_reinforcement(tmp_path):
    from core.kia.stress_test import StressTestEngine

    page = tmp_path / "03-Tech" / "weak.md"
    _write_page(page, "类型: 问题-解决\n置信度: 0.3\n证据级别: 单源\n")
    db_path = tmp_path / "stress.db"
    engine = StressTestEngine(wiki_base=str(tmp_path), db_path=db_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO stress_test_results
               (page_path, page_title, resilience_score, challenges_count, blind_spots_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(page), "weak", 3.2, 3, 2, "2026-01-01T00:00:00+00:00"),
        )

    result = engine.test_page(page)
    content = page.read_text(encoding="utf-8")

    assert result.resilience_score < 4.0
    assert "需加固: true" in content
