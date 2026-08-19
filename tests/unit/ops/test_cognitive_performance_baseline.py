from __future__ import annotations

import copy
import json
from pathlib import Path

from core.ops.cognitive_performance_baseline import (
    DEFAULT_MANIFEST_PATH,
    load_performance_baseline,
    validate_performance_baseline,
)


def _write_manifest(tmp_path: Path, manifest: dict[str, object]) -> Path:
    path = tmp_path / "cognitive_performance_manifest.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def test_phase0_performance_baseline_contract_is_valid() -> None:
    manifest = load_performance_baseline(DEFAULT_MANIFEST_PATH)

    assert validate_performance_baseline(manifest, verify_files=True) == []
    assert manifest["parent_root_id"] == "COG-040"
    assert manifest["work_package"] == "WP-COG-040-P0-BASELINE"
    assert manifest["contract_mode"] == "baseline_only"
    assert manifest["performance_certificate_eligible"] is False
    assert manifest["phase7_root_closed"] is False


def test_dataset_hash_drift_is_blocking(tmp_path: Path) -> None:
    manifest = copy.deepcopy(load_performance_baseline(DEFAULT_MANIFEST_PATH))
    manifest["datasets"][0]["sha256"] = "0" * 64

    errors = validate_performance_baseline(
        load_performance_baseline(_write_manifest(tmp_path, manifest)),
        verify_files=True,
    )

    assert any("dataset hash mismatch" in error for error in errors)


def test_protocol_requires_cold_hot_repetitions_and_noise_budget(
    tmp_path: Path,
) -> None:
    manifest = copy.deepcopy(load_performance_baseline(DEFAULT_MANIFEST_PATH))
    del manifest["measurement_protocol"]["cold"]
    manifest["measurement_protocol"]["hot"]["repetitions"] = 0
    del manifest["measurement_protocol"]["repeatability"]["max_cv"]

    errors = validate_performance_baseline(
        load_performance_baseline(_write_manifest(tmp_path, manifest)),
        verify_files=False,
    )

    assert any("cold protocol" in error for error in errors)
    assert any("hot repetitions" in error for error in errors)
    assert any("max_cv" in error for error in errors)


def test_baseline_cannot_claim_phase7_certificate_or_hide_missing_denominators(
    tmp_path: Path,
) -> None:
    manifest = copy.deepcopy(load_performance_baseline(DEFAULT_MANIFEST_PATH))
    manifest["performance_certificate_eligible"] = True
    manifest["phase7_root_closed"] = True
    manifest["denominators"]["recall"]["status"] = "passed"
    manifest["denominators"]["recall"]["observed"] = 0

    errors = validate_performance_baseline(
        load_performance_baseline(_write_manifest(tmp_path, manifest)),
        verify_files=False,
    )

    assert any("must not issue a PerformanceCertificate" in error for error in errors)
    assert any("must not close COG-040" in error for error in errors)
    assert any("recall denominator cannot pass with zero observations" in error for error in errors)


def test_function_recall_acl_effect_denominators_are_explicit() -> None:
    manifest = load_performance_baseline(DEFAULT_MANIFEST_PATH)
    denominators = manifest["denominators"]

    assert set(denominators) == {"functional", "recall", "acl", "effect"}
    assert denominators["functional"]["observed"] == 9
    for name in ("recall", "acl", "effect"):
        assert denominators[name]["observed"] == 0
        assert denominators[name]["status"] == "missing_phase7_evidence"
        assert denominators[name]["release_blocking"] is True


def test_zero_recall_acl_effect_denominators_cannot_pass(tmp_path: Path) -> None:
    baseline = load_performance_baseline(DEFAULT_MANIFEST_PATH)

    for name in ("recall", "acl", "effect"):
        manifest = copy.deepcopy(baseline)
        manifest["denominators"][name]["status"] = "passed"
        manifest["denominators"][name]["release_blocking"] = False

        errors = validate_performance_baseline(
            load_performance_baseline(_write_manifest(tmp_path, manifest)),
            verify_files=False,
        )

        assert f"{name} denominator cannot pass with zero observations" in errors
        assert f"{name} zero denominator must remain missing_phase7_evidence" in errors
        assert f"{name} zero denominator must block release" in errors


