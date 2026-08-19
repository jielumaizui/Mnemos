# -*- coding: utf-8 -*-
"""System-wide contracts for Mnemos cognitive assets and operations.

This module is intentionally small and dependency-light.  It gives existing
subsystems a common vocabulary without forcing an immediate storage rewrite.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, cast

from core.system_contract_registry import build_contract_registries

COGNITIVE_ASSET_SCHEMA_VERSION = "mnemos.cognitive_asset.v1"
QUALITY_DECISION_SCHEMA_VERSION = "mnemos.quality_decision.v1"
CAPABILITY_REGISTRY_SCHEMA_VERSION = "mnemos.capability_registry.v1"
PRIVACY_POLICY_SCHEMA_VERSION = "mnemos.privacy_retention.v1"
LIFECYCLE_STATUS_SCHEMA_VERSION = "mnemos.lifecycle_status.v1"
ACTION_LEDGER_SCHEMA_VERSION = "mnemos.action_ledger.v1"
DOMAIN_GLOSSARY_SCHEMA_VERSION = "mnemos.domain_glossary.v1"
SCORECARD_SCHEMA_VERSION = "mnemos.scorecard.v1"
MODULE_TOGGLE_SCHEMA_VERSION = "mnemos.module_toggle.v1"
TOGGLE_OUTPUT_SCHEMA_VERSION = "mnemos.toggle_output.v1"
MIGRATION_SCHEMA_VERSION = "mnemos.migration_registry.v1"
SNAPSHOT_SCHEMA_VERSION = "mnemos.snapshot_manifest.v2"
DATA_OWNERSHIP_SCHEMA_VERSION = "mnemos.data_ownership.v1"
GOLDEN_BENCHMARK_SCHEMA_VERSION = "mnemos.golden_benchmark.v1"
INSTALL_LIFECYCLE_SCHEMA_VERSION = "mnemos.install_lifecycle.v1"
WIKI_QUALITY_SCHEMA_VERSION = "mnemos.wiki_quality.v1"
COGNITIVE_READINESS_SCHEMA_VERSION = "mnemos.cognitive_readiness.v2"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _non_empty_list(values: Sequence[str]) -> bool:
    return bool(values) and all(isinstance(value, str) and value.strip() for value in values)


@dataclass(frozen=True)
class CognitiveAssetDefinition:
    asset_type: str
    producers: tuple[str, ...]
    consumers: tuple[str, ...]
    source_fields: tuple[str, ...]
    evidence_fields: tuple[str, ...]
    privacy_level: str
    lifecycle_statuses: tuple[str, ...]
    revision_policy: str


@dataclass(frozen=True)
class CognitiveAsset:
    asset_id: str
    asset_type: str
    source_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    confidence: float
    privacy_level: str
    status: str
    consumers: tuple[str, ...]
    revision_policy: str
    last_used_at: str | None = None
    supersedes: tuple[str, ...] = ()
    contradicts: tuple[str, ...] = ()
    schema_version: str = COGNITIVE_ASSET_SCHEMA_VERSION

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.asset_type not in COGNITIVE_ASSET_DEFINITIONS:
            errors.append(f"unknown cognitive asset type: {self.asset_type}")
        if not self.asset_id:
            errors.append("asset_id is required")
        if not _non_empty_list(self.source_refs):
            errors.append("source_refs must be non-empty")
        if not _non_empty_list(self.evidence_refs):
            errors.append("evidence_refs must be non-empty")
        if not 0.0 <= float(self.confidence) <= 1.0:
            errors.append("confidence must be between 0 and 1")
        if self.privacy_level not in PRIVACY_POLICIES:
            errors.append(f"unknown privacy level/policy: {self.privacy_level}")
        if self.status not in LIFECYCLE_STATUSES:
            errors.append(f"unknown lifecycle status: {self.status}")
        if not _non_empty_list(self.consumers):
            errors.append("consumers must be non-empty")
        if not self.revision_policy:
            errors.append("revision_policy is required")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


QUALITY_DECISIONS = {"accept", "skip", "degrade", "needs_review", "auto_fix", "reject"}
QUALITY_RISK_LEVELS = {"low", "medium", "high", "critical"}


@dataclass(frozen=True)
class QualityDecision:
    decision_id: str
    subject: str
    decision: str
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    risk_level: str
    confidence: float
    auto_fixable: bool
    next_action: str
    schema_version: str = QUALITY_DECISION_SCHEMA_VERSION

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.decision_id:
            errors.append("decision_id is required")
        if not self.subject:
            errors.append("subject is required")
        if self.decision not in QUALITY_DECISIONS:
            errors.append(f"unknown quality decision: {self.decision}")
        if not _non_empty_list(self.reason_codes):
            errors.append("reason_codes must be non-empty")
        if not _non_empty_list(self.evidence_refs):
            errors.append("evidence_refs must be non-empty")
        if self.risk_level not in QUALITY_RISK_LEVELS:
            errors.append(f"unknown risk_level: {self.risk_level}")
        if not 0.0 <= float(self.confidence) <= 1.0:
            errors.append("confidence must be between 0 and 1")
        if not self.next_action:
            errors.append("next_action is required")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QualityGateDefinition:
    domain: str
    producer: str
    consumer: str
    evidence_refs: tuple[str, ...]
    accepted_decisions: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityDefinition:
    capability: str
    owner: str
    required: bool
    config_keys: tuple[str, ...]
    health_surface: str
    repair_action: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class PrivacyPolicy:
    data_type: str
    privacy_level: str
    storage_class: str
    encryption_required: bool
    searchable: bool
    wiki_allowed: bool
    model_consumable: bool
    retention_days: int | None
    redaction_policy: str


@dataclass(frozen=True)
class LifecycleMapping:
    local_surface: str
    local_statuses: Mapping[str, str]
    failure_classes: Mapping[str, str]


@dataclass(frozen=True)
class ActionRecord:
    action_id: str
    actor: str
    action_type: str
    target: str
    evidence_refs: tuple[str, ...]
    status: str
    before_ref: str = ""
    after_ref: str = ""
    quality_decision_id: str = ""
    verification: Mapping[str, Any] = field(default_factory=dict)
    rollback_ref: str = ""
    subject_provenance: Mapping[str, Any] | None = None
    created_at: str = field(default_factory=_now_iso)
    schema_version: str = ACTION_LEDGER_SCHEMA_VERSION

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.action_id:
            errors.append("action_id is required")
        if not self.actor:
            errors.append("actor is required")
        if self.action_type not in ACTION_TYPES:
            errors.append(f"unknown action_type: {self.action_type}")
        if not self.target:
            errors.append("target is required")
        if not _non_empty_list(self.evidence_refs):
            errors.append("evidence_refs must be non-empty")
        if self.status not in ACTION_STATUSES:
            errors.append(f"unknown action status: {self.status}")
        if not self.rollback_ref and self.action_type in ACTIONS_REQUIRING_ROLLBACK:
            errors.append(f"{self.action_type} requires rollback_ref")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DomainTerm:
    term: str
    definition: str
    owner: str
    deprecated_aliases: tuple[str, ...] = ()
    forbidden_aliases: tuple[str, ...] = ()
    migration_note: str = ""


@dataclass(frozen=True)
class ScorecardDimension:
    dimension: str
    max_score: int
    evidence_commands: tuple[str, ...]
    runtime_metrics: tuple[str, ...]
    problem_refs: tuple[str, ...]
    score_owner: str


_CONTRACT_REGISTRIES = build_contract_registries(
    cognitive_asset_definition=CognitiveAssetDefinition,
    quality_gate_definition=QualityGateDefinition,
    capability_definition=CapabilityDefinition,
    privacy_policy=PrivacyPolicy,
    lifecycle_mapping=LifecycleMapping,
    domain_term=DomainTerm,
    scorecard_dimension=ScorecardDimension,
    quality_decisions=QUALITY_DECISIONS,
)
LIFECYCLE_STATUSES = _CONTRACT_REGISTRIES["lifecycle_statuses"]
FAILURE_CLASSES = _CONTRACT_REGISTRIES["failure_classes"]
ACTION_TYPES = _CONTRACT_REGISTRIES["action_types"]
ACTION_STATUSES = _CONTRACT_REGISTRIES["action_statuses"]
ACTIONS_REQUIRING_ROLLBACK = _CONTRACT_REGISTRIES["actions_requiring_rollback"]
COGNITIVE_ASSET_DEFINITIONS = _CONTRACT_REGISTRIES["cognitive_asset_definitions"]
QUALITY_GATE_DEFINITIONS = _CONTRACT_REGISTRIES["quality_gate_definitions"]
CAPABILITY_DEFINITIONS = _CONTRACT_REGISTRIES["capability_definitions"]
PRIVACY_POLICIES = _CONTRACT_REGISTRIES["privacy_policies"]
LIFECYCLE_MAPPINGS = _CONTRACT_REGISTRIES["lifecycle_mappings"]
DOMAIN_TERMS = _CONTRACT_REGISTRIES["domain_terms"]
SCORECARD_DIMENSIONS = _CONTRACT_REGISTRIES["scorecard_dimensions"]


def make_quality_decision(
    *,
    subject: str,
    decision: str,
    reason_codes: Iterable[str],
    evidence_refs: Iterable[str],
    risk_level: str = "low",
    confidence: float = 1.0,
    auto_fixable: bool = False,
    next_action: str = "record",
    decision_id: str | None = None,
) -> QualityDecision:
    return QualityDecision(
        decision_id=decision_id or f"qd-{uuid.uuid4().hex[:16]}",
        subject=subject,
        decision=decision,
        reason_codes=tuple(reason_codes),
        evidence_refs=tuple(evidence_refs),
        risk_level=risk_level,
        confidence=float(confidence),
        auto_fixable=bool(auto_fixable),
        next_action=next_action,
    )


def quality_decision_from_gate(
    *,
    subject: str,
    disposition: str,
    reason: str,
    score: float,
    evidence_refs: Iterable[str] = ("core/hephaestus/quality_gate.py",),
) -> QualityDecision:
    mapping = {
        "accept": "accept",
        "review": "needs_review",
        "reject": "reject",
    }
    decision = mapping.get(disposition, "degrade")
    return make_quality_decision(
        subject=subject,
        decision=decision,
        reason_codes=(reason,),
        evidence_refs=tuple(evidence_refs),
        risk_level="medium" if decision == "needs_review" else "low",
        confidence=max(0.0, min(1.0, float(score))),
        auto_fixable=False,
        next_action="route_to_action_router" if decision == "accept" else "queue_review",
    )


class ActionLedger:
    """Compatibility constructor for the deep append-only ledger module."""

    @classmethod
    def from_config(cls, config: Any, *, initialize: bool = False) -> "ActionLedger":
        from core.ops.action_ledger import ActionLedger as Implementation

        return cast(
            ActionLedger,
            Implementation.from_config(config, initialize=initialize),
        )

    def __new__(cls, db_path: Path, *, initialize: bool = False) -> "ActionLedger":
        from core.ops.action_ledger import ActionLedger as Implementation

        return cast(ActionLedger, Implementation(db_path, initialize=initialize))

    def record(
        self,
        record: ActionRecord,
        *,
        material_action: Any | None = None,
    ) -> str:
        """Persist one immutable action record."""

        raise AssertionError("compatibility constructor returns the deep implementation")

    def record_observation(self, observation: Any) -> str:
        """Persist one canonical typed diagnostic observation."""

        raise AssertionError("compatibility constructor returns the deep implementation")

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent immutable action records."""

        raise AssertionError("compatibility constructor returns the deep implementation")


