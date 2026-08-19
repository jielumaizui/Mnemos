from __future__ import annotations

from core.benchmarks.golden import (
    DEFAULT_BASELINE_PATH,
    DEFAULT_MANIFEST_PATH,
    GOLDEN_BENCHMARK_SCHEMA_VERSION,
    audit_golden_benchmark_contract,
    build_golden_benchmark_health,
    load_manifest,
)
from core.system_contracts import (
    ACTION_TYPES,
    CAPABILITY_DEFINITIONS,
    COGNITIVE_ASSET_DEFINITIONS,
    DOMAIN_TERMS,
    LIFECYCLE_MAPPINGS,
    PRIVACY_POLICIES,
    QUALITY_GATE_DEFINITIONS,
    SCORECARD_DIMENSIONS,
)


def test_golden_benchmark_contract_is_strictly_valid() -> None:
    assert audit_golden_benchmark_contract(strict=True) == []


def test_manifest_uses_only_mock_providers_and_required_categories() -> None:
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    assert manifest["schema_version"] == GOLDEN_BENCHMARK_SCHEMA_VERSION
    assert set(manifest["deterministic_providers"].values()) == {"mock"}
    categories = {
        category
        for sample in manifest["samples"]
        for category in sample["categories"]
    }
    assert {
        "raw_conversation",
        "user_document",
        "incident",
        "decision",
        "low_value",
        "conflict_input",
    } <= categories


def test_golden_benchmark_is_wired_into_system_contracts() -> None:
    assert "benchmark_result" in COGNITIVE_ASSET_DEFINITIONS
    assert "benchmark_result" in PRIVACY_POLICIES
    assert "golden_benchmark_quality" in QUALITY_GATE_DEFINITIONS
    assert "golden_benchmark" in CAPABILITY_DEFINITIONS
    assert "golden_benchmark" in LIFECYCLE_MAPPINGS
    assert "golden_benchmark" in DOMAIN_TERMS
    assert "golden_benchmark" in SCORECARD_DIMENSIONS
    assert "benchmark_consumer_verify" in ACTION_TYPES


def test_golden_benchmark_health_reports_fixture_counts() -> None:
    health = build_golden_benchmark_health()
    assert health["status"] == "ok"
    assert health["counts"]["samples"] >= 5
    assert DEFAULT_BASELINE_PATH.exists()
