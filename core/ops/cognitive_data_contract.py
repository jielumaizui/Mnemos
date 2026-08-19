"""Unified cognitive data event contracts and interface registry."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

COGNITIVE_DATA_EVENT_SCHEMA_VERSION = "mnemos.cognitive_data_event.v1"
DATA_INTERFACE_REGISTRY_SCHEMA_VERSION = "mnemos.data_interface_registry.v1"

REQUIRED_EVENT_FIELDS = (
    "event_id",
    "source_kind",
    "source_uri",
    "content_hash",
    "canonical_subject",
    "data_type",
    "producer",
    "intended_consumers",
    "privacy_level",
    "confidence",
    "evidence_refs",
    "dedupe_key",
    "created_at",
)

RECONCILIATION_TYPES = {"duplicate", "derived", "reinforcement"}
DATA_LIFECYCLE_STATUSES = {
    "produced",
    "normalized",
    "deduped",
    "consumed",
    "updated",
    "rejected",
    "expired",
    "superseded",
    "dead_letter",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_event_id(*parts: str) -> str:
    raw = "|".join(parts).encode("utf-8")
    return f"cde_{hashlib.sha256(raw).hexdigest()[:16]}"


def stable_dedupe_key(
    source_kind: str,
    canonical_subject: str,
    content_hash: str,
) -> str:
    raw = f"{source_kind}|{canonical_subject}|{content_hash}".encode("utf-8")
    return f"dedupe_{hashlib.sha256(raw).hexdigest()[:16]}"


def non_empty_strings(values: tuple[str, ...]) -> bool:
    return bool(values) and all(isinstance(value, str) and value.strip() for value in values)


@dataclass(frozen=True)
class CognitiveDataEvent:
    event_id: str
    source_kind: str
    source_uri: str
    content_hash: str
    canonical_subject: str
    data_type: str
    producer: str
    intended_consumers: tuple[str, ...]
    privacy_level: str
    confidence: float
    evidence_refs: tuple[str, ...]
    dedupe_key: str
    created_at: str
    source_id: str = ""
    asset_id: str = ""
    retention_policy: str = "default"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = COGNITIVE_DATA_EVENT_SCHEMA_VERSION

    def validate(self) -> list[str]:
        errors: list[str] = []
        consumerless_rejection = (
            self.data_type == "decision_trace"
            and self.intended_consumers == ()
        )
        for field_name in REQUIRED_EVENT_FIELDS:
            value = getattr(self, field_name)
            if field_name == "intended_consumers" and consumerless_rejection:
                continue
            if value in ("", (), None):
                errors.append(f"{field_name} is required")
        if not consumerless_rejection and not non_empty_strings(self.intended_consumers):
            errors.append("intended_consumers must be non-empty")
        if not non_empty_strings(self.evidence_refs):
            errors.append("evidence_refs must be non-empty")
        if not 0.0 <= float(self.confidence) <= 1.0:
            errors.append("confidence must be between 0 and 1")
        return errors

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["intended_consumers"] = list(self.intended_consumers)
        data["evidence_refs"] = list(self.evidence_refs)
        data["metadata"] = dict(self.metadata)
        return data


@dataclass(frozen=True)
class CognitiveDataInterface:
    interface_id: str
    source_kind: str
    data_type: str
    producer: str
    code_refs: tuple[str, ...]
    intended_consumers: tuple[str, ...]
    consumer_refs: tuple[str, ...]
    dedupe_rule: str
    lifecycle_statuses: tuple[str, ...]
    privacy_class: str
    retention_policy: str

    def validate(self, repo_root: Path | None = None) -> list[str]:
        errors: list[str] = []
        if not self.interface_id:
            errors.append("interface_id is required")
        if not self.source_kind:
            errors.append(f"{self.interface_id}: source_kind is required")
        if not self.data_type:
            errors.append(f"{self.interface_id}: data_type is required")
        if not self.producer:
            errors.append(f"{self.interface_id}: producer is required")
        if not non_empty_strings(self.code_refs):
            errors.append(f"{self.interface_id}: code_refs must be non-empty")
        if not non_empty_strings(self.intended_consumers):
            errors.append(f"{self.interface_id}: intended_consumers must be non-empty")
        if not non_empty_strings(self.consumer_refs):
            errors.append(f"{self.interface_id}: consumer_refs must be non-empty")
        if not self.dedupe_rule:
            errors.append(f"{self.interface_id}: dedupe_rule is required")
        if not set(self.lifecycle_statuses).issubset(DATA_LIFECYCLE_STATUSES):
            errors.append(f"{self.interface_id}: unsupported lifecycle status")
        if repo_root is not None:
            for ref in self.code_refs + self.consumer_refs:
                path = repo_root / ref
                if not path.exists():
                    errors.append(f"{self.interface_id}: missing code ref {ref}")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "interface_id": self.interface_id,
            "source_kind": self.source_kind,
            "data_type": self.data_type,
            "producer": self.producer,
            "code_refs": list(self.code_refs),
            "intended_consumers": list(self.intended_consumers),
            "consumer_refs": list(self.consumer_refs),
            "dedupe_rule": self.dedupe_rule,
            "lifecycle_statuses": list(self.lifecycle_statuses),
            "privacy_class": self.privacy_class,
            "retention_policy": self.retention_policy,
        }


@dataclass(frozen=True)
class CognitiveDataIdentityRule:
    """Register one event-scoped producer or consumer identity.

    Some semantic producers and consumers are emitted by a deep module through
    the shared cognitive state unit of work rather than owning a separate data
    interface.  Material-action consumers also contain a deterministic action
    digest, so an exact string allowlist cannot describe their identities.  A
    rule is therefore constrained by both the event contract and a full-match
    identifier pattern; a bare prefix is never sufficient.
    """

    rule_id: str
    identity_kind: str
    identifier_pattern: str
    source_kind: str
    data_type: str
    code_refs: tuple[str, ...]

    def validate(self, repo_root: Path | None = None) -> list[str]:
        errors: list[str] = []
        if not self.rule_id:
            errors.append("identity rule_id is required")
        if self.identity_kind not in {"producer", "consumer"}:
            errors.append(f"{self.rule_id}: unsupported identity kind")
        try:
            re.compile(self.identifier_pattern)
        except re.error as exc:
            errors.append(f"{self.rule_id}: invalid identifier pattern: {exc}")
        if not self.source_kind:
            errors.append(f"{self.rule_id}: source_kind is required")
        if not self.data_type:
            errors.append(f"{self.rule_id}: data_type is required")
        if not non_empty_strings(self.code_refs):
            errors.append(f"{self.rule_id}: code_refs must be non-empty")
        if repo_root is not None:
            for ref in self.code_refs:
                if not (repo_root / ref).exists():
                    errors.append(f"{self.rule_id}: missing code ref {ref}")
        return errors

    def matches(
        self,
        identifier: str,
        *,
        source_kind: str,
        data_type: str,
    ) -> bool:
        return bool(
            source_kind == self.source_kind
            and data_type == self.data_type
            and re.fullmatch(self.identifier_pattern, str(identifier))
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "identity_kind": self.identity_kind,
            "identifier_pattern": self.identifier_pattern,
            "source_kind": self.source_kind,
            "data_type": self.data_type,
            "code_refs": list(self.code_refs),
        }


COGNITIVE_DATA_INTERFACES: tuple[CognitiveDataInterface, ...] = (
    CognitiveDataInterface(
        "capture_service_turn",
        "raw_capture",
        "conversation_turn",
        "capture_service",
        ("core/sync_framework/capture_service.py",),
        ("capture_queue",),
        ("core/sync_framework/capture_queue.py",),
        "source_kind + canonical_subject + content_hash",
        ("produced", "deduped", "consumed", "dead_letter"),
        "local",
        "raw_retention",
    ),
    CognitiveDataInterface(
        "capture_queue_event",
        "capture_queue",
        "queued_capture_event",
        "capture_queue",
        ("core/sync_framework/capture_queue.py",),
        ("capture_worker",),
        ("core/sync_framework/capture_worker.py",),
        "dedupe_key + ttl",
        ("produced", "consumed", "expired", "dead_letter"),
        "local",
        "queue_retention",
    ),
    CognitiveDataInterface(
        "sync_engine_turn",
        "sync_engine",
        "synced_turn",
        "sync_engine",
        ("core/sync_framework/sync_engine.py",),
        ("amphora", "distill", "persona"),
        (
            "core/kia/amphora.py",
            "core/hephaestus/distillation_engine.py",
            "core/persona/psyche.py",
        ),
        "canonical_session_id + turn_number + content_hash",
        ("produced", "normalized", "deduped", "consumed"),
        "local",
        "raw_retention",
    ),
    CognitiveDataInterface(
        "file_ingestor_document",
        "file_ingest",
        "trusted_document",
        "file_ingestor",
        ("core/sync_framework/file_ingestor.py",),
        ("document_processor", "amphora", "distill"),
        (
            "core/document_import.py",
            "core/kia/amphora.py",
            "core/hephaestus/distillation_engine.py",
        ),
        "content_hash + canonical_subject",
        ("produced", "normalized", "deduped", "consumed", "rejected"),
        "local",
        "document_retention",
    ),
    CognitiveDataInterface(
        "document_processor_document",
        "document_processor",
        "parsed_document",
        "document_processor",
        ("core/document_import.py",),
        ("distill", "search", "wiki"),
        (
            "core/hephaestus/distillation_engine.py",
            "core/app/context_search.py",
            "core/hephaestus/distillation_wiki_page.py",
        ),
        "source_uri + content_hash",
        ("produced", "normalized", "consumed", "rejected"),
        "local",
        "document_retention",
    ),
    CognitiveDataInterface(
        "amphora_distill_task",
        "amphora",
        "distill_task",
        "amphora",
        ("core/kia/amphora.py",),
        ("distill_worker", "quality_gate"),
        ("core/hephaestus_worker.py", "core/hephaestus/quality_gate.py"),
        "task_id",
        ("produced", "consumed", "updated", "dead_letter"),
        "local",
        "queue_retention",
    ),
    CognitiveDataInterface(
        "event_bus_event",
        "event_bus",
        "system_event",
        "mnemos_bus",
        ("core/mnemos_bus.py",),
        ("kia", "persona", "reflection"),
        (
            "core/kia/kia_event_consumer.py",
            "core/persona/psyche.py",
            "core/reflection/consumers.py",
        ),
        "event_id",
        ("produced", "consumed", "dead_letter"),
        "local",
        "event_retention",
    ),
    CognitiveDataInterface(
        "reflection_record",
        "reflection",
        "reflection_record",
        "reflection_store",
        ("core/reflection/reflection_store.py",),
        ("persona", "policy", "preflight"),
        (
            "core/persona/psyche.py",
            "core/cognitive/policy_patch.py",
            "integrations/preflight_builder.py",
        ),
        "record_id + cognitive_shift",
        ("produced", "consumed", "updated", "superseded"),
        "local",
        "reflection_retention",
    ),
    CognitiveDataInterface(
        "adaptive_scoring_sample",
        "adaptive_scoring",
        "feedback_sample",
        "adaptive_scorer",
        ("core/scoring/adaptive_scorer_v2.py",),
        ("ranker", "health", "scorecard"),
        ("core/app/context_search.py", "core/ops/health_check.py", "core/system_contracts.py"),
        "query + result_id + outcome",
        ("produced", "consumed", "updated"),
        "local",
        "scoring_retention",
    ),
    CognitiveDataInterface(
        "distill_action",
        "distill_action",
        "knowledge_action",
        "distill_action_router",
        ("core/hephaestus/distill_action_router.py",),
        ("wiki", "observation", "reflection", "policy"),
        (
            "core/hephaestus/distillation_wiki_page.py",
            "core/cognitive/observation_store.py",
            "core/reflection/consumers.py",
            "core/cognitive/policy_patch.py",
        ),
        "action_id + target + action",
        ("produced", "consumed", "updated", "rejected"),
        "local",
        "knowledge_retention",
    ),
    CognitiveDataInterface(
        "cognition_asset_commit",
        "distill_skill",
        "cognitive_decision_asset",
        "cognition_asset_store",
        ("core/hephaestus/cognition_asset_store.py",),
        ("wiki", "wiki_search_index", "cognitive_decision_asset_proposal"),
        (
            "core/hephaestus/distillation_write_receipt.py",
            "core/wiki_projection_lifecycle.py",
            "core/hephaestus/cognition_asset_store.py",
        ),
        "input spec + extraction root + final fragments + source spans + policy version",
        ("produced", "consumed", "updated", "rejected"),
        "private",
        "knowledge_retention",
    ),
    CognitiveDataInterface(
        "cognitive_state_revision",
        "cognitive_state",
        "typed_cognitive_revision",
        "cognitive_state_store",
        (
            "core/cognitive/state_store.py",
            "core/application/cognitive_state.py",
        ),
        ("wiki", "cognitive_graph"),
        (
            "core/wiki_projection_lifecycle.py",
            "core/cognitive_graph/store.py",
        ),
        "typed payload hash + source revision + object revision lineage",
        ("produced", "consumed", "rejected", "dead_letter"),
        "private",
        "cognitive_state_retention",
    ),
    CognitiveDataInterface(
        "persona_profile_signal",
        "persona",
        "profile_signal",
        "persona_signal_store",
        ("core/persona/cognitive_profile.py",),
        ("preflight", "search", "distill", "quality_gate", "auto_healing", "cognitive_flywheel"),
        (
            "integrations/preflight_builder.py",
            "core/app/context_search.py",
            "core/hephaestus/prompt_builder.py",
            "core/hephaestus/cognitive_value_gate.py",
            "core/ops/auto_healing.py",
            "core/kia/ixion.py",
        ),
        "source_event_id + dimension + value",
        ("produced", "normalized", "deduped", "consumed", "superseded"),
        "local",
        "persona_retention",
    ),
)


COGNITIVE_DATA_IDENTITY_RULES: tuple[CognitiveDataIdentityRule, ...] = (
    CognitiveDataIdentityRule(
        "decision_trace_store_producer",
        "producer",
        r"decision_trace_store",
        "material_decision",
        "decision_trace",
        ("core/cognitive/decision_trace_store.py",),
    ),
    CognitiveDataIdentityRule(
        "observation_calibrator_producer",
        "producer",
        r"observation_calibrator",
        "observation_calibration",
        "calibration_record",
        ("core/cognitive/calibration_record.py",),
    ),
    CognitiveDataIdentityRule(
        "material_action_consumer",
        "consumer",
        r"material-action:[a-z][a-z0-9_-]{0,62}:[0-9a-f]{16}",
        "material_decision",
        "decision_trace",
        ("core/cognitive/decision_trace_store.py",),
    ),
    CognitiveDataIdentityRule(
        "observation_index_consumer",
        "consumer",
        r"observation_index",
        "observation_calibration",
        "calibration_record",
        (
            "core/cognitive/calibration_record.py",
            "core/cognitive/observation_engine.py",
        ),
    ),
    CognitiveDataIdentityRule(
        "calibration_wiki_projection_consumer",
        "consumer",
        r"wiki_projection",
        "observation_calibration",
        "calibration_record",
        (
            "core/cognitive/calibration_record.py",
            "core/cognitive/observation_engine.py",
        ),
    ),
    CognitiveDataIdentityRule(
        "cognition_episode_knowledge_graph_consumer",
        "consumer",
        r"knowledge_graph",
        "distillation_extraction",
        "cognition_episode",
        ("core/cognitive/cognition_episode_persistence.py",),
    ),
)


def _matches_identity_rule(
    identity_kind: str,
    identifier: str,
    *,
    source_kind: str,
    data_type: str,
) -> bool:
    return any(
        rule.identity_kind == identity_kind
        and rule.matches(
            identifier,
            source_kind=source_kind,
            data_type=data_type,
        )
        for rule in COGNITIVE_DATA_IDENTITY_RULES
    )


def _identity_event_field(
    event: CognitiveDataEvent | Mapping[str, Any],
    field_name: str,
) -> str:
    if isinstance(event, Mapping):
        return str(event.get(field_name) or "")
    return str(getattr(event, field_name))


def is_registered_producer(
    event: CognitiveDataEvent | Mapping[str, Any],
) -> bool:
    producer = _identity_event_field(event, "producer")
    if producer in {contract.producer for contract in COGNITIVE_DATA_INTERFACES}:
        return True
    return _matches_identity_rule(
        "producer",
        producer,
        source_kind=_identity_event_field(event, "source_kind"),
        data_type=_identity_event_field(event, "data_type"),
    )


def is_registered_consumer(
    event: CognitiveDataEvent | Mapping[str, Any],
    consumer_id: str,
) -> bool:
    registry_consumers = {
        consumer
        for contract in COGNITIVE_DATA_INTERFACES
        for consumer in contract.intended_consumers
    }
    if consumer_id in registry_consumers:
        return True
    return _matches_identity_rule(
        "consumer",
        consumer_id,
        source_kind=_identity_event_field(event, "source_kind"),
        data_type=_identity_event_field(event, "data_type"),
    )


RUNTIME_INSTRUMENTED_INTERFACE_IDS = frozenset(
    {
        "capture_service_turn",
        "capture_queue_event",
        "sync_engine_turn",
    }
)


def data_interface_registry_payload() -> dict[str, Any]:
    return {
        "schema_version": DATA_INTERFACE_REGISTRY_SCHEMA_VERSION,
        "required_event_fields": list(REQUIRED_EVENT_FIELDS),
        "interfaces": [
            {
                **contract.as_dict(),
                "evidence_mode": (
                    "runtime_receipts"
                    if contract.interface_id in RUNTIME_INSTRUMENTED_INTERFACE_IDS
                    else "contract_only"
                ),
            }
            for contract in COGNITIVE_DATA_INTERFACES
        ],
        "identity_rules": [
            rule.as_dict() for rule in COGNITIVE_DATA_IDENTITY_RULES
        ],
        "runtime_instrumented_interface_ids": sorted(RUNTIME_INSTRUMENTED_INTERFACE_IDS),
    }


def validate_data_interface_registry(repo_root: Path | None = None) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for contract in COGNITIVE_DATA_INTERFACES:
        if contract.interface_id in seen:
            errors.append(f"duplicate interface id {contract.interface_id}")
        seen.add(contract.interface_id)
        errors.extend(contract.validate(repo_root=repo_root))
    seen_rules: set[str] = set()
    for rule in COGNITIVE_DATA_IDENTITY_RULES:
        if rule.rule_id in seen_rules:
            errors.append(f"duplicate identity rule id {rule.rule_id}")
        seen_rules.add(rule.rule_id)
        errors.extend(rule.validate(repo_root=repo_root))
    required_producers = {
        "capture_service",
        "capture_queue",
        "sync_engine",
        "file_ingestor",
        "document_processor",
        "amphora",
        "mnemos_bus",
        "reflection_store",
        "adaptive_scorer",
        "distill_action_router",
        "cognition_asset_store",
        "cognitive_state_store",
        "persona_signal_store",
    }
    actual = {contract.producer for contract in COGNITIVE_DATA_INTERFACES}
    missing = sorted(required_producers - actual)
    if missing:
        errors.append(f"missing required producers: {', '.join(missing)}")
    return errors


def classify_reconciliation(
    existing: Mapping[str, Any],
    incoming: Mapping[str, Any],
) -> str:
    same_subject = existing.get("canonical_subject") == incoming.get("canonical_subject")
    same_type = existing.get("data_type") == incoming.get("data_type")
    if not same_subject or not same_type:
        return ""
    if existing.get("content_hash") == incoming.get("content_hash"):
        return "duplicate"
    if existing.get("source_uri") == incoming.get("source_uri"):
        return "derived"
    if existing.get("producer") != incoming.get("producer"):
        return "reinforcement"
    return ""