def make_action_record(
    *,
    actor: str,
    action_type: str,
    target: str,
    evidence_refs: Iterable[str],
    status: str = "verified",
    before_ref: str = "",
    after_ref: str = "",
    quality_decision_id: str = "",
    verification: Mapping[str, Any] | None = None,
    rollback_ref: str = "",
    subject_provenance: Mapping[str, Any] | None = None,
    action_id: str | None = None,
) -> ActionRecord:
    return ActionRecord(
        action_id=action_id or f"act-{uuid.uuid4().hex[:16]}",
        actor=actor,
        action_type=action_type,
        target=target,
        evidence_refs=tuple(evidence_refs),
        status=status,
        before_ref=before_ref,
        after_ref=after_ref,
        quality_decision_id=quality_decision_id,
        verification=dict(verification or {}),
        rollback_ref=rollback_ref,
        subject_provenance=dict(subject_provenance) if subject_provenance is not None else None,
    )


def make_quality_gate_observation(**kwargs: Any) -> Any:
    """Build a typed, non-effecting quality-gate observation."""

    from core.ops.action_ledger import make_quality_gate_observation as build

    return build(**kwargs)


def make_cognitive_readiness_observation(**kwargs: Any) -> Any:
    """Build a typed, non-effecting readiness-audit observation."""

    from core.ops.action_ledger import make_cognitive_readiness_observation as build

    return build(**kwargs)


