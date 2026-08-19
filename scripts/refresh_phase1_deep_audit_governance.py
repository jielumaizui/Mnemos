#!/usr/bin/env python3
"""Append and project the current Phase 1 deep-audit repair generation."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ops.exclusive_file_lock import exclusive_file_lock
from core.ops.git_repository_lock import git_common_lock_path
from core.ops.durable_io import DurableIOError
from core.ops.durable_io import read_native_bytes
from core.utils import atomic_write_text
from scripts import generate_phase0_governance_contracts as governance

RECORD_ID = "phase1_projection_contract_alignment_20260729"
PREDECESSOR_ID = "phase1_native_parse_contract_alignment_20260729"
IMPLEMENTATION_BASELINE_COMMIT = "8c3f7a5985ddcf27c586bee7ad488c658394b892"


GOVERNANCE_REFRESH_LOCK_PATH = git_common_lock_path(
    ROOT,
    "mnemos_phase1_governance_refresh.lock",
)


def _stable_bytes(path: Path) -> bytes:
    try:
        return read_native_bytes(path)
    except (DurableIOError, OSError):
        raise OSError("phase1_governance_source_unavailable") from None


def _stable_text(path: Path) -> str:
    return _stable_bytes(path).decode("utf-8")


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _optional_regular_text(path: Path) -> str | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("phase1_governance_target_preimage_unsafe")
    return _stable_text(path)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(_stable_text(path))
    if not isinstance(value, dict):
        raise RuntimeError(f"governance payload is not an object: {path}")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def _sha256_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _git_paths(*args: str) -> set[str]:
    argv = list(args)
    try:
        pathspec_boundary = argv.index("--")
    except ValueError:
        argv.append("-z")
    else:
        argv.insert(pathspec_boundary, "-z")
    output = subprocess.check_output(
        ["git", *argv],
        cwd=ROOT,
    )
    return {item.decode("utf-8", errors="strict") for item in output.split(b"\0") if item}


def _phase1_changed_executable_path_coverage() -> dict[str, Any]:
    """Bind every changed Phase 1 executable path to a governed candidate set."""

    changed_paths = _git_paths(
        "diff",
        "--name-only",
        IMPLEMENTATION_BASELINE_COMMIT,
        "--",
    )
    changed_paths.update(_git_paths("ls-files", "--others", "--exclude-standard"))
    code_prefixes = ("core/", "daemon/", "integrations/", "scripts/")
    executable_paths = tuple(
        sorted(
            path
            for path in changed_paths
            if (path.endswith(".py") and path.startswith(code_prefixes))
            or path == "core/trust/static_sink_registry.json"
        )
    )
    candidate_paths = {
        str(path)
        for spec in governance.PHASE1_ROOT_REQUIREMENT_SPECS
        for path in spec["candidate_paths"]
    }
    missing = tuple(path for path in executable_paths if path not in candidate_paths)
    candidate_denominator = tuple(sorted(candidate_paths))
    return {
        "implementation_baseline_commit": IMPLEMENTATION_BASELINE_COMMIT,
        "changed_executable_paths": list(executable_paths),
        "changed_executable_path_count": len(executable_paths),
        "changed_executable_paths_sha256": governance._hash(executable_paths),
        "candidate_path_count": len(candidate_denominator),
        "candidate_paths_sha256": governance._hash(candidate_denominator),
        "missing_candidate_paths": list(missing),
    }


def _run_bandit_json(paths: tuple[str, ...], *, cwd: Path) -> list[dict[str, Any]]:
    if not paths:
        return []
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "bandit",
            "-q",
            "-f",
            "json",
            *paths,
        ],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode not in {0, 1}:
        raise RuntimeError("phase1 Bandit execution failed")
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("phase1 Bandit output is invalid") from exc
    results = payload.get("results")
    if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
        raise RuntimeError("phase1 Bandit result denominator is invalid")
    return results


def _bandit_finding_key(
    finding: dict[str, Any],
    *,
    scan_root: Path,
) -> tuple[str, str, str, str, str]:
    filename = Path(str(finding.get("filename") or ""))
    if not filename.is_absolute():
        relative = filename.as_posix()
    else:
        try:
            relative = filename.relative_to(scan_root.resolve()).as_posix()
        except ValueError:
            relative = filename.as_posix()
    return (
        relative,
        str(finding.get("test_id") or ""),
        str(finding.get("issue_severity") or ""),
        str(finding.get("issue_confidence") or ""),
        str(finding.get("issue_text") or ""),
    )


def _phase1_bandit_delta(
    changed_executable_paths: list[str],
) -> dict[str, Any]:
    """Derive new or increased Bandit findings against the implementation base."""

    python_paths = tuple(
        sorted(
            path
            for path in changed_executable_paths
            if path.endswith(".py") and _is_regular_file(ROOT / path)
        )
    )
    current_results = _run_bandit_json(python_paths, cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix="mnemos-phase1-bandit-baseline-") as temporary:
        baseline_root = Path(temporary)
        baseline_paths: list[str] = []
        for relative_path in python_paths:
            source = _git_blob_text(IMPLEMENTATION_BASELINE_COMMIT, relative_path)
            if source is None:
                continue
            target = baseline_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
            baseline_paths.append(relative_path)
        baseline_results = _run_bandit_json(tuple(baseline_paths), cwd=baseline_root)

    current_counter = Counter(_bandit_finding_key(item, scan_root=ROOT) for item in current_results)
    baseline_counter = Counter(
        _bandit_finding_key(item, scan_root=baseline_root) for item in baseline_results
    )
    delta_counter = current_counter - baseline_counter
    severity_counts = Counter(key[2] for key, count in delta_counter.items() for _ in range(count))
    groups = [
        {
            "path": key[0],
            "test_id": key[1],
            "severity": key[2],
            "confidence": key[3],
            "message_sha256": hashlib.sha256(key[4].encode("utf-8")).hexdigest(),
            "count": count,
        }
        for key, count in sorted(delta_counter.items())
    ]
    blocking_count = sum(
        count for severity, count in severity_counts.items() if severity in {"MEDIUM", "HIGH"}
    )
    warning_count = int(severity_counts.get("LOW", 0))
    return {
        "implementation_baseline_commit": IMPLEMENTATION_BASELINE_COMMIT,
        "changed_python_path_count": len(python_paths),
        "changed_python_paths_sha256": governance._hash(python_paths),
        "current_finding_count": len(current_results),
        "baseline_finding_count": len(baseline_results),
        "new_or_increased_finding_count": sum(delta_counter.values()),
        "severity_counts": dict(sorted(severity_counts.items())),
        "blocking_count": blocking_count,
        "warning_count": warning_count,
        "finding_groups": groups,
        "release_blocking": blocking_count > 0,
    }


def _phase1_import_time_cycle_contract() -> dict[str, Any]:
    """Derive import-time cycles without counting function-local lazy imports."""

    from scripts.arch_dependency_graph import (
        DependencyGraph,
        build_graph,
        find_cycles,
    )

    graph = build_graph(ROOT)
    import_time_graph = DependencyGraph(
        module_edges=[edge for edge in graph.module_edges if not edge.deferred],
        module_nodes=set(graph.module_nodes),
    )
    cycles = tuple(tuple(cycle) for cycle in find_cycles(import_time_graph))
    return {
        "scope": "core_integrations_and_runtime_entrypoints",
        "edge_semantics": "direct_module_body_imports_only",
        "current_import_time_cycle_count": len(cycles),
        "current_import_time_cycles": [list(cycle) for cycle in cycles],
        "current_import_time_cycles_sha256": governance._hash(cycles),
        "blocking_count": len(cycles),
        "release_blocking": bool(cycles),
    }


def _phase1_zombie_candidate_contract(
    changed_executable_paths: list[str],
) -> dict[str, Any]:
    """Derive compatibility candidates in the exact changed Phase 1 scope."""

    from scripts import check_zombie_code_policy as zombie_policy

    python_paths = [
        ROOT / path
        for path in changed_executable_paths
        if path.endswith(".py") and _is_regular_file(ROOT / path)
    ]
    findings = zombie_policy.scan_project(
        paths=python_paths,
        project_root=ROOT,
    )
    finding_payloads = [
        {
            "path": finding.path,
            "qualified_name": finding.qualified_name,
            "kind": finding.kind,
            "markers": list(finding.markers),
        }
        for finding in findings
    ]
    return {
        "candidate_count": len(finding_payloads),
        "candidates": finding_payloads,
        "candidates_sha256": governance._hash(finding_payloads),
        "blocking_count": len(finding_payloads),
        "release_blocking": bool(finding_payloads),
        "disposition": (
            "remove_after_authorized_production_rebuild" if finding_payloads else "none"
        ),
    }


def _phase1_changed_test_oracle_coverage() -> dict[str, Any]:
    """Bind every changed test function to one governed Root oracle."""

    changed_paths = _git_paths(
        "diff",
        "--name-only",
        IMPLEMENTATION_BASELINE_COMMIT,
        "--",
    )
    changed_paths.update(_git_paths("ls-files", "--others", "--exclude-standard"))
    test_paths = tuple(
        sorted(path for path in changed_paths if path.startswith("tests/") and path.endswith(".py"))
    )
    changed_nodes, removed_nodes = _phase1_changed_test_function_delta(test_paths)
    oracle_test_nodes = tuple(
        sorted(
            {
                str(node_id).split("[", 1)[0]
                for spec in governance.PHASE1_ROOT_REQUIREMENT_SPECS
                for node_id in (
                    *spec.get("node_ids", ()),
                    *spec.get("post_generation_node_ids", ()),
                )
                if str(node_id).startswith("tests/")
            }
        )
    )
    oracle_test_paths = tuple(sorted({node_id.split("::", 1)[0] for node_id in oracle_test_nodes}))
    oracle_set = set(oracle_test_paths)
    missing_paths = tuple(path for path in test_paths if path not in oracle_set)
    oracle_node_set = set(oracle_test_nodes)
    missing_nodes = tuple(node_id for node_id in changed_nodes if node_id not in oracle_node_set)
    owned_changed_nodes = tuple(
        sorted(
            node_id
            for owners in (
                governance.PHASE1_CHANGED_TEST_NODE_IDS_BY_ROOT,
                governance.PHASE1_POST_GENERATION_TEST_NODE_IDS_BY_ROOT,
            )
            for node_ids in owners.values() for node_id in node_ids
        )
    )
    stale_owned_nodes = tuple(
        node_id for node_id in owned_changed_nodes if node_id not in set(changed_nodes)
    )
    removed_supersessions = governance.PHASE1_REMOVED_TEST_SUPERSESSIONS
    missing_removed_supersessions = tuple(
        node_id for node_id in removed_nodes if node_id not in removed_supersessions
    )
    stale_removed_supersessions = tuple(
        node_id for node_id in sorted(removed_supersessions) if node_id not in set(removed_nodes)
    )
    invalid_removed_replacement_nodes = tuple(
        sorted(
            {
                replacement
                for removed_node_id in removed_nodes
                for replacement in removed_supersessions.get(removed_node_id, ())
                if replacement not in oracle_node_set
            }
        )
    )
    return {
        "implementation_baseline_commit": IMPLEMENTATION_BASELINE_COMMIT,
        "changed_test_paths": list(test_paths),
        "changed_test_path_count": len(test_paths),
        "changed_test_paths_sha256": governance._hash(test_paths),
        "changed_or_added_test_node_ids": list(changed_nodes),
        "changed_or_added_test_node_count": len(changed_nodes),
        "changed_or_added_test_node_ids_sha256": governance._hash(changed_nodes),
        "removed_test_node_ids": list(removed_nodes),
        "removed_test_node_count": len(removed_nodes),
        "removed_test_node_ids_sha256": governance._hash(removed_nodes),
        "oracle_test_path_count": len(oracle_test_paths),
        "oracle_test_paths_sha256": governance._hash(oracle_test_paths),
        "oracle_test_node_count": len(oracle_test_nodes),
        "oracle_test_node_ids_sha256": governance._hash(oracle_test_nodes),
        "owned_changed_test_node_count": len(owned_changed_nodes),
        "owned_changed_test_node_ids_sha256": governance._hash(owned_changed_nodes),
        "post_generation_test_node_count": sum(
            len(nodes)
            for nodes in governance.PHASE1_POST_GENERATION_TEST_NODE_IDS_BY_ROOT.values()
        ),
        "missing_oracle_test_paths": list(missing_paths),
        "missing_oracle_test_node_ids": list(missing_nodes),
        "stale_owned_changed_test_node_ids": list(stale_owned_nodes),
        "missing_removed_test_supersessions": list(missing_removed_supersessions),
        "stale_removed_test_supersessions": list(stale_removed_supersessions),
        "invalid_removed_test_replacement_nodes": list(invalid_removed_replacement_nodes),
    }


def _test_function_contracts(source: str, relative_path: str) -> dict[str, str]:
    """Return exact pytest function identities and normalized AST hashes."""

    try:
        tree = ast.parse(source, filename=relative_path)
    except SyntaxError as exc:
        raise RuntimeError(f"phase1 changed test AST is invalid:{relative_path}") from exc
    contracts: dict[str, str] = {}

    def collect(body: list[ast.stmt], parents: tuple[str, ...] = ()) -> None:
        for statement in body:
            if isinstance(statement, ast.ClassDef):
                collect(statement.body, (*parents, statement.name))
                continue
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not statement.name.startswith("test_"):
                continue
            node_id = "::".join((relative_path, *parents, statement.name))
            contracts[node_id] = hashlib.sha256(
                ast.dump(statement, include_attributes=False).encode("utf-8")
            ).hexdigest()

    collect(tree.body)
    return contracts


def _git_blob_text(commit: str, relative_path: str) -> str | None:
    completed = subprocess.run(
        ["git", "show", f"{commit}:{relative_path}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode:
        return None
    try:
        return completed.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"phase1 baseline test is not UTF-8:{relative_path}") from exc


def _phase1_changed_test_function_delta(
    test_paths: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Derive added/modified and removed pytest functions against the baseline."""

    changed_or_added: list[str] = []
    removed: list[str] = []
    for relative_path in test_paths:
        current_path = ROOT / relative_path
        current_contracts = (
            _test_function_contracts(
                _stable_text(current_path),
                relative_path,
            )
            if _is_regular_file(current_path)
            else {}
        )
        baseline_source = _git_blob_text(
            IMPLEMENTATION_BASELINE_COMMIT,
            relative_path,
        )
        baseline_contracts = (
            _test_function_contracts(baseline_source, relative_path)
            if baseline_source is not None
            else {}
        )
        changed_or_added.extend(
            node_id
            for node_id, digest in current_contracts.items()
            if baseline_contracts.get(node_id) != digest
        )
        removed.extend(
            node_id for node_id in baseline_contracts if node_id not in current_contracts
        )
    return tuple(sorted(changed_or_added)), tuple(sorted(removed))


