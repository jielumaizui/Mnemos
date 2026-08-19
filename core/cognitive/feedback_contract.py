"""Immutable contracts shared by canonical feedback attribution owners."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping

from core.cognitive.access_control import validate_cognitive_access_envelope


FEEDBACK_TARGET_REGISTRY_VERSION = "mnemos.feedback_target_registry.v1"
FEEDBACK_TARGETS = (
    "belief_correction_proposal",
    "delivery_state",
    "persona_proposal",
    "policy_proposal",
    "reflection_evidence",
    "training_evidence",
    "trust_proposal",
)

FEEDBACK_SOURCE_CHANNELS = frozenset(
    {
        "predictive_push",
        "context_search",
        "reflection",
        "retrospective",
        "dialog_decision_push",
        "dialog_reminder",
        "delivery_feedback",
        "outcome_compatibility",
    }
)

FEEDBACK_TARGET_JOURNAL_CONTRACTS = {
    "belief_correction_proposal": {
        "db_file": "cognitive_graph.db",
        "owner_id": "cognitive_graph_store",
        "proposal_table": "belief_feedback_proposals",
        "action_table": "belief_feedback_proposal_actions",
        "receipt_table": "belief_feedback_proposal_receipts",
        "gate_contract_id": "mnemos.belief_feedback_correction_review.v1",
    },
    "delivery_state": {
        "db_file": "delivery_events.db",
        "owner_id": "delivery_router",
        "proposal_table": "delivery_feedback_proposals",
        "action_table": "delivery_feedback_proposal_actions",
        "receipt_table": "delivery_feedback_proposal_receipts",
        "gate_contract_id": "mnemos.delivery_feedback_proposal_review.v1",
    },
    "persona_proposal": {
        "db_file": "user_signals.db",
        "owner_id": "persona_signal_store",
        "proposal_table": "persona_feedback_proposals",
        "action_table": "persona_feedback_proposal_actions",
        "receipt_table": "persona_feedback_proposal_receipts",
        "gate_contract_id": "mnemos.persona_feedback_proposal_review.v1",
    },
    "policy_proposal": {
        "db_file": "policy_patches.db",
        "owner_id": "policy_patch_store",
        "proposal_table": "policy_feedback_proposals",
        "action_table": "policy_feedback_proposal_actions",
        "receipt_table": "policy_feedback_proposal_receipts",
        "gate_contract_id": "mnemos.policy_feedback_proposal_material_review.v1",
    },
    "reflection_evidence": {
        "db_file": "reflections.db",
        "owner_id": "reflection_store",
        "proposal_table": "reflection_feedback_proposals",
        "action_table": "reflection_feedback_proposal_actions",
        "receipt_table": "reflection_feedback_proposal_receipts",
        "gate_contract_id": "mnemos.reflection_feedback_evidence_review.v1",
    },
    "training_evidence": {
        "db_file": "mnemos.db",
        "owner_id": "adaptive_scorer",
        "proposal_table": "training_feedback_proposals",
        "action_table": "training_feedback_proposal_actions",
        "receipt_table": "training_feedback_proposal_receipts",
        "gate_contract_id": "mnemos.training_feedback_evidence_cog048_review.v1",
    },
    "trust_proposal": {
        "db_file": "trust_decisions.db",
        "owner_id": "trusted_push",
        "proposal_table": "trust_feedback_proposals",
        "action_table": "trust_feedback_proposal_actions",
        "receipt_table": "trust_feedback_proposal_receipts",
        "gate_contract_id": "mnemos.trust_feedback_proposal_review.v1",
    },
}

if tuple(sorted(FEEDBACK_TARGET_JOURNAL_CONTRACTS)) != FEEDBACK_TARGETS:
    raise RuntimeError("feedback target journal contract drift")


def _sha256_json(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


FEEDBACK_TARGET_REGISTRY_HASH = _sha256_json(
    {"version": FEEDBACK_TARGET_REGISTRY_VERSION, "targets": list(FEEDBACK_TARGETS)}
)


REACTION_KINDS = frozenset(
    {
        "accept",
        "ignore",
        "dismiss",
        "inaccurate",
        "outdated",
        "accurate",
        "useful",
        "insightful",
        "irrelevant",
        "opened",
        "clicked",
        "read",
        "dwell_observed",
        "repeated_query",
        "no_click",
        "silence_window_closed",
    }
)

EXPLICIT_REACTION_KINDS = frozenset(
    {
        "accept",
        "ignore",
        "dismiss",
        "inaccurate",
        "outdated",
        "accurate",
        "useful",
        "insightful",
        "irrelevant",
    }
)

ATTRIBUTION_DISPOSITIONS = frozenset(
    {
        "record_only",
        "proposal_eligible",
        "objective_only",
        "correction_pending",
        "compensation_pending",
        "superseded",
        "rejected",
    }
)

_FACT_BY_REACTION_KIND = {
    "accept": "accepted",
    "ignore": "ignored",
    "dismiss": "dismissed",
    "inaccurate": "inaccurate",
    "outdated": "outdated",
    "accurate": "accurate",
    "useful": "useful",
    "insightful": "insightful",
    "irrelevant": "irrelevant",
    "opened": "opened",
    "clicked": "clicked",
    "read": "read",
    "dwell_observed": "dwell_seconds",
    "repeated_query": "repeated_query",
    "no_click": "no_click",
    "silence_window_closed": "silence_window_closed",
}

_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REACTION_ID_PATTERN = re.compile(r"reaction-[0-9a-f]{32}\Z")
_REACTION_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "reaction_id",
        "revision_state",
        "reaction_input_hash",
        "source_event_ref",
        "observed_at",
        "recorded_at",
        "supersedes_event_id",
        "correction_of_event_id",
        "principal_ref",
        "scope",
        "source_channel",
        "authority_class",
        "subject_ref",
        "decision_ref",
        "prediction_ref",
        "action_ref",
        "delivery_ref",
        "display_ref",
        "search_ref",
        "interaction",
        "evidence",
        "observation_window",
        "exposure",
        "competing_causes",
        "source_completeness",
        "attribution",
        "downstream",
        "correction",
        "access_control",
    }
)


def _timestamp(value: Any, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_entity_ref(value: Any, field_name: str) -> None:
    fields = {"state", "id", "revision_id", "content_hash", "unavailable_reason"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{field_name} is invalid")
    if value["state"] == "available":
        if (
            not str(value["id"]).strip()
            or not str(value["revision_id"]).strip()
            or not _SHA256_PATTERN.fullmatch(str(value["content_hash"]))
            or value["unavailable_reason"]
        ):
            raise ValueError(f"{field_name} is invalid")
    elif value["state"] == "unavailable":
        if (
            value["id"]
            or value["revision_id"]
            or value["content_hash"]
            or not str(value["unavailable_reason"]).strip()
        ):
            raise ValueError(f"{field_name} is invalid")
    else:
        raise ValueError(f"{field_name} is invalid")


def _validate_display_ref(value: Any) -> None:
    fields = {"state", "display_id", "content_hash", "unavailable_reason"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("reaction display ref is invalid")
    if value["state"] == "available":
        valid = bool(
            str(value["display_id"]).strip()
            and _SHA256_PATTERN.fullmatch(str(value["content_hash"]))
            and not value["unavailable_reason"]
        )
    elif value["state"] == "unavailable":
        valid = bool(
            not value["display_id"]
            and not value["content_hash"]
            and str(value["unavailable_reason"]).strip()
        )
    else:
        valid = False
    if not valid:
        raise ValueError("reaction display ref is invalid")


def _validate_search_ref(value: Any) -> None:
    fields = {
        "state",
        "session_id",
        "result_id",
        "exposure_id",
        "unavailable_reason",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("reaction search ref is invalid")
    if value["state"] == "available":
        valid = bool(
            str(value["session_id"]).strip()
            and str(value["result_id"]).strip()
            and str(value["exposure_id"]).strip()
            and not value["unavailable_reason"]
        )
    elif value["state"] == "unavailable":
        valid = bool(
            not value["session_id"]
            and not value["result_id"]
            and not value["exposure_id"]
            and str(value["unavailable_reason"]).strip()
        )
    else:
        valid = False
    if not valid:
        raise ValueError("reaction search ref is invalid")


def reaction_input_hash(payload: Mapping[str, Any]) -> str:
    """Return the immutable identity of caller-observed reaction input."""

    identity = {
        str(key): value
        for key, value in payload.items()
        if key
        not in {
            "reaction_id",
            "revision_state",
            "reaction_input_hash",
            "recorded_at",
            "attribution",
            "downstream",
        }
    }
    return _sha256_json(identity)


def attribution_input_set_hash(payload: Mapping[str, Any]) -> str:
    """Bind the evidence set and policy identities used by attribution."""

    return _sha256_json(
        {
            "reaction_refs": payload.get("reaction_refs"),
            "outcome_refs": payload.get("outcome_refs"),
            "independence_keys": payload.get("independence_keys"),
            "method": payload.get("method"),
            "target_registry": payload.get("target_registry"),
        }
    )


def validate_user_reaction_payload(payload: Mapping[str, Any]) -> None:
    """Reject reaction kinds outside the fixed feedback taxonomy."""

    if set(payload) != _REACTION_PAYLOAD_FIELDS:
        raise ValueError("user_reaction_event fields are invalid")
    if payload.get("schema_version") != "mnemos.user_reaction_event.v1":
        raise ValueError("user_reaction_event schema_version mismatch")
    if not _REACTION_ID_PATTERN.fullmatch(str(payload.get("reaction_id") or "")):
        raise ValueError("user_reaction_event reaction_id is invalid")
    if payload.get("revision_state") not in {"recorded", "corrected"}:
        raise ValueError("user_reaction_event revision_state is invalid")
    observed_at = _timestamp(payload.get("observed_at"), "reaction observed_at")
    recorded_at = _timestamp(payload.get("recorded_at"), "reaction recorded_at")
    if recorded_at < observed_at:
        raise ValueError("reaction timestamp order is invalid")
    source_ref = payload.get("source_event_ref")
    if (
        not isinstance(source_ref, Mapping)
        or set(source_ref) != {"event_id", "source_revision_id", "content_hash"}
        or not str(source_ref["event_id"]).strip()
        or not str(source_ref["source_revision_id"]).strip()
        or not _SHA256_PATTERN.fullmatch(str(source_ref["content_hash"]))
    ):
        raise ValueError("reaction source event ref is invalid")
    subject_ref = payload.get("subject_ref")
    if (
        not isinstance(subject_ref, Mapping)
        or set(subject_ref) != {"type", "id"}
        or not str(subject_ref["type"]).strip()
        or not str(subject_ref["id"]).strip()
    ):
        raise ValueError("reaction subject ref is invalid")
    principal_ref = payload.get("principal_ref")
    if (
        not isinstance(principal_ref, Mapping)
        or set(principal_ref) != {"principal_id", "agent", "authorization_ref"}
        or not str(principal_ref["principal_id"]).strip()
        or not str(principal_ref["agent"]).strip()
        or not str(principal_ref["authorization_ref"]).strip()
    ):
        raise ValueError("reaction principal ref is invalid")
    scope = payload.get("scope")
    if (
        not isinstance(scope, Mapping)
        or set(scope) != {"type", "id", "project", "session_id"}
        or not str(scope["type"]).strip()
        or not str(scope["id"]).strip()
    ):
        raise ValueError("reaction scope is invalid")
    if payload.get("source_channel") not in FEEDBACK_SOURCE_CHANNELS:
        raise ValueError("reaction source channel is invalid")
    access_control = payload.get("access_control")
    if not isinstance(access_control, Mapping):
        raise ValueError("reaction access control is invalid")
    access = validate_cognitive_access_envelope(
        access_control,
        expected_scope_type=str(scope["type"]),
        expected_scope_id=str(scope["id"]),
    )
    if (
        str(access["owner"]["principal_id"]) != str(principal_ref["principal_id"])
        or str(access["owner"]["agent"]).lower()
        != str(principal_ref["agent"]).lower()
        or str(access["scope"]["project"]) != str(scope["project"]).lower()
        or str(access["scope"]["session_id"]) != str(scope["session_id"])
    ):
        raise ValueError("reaction principal or scope binding is invalid")
    for field in ("decision_ref", "prediction_ref", "action_ref"):
        _validate_entity_ref(payload.get(field), f"reaction {field.replace('_', ' ')}")
    delivery_ref = payload.get("delivery_ref")
    if not isinstance(delivery_ref, Mapping) or set(delivery_ref) != {
        "state",
        "event_id",
        "event_payload_hash",
        "unavailable_reason",
    }:
        raise ValueError("reaction delivery ref is invalid")
    if delivery_ref["state"] == "available":
        if (
            not str(delivery_ref["event_id"]).strip()
            or not _SHA256_PATTERN.fullmatch(str(delivery_ref["event_payload_hash"]))
            or delivery_ref["unavailable_reason"]
        ):
            raise ValueError("reaction delivery ref is invalid")
    elif delivery_ref["state"] == "unavailable":
        if (
            delivery_ref["event_id"]
            or delivery_ref["event_payload_hash"]
            or not str(delivery_ref["unavailable_reason"]).strip()
        ):
            raise ValueError("reaction delivery ref is invalid")
    else:
        raise ValueError("reaction delivery ref is invalid")
    _validate_display_ref(payload.get("display_ref"))
    _validate_search_ref(payload.get("search_ref"))
    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping) or set(evidence) != {
        "refs",
        "content_hashes",
    }:
        raise ValueError("reaction evidence is incomplete")
    evidence_refs = evidence["refs"]
    evidence_hashes = evidence["content_hashes"]
    if (
        not isinstance(evidence_refs, (list, tuple))
        or not evidence_refs
        or not isinstance(evidence_hashes, (list, tuple))
        or len(evidence_refs) != len(evidence_hashes)
        or any(not str(value).strip() for value in evidence_refs)
        or any(not _SHA256_PATTERN.fullmatch(str(value)) for value in evidence_hashes)
    ):
        raise ValueError("reaction evidence is incomplete")
    interaction = payload.get("interaction")
    if not isinstance(interaction, Mapping):
        raise ValueError("reaction interaction is invalid")
    kind = interaction.get("kind")
    if kind not in REACTION_KINDS:
        raise ValueError("unsupported_reaction_kind")
    expected_authority = (
        "explicit_user" if kind in EXPLICIT_REACTION_KINDS else "tool_observation"
    )
    if payload.get("authority_class") != expected_authority:
        raise ValueError("reaction source authority mismatch")
    if set(interaction) != {"kind", "observed_facts"}:
        raise ValueError("reaction interaction is invalid")
    observed_facts = interaction["observed_facts"]
    if not isinstance(observed_facts, (list, tuple)) or len(observed_facts) != 1:
        raise ValueError("reaction observed facts do not match registered kind")
    fact = observed_facts[0]
    if not isinstance(fact, Mapping) or set(fact) != {"name", "value"}:
        raise ValueError("reaction observed facts do not match registered kind")
    expected_name = _FACT_BY_REACTION_KIND[str(kind)]
    value = fact["value"]
    valid_value = (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and float(value) >= 0.0
        if expected_name == "dwell_seconds"
        else value is True
    )
    if fact["name"] != expected_name or not valid_value:
        raise ValueError("reaction observed facts do not match registered kind")
    window = payload.get("observation_window")
    if not isinstance(window, Mapping) or set(window) != {
        "starts_at",
        "ends_at",
        "status",
    }:
        raise ValueError("reaction observation window is invalid")
    window_start = _timestamp(window["starts_at"], "reaction window starts_at")
    window_end = _timestamp(window["ends_at"], "reaction window ends_at")
    if (
        window["status"] != "closed"
        or window_start > observed_at
        or observed_at > window_end
    ):
        raise ValueError("reaction observation window is invalid")
    exposure = payload.get("exposure")
    if (
        not isinstance(exposure, Mapping)
        or set(exposure) != {
            "session_id",
            "exposure_id",
            "interface_id",
            "was_visible",
        }
        or str(exposure["session_id"]) != str(scope["session_id"])
        or (
            str(scope["type"]) == "session"
            and not str(exposure["session_id"]).strip()
        )
        or not str(exposure["exposure_id"]).strip()
        or not str(exposure["interface_id"]).strip()
        or not isinstance(exposure["was_visible"], bool)
    ):
        raise ValueError("reaction exposure is invalid")
    search_ref = payload["search_ref"]
    if (
        search_ref["state"] == "available"
        and str(search_ref["exposure_id"]) != str(exposure["exposure_id"])
    ):
        raise ValueError("reaction search exposure binding is invalid")
    competing_causes = payload.get("competing_causes")
    if not isinstance(competing_causes, (list, tuple)):
        raise ValueError("reaction competing causes are invalid")
    for cause in competing_causes:
        if (
            not isinstance(cause, Mapping)
            or set(cause) != {"cause", "evidence_ref", "content_hash"}
            or not str(cause["cause"]).strip()
            or not str(cause["evidence_ref"]).strip()
            or not _SHA256_PATTERN.fullmatch(str(cause["content_hash"]))
        ):
            raise ValueError("reaction competing causes are invalid")
    completeness = payload.get("source_completeness")
    if (
        not isinstance(completeness, Mapping)
        or set(completeness) != {"state", "missing_refs"}
        or completeness["state"] not in {"complete", "incomplete"}
        or not isinstance(completeness["missing_refs"], (list, tuple))
        or any(not str(value).strip() for value in completeness["missing_refs"])
        or (
            completeness["state"] == "complete"
            and bool(completeness["missing_refs"])
        )
        or (
            completeness["state"] == "incomplete"
            and not completeness["missing_refs"]
        )
    ):
        raise ValueError("reaction source completeness is invalid")
    reaction_attribution = payload.get("attribution")
    expected_evidence_class = (
        "explicit_correction"
        if kind in {"inaccurate", "outdated"}
        else "explicit_preference"
        if kind in EXPLICIT_REACTION_KINDS
        else "weak_behavior"
    )
    expected_disposition = (
        "correction_pending"
        if expected_evidence_class == "explicit_correction"
        else "record_only"
    )
    if (
        not isinstance(reaction_attribution, Mapping)
        or set(reaction_attribution)
        != {"method", "version", "code_hash", "spec_hash", "disposition", "evidence_class"}
        or reaction_attribution["method"] != "conservative_observation"
        or reaction_attribution["version"] != "v1"
        or not _SHA256_PATTERN.fullmatch(str(reaction_attribution["code_hash"]))
        or not _SHA256_PATTERN.fullmatch(str(reaction_attribution["spec_hash"]))
        or reaction_attribution["disposition"] != expected_disposition
        or reaction_attribution["evidence_class"] != expected_evidence_class
    ):
        raise ValueError("reaction attribution contract is invalid")
    if payload.get("reaction_input_hash") != reaction_input_hash(payload):
        raise ValueError("reaction input hash mismatch")
    correction = payload.get("correction")
    if not isinstance(correction, Mapping) or set(correction) != {
        "state",
        "target_ref",
        "reason",
    }:
        raise ValueError("reaction correction state is invalid")
    if kind in {"inaccurate", "outdated"}:
        if (
            correction["state"] != "requested"
            or not str(correction["target_ref"]).strip()
            or not str(correction["reason"]).strip()
            or bool(str(payload.get("supersedes_event_id") or "").strip())
            != bool(str(payload.get("correction_of_event_id") or "").strip())
        ):
            raise ValueError("reaction correction lineage is incomplete")
    elif correction != {"state": "none", "target_ref": "", "reason": ""} or payload.get(
        "correction_of_event_id"
    ):
        raise ValueError("non-correction reaction claims correction lineage")
    downstream = payload.get("downstream")
    if not isinstance(downstream, Mapping) or set(downstream) != {
        "registry_version",
        "required_targets",
        "eligible_targets",
        "exclusions",
    }:
        raise ValueError("reaction downstream registry is incomplete")
    required_targets = tuple(str(value) for value in downstream["required_targets"])
    eligible_targets = tuple(str(value) for value in downstream["eligible_targets"])
    exclusions = downstream["exclusions"]
    if (
        downstream["registry_version"] != FEEDBACK_TARGET_REGISTRY_VERSION
        or required_targets != FEEDBACK_TARGETS
        or tuple(sorted(set(eligible_targets))) != eligible_targets
        or not set(eligible_targets).issubset(FEEDBACK_TARGETS)
        or not isinstance(exclusions, (list, tuple))
    ):
        raise ValueError("reaction downstream registry is incomplete")
    excluded_targets: list[str] = []
    for item in exclusions:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"target_id", "reason"}
            or not str(item["reason"]).strip()
        ):
            raise ValueError("reaction downstream exclusion is invalid")
        excluded_targets.append(str(item["target_id"]))
    if tuple(excluded_targets) != tuple(
        target_id for target_id in FEEDBACK_TARGETS if target_id not in eligible_targets
    ):
        raise ValueError("reaction downstream registry is incomplete")
    expected_eligible = FEEDBACK_TARGETS if kind in {"inaccurate", "outdated"} else ()
    if eligible_targets != expected_eligible:
        raise ValueError("reaction downstream eligibility is inconsistent")


def validate_feedback_attribution_payload(payload: Mapping[str, Any]) -> None:
    """Validate the fixed identity of a canonical attribution record."""

    if payload.get("schema_version") != "mnemos.feedback_attribution_record.v1":
        raise ValueError("feedback_attribution_record schema_version mismatch")
    if payload.get("input_set_hash") != attribution_input_set_hash(payload):
        raise ValueError("attribution input set hash mismatch")
    disposition = payload.get("disposition")
    if disposition not in ATTRIBUTION_DISPOSITIONS:
        raise ValueError("feedback attribution disposition is invalid")
    post_neutralization = payload.get("post_neutralization_disposition")
    if post_neutralization not in {"record_only", "proposal_eligible", "objective_only"}:
        raise ValueError("feedback post-neutralization disposition is invalid")
    if disposition != "correction_pending" and post_neutralization != disposition:
        raise ValueError("feedback post-neutralization disposition is inconsistent")
    materiality = payload.get("materiality")
    materiality_fields = {
        "decision",
        "observation_count",
        "distinct_session_count",
        "distinct_exposure_count",
        "span_seconds",
        "minimum_event_count",
        "minimum_independence_count",
        "minimum_span_seconds",
        "conflict_state",
    }
    if not isinstance(materiality, Mapping) or set(materiality) != materiality_fields:
        raise ValueError("feedback materiality proof is invalid")
    if (
        materiality["minimum_event_count"] != 3
        or materiality["minimum_independence_count"] != 2
        or materiality["minimum_span_seconds"] != 86400
    ):
        raise ValueError("feedback materiality registry mismatch")
    for field in (
        "observation_count",
        "distinct_session_count",
        "distinct_exposure_count",
        "span_seconds",
    ):
        value = materiality[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("feedback materiality proof is invalid")
    if materiality["decision"] != disposition:
        raise ValueError("feedback materiality disposition mismatch")
    weak_threshold_met = (
        materiality["observation_count"] >= 3
        and max(
            materiality["distinct_session_count"],
            materiality["distinct_exposure_count"],
        )
        >= 2
        and materiality["span_seconds"] >= 86400
        and materiality["conflict_state"] == "clear"
    )
    if (
        payload.get("evidence_class") == "weak_behavior"
        and disposition == "proposal_eligible"
        and not weak_threshold_met
    ):
        raise ValueError("weak feedback materiality threshold not met")
    registry = payload.get("target_registry")
    expected_registry = {
        "version": FEEDBACK_TARGET_REGISTRY_VERSION,
        "registry_hash": FEEDBACK_TARGET_REGISTRY_HASH,
        "targets": list(FEEDBACK_TARGETS),
    }
    if registry != expected_registry:
        raise ValueError("feedback target registry mismatch")
    dispositions = payload.get("target_dispositions")
    if not isinstance(dispositions, (list, tuple)):
        raise ValueError("target dispositions are incomplete")
    target_ids: list[str] = []
    for item in dispositions:
        if not isinstance(item, Mapping) or set(item) != {
            "target_id",
            "eligible",
            "exclusion_reason",
            "command_ref",
        }:
            raise ValueError("target disposition is invalid")
        target_id = str(item["target_id"])
        target_ids.append(target_id)
        if not isinstance(item["eligible"], bool):
            raise ValueError("target disposition eligibility is invalid")
        exclusion_reason = item["exclusion_reason"]
        if item["eligible"] is bool(exclusion_reason):
            raise ValueError("target disposition exclusion is inconsistent")
        command_ref = item["command_ref"]
        if (
            not isinstance(command_ref, Mapping)
            or set(command_ref) != {"command_key", "command_type"}
            or not str(command_ref["command_key"]).strip()
            or command_ref["command_type"]
            not in {
                "evaluate_feedback_target",
                "neutralize_feedback_effect",
            }
        ):
            raise ValueError("target disposition command ref is invalid")
    if tuple(target_ids) != FEEDBACK_TARGETS:
        raise ValueError("target dispositions are incomplete")