def make_data_inventory_observation(**kwargs: Any) -> Any:
    """Build a typed, non-effecting data-inventory observation."""

    from core.ops.action_ledger import make_data_inventory_observation as build

    return build(**kwargs)


def make_benchmark_consumer_observation(**kwargs: Any) -> Any:
    """Build a typed, non-effecting benchmark-consumer observation."""

    from core.ops.action_ledger import make_benchmark_consumer_observation as build

    return build(**kwargs)


def make_golden_benchmark_observation(**kwargs: Any) -> Any:
    """Build a typed observation of one hermetic golden-benchmark stage."""

    from core.ops.action_ledger import make_golden_benchmark_observation as build

    return build(**kwargs)


def _validate_paths(refs: Iterable[str], *, root: Path) -> list[str]:
    errors: list[str] = []
    for ref in refs:
        path = str(ref).split(":", 1)[0]
        if not path or path.startswith(("health.", "security_audit.", "mcp.", "mnemos ", "python3 ", ".venv/")):
            continue
        if path.endswith(".py") or path.endswith(".md") or "/" in path:
            if not (root / path).exists():
                errors.append(f"missing referenced path: {path}")
    return errors


def audit_cognitive_asset_schema(*, strict: bool = False, root: Path | None = None) -> list[str]:
    root = root or Path(__file__).resolve().parents[1]
    errors: list[str] = []
    required = {
        "wiki_page",
        "persona_assertion",
        "reflection_insight",
        "policy_patch",
        "distill_claim",
        "cognitive_decision_asset",
        "module_output",
        "activation_decision",
        "activation_evidence",
        "consumer_effect",
        "migration_plan",
        "snapshot_manifest",
        "data_export_manifest",
        "deletion_proof",
        "benchmark_result",
        "install_lifecycle_state",
        "wiki_quality_report",
    }
    missing = required - set(COGNITIVE_ASSET_DEFINITIONS)
    if missing:
        errors.append(f"missing cognitive asset definitions: {sorted(missing)}")
    for asset_type, definition in COGNITIVE_ASSET_DEFINITIONS.items():
        if asset_type != definition.asset_type:
            errors.append(f"{asset_type}: key and asset_type disagree")
        if not _non_empty_list(definition.producers):
            errors.append(f"{asset_type}: producers required")
        if not _non_empty_list(definition.consumers):
            errors.append(f"{asset_type}: consumers required")
        if not _non_empty_list(definition.source_fields):
            errors.append(f"{asset_type}: source_fields required")
        if not _non_empty_list(definition.evidence_fields):
            errors.append(f"{asset_type}: evidence_fields required")
        if definition.privacy_level not in PRIVACY_POLICIES:
            errors.append(f"{asset_type}: unknown privacy policy {definition.privacy_level}")
        if not all(status in LIFECYCLE_STATUSES for status in definition.lifecycle_statuses):
            errors.append(f"{asset_type}: unknown lifecycle status")
        if strict:
            errors.extend(_validate_paths(definition.producers, root=root))
    return errors


