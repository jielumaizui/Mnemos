#!/usr/bin/env python3
"""Generate and validate the Phase 0 COG-046 governance projections."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.ops.durable_io import DurableIOError
from core.ops.durable_io import read_native_bytes
from core.utils import atomic_write_text
from scripts.phase0_governance_constants import (  # noqa: F401
    ACCEPTANCE,
    APPLIES_TO_ALL,
    CERTIFICATE_IDS,
    COG008_REDACTED_BASELINE_PATH,
    FINDING_OWNERS,
    FINDING_SUPPORT,
    GOVERNANCE_REFRESH_LOCK_PATH,
    GOVERNING_CONTRACT_ASSET_ID,
    GOVERNING_CONTRACT_PATH,
    GOVERNING_CONTRACT_PREDECESSOR_ASSET_ID,
    GOVERNING_CONTRACT_PREDECESSOR_PATH,
    GOVERNING_CONTRACT_PREDECESSOR_SHA256,
    HISTORICAL_BODY_HEADING,
    HISTORICAL_SOURCE_ASSET_ID,
    HISTORICAL_SOURCE_PATH,
    IMPORTED_CORPUS_END,
    IMPORTED_CORPUS_START,
    INDEPENDENT_DENOMINATOR_PATH,
    INVALIDATED_ROOTS,
    MIGRATION_PREFIXES,
    PHASE0_FOLLOWUP_RESIDUAL_DISPOSITIONS,
    PHASE0_LEDGER_PATH,
    PHASE1_BASELINE_COMMITS,
    PHASE1_CHANGED_TEST_NODE_IDS_BY_ROOT,
    PHASE1_CLOSURE_BOUNDARIES,
    PHASE1_EXPLICIT_SOURCE_MUTATIONS,
    PHASE1_IMMUTABLE_HISTORICAL_ARTIFACTS,
    PHASE1_LEDGER_PATH,
    PHASE1_MUTATION_ORACLE_NODES,
    PHASE1_POST_GENERATION_TEST_NODE_IDS_BY_ROOT,
    PHASE1_REMOVED_TEST_SUPERSESSIONS,
    PHASE1_REVALIDATION_BOUNDARY_OVERRIDES,
    PHASE1_REVALIDATION_SEQUENCE,
    PHASE1_ROOT_REQUIREMENT_SPECS,
    PHASE0_SUPPORT_REQUIREMENT_SPECS,
    REQUIREMENT_FIELDS,
    ROOT,
    ROOT_CHANGE_BUDGET_OVERRIDES,
    ROOT_ORDER,
    SCHEMA_VERSION,
    SUPPORT_WPS,
    SUPPORT_WP_PREREQUISITES,
)
from scripts.phase0_governance_inventory import (  # noqa: F401
    InventoryContext,
    PHASE1_REGISTERED_SCHEMA_OWNERS,
    _audit_artifact_inventory,
    _audit_artifact_paths,
    _canonical,
    _closure_evidence,
    _full_score_gate_ids,
    _git_blob_bytes,
    _hash,
    _historical_contract_body,
    _imported_contract_corpus,
    _imported_corpus_matches_historical,
    _imported_root_definition_ids,
    _independent_denominator,
    _migration_paths,
    _root_entries,
    _schema_inventory,
    _section_sha256,
    _shift_markdown_headings,
    _has_unique_ordered_section_anchors,
    inventory_scope,
)
from scripts.phase1_governance_ledger_validation import (
    LedgerValidationDependencies,
    validate_phase0_ledger_evidence as _validate_phase0_ledger_evidence_impl,
    validate_phase1_historical_artifacts as _validate_phase1_historical_artifacts_impl,
    validate_phase1_cog045_evidence as _validate_phase1_cog045_evidence_impl,
)
from scripts.phase1_governance_execution_validation import (  # noqa: F401
    ExecutionValidationContext,
    _expected_phase1_mutation_changes as _expected_phase1_mutation_changes_impl,
    _git_z_paths,
    _phase1_execution_covers,
    _phase1_execution_has_noncredit,
    _phase1_execution_has_valid_hre,
    _phase1_execution_nodes,
    _phase1_execution_snapshot as _phase1_execution_snapshot_impl,
    _phase1_execution_snapshot_paths,
    _phase1_git_blob,
    _phase1_path_identity as _phase1_path_identity_impl,
    _phase1_static_outcome_markers_are_valid as _phase1_static_outcome_markers_are_valid_impl,
    _pytest_node_ast as _pytest_node_ast_impl,
    _pytest_node_exists as _pytest_node_exists_impl,
    _pytest_node_has_assertion as _pytest_node_has_assertion_impl,
    _pytest_node_outcome_markers as _pytest_node_outcome_markers_impl,
    _required_population_policy,
    _unregistered_requirement,
    _valid_phase1_execution_artifact as _valid_phase1_execution_artifact_impl,
    execution_validation_scope,
    phase1_execution_denominator_summary as phase1_execution_denominator_summary_impl,
    phase1_requirement_revalidation_summary as phase1_requirement_revalidation_summary_impl,
)


def _read_bytes(path: Path) -> bytes:
    try:
        return read_native_bytes(path)
    except (DurableIOError, OSError):
        raise OSError("governance_source_unavailable") from None


def _read_text(path: Path) -> str:
    return _read_bytes(path).decode("utf-8")


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _sha256_path(path: Path) -> str | None:
    if not _is_regular_file(path):
        return None
    return hashlib.sha256(_read_bytes(path)).hexdigest()


def _optional_regular_text(path: Path) -> str | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("governance_target_preimage_unsafe")
    return _read_text(path)


def _ledger_validation_dependencies() -> LedgerValidationDependencies:
    return LedgerValidationDependencies(
        requirement_revalidation_is_current=(
            _phase1_requirement_revalidation_is_current
        ),
        execution_evidence_binding_is_current=(
            _phase1_execution_evidence_binding_is_current
        ),
        current_generation_artifact_paths=(
            phase1_current_generation_artifact_paths
        ),
        acceptance=ACCEPTANCE,
        root=ROOT,
        phase0_ledger_path=PHASE0_LEDGER_PATH,
        phase1_ledger_path=PHASE1_LEDGER_PATH,
        cog008_redacted_baseline_path=COG008_REDACTED_BASELINE_PATH,
        phase1_revalidation_sequence=tuple(PHASE1_REVALIDATION_SEQUENCE),
        phase1_closure_boundaries=dict(PHASE1_CLOSURE_BOUNDARIES),
        phase1_revalidation_boundary_overrides=dict(
            PHASE1_REVALIDATION_BOUNDARY_OVERRIDES
        ),
        immutable_historical_artifacts=dict(
            PHASE1_IMMUTABLE_HISTORICAL_ARTIFACTS
        ),
        independent_denominator=_independent_denominator,
        git_blob_bytes=_git_blob_bytes,
    )


def _validate_phase0_ledger_evidence(
    expected_assets: dict[Path, str],
) -> list[str]:
    return _validate_phase0_ledger_evidence_impl(
        expected_assets,
        dependencies=_ledger_validation_dependencies(),
    )


def _validate_phase1_historical_artifacts(
    ledger: dict[str, Any],
) -> list[str]:
    return _validate_phase1_historical_artifacts_impl(
        ledger,
        dependencies=_ledger_validation_dependencies(),
    )


def _validate_phase1_cog045_evidence(
    expected_assets: dict[Path, str],
) -> list[str]:
    return _validate_phase1_cog045_evidence_impl(
        expected_assets,
        dependencies=_ledger_validation_dependencies(),
    )


def _execution_context(
    *,
    outcome_marker_reader: Any = None,
    execution_snapshot_reader: Any = None,
) -> ExecutionValidationContext:
    return ExecutionValidationContext(
        root=ROOT,
        requirement_specs=tuple(PHASE1_ROOT_REQUIREMENT_SPECS),
        baseline_commits=dict(PHASE1_BASELINE_COMMITS),
        outcome_marker_reader=outcome_marker_reader,
        execution_snapshot_reader=execution_snapshot_reader,
    )


def _inventory_context() -> InventoryContext:
    return InventoryContext(
        root=ROOT,
        independent_denominator_path=INDEPENDENT_DENOMINATOR_PATH,
        phase0_ledger_path=PHASE0_LEDGER_PATH,
        phase1_ledger_path=PHASE1_LEDGER_PATH,
        root_order=tuple(ROOT_ORDER),
        finding_owners=dict(FINDING_OWNERS),
        phase1_closure_boundaries=dict(PHASE1_CLOSURE_BOUNDARIES),
        phase1_revalidation_boundary_overrides=dict(
            PHASE1_REVALIDATION_BOUNDARY_OVERRIDES
        ),
        phase1_revalidation_sequence=tuple(PHASE1_REVALIDATION_SEQUENCE),
    )


def _pytest_node_ast(node_id: str) -> Any:
    with execution_validation_scope(_execution_context()):
        return _pytest_node_ast_impl(node_id)


def _pytest_node_exists(node_id: str) -> bool:
    with execution_validation_scope(_execution_context()):
        return _pytest_node_exists_impl(node_id)


def _pytest_node_has_assertion(node_id: str) -> bool:
    with execution_validation_scope(_execution_context()):
        return _pytest_node_has_assertion_impl(node_id)


def _pytest_node_outcome_markers(node_id: str) -> set[str]:
    with execution_validation_scope(_execution_context()):
        return _pytest_node_outcome_markers_impl(node_id)


def _phase1_static_outcome_markers_are_valid(
    spec: Mapping[str, Any],
    node_ids: Iterable[str],
) -> bool:
    context = _execution_context(
        outcome_marker_reader=_pytest_node_outcome_markers
    )
    with execution_validation_scope(context):
        return _phase1_static_outcome_markers_are_valid_impl(
            spec,
            node_ids,
        )


def _phase1_path_identity(relative: str) -> dict[str, Any]:
    with execution_validation_scope(_execution_context()):
        return _phase1_path_identity_impl(relative)


def _phase1_execution_snapshot() -> dict[str, Any]:
    with execution_validation_scope(_execution_context()):
        return _phase1_execution_snapshot_impl()


def _expected_phase1_mutation_changes(
    spec: Mapping[str, Any],
    baseline_commit: str,
    operator_id: str,
) -> list[dict[str, Any]]:
    with execution_validation_scope(_execution_context()):
        return _expected_phase1_mutation_changes_impl(
            spec,
            baseline_commit,
            operator_id,
        )


def phase1_execution_denominator_summary(
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    with execution_validation_scope(_execution_context()):
        return phase1_execution_denominator_summary_impl(evidence)


def _valid_phase1_execution_artifact(
    item: dict[str, Any],
) -> bool:
    context = _execution_context(
        execution_snapshot_reader=_phase1_execution_snapshot
    )
    with execution_validation_scope(context):
        return _valid_phase1_execution_artifact_impl(item)


def _phase0_support_requirement_spec(
    requirement_id: str,
    work_package_id: str,
    node_ids: str | tuple[str, ...],
    *,
    candidate_paths: tuple[str, ...],
    fixture_id: str,
    semantic_contract: str,
    baseline_expected_failure: str,
    mutation_operator_ids: tuple[str, ...],
    production_artifact_type: str,
    invalidates: tuple[str, ...],
    risk_level: str = "P0",
    test_lanes: tuple[str, ...] = ("T2", "T3"),
    execution_platforms: tuple[str, ...] = ("all",),
    coverage_scope: str = "phase0_support_wp",
    baseline_execution_artifact: str = "",
) -> dict[str, Any]:
    risk_scenario_ids: tuple[str, ...] = ()
    required_pending_lanes: tuple[str, ...] = ()
    explicit_mutations: tuple[dict[str, str], ...] = ()
    if coverage_scope == "phase1_root_revalidation":
        risk_scenario_ids = mutation_operator_ids
        configured_mutations = PHASE1_EXPLICIT_SOURCE_MUTATIONS.get(requirement_id)
        if isinstance(configured_mutations, dict):
            explicit_mutations = (configured_mutations,)
        elif isinstance(configured_mutations, tuple):
            explicit_mutations = configured_mutations
        mutation_operator_ids = (
            tuple(str(item["operator_id"]) for item in explicit_mutations)
            if explicit_mutations
            else ("revert_declared_implementation_artifacts_to_historical_baseline",)
        )
        if any(str(node_id).startswith("tests/integration/") for node_id in node_ids):
            test_lanes = tuple(dict.fromkeys((*test_lanes, "T4")))
        required_pending_lanes = ("T5", "T6", "T7")
        baseline_execution_artifact = (
            "docs/acceptance/phase1_historical_defect_execution_evidence.json"
        )
    spec: dict[str, Any] = {
        "requirement_id": requirement_id,
        "work_package_id": work_package_id,
        "node_ids": (node_ids,) if isinstance(node_ids, str) else node_ids,
        "candidate_paths": candidate_paths,
        "fixture_id": fixture_id,
        "semantic_contract": semantic_contract,
        "baseline_expected_failure": baseline_expected_failure,
        "mutation_operator_ids": mutation_operator_ids,
        "production_artifact_type": production_artifact_type,
        "invalidates": invalidates,
        "risk_level": risk_level,
        "test_lanes": test_lanes,
        "execution_platforms": execution_platforms,
    }
    if coverage_scope != "phase0_support_wp":
        spec["coverage_scope"] = coverage_scope
    if risk_scenario_ids:
        spec["fault_model_ids"] = mutation_operator_ids
        spec["risk_scenario_ids"] = risk_scenario_ids
        spec["risk_scenario_evidence_role"] = "non_credit_descriptive_risk_register"
        configured_oracles = PHASE1_MUTATION_ORACLE_NODES.get(requirement_id, ())
        mutation_oracles_by_operator: dict[str, tuple[str, ...]]
        if isinstance(configured_oracles, dict):
            mutation_oracles_by_operator = {
                str(operator_id): (
                    (str(node_id),)
                    if isinstance(node_id, str)
                    else tuple(str(value) for value in node_id)
                )
                for operator_id, node_id in configured_oracles.items()
            }
        else:
            mutation_oracles_by_operator = {
                str(operator_id): tuple(configured_oracles)
                for operator_id in mutation_operator_ids
            }
        mutation_oracles = tuple(
            dict.fromkeys(
                node_id
                for operator_nodes in mutation_oracles_by_operator.values()
                for node_id in operator_nodes
            )
        )
        if (
            not mutation_oracles
            or set(mutation_oracles_by_operator) != set(mutation_operator_ids)
            or not set(mutation_oracles) <= set(spec["node_ids"])
        ):
            raise ValueError(f"invalid mutation oracle binding: {requirement_id}")
        spec["mutation_oracle_node_ids"] = mutation_oracles
        spec["mutation_oracle_node_ids_by_operator"] = {
            operator_id: tuple(nodes)
            for operator_id, nodes in mutation_oracles_by_operator.items()
        }
        if explicit_mutations:
            spec["mutation_candidate_paths"] = tuple(
                dict.fromkeys(str(item["path"]) for item in explicit_mutations)
            )
            spec["mutation_source_replacements"] = tuple(
                dict(item) for item in explicit_mutations
            )
            if len(explicit_mutations) == 1:
                spec["mutation_source_replacement"] = dict(explicit_mutations[0])
        else:
            spec["mutation_candidate_paths"] = tuple(
                path
                for path in candidate_paths
                if not path.startswith(("tests/", "docs/acceptance/"))
            )
    if required_pending_lanes:
        spec["required_pending_lanes"] = required_pending_lanes
    if baseline_execution_artifact:
        spec["baseline_execution_artifact"] = baseline_execution_artifact
    return spec


def phase1_requirement_revalidation_summary() -> dict[str, Any]:
    """Return the exact mixed-platform requirement summary for one generation."""
    return phase1_requirement_revalidation_summary_impl(PHASE1_ROOT_REQUIREMENT_SPECS)


def _phase1_requirement_revalidation_is_current(
    latest: Mapping[str, Any],
) -> bool:
    return latest.get("requirement_revalidation") == phase1_requirement_revalidation_summary()


def _phase1_execution_evidence_binding_is_current(
    latest: Mapping[str, Any],
    *,
    evidence_path: Path | None = None,
) -> bool:
    """Bind the latest append-only generation to the current execution evidence."""
    target = evidence_path or (ACCEPTANCE / "phase1_historical_defect_execution_evidence.json")
    try:
        payload = json.loads(_read_text(target))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False
    verification = latest.get("verification")
    return isinstance(verification, Mapping) and verification.get(
        "phase1_execution_evidence_hash"
    ) == payload.get("evidence_hash")


def phase1_current_generation_artifact_paths() -> tuple[str, ...]:
    paths = {
        str(path) for spec in PHASE1_ROOT_REQUIREMENT_SPECS for path in spec["candidate_paths"]
    }
    paths.update(
        str(node).split("::", 1)[0]
        for spec in PHASE1_ROOT_REQUIREMENT_SPECS
        for node in spec["node_ids"]
    )
    paths.update(
        str(spec["baseline_execution_artifact"])
        for spec in PHASE1_ROOT_REQUIREMENT_SPECS
        if spec.get("baseline_execution_artifact")
    )
    paths.update(
        {
            "scripts/generate_phase0_governance_contracts.py",
            "scripts/generate_phase1_baseline_execution_evidence.py",
            "scripts/refresh_phase1_deep_audit_governance.py",
            "tests/unit/ops/test_phase0_governance_contracts.py",
            "docs/acceptance/cognitive_phase0_independent_denominator.json",
            "docs/acceptance/document_asset_manifest.json",
            COG008_REDACTED_BASELINE_PATH,
            "docs/acceptance/phase1_historical_defect_execution_evidence.json",
            "docs/acceptance/schema_owner_manifest.json",
            "docs/acceptance/cognitive_requirement_test_manifest.json",
            "docs/acceptance/audit_artifact_registry.json",
            "docs/acceptance/cognitive_root_closures.jsonl",
            "docs/acceptance/cognitive_root_closure_index.json",
        }
    )
    return tuple(sorted(paths))


def _registered_phase0_requirement(
    requirement: dict[str, Any],
    spec: dict[str, Any],
) -> dict[str, Any]:
    node_ids = tuple(str(node_id) for node_id in spec["node_ids"])
    oracle_sources = [
        {
            "node_id": node_id,
            "path": node_id.split("::", 1)[0],
            "sha256": _sha256_path(ROOT / node_id.split("::", 1)[0]),
        }
        for node_id in node_ids
    ]
    oracle_symbol = node_ids[0] if len(node_ids) == 1 else "pytest-oracle-bundle:" + _hash(node_ids)
    oracle_source_hash = (
        oracle_sources[0]["sha256"] if len(oracle_sources) == 1 else _hash(oracle_sources)
    )
    fixture_id = str(spec["fixture_id"])
    fixture_contract = {
        "fixture_id": fixture_id,
        "requirement_id": requirement["requirement_id"],
        "semantic_contract": spec["semantic_contract"],
    }
    fixture_hash = _hash(fixture_contract)
    baseline_artifact = {
        "fixture_hash": fixture_hash,
        "expected_failure": spec["baseline_expected_failure"],
        "mutation_operator_ids": list(spec["mutation_operator_ids"]),
        "oracle_node_ids": list(node_ids),
        "oracle_source_hash": oracle_source_hash,
    }
    baseline_execution_path = str(spec.get("baseline_execution_artifact") or "")
    if baseline_execution_path:
        execution_path = ROOT / baseline_execution_path
        baseline_artifact["execution_artifact"] = {
            "path": baseline_execution_path,
            "entry_key": (
                str(spec["requirement_id"])
                if spec.get("coverage_scope") == "phase1_root_revalidation"
                else ""
            ),
            "sha256": (
                _sha256_path(execution_path)
            ),
        }
    candidate_artifacts = [
        {
            "path": relative,
            "sha256": (
                _sha256_path(ROOT / relative)
            ),
        }
        for relative in spec["candidate_paths"]
    ]
    candidate_hash = _hash(candidate_artifacts)
    registered = dict(requirement)
    registered.update(
        {
            "work_package_id": spec["work_package_id"],
            "coverage_scope": spec.get("coverage_scope", "phase0_support_wp"),
            "risk_level": spec["risk_level"],
            "runner_kind": "pytest",
            "entrypoint": "python",
            "argv": ["-m", "pytest", "-q"],
            "node_ids": list(node_ids),
            "fixture_id": fixture_id,
            "fixture_hash": fixture_hash,
            "fixture_contract": fixture_contract,
            "oracle_symbol": oracle_symbol,
            "oracle_source_hash": oracle_source_hash,
            "oracle_sources": oracle_sources,
            "baseline_expected_failure": spec["baseline_expected_failure"],
            "baseline_artifact": baseline_artifact,
            "baseline_artifact_ref": "sha256:" + _hash(baseline_artifact),
            "candidate_artifacts": candidate_artifacts,
            "candidate_artifact_ref": "sha256:" + candidate_hash,
            "test_lanes": list(spec["test_lanes"]),
            "mutation_operator_ids": list(spec["mutation_operator_ids"]),
            "execution_platforms": list(spec["execution_platforms"]),
            "required_population_policy": _required_population_policy(spec),
            "production_artifact_type": spec["production_artifact_type"],
            "invalidates": list(spec["invalidates"]),
            "status": "REGISTERED",
            "release_blocking": True,
        }
    )
    if spec.get("coverage_scope") == "phase1_root_revalidation":
        registered["post_generation_node_ids"] = list(
            spec.get("post_generation_node_ids", ())
        )
        registered["fault_model_ids"] = list(spec.get("fault_model_ids", ()))
        registered["risk_scenario_ids"] = list(spec.get("risk_scenario_ids", ()))
        registered["risk_scenario_evidence_role"] = spec.get("risk_scenario_evidence_role")
        registered["mutation_oracle_node_ids"] = list(spec.get("mutation_oracle_node_ids", ()))
        registered["mutation_oracle_node_ids_by_operator"] = {
            operator_id: list(nodes)
            for operator_id, nodes in spec.get(
                "mutation_oracle_node_ids_by_operator",
                {},
            ).items()
        }
        registered["mutation_candidate_paths"] = list(spec.get("mutation_candidate_paths", ()))
        if isinstance(spec.get("mutation_source_replacement"), dict):
            registered["mutation_source_replacement"] = dict(spec["mutation_source_replacement"])
        if spec.get("mutation_source_replacements"):
            registered["mutation_source_replacements"] = [
                dict(item) for item in spec["mutation_source_replacements"]
            ]
        lane_evidence = {
            "T2": "registered_exact_oracles",
            "T3": "registered_stateful_oracles",
        }
        if "T4" in spec["test_lanes"]:
            lane_evidence["T4"] = "registered_cross_module_oracles"
        registered["test_lane_evidence"] = lane_evidence
        registered["required_pending_lanes"] = list(spec.get("required_pending_lanes", ()))
    return registered


def _build_assets_impl() -> dict[Path, str]:
    """Build all generated Phase 0 governance assets in memory."""

    root_entries = _root_entries()
    root_ids = {item["root_id"] for item in root_entries}
    findings = [
        {
            "finding_id": finding_id,
            "direct_owner_roots": list(owners),
            "support_wps": list(FINDING_SUPPORT.get(finding_id, ())),
            "invalidated_downstream_roots": list(INVALIDATED_ROOTS.get(finding_id, ())),
            "applies_to_all": finding_id in APPLIES_TO_ALL,
            "status": "OPEN",
        }
        for finding_id, owners in FINDING_OWNERS.items()
    ]
    dag = {
        "schema_version": SCHEMA_VERSION,
        "parent_root_id": "COG-046",
        "root_count": 50,
        "support_wps": [
            {
                "work_package": wp,
                "parent_root_id": parent,
                "prerequisites": list(SUPPORT_WP_PREREQUISITES[wp]),
            }
            for wp, parent in SUPPORT_WPS.items()
        ],
        "roots": root_entries,
    }
    budgets = {
        "schema_version": "mnemos.cognitive_root_change_budgets.v1",
        "parent_root_id": "COG-046",
        "root_count": 50,
        "roots": [
            {
                "root_id": root_id,
                **{
                    "allowed_interface_delta": 0,
                    "allowed_schema_delta": 0,
                    "allowed_migration_delta": 0,
                    "allowed_gate_delta": 0,
                },
                **ROOT_CHANGE_BUDGET_OVERRIDES.get(root_id, {}),
                "required_docs": ["repo", "desktop", "source_record"],
                "expansion_requires_explicit_contract_update": True,
            }
            for root_id, _ in ROOT_ORDER
        ],
    }
    overlay = {
        "schema_version": "mnemos.cognitive_finding_overlay.v1",
        "parent_root_id": "COG-046",
        "finding_count": 38,
        "findings": findings,
    }
    closure_evidence = _closure_evidence()
    source_section_sha256 = _independent_denominator()["governing_source"]["section_14_7_sha256"]
    closure_rows = []
    for item in root_entries:
        root_id = item["root_id"]
        evidence = closure_evidence.get(root_id)
        closure_rows.append(
            {
                "schema_version": "mnemos.cognitive_root_closure_projection.v2",
                "root_id": root_id,
                "state": evidence["state"] if evidence else "NOT_REVALIDATED",
                "source_asset_id": GOVERNING_CONTRACT_ASSET_ID,
                "source_record": f"Desktop/{GOVERNING_CONTRACT_PATH.name}",
                "source_anchor": {
                    "asset_id": GOVERNING_CONTRACT_ASSET_ID,
                    "start": "### 14.7",
                    "end": "### 14.8",
                },
                "source_section_sha256": source_section_sha256,
                "historical_source_records": [
                    {
                        "asset_id": HISTORICAL_SOURCE_ASSET_ID,
                        "record": f"Desktop/{HISTORICAL_SOURCE_PATH.name}",
                        "governance_role": "historical_provenance",
                    }
                ],
                "machine_artifact": evidence["evidence_pointer"] if evidence else None,
                "machine_evidence_hash": evidence["evidence_hash"] if evidence else None,
                "contract_hash": item["contract_hash"],
                "generated": True,
            }
        )
    closure_jsonl = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in closure_rows
    )
    index = {
        "schema_version": "mnemos.cognitive_root_closure_index.v2",
        "parent_root_id": "COG-046",
        "root_count": 50,
        "source_asset_id": GOVERNING_CONTRACT_ASSET_ID,
        "source_section_sha256": source_section_sha256,
        "root_ids": sorted(root_ids),
        "closure_jsonl_sha256": hashlib.sha256(closure_jsonl.encode()).hexdigest(),
        "state_counts": {
            state: sum(row["state"] == state for row in closure_rows)
            for state in sorted({row["state"] for row in closure_rows})
        },
    }
    schema_inventory = _schema_inventory()
    schema_manifest = {
        "schema_version": "mnemos.schema_owner_manifest.v1",
        "parent_root_id": "COG-046",
        "discovery": (
            "all core/**/*.py, scripts/**/*.py and daemon/**/*.py files containing "
            "CREATE/ALTER/DROP TABLE/INDEX/VIEW/TRIGGER DDL"
        ),
        "inventory_count": len(schema_inventory),
        "unregistered_count": sum(
            item["owner_status"] == "UNREGISTERED" for item in schema_inventory
        ),
        "release_eligible": False,
        "entries": schema_inventory,
    }
    requirement_entries = [
        _unregistered_requirement(
            requirement_id=f"ROOT-{root_id}",
            root_id=root_id,
            finding_id=None,
            requirement_kind="root_coverage",
            test_lanes=[],
        )
        for root_id, _ in ROOT_ORDER
    ]
    requirement_entries.extend(
        _unregistered_requirement(
            requirement_id=f"FINDING-{finding_id}-{root_id}",
            root_id=root_id,
            finding_id=finding_id,
            requirement_kind="finding_owner_coverage",
            test_lanes=["T2", "T3"],
        )
        for finding_id, owners in FINDING_OWNERS.items()
        for root_id in owners
    )
    support_specs = {
        str(spec["requirement_id"]): spec
        for spec in (*PHASE0_SUPPORT_REQUIREMENT_SPECS, *PHASE1_ROOT_REQUIREMENT_SPECS)
    }
    requirement_entries = [
        (
            _registered_phase0_requirement(item, support_specs[item["requirement_id"]])
            if item["requirement_id"] in support_specs
            else item
        )
        for item in requirement_entries
    ]
    phase0_support_requirements = [
        item for item in requirement_entries if item.get("coverage_scope") == "phase0_support_wp"
    ]
    phase1_root_requirements = [
        item
        for item in requirement_entries
        if item.get("coverage_scope") == "phase1_root_revalidation"
    ]
    phase0_support_work_packages = {
        work_package_id: {
            "parent_root_id": parent_root_id,
            "requirement_count": sum(
                item.get("work_package_id") == work_package_id
                for item in phase0_support_requirements
            ),
            "registered_count": sum(
                item.get("work_package_id") == work_package_id
                and item.get("status") == "REGISTERED"
                for item in phase0_support_requirements
            ),
        }
        for work_package_id, parent_root_id in SUPPORT_WPS.items()
    }
    requirements = {
        "schema_version": "mnemos.cognitive_requirement_test_manifest.v1",
        "parent_root_id": "COG-046",
        "requirement_count": len(requirement_entries),
        "root_coverage_count": len(ROOT_ORDER),
        "finding_owner_coverage_count": sum(len(owners) for owners in FINDING_OWNERS.values()),
        "unregistered_count": sum(
            item.get("status") == "UNREGISTERED" for item in requirement_entries
        ),
        "phase0_support_requirement_count": len(phase0_support_requirements),
        "phase0_support_registered_count": sum(
            item.get("status") == "REGISTERED" for item in phase0_support_requirements
        ),
        "phase0_exact_node_count": sum(
            len(item.get("node_ids", ())) for item in phase0_support_requirements
        ),
        "phase1_revalidated_requirement_count": len(phase1_root_requirements),
        "phase1_revalidated_exact_node_count": sum(
            len(item.get("node_ids", [])) for item in phase1_root_requirements
        ),
        "phase1_post_generation_exact_node_count": sum(
            len(item.get("post_generation_node_ids", []))
            for item in phase1_root_requirements
        ),
        "phase0_support_coverage_percent": (
            100
            if phase0_support_requirements
            and all(item.get("status") == "REGISTERED" for item in phase0_support_requirements)
            else 0
        ),
        "phase0_support_work_packages": phase0_support_work_packages,
        "release_eligible": False,
        "requirements": requirement_entries,
    }
    artifact_entries = _audit_artifact_inventory()
    artifact_registry = {
        "schema_version": "mnemos.audit_artifact_registry.v1",
        "parent_root_id": "COG-046",
        "discovery": "scripts/audit_*.py plus mandatory scripts/security_audit.py",
        "artifact_count": len(artifact_entries),
        "unregistered_count": sum(
            item["validator_status"] == "UNREGISTERED" for item in artifact_entries
        ),
        "release_eligible": False,
        "artifacts": artifact_entries,
    }
    migrations = [
        {
            "migration_id": path.stem,
            "runner_path": str(path.relative_to(ROOT)),
            "status": "UNREGISTERED",
            "apply_requires_exact_authorization": True,
            "release_blocking": True,
        }
        for path in _migration_paths()
    ]
    migration_manifest = {
        "schema_version": "mnemos.cognitive_migration_manifest.v1",
        "parent_root_id": "COG-046",
        "discovery": "scripts reconciliation/migrate/rebuild/replay/backfill/repair/project/recompact entrypoints",
        "migration_count": len(migrations),
        "unregistered_count": len(migrations),
        "release_eligible": False,
        "migrations": migrations,
    }
    full_score_gate_ids = _full_score_gate_ids()
    release_manifest = {
        "schema_version": "mnemos.cognitive_release_manifest.v1",
        "parent_root_id": "COG-046",
        "release_eligible": False,
        "required_gate_denominator": {
            "manifest_id": "mnemos.full-score.strict-real-api.v1",
            "gate_count": len(full_score_gate_ids),
            "gate_ids": full_score_gate_ids,
            "gate_ids_sha256": _hash(full_score_gate_ids),
            "runner": "scripts/run_full_score_gates.py",
            "verifier": "scripts/verify_full_score_certificate.py",
            "status": "DENOMINATOR_FROZEN_NOT_EXECUTED",
        },
        "certificates": [
            {
                "certificate_id": certificate_id,
                "status": "MISSING",
                "required": True,
            }
            for certificate_id in CERTIFICATE_IDS
        ],
    }
    return {
        ACCEPTANCE
        / "cognitive_root_dag.json": json.dumps(dag, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        ACCEPTANCE
        / "cognitive_root_change_budgets.json": json.dumps(
            budgets, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
        ACCEPTANCE
        / "cognitive_finding_overlay.json": json.dumps(
            overlay, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
        ACCEPTANCE / "cognitive_root_closures.jsonl": closure_jsonl,
        ACCEPTANCE
        / "cognitive_root_closure_index.json": json.dumps(
            index, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
        ACCEPTANCE
        / "schema_owner_manifest.json": json.dumps(
            schema_manifest, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
        ACCEPTANCE
        / "cognitive_requirement_test_manifest.json": json.dumps(
            requirements, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
        ACCEPTANCE
        / "audit_artifact_registry.json": json.dumps(
            artifact_registry, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
        ACCEPTANCE
        / "cognitive_migration_manifest.json": json.dumps(
            migration_manifest, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
        ACCEPTANCE
        / "cognitive_release_manifest.json": json.dumps(
            release_manifest, ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n",
    }


def build_assets() -> dict[Path, str]:
    """Build all generated governance assets under one injected inventory view."""

    with inventory_scope(_inventory_context()):
        return _build_assets_impl()


def _expected_contract_authority_resolution() -> dict[str, str]:
    return {
        "record_type": "append_only_authority_resolution",
        "legacy_field": "audit_contract.desktop_report",
        "legacy_evidence_role": "historical_snapshot_not_current_gate",
        "legacy_asset_id": HISTORICAL_SOURCE_ASSET_ID,
        "current_active_asset_id": GOVERNING_CONTRACT_ASSET_ID,
        "current_active_path": GOVERNING_CONTRACT_PATH.name,
        "gate_owner": "document_asset_manifest.external_governing_assets",
    }


def _validate_predecessor_ledger_authority() -> list[str]:
    errors: list[str] = []
    expected_resolution = _expected_contract_authority_resolution()
    for ledger_path in (PHASE0_LEDGER_PATH, PHASE1_LEDGER_PATH):
        try:
            ledger = json.loads(_read_text(ledger_path))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"legacy ledger unavailable for authority resolution: {exc}")
            continue
        audit_contract = ledger.get("audit_contract")
        if (
            not isinstance(audit_contract, dict)
            or audit_contract.get("desktop_report") != HISTORICAL_SOURCE_PATH.name
            or ledger.get("contract_authority_resolution_20260724") != expected_resolution
        ):
            errors.append("missing legacy audit contract authority resolution")
    return errors


def _validate_assets_impl(*, desktop_mode: str = "required") -> list[str]:
    """Return fail-closed drift errors for Phase 0 governance assets."""

    if desktop_mode not in {"required", "skip"}:
        return [f"unsupported Desktop governance mode: {desktop_mode}"]
    require_desktop = desktop_mode == "required"
    errors: list[str] = []
    try:
        independent = _independent_denominator()
    except (OSError, json.JSONDecodeError) as exc:
        return [f"independent denominator unavailable: {exc}"]
    source_contract = independent.get("governing_source", {})
    if not isinstance(source_contract, dict) or {
        "asset_id": source_contract.get("asset_id"),
        "path": source_contract.get("path"),
        "governance_role": source_contract.get("governance_role"),
        "section_anchors": source_contract.get("section_anchors"),
    } != {
        "asset_id": GOVERNING_CONTRACT_ASSET_ID,
        "path": GOVERNING_CONTRACT_PATH.name,
        "governance_role": "current_active",
        "section_anchors": ["### 14.7", "### 14.8", "### 14.9"],
    }:
        errors.append("independent governing source identity mismatch")
    errors.extend(_validate_predecessor_ledger_authority())
    governed_hashes = independent.get("governed_hashes", {})
    governed_values = {
        "root_order": ROOT_ORDER,
        "finding_owners": FINDING_OWNERS,
        "support_wps": SUPPORT_WPS,
        "support_wp_prerequisites": SUPPORT_WP_PREREQUISITES,
        "phase0_support_requirement_specs": PHASE0_SUPPORT_REQUIREMENT_SPECS,
        "phase1_root_requirement_specs": PHASE1_ROOT_REQUIREMENT_SPECS,
        "phase1_revalidation_sequence": PHASE1_REVALIDATION_SEQUENCE,
        "phase1_closure_boundaries": PHASE1_CLOSURE_BOUNDARIES,
        "phase1_immutable_historical_artifacts": PHASE1_IMMUTABLE_HISTORICAL_ARTIFACTS,
        "phase1_current_generation_artifact_paths": (phase1_current_generation_artifact_paths()),
        "phase0_followup_residual_dispositions": (PHASE0_FOLLOWUP_RESIDUAL_DISPOSITIONS),
        "finding_support": FINDING_SUPPORT,
        "invalidated_roots": INVALIDATED_ROOTS,
        "applies_to_all": sorted(APPLIES_TO_ALL),
    }
    for name, value in governed_values.items():
        if _hash(value) != governed_hashes.get(name):
            errors.append(f"independent governed hash mismatch: {name}")
    expected = build_assets()
    for path, content in expected.items():
        if not _is_regular_file(path):
            errors.append(f"missing generated asset: {path.name}")
        elif _read_text(path) != content:
            errors.append(f"stale generated asset: {path.name}")
    errors.extend(_validate_phase1_cog045_evidence(expected))
    errors.extend(_validate_phase0_ledger_evidence(expected))
    root_ids = {root_id for root_id, _ in ROOT_ORDER}
    if len(ROOT_ORDER) != 50 or len(root_ids) != 50:
        errors.append("canonical Root denominator must be exactly 50 unique")
    if len(FINDING_OWNERS) != 38:
        errors.append("finding denominator must be exactly 38 unique")
    phase_positions: dict[str, list[int]] = {}
    for _, phase_order in ROOT_ORDER:
        match = re.fullmatch(r"(P[0-7])-(\d{2})", phase_order)
        if not match:
            errors.append(f"invalid phase order: {phase_order}")
            continue
        phase_positions.setdefault(match.group(1), []).append(int(match.group(2)))
    for phase, positions in phase_positions.items():
        if positions != list(range(1, len(positions) + 1)):
            errors.append(f"non-contiguous phase order: {phase}")
    for finding_id, owners in FINDING_OWNERS.items():
        unknown = set(owners) - root_ids
        if unknown:
            errors.append(f"{finding_id} has unknown direct owners: {sorted(unknown)}")
        unknown_downstream = set(INVALIDATED_ROOTS.get(finding_id, ())) - root_ids
        if unknown_downstream:
            errors.append(
                f"{finding_id} has unknown downstream roots: {sorted(unknown_downstream)}"
            )
        for wp in FINDING_SUPPORT.get(finding_id, ()):
            if wp not in SUPPORT_WPS:
                errors.append(f"{finding_id} has unowned support WP: {wp}")
    manifest = json.loads(
        _read_text(ACCEPTANCE / "cognitive_runtime_interface_manifest.json")
    )
    interface_ids = [
        item.get("interface_id")
        for item in manifest.get("interfaces", [])
        if isinstance(item, dict)
    ]
    identity_contract = independent.get("identity_denominators", {})
    interface_contract = identity_contract.get("runtime_interface_ids", {})
    if len(interface_ids) != interface_contract.get("count") or _hash(
        interface_ids
    ) != interface_contract.get("sha256"):
        errors.append("independent identity denominator mismatch: runtime_interface_ids")
    if len(set(interface_ids)) != 13:
        errors.append("runtime interface manifest denominator must be exactly 13")
    certificate_contract = identity_contract.get("certificate_ids", {})
    if len(CERTIFICATE_IDS) != certificate_contract.get("count") or _hash(
        CERTIFICATE_IDS
    ) != certificate_contract.get("sha256"):
        errors.append("independent identity denominator mismatch: certificate_ids")
    document_manifest = json.loads(
        _read_text(ACCEPTANCE / "document_asset_manifest.json")
    )
    external_assets = document_manifest.get("external_governing_assets", [])
    if external_assets != independent.get("external_governing_assets"):
        errors.append("external governing document contract mismatch")
    active_entry = (
        external_assets[0]
        if isinstance(external_assets, list)
        and len(external_assets) == 1
        and isinstance(external_assets[0], dict)
        else {}
    )
    if (
        not isinstance(external_assets, list)
        or len(external_assets) != 1
        or active_entry.get("asset_id") != GOVERNING_CONTRACT_ASSET_ID
        or active_entry.get("path") != GOVERNING_CONTRACT_PATH.name
        or active_entry.get("governance_role") != "current_active"
    ):
        errors.append("external governing owner count must be exactly one")
    expected_predecessor = {
        "asset_id": GOVERNING_CONTRACT_PREDECESSOR_ASSET_ID,
        "path": GOVERNING_CONTRACT_PREDECESSOR_PATH.name,
        "sha256": "sha256:" + GOVERNING_CONTRACT_PREDECESSOR_SHA256,
    }
    predecessor = active_entry.get("renamed_from")
    expected_supersedes = [
        HISTORICAL_SOURCE_ASSET_ID,
        GOVERNING_CONTRACT_PREDECESSOR_ASSET_ID,
    ]
    phase0_ledger = json.loads(_read_text(PHASE0_LEDGER_PATH))
    legacy_handoff_hash = (
        phase0_ledger.get("phase0_cog046_denominator_lock_20260724", {})
        .get("desktop_sync", {})
        .get("handoff_sha256")
    )
    if (
        predecessor != expected_predecessor
        or active_entry.get("supersedes") != expected_supersedes
        or legacy_handoff_hash != GOVERNING_CONTRACT_PREDECESSOR_SHA256
        or (require_desktop and GOVERNING_CONTRACT_PREDECESSOR_PATH.exists())
    ):
        errors.append("renamed predecessor provenance mismatch")
    historical_assets = document_manifest.get("external_historical_assets", [])
    if historical_assets != independent.get("external_historical_assets"):
        errors.append("external historical document contract mismatch")
    historical_entry = (
        historical_assets[0]
        if isinstance(historical_assets, list)
        and len(historical_assets) == 1
        and isinstance(historical_assets[0], dict)
        else {}
    )
    if (
        not isinstance(historical_assets, list)
        or len(historical_assets) != 1
        or historical_entry.get("asset_id") != HISTORICAL_SOURCE_ASSET_ID
        or historical_entry.get("path") != HISTORICAL_SOURCE_PATH.name
        or historical_entry.get("governance_role") != "historical_provenance"
        or historical_entry.get("gate_eligible") is not False
        or historical_entry.get("superseded_by") != GOVERNING_CONTRACT_ASSET_ID
    ):
        errors.append("historical source must be non-gating provenance")
    if require_desktop and not _is_regular_file(HISTORICAL_SOURCE_PATH):
        errors.append("missing frozen historical source")
    elif require_desktop:
        expected_historical_hash = (
            "sha256:" + str(_sha256_path(HISTORICAL_SOURCE_PATH))
        )
        if historical_entry.get("frozen_sha256") != expected_historical_hash:
            errors.append("frozen historical source hash mismatch")
    if require_desktop and not _is_regular_file(GOVERNING_CONTRACT_PATH):
        errors.append("missing current governing contract")
    elif require_desktop:
        if not _has_unique_ordered_section_anchors(
            GOVERNING_CONTRACT_PATH,
            ("### 14.7", "### 14.8", "### 14.9"),
        ):
            errors.append("governing source anchor cardinality mismatch")
        else:
            for section, start, end in (
                ("section_14_7_sha256", "### 14.7", "### 14.8"),
                ("section_14_8_sha256", "### 14.8", "### 14.9"),
            ):
                if _section_sha256(
                    GOVERNING_CONTRACT_PATH,
                    start,
                    end,
                ) != source_contract.get(section):
                    errors.append(f"governing source section drift: {section}")
        imported_ids = _imported_root_definition_ids(GOVERNING_CONTRACT_PATH)
        expected_root_ids = {root_id for root_id, _ in ROOT_ORDER}
        if len(imported_ids) != 50 or set(imported_ids) != expected_root_ids:
            errors.append("active contract imported Root denominator mismatch")
        elif _section_sha256(
            GOVERNING_CONTRACT_PATH,
            IMPORTED_CORPUS_START,
            IMPORTED_CORPUS_END,
        ) != source_contract.get("imported_contract_corpus_sha256"):
            errors.append("governing source section drift: imported_contract_corpus_sha256")
        if not _imported_corpus_matches_historical(
            GOVERNING_CONTRACT_PATH,
            HISTORICAL_SOURCE_PATH,
        ):
            errors.append("imported contract corpus is not equivalent to frozen historical source")
    schema_paths = [entry["path"] for entry in _schema_inventory()]
    migration_paths = [str(path.relative_to(ROOT)) for path in _migration_paths()]
    artifact_paths = [str(path.relative_to(ROOT)) for path in _audit_artifact_paths()]
    discovered = {
        "schema_ddl_paths": schema_paths,
        "migration_paths": migration_paths,
        "audit_artifact_paths": artifact_paths,
        "full_score_gate_ids": _full_score_gate_ids(),
    }
    inventory_contract = independent.get("inventory_denominators", {})
    for name, values in discovered.items():
        contract = inventory_contract.get(name, {})
        if len(values) != contract.get("count") or _hash(values) != contract.get("sha256"):
            errors.append(f"independent inventory denominator mismatch: {name}")
    requirement_fields = set(REQUIREMENT_FIELDS)
    requirements = json.loads(expected[ACCEPTANCE / "cognitive_requirement_test_manifest.json"])[
        "requirements"
    ]
    if any(not requirement_fields <= set(item) for item in requirements):
        errors.append("requirement manifest is missing mandatory 15.2 fields")
    root_requirement_ids = {
        item.get("root_id")
        for item in requirements
        if item.get("requirement_kind") == "root_coverage"
    }
    if root_requirement_ids != root_ids:
        errors.append("requirement manifest Root coverage is incomplete")
    expected_finding_pairs = {
        (finding_id, root_id) for finding_id, owners in FINDING_OWNERS.items() for root_id in owners
    }
    actual_finding_pairs = {
        (item.get("finding_id"), item.get("root_id"))
        for item in requirements
        if item.get("requirement_kind") == "finding_owner_coverage"
    }
    if actual_finding_pairs != expected_finding_pairs:
        errors.append("requirement manifest finding-owner coverage is incomplete")
    if any(
        not {"T2", "T3"} <= set(item.get("test_lanes", []))
        for item in requirements
        if item.get("requirement_kind") == "finding_owner_coverage"
    ):
        errors.append("finding requirements must expose T2 and T3 lanes")
    phase0_parent_roots = set(SUPPORT_WPS.values())
    expected_phase0_support_ids = {
        *(f"ROOT-{root_id}" for root_id in phase0_parent_roots),
        *(
            f"FINDING-{finding_id}-{root_id}"
            for finding_id, owners in FINDING_OWNERS.items()
            for root_id in owners
            if root_id in phase0_parent_roots
        ),
    }
    phase0_support_requirements = [
        item for item in requirements if item.get("coverage_scope") == "phase0_support_wp"
    ]
    actual_phase0_support_ids = {item.get("requirement_id") for item in phase0_support_requirements}
    spec_ids = [str(spec.get("requirement_id")) for spec in PHASE0_SUPPORT_REQUIREMENT_SPECS]
    if (
        len(PHASE0_SUPPORT_REQUIREMENT_SPECS) != 14
        or len(set(spec_ids)) != 14
        or actual_phase0_support_ids != expected_phase0_support_ids
        or any(item.get("status") != "REGISTERED" for item in phase0_support_requirements)
    ):
        errors.append("Phase 0 support-WP requirement coverage is incomplete")
    for item in phase0_support_requirements:
        node_ids = item.get("node_ids")
        work_package_id = item.get("work_package_id")
        fixture_contract = item.get("fixture_contract")
        spec = next(
            (
                candidate
                for candidate in PHASE0_SUPPORT_REQUIREMENT_SPECS
                if candidate.get("requirement_id") == item.get("requirement_id")
            ),
            None,
        )
        expected_node_ids = (
            [str(node_id) for node_id in spec["node_ids"]] if isinstance(spec, dict) else None
        )
        expected_oracle_sources = (
            [
                {
                    "node_id": node_id,
                    "path": node_id.split("::", 1)[0],
                    "sha256": _sha256_path(
                        ROOT / node_id.split("::", 1)[0]
                    ),
                }
                for node_id in expected_node_ids
            ]
            if expected_node_ids is not None
            else None
        )
        expected_oracle_symbol = (
            expected_node_ids[0]
            if expected_node_ids is not None and len(expected_node_ids) == 1
            else (
                "pytest-oracle-bundle:" + _hash(tuple(expected_node_ids))
                if expected_node_ids
                else None
            )
        )
        expected_oracle_source_hash = (
            expected_oracle_sources[0]["sha256"]
            if expected_oracle_sources is not None and len(expected_oracle_sources) == 1
            else (_hash(expected_oracle_sources) if expected_oracle_sources else None)
        )
        expected_candidate_artifacts = (
            [
                {
                    "path": relative,
                    "sha256": _sha256_path(ROOT / relative),
                }
                for relative in spec["candidate_paths"]
            ]
            if isinstance(spec, dict)
            else None
        )
        baseline_artifact = item.get("baseline_artifact")
        expected_population_policy = (
            _required_population_policy(spec) if isinstance(spec, dict) else None
        )
        outcome_markers = (
            set().union(*(_pytest_node_outcome_markers(str(node_id)) for node_id in node_ids))
            if isinstance(node_ids, list)
            else {"missing"}
        )
        expected_baseline_artifact = (
            {
                "fixture_hash": _hash(fixture_contract),
                "expected_failure": spec["baseline_expected_failure"],
                "mutation_operator_ids": list(spec["mutation_operator_ids"]),
                "oracle_node_ids": expected_node_ids,
                "oracle_source_hash": expected_oracle_source_hash,
            }
            if (
                isinstance(spec, dict)
                and isinstance(fixture_contract, dict)
                and expected_node_ids
                and expected_oracle_source_hash is not None
            )
            else None
        )
        if (
            work_package_id not in SUPPORT_WPS
            or SUPPORT_WPS.get(str(work_package_id)) != item.get("root_id")
            or item.get("runner_kind") != "pytest"
            or item.get("entrypoint") != "python"
            or item.get("argv") != ["-m", "pytest", "-q"]
            or not isinstance(node_ids, list)
            or not node_ids
            or len(node_ids) != len(set(node_ids))
            or node_ids != expected_node_ids
            or any(not _pytest_node_exists(str(node_id)) for node_id in node_ids)
            or any(not _pytest_node_has_assertion(str(node_id)) for node_id in node_ids)
            or "xfail" in outcome_markers
            or (
                isinstance(spec, dict)
                and spec["execution_platforms"] == ("all",)
                and bool({"skip", "skipif"} & outcome_markers)
            )
            or (
                isinstance(spec, dict)
                and spec["execution_platforms"] == ("darwin",)
                and not bool({"skip", "skipif"} & outcome_markers)
            )
            or item.get("oracle_symbol") != expected_oracle_symbol
            or item.get("oracle_source_hash") != expected_oracle_source_hash
            or item.get("oracle_sources") != expected_oracle_sources
            or not isinstance(fixture_contract, dict)
            or item.get("fixture_hash") != _hash(fixture_contract)
            or not item.get("mutation_operator_ids")
            or not item.get("baseline_expected_failure")
            or not isinstance(spec, dict)
            or item.get("execution_platforms") != list(spec["execution_platforms"])
            or item.get("required_population_policy") != expected_population_policy
            or not isinstance(baseline_artifact, dict)
            or baseline_artifact != expected_baseline_artifact
            or item.get("baseline_artifact_ref") != "sha256:" + _hash(baseline_artifact)
            or expected_candidate_artifacts is None
            or any(
                not isinstance(candidate.get("sha256"), str)
                for candidate in expected_candidate_artifacts
            )
            or item.get("candidate_artifacts") != expected_candidate_artifacts
            or item.get("candidate_artifact_ref") != "sha256:" + _hash(expected_candidate_artifacts)
        ):
            errors.append(
                "invalid Phase 0 support-WP requirement: " + str(item.get("requirement_id"))
            )
    phase1_specs = {str(spec["requirement_id"]): spec for spec in PHASE1_ROOT_REQUIREMENT_SPECS}
    phase1_requirements = [
        item for item in requirements if item.get("coverage_scope") == "phase1_root_revalidation"
    ]
    if {item.get("requirement_id") for item in phase1_requirements} != set(phase1_specs) or any(
        item.get("status") != "REGISTERED" for item in phase1_requirements
    ):
        errors.append("Phase 1 Root requirement coverage is incomplete")
    for item in phase1_requirements:
        spec = phase1_specs.get(str(item.get("requirement_id")))
        node_ids = item.get("node_ids")
        expected_lane_evidence = {
            "T2": "registered_exact_oracles",
            "T3": "registered_stateful_oracles",
        }
        if isinstance(spec, dict) and "T4" in spec["test_lanes"]:
            expected_lane_evidence["T4"] = "registered_cross_module_oracles"
        if (
            not isinstance(spec, dict)
            or item.get("work_package_id") != spec.get("work_package_id")
            or item.get("runner_kind") != "pytest"
            or item.get("entrypoint") != "python"
            or item.get("argv") != ["-m", "pytest", "-q"]
            or not isinstance(node_ids, list)
            or not node_ids
            or node_ids != list(spec["node_ids"])
            or len(node_ids) != len(set(node_ids))
            or any(not _pytest_node_exists(str(node_id)) for node_id in node_ids)
            or any(not _pytest_node_has_assertion(str(node_id)) for node_id in node_ids)
            or not _phase1_static_outcome_markers_are_valid(spec, node_ids)
            or not item.get("baseline_expected_failure")
            or not item.get("mutation_operator_ids")
            or item.get("mutation_operator_ids") != list(spec["mutation_operator_ids"])
            or item.get("post_generation_node_ids")
            != list(spec.get("post_generation_node_ids", ()))
            or item.get("fault_model_ids") != list(spec.get("fault_model_ids", ()))
            or item.get("risk_scenario_ids") != list(spec.get("risk_scenario_ids", ()))
            or item.get("risk_scenario_evidence_role") != spec.get("risk_scenario_evidence_role")
            or item.get("mutation_oracle_node_ids")
            != list(spec.get("mutation_oracle_node_ids", ()))
            or item.get("mutation_oracle_node_ids_by_operator")
            != {
                operator_id: list(nodes)
                for operator_id, nodes in spec.get(
                    "mutation_oracle_node_ids_by_operator",
                    {},
                ).items()
            }
            or item.get("mutation_candidate_paths")
            != list(spec.get("mutation_candidate_paths", ()))
            or item.get("mutation_source_replacement") != spec.get("mutation_source_replacement")
            or item.get("mutation_source_replacements")
            != (
                [dict(value) for value in spec["mutation_source_replacements"]]
                if spec.get("mutation_source_replacements")
                else None
            )
            or item.get("test_lanes") != list(spec["test_lanes"])
            or item.get("test_lane_evidence") != expected_lane_evidence
            or item.get("required_pending_lanes") != list(spec.get("required_pending_lanes", ()))
            or not _valid_phase1_execution_artifact(item)
            or item.get("required_population_policy") != _required_population_policy(spec)
        ):
            errors.append("invalid Phase 1 Root requirement: " + str(item.get("requirement_id")))
    closure_evidence = _closure_evidence()
    for root_id, expected_state in independent.get("closure_states", {}).items():
        evidence = closure_evidence.get(root_id)
        if not evidence or evidence.get("state") != expected_state:
            errors.append(f"invalid Phase 0 closure ledger state: {root_id}")
    return errors


def validate_assets(*, desktop_mode: str = "required") -> list[str]:
    """Validate governance assets under the same injected inventory view."""

    with inventory_scope(_inventory_context()):
        return _validate_assets_impl(desktop_mode=desktop_mode)


def _restore_generated_preimages(
    originals: dict[Path, str | None],
    order: list[Path],
) -> None:
    """Restore every generator target after a failed locked publication."""
    for path in reversed(order):
        content = originals[path]
        if content is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write_text(path, content, encoding="utf-8")


def _write_assets_transactionally(*, desktop_mode: str) -> list[str]:
    """Publish all generated assets under one rollback boundary."""
    expected = build_assets()
    originals: dict[Path, str | None] = {}
    order: list[Path] = []
    try:
        for path, content in expected.items():
            originals[path] = _optional_regular_text(path)
            order.append(path)
            atomic_write_text(path, content, encoding="utf-8")
        errors = validate_assets(desktop_mode=desktop_mode)
        if errors:
            _restore_generated_preimages(originals, order)
        return errors
    except BaseException:
        _restore_generated_preimages(originals, order)
        raise


def main() -> int:
    """Write and validate Phase 0 governance projections."""
    from scripts.phase0_governance_cli import (
        GovernanceCliDependencies,
        main as cli_main,
    )

    return cli_main(
        GovernanceCliDependencies(
            governance_refresh_lock_path=GOVERNANCE_REFRESH_LOCK_PATH,
            validate_assets=validate_assets,
            write_assets_transactionally=_write_assets_transactionally,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
