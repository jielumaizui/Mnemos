"""Private implementation module for successor_d0_verification.wire."""

from __future__ import annotations

from collections import Counter

from dataclasses import dataclass

from pathlib import Path

from typing import Any

from typing import Iterable

from typing import Mapping

from typing import Sequence

import hashlib

import json

import os

import re

import stat

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_BUNDLE = ROOT / "docs" / "acceptance" / "cognitive_successor_d0"

REPORT_SCHEMA = "mnemos.cognitive_successor_d0.verification.v1"

MANIFEST_SCHEMA = "mnemos.cognitive_successor_d0.manifest.v1"

CLOSURE_SCHEMA = "mnemos.cognitive_successor_d0.closure.v1"

ARTIFACT_ORDER = (
    "requirements",
    "surfaces",
    "capabilities",
    "tests_oracles",
    "coverage_edges",
)

ARTIFACT_SCHEMAS = {
    artifact: f"mnemos.cognitive_successor_d0.{artifact}.v1" for artifact in ARTIFACT_ORDER
}

ARTIFACT_METADATA_FIELDS = frozenset(
    {
        "artifact_id",
        "path",
        "schema_version",
        "record_type",
        "record_count",
        "record_id_set_sha256",
        "discovery_key_set_sha256",
        "record_root_sha256",
        "sha256",
        "byte_length",
    }
)

REQUIRED_BINDING_KINDS = {
    "successor_d0_design": ("external_exact_file", None),
    "phase0_7_global_engineering_contract": ("external_exact_file", None),
    "phase_contract_asset_authority": (
        "legacy_repo_file",
        "docs/acceptance/document_asset_manifest.json",
    ),
    "schema_owner_manifest": (
        "legacy_repo_file",
        "docs/acceptance/schema_owner_manifest.json",
    ),
    "source_support_manifest": (
        "legacy_repo_file",
        "core/agent_kit/agent_source_support_manifest.json",
    ),
    "function_matrix": (
        "legacy_repo_file",
        "docs/acceptance/function_matrix.json",
    ),
    "requirement_test_manifest": (
        "legacy_repo_file",
        "docs/acceptance/cognitive_requirement_test_manifest.json",
    ),
    "phase1_requirement_source": (
        "legacy_repo_file",
        "scripts/phase1_governance_data.json",
    ),
    "behavior_scenarios": (
        "legacy_repo_file",
        "docs/acceptance/cognitive_behavior_scenarios.json",
    ),
    "ops_resilience_matrix": (
        "legacy_repo_file",
        "docs/acceptance/ops_resilience_matrix.json",
    ),
    "runtime_interfaces": (
        "legacy_repo_file",
        "docs/acceptance/cognitive_runtime_interface_manifest.json",
    ),
    "audit_artifact_registry": (
        "legacy_repo_file",
        "docs/acceptance/audit_artifact_registry.json",
    ),
    "release_manifest": (
        "legacy_repo_file",
        "docs/acceptance/cognitive_release_manifest.json",
    ),
}

MAX_EXTERNAL_BINDING_BYTES = 64 * 1024 * 1024

GIT_COMMAND_TIMEOUT_SECONDS = 60

GIT_ARCHIVE_TIMEOUT_SECONDS = 120

MAX_SNAPSHOT_FILE_COUNT = 20_000

MAX_SNAPSHOT_BLOB_BYTES = 64 * 1024 * 1024

MAX_SNAPSHOT_TOTAL_BYTES = 512 * 1024 * 1024

MAX_SNAPSHOT_ARCHIVE_BYTES = MAX_SNAPSHOT_TOTAL_BYTES + MAX_SNAPSHOT_FILE_COUNT * 4096 + 1024 * 1024

MAX_MANIFEST_BYTES = 4 * 1024 * 1024

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024

MAX_JSONL_LINE_BYTES = 1024 * 1024

MAX_JSONL_RECORDS = 100_000

