"""Canonical AgentSource support manifest and runtime snapshot contract.

Only ``agent_source_support_manifest.json`` defines which local sources are
hosts, ingestion-only parsers, or retired.  Runtime consumers may derive
views from this module, but they must not add a second editable source list.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from core.ops.durable_io import read_native_bytes

MANIFEST_FILE_NAME = "agent_source_support_manifest.json"
MANIFEST_SCHEMA_VERSION = "mnemos.agent_source_support_manifest.v1"
NATIVE_SOURCE_SNAPSHOT_SCHEMA_VERSION = "mnemos.native_source_snapshot.v2"
RUNTIME_REPORT_SCHEMA_VERSION = "mnemos.agent_source_runtime_report.v2"
RUNTIME_REPORT_PRODUCERS = frozenset({"scripts.backfill_raw_event_store", "daemon.raw_sync"})
MANIFEST_OWNER_PATH = "core/agent_kit/agent_source_support_manifest.json"
VALID_SOURCE_ROLES = frozenset({"host_agent", "ingestion_only", "retired"})
VALID_SOURCE_FIDELITIES = frozenset({"full", "derived", "experimental", "unknown"})
CONTINUOUS_CAPTURE_OWNER = "daemon.raw_sync"
CONTINUOUS_CAPTURE_SERVICE = "raw_sync"
CONTINUOUS_CAPTURE_ACTIVATION_KEY = "daemon.services.raw_sync"
CONTINUOUS_CAPTURE_CURSOR_KIND = "continuous_tail_reconcile_v1"
CONTINUOUS_CAPTURE_CURSOR_HASH_FIELDS = frozenset(
    {
        "capture_roster_hash",
        "capture_denominator_session_set_hash",
        "capture_expected_turn_fingerprint_set_hash",
        "capture_receipt_binding_set_hash",
    }
)
CONTINUOUS_CAPTURE_CURSOR_COUNT_FIELDS = frozenset(
    {
        "capture_expected_turn_count",
        "capture_receipt_count",
        "capture_exact_receipt_count",
        "capture_pending_turn_count",
        "capture_orphan_receipt_count",
    }
)
_REQUIRED_SOURCE_FIELDS = frozenset(
    {
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
)


class AgentSourceSupportManifestError(ValueError):
    """Raised when the canonical support manifest is missing or malformed."""


def _canonical_json(value: Any) -> str:
    return json.dumps(_thaw(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _valid_sha256(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentSourceSupportManifestError(f"{label} must be an object")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, list):
        return [_thaw(item) for item in value]
    return value


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise AgentSourceSupportManifestError(f"{label} must be a list of non-empty strings")
    return tuple(value)


def _validate_format_resolution(name: str, native: Mapping[str, Any]) -> None:
    """Validate optional manifest-owned multi-format identity contracts.

    OpenClaw uses this for trajectory, ordinary JSONL, and corpus artifacts.
    Its parser must derive glob, priority, and equivalence semantics from this
    one declaration instead of reintroducing a handwritten format roster.
    """
    resolution_raw = native.get("format_resolution")
    if resolution_raw is None:
        if name == "openclaw":
            raise AgentSourceSupportManifestError("openclaw.native.format_resolution is required")
        return
    resolution = _mapping(resolution_raw, f"{name}.native.format_resolution")
    variants_raw = resolution.get("variants")
    if not isinstance(variants_raw, (list, tuple)) or not variants_raw:
        raise AgentSourceSupportManifestError(
            f"{name}.native.format_resolution.variants must be a non-empty list"
        )
    source_kinds: set[str] = set()
    priorities: set[int] = set()
    variants_by_kind: dict[str, Mapping[str, Any]] = {}
    for index, raw_variant in enumerate(variants_raw):
        variant = _mapping(
            raw_variant,
            f"{name}.native.format_resolution.variants[{index}]",
        )
        source_kind = str(variant.get("source_kind") or "")
        path_glob = str(variant.get("path_glob") or "")
        priority = variant.get("priority")
        if not source_kind or not path_glob:
            raise AgentSourceSupportManifestError(
                f"{name}.native.format_resolution variant requires source_kind and path_glob"
            )
        if source_kind in source_kinds:
            raise AgentSourceSupportManifestError(
                f"{name}.native.format_resolution source_kind is duplicated: {source_kind}"
            )
        if not isinstance(priority, int) or isinstance(priority, bool) or priority <= 0:
            raise AgentSourceSupportManifestError(
                f"{name}.native.format_resolution {source_kind}.priority must be positive"
            )
        if priority in priorities:
            raise AgentSourceSupportManifestError(
                f"{name}.native.format_resolution priority is duplicated: {priority}"
            )
        if variant.get("identity") != "native_session_id":
            raise AgentSourceSupportManifestError(
                f"{name}.native.format_resolution {source_kind}.identity is invalid"
            )
        if variant.get("content_equivalence") != "turn_fingerprint":
            raise AgentSourceSupportManifestError(
                f"{name}.native.format_resolution {source_kind}.content_equivalence is invalid"
            )
        exclude_suffix = variant.get("exclude_suffix")
        if exclude_suffix is not None and not isinstance(exclude_suffix, str):
            raise AgentSourceSupportManifestError(
                f"{name}.native.format_resolution {source_kind}.exclude_suffix must be string"
            )
        source_kinds.add(source_kind)
        priorities.add(priority)
        variants_by_kind[source_kind] = variant
    if name == "openclaw":
        if source_kinds != {"trajectory", "normal_jsonl", "corpus"}:
            raise AgentSourceSupportManifestError(
                "openclaw.native.format_resolution must declare trajectory, normal_jsonl, and corpus"
            )
        expected_paths = {
            "trajectory": ("agents/*/sessions/*.trajectory.jsonl", None),
            "normal_jsonl": ("agents/*/sessions/*.jsonl", ".trajectory.jsonl"),
            "corpus": ("workspace/memory/.dreams/session-corpus/*.txt", None),
        }
        if any(
            (
                variants_by_kind[kind].get("path_glob"),
                variants_by_kind[kind].get("exclude_suffix"),
            )
            != expected
            for kind, expected in expected_paths.items()
        ):
            raise AgentSourceSupportManifestError("openclaw format path contract is invalid")
        priority_by_kind = {
            kind: int(variants_by_kind[kind]["priority"])
            for kind in ("trajectory", "normal_jsonl", "corpus")
        }
        if not (
            priority_by_kind["trajectory"]
            > priority_by_kind["normal_jsonl"]
            > priority_by_kind["corpus"]
        ):
            raise AgentSourceSupportManifestError("openclaw format priority order is invalid")
        if resolution.get("extension_rule") != "longest_turn_fingerprint_prefix":
            raise AgentSourceSupportManifestError(
                "openclaw.native.format_resolution.extension_rule is invalid"
            )
        if resolution.get("conflict_rule") != "opaque_artifact_identity":
            raise AgentSourceSupportManifestError(
                "openclaw.native.format_resolution.conflict_rule is invalid"
            )


def _validate_artifact_resolution(name: str, native: Mapping[str, Any]) -> None:
    """Validate manifest-owned native artifact boundaries for Kimi.

    Kimi context archives, subagent contexts, and wire streams cannot share a
    mutable "session directory" shortcut.  The manifest therefore owns their
    source kinds, grouping rule, lineage rule, and safe duplicate semantics.
    """
    resolution_raw = native.get("artifact_resolution")
    if resolution_raw is None:
        if name == "kimi":
            raise AgentSourceSupportManifestError("kimi.native.artifact_resolution is required")
        return
    resolution = _mapping(resolution_raw, f"{name}.native.artifact_resolution")
    variants_raw = resolution.get("variants")
    if not isinstance(variants_raw, (list, tuple)) or not variants_raw:
        raise AgentSourceSupportManifestError(
            f"{name}.native.artifact_resolution.variants must be a non-empty list"
        )
    source_kinds: set[str] = set()
    observed: dict[str, tuple[str, str, str]] = {}
    for index, raw_variant in enumerate(variants_raw):
        variant = _mapping(
            raw_variant,
            f"{name}.native.artifact_resolution.variants[{index}]",
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
            raise AgentSourceSupportManifestError(
                f"{name}.native.artifact_resolution variant is incomplete"
            )
        if source_kind in source_kinds:
            raise AgentSourceSupportManifestError(
                f"{name}.native.artifact_resolution source_kind is duplicated: {source_kind}"
            )
        if variant.get("identity") != "native_artifact_id":
            raise AgentSourceSupportManifestError(
                f"{name}.native.artifact_resolution {source_kind}.identity is invalid"
            )
        source_kinds.add(source_kind)
        observed[source_kind] = (selector, aggregation, parent_relation)
    if name == "kimi":
        expected = {
            "main_context": (
                "outside_subagents",
                "context_segments_per_directory",
                "none",
            ),
            "subagent_context": (
                "under_subagents",
                "context_segments_per_directory",
                "native_subagent_path",
            ),
            "main_wire": ("main_wire", "single_wire", "native_main_wire_path"),
            "subagent_wire": (
                "under_subagents",
                "single_wire",
                "native_subagent_wire_path",
            ),
        }
        if source_kinds != set(expected) or observed != expected:
            raise AgentSourceSupportManifestError(
                "kimi.native.artifact_resolution variants are invalid"
            )
        if (
            resolution.get("archive_order_rule")
            != "context_numeric_ascending_then_lexical_unknown_then_active"
        ):
            raise AgentSourceSupportManifestError(
                "kimi.native.artifact_resolution.archive_order_rule is invalid"
            )
        if (
            resolution.get("duplicate_event_rule")
            != "dedupe_explicit_native_event_id_and_canonical_json_value_only"
        ):
            raise AgentSourceSupportManifestError(
                "kimi.native.artifact_resolution.duplicate_event_rule is invalid"
            )
        if resolution.get("conflict_rule") != "opaque_artifact_identity":
            raise AgentSourceSupportManifestError(
                "kimi.native.artifact_resolution.conflict_rule is invalid"
            )
        expected_rules = {
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
                raise AgentSourceSupportManifestError(
                    f"kimi.native.artifact_resolution.{field} is invalid"
                )


def _validate_recursive_discovery_contract(
    name: str,
    native: Mapping[str, Any],
    resolver: Mapping[str, Any],
) -> None:
    """Keep recursive native denominators owned by the support manifest."""
    if name != "claude":
        return
    formats = _string_list(native.get("formats"), "claude.native.formats")
    if formats != ("projects/**/*.jsonl",):
        raise AgentSourceSupportManifestError(
            "claude.native.formats must declare recursive projects JSONL"
        )
    if resolver.get("transcript_subdir") != "projects":
        raise AgentSourceSupportManifestError(
            "claude.native.root_resolver.transcript_subdir must be projects"
        )


def _validate_multi_root_contract(
    name: str,
    native: Mapping[str, Any],
    resolver: Mapping[str, Any],
) -> None:
    """Keep all-valid multi-root denominators source-owned and fail-closed."""
    if name != "crush":
        return
    if _string_list(native.get("formats"), "crush.native.formats") != ("crush.db",):
        raise AgentSourceSupportManifestError("crush.native.formats must declare crush.db")
    multi_root = _mapping(
        resolver.get("multi_root"),
        "crush.native.root_resolver.multi_root",
    )
    if (
        multi_root.get("mode") != "all_valid"
        or multi_root.get("project_ancestor_search") is not True
    ):
        raise AgentSourceSupportManifestError(
            "crush multi-root contract must enumerate all valid roots"
        )


def _normalize(name: str) -> str:
    return str(name or "").strip().lower().replace(" ", "-")


@dataclass(frozen=True)
class AgentSourceSupportSpec:
    """One immutable source declaration from the canonical manifest."""

    name: str
    role: str
    aliases: tuple[str, ...]
    payload: Mapping[str, Any]

    @property
    def parser_module(self) -> str:
        return str(self.parser["module"])

    @property
    def parser_class(self) -> str:
        return str(self.parser["class"])

    @property
    def parser(self) -> Mapping[str, Any]:
        return _mapping(self.payload["parser"], f"{self.name}.parser")

    @property
    def native(self) -> Mapping[str, Any]:
        return _mapping(self.payload["native"], f"{self.name}.native")

    @property
    def root_resolver(self) -> Mapping[str, Any]:
        return _mapping(self.native["root_resolver"], f"{self.name}.native.root_resolver")

    @property
    def capability(self) -> Mapping[str, Any]:
        return _mapping(self.payload["capability"], f"{self.name}.capability")

    @property
    def authorization(self) -> Mapping[str, Any]:
        return _mapping(self.payload["authorization"], f"{self.name}.authorization")

    @property
    def continuous(self) -> Mapping[str, Any]:
        """Return the manifest-owned continuous-capture contract."""
        return _mapping(self.payload["continuous"], f"{self.name}.continuous")

    @property
    def raw_contract(self) -> Mapping[str, Any]:
        return _mapping(self.payload["raw_contract"], f"{self.name}.raw_contract")

    @property
    def retention(self) -> Mapping[str, Any]:
        """Return the source's immutable raw-retention declaration."""
        return _mapping(self.payload["retention"], f"{self.name}.retention")

    @property
    def active_setup(self) -> Mapping[str, Any]:
        return _mapping(self.payload["active_setup"], f"{self.name}.active_setup")

    @property
    def active_entrypoint(self) -> str:
        return str(self.payload["active_entrypoint"])

    @property
    def required_cognitive_capabilities(self) -> tuple[str, ...]:
        return _string_list(
            self.capability["required_cognitive_capabilities"],
            f"{self.name}.capability.required_cognitive_capabilities",
        )

    @property
    def capability_contract_hash(self) -> str:
        return _hash(self.capability)

    @property
    def raw_contract_hash(self) -> str:
        """Hash the Raw, retention, and authorization contract as one unit."""
        return _hash(
            {
                "raw_contract": self.raw_contract,
                "retention": self.retention,
                "authorization": self.authorization,
            }
        )

    @property
    def source_fidelity(self) -> str:
        return str(self.capability["source_fidelity"])

    @property
    def is_active(self) -> bool:
        return self.role != "retired"

    @property
    def is_host_agent(self) -> bool:
        return self.role == "host_agent"

    @property
    def is_ingestion_only(self) -> bool:
        return self.role == "ingestion_only"


