"""Pytest-node and mutation execution evidence validation for Phase 1."""

from __future__ import annotations

import ast
import hashlib
import json
import stat
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping

from core.ops.durable_io import DurableIOError, regular_file_sha256
from core.ops.durable_io import read_native_bytes
from scripts.phase0_governance_constants import (
    PHASE1_BASELINE_COMMITS,
    PHASE1_ROOT_REQUIREMENT_SPECS,
    ROOT,
)
from scripts.phase0_governance_inventory import _hash


@dataclass(frozen=True)
class ExecutionValidationContext:
    root: Any
    requirement_specs: tuple[Mapping[str, Any], ...]
    baseline_commits: Mapping[str, str]
    outcome_marker_reader: Any = None
    execution_snapshot_reader: Any = None


_ACTIVE_CONTEXT: ContextVar[ExecutionValidationContext | None] = ContextVar(
    "phase1_governance_execution_context",
    default=None,
)


@contextmanager
def execution_validation_scope(
    context: ExecutionValidationContext,
) -> Iterator[None]:
    token = _ACTIVE_CONTEXT.set(context)
    try:
        yield
    finally:
        _ACTIVE_CONTEXT.reset(token)


def _context() -> ExecutionValidationContext:
    active = _ACTIVE_CONTEXT.get()
    if active is not None:
        return active
    return ExecutionValidationContext(
        root=ROOT,
        requirement_specs=tuple(PHASE1_ROOT_REQUIREMENT_SPECS),
        baseline_commits=dict(PHASE1_BASELINE_COMMITS),
    )


def _stable_regular_bytes(path: Any) -> bytes | None:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            return None
        return read_native_bytes(path)
    except (DurableIOError, OSError):
        return None


def _unregistered_requirement(
    *,
    requirement_id: str,
    root_id: str,
    finding_id: str | None,
    requirement_kind: str,
    test_lanes: list[str],
) -> dict[str, Any]:
    return {
        "requirement_id": requirement_id,
        "root_id": root_id,
        "finding_id": finding_id,
        "requirement_kind": requirement_kind,
        "work_package_id": None,
        "coverage_scope": None,
        "risk_level": "UNCLASSIFIED",
        "runner_kind": None,
        "entrypoint": None,
        "argv": [],
        "node_ids": [],
        "fixture_id": None,
        "fixture_hash": None,
        "oracle_symbol": None,
        "oracle_source_hash": None,
        "baseline_expected_failure": None,
        "baseline_artifact_ref": None,
        "candidate_artifact_ref": None,
        "test_lanes": test_lanes,
        "mutation_operator_ids": [],
        "required_population_policy": "UNREGISTERED",
        "production_artifact_type": None,
        "invalidates": [],
        "status": "UNREGISTERED",
        "release_blocking": True,
    }