ALLOWED_RELATIONS = {
    "REQUIREMENT_SATISFIED_BY_CAPABILITY",
    "SURFACE_EXPOSES_CAPABILITY",
    "CAPABILITY_VERIFIED_BY_ORACLE",
}

REQUIRED_RECORD_FIELDS = {
    "schema_version",
    "record_type",
    "record_id",
    "discovery_key",
    "record_status",
    "evidence_refs",
}

REQUIRED_ARTIFACT_FIELDS = {
    "requirements": {
        "requirement_id",
        "title",
        "kind",
        "normative_statement",
        "source_refs",
        "scope",
        "acceptance_observables",
        "risk_level",
        "release_blocking",
        "contract_status",
        "conflicts_with",
        "supersedes",
        "decision_ref",
    },
    "surfaces": {
        "kind",
        "canonical_selector",
        "surface_family_id",
        "facet_contract",
        "principal_policy_ref",
        "input_contract_ref",
        "output_contract_ref",
        "lifecycle",
        "decision_ref",
    },
    "capabilities": {
        "capability_id",
        "title",
        "domain",
        "contract_revision",
        "input_contract",
        "output_contract",
        "state_contract",
        "effect_contracts",
        "failure_contract",
        "data_contract",
        "security_privacy_contract",
        "performance_contract_ref",
        "migration_rollback_contract",
        "legacy_behavior_state",
        "surface_refs",
        "requirement_refs",
        "oracle_refs",
        "contract_status",
        "decision_ref",
    },
    "tests_oracles": {
        "kind",
        "runner",
        "source_anchors",
        "fixture_refs",
        "mutation_operator_ids",
        "fault_model_ids",
        "asserted_observables",
        "evidence_schema",
        "population_policy",
        "independence_class",
        "release_blocking",
        "decision_ref",
    },
    "coverage_edges": {
        "from_id",
        "relation",
        "to_id",
        "facet",
        "assertion_authority",
        "decision_ref",
    },
}

SURFACE_KINDS = {
    "application_facade",
    "chronos_step",
    "cli",
    "cli_argument_facet",
    "cli_dispatch_route",
    "console_script",
    "daemon_mode",
    "daemon_mode_facet",
    "daemon_service",
    "daemon_service_alias",
    "dynamic_trigger_registry",
    "event_policy",
    "event_subscription",
    "event_subscription_wildcard",
    "health_check",
    "kia_module",
    "mcp_protocol",
    "mcp_tool",
    "repo_entry_challenger",
    "schema_owner_seed",
    "script_module",
    "agent_source",
}

ORACLE_KINDS = {
    "audit_artifact",
    "behavior_scenario_bundle",
    "function_validation_command",
    "ops_resilience_control",
    "release_certificate",
    "release_gate",
    "requirement_pytest_node",
    "runtime_target_effect_oracle",
    "pytest_file",
}

ALLOWED_RECORD_STATUSES = {
    "ACTIVE",
    "ADJUDICATION_REQUIRED",
    "ALIAS",
    "CONTRACTED",
    "DECLARED_NOT_INDEPENDENT",
    "DENOMINATOR_DECLARED_NOT_EXECUTED",
    "DISCOVERED",
    "INVALID",
    "LINKED_DECLARATION",
    "MISSING",
    "REGISTERED",
    "REGISTERED_CANDIDATE",
    "STALE_SOURCE",
    "UNKNOWN",
    "UNLINKED",
    "UNREGISTERED",
}

V1_ORACLE_INDEPENDENCE_CLASSES = {
    "DECLARED_NOT_INDEPENDENT",
    "REQUIRED_NOT_OBSERVED",
    "UNCLASSIFIED_TEST_FILE",
    "UNKNOWN",
}

DISCOVERY_ONLY_SCHEMAS = frozenset({MANIFEST_SCHEMA})

FREEZE_CAPABLE_SCHEMAS: frozenset[str] = frozenset()

