from datetime import datetime, timedelta


def test_freshness_checker_detects_newer_version():
    from core.kia.proteus import KnowledgeFreshnessChecker

    alert = KnowledgeFreshnessChecker().check({
        "frontmatter": {
            "temporal_scope": "version-bound",
            "version_info": "1.0",
            "latest_version": "2.0",
        }
    })

    assert alert.type == "newer_version"
    assert alert.severity == "high"


def test_freshness_checker_supports_chinese_frontmatter_and_stale_context():
    from core.kia.proteus import KnowledgeFreshnessChecker

    old = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d")
    alert = KnowledgeFreshnessChecker().check({
        "frontmatter": {
            "时效性": "上下文相关",
            "修改日期": old,
        }
    })

    assert alert.type == "potentially_stale"
    assert "120" in alert.message


def test_freshness_checker_ignores_timeless_pages():
    from core.kia.proteus import KnowledgeFreshnessChecker

    assert KnowledgeFreshnessChecker().check({"frontmatter": {"temporal_scope": "timeless"}}) is None


def test_iteration_tracker_relaxed_quality_gate_constants():
    from core.kia.proteus import IterationTracker

    assert IterationTracker.MIN_CHECKLIST_DELTA_RATIO == 0.1
    assert IterationTracker.MAX_VERSIONS_PER_DAY == 5
