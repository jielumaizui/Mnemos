"""Private implementation module for successor_d0_generation.builder."""

from __future__ import annotations

from collections import defaultdict

from pathlib import Path

from typing import Any

from typing import Mapping

from typing import Sequence

import os

import re

import stat

import tempfile

from .cli_inventory import (
    _collect_cli_dispatch_surfaces,
    _collect_console_script_surfaces,
    _collect_daemon_modes,
    _collect_static_cli_surfaces,
)

from .contract_inventory import (
    _collect_capabilities,
    _collect_oracles,
    _collect_requirements,
    _successor_constitution_requirement_records,
)

from .model import (
    ALLOWED_RECORD_STATUSES,
    ARTIFACT_ORDER,
    ARTIFACT_SCHEMAS,
    CatalogBundle,
    CatalogInputError,
    CatalogRequest,
    MANIFEST_SCHEMA,
    MAX_ARTIFACT_BYTES,
    MAX_EXTERNAL_BINDING_BYTES,
    MAX_JSONL_LINE_BYTES,
    MAX_JSONL_RECORDS,
    MAX_MANIFEST_BYTES,
    REQUIRED_INDEPENDENT_INVENTORY_FAMILIES,
    REQUIRED_ZERO_FIELDS,
    SUCCESSOR_CONSTITUTION_REQUIREMENT_IDS,
    V1_DISCOVERY_ONLY_MISSING_MECHANISMS,
    V1_ORACLE_INDEPENDENCE_CLASSES,
    _COMMON_RECORD_FIELDS,
    _edge,
    _set_sha256,
    canonical_json,
    canonical_json_bytes,
    sha256_bytes,
)

from .repository_inventory import (
    _collect_external_entry_challengers,
    _collect_schema_surfaces,
    _collect_script_surfaces,
)

from .runtime_inventory import (
    _collect_chronos_surfaces,
    _collect_daemon_services,
    _collect_event_surfaces,
    _collect_facade_surfaces,
    _collect_health_surfaces,
    _collect_kia_surfaces,
    _collect_mcp_surfaces,
    _collect_source_surfaces,
)

from .snapshot import (
    _CatalogContext,
    _archived_snapshot,
    _source_bindings,
)

_GENERATOR_IMPLEMENTATION_PATHS = tuple(
    sorted(
        {
            "scripts/generate_successor_d0_catalog.py",
            "scripts/successor_d0_catalog.py",
            "scripts/successor_d0_generation/__init__.py",
            "scripts/successor_d0_generation/builder.py",
            "scripts/successor_d0_generation/cli_inventory.py",
            "scripts/successor_d0_generation/contract_inventory.py",
            "scripts/successor_d0_generation/model.py",
            "scripts/successor_d0_generation/repository_inventory.py",
            "scripts/successor_d0_generation/runtime_inventory.py",
            "scripts/successor_d0_generation/snapshot.py",
            "scripts/successor_d0_generation/static_python.py",
        }
    )
)