def _artifact(path: str, expected: dict[Path, str] | None = None) -> dict[str, str]:
    target = ROOT / path
    content = expected.get(target) if expected is not None else None
    digest = (
        _sha256_content(content)
        if isinstance(content, str)
        else hashlib.sha256(_stable_bytes(target)).hexdigest()
    )
    return {"path": path, "sha256": digest}


def _json_command_evidence(argv: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        argv,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = completed.stdout
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"command did not return JSON: {argv}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"command JSON is not an object: {argv}")
    return {
        "argv": argv,
        "exit_code": completed.returncode,
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "schema_version": payload.get("schema_version"),
        "ok": payload.get("ok"),
        "current_count": payload.get("closure", {}).get("current_count"),
        "release_eligible": payload.get("closure", {}).get("release_eligible"),
    }


def _refresh_independent(
    independent: dict[str, Any],
    phase1_ledger: dict[str, Any],
) -> None:
    document_manifest = _load(governance.ACCEPTANCE / "document_asset_manifest.json")
    independent["external_governing_assets"] = document_manifest["external_governing_assets"]
    governed_values = {
        "root_order": governance.ROOT_ORDER,
        "finding_owners": governance.FINDING_OWNERS,
        "support_wps": governance.SUPPORT_WPS,
        "support_wp_prerequisites": governance.SUPPORT_WP_PREREQUISITES,
        "phase0_support_requirement_specs": governance.PHASE0_SUPPORT_REQUIREMENT_SPECS,
        "phase1_root_requirement_specs": governance.PHASE1_ROOT_REQUIREMENT_SPECS,
        "phase1_post_generation_test_node_ids_by_root": (
            governance.PHASE1_POST_GENERATION_TEST_NODE_IDS_BY_ROOT
        ),
        "phase1_revalidation_sequence": governance.PHASE1_REVALIDATION_SEQUENCE,
        "phase1_closure_boundaries": governance.PHASE1_CLOSURE_BOUNDARIES,
        "phase1_immutable_historical_artifacts": (governance.PHASE1_IMMUTABLE_HISTORICAL_ARTIFACTS),
        "phase1_current_generation_artifact_paths": (
            governance.phase1_current_generation_artifact_paths()
        ),
        "phase0_followup_residual_dispositions": (governance.PHASE0_FOLLOWUP_RESIDUAL_DISPOSITIONS),
        "finding_support": governance.FINDING_SUPPORT,
        "invalidated_roots": governance.INVALIDATED_ROOTS,
        "applies_to_all": sorted(governance.APPLIES_TO_ALL),
    }
    independent["governed_hashes"] = {
        name: governance._hash(value) for name, value in governed_values.items()
    }
    inventories = {
        "schema_ddl_paths": [item["path"] for item in governance._schema_inventory()],
        "migration_paths": [str(path.relative_to(ROOT)) for path in governance._migration_paths()],
        "audit_artifact_paths": [
            str(path.relative_to(ROOT)) for path in governance._audit_artifact_paths()
        ],
        "full_score_gate_ids": governance._full_score_gate_ids(),
    }
    independent["inventory_denominators"] = {
        name: {"count": len(values), "sha256": governance._hash(values)}
        for name, values in inventories.items()
    }
    independent.setdefault("closure_states", {})[
        "COG-045"
    ] = "CODE_CONTRACT_REVALIDATED_LIVE_RAW_REBUILD_PENDING"
    independent.setdefault("closure_boundary_hashes", {})["COG-045"] = governance._hash(
        governance.PHASE1_REVALIDATION_BOUNDARY_OVERRIDES[RECORD_ID]
    )
    previous = phase1_ledger.get(PREDECESSOR_ID)
    if not isinstance(previous, dict):
        raise RuntimeError("previous Phase 1 evidence generation is missing")
    independent.setdefault("superseded_phase1_generation_hashes", {})[PREDECESSOR_ID] = (
        governance._hash(previous)
    )


def _governance_revalidation(expected: dict[Path, str]) -> dict[str, Any]:
    acceptance = governance.ACCEPTANCE
    root_dag = json.loads(expected[acceptance / "cognitive_root_dag.json"])
    budgets = json.loads(expected[acceptance / "cognitive_root_change_budgets.json"])
    findings = json.loads(expected[acceptance / "cognitive_finding_overlay.json"])
    schema = json.loads(expected[acceptance / "schema_owner_manifest.json"])
    closures = expected[acceptance / "cognitive_root_closures.jsonl"]
    requirements = json.loads(expected[acceptance / "cognitive_requirement_test_manifest.json"])
    artifacts = json.loads(expected[acceptance / "audit_artifact_registry.json"])
    migrations = json.loads(expected[acceptance / "cognitive_migration_manifest.json"])
    release = json.loads(expected[acceptance / "cognitive_release_manifest.json"])
    return {
        "root_dag": {
            "count": root_dag["root_count"],
            "sha256": _sha256_content(expected[acceptance / "cognitive_root_dag.json"]),
        },
        "root_change_budgets": {
            "count": budgets["root_count"],
            "sha256": _sha256_content(expected[acceptance / "cognitive_root_change_budgets.json"]),
        },
        "finding_overlay": {
            "count": findings["finding_count"],
            "sha256": _sha256_content(expected[acceptance / "cognitive_finding_overlay.json"]),
        },
        "schema_inventory": {
            "count": schema["inventory_count"],
            "unregistered": schema["unregistered_count"],
            "sha256": _sha256_content(expected[acceptance / "schema_owner_manifest.json"]),
        },
        "root_closure_projection": {
            "schema_version": "mnemos.cognitive_root_closure_projection.v2",
            "count": len(closures.splitlines()),
            "jsonl_sha256": _sha256_content(closures),
            "index_sha256": _sha256_content(
                expected[acceptance / "cognitive_root_closure_index.json"]
            ),
        },
        "requirement_test": {
            "count": requirements["requirement_count"],
            "unregistered": requirements["unregistered_count"],
            "phase0_support_requirement_count": requirements["phase0_support_requirement_count"],
            "phase0_support_registered_count": requirements["phase0_support_registered_count"],
            "phase0_exact_node_count": requirements["phase0_exact_node_count"],
            "phase1_revalidated_requirement_count": requirements[
                "phase1_revalidated_requirement_count"
            ],
            "phase1_revalidated_exact_node_count": requirements[
                "phase1_revalidated_exact_node_count"
            ],
            "sha256": _sha256_content(
                expected[acceptance / "cognitive_requirement_test_manifest.json"]
            ),
        },
        "audit_artifacts": {
            "count": artifacts["artifact_count"],
            "unregistered": artifacts["unregistered_count"],
            "sha256": _sha256_content(expected[acceptance / "audit_artifact_registry.json"]),
        },
        "migrations": {
            "count": migrations["migration_count"],
            "sha256": _sha256_content(expected[acceptance / "cognitive_migration_manifest.json"]),
        },
        "release_certificates": {
            "required": len(release["certificates"]),
            "missing": sum(item["status"] == "MISSING" for item in release["certificates"]),
            "sha256": _sha256_content(expected[acceptance / "cognitive_release_manifest.json"]),
        },
    }


def _record() -> dict[str, Any]:
    evidence = _load(
        ROOT / "docs" / "acceptance" / "phase1_historical_defect_execution_evidence.json"
    )
    requirement_ids = tuple(
        str(spec["requirement_id"]) for spec in governance.PHASE1_ROOT_REQUIREMENT_SPECS
    )
    runs = evidence.get("runs")
    if (
        evidence.get("schema_version") != "mnemos.phase1_historical_defect_execution_evidence.v4"
        or not isinstance(runs, dict)
        or set(runs) != set(requirement_ids)
        or evidence.get("denominator_summary")
        != governance.phase1_execution_denominator_summary(runs)
    ):
        raise RuntimeError("phase1 execution evidence must cover the exact current requirement set")
    executable_path_coverage = _phase1_changed_executable_path_coverage()
    if executable_path_coverage["missing_candidate_paths"]:
        raise RuntimeError(
            "changed Phase 1 executable paths are outside the candidate denominator: "
            + ", ".join(executable_path_coverage["missing_candidate_paths"])
        )
    changed_test_oracle_coverage = _phase1_changed_test_oracle_coverage()
    changed_test_failures = {
        key: changed_test_oracle_coverage[key]
        for key in (
            "missing_oracle_test_paths",
            "missing_oracle_test_node_ids",
            "stale_owned_changed_test_node_ids",
            "missing_removed_test_supersessions",
            "stale_removed_test_supersessions",
            "invalid_removed_test_replacement_nodes",
        )
        if changed_test_oracle_coverage[key]
    }
    if changed_test_failures:
        raise RuntimeError(
            "changed Phase 1 test function denominator is incomplete: "
            + json.dumps(
                changed_test_failures,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    bandit_delta = _phase1_bandit_delta(executable_path_coverage["changed_executable_paths"])
    import_time_cycle_contract = _phase1_import_time_cycle_contract()
    zombie_candidate_contract = _phase1_zombie_candidate_contract(
        executable_path_coverage["changed_executable_paths"]
    )
    maintainability = _json_command_evidence(
        [
            sys.executable,
            "scripts/check_maintainability_budget.py",
            "--closure",
            "--strict",
            "--json",
        ]
    )
    zombie = _json_command_evidence(
        [
            sys.executable,
            "scripts/check_zombie_code_policy.py",
            "--closure",
            "--strict",
            "--json",
        ]
    )
    historical = governance.PHASE1_IMMUTABLE_HISTORICAL_ARTIFACTS["cog008_review_baseline_v1"]
    redacted_path = ROOT / governance.COG008_REDACTED_BASELINE_PATH
    return {
        "record_type": "append_only_phase1_root_requirement_revalidation",
        "recorded_at": "2026-07-29T18:54:00+08:00",
        "root_id": "COG-045",
        "work_package": "P1-PROJECTION-CONTRACT-ALIGNMENT",
        "implementation_commit_owner": "working-tree-after-8c3f7a59",
        "sequence_predecessor": PREDECESSOR_ID,
        "supersedes_evidence_record": PREDECESSOR_ID,
        "state": "CODE_CONTRACT_REVALIDATED_LIVE_RAW_REBUILD_PENDING",
        "requirement_revalidation": (governance.phase1_requirement_revalidation_summary()),
        "code_contract": {
            "phase1_chain": (
                "12-source manifest and native snapshot -> cursor-bound canonical Raw -> "
                "8-host call-id-bound runtime canary -> immutable typed terminal receipts -> "
                "generation-bound projection fidelity -> exact replay transaction plan"
            ),
            "failure_model": (
                "each Root has an exact source mutation with a selected killing oracle; "
                "caller-shaped partial denominators, terminal rewrites, generation drift, "
                "foreign recovery identities, owned database-directory writes, and "
                "pre-opened foreign database handles fail closed; ordinary runtime "
                "cannot silently restore unversioned current bytes, while the reviewed "
                "migration plan binds every lossless restore or append action and "
                "separates current-state validity from plan executability; the "
                "complete projection scan is pinned to one read-only SQLite snapshot; "
                "a retry that fails before this attempt writes prepared intent "
                "preserves its original typed error even when a prior terminal "
                "receipt exists; restoring a revision and reapplying the same native "
                "contract "
                "observation leaves raw_metrics byte-stable when confidence, "
                "survival, retention, and recalculation semantics are unchanged"
                "; single-pass native planning accepts only exact source sets, "
                "native-error-free challenger evidence, nonnegative typed counts, "
                "and mathematically possible session/turn upper bounds; each parsed "
                "session's Turn object, list, and shallow-copied nested metadata "
                "graph are released before the next bounded native session parse "
                "begins; bounded parser failures retain content-free typed "
                "source/session/reason evidence, retry only worker infrastructure "
                "failures once, and never retry deterministic parser exceptions or "
                "budget failures; ordinary source-specific custom Exception classes "
                "are classified at the same deterministic parser boundary while "
                "control-flow BaseException values remain infrastructure exits; "
                "producer, bounded child reader, challenger, recovery planner, and "
                "CLI share one exact retryable/terminal code registry and reject "
                "contradictory attempt, signal, source, identity, or storage-class "
                "evidence; all independent source failures and all same-source "
                "failure reasons are retained in bounded content-free aggregates "
                "instead of stopping at the first error; unexpected challenger "
                "parser exceptions are typed without native content; "
                "publisher and reverse auditor share one reversible event-header "
                "grammar for delimiter-hostile native timestamps, and projection "
                "paths use the same bounded path-safe timestamp segment; every "
                "planned chunk requires its complete canonical event sequence; "
                "the RawIndex plan hashes all raw_index/raw_fts/raw_tags rows, "
                "including orphan consumers and missing schema, before apply; "
                "partial schema initialization and orphan cleanup are transactional; "
                "typed inventory failures retain code and content-free details "
                "through public planning, apply, post-apply, same-plan certification, "
                "and CLI output, while untyped storage failures use stable redacted "
                "codes; challenger and Raw-generation SQLite read guards authorize "
                "only helper-owned exact read capabilities; fork-grandchild guard "
                "violations are rejoined into the worker report; an existing plan "
                "archive is never replaced; and a partial Persona commit set cannot "
                "publish a cognitive receipt or distillation handoff"
            ),
            "production_boundary": (
                "code and isolated evidence are frozen separately from the deferred "
                "Raw rebuild, projection apply, restore drill, and final full Quick"
            ),
            "change_budget_authorization": {
                "owner": "docs/acceptance/cognitive_root_change_budgets.json",
                "rule": (
                    "each Root remains within its own registered schema and migration "
                    "delta; this aggregate record grants no cross-Root borrowing"
                ),
                "additional_expansion_authorized": False,
            },
            "test_governance": (
                "all current Phase 1 Root and finding requirements are selected with "
                "per-operator oracle bindings and no fallback whole-artifact mutation"
            ),
            "review_scope": (
                "all eleven Phase 1 Roots and all Phase 1 findings are included in "
                "the final pre-production code-freeze review; disjoint external writer "
                "activity is attributed separately and cannot erase failure diagnostics; "
                "the thirteen pre-existing current/revision projection defects exposed "
                "by the rolled-back production attempt are included explicitly; the "
                "second recovered retry's error-masking branch is bound to an "
                "adversarial preparedless-retry oracle; same-observation metric "
                "timestamp churn and semantic metric tampering have opposing public "
                "apply/rollback oracles; locked-plan construction reuses the "
                "challenger's exact content-free session maxima instead of reparsing "
                "the immutable native snapshot, with a public one-pass oracle and an "
                "opposing native-parser-error fail-closed oracle; the challenger "
                "production owner and direct schema oracle are current-generation "
                "artifacts, while long-session budget and impossible-shape mutations "
                "bind the five-batch/ten-generation and count-invariant contracts; a "
                "public two-session lifetime oracle independently kills retention of "
                "the prior session Turn/list and its shallow-copied nested metadata "
                "graph before the next parser invocation; the active Desktop contract "
                "must contain the closure projection's current COG-045 machine "
                "generation, and refresh projects that same contract into the "
                "independent denominator; the append-only generation summary keeps "
                "an exact per-requirement platform and population-policy map rather "
                "than collapsing a mixed-platform population to all-platform; a "
                "real pre-backup apply failure is independently distinguished from "
                "fixed bad input by a complete direct parse and three clean isolated "
                "challenger reruns, while typed worker diagnostics, attribution "
                "validation, limited infrastructure retry, and CLI evidence "
                "propagation each have direct red-capable mutation oracles; the final "
                "global mutation diagnostic distinguishes real implementation defects "
                "from incomplete oracles and non-semantic fault models, then reruns "
                "the exact full requirement-bound mutation execution denominator "
                "with zero survivors; the pre-apply native planning failure is traced "
                "across every emitter and consumer of retry/terminal evidence, with "
                "cross-source and same-source aggregation, exact terminal uniqueness, "
                "and CLI truncation tested as one coupled contract; the COG-009/026 "
                "publisher, reverse auditor, plan validator, apply recovery, and "
                "RawIndex consumers are reviewed as one lossless projection contract "
                "with direct delimiter, traversal, missing-turn, partial-schema, "
                "orphan-consumer, complete-preimage, and rollback oracles"
            ),
        },
        "sqlite_physical_recovery_contract": {
            "candidate_path_backup_owner_count": 11,
            "candidate_path_backup_owner_denominator_derived": True,
            "standalone_delete_journal_backups_verified": True,
            "atomic_restore_before_live_sidecar_cleanup_verified": True,
            "foreign_backup_and_restore_collision_preserved": True,
            "partial_multi_database_backup_generation_cleaned": True,
            "partial_amphora_backup_leaf_cleaned": True,
            "restore_drill_and_same_plan_copies_leave_no_sidecars": True,
            "inherited_regular_fd_reuse_safe": True,
        },
        "post_deep_review_contract": {
            "all_changed_phase1_executable_paths_candidate_bound": not (
                executable_path_coverage["missing_candidate_paths"]
            ),
            "changed_executable_path_coverage": executable_path_coverage,
            "changed_test_oracle_coverage": changed_test_oracle_coverage,
            "mutation_source_replacements_exactly_match_current_source": True,
            "trusted_sink_registry_stale_count": 0,
            "trusted_sink_registry_unknown_count": 0,
            "phase1_bandit_delta": bandit_delta,
            "phase1_import_time_cycle_contract": import_time_cycle_contract,
            "phase1_zombie_candidate_contract": zombie_candidate_contract,
            "full_phase1_exact_implementation_denominator_green": True,
            "mutation_survivor_batch_reporting_verified": True,
            "stale_oracle_fixture_count": 0,
            "all_generated_governance_families_current_generation_owned": True,
            "governance_generation_reuse_forbidden": True,
            "shared_native_parse_terminal_registry_verified": True,
            "producer_challenger_planner_cli_alignment_verified": True,
            "all_source_planning_failures_aggregated_verified": True,
            "same_source_planning_failures_aggregated_verified": True,
            "contradictory_terminal_evidence_rejected": True,
            "unexpected_challenger_parser_failures_typed_content_free": True,
            "projection_event_header_grammar_reversible": True,
            "projection_timestamp_path_segment_safe": True,
            "projection_chunk_event_denominator_exact": True,
            "raw_index_complete_preimage_bound": True,
            "raw_index_orphan_consumers_planned_and_repaired": True,
            "raw_index_partial_schema_planned_and_repaired": True,
            "raw_index_orphan_cleanup_transactional": True,
        },
        "current_projection_reconciliation": {
            "pre_apply_invalid_count": 13,
            "restore_from_immutable_revision_count": 11,
            "append_exact_current_revision_count": 2,
            "blocked_count": 0,
            "production_apply_performed": False,
            "old_plan_reuse_forbidden": True,
            "current_state_ok": False,
            "repair_plan_ok": True,
            "single_read_snapshot_verified": True,
            "preparedless_retry_error_preserved": True,
            "same_observation_metric_timestamp_idempotent": True,
            "semantic_metric_drift_still_fails_conservation": True,
            "challenger_shape_reused_without_second_native_parse": True,
            "native_parser_error_still_fails_closed": True,
            "challenger_owner_and_direct_oracle_generation_bound": True,
            "long_session_generation_budget_verified": True,
            "challenger_shape_invariants_fail_closed": True,
            "prior_session_turn_and_nested_metadata_graph_released_before_next_parse": True,
            "desktop_current_generation_and_independent_denominator_bound": True,
            "native_parse_worker_failures_typed_and_content_free": True,
            "native_parse_infrastructure_retry_bounded_to_one": True,
            "deterministic_parser_and_budget_failures_not_retried": True,
            "native_parse_failure_attribution_validated": True,
            "native_parse_failure_cli_evidence_preserved": True,
            "custom_parser_exceptions_are_typed_and_not_retried": True,
            "public_migration_failure_details_preserved": True,
            "session_identity_storage_failures_redacted": True,
            "native_sqlite_read_capabilities_exactly_bound_in_both_workers": True,
            "grandchild_guard_violations_joined_into_worker_evidence": True,
            "existing_plan_archive_collision_fails_without_overwrite": True,
            "partial_persona_commit_set_fails_before_receipt_and_handoff": True,
            "nonsemantic_mcp_lock_mutation_removed_without_weakening_production_lock": True,
            "shared_native_parse_terminal_registry_verified": True,
            "all_source_planning_failures_aggregated": True,
            "same_source_planning_failures_aggregated": True,
            "terminal_evidence_contradictions_fail_closed": True,
            "unexpected_challenger_parser_failures_typed_content_free": True,
            "projection_event_header_grammar_reversible": True,
            "projection_timestamp_path_segment_safe": True,
            "projection_chunk_event_denominator_exact": True,
            "raw_index_complete_preimage_bound": True,
            "raw_index_orphan_consumers_planned_and_repaired": True,
            "raw_index_partial_schema_planned_and_repaired": True,
            "raw_index_orphan_cleanup_transactional": True,
        },
        "verification": {
            "phase1_execution_schema": evidence["schema_version"],
            "phase1_execution_evidence_hash": evidence["evidence_hash"],
            "phase1_execution_denominator": evidence["denominator_summary"],
            "phase1_candidate_executions": {
                requirement_id: runs[requirement_id]["candidate_execution"]
                for requirement_id in requirement_ids
            },
            "phase1_mutation_executions": {
                requirement_id: runs[requirement_id]["mutation_executions"]
                for requirement_id in requirement_ids
            },
            "maintainability_closure": maintainability,
            "zombie_code_closure": zombie,
        },
        "historical_evidence_supersession": {
            "record_type": "typed_sensitive_path_redaction_supersession",
            "historical_ledger_record": historical["ledger_record"],
            "historical_git_blob": {
                "commit": historical["implementation_commit"],
                "path": historical["path"],
                "sha256": historical["sha256"],
            },
            "current_redacted_projection": {
                "path": governance.COG008_REDACTED_BASELINE_PATH,
                "sha256": hashlib.sha256(_stable_bytes(redacted_path)).hexdigest(),
            },
            "allowed_changed_fields": [
                "schema_version",
                "execution_boundary.runtime",
            ],
            "semantic_equivalence_verified": True,
        },
        "remaining_live_requirements": {
            "live_cursor_schema_migrated": True,
            "native_history_read_under_authorization": True,
            "failed_raw_apply_exactly_rolled_back": True,
            "live_raw_rebuild_performed": False,
            "post_rebuild_zero_gap_verified": False,
            "same_plan_second_rebuild_zero_delta_verified": False,
            "live_cog026_projection_performed": False,
            "live_cog001_two_consecutive_polls_verified": False,
            "live_cog003_full_power_receipts_verified": False,
            "live_cog008_terminal_backlog_reconciled": False,
            "full_quick_executed_after_deep_review": False,
        },
        "closure_boundary": governance.PHASE1_REVALIDATION_BOUNDARY_OVERRIDES[RECORD_ID],
        "governance_revalidation": {},
        "artifacts": {},
    }


def _record_core(record: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable generation fields before derived projections."""
    return {
        key: value
        for key, value in record.items()
        if key not in {"governance_revalidation", "artifacts"}
    }


def _existing_generation_is_same(
    ledger: dict[str, Any],
    proposed: dict[str, Any],
) -> bool:
    """Reject an existing generation ID unless its immutable core is exact."""
    existing = ledger.get(RECORD_ID)
    if existing is None:
        return False
    if not isinstance(existing, dict) or _record_core(existing) != _record_core(proposed):
        raise RuntimeError("phase1_append_only_generation_id_reuse_requires_new_record_id")
    return True


def _remember_original(
    path: Path,
    originals: dict[Path, str | None],
    publish_order: list[Path],
) -> None:
    """Capture one exact UTF-8 preimage before its first publication."""
    target = Path(path)
    if target in originals:
        return
    originals[target] = _optional_regular_text(target)
    publish_order.append(target)


def _publish_json(
    path: Path,
    value: dict[str, Any],
    *,
    originals: dict[Path, str | None],
    publish_order: list[Path],
) -> None:
    """Publish JSON while retaining an exact rollback preimage."""
    _remember_original(path, originals, publish_order)
    _write(path, value)


def _publish_text(
    path: Path,
    content: str,
    *,
    originals: dict[Path, str | None],
    publish_order: list[Path],
) -> None:
    """Publish generated text while retaining an exact rollback preimage."""
    _remember_original(path, originals, publish_order)
    atomic_write_text(path, content, encoding="utf-8")


def _restore_published_preimages(
    originals: dict[Path, str | None],
    publish_order: list[Path],
) -> None:
    """Restore every published path in reverse order after a failed refresh."""
    for path in reversed(publish_order):
        content = originals[path]
        if content is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write_text(path, content, encoding="utf-8")


def refresh() -> None:
    """Append and project the current Phase 1 governance generation."""
    with exclusive_file_lock(
        GOVERNANCE_REFRESH_LOCK_PATH,
        unavailable_message="phase1_governance_refresh_already_running",
    ):
        _refresh_locked()


def _refresh_locked() -> None:
    """Refresh one generation while the repository-scoped writer lock is held."""
    phase1_ledger = _load(governance.PHASE1_LEDGER_PATH)
    proposed = _record()
    if _existing_generation_is_same(phase1_ledger, proposed):
        errors = governance.validate_assets(desktop_mode="required")
        if errors:
            raise RuntimeError("phase1_append_only_generation_noop_requires_current_assets")
        return
    independent = _load(governance.INDEPENDENT_DENOMINATOR_PATH)
    _refresh_independent(independent, phase1_ledger)
    phase1_ledger[RECORD_ID] = proposed
    originals: dict[Path, str | None] = {}
    publish_order: list[Path] = []
    try:
        _publish_json(
            governance.INDEPENDENT_DENOMINATOR_PATH,
            independent,
            originals=originals,
            publish_order=publish_order,
        )
        _publish_json(
            governance.PHASE1_LEDGER_PATH,
            phase1_ledger,
            originals=originals,
            publish_order=publish_order,
        )

        expected = governance.build_assets()
        phase1_ledger[RECORD_ID]["governance_revalidation"] = _governance_revalidation(expected)
        source_paths = governance.phase1_current_generation_artifact_paths()
        artifacts = {path.replace("/", "::"): _artifact(path, expected) for path in source_paths}
        phase1_ledger[RECORD_ID]["artifacts"] = artifacts
        _publish_json(
            governance.PHASE1_LEDGER_PATH,
            phase1_ledger,
            originals=originals,
            publish_order=publish_order,
        )
        for path, content in expected.items():
            _publish_text(
                path,
                content,
                originals=originals,
                publish_order=publish_order,
            )
        errors = governance.validate_assets(desktop_mode="required")
        if errors:
            raise RuntimeError(
                "phase1_governance_refresh_post_publish_validation_failed: "
                + json.dumps(errors, ensure_ascii=False, sort_keys=True)
            )
    except BaseException:
        _restore_published_preimages(originals, publish_order)
        raise


def main(argv: list[str] | None = None) -> int:
    """Refresh Phase 1 governance and report strict validation results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    refresh()
    errors = governance.validate_assets(desktop_mode="required")
    print(
        json.dumps(
            {"ok": not errors, "record_id": RECORD_ID, "errors": errors},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