@dataclass(frozen=True)
class AgentSourceSupportManifest:
    """Typed read-only view of the one tracked source support definition."""

    source_path: Path
    manifest_hash: str
    host_core_cognitive_capabilities: tuple[str, ...]
    _specs: Mapping[str, AgentSourceSupportSpec]
    _aliases: Mapping[str, str]

    @property
    def source_names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    @property
    def active_source_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._specs.values() if spec.is_active)

    @property
    def host_agent_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._specs.values() if spec.is_host_agent)

    @property
    def ingestion_only_source_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._specs.values() if spec.is_ingestion_only)

    @property
    def retired_source_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._specs.values() if spec.role == "retired")

    @property
    def aliases(self) -> Mapping[str, str]:
        return self._aliases

    @property
    def active_entrypoints(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                spec.name: spec.active_entrypoint
                for spec in self._specs.values()
                if spec.is_host_agent
            }
        )

    @property
    def agent_cognitive_capabilities(self) -> Mapping[str, tuple[str, ...]]:
        core = set(self.host_core_cognitive_capabilities)
        return MappingProxyType(
            {
                spec.name: tuple(
                    capability
                    for capability in spec.required_cognitive_capabilities
                    if capability not in core
                )
                for spec in self._specs.values()
                if spec.is_host_agent
            }
        )

    @property
    def mcp_only_host_names(self) -> tuple[str, ...]:
        return tuple(
            spec.name
            for spec in self._specs.values()
            if spec.is_host_agent and spec.active_entrypoint == "mcp_only"
        )

    def normalize_name(self, value: str) -> str:
        """Normalize one source name or alias to its canonical manifest name."""
        normalized = _normalize(value)
        return self._aliases.get(normalized, normalized)

    def source(self, name: str) -> AgentSourceSupportSpec:
        """Return a declared source or fail closed for an unknown identifier."""
        normalized = self.normalize_name(name)
        try:
            return self._specs[normalized]
        except KeyError as exc:
            raise AgentSourceSupportManifestError(
                f"source {name!r} is not declared by the support manifest"
            ) from exc

    def require_active_source(self, name: str) -> AgentSourceSupportSpec:
        """Return an active source and reject retired declarations."""
        spec = self.source(name)
        if not spec.is_active:
            raise AgentSourceSupportManifestError(f"source {spec.name} is retired")
        return spec

    def require_host_agent(self, name: str) -> AgentSourceSupportSpec:
        """Return an active host-agent declaration, never ingestion-only."""
        spec = self.require_active_source(name)
        if not spec.is_host_agent:
            raise AgentSourceSupportManifestError(f"source {spec.name} is not a host agent")
        return spec

    def builtin_registry_specs(self) -> tuple[tuple[str, str, str], ...]:
        """Build active registry parser specs from the canonical manifest."""
        return tuple(
            (spec.name, spec.parser_module, spec.parser_class)
            for spec in self._specs.values()
            if spec.is_active
        )

    def support_metadata(self, name: str) -> dict[str, str]:
        """Build the immutable metadata every native Raw receipt must carry."""
        spec = self.require_active_source(name)
        return {
            "support_manifest_hash": self.manifest_hash,
            "support_role": spec.role,
            "support_parser": f"{spec.parser_module}.{spec.parser_class}",
            "support_capability_contract_hash": spec.capability_contract_hash,
            "support_raw_contract_hash": spec.raw_contract_hash,
            "support_native_to_raw": str(spec.raw_contract["native_to_raw"]),
            "support_acl_policy": str(spec.raw_contract["acl"]),
            "support_retention_policy": str(spec.retention["policy"]),
            "support_authorization_scope": str(spec.authorization["scope"]),
        }