SUCCESSOR_CONSTITUTION_CONTRACT = {
    "SUCCESSOR-CONSTITUTION-001": {
        "record_id": "req:successor-constitution-001",
        "discovery_key": "requirement:SUCCESSOR-CONSTITUTION-001",
        "anchor": "accepted-principle:complete-function-denominator",
        "title": "Complete effective capability denominator before cutover",
        "normative_statement": (
            "Development may proceed by capability cluster, but final cutover "
            "requires 100% of the approved effective capability denominator; "
            "no effective function may be deleted or permanently deferred."
        ),
    },
    "SUCCESSOR-CONSTITUTION-002": {
        "record_id": "req:successor-constitution-002",
        "discovery_key": "requirement:SUCCESSOR-CONSTITUTION-002",
        "anchor": "accepted-principle:legacy-frozen-oracle-rollback",
        "title": "Keep legacy Mnemos frozen as oracle and rollback engine",
        "normative_statement": (
            "Before cutover, legacy Mnemos remains frozen and available, with "
            "changes limited to highest-priority security or data-corruption "
            "repairs, and serves as data source, behavior reference, offline "
            "oracle, and rollback engine."
        ),
    },
}

SUCCESSOR_CONSTITUTION_ANCHORS = tuple(
    str(contract["anchor"])
    for _requirement_id, contract in sorted(SUCCESSOR_CONSTITUTION_CONTRACT.items())
)

REQUIRED_INDEPENDENT_INVENTORY_FAMILIES = frozenset(
    {
        "state_writer_sites",
        "filesystem_vault_cas_paths_and_writers",
        "config_keyring_and_model_artifacts",
        "external_effect_targets_and_dispatch_sites",
        "projection_activation_sites",
    }
)

REQUIRED_ZERO_FIELDS = (
    "freeze_evaluator_unimplemented",
    "surface_unmapped",
    "behavior_without_surface",
    "requirement_without_capability_or_adjudication",
    "capability_without_requirement_or_adjudication",
    "capability_without_independent_test_or_oracle",
    "test_without_capability_or_adjudication",
    "test_file_without_disposition",
    "declared_missing_test_file",
    "canonical_owner_unknown",
    "effect_target_unknown",
    "parameter_mode_unclassified",
    "script_entry_unclassified",
    "script_parameter_contract_unknown",
    "dynamic_trigger_unclassified",
    "contract_conflict_unresolved",
    "effective_capability_excluded",
    "independent_inventory_diff",
    "independent_inventory_pending_family",
    "missing_required_source_binding",
    "config_applicability_attestation_gap",
    "constitution_requirement_missing",
    "constitution_approval_missing",
    "duplicate_record_id",
    "duplicate_discovery_key",
    "invalid_record",
    "generator_error",
    "unresolved_adjudication",
)

CLOSURE_SUPPLEMENTAL_COUNT_FIELDS = (
    "test_file_denominator",
    "test_file_linked_declaration",
    "test_file_unlinked",
)

CLOSURE_COUNT_FIELDS = REQUIRED_ZERO_FIELDS + CLOSURE_SUPPLEMENTAL_COUNT_FIELDS

CLOSURE_FIELDS = frozenset(
    {
        "schema_version",
        "required_zero_fields",
        "counts",
        "local_ok",
        "verification_pending",
        "frozen_eligible",
    }
)

CANONICALIZATION_CONTRACT = {
    "json": (
        "json.dumps(ensure_ascii=False,sort_keys=True," "separators=(',',':'),allow_nan=False)"
    ),
    "jsonl": "canonical-json UTF-8 plus one LF per record",
    "record_order": "ascending (record_id,discovery_key)",
    "set_hash": "sha256(canonical-json(sorted(unique strings))+LF)",
    "record_root": "sha256(canonical-json(ordered sha256 exact-line strings)+LF)",
    "digest_encoding": "sha256:<lowercase-hex>",
}

MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "bundle_status",
        "release_eligible",
        "denominator_frozen",
        "denominator_approved",
        "verification_scope",
        "legacy_snapshot",
        "config_snapshot",
        "source_bindings",
        "generator_identity",
        "canonicalization",
        "inventory_metrics",
        "artifact_order",
        "artifacts",
        "closure",
        "finding_counts",
        "findings",
    }
)

INVENTORY_METRIC_FIELDS = frozenset({"main_cli", "mcp"})

MAIN_CLI_METRIC_FIELDS = frozenset(
    {
        "enumeration_mode",
        "parameter_definition_basis",
        "command_node_count",
        "top_command_count",
        "leaf_count",
        "parameter_action_count",
        "optional_action_count",
        "positional_action_count",
        "boolean_action_count",
        "choice_action_count",
        "choice_value_count",
        "effective_parameter_facet_count",
        "effective_optional_facet_count",
        "effective_positional_facet_count",
        "effective_boolean_facet_count",
        "effective_choice_facet_count",
        "effective_choice_value_count",
    }
)

MCP_METRIC_FIELDS = frozenset(
    {
        "tool_union_count",
        "registered_tool_count",
        "schema_tool_count",
        "categorized_tool_count",
        "policy_tool_count",
        "category_registry_gap_tool_names",
    }
)

INTEGRITY_CODES = {
    "ARTIFACT_BYTES_INVALID",
    "ARTIFACT_METADATA_MISMATCH",
    "ARTIFACT_MISSING",
    "BINDING_HASH_MISMATCH",
    "BINDING_MISSING",
    "CLOSURE_MISMATCH",
    "DUPLICATE_DISCOVERY_KEY",
    "DUPLICATE_RECORD_ID",
    "EDGE_ENDPOINT_MISSING",
    "EDGE_INVENTORY_DIFF",
    "ENUMERATOR_FAILED",
    "EVIDENCE_REF_INVALID",
    "INDEPENDENT_INVENTORY_DIFF",
    "INDEPENDENT_INVENTORY_INCOMPLETE",
    "INVALID_RECORD",
    "MANIFEST_INVALID",
    "RESOURCE_LIMIT_EXCEEDED",
    "SNAPSHOT_MISMATCH",
    "SOURCE_BINDING_INVALID",
}

_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

_SHA256_REF = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    artifact: str
    record_id: str | None
    source_ref: str | None
    message: str
    repair_action: str


class _VerifierResourceLimit(ValueError):
    """A bounded verifier input exceeded its declared resource budget."""