def audit_quality_decision_contract(*, strict: bool = False, root: Path | None = None) -> list[str]:
    root = root or Path(__file__).resolve().parents[1]
    errors: list[str] = []
    required_domains = {
        "raw",
        "document",
        "distill",
        "wiki",
        "persona",
        "auto_heal",
        "activation_quality_decision",
        "migration_quality_decision",
        "restore_quality_decision",
        "data_ownership_quality_decision",
        "golden_benchmark_quality",
        "install_lifecycle_quality",
    }
    missing = required_domains - set(QUALITY_GATE_DEFINITIONS)
    if missing:
        errors.append(f"missing quality gate domains: {sorted(missing)}")
    for domain, definition in QUALITY_GATE_DEFINITIONS.items():
        if definition.domain != domain:
            errors.append(f"{domain}: key and domain disagree")
        if set(definition.accepted_decisions) != QUALITY_DECISIONS:
            errors.append(f"{domain}: must accept canonical decision enum")
        if not _non_empty_list(definition.evidence_refs):
            errors.append(f"{domain}: evidence_refs required")
        if strict:
            errors.extend(_validate_paths(definition.evidence_refs, root=root))
    sample = make_quality_decision(
        subject="contract-sample",
        decision="accept",
        reason_codes=("schema_valid",),
        evidence_refs=("core/system_contracts.py",),
    )
    errors.extend(sample.validate())
    return errors