def _validate_source(payload: Mapping[str, Any], index: int) -> AgentSourceSupportSpec:
    missing = sorted(_REQUIRED_SOURCE_FIELDS - set(payload))
    if missing:
        raise AgentSourceSupportManifestError(
            f"sources[{index}] missing required fields: {missing}"
        )
    name = _normalize(str(payload.get("name") or ""))
    if not name:
        raise AgentSourceSupportManifestError(f"sources[{index}].name is required")
    role = str(payload.get("role") or "")
    if role not in VALID_SOURCE_ROLES:
        raise AgentSourceSupportManifestError(f"{name}.role is invalid: {role!r}")
    aliases = tuple(
        _normalize(alias) for alias in _string_list(payload.get("aliases"), f"{name}.aliases")
    )
    parser = _mapping(payload.get("parser"), f"{name}.parser")
    for key in ("module", "class", "owner"):
        if not str(parser.get(key) or ""):
            raise AgentSourceSupportManifestError(f"{name}.parser.{key} is required")
    native = _mapping(payload.get("native"), f"{name}.native")
    resolver = _mapping(native.get("root_resolver"), f"{name}.native.root_resolver")
    _string_list(native.get("formats"), f"{name}.native.formats")
    _validate_format_resolution(name, native)
    _validate_artifact_resolution(name, native)
    _validate_recursive_discovery_contract(name, native, resolver)
    _validate_multi_root_contract(name, native, resolver)
    for key in (
        "configuration_keys",
        "environment",
        "standard_paths",
        "process_args",
        "heuristic",
        "transcript_subdir",
    ):
        if key not in resolver:
            raise AgentSourceSupportManifestError(f"{name}.native.root_resolver.{key} is required")
    if "multi_root" in resolver:
        multi_root = _mapping(
            resolver.get("multi_root"),
            f"{name}.native.root_resolver.multi_root",
        )
        if multi_root.get("mode") not in {"first_valid", "all_valid"}:
            raise AgentSourceSupportManifestError(
                f"{name}.native.root_resolver.multi_root.mode is invalid"
            )
        ancestor_search = multi_root.get("project_ancestor_search", False)
        if not isinstance(ancestor_search, bool):
            raise AgentSourceSupportManifestError(
                f"{name}.native.root_resolver.multi_root.project_ancestor_search must be bool"
            )
    capability = _mapping(payload.get("capability"), f"{name}.capability")
    _string_list(
        capability.get("required_cognitive_capabilities"),
        f"{name}.capability.required_cognitive_capabilities",
    )
    if not str(capability.get("source_fidelity") or ""):
        raise AgentSourceSupportManifestError(f"{name}.capability.source_fidelity is required")
    for key in (
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
        _mapping(payload.get(key), f"{name}.{key}")
    continuous = _mapping(payload.get("continuous"), f"{name}.continuous")
    if role != "retired":
        if continuous.get("enabled") is not True:
            raise AgentSourceSupportManifestError(
                f"{name}: active source continuous capture must be enabled"
            )
        for key in ("owner", "service", "activation_key", "trigger"):
            if not str(continuous.get(key) or ""):
                raise AgentSourceSupportManifestError(f"{name}.continuous.{key} is required")
        for key in ("poll_interval_seconds", "max_latency_seconds"):
            value = continuous.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise AgentSourceSupportManifestError(
                    f"{name}.continuous.{key} must be a positive integer"
                )
        if continuous.get("owner") != CONTINUOUS_CAPTURE_OWNER:
            raise AgentSourceSupportManifestError(
                f"{name}.continuous.owner must be {CONTINUOUS_CAPTURE_OWNER}"
            )
        if continuous.get("service") != CONTINUOUS_CAPTURE_SERVICE:
            raise AgentSourceSupportManifestError(
                f"{name}.continuous.service must be {CONTINUOUS_CAPTURE_SERVICE}"
            )
        if continuous.get("activation_key") != CONTINUOUS_CAPTURE_ACTIVATION_KEY:
            raise AgentSourceSupportManifestError(
                f"{name}.continuous.activation_key must be " f"{CONTINUOUS_CAPTURE_ACTIVATION_KEY}"
            )
        if int(continuous["max_latency_seconds"]) < int(continuous["poll_interval_seconds"]):
            raise AgentSourceSupportManifestError(
                f"{name}.continuous.max_latency_seconds must cover at least one poll"
            )
    authorization = _mapping(payload.get("authorization"), f"{name}.authorization")
    if not str(authorization.get("content_access") or ""):
        raise AgentSourceSupportManifestError(f"{name}.authorization.content_access is required")
    if not str(authorization.get("scope") or ""):
        raise AgentSourceSupportManifestError(f"{name}.authorization.scope is required")
    raw_contract = _mapping(payload.get("raw_contract"), f"{name}.raw_contract")
    for key in ("native_to_raw", "acl", "fidelity"):
        if not str(raw_contract.get(key) or ""):
            raise AgentSourceSupportManifestError(f"{name}.raw_contract.{key} is required")
    if raw_contract.get("fidelity") != capability.get("source_fidelity"):
        raise AgentSourceSupportManifestError(
            f"{name}: raw contract fidelity must match capability"
        )
    if raw_contract.get("native_to_raw") != "lossless_visible_v1":
        raise AgentSourceSupportManifestError(f"{name}: raw contract must use lossless_visible_v1")
    if raw_contract.get("acl") != "inherit_source_scope":
        raise AgentSourceSupportManifestError(f"{name}: raw contract must inherit the source scope")
    retention = _mapping(payload.get("retention"), f"{name}.retention")
    if not str(retention.get("policy") or ""):
        raise AgentSourceSupportManifestError(f"{name}.retention.policy is required")
    runtime_probe = _mapping(payload.get("runtime_probe"), f"{name}.runtime_probe")
    active_entrypoint = str(payload.get("active_entrypoint") or "")
    if role == "host_agent":
        if active_entrypoint not in {"adapter", "mcp_only"}:
            raise AgentSourceSupportManifestError(f"{name}: host agent needs an active entrypoint")
        if runtime_probe.get("required") is not True:
            raise AgentSourceSupportManifestError(f"{name}: host runtime probe must be required")
    elif active_entrypoint != "none":
        raise AgentSourceSupportManifestError(
            f"{name}: non-host source cannot expose an active entrypoint"
        )
    return AgentSourceSupportSpec(
        name=name,
        role=role,
        aliases=aliases,
        payload=_freeze(payload),
    )


def load_agent_source_support_manifest(path: Path | None = None) -> AgentSourceSupportManifest:
    """Load and validate the tracked manifest without importing parser modules."""
    try:
        if path is None:
            resource = importlib.resources.files("core.agent_kit").joinpath(
                MANIFEST_FILE_NAME
            )
            with importlib.resources.as_file(resource) as resource_path:
                source_path = Path(resource_path)
                raw_text = read_native_bytes(source_path).decode("utf-8")
        else:
            source_path = Path(path)
            raw_text = read_native_bytes(source_path).decode("utf-8")
    except (OSError, UnicodeError):
        raise AgentSourceSupportManifestError(
            "support manifest is unavailable"
        ) from None
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise AgentSourceSupportManifestError("support manifest is not valid JSON") from exc
    root = _mapping(payload, "support manifest")
    if root.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise AgentSourceSupportManifestError("support manifest schema_version is unsupported")
    if root.get("source_definition_owner") != MANIFEST_OWNER_PATH:
        raise AgentSourceSupportManifestError("support manifest owner path is invalid")
    core_capabilities = _string_list(
        root.get("host_core_cognitive_capabilities"),
        "host_core_cognitive_capabilities",
    )
    sources = root.get("sources")
    if not isinstance(sources, list):
        raise AgentSourceSupportManifestError("support manifest sources must be a list")
    specs: dict[str, AgentSourceSupportSpec] = {}
    aliases: dict[str, str] = {}
    for index, raw_spec in enumerate(sources):
        spec = _validate_source(_mapping(raw_spec, f"sources[{index}]"), index)
        if spec.name in specs:
            raise AgentSourceSupportManifestError(f"duplicate support source {spec.name}")
        specs[spec.name] = spec
        for alias in spec.aliases:
            if alias in specs or alias in aliases:
                raise AgentSourceSupportManifestError(f"duplicate source alias {alias}")
            aliases[alias] = spec.name
    if not specs:
        raise AgentSourceSupportManifestError("support manifest must declare at least one source")
    return AgentSourceSupportManifest(
        source_path=source_path,
        manifest_hash=_hash(root),
        host_core_cognitive_capabilities=core_capabilities,
        _specs=MappingProxyType(specs),
        _aliases=MappingProxyType(aliases),
    )


def get_agent_source_support_manifest() -> AgentSourceSupportManifest:
    """Reload the canonical manifest so changed contracts invalidate receipts."""
    return load_agent_source_support_manifest()


def expand_path_template(
    value: str,
    *,
    home: Path | None = None,
    cwd: Path | None = None,
) -> Path:
    """Expand the manifest's limited ``{home}``/``{cwd}`` path templates."""
    rendered = (
        str(value)
        .replace("{home}", str(home or Path.home()))
        .replace("{cwd}", str(cwd or Path.cwd()))
    )
    return Path(rendered).expanduser()


def expand_path_templates(
    values: Iterable[str],
    *,
    home: Path | None = None,
    cwd: Path | None = None,
) -> tuple[Path, ...]:
    return tuple(expand_path_template(value, home=home, cwd=cwd) for value in values)


def bind_source_support_metadata(
    metadata: Mapping[str, Any] | None,
    source_name: str,
    *,
    manifest: AgentSourceSupportManifest | None = None,
    require_declared: bool = False,
) -> dict[str, Any]:
    """Bind one declared source's raw receipt to the current manifest hash.

    Generic capture callers can retain caller-owned metadata for an unknown
    non-native source. Native parser callers pass ``require_declared=True``;
    unknown, retired, stale, or forged support identity is then rejected before
    Raw insertion.
    """
    result = dict(metadata or {})
    support_manifest = manifest or get_agent_source_support_manifest()
    native_capture = result.get("support_native_capture")
    if native_capture not in (None, True, False):
        raise AgentSourceSupportManifestError("support_native_capture must be boolean")
    try:
        spec = support_manifest.source(source_name)
    except AgentSourceSupportManifestError:
        if require_declared or native_capture is True:
            raise
        return result
    if not spec.is_active:
        raise AgentSourceSupportManifestError(
            f"retired source {spec.name} cannot write a raw receipt"
        )
    expected = support_manifest.support_metadata(spec.name)
    for key, value in expected.items():
        supplied = result.get(key)
        if supplied not in (None, "", value):
            raise AgentSourceSupportManifestError(
                f"{spec.name}: stale or forged {key} on raw receipt"
            )
        result[key] = value
    if require_declared:
        if native_capture is False:
            raise AgentSourceSupportManifestError(
                f"{spec.name}: native capture cannot disable the support contract"
            )
        result["support_native_capture"] = True
    if require_declared or native_capture is True:
        observed_fidelity = str(result.get("source_fidelity") or "unknown")
        if observed_fidelity not in VALID_SOURCE_FIDELITIES:
            raise AgentSourceSupportManifestError(
                f"{spec.name}: observed source fidelity is invalid: {observed_fidelity!r}"
            )
        result["source_fidelity"] = observed_fidelity
        fidelity_state = (
            "conformant" if observed_fidelity == spec.source_fidelity else "observed_mismatch"
        )
        for key, value in {
            "support_fidelity_observed": observed_fidelity,
            "support_fidelity_contract_state": fidelity_state,
        }.items():
            supplied = result.get(key)
            if supplied not in (None, "", value):
                raise AgentSourceSupportManifestError(
                    f"{spec.name}: stale or forged {key} on raw receipt"
                )
            result[key] = value
    return result


def validate_native_raw_contract(
    metadata: Mapping[str, Any] | None,
    completeness: Mapping[str, Any] | None,
    source_name: str,
    *,
    manifest: AgentSourceSupportManifest | None = None,
) -> tuple[str, ...]:
    """Return contract-error codes for one manifest-bound native Raw receipt."""
    result = dict(metadata or {})
    support_manifest = manifest or get_agent_source_support_manifest()
    try:
        spec = support_manifest.require_active_source(source_name)
    except AgentSourceSupportManifestError:
        return ("native_source_not_declared",)

    errors: list[str] = []
    if result.get("support_native_capture") is not True:
        errors.append("native_capture_marker_missing")
    expected = support_manifest.support_metadata(spec.name)
    for key, value in expected.items():
        if result.get(key) != value:
            errors.append(f"{key}_mismatch")

    raw_contract = spec.raw_contract
    receipt_completeness = dict(completeness or {})
    if raw_contract["native_to_raw"] == "lossless_visible_v1":
        if receipt_completeness.get("visible_text") != "full":
            errors.append("visible_text_not_lossless")
        if receipt_completeness.get("truncated") is not False:
            errors.append("raw_content_truncated")
        if receipt_completeness.get("loss_reasons") not in (None, [], ()):
            errors.append("raw_content_loss_reason_present")
    if raw_contract["acl"] == "inherit_source_scope":
        if not str(result.get("canonical_session_id") or ""):
            errors.append("acl_canonical_session_missing")
        if not str(result.get("source_session_id") or ""):
            errors.append("acl_source_session_missing")
    observed_fidelity = str(result.get("source_fidelity") or "")
    if observed_fidelity not in VALID_SOURCE_FIDELITIES:
        errors.append("source_fidelity_invalid")
    else:
        expected_state = (
            "conformant" if observed_fidelity == raw_contract["fidelity"] else "observed_mismatch"
        )
        if observed_fidelity != raw_contract["fidelity"]:
            errors.append("source_fidelity_contract_mismatch")
        if result.get("support_fidelity_observed") != observed_fidelity:
            errors.append("source_fidelity_observation_mismatch")
        if result.get("support_fidelity_contract_state") != expected_state:
            errors.append("source_fidelity_contract_state_mismatch")
    return tuple(dict.fromkeys(errors))


def build_source_support_runtime_report(
    payload: Mapping[str, Any],
    *,
    producer: str,
    manifest: AgentSourceSupportManifest | None = None,
) -> dict[str, Any]:
    """Build a structural source-observation report, never a runtime attestation.

    Daemon/backfill JSON is useful to validate that a snapshot has the current
    manifest shape, but it is not authenticated evidence of a host's runtime
    capability.  In particular, it may not embed runtime receipts: only
    :class:`AgentRuntimeReceiptStore` owns those durable, authenticated probe
    results.  ``report_hash`` is a corruption checksum, not a signature or
    provenance claim.
    """
    if producer not in RUNTIME_REPORT_PRODUCERS:
        raise AgentSourceSupportManifestError(f"unsupported runtime report producer: {producer}")
    support_manifest = manifest or get_agent_source_support_manifest()
    thawed_payload = _thaw(dict(payload))
    if not isinstance(thawed_payload, Mapping):
        raise AgentSourceSupportManifestError("runtime report payload must remain an object")
    result: dict[str, Any] = {str(key): value for key, value in thawed_payload.items()}
    forbidden = {
        "runtime_receipts",
        "runtime_receipt_scope",
        "evidence_hash",
        "runtime_attestation",
        "certifying",
        "release_eligible",
        "runtime_full_power_ok",
        "full_power_agents",
    }
    present_forbidden = sorted(key for key in forbidden if key in result)
    if present_forbidden:
        raise AgentSourceSupportManifestError(
            "structural runtime reports cannot carry runtime receipts or attestations: "
            + ", ".join(present_forbidden)
        )
    result.pop("report_hash", None)
    result["schema_version"] = RUNTIME_REPORT_SCHEMA_VERSION
    result["report_kind"] = "structural_source_observation"
    result["producer"] = producer
    result["support_manifest_hash"] = support_manifest.manifest_hash
    result["report_hash"] = _hash(result)
    return result


@dataclass(frozen=True)
class NativeSourceSnapshot:
    """Runtime observation bound to immutable support-manifest identity.

    It is evidence only: fields such as roots and denominator are runtime
    observations, while source role, parser, capability hash, and manifest hash
    must exactly match the tracked manifest.  The snapshot therefore cannot be
    promoted into an alternate source-definition owner.
    """

    schema_version: str
    source_name: str
    source_role: str
    support_manifest_hash: str
    parser_module: str
    parser_class: str
    capability_contract_hash: str
    resolved_roots: tuple[str, ...]
    cursor: Mapping[str, Any]
    native_denominator: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_name": self.source_name,
            "source_role": self.source_role,
            "support_manifest_hash": self.support_manifest_hash,
            "parser_module": self.parser_module,
            "parser_class": self.parser_class,
            "capability_contract_hash": self.capability_contract_hash,
            "resolved_roots": list(self.resolved_roots),
            "cursor": dict(self.cursor),
            "native_denominator": dict(self.native_denominator),
        }

    @property
    def snapshot_hash(self) -> str:
        """Return the canonical, content-free identity of this observation."""
        return _hash(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NativeSourceSnapshot":
        required = {
            "schema_version",
            "source_name",
            "source_role",
            "support_manifest_hash",
            "parser_module",
            "parser_class",
            "capability_contract_hash",
            "resolved_roots",
            "cursor",
            "native_denominator",
        }
        missing = sorted(required - set(payload))
        if missing:
            raise AgentSourceSupportManifestError(f"native source snapshot missing {missing}")
        roots = payload.get("resolved_roots")
        if not isinstance(roots, list) or not all(isinstance(value, str) for value in roots):
            raise AgentSourceSupportManifestError("native source snapshot roots are malformed")
        cursor = _mapping(payload.get("cursor"), "native source snapshot cursor")
        denominator = _mapping(
            payload.get("native_denominator"),
            "native source snapshot denominator",
        )
        normalized_denominator: dict[str, int] = {}
        for key in ("sessions", "turns"):
            value = denominator.get(key)
            if not isinstance(value, int) or value < 0:
                raise AgentSourceSupportManifestError(
                    f"native source snapshot denominator {key} must be a non-negative integer"
                )
            normalized_denominator[key] = value
        return cls(
            schema_version=str(payload["schema_version"]),
            source_name=_normalize(str(payload["source_name"])),
            source_role=str(payload["source_role"]),
            support_manifest_hash=str(payload["support_manifest_hash"]),
            parser_module=str(payload["parser_module"]),
            parser_class=str(payload["parser_class"]),
            capability_contract_hash=str(payload["capability_contract_hash"]),
            resolved_roots=tuple(roots),
            cursor=MappingProxyType(dict(cursor)),
            native_denominator=MappingProxyType(normalized_denominator),
        )


def build_native_source_snapshot(
    source_name: str,
    *,
    resolved_roots: Iterable[Path | str],
    cursor: Mapping[str, Any],
    native_denominator: Mapping[str, int],
    manifest: AgentSourceSupportManifest | None = None,
) -> NativeSourceSnapshot:
    """Build a manifest-bound runtime observation for one active source."""
    support_manifest = manifest or get_agent_source_support_manifest()
    spec = support_manifest.require_active_source(source_name)
    snapshot = NativeSourceSnapshot.from_dict(
        {
            "schema_version": NATIVE_SOURCE_SNAPSHOT_SCHEMA_VERSION,
            "source_name": spec.name,
            "source_role": spec.role,
            "support_manifest_hash": support_manifest.manifest_hash,
            "parser_module": spec.parser_module,
            "parser_class": spec.parser_class,
            "capability_contract_hash": spec.capability_contract_hash,
            "resolved_roots": [str(Path(root)) for root in resolved_roots],
            "cursor": dict(cursor),
            "native_denominator": dict(native_denominator),
        }
    )
    errors = validate_native_source_snapshot(snapshot, manifest=support_manifest)
    if errors:
        raise AgentSourceSupportManifestError(
            f"refusing invalid native source snapshot: {', '.join(errors)}"
        )
    return snapshot


def native_source_snapshot_hash(snapshot: NativeSourceSnapshot | Mapping[str, Any]) -> str:
    """Hash one validated NativeSourceSnapshot without exposing its roots.

    The digest binds a later capture-attestation receipt to the exact manifest,
    parser contract, cursor generation, and native denominator that the daemon
    observed.  It is an integrity anchor, not a new source-definition owner.
    """
    normalized = (
        snapshot
        if isinstance(snapshot, NativeSourceSnapshot)
        else NativeSourceSnapshot.from_dict(snapshot)
    )
    errors = validate_native_source_snapshot(normalized)
    if errors:
        raise AgentSourceSupportManifestError(
            "refusing invalid native source snapshot hash: " + ", ".join(errors)
        )
    return normalized.snapshot_hash


def validate_native_source_snapshot(
    snapshot: NativeSourceSnapshot,
    *,
    manifest: AgentSourceSupportManifest | None = None,
) -> list[str]:
    """Return fail-closed validation codes for a persisted/runtime snapshot."""
    support_manifest = manifest or get_agent_source_support_manifest()
    errors: list[str] = []
    if snapshot.schema_version != NATIVE_SOURCE_SNAPSHOT_SCHEMA_VERSION:
        errors.append("snapshot_schema_version_mismatch")
    try:
        spec = support_manifest.source(snapshot.source_name)
    except AgentSourceSupportManifestError:
        return errors + ["snapshot_source_not_in_manifest"]
    if not spec.is_active:
        errors.append("retired_source_snapshot")
    if snapshot.support_manifest_hash != support_manifest.manifest_hash:
        errors.append("support_manifest_hash_mismatch")
    if snapshot.source_role != spec.role:
        errors.append("snapshot_role_mismatch")
    if snapshot.parser_module != spec.parser_module or snapshot.parser_class != spec.parser_class:
        errors.append("snapshot_parser_mismatch")
    if snapshot.capability_contract_hash != spec.capability_contract_hash:
        errors.append("snapshot_capability_mismatch")
    if not isinstance(snapshot.cursor, Mapping) or not str(snapshot.cursor.get("kind") or ""):
        errors.append("snapshot_cursor_malformed")
    elif snapshot.cursor.get("kind") == CONTINUOUS_CAPTURE_CURSOR_KIND:
        cursor = snapshot.cursor
        required_capture_fields = (
            CONTINUOUS_CAPTURE_CURSOR_HASH_FIELDS
            | CONTINUOUS_CAPTURE_CURSOR_COUNT_FIELDS
            | {
                "capture_generation_id",
                "capture_generation_eligible",
                "denominator_complete",
                "denominator_observed_sessions",
                "discovered_sessions",
                "denominator_turns",
            }
        )
        if not required_capture_fields.issubset(cursor):
            errors.append("snapshot_capture_cursor_incomplete")
        else:
            capture_counts_valid = all(
                isinstance(cursor.get(key), int)
                and not isinstance(cursor.get(key), bool)
                and int(cursor[key]) >= 0
                for key in CONTINUOUS_CAPTURE_CURSOR_COUNT_FIELDS
            )
            denominator_counts_valid = all(
                isinstance(cursor.get(key), int)
                and not isinstance(cursor.get(key), bool)
                and int(cursor[key]) >= 0
                for key in (
                    "denominator_observed_sessions",
                    "discovered_sessions",
                    "denominator_turns",
                )
            )
            if (
                not str(cursor.get("capture_generation_id") or "")
                or not isinstance(
                    cursor.get("capture_generation_eligible"),
                    bool,
                )
                or not isinstance(cursor.get("denominator_complete"), bool)
                or not capture_counts_valid
                or not denominator_counts_valid
                or any(
                    not _valid_sha256(cursor.get(key))
                    for key in CONTINUOUS_CAPTURE_CURSOR_HASH_FIELDS
                )
            ):
                errors.append("snapshot_capture_cursor_malformed")
            elif (
                int(cursor["capture_exact_receipt_count"])
                + int(cursor["capture_pending_turn_count"])
                != int(cursor["capture_expected_turn_count"])
                or int(cursor["capture_exact_receipt_count"]) > int(cursor["capture_receipt_count"])
                or int(cursor["capture_orphan_receipt_count"])
                > int(cursor["capture_receipt_count"])
                or int(cursor["capture_expected_turn_count"]) != int(cursor["denominator_turns"])
                or int(snapshot.native_denominator.get("sessions", -1))
                != int(cursor["discovered_sessions"])
                or (
                    cursor["denominator_complete"] is True
                    and int(snapshot.native_denominator.get("turns", -1))
                    != int(cursor["denominator_turns"])
                )
                or (
                    cursor["denominator_complete"] is False
                    and int(snapshot.native_denominator.get("turns", -1)) != 0
                )
            ):
                errors.append("snapshot_capture_cursor_inconsistent")
    for key in ("sessions", "turns"):
        value = snapshot.native_denominator.get(key)
        if not isinstance(value, int) or value < 0:
            errors.append("snapshot_native_denominator_malformed")
            break
    return errors
