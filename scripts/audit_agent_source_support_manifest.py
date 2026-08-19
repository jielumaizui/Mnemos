#!/usr/bin/env python3
"""Independently audit the canonical AgentSource support manifest.

The manifest is the only editable source-definition owner.  This verifier is
deliberately independent of the runtime loader: it parses the tracked JSON and
the source tree directly so a caller cannot hide a deleted parser or a second
hand-maintained list behind the same implementation that consumes it.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any, Mapping, cast

from core.ops.durable_io import read_native_bytes
from scripts.agent_source_support_runtime_audit import (
    audit_runtime_evidence as _audit_runtime_evidence,
    canonical_hash as _canonical_hash,
    load_runtime_evidence as _load_runtime_evidence,
    report_is_structural_only as _report_is_structural_only,
    validate_runtime_evidence_envelope as _validate_runtime_evidence_envelope,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_RELATIVE_PATH = Path("core/agent_kit/agent_source_support_manifest.json")
SCHEMA_VERSION = "mnemos.agent_source_support_manifest.v1"
REPORT_SCHEMA_VERSION = "mnemos.agent_source_support_audit.v1"
VALID_ROLES = {"host_agent", "ingestion_only", "retired"}
CONTINUOUS_CAPTURE_OWNER = "daemon.raw_sync"
CONTINUOUS_CAPTURE_SERVICE = "raw_sync"
CONTINUOUS_CAPTURE_ACTIVATION_KEY = "daemon.services.raw_sync"
REQUIRED_SOURCE_FIELDS = {
    "name",
    "role",
    "aliases",
    "parser",
    "native",
    "capability",
    "authorization",
    "continuous",
    "backfill",
    "runtime_probe",
    "raw_contract",
    "retention",
    "retirement",
    "install_evidence",
    "active_entrypoint",
    "active_setup",
}


def _finding(code: str, message: str, *, path: str = "") -> dict[str, str]:
    return {
        "code": code,
        "severity": "blocking",
        "message": message,
        "path": path,
        "repair_action": "restore the canonical AgentSource support contract",
    }


def _read_text(
    root: Path,
    relative: str,
    overrides: Mapping[str, str],
) -> str:
    if relative in overrides:
        return overrides[relative]
    return read_native_bytes(root / relative).decode("utf-8")


def _read_optional_text(
    root: Path,
    relative: str,
    overrides: Mapping[str, str],
) -> str:
    try:
        return _read_text(root, relative, overrides)
    except (OSError, UnicodeError):
        return ""


def _load_manifest(path: Path) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    try:
        payload = json.loads(read_native_bytes(path).decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, [_finding("support_manifest_unreadable", str(exc), path=str(path))]
    if not isinstance(payload, dict):
        return None, [
            _finding(
                "support_manifest_not_object", "manifest root must be an object", path=str(path)
            )
        ]
    return payload, []


def _expr_is_manifest_derived(node: ast.AST, *, allow_target_alias: bool = False) -> bool:
    if allow_target_alias and isinstance(node, ast.Name) and node.id == "TARGET_AGENT_NAMES":
        return True
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id in {"_MANIFEST", "manifest"}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return isinstance(node.func.value, ast.Name) and node.func.value.id in {
            "_MANIFEST",
            "manifest",
        }
    return False


def _assigned_value(tree: ast.AST, name: str) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                return node.value
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                return node.value
    return None


def _registry_builtin_specs_are_manifest_derived(tree: ast.AST) -> bool:
    """Require the registry method itself to return manifest registry specs."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "builtin_agent_specs":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Return) or not isinstance(child.value, ast.Call):
                continue
            outer = child.value
            if (
                not isinstance(outer.func, ast.Attribute)
                or outer.func.attr != "builtin_registry_specs"
            ):
                continue
            inner = outer.func.value
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "get_agent_source_support_manifest"
            ):
                return True
    return False