def audit_capability_registry(*, strict: bool = False, root: Path | None = None) -> list[str]:
    root = root or Path(__file__).resolve().parents[1]
    errors: list[str] = []
    required = {
        "llm",
        "embedding",
        "reranker",
        "obsidian_vault",
        "raw_vault",
        "document_process",
        "module_toggle_registry",
        "migration_registry",
        "snapshot_manager",
        "data_ownership",
        "golden_benchmark",
        "install_lifecycle",
    }
    missing = required - set(CAPABILITY_DEFINITIONS)
    if missing:
        errors.append(f"missing capabilities: {sorted(missing)}")
    for name, capability in CAPABILITY_DEFINITIONS.items():
        if name != capability.capability:
            errors.append(f"{name}: key and capability disagree")
        if not capability.owner:
            errors.append(f"{name}: owner required")
        if not _non_empty_list(capability.config_keys):
            errors.append(f"{name}: config_keys required")
        if not capability.health_surface:
            errors.append(f"{name}: health_surface required")
        if not capability.repair_action:
            errors.append(f"{name}: repair_action required")
        for dep in capability.depends_on:
            if dep not in CAPABILITY_DEFINITIONS:
                errors.append(f"{name}: unknown dependency {dep}")
        if strict:
            errors.extend(_validate_paths((capability.repair_action,), root=root))
    return errors


def audit_privacy_retention_policy(*, strict: bool = False) -> list[str]:
    errors: list[str] = []
    required = {
        "raw_event",
        "artifact",
        "persona",
        "document",
        "reasoning",
        "api_key",
        "wiki_page",
        "action_ledger",
        "module_output",
        "activation_evidence",
        "consumer_effect",
        "migration_ledger",
        "snapshot_manifest",
        "data_export",
        "deletion_proof",
        "frozen_data_request",
        "benchmark_result",
        "install_lifecycle",
        "wiki_quality",
    }
    missing = required - set(PRIVACY_POLICIES)
    if missing:
        errors.append(f"missing privacy policies: {sorted(missing)}")
    for name, policy in PRIVACY_POLICIES.items():
        if name != policy.data_type:
            errors.append(f"{name}: key and data_type disagree")
        if policy.privacy_level == "secret" and (policy.searchable or policy.wiki_allowed or policy.model_consumable):
            errors.append(f"{name}: secrets must not be searchable/wiki/model consumable")
        if policy.encryption_required and policy.storage_class not in {"env_or_keyring"}:
            errors.append(f"{name}: encryption_required storage_class must be explicit")
        if strict and policy.retention_days is not None and policy.retention_days <= 0:
            errors.append(f"{name}: retention_days must be positive or None")
    return errors


