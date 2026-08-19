"""Private implementation module for successor_d0_generation.model."""

from __future__ import annotations

from dataclasses import dataclass

from pathlib import Path

from typing import Any

from typing import Iterable

from typing import Mapping

from typing import Sequence

import hashlib

import json

import re

MANIFEST_SCHEMA = "mnemos.cognitive_successor_d0.manifest.v1"

ARTIFACT_SCHEMAS = {
    "requirements": "mnemos.cognitive_successor_d0.requirements.v1",
    "surfaces": "mnemos.cognitive_successor_d0.surfaces.v1",
    "capabilities": "mnemos.cognitive_successor_d0.capabilities.v1",
    "tests_oracles": "mnemos.cognitive_successor_d0.tests_oracles.v1",
    "coverage_edges": "mnemos.cognitive_successor_d0.coverage_edges.v1",
}

ARTIFACT_ORDER = tuple(ARTIFACT_SCHEMAS)

REQUIRED_SOURCE_BINDINGS = (
    (
        "phase_contract_asset_authority",
        "document_asset_manifest",
        "docs/acceptance/document_asset_manifest.json",
    ),
    (
        "schema_owner_manifest",
        "schema_manifest",
        "docs/acceptance/schema_owner_manifest.json",
    ),
    (
        "source_support_manifest",
        "source_manifest",
        "core/agent_kit/agent_source_support_manifest.json",
    ),
    (
        "function_matrix",
        "function_manifest",
        "docs/acceptance/function_matrix.json",
    ),
    (
        "requirement_test_manifest",
        "requirement_manifest",
        "docs/acceptance/cognitive_requirement_test_manifest.json",
    ),
    (
        "phase1_requirement_source",
        "requirement_source",
        "scripts/phase1_governance_data.json",
    ),
    (
        "behavior_scenarios",
        "oracle_seed",
        "docs/acceptance/cognitive_behavior_scenarios.json",
    ),
    (
        "ops_resilience_matrix",
        "oracle_seed",
        "docs/acceptance/ops_resilience_matrix.json",
    ),
    (
        "runtime_interfaces",
        "oracle_seed",
        "docs/acceptance/cognitive_runtime_interface_manifest.json",
    ),
    (
        "audit_artifact_registry",
        "oracle_seed",
        "docs/acceptance/audit_artifact_registry.json",
    ),
    (
        "release_manifest",
        "oracle_seed",
        "docs/acceptance/cognitive_release_manifest.json",
    ),
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

V1_DISCOVERY_ONLY_MISSING_MECHANISMS = (
    "typed_adjudication_receipts",
    "canonical_owner_registry",
    "effect_target_registry",
    "independent_oracle_receipts",
    "config_applicability_attestation",
    "complete_reverse_state_effect_inventory",
)

REQUIRED_INDEPENDENT_INVENTORY_FAMILIES = (
    "state_writer_sites",
    "filesystem_vault_cas_paths_and_writers",
    "config_keyring_and_model_artifacts",
    "external_effect_targets_and_dispatch_sites",
    "projection_activation_sites",
)

SUCCESSOR_CONSTITUTION_REQUIREMENT_IDS = (
    "SUCCESSOR-CONSTITUTION-001",
    "SUCCESSOR-CONSTITUTION-002",
)

SUCCESSOR_CONSTITUTION_ANCHORS = (
    "accepted-principle:complete-function-denominator",
    "accepted-principle:legacy-frozen-oracle-rollback",
)

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

_COMMON_RECORD_FIELDS = {
    "schema_version",
    "record_type",
    "record_id",
    "discovery_key",
    "record_status",
    "evidence_refs",
}

_EXACT_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")

_DDL_PATTERN = re.compile(
    r"\b(?:"
    r"CREATE\s+(?:(?:TEMP(?:ORARY)?|UNIQUE|VIRTUAL)\s+)*"
    r"(?:TABLE|INDEX|VIEW|TRIGGER)"
    r"|(?:ALTER|DROP)\s+(?:TABLE|INDEX|VIEW|TRIGGER)"
    r")\b",
    re.IGNORECASE,
)

_TEST_PATH_PATTERN = re.compile(r"tests/[A-Za-z0-9_./-]+\.py(?:\:\:[A-Za-z0-9_./\[\]-]+)*")

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


class CatalogInputError(RuntimeError):
    """The requested snapshot cannot be inspected safely or exactly."""


def _reject_json_constant(token: str) -> None:
    raise ValueError(f"non-I-JSON constant: {token}")


@dataclass(frozen=True)
class CatalogRequest:
    """Inputs a caller must provide at the D0 generator Seam."""

    repo_root: Path
    legacy_commit: str
    design_path: Path
    phase_contract_path: Path
    config_snapshot: Path | None = None


@dataclass(frozen=True)
class CatalogBundle:
    """Exact generated bytes plus the manifest describing those bytes."""

    manifest: Mapping[str, Any]
    artifacts: Mapping[str, bytes]

    @property
    def blocked(self) -> bool:
        return self.manifest.get("bundle_status") == "BLOCKED"


def canonical_json(value: Any) -> str:
    """Return the canonical JSON representation used by all D0 hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8") + b"\n"


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _set_sha256(values: Iterable[str]) -> str:
    return sha256_bytes(canonical_json_bytes(sorted(set(values))))


def _stable_digest(value: Any, *, length: int = 24) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]


def _slug(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", ".", str(value).strip().lower())
    return normalized.strip(".") or "unknown"


def _record(
    artifact: str,
    *,
    record_id: str,
    discovery_key: str,
    record_status: str,
    evidence_refs: Sequence[Mapping[str, Any]],
    **fields: Any,
) -> dict[str, Any]:
    return {
        "schema_version": ARTIFACT_SCHEMAS[artifact],
        "record_type": artifact,
        "record_id": record_id,
        "discovery_key": discovery_key,
        "record_status": record_status,
        "evidence_refs": [dict(item) for item in evidence_refs],
        **fields,
    }


def _edge(
    *,
    from_id: str,
    relation: str,
    to_id: str,
    facet: str,
    evidence_refs: Sequence[Mapping[str, Any]],
    assertion_authority: str = "MECHANICAL",
) -> dict[str, Any]:
    identity = {
        "from_id": from_id,
        "relation": relation,
        "to_id": to_id,
        "facet": facet,
    }
    digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return _record(
        "coverage_edges",
        record_id=f"edge:sha256:{digest}",
        discovery_key=f"edge:{relation}:{from_id}:{to_id}:{facet}",
        record_status="ACTIVE",
        evidence_refs=evidence_refs,
        **identity,
        assertion_authority=assertion_authority,
        decision_ref=None,
    )
