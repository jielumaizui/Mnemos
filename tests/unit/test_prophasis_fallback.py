from core.kia.kairos import TimeWindow, TimeWindowType


def test_preflight_fallback_outputs_actionable_items_with_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    retro_dir = tmp_path / "06-Retrospectives"
    retro_dir.mkdir(parents=True)
    page = retro_dir / "coding_反模式_1.md"
    page.write_text(
        "---\n"
        "类型: retrospective\n"
        "名称: 数据管道缺陷复盘\n"
        "来源: claude\n"
        "关键词:\n"
        "- 并发安全\n"
        "- SQLite\n"
        "触发器:\n"
        "- 审计数据摄取管道\n"
        "---\n"
        "# 数据管道缺陷复盘\n",
        encoding="utf-8",
    )

    from core.kia.prophasis import PreFlightInjector

    injector = PreFlightInjector(wiki_base=str(tmp_path))
    knowledge = injector.inject(
        "coding",
        "debug",
        TimeWindow(window=TimeWindowType.IMMEDIATE, days_until=0),
        "排查数据摄取管道问题",
    )

    assert knowledge is not None
    assert len(knowledge.checklist) <= 10
    assert knowledge.lessons_summary.startswith("未命中专用复盘文件")
    first = knowledge.checklist[0]
    assert first.item.startswith("复用《数据管道缺陷复盘》：")
    assert first.source == "06-Retrospectives/coding_反模式_1.md"
    assert first.detail == "source_agent=claude"