def audit_lifecycle_status_contract(*, strict: bool = False) -> list[str]:
    errors: list[str] = []
    required = {
        "capture_events",
        "sync_log",
        "distillation_tasks",
        "dialog_reminders",
        "daemon_service",
        "document_import",
        "module_toggle",
        "quality_decision",
        "migration",
        "snapshot_restore",
        "data_ownership",
        "golden_benchmark",
        "install_lifecycle",
        "wiki_quality",
    }
    missing = required - set(LIFECYCLE_MAPPINGS)
    if missing:
        errors.append(f"missing lifecycle mappings: {sorted(missing)}")
    for name, mapping in LIFECYCLE_MAPPINGS.items():
        if name != mapping.local_surface:
            errors.append(f"{name}: key and local_surface disagree")
        if not mapping.local_statuses:
            errors.append(f"{name}: local_statuses required")
        unknown_statuses = set(mapping.local_statuses.values()) - LIFECYCLE_STATUSES
        if unknown_statuses:
            errors.append(f"{name}: unknown lifecycle statuses {sorted(unknown_statuses)}")
        unknown_failures = set(mapping.failure_classes.values()) - FAILURE_CLASSES
        if unknown_failures:
            errors.append(f"{name}: unknown failure classes {sorted(unknown_failures)}")
        if strict and "failed" in mapping.local_statuses:
            unified = mapping.local_statuses["failed"]
            if unified not in {"failed_recoverable", "failed_terminal"}:
                errors.append(f"{name}: failed must map to recoverable or terminal")
    return errors


def audit_action_ledger_contract(*, strict: bool = False) -> list[str]:
    errors: list[str] = []
    required_actions = {
        "distill_write",
        "auto_heal",
        "persona_update",
        "config_update",
        "wiki_route",
        "document_import",
        "golden_benchmark_observation",
        "quality_gate",
        "policy_patch",
        "module_toggle",
        "migration_plan",
        "migration_apply",
        "migration_rollback",
        "snapshot_create",
        "restore_plan",
        "restore_apply",
        "data_inventory",
        "data_export",
        "data_freeze",
        "data_delete",
        "deletion_proof",
        "benchmark_consumer_verify",
        "install_setup",
        "install_upgrade",
        "install_uninstall",
        "install_repair_all",
        "wiki_quality_fix",
        "cognitive_readiness_gap",
        "raw_projection_recovered",
    }
    missing = required_actions - ACTION_TYPES
    if missing:
        errors.append(f"missing action types: {sorted(missing)}")
    sample = make_action_record(
        actor="contract",
        action_type="quality_gate",
        target="contract-sample",
        evidence_refs=("core/system_contracts.py",),
        status="verified",
        verification={"command": "python3 scripts/audit_action_ledger.py --strict"},
    )
    errors.extend(sample.validate())
    if strict:
        rollback_sample = make_action_record(
            actor="contract",
            action_type="auto_heal",
            target="contract-sample",
            evidence_refs=("core/system_contracts.py",),
            status="verified",
            rollback_ref="contract://rollback/sample",
        )
        errors.extend(rollback_sample.validate())
    return errors


def audit_domain_glossary(*, strict: bool = False) -> list[str]:
    errors: list[str] = []
    required = {
        "cognitive_asset",
        "cognitive_decision_flywheel",
        "trusted_user_document",
        "raw_event",
        "obsidian_presentation_layer",
        "quality_decision",
        "action_ledger",
        "module_toggle",
        "migration_registry",
        "snapshot_manifest",
        "data_ownership_contract",
        "deletion_proof",
        "golden_benchmark",
        "install_lifecycle",
        "wiki_quality_report",
    }
    missing = required - set(DOMAIN_TERMS)
    if missing:
        errors.append(f"missing domain terms: {sorted(missing)}")
    aliases: dict[str, str] = {}
    for term, definition in DOMAIN_TERMS.items():
        if term != definition.term:
            errors.append(f"{term}: key and term disagree")
        if not definition.definition or not definition.owner:
            errors.append(f"{term}: definition and owner required")
        for alias in definition.deprecated_aliases + definition.forbidden_aliases:
            if alias in aliases:
                errors.append(f"alias {alias!r} reused by {term} and {aliases[alias]}")
            aliases[alias] = term
        if strict and definition.deprecated_aliases and not definition.migration_note:
            errors.append(f"{term}: deprecated aliases require migration_note")
    return errors