def _pytest_node_ast(
    node_id: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    parts = node_id.split("::")
    if len(parts) < 2:
        return None
    path = _context().root / parts[0]
    source = _stable_regular_bytes(path)
    if source is None:
        return None
    try:
        tree = ast.parse(source.decode("utf-8"))
    except (UnicodeError, SyntaxError):
        return None
    candidates: list[ast.stmt] = list(tree.body)
    for index, raw_name in enumerate(parts[1:]):
        name = raw_name.split("[", 1)[0]
        match = next(
            (
                node
                for node in candidates
                if isinstance(
                    node,
                    (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                )
                and node.name == name
            ),
            None,
        )
        if match is None:
            return None
        final = index == len(parts[1:]) - 1
        if final:
            return match if isinstance(match, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
        if not isinstance(match, ast.ClassDef):
            return None
        candidates = list(match.body)
    return None


def _pytest_node_exists(node_id: str) -> bool:
    return _pytest_node_ast(node_id) is not None


def _pytest_node_has_assertion(node_id: str) -> bool:
    node = _pytest_node_ast(node_id)
    if node is None:
        return False
    for child in ast.walk(node):
        if not isinstance(child, ast.Assert):
            continue
        try:
            ast.literal_eval(child.test)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            return True
    if any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and isinstance(child.func.value, ast.Name)
        and child.func.value.id == "self"
        and (child.func.attr.startswith("assert") or child.func.attr == "fail")
        for child in ast.walk(node)
    ):
        return True
    mock_assertion_methods = {
        "assert_any_await",
        "assert_any_call",
        "assert_awaited",
        "assert_awaited_once",
        "assert_awaited_once_with",
        "assert_awaited_with",
        "assert_called",
        "assert_called_once",
        "assert_called_once_with",
        "assert_called_with",
        "assert_has_awaits",
        "assert_has_calls",
        "assert_not_awaited",
        "assert_not_called",
    }
    if any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr in mock_assertion_methods
        for child in ast.walk(node)
    ):
        return True
    return any(
        isinstance(child, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and isinstance(item.context_expr.func.value, ast.Name)
            and item.context_expr.func.value.id == "pytest"
            and item.context_expr.func.attr == "raises"
            for item in child.items
        )
        for child in ast.walk(node)
    )


def _pytest_node_outcome_markers(node_id: str) -> set[str]:
    path = _context().root / node_id.split("::", 1)[0]
    node = _pytest_node_ast(node_id)
    if node is None:
        return {"missing"}
    try:
        source = _stable_regular_bytes(path)
        if source is None:
            return {"missing"}
        tree = ast.parse(source.decode("utf-8"))
    except (UnicodeError, SyntaxError):
        return {"missing"}
    markers: set[str] = set()

    def collect(value: ast.AST) -> None:
        for child in ast.walk(value):
            if isinstance(child, ast.Attribute) and child.attr in {
                "skip",
                "skipif",
                "xfail",
            }:
                markers.add(child.attr)
            elif isinstance(child, ast.Name) and child.id == "requires_macos_sandbox":
                markers.add("skipif")

    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if statement.value is not None and any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets
        ):
            collect(statement.value)
    for decorator in node.decorator_list:
        collect(decorator)
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id == "pytest"
            and child.func.attr in {"skip", "xfail"}
        ):
            markers.add(child.func.attr)
    return markers


def _phase1_static_outcome_markers_are_valid(
    spec: Mapping[str, Any],
    node_ids: Iterable[str],
) -> bool:
    marker_reader = (
        _context().outcome_marker_reader
        or _pytest_node_outcome_markers
    )
    markers = set().union(
        *(marker_reader(str(node_id)) for node_id in node_ids)
    )
    execution_platforms = tuple(spec.get("execution_platforms", ()))
    if "xfail" in markers:
        return False
    if execution_platforms == ("all",):
        return not markers
    if execution_platforms == ("darwin",):
        return bool({"skip", "skipif"} & markers) and markers <= {
            "skip",
            "skipif",
        }
    return False


def _required_population_policy(spec: Mapping[str, Any]) -> str:
    execution_platforms = tuple(spec.get("execution_platforms", ()))
    if execution_platforms == ("all",):
        return "all_exact_nodes_required_no_skip_no_xfail"
    if execution_platforms == ("darwin",):
        return "all_exact_nodes_required_no_skip_no_xfail_on_darwin"
    raise ValueError("unsupported_requirement_execution_platform")


def _git_z_paths(*args: str) -> tuple[str, ...]:
    try:
        output = subprocess.check_output(
            ["git", *args, "-z"],
            cwd=_context().root,
        )
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return ()
    return tuple(part.decode("utf-8", errors="strict") for part in output.split(b"\0") if part)


def _phase1_execution_snapshot_paths() -> tuple[str, ...]:
    source_paths = set(_git_z_paths("ls-files"))
    source_paths.update(_git_z_paths("ls-files", "--others", "--exclude-standard"))
    exact_paths = {
        str(node).split("::", 1)[0]
        for spec in _context().requirement_specs
        for node in spec["node_ids"]
    }
    exact_paths.update(
        str(path)
        for spec in _context().requirement_specs
        for path in spec["candidate_paths"]
    )
    exact_paths.update(
        {
            "scripts/generate_phase0_governance_contracts.py",
            "scripts/generate_phase1_baseline_execution_evidence.py",
            "scripts/refresh_phase1_deep_audit_governance.py",
        }
    )
    code_prefixes = ("core/", "daemon/", "integrations/", "scripts/", "tests/")
    for relative in source_paths:
        if relative.startswith(code_prefixes):
            exact_paths.add(relative)
        elif "/" not in relative and relative.endswith((".py", ".toml", ".json", ".yaml", ".yml")):
            exact_paths.add(relative)
    return tuple(sorted(exact_paths))