def _merge_selector_maps(*mappings: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = defaultdict(list)
    for mapping in mappings:
        for selector, record_ids in mapping.items():
            merged[str(selector)].extend(str(record_id) for record_id in record_ids)
    return {key: sorted(set(values)) for key, values in merged.items()}


def _normalize_declared_entrypoint(
    entrypoint: str,
    *,
    cli_leaf_paths: set[str],
) -> tuple[str, list[str]]:
    if entrypoint.startswith("cli:"):
        body = entrypoint.removeprefix("cli:")
        tokens = body.split()
        command_tokens = [token for token in tokens if not token.startswith("-")]
        command = ""
        while command_tokens:
            candidate = " ".join(command_tokens)
            if candidate in cli_leaf_paths:
                command = candidate
                break
            command_tokens.pop()
        selectors = [f"cli:{command}"] if command else []
        if command:
            selectors.extend(f"cli:{command} {token}" for token in tokens if token.startswith("-"))
        return command, selectors
    if entrypoint.startswith("script:"):
        match = re.search(r"scripts/[^\s]+?\.py", entrypoint)
        return (match.group(0) if match else ""), ([f"script:{match.group(0)}"] if match else [])
    return entrypoint, [entrypoint]


def _build_coverage_edges(
    context: _CatalogContext,
    *,
    feature_by_capability: Mapping[str, Mapping[str, Any]],
    selector_map: Mapping[str, Sequence[str]],
    cli_leaf_paths: set[str],
    capability_oracles: Mapping[str, Sequence[str]],
) -> tuple[list[dict[str, Any]], dict[str, set[str]], dict[str, set[str]], list[str]]:
    edges: list[dict[str, Any]] = []
    capability_surfaces: dict[str, set[str]] = defaultdict(set)
    surface_capabilities: dict[str, set[str]] = defaultdict(set)
    unresolved_entrypoints: list[str] = []
    function_evidence = [
        context.evidence("docs/acceptance/function_matrix.json", anchor="user_entrypoints")
    ]
    for capability_id, feature in sorted(feature_by_capability.items()):
        entrypoints = feature.get("user_entrypoints", [])
        if not isinstance(entrypoints, list):
            entrypoints = []
        for raw_entrypoint in entrypoints:
            entrypoint = str(raw_entrypoint)
            _, selectors = _normalize_declared_entrypoint(
                entrypoint,
                cli_leaf_paths=cli_leaf_paths,
            )
            matched: set[str] = set()
            for selector in selectors:
                matched.update(str(item) for item in selector_map.get(selector, ()))
            if not matched:
                unresolved_entrypoints.append(f"{capability_id}:{entrypoint}")
                continue
            for surface_id in sorted(matched):
                capability_surfaces[capability_id].add(surface_id)
                surface_capabilities[surface_id].add(capability_id)
                edges.append(
                    _edge(
                        from_id=surface_id,
                        relation="SURFACE_EXPOSES_CAPABILITY",
                        to_id=capability_id,
                        facet=entrypoint,
                        evidence_refs=function_evidence,
                    )
                )
        for oracle_id in sorted(set(capability_oracles.get(capability_id, ()))):
            edges.append(
                _edge(
                    from_id=capability_id,
                    relation="CAPABILITY_VERIFIED_BY_ORACLE",
                    to_id=oracle_id,
                    facet="legacy_validation_command",
                    evidence_refs=[
                        context.evidence(
                            "docs/acceptance/function_matrix.json",
                            anchor=str(feature.get("id") or capability_id),
                        )
                    ],
                    assertion_authority="DECLARED_LEGACY",
                )
            )
    if unresolved_entrypoints:
        context.finding(
            "UNMAPPED_DECLARED_ENTRYPOINT",
            "function matrix contains declared entrypoints absent from the exact snapshot inventory",
            source_ref="docs/acceptance/function_matrix.json",
            evidence={"count": len(unresolved_entrypoints), "entrypoints": unresolved_entrypoints},
        )
    return edges, dict(capability_surfaces), dict(surface_capabilities), unresolved_entrypoints


def _validate_records(
    context: _CatalogContext,
    artifacts: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, int]:
    record_occurrences: dict[str, list[str]] = defaultdict(list)
    discovery_occurrences: dict[str, list[str]] = defaultdict(list)
    invalid = 0
    for artifact, records in artifacts.items():
        expected_schema = ARTIFACT_SCHEMAS[artifact]
        for ordinal, record in enumerate(records, start=1):
            missing = sorted(_COMMON_RECORD_FIELDS - set(record))
            record_id = str(record.get("record_id") or "")
            discovery_key = str(record.get("discovery_key") or "")
            record_status = record.get("record_status")
            v1_semantic_shape_invalid = (
                record_status not in ALLOWED_RECORD_STATUSES
                or record.get("decision_ref") is not None
                or (
                    artifact == "tests_oracles"
                    and record.get("independence_class") not in V1_ORACLE_INDEPENDENCE_CLASSES
                )
            )
            if (
                missing
                or not record_id
                or not discovery_key
                or not isinstance(record_status, str)
                or not record_status
                or record.get("schema_version") != expected_schema
                or record.get("record_type") != artifact
                or not isinstance(record.get("evidence_refs"), list)
                or v1_semantic_shape_invalid
            ):
                invalid += 1
                context.finding(
                    "SCHEMA_INVALID",
                    f"invalid {artifact} record at ordinal {ordinal}",
                    artifact_id=artifact,
                    record_id=record_id or None,
                    evidence={
                        "missing_fields": missing,
                        "record_status": record_status,
                        "decision_ref_must_be_null_in_v1": record.get("decision_ref"),
                        "independence_class": record.get("independence_class"),
                    },
                )
            try:
                canonical_json(record)
            except (TypeError, ValueError) as exc:
                invalid += 1
                context.finding(
                    "SCHEMA_INVALID",
                    f"non-serializable {artifact} record: {exc}",
                    artifact_id=artifact,
                    record_id=record_id or None,
                )
            record_occurrences[record_id].append(artifact)
            discovery_occurrences[discovery_key].append(artifact)
    duplicate_ids = {
        key: value for key, value in record_occurrences.items() if key and len(value) > 1
    }
    duplicate_keys = {
        key: value for key, value in discovery_occurrences.items() if key and len(value) > 1
    }
    for record_id, artifact_names in sorted(duplicate_ids.items()):
        context.finding(
            "DUPLICATE_ID",
            f"record_id is not globally unique: {record_id}",
            record_id=record_id,
            evidence={"artifacts": artifact_names},
        )
    for discovery_key, artifact_names in sorted(duplicate_keys.items()):
        context.finding(
            "DUPLICATE_DISCOVERY_KEY",
            f"discovery_key is not globally unique: {discovery_key}",
            evidence={"artifacts": artifact_names},
        )
    return {
        "duplicate_record_id": len(duplicate_ids),
        "duplicate_discovery_key": len(duplicate_keys),
        "invalid_record": invalid,
    }


def _serialize_artifact(
    artifact: str,
    records: Sequence[Mapping[str, Any]],
) -> tuple[bytes, dict[str, Any]]:
    ordered = sorted(
        (dict(record) for record in records),
        key=lambda item: (str(item["record_id"]), str(item["discovery_key"])),
    )
    if len(ordered) > MAX_JSONL_RECORDS:
        raise CatalogInputError(f"{artifact} record count exceeds limit {MAX_JSONL_RECORDS}")
    lines: list[bytes] = []
    total_bytes = 0
    for record in ordered:
        line = canonical_json(record).encode("utf-8") + b"\n"
        if len(line) > MAX_JSONL_LINE_BYTES:
            raise CatalogInputError(f"{artifact} JSONL line exceeds limit {MAX_JSONL_LINE_BYTES}")
        total_bytes += len(line)
        if total_bytes > MAX_ARTIFACT_BYTES:
            raise CatalogInputError(f"{artifact} artifact exceeds limit {MAX_ARTIFACT_BYTES}")
        lines.append(line)
    payload = b"".join(lines)
    line_hashes = [sha256_bytes(line) for line in lines]
    metadata = {
        "artifact_id": artifact,
        "path": f"{artifact}.jsonl",
        "schema_version": ARTIFACT_SCHEMAS[artifact],
        "record_type": artifact,
        "record_count": len(ordered),
        "record_id_set_sha256": _set_sha256(str(record["record_id"]) for record in ordered),
        "discovery_key_set_sha256": _set_sha256(str(record["discovery_key"]) for record in ordered),
        "record_root_sha256": sha256_bytes(canonical_json_bytes(line_hashes)),
        "sha256": sha256_bytes(payload),
        "byte_length": len(payload),
    }
    return payload, metadata


def _config_snapshot_binding(path: Path | None, *, repo_root: Path) -> dict[str, Any]:
    if path is None:
        return {
            "mode": "OMITTED",
            "binding_kind": "external_exact_file",
            "required_for_freeze": True,
            "provided_path": None,
            "locator": None,
            "locator_kind": "omitted",
            "sha256": None,
            "byte_length": None,
        }
    provided = path.expanduser()
    resolved = provided if provided.is_absolute() else repo_root / provided
    try:
        metadata = resolved.lstat()
    except FileNotFoundError as exc:
        raise CatalogInputError(f"explicit config snapshot is missing: {provided}") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise CatalogInputError("explicit config snapshot must be a non-symlink regular file")
    if metadata.st_size > MAX_EXTERNAL_BINDING_BYTES:
        raise CatalogInputError(
            f"explicit config snapshot exceeds {MAX_EXTERNAL_BINDING_BYTES} bytes"
        )
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise CatalogInputError(f"explicit config snapshot cannot be read: {exc}") from exc
    if len(payload) > MAX_EXTERNAL_BINDING_BYTES:
        raise CatalogInputError(
            f"explicit config snapshot exceeds {MAX_EXTERNAL_BINDING_BYTES} bytes"
        )
    return {
        "mode": "EXACT_FILE",
        "binding_kind": "external_exact_file",
        "required_for_freeze": True,
        "provided_path": None,
        "locator": "external:config_snapshot",
        "locator_kind": "explicit_override",
        "sha256": sha256_bytes(payload),
        "byte_length": len(payload),
    }


def _generator_identity() -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    implementation_files: list[dict[str, Any]] = []
    for relative in _GENERATOR_IMPLEMENTATION_PATHS:
        path = repo_root / relative
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise CatalogInputError(
                f"generator implementation file is missing: {relative}"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CatalogInputError(
                f"generator implementation path is not a regular file: {relative}"
            )
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise CatalogInputError(
                f"generator implementation file cannot be read: {relative}: {exc}"
            ) from exc
        implementation_files.append(
            {
                "path": relative,
                "sha256": sha256_bytes(payload),
                "byte_length": len(payload),
            }
        )
    identity_tuples = [
        [row["path"], row["sha256"], row["byte_length"]] for row in implementation_files
    ]
    schema_set = {
        "manifest": MANIFEST_SCHEMA,
        **ARTIFACT_SCHEMAS,
    }
    return {
        "code_identity_version": "exact-file-set-v1",
        "entry_symbol": ("scripts.successor_d0_generation.builder.SuccessorD0Catalog.generate"),
        "implementation_files": implementation_files,
        "implementation_root_sha256": sha256_bytes(canonical_json_bytes(identity_tuples)),
        "schema_set_sha256": sha256_bytes(canonical_json_bytes(schema_set)),
        "implementation_version": "d0-catalog-v1",
    }


def _guarded_adapter(
    context: _CatalogContext,
    name: str,
    default: Any,
    function: Any,
) -> Any:
    try:
        return function()
    except (CatalogInputError, OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        context.finding(
            "ENUMERATOR_FAILED",
            f"{name} Adapter failed closed: {type(exc).__name__}: {exc}",
            evidence={"adapter": name},
        )
        return default


def _patch_capability_views(
    capabilities: list[dict[str, Any]],
    capability_surfaces: Mapping[str, set[str]],
    capability_oracles: Mapping[str, Sequence[str]],
) -> None:
    for capability in capabilities:
        capability_id = str(capability["record_id"])
        capability["surface_refs"] = sorted(capability_surfaces.get(capability_id, set()))
        capability["oracle_refs"] = sorted(set(capability_oracles.get(capability_id, ())))


def _build_closure(
    *,
    artifacts: Mapping[str, Sequence[Mapping[str, Any]]],
    source_bindings: Sequence[Mapping[str, Any]],
    config_snapshot: Mapping[str, Any],
    validation_counts: Mapping[str, int],
    surface_capabilities: Mapping[str, set[str]],
    capability_surfaces: Mapping[str, set[str]],
    capability_oracles: Mapping[str, Sequence[str]],
    findings: Sequence[Mapping[str, Any]],
    oracle_metrics: Mapping[str, int],
    script_unclassified: int,
    challenger_unclassified: int,
    schema_unknown_owner: int,
) -> dict[str, Any]:
    requirements = list(artifacts["requirements"])
    surfaces = list(artifacts["surfaces"])
    capabilities = list(artifacts["capabilities"])
    oracles = list(artifacts["tests_oracles"])
    edges = list(artifacts["coverage_edges"])
    relation_rows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for edge in edges:
        relation_rows[str(edge.get("relation") or "")].append(edge)
    requirement_covered = {
        str(edge["from_id"]) for edge in relation_rows["REQUIREMENT_SATISFIED_BY_CAPABILITY"]
    }
    capability_required = {
        str(edge["to_id"]) for edge in relation_rows["REQUIREMENT_SATISFIED_BY_CAPABILITY"]
    }
    oracle_capability = {
        str(edge["to_id"]): str(edge["from_id"])
        for edge in relation_rows["CAPABILITY_VERIFIED_BY_ORACLE"]
    }
    unresolved_subject_ids = {
        str(requirement["record_id"])
        for requirement in requirements
        if str(requirement["record_id"]) not in requirement_covered
    }
    unresolved_subject_ids.update(str(capability["record_id"]) for capability in capabilities)
    unresolved_subject_ids.update(
        str(surface["record_id"])
        for surface in surfaces
        if str(surface["record_id"]) not in surface_capabilities
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
        str(oracle["record_id"])
        for oracle in oracles
        if str(oracle["record_id"]) not in oracle_capability
    )
    formal_oracles = [oracle for oracle in oracles if oracle.get("kind") != "pytest_file"]
    constitution_ids = {
        str(requirement.get("requirement_id"))
        for requirement in requirements
        if requirement.get("kind") == "SUCCESSOR_CONSTITUTION"
    }
    counts: dict[str, Any] = {
        # v1 is a discovery wire format.  No record-local string can upgrade it
        # into a freeze evaluator; that requires a new schema with detached
        # receipts and approved registries.
        "freeze_evaluator_unimplemented": 1,
        "surface_unmapped": sum(
            str(surface["record_id"]) not in surface_capabilities for surface in surfaces
        ),
        "behavior_without_surface": sum(
            str(capability["record_id"]) not in capability_surfaces for capability in capabilities
        ),
        "requirement_without_capability_or_adjudication": sum(
            str(requirement["record_id"]) not in requirement_covered for requirement in requirements
        ),
        "capability_without_requirement_or_adjudication": sum(
            str(capability["record_id"]) not in capability_required for capability in capabilities
        ),
        "capability_without_independent_test_or_oracle": len(capabilities),
        "test_without_capability_or_adjudication": sum(
            str(oracle["record_id"]) not in oracle_capability for oracle in formal_oracles
        ),
        "test_file_without_disposition": int(oracle_metrics.get("test_file_unlinked", 0)),
        "declared_missing_test_file": int(oracle_metrics.get("declared_missing_test_file", 0)),
        "canonical_owner_unknown": len(capabilities)
        + sum(
            surface.get("kind") == "schema_owner_seed"
            and (
                not isinstance(surface.get("facet_contract"), Mapping)
                or surface.get("facet_contract", {}).get("owner_status") != "REGISTERED"
            )
            for surface in surfaces
        ),
        "effect_target_unknown": len(capabilities),
        "parameter_mode_unclassified": sum(
            surface.get("kind") == "cli_argument_facet" for surface in surfaces
        ),
        "script_entry_unclassified": script_unclassified + challenger_unclassified,
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
            record.get("record_status") == "CONFLICT"
            for records in artifacts.values()
            for record in records
        ),
        "effective_capability_excluded": sum(
            capability.get("legacy_behavior_state") == "removed" for capability in capabilities
        ),
        "independent_inventory_diff": None,
        "independent_inventory_pending_family": None,
        "missing_required_source_binding": sum(
            bool(binding.get("required") and binding.get("status") != "BOUND")
            for binding in source_bindings
        )
        + int(config_snapshot.get("mode") != "EXACT_FILE"),
        "config_applicability_attestation_gap": 1,
        "constitution_requirement_missing": len(
            set(SUCCESSOR_CONSTITUTION_REQUIREMENT_IDS) - constitution_ids
        ),
        "constitution_approval_missing": 1,
        "duplicate_record_id": int(validation_counts["duplicate_record_id"]),
        "duplicate_discovery_key": int(validation_counts["duplicate_discovery_key"]),
        "invalid_record": int(validation_counts["invalid_record"]),
        "generator_error": sum(
            finding.get("code") in {"ENUMERATOR_FAILED", "SNAPSHOT_MUTATED_BY_ENUMERATOR"}
            for finding in findings
        ),
        "unresolved_adjudication": len(unresolved_subject_ids),
        "test_file_denominator": int(oracle_metrics.get("test_file_denominator", 0)),
        "test_file_linked_declaration": int(oracle_metrics.get("test_file_linked_declaration", 0)),
        "test_file_unlinked": int(oracle_metrics.get("test_file_unlinked", 0)),
    }
    local_fields = [
        field
        for field in REQUIRED_ZERO_FIELDS
        if field not in {"independent_inventory_diff", "independent_inventory_pending_family"}
    ]
    local_ok = all(counts.get(field) == 0 for field in local_fields)
    return {
        "schema_version": "mnemos.cognitive_successor_d0.closure.v1",
        "required_zero_fields": list(REQUIRED_ZERO_FIELDS),
        "counts": counts,
        "local_ok": local_ok,
        "verification_pending": True,
        "frozen_eligible": False,
    }


class SuccessorD0Catalog:
    """Deep Module for deterministic, fail-closed D0 catalog generation."""

    def generate(self, request: CatalogRequest) -> CatalogBundle:
        """Collect exact D0 artifacts from the requested immutable source commit.

        The method never reads a default Mnemos configuration.  A supplied
        config snapshot is hashed as an opaque exact input and is not loaded.
        """

        repo_root = request.repo_root.expanduser().resolve()
        config_snapshot = _config_snapshot_binding(
            request.config_snapshot,
            repo_root=repo_root,
        )
        with _archived_snapshot(request.repo_root, request.legacy_commit) as (
            snapshot,
            legacy_snapshot,
        ):
            context = _CatalogContext(snapshot)
            source_bindings = _source_bindings(
                context,
                repo_root=repo_root,
                design_path=request.design_path,
                phase_contract_path=request.phase_contract_path,
            )

            requirements, _canonical_specs, node_requirements, _requirement_metrics = (
                _guarded_adapter(
                    context,
                    "requirements",
                    ([], {}, {}, {}),
                    lambda: _collect_requirements(context),
                )
            )
            requirements.extend(_successor_constitution_requirement_records())
            capabilities, feature_by_capability = _guarded_adapter(
                context,
                "capabilities",
                ([], {}),
                lambda: _collect_capabilities(context),
            )

            surfaces: list[dict[str, Any]] = []
            selector_maps: list[Mapping[str, Sequence[str]]] = []
            cli_records, cli_map, command_paths, cli_metrics = _guarded_adapter(
                context,
                "main_cli",
                ([], {}, set(), {}),
                lambda: _collect_static_cli_surfaces(context),
            )
            surfaces.extend(cli_records)
            selector_maps.append(cli_map)
            surfaces.extend(
                _guarded_adapter(
                    context,
                    "console_scripts",
                    [],
                    lambda: _collect_console_script_surfaces(context),
                )
            )
            surfaces.extend(
                _guarded_adapter(
                    context,
                    "cli_dispatch",
                    [],
                    lambda: _collect_cli_dispatch_surfaces(context, command_paths),
                )
            )
            daemon_modes, daemon_mode_map = _guarded_adapter(
                context,
                "daemon_modes",
                ([], {}),
                lambda: _collect_daemon_modes(context),
            )
            surfaces.extend(daemon_modes)
            selector_maps.append(daemon_mode_map)
            mcp_records, mcp_map = _guarded_adapter(
                context,
                "mcp",
                ([], {}),
                lambda: _collect_mcp_surfaces(context),
            )
            surfaces.extend(mcp_records)
            selector_maps.append(mcp_map)
            mcp_tool_records = [
                record for record in mcp_records if record.get("kind") == "mcp_tool"
            ]
            mcp_metrics = {
                "tool_union_count": len(mcp_tool_records),
                "registered_tool_count": sum(
                    bool(record.get("facet_contract", {}).get("registered"))
                    for record in mcp_tool_records
                ),
                "schema_tool_count": sum(
                    record.get("facet_contract", {}).get("input_schema") is not None
                    for record in mcp_tool_records
                ),
                "categorized_tool_count": sum(
                    record.get("facet_contract", {}).get("category") is not None
                    for record in mcp_tool_records
                ),
                "policy_tool_count": sum(
                    record.get("facet_contract", {}).get("policy") is not None
                    for record in mcp_tool_records
                ),
                "category_registry_gap_tool_names": sorted(
                    str(record.get("facet_contract", {}).get("tool_name"))
                    for record in mcp_tool_records
                    if record.get("facet_contract", {}).get("category") is None
                ),
            }
            surfaces.extend(
                _guarded_adapter(context, "facade", [], lambda: _collect_facade_surfaces(context))
            )
            source_records, source_map = _guarded_adapter(
                context,
                "sources",
                ([], {}),
                lambda: _collect_source_surfaces(context),
            )
            surfaces.extend(source_records)
            selector_maps.append(source_map)
            daemon_services, daemon_service_map = _guarded_adapter(
                context,
                "daemon_services",
                ([], {}),
                lambda: _collect_daemon_services(context),
            )
            surfaces.extend(daemon_services)
            selector_maps.append(daemon_service_map)
            chronos_records, chronos_map, chronos_routes = _guarded_adapter(
                context,
                "chronos",
                ([], {}, []),
                lambda: _collect_chronos_surfaces(context),
            )
            surfaces.extend(chronos_records)
            selector_maps.append(chronos_map)
            surfaces.extend(
                _guarded_adapter(
                    context,
                    "eventbus",
                    [],
                    lambda: _collect_event_surfaces(context, chronos_routes),
                )
            )
            surfaces.extend(
                _guarded_adapter(context, "health", [], lambda: _collect_health_surfaces(context))
            )
            surfaces.extend(
                _guarded_adapter(context, "kia", [], lambda: _collect_kia_surfaces(context))
            )
            script_records, script_map, script_unclassified = _guarded_adapter(
                context,
                "scripts",
                ([], {}, 0),
                lambda: _collect_script_surfaces(context),
            )
            surfaces.extend(script_records)
            selector_maps.append(script_map)
            challenger_records, challenger_unclassified = _guarded_adapter(
                context,
                "repo_entry_challengers",
                ([], 0),
                lambda: _collect_external_entry_challengers(context),
            )
            surfaces.extend(challenger_records)
            schema_records, schema_unknown_owner = _guarded_adapter(
                context,
                "schema_owners",
                ([], 0),
                lambda: _collect_schema_surfaces(context),
            )
            surfaces.extend(schema_records)

            oracles, capability_oracles, _validation_keys, oracle_metrics = _guarded_adapter(
                context,
                "tests_oracles",
                ([], {}, {}, {}),
                lambda: _collect_oracles(
                    context,
                    node_requirements=node_requirements,
                    feature_by_capability=feature_by_capability,
                ),
            )
            selector_map = _merge_selector_maps(*selector_maps)
            cli_leaf_paths = {
                str(record.get("canonical_selector", "")).removeprefix("cli:")
                for record in cli_records
                if record.get("kind") == "cli"
                and str(record.get("canonical_selector", "")).startswith("cli:")
            }
            edges, capability_surfaces, surface_capabilities, _unresolved_entrypoints = (
                _guarded_adapter(
                    context,
                    "coverage_edges",
                    ([], {}, {}, []),
                    lambda: _build_coverage_edges(
                        context,
                        feature_by_capability=feature_by_capability,
                        selector_map=selector_map,
                        cli_leaf_paths=cli_leaf_paths,
                        capability_oracles=capability_oracles,
                    ),
                )
            )
            _patch_capability_views(capabilities, capability_surfaces, capability_oracles)

            documented_cli = {
                _normalize_declared_entrypoint(str(entrypoint), cli_leaf_paths=cli_leaf_paths)[0]
                for feature in feature_by_capability.values()
                for entrypoint in feature.get("user_entrypoints", [])
                if str(entrypoint).startswith("cli:")
            }
            documented_cli.discard("")
            documented_mcp = {
                str(entrypoint).removeprefix("mcp:")
                for feature in feature_by_capability.values()
                for entrypoint in feature.get("user_entrypoints", [])
                if str(entrypoint).startswith("mcp:")
            }
            mcp_names = {
                str(record.get("canonical_selector", "")).removeprefix("mcp:")
                for record in mcp_records
                if record.get("kind") == "mcp_tool"
            }
            if cli_leaf_paths - documented_cli or mcp_names - documented_mcp:
                context.finding(
                    "FUNCTION_MATRIX_SURFACE_GAP",
                    "function matrix does not cover the exact CLI/MCP surface denominator",
                    source_ref="docs/acceptance/function_matrix.json",
                    evidence={
                        "cli_leaf_count": len(cli_leaf_paths),
                        "cli_mapped_count": len(cli_leaf_paths & documented_cli),
                        "cli_unmapped": sorted(cli_leaf_paths - documented_cli),
                        "mcp_tool_count": len(mcp_names),
                        "mcp_mapped_count": len(mcp_names & documented_mcp),
                        "mcp_unmapped": sorted(mcp_names - documented_mcp),
                        "all_argparse_command_path_count": len(command_paths),
                    },
                )

            context.finding(
                "D0_V1_DISCOVERY_ONLY",
                "manifest v1 is a discovery-only wire format and has no freeze evaluator",
                evidence={
                    "missing_mechanisms": list(V1_DISCOVERY_ONLY_MISSING_MECHANISMS),
                    "freeze_evaluator_unimplemented": 1,
                },
                repair_action=(
                    "introduce a new freeze-capable schema with detached typed receipts and "
                    "approved registries; do not reinterpret v1 in place"
                ),
            )
            context.finding(
                "INDEPENDENT_INVENTORY_INCOMPLETE",
                "reverse state/effect inventory families are not implemented in D0 v1",
                evidence={"pending_families": list(REQUIRED_INDEPENDENT_INVENTORY_FAMILIES)},
                repair_action="implement each independent reverse Adapter before a v2 freeze gate",
            )
            context.finding(
                "CONSTITUTION_APPROVAL_MISSING",
                "successor constitution clauses have no detached exact-byte approval receipt",
                evidence={"requirement_ids": list(SUCCESSOR_CONSTITUTION_REQUIREMENT_IDS)},
                repair_action=(
                    "approve a machine-readable constitution and bind its exact receipt in a "
                    "future freeze-capable schema"
                ),
            )

            artifact_records: dict[str, list[dict[str, Any]]] = {
                "requirements": requirements,
                "surfaces": surfaces,
                "capabilities": capabilities,
                "tests_oracles": oracles,
                "coverage_edges": edges,
            }
            validation_counts = _validate_records(context, artifact_records)
            closure = _build_closure(
                artifacts=artifact_records,
                source_bindings=source_bindings,
                config_snapshot=config_snapshot,
                validation_counts=validation_counts,
                surface_capabilities=surface_capabilities,
                capability_surfaces=capability_surfaces,
                capability_oracles=capability_oracles,
                findings=context.findings,
                oracle_metrics=oracle_metrics,
                script_unclassified=script_unclassified,
                challenger_unclassified=challenger_unclassified,
                schema_unknown_owner=schema_unknown_owner,
            )
            findings = sorted(
                context.findings,
                key=lambda item: (
                    str(item.get("code") or ""),
                    str(item.get("artifact_id") or ""),
                    str(item.get("record_id") or ""),
                    str(item.get("source_ref") or ""),
                    str(item.get("message") or ""),
                ),
            )
            blocking_count = sum(item.get("severity") == "BLOCKING" for item in findings)
            bundle_status = "BLOCKED"

            artifact_bytes: dict[str, bytes] = {}
            artifact_metadata: list[dict[str, Any]] = []
            for artifact in ARTIFACT_ORDER:
                payload, metadata = _serialize_artifact(artifact, artifact_records[artifact])
                artifact_bytes[f"{artifact}.jsonl"] = payload
                artifact_metadata.append(metadata)

            manifest = {
                "schema_version": MANIFEST_SCHEMA,
                "bundle_status": bundle_status,
                "release_eligible": False,
                "denominator_frozen": False,
                "denominator_approved": False,
                "verification_scope": {
                    "mode": "DISCOVERY_ONLY",
                    "freeze_capable": False,
                    "missing_mechanisms": list(V1_DISCOVERY_ONLY_MISSING_MECHANISMS),
                },
                "legacy_snapshot": legacy_snapshot,
                "config_snapshot": config_snapshot,
                "source_bindings": source_bindings,
                "generator_identity": _generator_identity(),
                "canonicalization": {
                    "json": (
                        "json.dumps(ensure_ascii=False,sort_keys=True,"
                        "separators=(',',':'),allow_nan=False)"
                    ),
                    "jsonl": "canonical-json UTF-8 plus one LF per record",
                    "record_order": "ascending (record_id,discovery_key)",
                    "set_hash": "sha256(canonical-json(sorted(unique strings))+LF)",
                    "record_root": ("sha256(canonical-json(ordered sha256 exact-line strings)+LF)"),
                    "digest_encoding": "sha256:<lowercase-hex>",
                },
                "inventory_metrics": {
                    "main_cli": cli_metrics,
                    "mcp": mcp_metrics,
                },
                "artifact_order": list(ARTIFACT_ORDER),
                "artifacts": artifact_metadata,
                "closure": closure,
                "finding_counts": {
                    "blocking": blocking_count,
                    "warning": sum(item.get("severity") == "WARNING" for item in findings),
                },
                "findings": findings,
            }
            manifest_bytes = canonical_json_bytes(manifest)
            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                raise CatalogInputError(
                    f"manifest exceeds limit {MAX_MANIFEST_BYTES}: {len(manifest_bytes)}"
                )
            artifact_bytes["manifest.json"] = manifest_bytes
            return CatalogBundle(manifest=manifest, artifacts=artifact_bytes)

    def write(self, bundle: CatalogBundle, output_dir: Path) -> tuple[Path, ...]:
        """Publish files atomically one-by-one, with the manifest published last.

        The directory swap is not atomic.  An interruption before the final
        manifest replacement leaves a mixed generation that exact-byte
        verification rejects closed.
        """

        destination = output_dir.expanduser()
        if destination.exists():
            metadata = destination.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise CatalogInputError("output directory must be a non-symlink directory")
        else:
            destination.mkdir(parents=True, mode=0o755)
        for name in bundle.artifacts:
            target = destination / name
            if target.exists():
                metadata = target.lstat()
                if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    raise CatalogInputError(f"output target is not a regular file: {target}")
        ordered_names = [f"{artifact}.jsonl" for artifact in ARTIFACT_ORDER] + ["manifest.json"]
        with tempfile.TemporaryDirectory(prefix=".successor-d0-", dir=destination.parent) as temp:
            staging = Path(temp)
            for name in ordered_names:
                target = staging / name
                with target.open("xb") as handle:
                    handle.write(bundle.artifacts[name])
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(target, 0o644)
            written: list[Path] = []
            for name in ordered_names[:-1]:
                target = destination / name
                os.replace(staging / name, target)
                written.append(target)
            directory_fd = os.open(destination, os.O_RDONLY)
            try:
                # Persist every artifact rename before publishing the manifest
                # as the generation's commit marker.
                os.fsync(directory_fd)
                manifest_name = ordered_names[-1]
                manifest_target = destination / manifest_name
                os.replace(staging / manifest_name, manifest_target)
                written.append(manifest_target)
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return tuple(written)
