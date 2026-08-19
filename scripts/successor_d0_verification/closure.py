"""Private implementation module for successor_d0_verification.closure."""

from __future__ import annotations

from collections import Counter

from pathlib import Path

from typing import Any

from typing import Mapping

from typing import Sequence

import hashlib

import json

import stat

from .census import (
    _independent_static_census,
)

from .wire import (
    ALLOWED_RELATIONS,
    Finding,
    REQUIRED_BINDING_KINDS,
    REQUIRED_INDEPENDENT_INVENTORY_FAMILIES,
    _canonical_json_bytes,
    _finding,
    _record_contract_errors,
    _sha256,
)


def _verify_independent_census(
    snapshot_root: Path,
    records: Mapping[str, Sequence[Mapping[str, Any]]],
    findings: list[Finding],
    *,
    inventory_metrics: object = None,
) -> dict[str, Any]:
    try:
        census = _independent_static_census(snapshot_root)
    except (
        OSError,
        UnicodeError,
        SyntaxError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        findings.append(
            _finding(
                "ENUMERATOR_FAILED",
                "independent_inventory",
                str(exc),
                "repair the independent snapshot enumerator before any D0 verification",
            )
        )
        return {"ok": False, "diffs": {"enumerator": str(exc)}}

    requirements = records.get("requirements", [])
    capabilities = records.get("capabilities", [])
    surfaces = records.get("surfaces", [])
    tests = records.get("tests_oracles", [])
    surface_by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for record in surfaces:
        kind = record.get("kind")
        if isinstance(kind, str):
            surface_by_kind.setdefault(kind, []).append(record)
    oracle_by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for record in tests:
        kind = record.get("kind")
        if isinstance(kind, str):
            oracle_by_kind.setdefault(kind, []).append(record)

    cli_record_to_command = {
        str(record.get("record_id")): str(record.get("canonical_selector", "")).removeprefix("cli:")
        for record in surface_by_kind.get("cli", [])
    }
    actual_cli_facet_counts: dict[str, int] = {
        command: 0 for command in cli_record_to_command.values()
    }
    actual_cli_facet_signatures: list[str] = []
    action_names = {
        "_StoreTrueAction": "store_true",
        "_StoreFalseAction": "store_false",
        "_AppendAction": "append",
        "_AppendConstAction": "append_const",
        "_CountAction": "count",
        "_StoreConstAction": "store_const",
        "_StoreAction": None,
    }
    for record in surface_by_kind.get("cli_argument_facet", []):
        command = cli_record_to_command.get(str(record.get("surface_family_id")), "")
        if command:
            actual_cli_facet_counts[command] = actual_cli_facet_counts.get(command, 0) + 1
            facet = record.get("facet_contract")
            if isinstance(facet, dict):
                action = facet.get("action")
                signature = {
                    "command": command,
                    "option_strings": facet.get("option_strings"),
                    "dest": facet.get("dest"),
                    "action": action_names.get(str(action), action),
                    "choices": facet.get("choices"),
                    "required": facet.get("required"),
                    "nargs": facet.get("nargs"),
                }
                actual_cli_facet_signatures.append(
                    _canonical_json_bytes(signature).decode("utf-8").rstrip("\n")
                )

    def facet_values(kind: str, field_name: str) -> list[str]:
        values: list[str] = []
        for record in surface_by_kind.get(kind, []):
            facet = record.get("facet_contract")
            if isinstance(facet, dict) and facet.get(field_name) is not None:
                values.append(str(facet[field_name]))
        return sorted(values)

    def oracle_discovery(kind: str, prefix: str) -> list[str]:
        return sorted(
            str(record.get("discovery_key", "")).removeprefix(prefix)
            for record in oracle_by_kind.get(kind, [])
        )

    mcp_records = surface_by_kind.get("mcp_tool", [])
    mcp_names = facet_values("mcp_tool", "tool_name")
    mcp_categories = sorted(
        str(record.get("facet_contract", {}).get("tool_name"))
        for record in mcp_records
        if isinstance(record.get("facet_contract"), dict)
        and record.get("facet_contract", {}).get("category")
    )
    mcp_registered = sorted(
        str(record.get("facet_contract", {}).get("tool_name"))
        for record in mcp_records
        if isinstance(record.get("facet_contract"), dict)
        and record.get("facet_contract", {}).get("registered") is True
    )
    mcp_policies = sorted(
        str(record.get("facet_contract", {}).get("tool_name"))
        for record in mcp_records
        if isinstance(record.get("facet_contract"), dict)
        and record.get("facet_contract", {}).get("policy") is not None
    )
    actual_mcp_tool_signatures = sorted(
        _canonical_json_bytes(
            {
                "name": record.get("facet_contract", {}).get("tool_name"),
                "input_schema_sha256": record.get("facet_contract", {}).get("input_schema_sha256"),
                "registered": record.get("facet_contract", {}).get("registered"),
                "category": record.get("facet_contract", {}).get("category"),
                "policy": record.get("facet_contract", {}).get("policy"),
            }
        )
        .decode("utf-8")
        .rstrip("\n")
        for record in mcp_records
        if isinstance(record.get("facet_contract"), dict)
    )
    console_actual = sorted(
        f"{record.get('facet_contract', {}).get('name')}="
        f"{record.get('facet_contract', {}).get('target')}"
        for record in surface_by_kind.get("console_script", [])
        if isinstance(record.get("facet_contract"), dict)
    )
    event_records = [
        *surface_by_kind.get("event_subscription", []),
        *surface_by_kind.get("event_subscription_wildcard", []),
    ]
    actual_event_path_topics = sorted(
        f"{record.get('facet_contract', {}).get('path')}:"
        f"{record.get('facet_contract', {}).get('event_type')}:"
        f"{record.get('kind')}"
        for record in event_records
        if isinstance(record.get("facet_contract"), dict)
    )
    actual_coverage_edge_contracts = sorted(
        _canonical_json_bytes(
            {
                "assertion_authority": edge.get("assertion_authority"),
                "facet": edge.get("facet"),
                "from_id": edge.get("from_id"),
                "relation": edge.get("relation"),
                "to_id": edge.get("to_id"),
            }
        )
        .decode("utf-8")
        .rstrip("\n")
        for edge in records.get("coverage_edges", [])
    )
    expected_event_path_topics = sorted(
        f"{path}:{event}:"
        f"{'event_subscription_wildcard' if event == '*' else 'event_subscription'}"
        for item in census["event_subscription_edges"]
        for path, _line, event in [item.rsplit(":", 2)]
    )
    expected_script_classification = {
        "script_entry": census["script_main"],
        "script_unguarded_wrapper": census["script_import_time_effect_candidates"],
        "script_helper": sorted(
            set(census["script_helper"]) - set(census["script_import_time_effect_candidates"])
        ),
    }
    comparisons = {
        "requirement_ids": (
            census["requirement_ids"],
            sorted(
                str(record.get("legacy_requirement_id"))
                for record in requirements
                if record.get("legacy_requirement_id")
            ),
        ),
        "legacy_feature_ids": (
            census["legacy_feature_ids"],
            sorted(
                str(record.get("legacy_feature_id"))
                for record in capabilities
                if record.get("legacy_feature_id")
            ),
        ),
        "pytest_files": (
            census["pytest_files"],
            sorted(
                str(record.get("source_path"))
                for record in tests
                if record.get("kind") == "pytest_file" and record.get("source_path")
            ),
        ),
        "script_modules": (
            census["script_modules"],
            sorted(
                str(record.get("source_path"))
                for record in surfaces
                if record.get("kind") == "script_module" and record.get("source_path")
            ),
        ),
        "source_ids": (
            census["source_ids"],
            sorted(
                str(record.get("selector"))
                for record in surfaces
                if record.get("kind") == "agent_source" and record.get("selector")
            ),
        ),
        "schema_owner_paths": (
            census["schema_owner_paths"],
            sorted(
                str(record.get("source_path"))
                for record in surfaces
                if record.get("kind") == "schema_owner_seed" and record.get("source_path")
            ),
        ),
        "schema_reverse_paths": (
            census["schema_reverse_paths"],
            sorted(
                str(record.get("source_path"))
                for record in surface_by_kind.get("schema_owner_seed", [])
                if record.get("source_path")
            ),
        ),
        "console_scripts": (
            sorted(f"{name}={target}" for name, target in census["console_script_map"].items()),
            console_actual,
        ),
        "cli_leaves": (
            census["cli_leaves"],
            sorted(cli_record_to_command.values()),
        ),
        "cli_effective_facets_by_leaf": (
            sorted(
                f"{command}={count}"
                for command, count in census["cli_leaf_argument_counts"].items()
            ),
            sorted(f"{command}={count}" for command, count in actual_cli_facet_counts.items()),
        ),
        "cli_effective_facet_contracts": (
            census["cli_leaf_argument_signatures"],
            sorted(actual_cli_facet_signatures),
        ),
        "cli_dispatch_routes": (
            census["cli_dispatch_all"],
            facet_values("cli_dispatch_route", "command"),
        ),
        "daemon_cli_modes": (
            census["daemon_cli_modes"],
            facet_values("daemon_mode", "mode"),
        ),
        "daemon_controlled_mode_facets": (
            census["daemon_controlled_modes"],
            facet_values("daemon_mode_facet", "mode"),
        ),
        "mcp_tools": (census["mcp_schema_tools"], mcp_names),
        "mcp_registered_tools": (census["mcp_registered_tools"], mcp_registered),
        "mcp_categorized_tools": (census["mcp_categorized_tools"], mcp_categories),
        "mcp_policy_tools": (census["mcp_policy_tools"], mcp_policies),
        "mcp_tool_contracts": (
            census["mcp_tool_signatures"],
            actual_mcp_tool_signatures,
        ),
        "mcp_protocol_methods": (
            census["mcp_protocol_methods"],
            facet_values("mcp_protocol", "method"),
        ),
        "application_facade": (
            census["facade_methods"],
            facet_values("application_facade", "method"),
        ),
        "daemon_services": (
            census["daemon_intervals"],
            facet_values("daemon_service", "service_name"),
        ),
        "daemon_service_aliases": (
            census["daemon_aliases"],
            facet_values("daemon_service_alias", "service_name"),
        ),
        "chronos_steps": (
            census["chronos_steps"],
            facet_values("chronos_step", "step_name"),
        ),
        "event_policy_persistent": (
            census["event_policy_persistent"],
            sorted(
                str(record.get("facet_contract", {}).get("event_type"))
                for record in surface_by_kind.get("event_policy", [])
                if isinstance(record.get("facet_contract"), dict)
                and record.get("facet_contract", {}).get("persistence_policy") == "persistent"
            ),
        ),
        "event_policy_no_persist": (
            census["event_policy_no_persist"],
            sorted(
                str(record.get("facet_contract", {}).get("event_type"))
                for record in surface_by_kind.get("event_policy", [])
                if isinstance(record.get("facet_contract"), dict)
                and record.get("facet_contract", {}).get("persistence_policy") == "no_persist"
            ),
        ),
        "event_subscription_path_topics": (
            expected_event_path_topics,
            actual_event_path_topics,
        ),
        "dynamic_trigger_registries": (
            census["dynamic_trigger_selectors"],
            sorted(
                str(record.get("canonical_selector"))
                for record in surface_by_kind.get("dynamic_trigger_registry", [])
            ),
        ),
        "health_checks": (
            census["health_checks"],
            facet_values("health_check", "check_id"),
        ),
        "kia_modules": (
            census["kia_modules"],
            facet_values("kia_module", "module_id"),
        ),
        "repo_entry_challengers": (
            census["repo_entry_challenger_paths"],
            facet_values("repo_entry_challenger", "path"),
        ),
        "requirement_pytest_nodes": (
            census["canonical_requirement_node_ids"],
            oracle_discovery("requirement_pytest_node", "pytest-node:"),
        ),
        "behavior_scenarios": (
            census["behavior_scenario_ids"],
            oracle_discovery("behavior_scenario_bundle", "behavior-scenario:"),
        ),
        "ops_resilience_controls": (
            census["ops_resilience_control_ids"],
            oracle_discovery("ops_resilience_control", "ops-resilience:"),
        ),
        "function_validation_commands": (
            census["function_validation_discovery_keys"],
            sorted(
                str(record.get("discovery_key"))
                for record in oracle_by_kind.get("function_validation_command", [])
            ),
        ),
        "runtime_interfaces": (
            census["runtime_interface_ids"],
            oracle_discovery("runtime_target_effect_oracle", "runtime-interface:"),
        ),
        "audit_artifacts": (
            census["audit_artifact_ids"],
            oracle_discovery("audit_artifact", "audit-artifact:"),
        ),
        "release_gates": (
            census["release_gate_ids"],
            oracle_discovery("release_gate", "release-gate:"),
        ),
        "release_certificates": (
            census["release_certificate_ids"],
            oracle_discovery("release_certificate", "release-certificate:"),
        ),
        "coverage_edge_multiset": (
            census["expected_coverage_edge_contracts"],
            actual_coverage_edge_contracts,
        ),
    }
    for classification, expected in expected_script_classification.items():
        comparisons[f"scripts_{classification}"] = (
            expected,
            sorted(
                str(record.get("source_path"))
                for record in surface_by_kind.get("script_module", [])
                if record.get("script_classification") == classification
                and record.get("source_path")
            ),
        )
    diffs: dict[str, dict[str, list[str]]] = {}
    for label, (expected, actual) in comparisons.items():
        expected_counter = Counter(expected)
        actual_counter = Counter(actual)
        missing = sorted((expected_counter - actual_counter).elements())
        extra = sorted((actual_counter - expected_counter).elements())
        if missing or extra:
            diffs[label] = {
                "missing": missing,
                "extra": extra,
            }
            finding_code = (
                "EDGE_INVENTORY_DIFF"
                if label == "coverage_edge_multiset"
                else "INDEPENDENT_INVENTORY_DIFF"
            )
            findings.append(
                _finding(
                    finding_code,
                    label,
                    f"expected={len(expected)} actual={len(actual)} missing={len(missing)} extra={len(extra)}",
                    (
                        "regenerate coverage edges from the frozen source census"
                        if label == "coverage_edge_multiset"
                        else "repair the catalog Adapter or adjudicate the exact challenger inventory"
                    ),
                )
            )
    expected_inventory_metrics = {
        "main_cli": {
            "enumeration_mode": "pure_ast_no_legacy_execution",
            "parameter_definition_basis": "source_defined_non_help_actions",
            "command_node_count": len(census["cli_nodes"]),
            "top_command_count": len(census["cli_top"]),
            "leaf_count": len(census["cli_leaves"]),
            **census["cli_parameter_counts"],
            **census["cli_effective_facet_counts"],
        },
        "mcp": {
            "tool_union_count": len(census["mcp_tool_union"]),
            "registered_tool_count": len(census["mcp_registered_tools"]),
            "schema_tool_count": len(census["mcp_schema_tools"]),
            "categorized_tool_count": len(census["mcp_categorized_tools"]),
            "policy_tool_count": len(census["mcp_policy_tools"]),
            "category_registry_gap_tool_names": sorted(
                set(census["mcp_tool_union"]) - set(census["mcp_categorized_tools"])
            ),
        },
    }
    if inventory_metrics != expected_inventory_metrics:
        expected_metric_json = _canonical_json_bytes(expected_inventory_metrics).decode().rstrip()
        actual_metric_json = _canonical_json_bytes(inventory_metrics).decode().rstrip()
        diffs["manifest_inventory_metrics"] = {
            "missing": [expected_metric_json],
            "extra": [actual_metric_json],
        }
        findings.append(
            _finding(
                "INDEPENDENT_INVENTORY_DIFF",
                "inventory_metrics",
                "manifest inventory_metrics differs from the independently rebuilt census",
                "derive the complete inventory metric object from the frozen source census",
            )
        )
    examined_families = sorted(comparisons)
    verified_families = sorted(set(comparisons) - set(diffs))
    pending_families = sorted(REQUIRED_INDEPENDENT_INVENTORY_FAMILIES)
    findings.append(
        _finding(
            "INDEPENDENT_INVENTORY_INCOMPLETE",
            "independent_inventory",
            "the v1 challenger does not enumerate the five required reverse "
            f"state/effect families: {', '.join(pending_families)}",
            "implement each fixed family in a new freeze-capable schema and "
            "compare its exact multiset independently",
        )
    )
    return {
        "ok": False,
        "complete": False,
        "examined_families": examined_families,
        "verified_families": verified_families,
        "pending_families": pending_families,
        "counts": {key: len(value) for key, value in census.items() if isinstance(value, list)},
        "expected_manifest_inventory_metrics": expected_inventory_metrics,
        "diffs": diffs,
    }


def _verify_edges(
    records: Mapping[str, Sequence[Mapping[str, Any]]], findings: list[Finding]
) -> None:
    id_to_artifact = {
        str(record.get("record_id")): artifact_id
        for artifact_id, artifact_records in records.items()
        if artifact_id != "coverage_edges"
        for record in artifact_records
    }
    expected_types = {
        "REQUIREMENT_SATISFIED_BY_CAPABILITY": ("requirements", "capabilities"),
        "SURFACE_EXPOSES_CAPABILITY": ("surfaces", "capabilities"),
        "CAPABILITY_VERIFIED_BY_ORACLE": ("capabilities", "tests_oracles"),
    }
    expected_authority = {
        "REQUIREMENT_SATISFIED_BY_CAPABILITY": "DECLARED_LEGACY",
        "SURFACE_EXPOSES_CAPABILITY": "MECHANICAL",
        "CAPABILITY_VERIFIED_BY_ORACLE": "DECLARED_LEGACY",
    }
    for edge in records.get("coverage_edges", []):
        edge_id = str(edge.get("record_id") or "")
        relation = edge.get("relation")
        if relation not in ALLOWED_RELATIONS:
            findings.append(
                _finding(
                    "INVALID_RECORD",
                    "coverage_edges",
                    f"unknown relation {relation!r}",
                    "use only the three canonical D0 coverage relations",
                    record_id=edge_id or None,
                )
            )
        identity = {
            "from_id": edge.get("from_id"),
            "relation": relation,
            "to_id": edge.get("to_id"),
            "facet": edge.get("facet"),
        }
        canonical_identity = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        expected_edge_id = (
            "edge:sha256:" + hashlib.sha256(canonical_identity.encode("utf-8")).hexdigest()
        )
        expected_discovery_key = (
            f"edge:{relation}:{edge.get('from_id')}:{edge.get('to_id')}:" f"{edge.get('facet')}"
        )
        if edge_id != expected_edge_id:
            findings.append(
                _finding(
                    "INVALID_RECORD",
                    "coverage_edges",
                    f"edge identity is not derivable from its exact relation tuple: {edge_id!r}",
                    "regenerate record_id from from_id/relation/to_id/facet",
                    record_id=edge_id or None,
                )
            )
        if edge.get("discovery_key") != expected_discovery_key:
            findings.append(
                _finding(
                    "INVALID_RECORD",
                    "coverage_edges",
                    "edge discovery_key is not derivable from its exact relation tuple",
                    "regenerate discovery_key from from_id/relation/to_id/facet",
                    record_id=edge_id or None,
                )
            )
        if (
            relation in expected_authority
            and edge.get("assertion_authority") != expected_authority[relation]
        ):
            findings.append(
                _finding(
                    "INVALID_RECORD",
                    "coverage_edges",
                    f"{relation} must use assertion_authority={expected_authority[relation]}",
                    "restore the fixed v1 assertion authority",
                    record_id=edge_id or None,
                )
            )
        for field in ("from_id", "to_id"):
            endpoint = edge.get(field)
            if not isinstance(endpoint, str) or endpoint not in id_to_artifact:
                findings.append(
                    _finding(
                        "EDGE_ENDPOINT_MISSING",
                        "coverage_edges",
                        f"{field}={endpoint!r} is not present in the exact bundle",
                        "restore the endpoint or remove the unsupported candidate edge",
                        record_id=edge_id or None,
                    )
                )
        if relation in expected_types:
            from_type, to_type = expected_types[str(relation)]
            actual_from = id_to_artifact.get(str(edge.get("from_id") or ""))
            actual_to = id_to_artifact.get(str(edge.get("to_id") or ""))
            if actual_from is not None and actual_from != from_type:
                findings.append(
                    _finding(
                        "INVALID_RECORD",
                        "coverage_edges",
                        f"{relation} from_id must reference {from_type}, got {actual_from}",
                        "restore the canonical relation direction and endpoint types",
                        record_id=edge_id or None,
                    )
                )
            if actual_to is not None and actual_to != to_type:
                findings.append(
                    _finding(
                        "INVALID_RECORD",
                        "coverage_edges",
                        f"{relation} to_id must reference {to_type}, got {actual_to}",
                        "restore the canonical relation direction and endpoint types",
                        record_id=edge_id or None,
                    )
                )


def _verify_global_identity(
    records: Mapping[str, Sequence[Mapping[str, Any]]],
    findings: list[Finding],
) -> None:
    for field_name, code in (
        ("record_id", "DUPLICATE_RECORD_ID"),
        ("discovery_key", "DUPLICATE_DISCOVERY_KEY"),
    ):
        owners: dict[str, list[str]] = {}
        for artifact_id, artifact_records in records.items():
            for record in artifact_records:
                value = record.get(field_name)
                if isinstance(value, str) and value:
                    owners.setdefault(value, []).append(artifact_id)
        for value, artifact_ids in sorted(owners.items()):
            if len(artifact_ids) <= 1:
                continue
            findings.append(
                _finding(
                    code,
                    "bundle",
                    f"global duplicate {field_name} {value!r} in {artifact_ids}",
                    "assign globally unique identities and regenerate all five books",
                    record_id=value if field_name == "record_id" else None,
                )
            )


def _verify_snapshot_evidence(
    snapshot_root: Path,
    records: Mapping[str, Sequence[Mapping[str, Any]]],
    findings: list[Finding],
) -> None:
    root = snapshot_root.resolve()
    digest_cache: dict[str, str | None] = {}
    for artifact_id, artifact_records in records.items():
        for record in artifact_records:
            record_id = str(record.get("record_id") or "") or None
            references: list[Mapping[str, Any]] = []
            evidence_refs = record.get("evidence_refs")
            if isinstance(evidence_refs, list):
                references.extend(item for item in evidence_refs if isinstance(item, dict))
            source_anchors = record.get("source_anchors")
            if isinstance(source_anchors, list):
                references.extend(item for item in source_anchors if isinstance(item, dict))
            for reference in references:
                relative = reference.get("path")
                digest = reference.get("sha256")
                if not isinstance(relative, str) or not relative:
                    continue
                candidate = snapshot_root.joinpath(*Path(relative).parts)
                try:
                    candidate.resolve(strict=False).relative_to(root)
                except (OSError, ValueError):
                    findings.append(
                        _finding(
                            "EVIDENCE_REF_INVALID",
                            artifact_id,
                            f"evidence path escapes snapshot: {relative!r}",
                            "use one exact legacy-repository-relative evidence path",
                            record_id=record_id,
                        )
                    )
                    continue
                if relative in digest_cache:
                    actual = digest_cache[relative]
                else:
                    try:
                        metadata = candidate.lstat()
                    except FileNotFoundError:
                        digest_cache[relative] = None
                        metadata = None
                    if metadata is not None and (
                        not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
                    ):
                        findings.append(
                            _finding(
                                "EVIDENCE_REF_INVALID",
                                artifact_id,
                                f"evidence path is not a regular file: {relative}",
                                "bind an exact regular Git blob",
                                record_id=record_id,
                            )
                        )
                        digest_cache[relative] = None
                        continue
                    actual = _sha256(candidate.read_bytes()) if metadata is not None else None
                    digest_cache[relative] = actual
                if actual is None:
                    if digest is None and record.get("record_status") == "STALE_SOURCE":
                        continue
                    findings.append(
                        _finding(
                            "EVIDENCE_REF_INVALID",
                            artifact_id,
                            f"evidence path is missing: {relative}",
                            "restore the bound source or mark a truthful stale-source record",
                            record_id=record_id,
                        )
                    )
                    continue
                if digest != actual:
                    findings.append(
                        _finding(
                            "EVIDENCE_REF_INVALID",
                            artifact_id,
                            f"evidence hash mismatch for {relative}",
                            "regenerate evidence refs from the immutable snapshot",
                            record_id=record_id,
                        )
                    )


def _source_anchors(record: Mapping[str, Any]) -> Sequence[Any]:
    value = record.get("source_anchors")
    return value if isinstance(value, list) else ()


def _independent_closure_counts(
    records: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    source_bindings: object,
    config_snapshot: object,
    generator_findings: object,
    independent_inventory_diff: int,
) -> dict[str, int]:
    requirements = list(records.get("requirements", ()))
    surfaces = list(records.get("surfaces", ()))
    capabilities = list(records.get("capabilities", ()))
    oracles = list(records.get("tests_oracles", ()))
    edges = list(records.get("coverage_edges", ()))
    by_relation: dict[str, list[Mapping[str, Any]]] = {}
    for edge in edges:
        relation = edge.get("relation")
        if isinstance(relation, str):
            by_relation.setdefault(relation, []).append(edge)
    requirement_links = by_relation.get("REQUIREMENT_SATISFIED_BY_CAPABILITY", [])
    surface_links = by_relation.get("SURFACE_EXPOSES_CAPABILITY", [])
    oracle_links = by_relation.get("CAPABILITY_VERIFIED_BY_ORACLE", [])
    covered_requirements = {str(edge.get("from_id")) for edge in requirement_links}
    required_capabilities = {str(edge.get("to_id")) for edge in requirement_links}
    surfaced_capabilities = {str(edge.get("to_id")) for edge in surface_links}
    mapped_surfaces = {str(edge.get("from_id")) for edge in surface_links}
    capability_oracles = {
        (str(edge.get("from_id")), str(edge.get("to_id"))) for edge in oracle_links
    }
    linked_oracles = {oracle_id for _capability_id, oracle_id in capability_oracles}
    unresolved_subject_ids = {
        str(requirement.get("record_id"))
        for requirement in requirements
        if str(requirement.get("record_id")) not in covered_requirements
    }
    unresolved_subject_ids.update(str(capability.get("record_id")) for capability in capabilities)
    unresolved_subject_ids.update(
        str(surface.get("record_id"))
        for surface in surfaces
        if str(surface.get("record_id")) not in mapped_surfaces
        or surface.get("kind") == "cli_argument_facet"
        or surface.get("kind") == "repo_entry_challenger"
        or (
            surface.get("kind") == "script_module"
            and (
                surface.get("script_classification") == "script_helper"
                or surface.get("input_contract_ref") == "argparse:unknown"
            )
        )
        or surface.get("kind") in {"dynamic_trigger_registry", "event_subscription_wildcard"}
    )
    unresolved_subject_ids.update(
        str(oracle.get("record_id"))
        for oracle in oracles
        if str(oracle.get("record_id")) not in linked_oracles
    )
    constitution_ids = {
        str(requirement.get("requirement_id"))
        for requirement in requirements
        if requirement.get("kind") == "SUCCESSOR_CONSTITUTION"
    }
    declared_missing_test_paths = {
        str(anchor.get("path"))
        for oracle in oracles
        for anchor in _source_anchors(oracle)
        if isinstance(anchor, dict)
        and isinstance(anchor.get("path"), str)
        and str(anchor.get("path")).startswith("tests/")
        and anchor.get("sha256") is None
    }
    test_declaration_kinds = {
        "requirement_pytest_node",
        "behavior_scenario_bundle",
        "ops_resilience_control",
        "function_validation_command",
    }
    declared_test_paths = {
        str(anchor.get("path"))
        for oracle in oracles
        if oracle.get("kind") in test_declaration_kinds
        for anchor in _source_anchors(oracle)
        if isinstance(anchor, dict)
        and isinstance(anchor.get("path"), str)
        and str(anchor.get("path")).startswith("tests/")
        and anchor.get("sha256") is not None
    }
    test_file_paths = [
        str(oracle.get("source_path"))
        for oracle in oracles
        if oracle.get("kind") == "pytest_file" and isinstance(oracle.get("source_path"), str)
    ]
    test_file_linked_declaration = sum(path in declared_test_paths for path in test_file_paths)
    test_file_unlinked = len(test_file_paths) - test_file_linked_declaration

    all_records = [record for book in records.values() for record in book]
    id_counts: dict[str, int] = {}
    discovery_counts: dict[str, int] = {}
    for record in all_records:
        record_id = record.get("record_id")
        discovery_key = record.get("discovery_key")
        if isinstance(record_id, str) and record_id:
            id_counts[record_id] = id_counts.get(record_id, 0) + 1
        if isinstance(discovery_key, str) and discovery_key:
            discovery_counts[discovery_key] = discovery_counts.get(discovery_key, 0) + 1
    invalid_records = sum(
        bool(_record_contract_errors(artifact_id, record))
        for artifact_id, artifact_records in records.items()
        for record in artifact_records
    )
    bindings = source_bindings if isinstance(source_bindings, list) else []
    bound_ids = {
        str(binding.get("binding_id"))
        for binding in bindings
        if isinstance(binding, dict)
        and binding.get("required") is True
        and binding.get("status") == "BOUND"
    }
    missing_bindings = len(set(REQUIRED_BINDING_KINDS) - bound_ids)
    if not isinstance(config_snapshot, dict) or config_snapshot.get("mode") != "EXACT_FILE":
        missing_bindings += 1
    findings = generator_findings if isinstance(generator_findings, list) else []
    return {
        "freeze_evaluator_unimplemented": 1,
        "surface_unmapped": sum(
            str(surface.get("record_id")) not in mapped_surfaces for surface in surfaces
        ),
        "behavior_without_surface": sum(
            str(capability.get("record_id")) not in surfaced_capabilities
            for capability in capabilities
        ),
        "requirement_without_capability_or_adjudication": sum(
            str(requirement.get("record_id")) not in covered_requirements
            for requirement in requirements
        ),
        "capability_without_requirement_or_adjudication": sum(
            str(capability.get("record_id")) not in required_capabilities
            for capability in capabilities
        ),
        "capability_without_independent_test_or_oracle": len(capabilities),
        "test_without_capability_or_adjudication": sum(
            oracle.get("kind") != "pytest_file"
            and str(oracle.get("record_id")) not in linked_oracles
            for oracle in oracles
        ),
        "test_file_without_disposition": test_file_unlinked,
        "declared_missing_test_file": len(declared_missing_test_paths),
        "canonical_owner_unknown": len(capabilities)
        + sum(
            surface.get("kind") == "schema_owner_seed"
            and (
                not isinstance(surface.get("facet_contract"), dict)
                or surface.get("facet_contract", {}).get("owner_status") != "REGISTERED"
            )
            for surface in surfaces
        ),
        "effect_target_unknown": len(capabilities),
        "parameter_mode_unclassified": sum(
            surface.get("kind") == "cli_argument_facet" for surface in surfaces
        ),
        "script_entry_unclassified": sum(
            surface.get("kind") == "repo_entry_challenger"
            or (
                surface.get("kind") == "script_module"
                and surface.get("script_classification") != "script_entry"
            )
            for surface in surfaces
        ),
        "script_parameter_contract_unknown": sum(
            surface.get("kind") == "script_module"
            and surface.get("script_classification") == "script_entry"
            and surface.get("input_contract_ref") == "argparse:unknown"
            for surface in surfaces
        ),
        "dynamic_trigger_unclassified": sum(
            surface.get("kind") in {"dynamic_trigger_registry", "event_subscription_wildcard"}
            for surface in surfaces
        ),
        "contract_conflict_unresolved": sum(
            record.get("record_status") == "CONFLICT" for record in all_records
        ),
        "effective_capability_excluded": sum(
            capability.get("legacy_behavior_state") == "removed" for capability in capabilities
        ),
        "independent_inventory_diff": independent_inventory_diff,
        "independent_inventory_pending_family": len(REQUIRED_INDEPENDENT_INVENTORY_FAMILIES),
        "missing_required_source_binding": missing_bindings,
        "config_applicability_attestation_gap": 1,
        "constitution_requirement_missing": len(
            {"SUCCESSOR-CONSTITUTION-001", "SUCCESSOR-CONSTITUTION-002"} - constitution_ids
        ),
        "constitution_approval_missing": 1,
        "duplicate_record_id": sum(count > 1 for count in id_counts.values()),
        "duplicate_discovery_key": sum(count > 1 for count in discovery_counts.values()),
        "invalid_record": invalid_records,
        "generator_error": sum(
            isinstance(finding, dict)
            and finding.get("code") in {"ENUMERATOR_FAILED", "SNAPSHOT_MUTATED_BY_ENUMERATOR"}
            for finding in findings
        ),
        "unresolved_adjudication": len(unresolved_subject_ids),
        "test_file_denominator": len(test_file_paths),
        "test_file_linked_declaration": test_file_linked_declaration,
        "test_file_unlinked": test_file_unlinked,
    }