def _read_bounded_regular_file(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    require_absolute: bool = False,
) -> bytes:
    if require_absolute and not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} must be a regular non-symlink file")
        if metadata.st_size > max_bytes:
            raise _VerifierResourceLimit(f"{label} exceeds limit {max_bytes}: {metadata.st_size}")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"{label} changed during exact read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"{label} grew during exact read")
        final_metadata = os.fstat(descriptor)
        if (
            final_metadata.st_size != metadata.st_size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
            or final_metadata.st_dev != metadata.st_dev
            or final_metadata.st_ino != metadata.st_ino
        ):
            raise ValueError(f"{label} changed during exact read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_exact_regular_file(path: Path) -> bytes:
    return _read_bounded_regular_file(
        path,
        max_bytes=MAX_EXTERNAL_BINDING_BYTES,
        label="external binding override",
        require_absolute=True,
    )


def _finding(
    code: str,
    artifact: str,
    message: str,
    repair_action: str,
    *,
    record_id: str | None = None,
    source_ref: str | None = None,
) -> Finding:
    return Finding(
        code=code,
        severity="blocking",
        artifact=artifact,
        record_id=record_id,
        source_ref=source_ref,
        message=message,
        repair_action=repair_action,
    )


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _reject_json_constant(token: str) -> None:
    raise ValueError(f"non-I-JSON constant: {token}")


def _json_loads(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_json_constant)


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _set_hash(values: Iterable[str]) -> str:
    return _sha256(_canonical_json_bytes(sorted(set(values))))


def _record_root(line_bytes: Sequence[bytes]) -> str:
    line_hashes = [_sha256(line) for line in line_bytes]
    return _sha256(_canonical_json_bytes(line_hashes))


def _json_file(path: Path) -> dict[str, Any]:
    value = _json_loads(path.read_bytes().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def _record_contract_errors(artifact_id: str, value: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = sorted((REQUIRED_RECORD_FIELDS | REQUIRED_ARTIFACT_FIELDS[artifact_id]) - set(value))
    if missing:
        errors.append(f"missing fields: {missing}")
    for field_name in ("record_id", "discovery_key", "record_status"):
        if not isinstance(value.get(field_name), str) or not value.get(field_name):
            errors.append(f"{field_name} must be a non-empty string")
    if value.get("record_status") not in ALLOWED_RECORD_STATUSES:
        errors.append("record_status is not allowed by the discovery-only v1 wire schema")
    if value.get("decision_ref") is not None:
        errors.append("decision_ref must be null until typed adjudication receipts exist")
    if value.get("schema_version") != ARTIFACT_SCHEMAS[artifact_id]:
        errors.append("schema_version differs from the verifier-owned wire schema")
    if value.get("record_type") != artifact_id:
        errors.append("record_type differs from the artifact book")
    evidence_refs = value.get("evidence_refs")
    if not isinstance(evidence_refs, list):
        errors.append("evidence_refs must be a list")
    else:
        for index, evidence in enumerate(evidence_refs):
            if not isinstance(evidence, dict):
                errors.append(f"evidence_refs[{index}] must be an object")
                continue
            if not isinstance(evidence.get("path"), str) or not evidence.get("path"):
                errors.append(f"evidence_refs[{index}].path must be non-empty")
            if not isinstance(evidence.get("anchor", ""), str):
                errors.append(f"evidence_refs[{index}].anchor must be a string")
            digest = evidence.get("sha256")
            if digest is not None and (
                not isinstance(digest, str) or not _SHA256_REF.fullmatch(digest)
            ):
                errors.append(f"evidence_refs[{index}].sha256 is invalid")
    if artifact_id == "requirements":
        requirement_id = value.get("requirement_id")
        if not isinstance(requirement_id, str) or not requirement_id:
            errors.append("requirement_id must be a non-empty string")
        if value.get("kind") == "SUCCESSOR_CONSTITUTION":
            contract = SUCCESSOR_CONSTITUTION_CONTRACT.get(str(requirement_id))
            if contract is None:
                errors.append("unknown successor constitution requirement")
            else:
                expected_fields = {
                    "record_id": contract["record_id"],
                    "discovery_key": contract["discovery_key"],
                    "record_status": "CONTRACTED",
                    "title": contract["title"],
                    "normative_statement": contract["normative_statement"],
                    "scope": "successor_cutover",
                    "risk_level": "CRITICAL",
                    "release_blocking": True,
                    "contract_status": "CONTRACTED_PENDING_TYPED_APPROVAL",
                    "source_refs": {
                        "binding_id": "successor_d0_design",
                        "anchor": contract["anchor"],
                        "approval_receipt": None,
                    },
                    "acceptance_observables": {
                        "exact_set_equality_required": True,
                        "permanent_deferral_allowed": False,
                    },
                }
                for field_name, expected_value in expected_fields.items():
                    if value.get(field_name) != expected_value:
                        errors.append(
                            f"successor constitution {field_name} differs from the "
                            "verifier-owned v1 contract"
                        )
    elif artifact_id == "surfaces":
        if value.get("kind") not in SURFACE_KINDS:
            errors.append(f"unknown surface kind: {value.get('kind')!r}")
        if not isinstance(value.get("facet_contract"), dict):
            errors.append("facet_contract must be an object")
    elif artifact_id == "capabilities":
        if value.get("capability_id") != value.get("record_id"):
            errors.append("capability_id must equal record_id")
        if not isinstance(value.get("state_contract"), dict):
            errors.append("state_contract must be an object")
        if not isinstance(value.get("effect_contracts"), list):
            errors.append("effect_contracts must be a list")
    elif artifact_id == "tests_oracles":
        if value.get("kind") not in ORACLE_KINDS:
            errors.append(f"unknown oracle kind: {value.get('kind')!r}")
        if not isinstance(value.get("runner"), dict):
            errors.append("runner must be an object")
        if not isinstance(value.get("source_anchors"), list):
            errors.append("source_anchors must be a list")
        if value.get("independence_class") not in V1_ORACLE_INDEPENDENCE_CLASSES:
            errors.append(
                "independence_class cannot claim verified independence in discovery-only v1"
            )
    elif artifact_id == "coverage_edges":
        if value.get("relation") not in ALLOWED_RELATIONS:
            errors.append(f"unknown relation: {value.get('relation')!r}")
        for field_name in ("from_id", "to_id", "facet", "assertion_authority"):
            if not isinstance(value.get(field_name), str) or not value.get(field_name):
                errors.append(f"{field_name} must be a non-empty string")
    return errors


def _read_artifact(
    bundle_dir: Path,
    artifact_id: str,
    metadata: Mapping[str, Any],
    findings: list[Finding],
) -> list[dict[str, Any]]:
    if set(metadata) != ARTIFACT_METADATA_FIELDS:
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                artifact_id,
                "artifact metadata fields differ from the closed v1 contract",
                "emit exactly the verifier-owned artifact metadata fields",
            )
        )
    if (
        metadata.get("artifact_id") != artifact_id
        or metadata.get("path") != f"{artifact_id}.jsonl"
        or metadata.get("schema_version") != ARTIFACT_SCHEMAS[artifact_id]
        or metadata.get("record_type") != artifact_id
    ):
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                artifact_id,
                "artifact metadata differs from the verifier-owned wire schema",
                "regenerate under the fixed cognitive-successor D0 v1 schema",
            )
        )
    relative = metadata.get("path")
    if not isinstance(relative, str) or not relative:
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                artifact_id,
                "artifact path is absent or non-string",
                "regenerate the manifest from the bound catalog generator",
            )
        )
        return []
    candidate = bundle_dir / relative
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(bundle_dir.resolve(strict=True))
        raw = _read_bounded_regular_file(
            candidate,
            max_bytes=MAX_ARTIFACT_BYTES,
            label=f"{artifact_id} artifact",
        )
    except _VerifierResourceLimit as exc:
        findings.append(
            _finding(
                "RESOURCE_LIMIT_EXCEEDED",
                artifact_id,
                str(exc),
                "reduce the artifact to the fixed D0 resource budget and regenerate",
                source_ref=relative,
            )
        )
        return []
    except (OSError, RuntimeError, ValueError) as exc:
        findings.append(
            _finding(
                "ARTIFACT_MISSING",
                artifact_id,
                str(exc),
                "restore the exact generated artifact inside the bundle directory",
                source_ref=str(candidate),
            )
        )
        return []

    expected_sha = metadata.get("sha256")
    expected_bytes = metadata.get("byte_length")
    if (
        not isinstance(expected_sha, str)
        or not _SHA256_REF.fullmatch(expected_sha)
        or _sha256(raw) != expected_sha
        or len(raw) != expected_bytes
    ):
        findings.append(
            _finding(
                "ARTIFACT_METADATA_MISMATCH",
                artifact_id,
                "exact byte hash or byte length differs from manifest",
                "regenerate all catalog artifacts and discard stale verification receipts",
                source_ref=relative,
            )
        )

    if raw and not raw.endswith(b"\n"):
        findings.append(
            _finding(
                "ARTIFACT_BYTES_INVALID",
                artifact_id,
                "JSONL artifact does not end with exactly one LF per record",
                "serialize canonical UTF-8 JSONL and regenerate the bundle",
                source_ref=relative,
            )
        )
    raw_line_count = raw.count(b"\n") + int(bool(raw) and not raw.endswith(b"\n"))
    if raw_line_count > MAX_JSONL_RECORDS:
        findings.append(
            _finding(
                "RESOURCE_LIMIT_EXCEEDED",
                artifact_id,
                f"JSONL record count exceeds limit {MAX_JSONL_RECORDS}",
                "reduce the artifact record count and regenerate",
                source_ref=relative,
            )
        )
        return []
    raw_lines = raw.splitlines(keepends=True)
    records: list[dict[str, Any]] = []
    parsed_lines: list[bytes] = []
    for index, line in enumerate(raw_lines, start=1):
        if len(line) > MAX_JSONL_LINE_BYTES:
            findings.append(
                _finding(
                    "RESOURCE_LIMIT_EXCEEDED",
                    artifact_id,
                    f"JSONL line {index} exceeds limit {MAX_JSONL_LINE_BYTES}",
                    "split or reduce the oversized record and regenerate",
                    source_ref=f"{relative}:{index}",
                )
            )
            return []
        if line in {b"", b"\n", b"\r\n"}:
            findings.append(
                _finding(
                    "ARTIFACT_BYTES_INVALID",
                    artifact_id,
                    f"blank JSONL record at line {index}",
                    "remove blank records and regenerate the bundle",
                    source_ref=f"{relative}:{index}",
                )
            )
            continue
        try:
            value = _json_loads(line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            findings.append(
                _finding(
                    "ARTIFACT_BYTES_INVALID",
                    artifact_id,
                    f"invalid JSONL at line {index}: {exc}",
                    "regenerate the artifact from the immutable source snapshot",
                    source_ref=f"{relative}:{index}",
                )
            )
            continue
        if not isinstance(value, dict):
            findings.append(
                _finding(
                    "INVALID_RECORD",
                    artifact_id,
                    f"line {index} is not a JSON object",
                    "emit one typed object per JSONL record",
                    source_ref=f"{relative}:{index}",
                )
            )
            continue
        parsed_lines.append(line)
        try:
            canonical = _canonical_json_bytes(value)
        except (TypeError, ValueError, UnicodeError) as exc:
            findings.append(
                _finding(
                    "INVALID_RECORD",
                    artifact_id,
                    f"line {index} is outside the canonical I-JSON subset: {exc}",
                    "remove non-finite numbers and invalid Unicode, then regenerate",
                    source_ref=f"{relative}:{index}",
                )
            )
            continue
        if canonical != line:
            findings.append(
                _finding(
                    "ARTIFACT_BYTES_INVALID",
                    artifact_id,
                    f"line {index} is not canonical JSONL",
                    "sort keys, remove insignificant whitespace, and use one LF",
                    record_id=str(value.get("record_id") or "") or None,
                    source_ref=f"{relative}:{index}",
                )
            )
        contract_errors = _record_contract_errors(artifact_id, value)
        if contract_errors:
            findings.append(
                _finding(
                    "INVALID_RECORD",
                    artifact_id,
                    f"line {index} violates the wire contract: {contract_errors}",
                    "regenerate the record with the complete D0 wire contract",
                    record_id=str(value.get("record_id") or "") or None,
                    source_ref=f"{relative}:{index}",
                )
            )
        if value.get("record_type") != metadata.get("record_type"):
            findings.append(
                _finding(
                    "INVALID_RECORD",
                    artifact_id,
                    f"line {index} record_type disagrees with manifest",
                    "do not mix record types within one artifact",
                    record_id=str(value.get("record_id") or "") or None,
                    source_ref=f"{relative}:{index}",
                )
            )
        if value.get("schema_version") != metadata.get("schema_version"):
            findings.append(
                _finding(
                    "INVALID_RECORD",
                    artifact_id,
                    f"line {index} schema_version disagrees with manifest",
                    "regenerate all records under one artifact schema",
                    record_id=str(value.get("record_id") or "") or None,
                    source_ref=f"{relative}:{index}",
                )
            )
        records.append(value)

    order = [(str(row.get("record_id", "")), str(row.get("discovery_key", ""))) for row in records]
    if order != sorted(order):
        findings.append(
            _finding(
                "ARTIFACT_BYTES_INVALID",
                artifact_id,
                "records are not sorted by (record_id, discovery_key)",
                "sort records deterministically and regenerate the artifact",
                source_ref=relative,
            )
        )
    record_ids = [item[0] for item in order]
    discovery_keys = [item[1] for item in order]
    for values, code, label in (
        (record_ids, "DUPLICATE_RECORD_ID", "record_id"),
        (discovery_keys, "DUPLICATE_DISCOVERY_KEY", "discovery_key"),
    ):
        duplicates = sorted(
            value for value, count in Counter(values).items() if value and count > 1
        )
        if duplicates:
            findings.append(
                _finding(
                    code,
                    artifact_id,
                    f"duplicate {label}: {duplicates[:20]}",
                    "assign one stable identity to each distinct record and regenerate",
                    source_ref=relative,
                )
            )
    if any(not value for value in record_ids + discovery_keys):
        findings.append(
            _finding(
                "INVALID_RECORD",
                artifact_id,
                "record_id and discovery_key must be non-empty",
                "assign source-owned identities before catalog generation",
                source_ref=relative,
            )
        )

    computed = {
        "record_count": len(records),
        "record_id_set_sha256": _set_hash(record_ids),
        "discovery_key_set_sha256": _set_hash(discovery_keys),
        "record_root_sha256": _record_root(parsed_lines),
    }
    for field, actual in computed.items():
        if metadata.get(field) != actual:
            findings.append(
                _finding(
                    "ARTIFACT_METADATA_MISMATCH",
                    artifact_id,
                    f"{field} expected={metadata.get(field)!r} actual={actual!r}",
                    "regenerate manifest metadata from exact artifact bytes",
                    source_ref=relative,
                )
            )
    return records


def _verify_manifest_findings(manifest: Mapping[str, Any], findings: list[Finding]) -> None:
    rows = manifest.get("findings")
    if not isinstance(rows, list):
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "findings",
                "manifest.findings must be a list",
                "emit the complete typed generator finding set",
            )
        )
        return
    invalid_rows = [
        index
        for index, row in enumerate(rows)
        if not isinstance(row, dict)
        or not isinstance(row.get("code"), str)
        or not row.get("code")
        or row.get("severity") not in {"BLOCKING", "WARNING"}
        or not isinstance(row.get("message"), str)
        or not isinstance(row.get("repair_action"), str)
    ]
    if invalid_rows:
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "findings",
                f"malformed generator findings at indexes {invalid_rows[:20]}",
                "regenerate the typed finding ledger",
            )
        )
    blocking_rows = [
        row for row in rows if isinstance(row, dict) and row.get("severity") == "BLOCKING"
    ]
    warning_rows = [
        row for row in rows if isinstance(row, dict) and row.get("severity") == "WARNING"
    ]
    declared_counts = manifest.get("finding_counts")
    if not isinstance(declared_counts, dict) or declared_counts != {
        "blocking": len(blocking_rows),
        "warning": len(warning_rows),
    }:
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "findings",
                "finding_counts differs from the typed finding rows",
                "derive finding_counts solely from the exact finding ledger",
            )
        )
    if blocking_rows:
        findings.append(
            _finding(
                "GENERATOR_BLOCKING_FINDING",
                "findings",
                f"generator reports {len(blocking_rows)} blocking findings: "
                f"{sorted({str(row.get('code')) for row in blocking_rows})}",
                "resolve the typed generator findings and regenerate before verification",
            )
        )
    expected_status = "BLOCKED"
    if manifest.get("bundle_status") != expected_status:
        findings.append(
            _finding(
                "MANIFEST_INVALID",
                "manifest",
                f"bundle_status must be {expected_status} for the exact closure/findings",
                "derive bundle_status from local closure and typed blocking findings",
            )
        )