def audit_mnemos_scorecard(*, strict: bool = False) -> list[str]:
    errors: list[str] = []
    required = {
        "engineering_health",
        "deployment_migration",
        "privacy_security",
        "data_pipeline",
        "cognitive_assets",
        "user_profile",
        "auto_healing",
        "obsidian_experience",
        "wow_path",
        "module_governance",
        "data_ownership",
        "golden_benchmark",
    }
    missing = required - set(SCORECARD_DIMENSIONS)
    if missing:
        errors.append(f"missing scorecard dimensions: {sorted(missing)}")
    for name, dimension in SCORECARD_DIMENSIONS.items():
        if name != dimension.dimension:
            errors.append(f"{name}: key and dimension disagree")
        if dimension.max_score not in {100, 150}:
            errors.append(f"{name}: max_score must be 100 or 150")
        if not _non_empty_list(dimension.evidence_commands):
            errors.append(f"{name}: evidence_commands required")
        if not _non_empty_list(dimension.runtime_metrics):
            errors.append(f"{name}: runtime_metrics required")
        if not _non_empty_list(dimension.problem_refs):
            errors.append(f"{name}: problem_refs required")
        if strict and not all(
            command.startswith(("python3 ", ".venv/bin/python "))
            for command in dimension.evidence_commands
        ):
            errors.append(f"{name}: evidence commands must be executable shell commands")
    return errors


def validate_all_system_contracts(*, strict: bool = False) -> list[str]:
    audits = {
        "cognitive_asset": audit_cognitive_asset_schema,
        "quality_decision": audit_quality_decision_contract,
        "capability_registry": audit_capability_registry,
        "privacy_retention": audit_privacy_retention_policy,
        "lifecycle_status": audit_lifecycle_status_contract,
        "action_ledger": audit_action_ledger_contract,
        "domain_glossary": audit_domain_glossary,
        "scorecard": audit_mnemos_scorecard,
    }
    errors: list[str] = []
    for name, audit in audits.items():
        errors.extend(f"{name}: {error}" for error in audit(strict=strict))
    from core.module_toggles import validate_all_module_toggle_contracts
    from core.migrations.registry import audit_migration_registry
    from core.backup.snapshot_manager import audit_backup_recovery_contract
    from core.privacy.data_ownership import audit_data_ownership_contract
    from core.setup.install_lifecycle import audit_install_upgrade_contract

    errors.extend(
        f"module_toggle_contracts: {error}"
        for error in validate_all_module_toggle_contracts(strict=strict)
    )
    errors.extend(
        f"migration_registry: {error}"
        for error in audit_migration_registry(strict=strict)
    )
    errors.extend(
        f"backup_recovery: {error}"
        for error in audit_backup_recovery_contract(strict=strict)
    )
    errors.extend(
        f"data_ownership: {error}"
        for error in audit_data_ownership_contract(strict=strict)
    )
    errors.extend(
        f"install_lifecycle: {error}"
        for error in audit_install_upgrade_contract(strict=strict)
    )
    return errors