def _phase1_path_identity(relative: str) -> dict[str, Any]:
    path = _context().root / relative
    try:
        metadata = path.lstat()
    except OSError:
        return {"path": relative, "kind": "missing"}
    if stat.S_ISLNK(metadata.st_mode):
        return {
            "path": relative,
            "kind": "symlink",
            "target": path.readlink().as_posix(),
        }
    if stat.S_ISREG(metadata.st_mode):
        try:
            digest = regular_file_sha256(path)
        except (DurableIOError, OSError):
            return {"path": relative, "kind": "unsafe"}
        return {
            "path": relative,
            "kind": "file",
            "sha256": digest,
        }
    return {"path": relative, "kind": "unsafe"}


def _phase1_execution_snapshot() -> dict[str, Any]:
    entries = [_phase1_path_identity(relative) for relative in _phase1_execution_snapshot_paths()]
    return {"path_count": len(entries), "sha256": _hash(entries)}


def phase1_execution_snapshot() -> dict[str, Any]:
    """Return the current exact candidate-tree identity used by Phase 1 evidence."""
    return _phase1_execution_snapshot()


def _phase1_git_blob(commit: str, relative: str) -> bytes | None:
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{relative}"],
        cwd=_context().root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode:
        return None
    return subprocess.check_output(
        ["git", "show", f"{commit}:{relative}"],
        cwd=_context().root,
    )


def _expected_phase1_mutation_changes(
    spec: Mapping[str, Any],
    baseline_commit: str,
    operator_id: str,
) -> list[dict[str, Any]]:
    replacements = {
        str(item["operator_id"]): item for item in spec.get("mutation_source_replacements", ())
    }
    replacement = replacements.get(operator_id)
    if replacement is None and isinstance(spec.get("mutation_source_replacement"), dict):
        replacement = spec["mutation_source_replacement"]
    if isinstance(replacement, dict):
        relative = str(replacement["path"])
        candidate = _stable_regular_bytes(_context().root / relative)
        if candidate is None:
            return []
        old = str(replacement["old"]).encode("utf-8")
        new = str(replacement["new"]).encode("utf-8")
        if candidate.count(old) != 1 or old == new:
            return []
        mutated = candidate.replace(old, new, 1)
        return [
            {
                "path": relative,
                "operation": "replace_exact_text",
                "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
                "mutated_sha256": hashlib.sha256(mutated).hexdigest(),
                "replacement_contract_sha256": _hash(
                    {"old": replacement["old"], "new": replacement["new"]}
                ),
            }
        ]
    changes: list[dict[str, Any]] = []
    for relative_value in spec.get("mutation_candidate_paths", ()):
        relative = str(relative_value)
        path = _context().root / relative
        candidate_bytes = _stable_regular_bytes(path)
        historical = _phase1_git_blob(baseline_commit, relative)
        if candidate_bytes == historical:
            continue
        changes.append(
            {
                "path": relative,
                "operation": "delete" if historical is None else "replace",
                "candidate_sha256": (
                    hashlib.sha256(candidate_bytes).hexdigest()
                    if candidate_bytes is not None
                    else None
                ),
                "historical_sha256": (
                    hashlib.sha256(historical).hexdigest() if historical is not None else None
                ),
            }
        )
    return changes


def _phase1_execution_nodes(execution: dict[str, Any]) -> set[str]:
    outcomes = execution.get("outcomes", {})
    if not isinstance(outcomes, dict):
        return set()
    return {
        str(node)
        for status in ("passed", "failed", "error", "skipped", "xfail", "xpass")
        for node in outcomes.get(status, [])
    }


def _phase1_execution_covers(
    execution: dict[str, Any],
    selected_nodes: set[str],
) -> bool:
    executed = _phase1_execution_nodes(execution)
    if not executed:
        return False
    if any(
        not any(node == selected or node.startswith(selected + "[") for selected in selected_nodes)
        for node in executed
    ):
        return False
    return all(
        any(node == selected or node.startswith(selected + "[") for node in executed)
        for selected in selected_nodes
    )


