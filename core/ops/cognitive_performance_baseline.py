"""Phase 0 contract validation for the COG-040 performance baseline.

This module validates a frozen, hermetic baseline contract.  It deliberately
does not run performance workloads or issue a Phase 7 PerformanceCertificate.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from core.file_ops import sha256_file
from core.utils import load_json_value, read_text_value

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST_PATH = PROJECT_ROOT / "docs" / "acceptance" / "cognitive_performance_manifest.json"
SCHEMA_VERSION = "mnemos.cognitive_performance_baseline.v1"
EXPECTED_SOURCE_COMMIT = "848b6795c67b48f9e69c64cf55e09145bf3f8bd4"
EXPECTED_CONTRACT_FINGERPRINT = "c5aa99b6a94d1a6fd7e2bf786e5e4c49a6d23c9c538c845ffab8ca82441ae134"
_DENOMINATORS = {"functional", "recall", "acl", "effect"}
_HEX_DIGITS = frozenset("0123456789abcdef")
_REPEATABILITY_REPORT = {
    "median",
    "p95",
    "p99",
    "mean",
    "standard_deviation",
    "coefficient_of_variation",
}
_EXPECTED_FUNCTIONAL_NODE_IDS = {
    "tests/benchmark/test_benchmark_sync.py::test_sync_session_smoke_latency",
    "tests/benchmark/test_benchmark_sync.py::test_sync_batch_smoke_throughput",
    (
        "tests/benchmark/test_feedback_attribution_capacity.py::"
        "test_real_replay_converges_for_reaction_proposal_and_compensation_capacity"
    ),
    (
        "tests/benchmark/test_smoke_bench.py::TestSyncEngineSmoke::"
        "test_sync_engine_symbols_imported"
    ),
    (
        "tests/benchmark/test_smoke_bench.py::TestSyncEngineSmoke::"
        "test_sanitize_content_performance"
    ),
    (
        "tests/benchmark/test_smoke_bench.py::TestKnowledgeGraphSmoke::"
        "test_knowledge_graph_symbols_imported"
    ),
    (
        "tests/benchmark/test_smoke_bench.py::TestKnowledgeGraphSmoke::"
        "test_knowledge_graph_schema_defined"
    ),
    (
        "tests/benchmark/test_smoke_bench.py::TestContextSearchSmoke::"
        "test_context_search_symbols_imported"
    ),
    (
        "tests/benchmark/test_smoke_bench.py::TestContextSearchSmoke::"
        "test_search_result_dataclass_performance"
    ),
}
_EXPECTED_WORKLOADS = {
    "sync_session_5_turn_hot": {
        "dataset_path": "tests/benchmark/test_benchmark_sync.py",
        "shape": {"sessions": 1, "turns_per_session": 5, "measured_repetitions": 12},
        "absolute_budget": {"p95_ms_lt": 500.0},
    },
    "sync_batch_10x3_hot": {
        "dataset_path": "tests/benchmark/test_benchmark_sync.py",
        "shape": {"sessions": 10, "turns_per_session": 3, "measured_repetitions": 1},
        "absolute_budget": {"elapsed_ms_lt": 3000.0},
    },
    "feedback_attribution_bounded_capacity": {
        "dataset_path": "tests/benchmark/test_feedback_attribution_capacity.py",
        "shape": {
            "source_reactions": 1501,
            "proposal_commands_gt": 10000,
            "compensation_commands_gt": 10000,
        },
        "absolute_budget": {"wall_clock": "not_a_phase0_oracle"},
    },
}


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value).issubset(_HEX_DIGITS)


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _positive_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) > 0


def _contract_identity(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: manifest.get(key)
        for key in (
            "source_commit",
            "hardware",
            "configuration",
            "runner_selector",
            "measurement_protocol",
            "datasets",
            "workloads",
            "denominators",
            "missing_phase7_evidence",
        )
    }


def _node_id_exists(project_root: Path, node_id: str) -> bool:
    parts = node_id.split("::")
    path = project_root / parts[0]
    if len(parts) < 2 or not path.is_file():
        return False
    try:
        body = ast.parse(read_text_value(path)).body
    except (OSError, SyntaxError, UnicodeError):
        return False
    current = body
    for name in parts[1:]:
        match = next(
            (
                node
                for node in current
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name
            ),
            None,
        )
        if match is None:
            return False
        current = match.body if isinstance(match, ast.ClassDef) else []
    return True


def load_performance_baseline(
    path: Path = DEFAULT_MANIFEST_PATH,
) -> dict[str, Any]:
    """Load the frozen COG-040 performance baseline manifest."""

    payload = load_json_value(path)
    if not isinstance(payload, dict):
        raise ValueError("performance baseline manifest must be a JSON object")
    return payload


def validate_performance_baseline(
    manifest: Mapping[str, Any],
    *,
    verify_files: bool,
    project_root: Path = PROJECT_ROOT,
) -> list[str]:
    """Return fail-closed contract errors without executing a benchmark."""

    errors: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported performance baseline schema_version")
    if manifest.get("parent_root_id") != "COG-040":
        errors.append("performance baseline parent_root_id must be COG-040")
    if manifest.get("work_package") != "WP-COG-040-P0-BASELINE":
        errors.append("performance baseline work_package is not canonical")
    if manifest.get("contract_mode") != "baseline_only":
        errors.append("Phase 0 performance contract must remain baseline_only")
    if manifest.get("source_commit") != EXPECTED_SOURCE_COMMIT:
        errors.append("performance baseline source_commit is not the frozen commit")
    if manifest.get("performance_certificate_eligible") is not False:
        errors.append("Phase 0 baseline must not issue a PerformanceCertificate")
    if manifest.get("phase7_root_closed") is not False:
        errors.append("Phase 0 baseline must not close COG-040")

    hardware = manifest.get("hardware")
    if not isinstance(hardware, dict):
        errors.append("hardware fingerprint fields are required")
    else:
        identity = {
            key: hardware.get(key) for key in ("architecture", "cpu", "os", "os_version", "python")
        }
        if any(not isinstance(value, str) or not value for value in identity.values()):
            errors.append("hardware fingerprint fields must be non-empty strings")
        if hardware.get("fingerprint_sha256") != _canonical_hash(identity):
            errors.append("hardware fingerprint mismatch")

    configuration = manifest.get("configuration")
    if not isinstance(configuration, dict):
        errors.append("configuration fingerprint fields are required")
    else:
        identity = {
            key: configuration.get(key)
            for key in (
                "api_credentials",
                "execution_mode",
                "formal_state",
                "network",
                "pytest_plugin_autoload",
                "pytest_benchmark_required",
                "storage_backend",
                "timing_clock",
            )
        }
        if configuration.get("fingerprint_sha256") != _canonical_hash(identity):
            errors.append("configuration fingerprint mismatch")
        expected_configuration = {
            "api_credentials": "not_inherited",
            "execution_mode": "hermetic_synthetic",
            "formal_state": "must_not_be_read_or_written",
            "network": "disabled_by_contract",
            "pytest_plugin_autoload": "disabled",
            "pytest_benchmark_required": False,
            "storage_backend": "in_memory_or_run_owned_sqlite",
            "timing_clock": "time.perf_counter",
        }
        if identity != expected_configuration:
            errors.append("configuration does not match the frozen safe values")

    runner = manifest.get("runner_selector")
    if not isinstance(runner, dict):
        errors.append("runner_selector is required")
    else:
        if runner.get("environment") != {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}:
            errors.append("runner must disable pytest plugin autoload")
        if runner.get("node_ids") != sorted(_EXPECTED_FUNCTIONAL_NODE_IDS):
            errors.append("runner node_ids do not match the frozen benchmark population")

    protocol = manifest.get("measurement_protocol")
    if not isinstance(protocol, dict):
        errors.append("measurement_protocol is required")
    else:
        cold = protocol.get("cold")
        if not isinstance(cold, dict) or not _positive_int(cold.get("fresh_process_repetitions")):
            errors.append("cold protocol requires fresh_process_repetitions")
        hot = protocol.get("hot")
        if not isinstance(hot, dict) or not _positive_int(hot.get("repetitions")):
            errors.append("hot repetitions must be positive")
        elif not _positive_int(hot.get("warmup_repetitions")):
            errors.append("hot warmup_repetitions must be positive")
        repeatability = protocol.get("repeatability")
        if not isinstance(repeatability, dict) or not isinstance(
            repeatability.get("max_cv"), (int, float)
        ):
            errors.append("repeatability max_cv is required")
        elif not 0 < float(repeatability["max_cv"]) < 1:
            errors.append("repeatability max_cv must be between zero and one")
        if isinstance(repeatability, dict):
            if not _positive_int(repeatability.get("independent_runs")):
                errors.append("repeatability independent_runs must be positive")
            report = repeatability.get("report")
            if not isinstance(report, list) or set(report) != _REPEATABILITY_REPORT:
                errors.append("repeatability report fields are incomplete")
            if repeatability.get("p95_relative_regression_budget") != 0.10:
                errors.append("P95 relative regression budget must remain 10%")
            if repeatability.get("peak_rss_relative_regression_budget") != 0.15:
                errors.append("peak RSS relative regression budget must remain 15%")
            if not _positive_number(repeatability.get("absolute_floor_ms")):
                errors.append("repeatability absolute_floor_ms must be positive")

    datasets = manifest.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        errors.append("at least one frozen dataset is required")
    else:
        seen_paths: set[str] = set()
        for dataset in datasets:
            if not isinstance(dataset, dict):
                errors.append("dataset entries must be objects")
                continue
            relative_path = dataset.get("path")
            expected_hash = dataset.get("sha256")
            if not isinstance(relative_path, str) or not relative_path:
                errors.append("dataset path is required")
                continue
            if relative_path in seen_paths:
                errors.append(f"duplicate dataset path: {relative_path}")
            seen_paths.add(relative_path)
            if not _valid_sha256(expected_hash):
                errors.append(f"dataset sha256 is invalid: {relative_path}")
                continue
            if verify_files:
                path = project_root / relative_path
                if not path.is_file():
                    errors.append(f"dataset is missing: {relative_path}")
                elif sha256_file(path) != expected_hash:
                    errors.append(f"dataset hash mismatch: {relative_path}")

    workloads = manifest.get("workloads")
    if not isinstance(workloads, list) or not workloads:
        errors.append("at least one workload is required")
    else:
        workload_ids: set[str] = set()
        dataset_paths = (
            {
                item.get("path")
                for item in datasets
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            }
            if isinstance(datasets, list)
            else set()
        )
        for workload in workloads:
            if not isinstance(workload, dict):
                errors.append("workload entries must be objects")
                continue
            workload_id = workload.get("workload_id")
            if not isinstance(workload_id, str) or not workload_id:
                errors.append("workload_id is required")
                continue
            if workload_id in workload_ids:
                errors.append(f"duplicate workload_id: {workload_id}")
            workload_ids.add(workload_id)
            if workload.get("dataset_path") not in dataset_paths:
                errors.append(f"workload dataset_path is required: {workload_id}")
            if not isinstance(workload.get("absolute_budget"), dict):
                errors.append(f"workload absolute_budget is required: {workload_id}")
            if not isinstance(workload.get("baseline_observation"), dict):
                errors.append(f"workload baseline_observation is required: {workload_id}")
            elif workload["baseline_observation"].get("certifying") is not False:
                errors.append(f"workload baseline must be non-certifying: {workload_id}")
            expected = _EXPECTED_WORKLOADS.get(workload_id)
            if expected is None:
                errors.append(f"unknown workload_id: {workload_id}")
            else:
                for field, expected_value in expected.items():
                    if workload.get(field) != expected_value:
                        errors.append(
                            f"workload {field} drifted from frozen contract: {workload_id}"
                        )
        if workload_ids != set(_EXPECTED_WORKLOADS):
            errors.append("workload denominator does not match the frozen contract")

    denominators = manifest.get("denominators")
    if not isinstance(denominators, dict):
        errors.append("functional/recall/acl/effect denominators are required")
    else:
        keys = set(denominators)
        if keys != _DENOMINATORS:
            missing = sorted(_DENOMINATORS - keys)
            extra = sorted(keys - _DENOMINATORS)
            errors.append(f"denominator keys mismatch: missing={missing}, extra={extra}")
        for name in sorted(_DENOMINATORS & keys):
            denominator = denominators.get(name)
            if not isinstance(denominator, dict):
                errors.append(f"{name} denominator must be an object")
                continue
            observed = denominator.get("observed")
            if not isinstance(observed, int) or observed < 0:
                errors.append(f"{name} denominator observed must be non-negative")
                continue
            status = denominator.get("status")
            if observed == 0 and status == "passed":
                errors.append(f"{name} denominator cannot pass with zero observations")
            if name != "functional" and observed == 0:
                if status != "missing_phase7_evidence":
                    errors.append(f"{name} zero denominator must remain missing_phase7_evidence")
                if denominator.get("release_blocking") is not True:
                    errors.append(f"{name} zero denominator must block release")
            if name == "functional":
                node_ids = denominator.get("node_ids")
                valid_node_ids = (
                    node_ids
                    if isinstance(node_ids, list)
                    and all(isinstance(node_id, str) for node_id in node_ids)
                    else []
                )
                if not isinstance(node_ids, list) or not all(
                    isinstance(node_id, str) and node_id.startswith("tests/benchmark/")
                    for node_id in node_ids
                ):
                    errors.append("functional denominator requires exact benchmark node_ids")
                elif len(node_ids) != len(set(node_ids)):
                    errors.append("functional denominator node_ids must be unique")
                elif observed != len(node_ids):
                    errors.append("functional denominator observed must equal exact node_ids")
                if denominator.get("required") != observed or observed != len(
                    _EXPECTED_FUNCTIONAL_NODE_IDS
                ):
                    errors.append("functional denominator must remain exact 9/9")
                if set(valid_node_ids) != _EXPECTED_FUNCTIONAL_NODE_IDS:
                    errors.append("functional denominator node_ids do not match frozen population")
                elif verify_files and any(
                    not _node_id_exists(project_root, node_id) for node_id in valid_node_ids
                ):
                    errors.append("functional denominator contains nonexistent node_ids")
            else:
                if observed != 0:
                    errors.append(f"{name} Phase 0 observed denominator must remain zero")
                if not denominator.get("required"):
                    errors.append(f"{name} required denominator must be explicit")

    missing_phase7 = manifest.get("missing_phase7_evidence")
    required_missing = {
        "formal_performance_runner",
        "recall_acl_effect_oracle",
        "queue_fairness_starvation",
        "2h_24h_soak",
    }
    if not isinstance(missing_phase7, list) or set(missing_phase7) != required_missing:
        errors.append("missing_phase7_evidence denominator is incomplete")

    contract_fingerprint = _canonical_hash(_contract_identity(manifest))
    if manifest.get("contract_fingerprint_sha256") != contract_fingerprint:
        errors.append("performance contract fingerprint mismatch")
    if contract_fingerprint != EXPECTED_CONTRACT_FINGERPRINT:
        errors.append("performance contract does not match the frozen canonical fingerprint")

    return errors


def build_performance_baseline_health(
    manifest: Mapping[str, Any],
    *,
    verify_files: bool = True,
) -> dict[str, Any]:
    """Build a non-certifying health report for the frozen baseline contract."""

    errors = validate_performance_baseline(manifest, verify_files=verify_files)
    denominators = manifest.get("denominators", {})
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not errors,
        "errors": errors,
        "contract_mode": manifest.get("contract_mode"),
        "performance_certificate_eligible": False,
        "phase7_root_closed": False,
        "counts": {
            "datasets": len(manifest.get("datasets", [])),
            "workloads": len(manifest.get("workloads", [])),
            "functional_observed": (
                denominators.get("functional", {}).get("observed", 0)
                if isinstance(denominators, dict)
                else 0
            ),
            "missing_phase7_evidence": len(manifest.get("missing_phase7_evidence", [])),
        },
    }