def build_contract_health() -> dict[str, Any]:
    from core.module_toggles import build_module_toggle_health
    from core.migrations.registry import MigrationRegistry
    from core.backup.snapshot_manager import SNAPSHOT_SCOPES, HIGH_RISK_ACTIONS_REQUIRING_SNAPSHOT
    from core.privacy.data_ownership import DATA_DOMAINS, DATA_SCOPES

    errors = validate_all_system_contracts(strict=True)
    module_toggle_health = build_module_toggle_health()
    migration_registry = MigrationRegistry()
    return {
        "status": "ok" if not errors else "degraded",
        "schema_versions": {
            "cognitive_asset": COGNITIVE_ASSET_SCHEMA_VERSION,
            "quality_decision": QUALITY_DECISION_SCHEMA_VERSION,
            "capability_registry": CAPABILITY_REGISTRY_SCHEMA_VERSION,
            "privacy_policy": PRIVACY_POLICY_SCHEMA_VERSION,
            "lifecycle_status": LIFECYCLE_STATUS_SCHEMA_VERSION,
            "action_ledger": ACTION_LEDGER_SCHEMA_VERSION,
            "domain_glossary": DOMAIN_GLOSSARY_SCHEMA_VERSION,
            "scorecard": SCORECARD_SCHEMA_VERSION,
            "module_toggle": MODULE_TOGGLE_SCHEMA_VERSION,
            "toggle_output": TOGGLE_OUTPUT_SCHEMA_VERSION,
            "migration": MIGRATION_SCHEMA_VERSION,
            "snapshot": SNAPSHOT_SCHEMA_VERSION,
            "data_ownership": DATA_OWNERSHIP_SCHEMA_VERSION,
            "golden_benchmark": GOLDEN_BENCHMARK_SCHEMA_VERSION,
            "install_lifecycle": INSTALL_LIFECYCLE_SCHEMA_VERSION,
            "wiki_quality": WIKI_QUALITY_SCHEMA_VERSION,
            "cognitive_readiness": COGNITIVE_READINESS_SCHEMA_VERSION,
        },
        "counts": {
            "asset_types": len(COGNITIVE_ASSET_DEFINITIONS),
            "quality_domains": len(QUALITY_GATE_DEFINITIONS),
            "capabilities": len(CAPABILITY_DEFINITIONS),
            "privacy_policies": len(PRIVACY_POLICIES),
            "lifecycle_surfaces": len(LIFECYCLE_MAPPINGS),
            "action_types": len(ACTION_TYPES),
            "domain_terms": len(DOMAIN_TERMS),
            "scorecard_dimensions": len(SCORECARD_DIMENSIONS),
            "module_toggles": module_toggle_health["counts"]["toggles"],
            "toggle_output_contracts": module_toggle_health["counts"]["output_contracts"],
            "auto_enable_candidates": module_toggle_health["counts"]["auto_enable_candidates"],
            "registered_but_unwired": module_toggle_health["counts"]["registered_but_unwired"],
            "migration_specs": len(migration_registry.specs),
            "snapshot_scopes": len(SNAPSHOT_SCOPES),
            "snapshot_high_risk_policies": len(HIGH_RISK_ACTIONS_REQUIRING_SNAPSHOT),
            "data_ownership_domains": len(DATA_DOMAINS),
            "data_ownership_scopes": len(DATA_SCOPES),
        },
        "module_toggles": module_toggle_health,
        "errors": errors,
    }


def contract_snapshot() -> dict[str, Any]:
    from core.module_toggles import module_toggle_snapshot

    return {
        "schema_versions": build_contract_health()["schema_versions"],
        "cognitive_assets": {key: asdict(value) for key, value in COGNITIVE_ASSET_DEFINITIONS.items()},
        "quality_gates": {key: asdict(value) for key, value in QUALITY_GATE_DEFINITIONS.items()},
        "capabilities": {key: asdict(value) for key, value in CAPABILITY_DEFINITIONS.items()},
        "privacy_policies": {key: asdict(value) for key, value in PRIVACY_POLICIES.items()},
        "lifecycle_mappings": {key: asdict(value) for key, value in LIFECYCLE_MAPPINGS.items()},
        "domain_terms": {key: asdict(value) for key, value in DOMAIN_TERMS.items()},
        "scorecard": {key: asdict(value) for key, value in SCORECARD_DIMENSIONS.items()},
        "module_toggles": module_toggle_snapshot()["toggles"],
    }