def _phase1_execution_has_noncredit(execution: dict[str, Any]) -> bool:
    outcomes = execution.get("outcomes", {})
    return any(outcomes.get(key) for key in ("error", "skipped", "xfail", "xpass"))


def _phase1_execution_has_valid_hre(execution: dict[str, Any]) -> bool:
    report = execution.get("hermetic_run", {})
    return bool(
        isinstance(report, dict)
        and set(report)
        == {
            "profile",
            "environment_hash",
            "outside_write_count",
            "formal_state_diff",
            "credentials_inherited",
            "manifest_verified",
            "manifest_integrity_digest",
        }
        and report.get("profile") == "isolated"
        and isinstance(report.get("environment_hash"), str)
        and len(report["environment_hash"]) == 64
        and report.get("outside_write_count") == 0
        and report.get("formal_state_diff") == []
        and report.get("credentials_inherited") is False
        and report.get("manifest_verified") is True
        and isinstance(report.get("manifest_integrity_digest"), str)
        and len(report["manifest_integrity_digest"]) == 64
    )


def phase1_execution_denominator_summary(
    runs: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive reference, unique-case, and semantic mutation denominators."""

    selected_node_references: list[str] = []
    candidate_passed_case_references: list[str] = []
    operator_ids: list[str] = []
    mutation_contract_hashes: list[str] = []
    contract_hashes_by_operator: dict[str, set[str]] = {}
    for run in runs.values():
        selected_node_references.extend(str(node) for node in run.get("selected_nodes", ()))
        candidate_execution = run.get("candidate_execution", {})
        candidate_outcomes = (
            candidate_execution.get("outcomes", {}) if isinstance(candidate_execution, dict) else {}
        )
        candidate_passed_case_references.extend(
            str(node) for node in candidate_outcomes.get("passed", ())
        )
        mutation_executions = run.get("mutation_executions", {})
        if not isinstance(mutation_executions, dict):
            continue
        for operator_id, result in mutation_executions.items():
            operator_text = str(operator_id)
            operator_ids.append(operator_text)
            mutation = result.get("mutation", {}) if isinstance(result, dict) else {}
            if not isinstance(mutation, dict):
                mutation = {}
            semantic_contract = {
                key: value for key, value in mutation.items() if key != "operator_id"
            }
            contract_hash = _hash(semantic_contract)
            mutation_contract_hashes.append(contract_hash)
            contract_hashes_by_operator.setdefault(operator_text, set()).add(contract_hash)
    distinct_contracts = set(mutation_contract_hashes)
    semantic_collision_ids = sorted(
        operator_id
        for operator_id, contract_hashes in contract_hashes_by_operator.items()
        if len(contract_hashes) > 1
    )
    return {
        "requirement_count": len(runs),
        "selected_node_reference_count": len(selected_node_references),
        "unique_selected_node_count": len(set(selected_node_references)),
        "candidate_passed_case_reference_count": len(candidate_passed_case_references),
        "unique_candidate_passed_case_count": len(set(candidate_passed_case_references)),
        "mutation_execution_count": len(operator_ids),
        "distinct_operator_id_count": len(set(operator_ids)),
        "distinct_mutation_contract_count": len(distinct_contracts),
        "duplicate_mutation_execution_count": (
            len(mutation_contract_hashes) - len(distinct_contracts)
        ),
        "operator_id_semantic_collision_count": len(semantic_collision_ids),
        "operator_id_semantic_collision_ids": semantic_collision_ids,
    }


def phase1_requirement_revalidation_summary(
    specs_value: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Describe the pre-generation denominator and separate post-generation gate."""

    specs = tuple(specs_value)
    requirement_ids = tuple(str(spec["requirement_id"]) for spec in specs)
    return {
        "coverage_scope": "phase1_root_revalidation",
        "requirement_ids": list(requirement_ids),
        "registered_count": len(requirement_ids),
        "exact_node_count": len(
            {str(node_id) for spec in specs for node_id in spec["node_ids"]}
        ),
        "post_generation_exact_node_count": sum(
            len(spec.get("post_generation_node_ids", ())) for spec in specs
        ),
        "post_generation_node_ids_by_requirement": {
            str(spec["requirement_id"]): list(spec.get("post_generation_node_ids", ()))
            for spec in specs
            if spec.get("post_generation_node_ids")
        },
        "population_policy_mode": "per_requirement_exact",
        "population_policy_by_requirement": {
            str(spec["requirement_id"]): _required_population_policy(spec) for spec in specs
        },
        "execution_platforms_by_requirement": {
            str(spec["requirement_id"]): list(spec["execution_platforms"]) for spec in specs
        },
        "test_lanes": sorted({str(lane) for spec in specs for lane in spec["test_lanes"]}),
        "required_pending_lanes": ["T5", "T6", "T7"],
    }


def _valid_phase1_execution_artifact(item: dict[str, Any]) -> bool:
    baseline_artifact = item.get("baseline_artifact")
    if not isinstance(baseline_artifact, dict):
        return False
    execution_ref = baseline_artifact.get("execution_artifact")
    if not isinstance(execution_ref, dict):
        return False
    relative = execution_ref.get("path")
    entry_key = execution_ref.get("entry_key")
    if (
        relative != "docs/acceptance/phase1_historical_defect_execution_evidence.json"
        or entry_key != item.get("requirement_id")
    ):
        return False
    path = _context().root / relative
    payload_bytes = _stable_regular_bytes(path)
    if payload_bytes is None:
        return False
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    if execution_ref.get("sha256") != payload_sha256:
        return False
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return False
    claimed_hash = payload.get("evidence_hash") if isinstance(payload, dict) else None
    unsigned = dict(payload) if isinstance(payload, dict) else {}
    unsigned.pop("evidence_hash", None)
    if payload.get(
        "schema_version"
    ) != "mnemos.phase1_historical_defect_execution_evidence.v4" or claimed_hash != _hash(unsigned):
        return False
    runs = payload.get("runs")
    if not isinstance(runs, dict) or payload.get(
        "denominator_summary"
    ) != phase1_execution_denominator_summary(runs):
        return False
    run = runs.get(entry_key)
    spec = next(
        (
            candidate_spec
            for candidate_spec in _context().requirement_specs
            if candidate_spec.get("requirement_id") == item.get("requirement_id")
        ),
        None,
    )
    if not isinstance(run, dict) or not isinstance(spec, dict):
        return False
    selected_nodes = set(run.get("selected_nodes", []))
    required_nodes = set(item.get("node_ids", []))
    operator_ids = set(run.get("mutation_operator_ids", []))
    required_operators = set(item.get("mutation_operator_ids", []))
    candidate = run.get("candidate_execution", {})
    candidate_outcomes = candidate.get("outcomes", {})
    mutation_executions = run.get("mutation_executions", {})
    snapshot_reader = (
        _context().execution_snapshot_reader
        or _phase1_execution_snapshot
    )
    current_snapshot = snapshot_reader()
    top_snapshot = payload.get("candidate_snapshot", {})
    expected_baseline = _context().baseline_commits.get(
        str(item.get("root_id"))
    )
    expected_changes_by_operator = (
        {
            operator_id: _expected_phase1_mutation_changes(
                spec,
                expected_baseline,
                operator_id,
            )
            for operator_id in operator_ids
        }
        if expected_baseline
        else {}
    )
    expected_oracles = [
        _phase1_path_identity(relative_path)
        for relative_path in sorted({str(node).split("::", 1)[0] for node in required_nodes})
    ]
    if (
        set(candidate)
        != {
            "execution_id",
            "exit_code",
            "outcomes",
            "executed_node_count",
            "hermetic_run",
        }
        or candidate.get("execution_id") != f"{entry_key}-candidate"
        or selected_nodes != required_nodes
        or operator_ids != required_operators
        or run.get("root_id") != item.get("root_id")
        or run.get("fault_model_ids") != item.get("fault_model_ids")
        or run.get("risk_scenario_ids") != item.get("risk_scenario_ids")
        or run.get("risk_scenario_evidence_role") != "non_credit_descriptive_risk_register"
        or item.get("risk_scenario_evidence_role") != "non_credit_descriptive_risk_register"
        or operator_ids != set(run.get("fault_model_ids", []))
        or run.get("mutation_oracle_node_ids") != item.get("mutation_oracle_node_ids")
        or run.get("mutation_oracle_node_ids_by_operator")
        != item.get("mutation_oracle_node_ids_by_operator")
        or not _phase1_execution_covers(candidate, selected_nodes)
        or candidate.get("exit_code") != 0
        or candidate_outcomes.get("failed")
        or _phase1_execution_has_noncredit(candidate)
        or not _phase1_execution_has_valid_hre(candidate)
        or candidate.get("executed_node_count") != len(candidate_outcomes.get("passed", []))
        or run.get("oracle_materialization") != expected_oracles
        or run.get("candidate_snapshot") != current_snapshot
        or {
            "path_count": top_snapshot.get("path_count"),
            "sha256": top_snapshot.get("sha256"),
        }
        != current_snapshot
        or top_snapshot.get("scope") != "phase1_code_tests_and_exact_candidate_artifacts"
        or not expected_changes_by_operator
        or any(not changes for changes in expected_changes_by_operator.values())
        or not isinstance(mutation_executions, dict)
        or set(mutation_executions) != operator_ids
        or run.get("kill_summary")
        != {
            "executed_operator_ids": sorted(operator_ids),
            "killed_operator_ids": sorted(operator_ids),
            "survived_operator_ids": [],
            "kill_rate_percent": 100,
        }
    ):
        return False
    for operator_id, result in mutation_executions.items():
        if not isinstance(result, dict):
            return False
        mutation = result.get("mutation")
        execution = result.get("execution", {})
        outcomes = execution.get("outcomes", {})
        source_replacements = {
            str(item["operator_id"]): item for item in spec.get("mutation_source_replacements", ())
        }
        source_replacement = source_replacements.get(operator_id)
        if source_replacement is None and isinstance(spec.get("mutation_source_replacement"), dict):
            source_replacement = spec["mutation_source_replacement"]
        expected_strategy = (
            "exact_source_replacement"
            if isinstance(source_replacement, dict)
            else "historical_implementation_revert"
        )
        expected_mutation_baseline = (
            None if isinstance(source_replacement, dict) else expected_baseline
        )
        required_killing_nodes = set(
            spec.get("mutation_oracle_node_ids_by_operator", {}).get(
                operator_id,
                (),
            )
        )
        failed_nodes = set(outcomes.get("failed", []))
        oracle_binding = {
            "declared_killing_node_ids": sorted(required_killing_nodes),
            "observed_failed_node_ids": sorted(failed_nodes),
        }
        killing_nodes_failed = all(
            any(failed == required or failed.startswith(required + "[") for failed in failed_nodes)
            for required in required_killing_nodes
        )
        if (
            set(result)
            != {
                "mutation",
                "mutation_hash",
                "oracle_binding",
                "execution",
                "status",
            }
            or set(execution)
            != {
                "execution_id",
                "exit_code",
                "outcomes",
                "executed_node_count",
                "hermetic_run",
            }
            or execution.get("execution_id") != f"{entry_key}-mutation-{operator_id}"
            or not isinstance(mutation, dict)
            or mutation.get("operator_id") != operator_id
            or mutation.get("strategy") != expected_strategy
            or mutation.get("baseline_commit") != expected_mutation_baseline
            or mutation.get("changed_artifacts") != expected_changes_by_operator.get(operator_id)
            or result.get("mutation_hash") != _hash(mutation)
            or result.get("oracle_binding") != oracle_binding
            or result.get("status") != "killed"
            or execution.get("exit_code") != 1
            or not outcomes.get("failed")
            or not required_killing_nodes
            or not required_killing_nodes <= selected_nodes
            or not killing_nodes_failed
            or _phase1_execution_has_noncredit(execution)
            or not _phase1_execution_has_valid_hre(execution)
            or not _phase1_execution_covers(
                execution,
                required_killing_nodes,
            )
            or execution.get("executed_node_count")
            != (len(outcomes.get("passed", [])) + len(outcomes.get("failed", [])))
        ):
            return False
    return True