def _source_classes(root: Path) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    source_dir = root / "integrations/sources"
    for path in sorted(source_dir.glob("*_source.py")):
        module = "integrations.sources." + path.stem
        try:
            tree = ast.parse(read_native_bytes(path).decode("utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {
                base.id if isinstance(base, ast.Name) else base.attr
                for base in node.bases
                if isinstance(base, (ast.Name, ast.Attribute))
            }
            if "BaseAgentSource" in bases:
                result[(module, node.name)] = str(path.relative_to(root))
    return result


def _required_mapping(value: Any, label: str, findings: list[dict[str, str]]) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    findings.append(_finding("support_manifest_schema", f"{label} must be an object"))
    return {}


def _validate_format_resolution(
    name: str,
    native: Mapping[str, Any],
    findings: list[dict[str, str]],
) -> None:
    """Independently validate manifest-owned multi-format identity semantics."""
    resolution_raw = native.get("format_resolution")
    if resolution_raw is None:
        if name == "openclaw":
            findings.append(
                _finding(
                    "support_manifest_schema",
                    "openclaw: native format_resolution is required",
                )
            )
        return
    resolution = _required_mapping(
        resolution_raw,
        f"{name}.native.format_resolution",
        findings,
    )
    variants = resolution.get("variants")
    if not isinstance(variants, list) or not variants:
        findings.append(
            _finding(
                "support_manifest_schema",
                f"{name}: format_resolution variants must be a non-empty list",
            )
        )
        return
    source_kinds: set[str] = set()
    priorities: set[int] = set()
    variants_by_kind: dict[str, Mapping[str, Any]] = {}
    for index, raw_variant in enumerate(variants):
        variant = _required_mapping(
            raw_variant,
            f"{name}.native.format_resolution.variants[{index}]",
            findings,
        )
        source_kind = str(variant.get("source_kind") or "")
        path_glob = str(variant.get("path_glob") or "")
        priority = variant.get("priority")
        if not source_kind or not path_glob:
            findings.append(
                _finding(
                    "support_manifest_schema",
                    f"{name}: format variant requires source_kind and path_glob",
                )
            )
        if source_kind in source_kinds:
            findings.append(
                _finding(
                    "support_manifest_schema",
                    f"{name}: format source_kind is duplicated: {source_kind}",
                )
            )
        if not isinstance(priority, int) or isinstance(priority, bool) or priority <= 0:
            findings.append(
                _finding(
                    "support_manifest_schema",
                    f"{name}: format priority must be positive",
                )
            )
        elif priority in priorities:
            findings.append(
                _finding(
                    "support_manifest_schema",
                    f"{name}: format priority is duplicated: {priority}",
                )
            )
        if variant.get("identity") != "native_session_id":
            findings.append(
                _finding(
                    "support_manifest_schema",
                    f"{name}: format identity must be native_session_id",
                )
            )
        if variant.get("content_equivalence") != "turn_fingerprint":
            findings.append(
                _finding(
                    "support_manifest_schema",
                    f"{name}: format content_equivalence must be turn_fingerprint",
                )
            )
        exclude_suffix = variant.get("exclude_suffix")
        if exclude_suffix is not None and not isinstance(exclude_suffix, str):
            findings.append(
                _finding(
                    "support_manifest_schema",
                    f"{name}: format exclude_suffix must be a string",
                )
            )
        source_kinds.add(source_kind)
        variants_by_kind[source_kind] = variant
        if isinstance(priority, int) and not isinstance(priority, bool):
            priorities.add(priority)
    if name == "openclaw":
        if source_kinds != {"trajectory", "normal_jsonl", "corpus"}:
            findings.append(
                _finding(
                    "support_manifest_schema",
                    "openclaw: format resolution must declare trajectory, normal_jsonl, and corpus",
                )
            )
        else:
            expected_paths = {
                "trajectory": ("agents/*/sessions/*.trajectory.jsonl", None),
                "normal_jsonl": ("agents/*/sessions/*.jsonl", ".trajectory.jsonl"),
                "corpus": (
                    "workspace/memory/.dreams/session-corpus/*.txt",
                    None,
                ),
            }
            if any(
                (
                    variants_by_kind[kind].get("path_glob"),
                    variants_by_kind[kind].get("exclude_suffix"),
                )
                != expected
                for kind, expected in expected_paths.items()
            ):
                findings.append(
                    _finding(
                        "support_manifest_schema",
                        "openclaw: invalid format path contract",
                    )
                )
            priority_by_kind = {
                kind: variants_by_kind[kind].get("priority")
                for kind in ("trajectory", "normal_jsonl", "corpus")
            }
            trajectory_priority = priority_by_kind["trajectory"]
            normal_priority = priority_by_kind["normal_jsonl"]
            corpus_priority = priority_by_kind["corpus"]
            if not (
                isinstance(trajectory_priority, int)
                and not isinstance(trajectory_priority, bool)
                and isinstance(normal_priority, int)
                and not isinstance(normal_priority, bool)
                and isinstance(corpus_priority, int)
                and not isinstance(corpus_priority, bool)
                and trajectory_priority > normal_priority > corpus_priority
            ):
                findings.append(
                    _finding(
                        "support_manifest_schema",
                        "openclaw: invalid format priority order",
                    )
                )
        if resolution.get("extension_rule") != "longest_turn_fingerprint_prefix":
            findings.append(
                _finding(
                    "support_manifest_schema",
                    "openclaw: invalid format extension rule",
                )
            )
        if resolution.get("conflict_rule") != "opaque_artifact_identity":
            findings.append(
                _finding(
                    "support_manifest_schema",
                    "openclaw: invalid format conflict rule",
                )
            )


def _validate_artifact_resolution(
    name: str,
    native: Mapping[str, Any],
    findings: list[dict[str, str]],
) -> None:
    """Independently validate Kimi's native artifact grouping contract."""
    resolution_raw = native.get("artifact_resolution")
    if resolution_raw is None:
        if name == "kimi":
            findings.append(
                _finding(
                    "support_manifest_schema",
                    "kimi: native artifact_resolution is required",
                )
            )
        return
    resolution = _required_mapping(
        resolution_raw,
        f"{name}.native.artifact_resolution",
        findings,
    )
    variants = resolution.get("variants")
    if not isinstance(variants, list) or not variants:
        findings.append(
            _finding(
                "support_manifest_schema",
                f"{name}: artifact_resolution variants must be a non-empty list",
            )
        )
        return
    source_kinds: set[str] = set()
    observed: dict[str, tuple[str, str, str]] = {}
    for index, raw_variant in enumerate(variants):
        variant = _required_mapping(
            raw_variant,
            f"{name}.native.artifact_resolution.variants[{index}]",
            findings,
        )
        source_kind = str(variant.get("source_kind") or "")
        path_glob = str(variant.get("path_glob") or "")
        selector = str(variant.get("selector") or "")
        aggregation = str(variant.get("aggregation") or "")
        parent_relation = str(variant.get("parent_relation") or "")
        if (
            not source_kind
            or not path_glob
            or not selector
            or not aggregation
            or not parent_relation
        ):
            findings.append(
                _finding(
                    "support_manifest_schema",
                    f"{name}: artifact variant is incomplete",
                )
            )
        if source_kind in source_kinds:
            findings.append(
                _finding(
                    "support_manifest_schema",
                    f"{name}: artifact source_kind is duplicated: {source_kind}",
                )
            )
        if variant.get("identity") != "native_artifact_id":
            findings.append(
                _finding(
                    "support_manifest_schema",
                    f"{name}: artifact identity must be native_artifact_id",
                )
            )
        source_kinds.add(source_kind)
        observed[source_kind] = (selector, aggregation, parent_relation)
    if name != "kimi":
        return
    expected = {
        "main_context": ("outside_subagents", "context_segments_per_directory", "none"),
        "subagent_context": (
            "under_subagents",
            "context_segments_per_directory",
            "native_subagent_path",
        ),
        "main_wire": ("main_wire", "single_wire", "native_main_wire_path"),
        "subagent_wire": ("under_subagents", "single_wire", "native_subagent_wire_path"),
    }
    if source_kinds != set(expected) or observed != expected:
        findings.append(
            _finding(
                "support_manifest_schema",
                "kimi: artifact resolution variants are invalid",
            )
        )
    expected_rules = {
        "archive_order_rule": "context_numeric_ascending_then_lexical_unknown_then_active",
        "duplicate_event_rule": "dedupe_explicit_native_event_id_and_canonical_json_value_only",
        "conflict_rule": "opaque_artifact_identity",
        "identity_contract": "kimi-native-artifact-v2",
        "state_fingerprint_rule": "ordered_artifact_name_and_bytes_sha256",
        "decoder_contract": "reversible_jsonl_v1",
        "decoder_rejection_rule": (
            "preserve_invalid_utf8_json_nonobject_duplicate_key_nonfinite_"
            "surrogate_and_excessive_nesting"
        ),
        "json_value_equality_rule": "number_value_equal_boolean_type_distinct",
        "number_decode_rule": (
            "decimal_identity_exact_utf8_source_line_and_json_valid_runtime_wrapper"
        ),
        "migration_rule": "fail_closed_on_legacy_raw_overlap",
        "legacy_identity_rule": "bind_fixed_point_misclassification_aliases",
        "timestamp_rule": "preserve_invalid_numeric_timestamp_as_typed_raw",
        "parent_identity_rule": "native_parent_plus_canonical_main_artifact_v2",
    }
    for field, expected_value in expected_rules.items():
        if resolution.get(field) != expected_value:
            findings.append(
                _finding(
                    "support_manifest_schema",
                    f"kimi: invalid artifact {field}",
                )
            )


def _validate_recursive_discovery_contract(
    name: str,
    native: Mapping[str, Any],
    resolver: Mapping[str, Any],
    findings: list[dict[str, str]],
) -> None:
    """Independently reject a bounded Claude transcript denominator."""
    if name != "claude":
        return
    if native.get("formats") != ["projects/**/*.jsonl"]:
        findings.append(
            _finding(
                "claude_recursive_discovery_contract_invalid",
                "claude: native formats must be exactly projects/**/*.jsonl",
            )
        )
    if resolver.get("transcript_subdir") != "projects":
        findings.append(
            _finding(
                "claude_recursive_discovery_contract_invalid",
                "claude: transcript_subdir must be projects",
            )
        )


def _validate_multi_root_contract(
    name: str,
    native: Mapping[str, Any],
    resolver: Mapping[str, Any],
    findings: list[dict[str, str]],
) -> None:
    """Independently reject a first-valid Crush completeness boundary."""
    if name != "crush":
        return
    multi_root = resolver.get("multi_root")
    if (
        native.get("formats") != ["crush.db"]
        or not isinstance(multi_root, Mapping)
        or multi_root.get("mode") != "all_valid"
        or multi_root.get("project_ancestor_search") is not True
    ):
        findings.append(
            _finding(
                "crush_multi_root_contract_invalid",
                "crush: all valid roots plus project ancestor search are required",
            )
        )


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    """Return a typed mapping view without making malformed evidence trusted."""
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _validate_manifest_schema(
    payload: Mapping[str, Any],
    parser_classes: Mapping[tuple[str, str], str],
) -> tuple[list[dict[str, str]], dict[str, Mapping[str, Any]]]:
    findings: list[dict[str, str]] = []
    specs: dict[str, Mapping[str, Any]] = {}
    if payload.get("schema_version") != SCHEMA_VERSION:
        findings.append(_finding("support_manifest_schema", "schema_version is unsupported"))
    if payload.get("source_definition_owner") != str(MANIFEST_RELATIVE_PATH):
        findings.append(
            _finding(
                "support_manifest_owner",
                "source_definition_owner must name this tracked manifest",
            )
        )
    core_caps = payload.get("host_core_cognitive_capabilities")
    if not isinstance(core_caps, list) or not all(
        isinstance(value, str) and value for value in core_caps
    ):
        findings.append(
            _finding(
                "support_manifest_schema",
                "host_core_cognitive_capabilities must be a non-empty string list",
            )
        )

    source_specs = payload.get("sources")
    if not isinstance(source_specs, list):
        findings.append(_finding("support_manifest_schema", "sources must be a list"))
        return findings, specs
    for index, source in enumerate(source_specs):
        if not isinstance(source, Mapping):
            findings.append(
                _finding("support_manifest_schema", f"sources[{index}] must be an object")
            )
            continue
        missing = sorted(REQUIRED_SOURCE_FIELDS - set(source))
        if missing:
            findings.append(
                _finding("support_manifest_schema", f"sources[{index}] missing {missing}")
            )
        name = str(source.get("name") or "").strip()
        if not name:
            findings.append(_finding("support_manifest_schema", f"sources[{index}] has no name"))
            continue
        if name in specs:
            findings.append(
                _finding("support_manifest_duplicate_source", f"duplicate source {name}")
            )
            continue
        specs[name] = source
        role = source.get("role")
        if role not in VALID_ROLES:
            findings.append(_finding("support_manifest_role", f"{name}: invalid role {role!r}"))
        aliases = source.get("aliases")
        if not isinstance(aliases, list) or not all(
            isinstance(alias, str) and alias for alias in aliases
        ):
            findings.append(
                _finding("support_manifest_schema", f"{name}: aliases must be a string list")
            )
        parser = _required_mapping(source.get("parser"), f"{name}.parser", findings)
        module = str(parser.get("module") or "")
        class_name = str(parser.get("class") or "")
        if not str(parser.get("owner") or ""):
            findings.append(
                _finding("support_manifest_schema", f"{name}: parser owner is required")
            )
        if (module, class_name) not in parser_classes:
            findings.append(
                _finding(
                    "manifest_without_parser", f"{name}: parser {module}.{class_name} is absent"
                )
            )
        native = _required_mapping(source.get("native"), f"{name}.native", findings)
        resolver = _required_mapping(
            native.get("root_resolver"), f"{name}.native.root_resolver", findings
        )
        if not isinstance(native.get("formats"), list) or not native.get("formats"):
            findings.append(
                _finding("support_manifest_schema", f"{name}: native formats are required")
            )
        _validate_format_resolution(name, native, findings)
        _validate_artifact_resolution(name, native, findings)
        _validate_recursive_discovery_contract(name, native, resolver, findings)
        _validate_multi_root_contract(name, native, resolver, findings)
        for resolver_field in (
            "configuration_keys",
            "environment",
            "standard_paths",
            "process_args",
            "heuristic",
            "transcript_subdir",
        ):
            if resolver_field not in resolver:
                findings.append(
                    _finding(
                        "support_manifest_schema", f"{name}: root resolver missing {resolver_field}"
                    )
                )
        if "multi_root" in resolver:
            multi_root = _required_mapping(
                resolver.get("multi_root"),
                f"{name}.native.root_resolver.multi_root",
                findings,
            )
            if multi_root.get("mode") not in {"first_valid", "all_valid"}:
                findings.append(
                    _finding("support_manifest_schema", f"{name}: invalid multi-root mode")
                )
            if not isinstance(multi_root.get("project_ancestor_search", False), bool):
                findings.append(
                    _finding(
                        "support_manifest_schema",
                        f"{name}: multi-root project_ancestor_search must be bool",
                    )
                )
        capability = _required_mapping(source.get("capability"), f"{name}.capability", findings)
        if not str(capability.get("source_fidelity") or ""):
            findings.append(
                _finding("support_manifest_schema", f"{name}: source fidelity is required")
            )
        required_caps = capability.get("required_cognitive_capabilities")
        if not isinstance(required_caps, list):
            findings.append(
                _finding("support_manifest_schema", f"{name}: required capabilities must be a list")
            )
        for field_name in (
            "authorization",
            "continuous",
            "backfill",
            "runtime_probe",
            "raw_contract",
            "retention",
            "retirement",
            "install_evidence",
            "active_setup",
        ):
            if not isinstance(source.get(field_name), Mapping):
                findings.append(
                    _finding(
                        "support_manifest_schema",
                        f"{name}: {field_name} must be an object",
                    )
                )
        continuous = _required_mapping(source.get("continuous"), f"{name}.continuous", findings)
        if role != "retired":
            if continuous.get("enabled") is not True:
                findings.append(
                    _finding(
                        "continuous_capture_disabled",
                        f"{name}: active source must declare continuous capture enabled",
                    )
                )
            for field_name, expected in (
                ("owner", CONTINUOUS_CAPTURE_OWNER),
                ("service", CONTINUOUS_CAPTURE_SERVICE),
                ("activation_key", CONTINUOUS_CAPTURE_ACTIVATION_KEY),
            ):
                if continuous.get(field_name) != expected:
                    findings.append(
                        _finding(
                            "continuous_capture_owner_invalid",
                            f"{name}: continuous {field_name} must be {expected}",
                        )
                    )
            if not str(continuous.get("trigger") or ""):
                findings.append(
                    _finding(
                        "continuous_capture_trigger_missing",
                        f"{name}: continuous trigger is required",
                    )
                )
            poll_interval = continuous.get("poll_interval_seconds")
            max_latency = continuous.get("max_latency_seconds")
            if (
                not isinstance(poll_interval, int)
                or isinstance(poll_interval, bool)
                or poll_interval <= 0
                or not isinstance(max_latency, int)
                or isinstance(max_latency, bool)
                or max_latency < poll_interval
            ):
                findings.append(
                    _finding(
                        "continuous_capture_sla_invalid",
                        f"{name}: continuous poll interval and latency SLA are invalid",
                    )
                )
        raw_contract = _required_mapping(
            source.get("raw_contract"), f"{name}.raw_contract", findings
        )
        if raw_contract.get("fidelity") != capability.get("source_fidelity"):
            findings.append(
                _finding(
                    "support_manifest_fidelity",
                    f"{name}: raw and capability fidelity diverge",
                )
            )
        runtime_probe = _required_mapping(
            source.get("runtime_probe"), f"{name}.runtime_probe", findings
        )
        if role == "host_agent":
            if source.get("active_entrypoint") not in {"adapter", "mcp_only"}:
                findings.append(
                    _finding(
                        "support_manifest_role",
                        f"{name}: host agent requires an active entrypoint",
                    )
                )
            if runtime_probe.get("required") is not True:
                findings.append(
                    _finding(
                        "support_manifest_role",
                        f"{name}: host runtime probe must be required",
                    )
                )
            if not required_caps:
                findings.append(
                    _finding(
                        "support_manifest_role",
                        f"{name}: host capability contract is empty",
                    )
                )
        elif role == "ingestion_only":
            if source.get("active_entrypoint") != "none":
                findings.append(
                    _finding(
                        "support_manifest_role",
                        f"{name}: ingestion-only source cannot expose a host entrypoint",
                    )
                )
            if runtime_probe.get("required") is not False:
                findings.append(
                    _finding(
                        "support_manifest_role",
                        f"{name}: ingestion-only runtime probe must be disabled",
                    )
                )
        elif role == "retired":
            if source.get("active_entrypoint") != "none":
                findings.append(
                    _finding(
                        "support_manifest_role",
                        f"{name}: retired source cannot expose a host entrypoint",
                    )
                )

    declared_parsers: set[tuple[str, str]] = {
        (
            str(_required_mapping(spec.get("parser"), "parser", findings).get("module") or ""),
            str(_required_mapping(spec.get("parser"), "parser", findings).get("class") or ""),
        )
        for spec in specs.values()
    }
    for parser_identity, parser_path in parser_classes.items():
        if parser_identity not in declared_parsers:
            findings.append(
                _finding(
                    "builtin_source_without_manifest",
                    f"{parser_path}: {parser_identity[1]} is not represented in the manifest",
                    path=parser_path,
                )
            )
    for parser_identity in declared_parsers:
        if parser_identity not in parser_classes:
            findings.append(
                _finding(
                    "manifest_without_parser",
                    f"manifest parser {parser_identity[0]}.{parser_identity[1]} is absent",
                )
            )
    return findings, specs


def _audit_runtime_derivation(
    root: Path,
    overrides: Mapping[str, str],
) -> tuple[list[dict[str, str]], list[str]]:
    findings: list[dict[str, str]] = []
    owner_paths = [str(MANIFEST_RELATIVE_PATH)]

    protocol_relative = "core/agent_kit/protocol.py"
    protocol_text = _read_text(root, protocol_relative, overrides)
    protocol_tree = ast.parse(protocol_text, filename=protocol_relative)
    for constant in (
        "TARGET_AGENT_NAMES",
        "CONTEXT_SHARE_AGENT_NAMES",
        "AGENT_ALIASES",
        "ACTIVE_ENTRYPOINTS",
        "AGENT_COGNITIVE_CAPABILITIES",
    ):
        value = _assigned_value(protocol_tree, constant)
        if value is None or not _expr_is_manifest_derived(
            value,
            allow_target_alias=constant == "CONTEXT_SHARE_AGENT_NAMES",
        ):
            findings.append(
                _finding(
                    "protocol_host_list_not_manifest_derived",
                    f"{constant} is not derived from the support manifest",
                    path=protocol_relative,
                )
            )
            owner_paths.append(protocol_relative)
            break

    registry_relative = "core/sync_framework/registry.py"
    registry_text = _read_text(root, registry_relative, overrides)
    registry_tree = ast.parse(registry_text, filename=registry_relative)
    for legacy_name in (
        "_builtin_agents",
        "AGENT_CONFIG",
        "PROCESS_ARG_MAP",
        "HEURISTIC_PATTERNS",
        "AGENT_SUBDIRS",
    ):
        if _assigned_value(registry_tree, legacy_name) is not None:
            findings.append(
                _finding(
                    "registry_source_list_not_manifest_derived",
                    f"{legacy_name} remains an editable registry source definition",
                    path=registry_relative,
                )
            )
            owner_paths.append(registry_relative)
            break
    if not _registry_builtin_specs_are_manifest_derived(registry_tree):
        findings.append(
            _finding(
                "registry_manifest_derivation_missing",
                "registry does not load the support manifest",
                path=registry_relative,
            )
        )
        owner_paths.append(registry_relative)
    if "require_active_source" not in registry_text:
        findings.append(
            _finding(
                "registry_declared_source_guard_missing",
                "registry can register a native source without manifest validation",
                path=registry_relative,
            )
        )
    if (
        "_require_manifest_source_class" not in registry_text
        or "source_class is not expected_class" not in registry_text
    ):
        findings.append(
            _finding(
                "registry_parser_identity_guard_missing",
                "registry can substitute a parser class for a manifest-declared source name",
                path=registry_relative,
            )
        )

    evidence_relative = "core/agent_kit/evidence.py"
    evidence_text = _read_text(root, evidence_relative, overrides)
    evidence_tree = ast.parse(evidence_text, filename=evidence_relative)
    if (
        _assigned_value(evidence_tree, "_CLI_NAMES") is not None
        or "get_agent_source_support_manifest" not in evidence_text
    ):
        findings.append(
            _finding(
                "install_evidence_not_manifest_derived",
                "installation evidence still owns a handwritten source map",
                path=evidence_relative,
            )
        )
        owner_paths.append(evidence_relative)

    report_relative = "core/agent_kit/report.py"
    report_text = _read_text(root, report_relative, overrides)
    if "ingestion_sources" not in report_text or "ingestion_only_source_names" not in report_text:
        findings.append(
            _finding(
                "installed_source_silently_ignored",
                "Agent Kit report does not surface manifest ingestion-only sources",
                path=report_relative,
            )
        )

    diagnostics_relative = "core/diagnostics.py"
    diagnostics_text = _read_text(root, diagnostics_relative, overrides)
    if (
        "ingestion_only_source_names" not in diagnostics_text
        or "active_source_names" not in diagnostics_text
    ):
        findings.append(
            _finding(
                "diagnostics_source_list_not_manifest_derived",
                "diagnostics does not enumerate sources from the support manifest",
                path=diagnostics_relative,
            )
        )
        owner_paths.append(diagnostics_relative)

    facade_relative = "core/application/facade.py"
    facade_text = _read_text(root, facade_relative, overrides)
    if "active_source_names" not in facade_text:
        findings.append(
            _finding(
                "facade_source_list_not_manifest_derived",
                "application facade does not enumerate sources from the support manifest",
                path=facade_relative,
            )
        )
        owner_paths.append(facade_relative)

    daemon_relative = "daemon/raw_sync.py"
    daemon_text = _read_text(root, daemon_relative, overrides)
    if "build_native_source_snapshot" not in daemon_text or "SourceRegistry" not in daemon_text:
        findings.append(
            _finding(
                "daemon_source_snapshot_not_manifest_derived",
                "daemon raw sync does not emit manifest-bound source snapshots",
                path=daemon_relative,
            )
        )
    if "require_active_source(source.name)" not in daemon_text:
        findings.append(
            _finding(
                "daemon_declared_source_guard_missing",
                "daemon may parse an undeclared source before validating the manifest",
                path=daemon_relative,
            )
        )
    if "build_source_support_runtime_report" not in daemon_text:
        findings.append(
            _finding(
                "daemon_runtime_report_contract_missing",
                "daemon raw sync does not emit the canonical structural source report",
                path=daemon_relative,
            )
        )

    trigger_daemon_relative = "mnemos_daemon.py"
    trigger_daemon_text = _read_text(root, trigger_daemon_relative, overrides)
    if (
        "get_agent_source_support_manifest" not in trigger_daemon_text
        or 'source_spec.continuous["trigger"]' not in trigger_daemon_text
        or "runtime trigger strategy does not match" not in trigger_daemon_text
    ):
        findings.append(
            _finding(
                "daemon_trigger_contract_not_manifest_bound",
                "daemon trigger accelerator is not checked against the manifest continuous contract",
                path=trigger_daemon_relative,
            )
        )

    backfill_relative = "scripts/backfill_raw_event_store.py"
    backfill_text = _read_text(root, backfill_relative, overrides)
    if "manifest.require_active_source(source.name)" not in backfill_text:
        findings.append(
            _finding(
                "backfill_declared_source_guard_missing",
                "backfill may parse an undeclared source before validating the manifest",
                path=backfill_relative,
            )
        )
    if "build_source_support_runtime_report" not in backfill_text:
        findings.append(
            _finding(
                "backfill_runtime_report_contract_missing",
                "backfill does not emit the canonical structural source report",
                path=backfill_relative,
            )
        )

    raw_store_relative = "core/sync_framework/raw_event_store.py"
    raw_store_text = _read_text(root, raw_store_relative, overrides)
    native_ledger_relative = "core/sync_framework/native_raw_contract_ledger.py"
    native_ledger_text = _read_optional_text(
        root,
        native_ledger_relative,
        overrides,
    )
    if (
        "validate_native_raw_contract" not in raw_store_text
        or "_record_native_raw_contract_outcome" not in raw_store_text
        or "NativeRawContractLedger" not in raw_store_text
        or "raw_native_contract_observations" not in native_ledger_text
        or "refresh_effective_state" not in native_ledger_text
        or "_sync_effective_metrics" not in native_ledger_text
    ):
        findings.append(
            _finding(
                "native_raw_contract_guard_missing",
                "native Raw upsert does not preserve, surface, and score its contract outcome",
                path=raw_store_relative,
            )
        )

    loader_relative = "core/agent_kit/source_support_manifest.py"
    loader_text = _read_optional_text(root, loader_relative, overrides)
    if "NativeSourceSnapshot" not in loader_text or "support_manifest_hash" not in loader_text:
        findings.append(
            _finding(
                "native_source_snapshot_contract_missing",
                "runtime NativeSourceSnapshot is not bound to the support manifest",
                path=loader_relative,
            )
        )
    if "build_source_support_runtime_report" not in loader_text:
        findings.append(
            _finding(
                "runtime_report_contract_missing",
                "runtime reports lack the canonical structural-report contract",
                path=loader_relative,
            )
        )

    receipt_relative = "core/agent_kit/runtime_receipts.py"
    receipt_text = _read_text(root, receipt_relative, overrides)
    required_receipt_fields = (
        "support_manifest_hash",
        "runtime_canary_hash",
        "runtime_receipt_id_hash",
        "runtime_canary_raw_revision_ids_hash",
    )
    missing_receipt_contract = [
        field for field in required_receipt_fields if field not in receipt_text
    ]
    if missing_receipt_contract:
        findings.append(
            _finding(
                "runtime_receipt_contract_missing",
                "runtime receipts omit current content-bound fields: "
                + ", ".join(missing_receipt_contract),
                path=receipt_relative,
            )
        )

    docs_relative = "docs/AGENT_SOURCE_SUPPORT_MANIFEST.md"
    docs_text = _read_optional_text(root, docs_relative, overrides)
    if (
        "agent_source_support_manifest.json" not in docs_text
        or "ingestion-only" not in docs_text
        or "nonconforming" not in docs_text
        or "structural_source_observation" not in docs_text
        or "AgentRuntimeReceiptStore" not in docs_text
        or "runtime_canary_hash" not in docs_text
        or "runtime_receipt_id_hash" not in docs_text
        or "canonical Raw" not in docs_text
    ):
        findings.append(
            _finding(
                "support_manifest_docs_missing",
                "tracked support-manifest documentation is absent or incomplete",
                path=docs_relative,
            )
        )

    pyproject_relative = "pyproject.toml"
    pyproject_text = _read_text(root, pyproject_relative, overrides)
    if 'core = ["*.json"]' not in pyproject_text:
        findings.append(
            _finding(
                "support_manifest_package_data_missing",
                "package data does not include the tracked JSON manifest",
                path=pyproject_relative,
            )
        )

    return findings, list(dict.fromkeys(owner_paths))


def audit_agent_source_support_manifest(
    project_root: Path = PROJECT_ROOT,
    *,
    manifest_path: Path | None = None,
    code_overrides: Mapping[str, str] | None = None,
    runtime_evidence: Path | Mapping[str, Any] | None = None,
    require_runtime_evidence: bool = False,
) -> dict[str, Any]:
    """Return a fail-closed audit report without importing runtime consumers."""
    root = Path(project_root).resolve()
    overrides = dict(code_overrides or {})
    manifest_file = Path(manifest_path or root / MANIFEST_RELATIVE_PATH)
    payload, findings = _load_manifest(manifest_file)
    parser_classes = _source_classes(root)
    specs: dict[str, Mapping[str, Any]] = {}
    if payload is not None:
        schema_findings, specs = _validate_manifest_schema(payload, parser_classes)
        findings.extend(schema_findings)
    derivation_findings, owner_paths = _audit_runtime_derivation(root, overrides)
    findings.extend(derivation_findings)

    host_names = sorted(name for name, spec in specs.items() if spec.get("role") == "host_agent")
    ingestion_names = sorted(
        name for name, spec in specs.items() if spec.get("role") == "ingestion_only"
    )
    retired_names = sorted(name for name, spec in specs.items() if spec.get("role") == "retired")
    if len(host_names) != 8:
        findings.append(
            _finding(
                "host_agent_denominator_mismatch",
                f"expected 8 host agents, got {len(host_names)}",
            )
        )
    if len(ingestion_names) != 4:
        findings.append(
            _finding(
                "ingestion_only_denominator_mismatch",
                f"expected 4 ingestion-only sources, got {len(ingestion_names)}",
            )
        )

    retired_callers = 0
    if retired_names:
        runtime_text = "\n".join(
            _read_text(root, relative, overrides)
            for relative in (
                "core/agent_kit/protocol.py",
                "core/sync_framework/registry.py",
                "core/agent_kit/report.py",
                "core/diagnostics.py",
                "core/application/facade.py",
            )
        )
        retired_callers = sum(
            f'"{name}"' in runtime_text or f"'{name}'" in runtime_text for name in retired_names
        )
        if retired_callers:
            findings.append(
                _finding(
                    "retired_source_callers",
                    "retired source still has a runtime caller",
                )
            )

    manifest_hash = _canonical_hash(payload) if payload is not None else ""
    evidence_payload, evidence_findings = _load_runtime_evidence(runtime_evidence)
    findings.extend(evidence_findings)
    findings.extend(
        _validate_runtime_evidence_envelope(
            evidence_payload,
            manifest_hash=manifest_hash,
            specs=specs,
        )
    )
    runtime_report, runtime_findings = _audit_runtime_evidence(
        evidence_payload,
        specs,
        manifest_hash,
    )
    findings.extend(runtime_findings)
    if require_runtime_evidence and runtime_report["snapshot_record_count"] == 0:
        findings.append(
            _finding(
                "runtime_evidence_missing",
                "a daemon/backfill runtime report with at least one source snapshot is required",
            )
        )

    blocking_count = len(findings)
    codes = {finding["code"] for finding in findings}
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "structural_contract_ok": blocking_count == 0,
        "certifying": False,
        "release_eligible": False,
        "runtime_full_power_verifier": "mnemos_cli.py agent kit --json",
        "runtime_evidence_structural_only": _report_is_structural_only(evidence_payload),
        "manifest_path": str(manifest_file),
        "source_definition_owner_count": len(owner_paths),
        "source_definition_owners": owner_paths,
        "builtin_source_count": len(parser_classes),
        "manifest_source_count": len(specs),
        "host_agents": host_names,
        "ingestion_only_sources": ingestion_names,
        "retired_sources": retired_names,
        "builtin_source_without_manifest": sum(
            finding["code"] == "builtin_source_without_manifest" for finding in findings
        ),
        "manifest_without_parser": sum(
            finding["code"] == "manifest_without_parser" for finding in findings
        ),
        "installed_source_silently_ignored": (
            4 if "installed_source_silently_ignored" in codes else 0
        ),
        "retired_source_callers": retired_callers,
        "manifest_hash": manifest_hash,
        **runtime_report,
        "blocking_count": blocking_count,
        "ok": blocking_count == 0,
        "findings": findings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", default="", help="Override the manifest path for an isolated audit"
    )
    parser.add_argument(
        "--runtime-evidence",
        default="",
        help="Read a daemon/backfill JSON report and validate observed snapshots/receipts",
    )
    parser.add_argument(
        "--require-runtime-evidence",
        action="store_true",
        help="Fail when no observed native source snapshot was supplied",
    )
    parser.add_argument(
        "--strict", action="store_true", help="Exit non-zero for any blocking finding"
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = audit_agent_source_support_manifest(
        manifest_path=Path(args.manifest).resolve() if args.manifest else None,
        runtime_evidence=Path(args.runtime_evidence).resolve() if args.runtime_evidence else None,
        require_runtime_evidence=args.require_runtime_evidence,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "AgentSource support manifest: "
            f"{'PASS' if report['ok'] else 'FAIL'} "
            f"({report['blocking_count']} blockers)"
        )
        for finding in report["findings"]:
            print(f"- {finding['code']}: {finding['message']}")
    return 1 if args.strict and not report["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