def test_hardware_fingerprint_is_bound_to_declared_fields(tmp_path: Path) -> None:
    manifest = copy.deepcopy(load_performance_baseline(DEFAULT_MANIFEST_PATH))
    manifest["hardware"]["cpu"] = "different"

    errors = validate_performance_baseline(
        load_performance_baseline(_write_manifest(tmp_path, manifest)),
        verify_files=False,
    )

    assert any("hardware fingerprint mismatch" in error for error in errors)


def test_configuration_and_functional_denominator_are_hash_bound(
    tmp_path: Path,
) -> None:
    manifest = copy.deepcopy(load_performance_baseline(DEFAULT_MANIFEST_PATH))
    manifest["configuration"]["network"] = "enabled"
    manifest["denominators"]["functional"]["node_ids"].pop()

    errors = validate_performance_baseline(
        load_performance_baseline(_write_manifest(tmp_path, manifest)),
        verify_files=False,
    )

    assert any("configuration fingerprint mismatch" in error for error in errors)
    assert any(
        "functional denominator observed must equal exact node_ids" in error for error in errors
    )


def test_frozen_protocol_and_workload_mutations_are_blocking(tmp_path: Path) -> None:
    manifest = copy.deepcopy(load_performance_baseline(DEFAULT_MANIFEST_PATH))
    manifest["measurement_protocol"]["hot"]["warmup_repetitions"] = 0
    manifest["measurement_protocol"]["repeatability"]["independent_runs"] = 0
    manifest["measurement_protocol"]["repeatability"]["p95_relative_regression_budget"] = 0.25
    manifest["workloads"][0]["absolute_budget"]["p95_ms_lt"] = 1_000_000.0
    manifest["workloads"][0]["baseline_observation"]["certifying"] = True

    errors = validate_performance_baseline(
        load_performance_baseline(_write_manifest(tmp_path, manifest)),
        verify_files=False,
    )

    assert any("warmup_repetitions" in error for error in errors)
    assert any("independent_runs" in error for error in errors)
    assert any("must remain 10%" in error for error in errors)
    assert any("absolute_budget drifted" in error for error in errors)
    assert any("must be non-certifying" in error for error in errors)


def test_fake_nodes_and_shrunk_denominators_are_blocking(tmp_path: Path) -> None:
    manifest = copy.deepcopy(load_performance_baseline(DEFAULT_MANIFEST_PATH))
    manifest["denominators"]["functional"]["required"] = 0
    manifest["denominators"]["functional"]["node_ids"][
        0
    ] = "tests/benchmark/not_a_real_test.py::test_fake"
    manifest["denominators"]["recall"]["observed"] = 1
    manifest["denominators"]["recall"]["status"] = "passed"
    manifest["denominators"]["recall"]["release_blocking"] = False

    errors = validate_performance_baseline(
        load_performance_baseline(_write_manifest(tmp_path, manifest)),
        verify_files=True,
    )

    assert any("must remain exact 9/9" in error for error in errors)
    assert any("do not match frozen population" in error for error in errors)
    assert any("recall Phase 0 observed denominator must remain zero" in error for error in errors)


def test_invalid_numeric_protocol_values_return_errors_not_exceptions(
    tmp_path: Path,
) -> None:
    manifest = copy.deepcopy(load_performance_baseline(DEFAULT_MANIFEST_PATH))
    manifest["measurement_protocol"]["cold"]["fresh_process_repetitions"] = "bad"

    errors = validate_performance_baseline(
        load_performance_baseline(_write_manifest(tmp_path, manifest)),
        verify_files=False,
    )

    assert any("cold protocol" in error for error in errors)
