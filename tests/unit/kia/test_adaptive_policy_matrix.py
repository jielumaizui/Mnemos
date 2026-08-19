from core.kia.adaptive_policy_matrix import (
    REQUIRED_ADAPTIVE_DOMAINS,
    audit_adaptive_policy_coverage,
    build_adaptive_policy_report,
)


def test_adaptive_policy_matrix_is_strictly_valid():
    assert audit_adaptive_policy_coverage(strict=True) == []


def test_adaptive_policy_report_covers_required_domains():
    report = build_adaptive_policy_report()

    assert report["ok"] is True
    assert report["rule_count"] == report["coverage_count"]
    assert REQUIRED_ADAPTIVE_DOMAINS.issubset(set(report["domains"]))
