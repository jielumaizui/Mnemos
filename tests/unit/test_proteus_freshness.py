from datetime import datetime, timedelta


def test_freshness_checker_detects_newer_version():
    from core.kia.proteus import KnowledgeFreshnessChecker

    alert = KnowledgeFreshnessChecker().check(
        {
            "frontmatter": {
                "temporal_scope": "version-bound",
                "version_info": "1.0",
                "latest_version": "2.0",
            }
        }
    )

    assert alert.type == "newer_version"
    assert alert.severity == "high"


def test_freshness_checker_supports_chinese_frontmatter_and_stale_context():
    from core.kia.proteus import KnowledgeFreshnessChecker

    old = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
    alert = KnowledgeFreshnessChecker().check(
        {
            "frontmatter": {
                "时效性": "上下文相关",
                "修改日期": old,
            }
        }
    )

    assert alert.type == "potentially_stale"
    assert "120" in alert.message


def test_freshness_checker_ignores_timeless_pages():
    from core.kia.proteus import KnowledgeFreshnessChecker

    assert (
        KnowledgeFreshnessChecker().check({"frontmatter": {"temporal_scope": "timeless"}}) is None
    )


def test_iteration_tracker_relaxed_quality_gate_constants():
    from core.kia.proteus import IterationTracker

    assert IterationTracker.MIN_CHECKLIST_DELTA_RATIO == 0.1
    assert IterationTracker.MAX_VERSIONS_PER_DAY == 5


def test_knowledge_evolution_report_renders_session_count():
    from core.kia.proteus import KnowledgeEvolutionReport, VersionSnapshot

    report = KnowledgeEvolutionReport(
        topic="coding",
        session_count=3,
        versions=[
            VersionSnapshot(
                date_str="2026-01-01",
                summary="初始实践",
            )
        ],
        evolution_path="从初始实践到系统化沉淀",
    )

    markdown = report.to_markdown()

    assert "**会话数**: 3" in markdown


def test_iteration_tracker_records_pages_when_generating_evolution_report(tmp_path):
    from core.kia.proteus import IterationTracker

    (tmp_path / "first.md").write_text(
        """---
task_type: coding
updated_at: 2026-01-01
summary: 初始 Python 代码实践
---
记录 [[Python]] 基础用法。
""",
        encoding="utf-8",
    )
    (tmp_path / "second.md").write_text(
        """---
task_type: coding
updated_at: 2026-02-01
summary: 深化 Python 测试实践
---
补充 [[Python]] 与 `pytest` 的测试实践。
""",
        encoding="utf-8",
    )

    tracker = IterationTracker(tmp_path)

    result = tracker.scan_and_report()

    assert result["status"] == "ok"
    assert result["reports"] == 1
    assert result["topics"] == ["coding"]
    assert tracker.get_stats() == {"total": 2, "pages": 2}
