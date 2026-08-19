"""Pure identity and payload builders for canonical feedback reactions."""

from __future__ import annotations

from typing import Any, Mapping

from core.access_policy import PrincipalEnvelope
from core.cognitive.feedback_contract import (
    EXPLICIT_REACTION_KINDS,
    FEEDBACK_TARGET_REGISTRY_VERSION,
    FEEDBACK_TARGETS,
    reaction_input_hash,
)
from core.cognitive.feedback_models import UserReactionInput
from core.cognitive.state_contract import sha256_json
from core.evidence.artifact_catalog import sha256_file


REACTION_BUILDER_CODE_HASH = "sha256:" + sha256_file(__file__)

_CORRECTION_KINDS = frozenset({"inaccurate", "outdated"})
_SUBJECT_BOUND_CHANNELS = frozenset(
    {
        "reflection",
        "dialog_decision_push",
        "dialog_reminder",
        "retrospective",
    }
)
_FACT_BY_KIND = {
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


def build_reaction_id(
    reaction: UserReactionInput,
    principal: PrincipalEnvelope,
) -> str:
    """Derive one stable reaction-chain identity from authenticated bindings."""

    delivery_id = str(reaction.delivery_ref.get("event_id") or "")
    search_exposure = str(reaction.search_ref.get("exposure_id") or "")
    subject_binding = (
        f"{reaction.subject_ref.get('type')}:{reaction.subject_ref.get('id')}"
        if reaction.source_channel in _SUBJECT_BOUND_CHANNELS
        else ""
    )
    binding = (
        delivery_id
        or search_exposure
        or subject_binding
        or reaction.source_event_id
    )
    identity = {
        "principal_id": principal.principal_id,
        "scope_type": reaction.scope_type,
        "scope_id": reaction.scope_id,
        "subject_ref": dict(reaction.subject_ref),
        "source_channel": reaction.source_channel,
        "binding": binding,
    }
    return "reaction-" + str(sha256_json(identity)).split(":", 1)[1][:32]


def build_reaction_payload(
    reaction: UserReactionInput,
    *,
    principal: PrincipalEnvelope,
    access: Mapping[str, Any],
    reaction_id: str,
    recorded_at: str,
    has_current: bool,
    attribution_code_hash: str,
    attribution_spec_hash: str,
) -> dict[str, Any]:
    """Build the immutable canonical reaction payload without state access."""

    evidence_class = (
        "explicit_correction"
        if reaction.kind in _CORRECTION_KINDS
        else "explicit_preference"
        if reaction.kind in EXPLICIT_REACTION_KINDS
        else "weak_behavior"
    )
    disposition = (
        "correction_pending"
        if reaction.kind in _CORRECTION_KINDS
        else "record_only"
    )
    eligible_targets = (
        list(FEEDBACK_TARGETS) if disposition == "correction_pending" else []
    )
    exclusions = [
        {"target_id": target_id, "reason": "single_reaction_record_only"}
        for target_id in FEEDBACK_TARGETS
        if target_id not in eligible_targets
    ]
    correction = (
        {
            "state": "requested",
            "target_ref": reaction.correction_target_ref,
            "reason": reaction.correction_reason,
        }
        if reaction.kind in _CORRECTION_KINDS
        else {"state": "none", "target_ref": "", "reason": ""}
    )
    scope = access["scope"]
    payload: dict[str, Any] = {
        "schema_version": "mnemos.user_reaction_event.v1",
        "reaction_id": reaction_id,
        "revision_state": "corrected" if has_current else "recorded",
        "reaction_input_hash": "",
        "source_event_ref": {
            "event_id": reaction.source_event_id,
            "source_revision_id": reaction.source_revision_id,
            "content_hash": reaction.source_content_hash,
        },
        "observed_at": reaction.observed_at,
        "recorded_at": recorded_at,
        "supersedes_event_id": reaction.supersedes_event_id,
        "correction_of_event_id": reaction.correction_of_event_id,
        "principal_ref": {
            "principal_id": principal.principal_id,
            "agent": principal.agent,
            "authorization_ref": "feedback-authz:"
            + str(
                sha256_json(
                    {
                        "principal_id": principal.principal_id,
                        "source_event_id": reaction.source_event_id,
                        "scope_type": reaction.scope_type,
                        "scope_id": reaction.scope_id,
                    }
                )
            ).split(":", 1)[1][:32],
        },
        "scope": {
            "type": reaction.scope_type,
            "id": reaction.scope_id,
            "project": str(scope["project"]),
            "session_id": str(scope["session_id"]),
        },
        "source_channel": reaction.source_channel,
        "authority_class": (
            "explicit_user"
            if reaction.kind in EXPLICIT_REACTION_KINDS
            else "tool_observation"
        ),
        "subject_ref": dict(reaction.subject_ref),
        "decision_ref": dict(reaction.decision_ref),
        "prediction_ref": dict(reaction.prediction_ref),
        "action_ref": dict(reaction.action_ref),
        "delivery_ref": dict(reaction.delivery_ref),
        "display_ref": dict(reaction.display_ref),
        "search_ref": dict(reaction.search_ref),
        "interaction": {
            "kind": reaction.kind,
            "observed_facts": [
                {
                    "name": _FACT_BY_KIND.get(reaction.kind, reaction.kind),
                    "value": reaction.observed_value,
                }
            ],
        },
        "evidence": {
            "refs": list(reaction.evidence_refs),
            "content_hashes": list(reaction.evidence_content_hashes),
        },
        "observation_window": {
            "starts_at": reaction.observed_at,
            "ends_at": reaction.observed_at,
            "status": "closed",
        },
        "exposure": {
            "session_id": str(scope["session_id"]),
            "exposure_id": reaction.exposure_id,
            "interface_id": reaction.interface_id,
            "was_visible": reaction.was_visible,
        },
        "competing_causes": [],
        "source_completeness": {"state": "complete", "missing_refs": []},
        "attribution": {
            "method": "conservative_observation",
            "version": "v1",
            "code_hash": attribution_code_hash,
            "spec_hash": attribution_spec_hash,
            "disposition": disposition,
            "evidence_class": evidence_class,
        },
        "downstream": {
            "registry_version": FEEDBACK_TARGET_REGISTRY_VERSION,
            "required_targets": list(FEEDBACK_TARGETS),
            "eligible_targets": eligible_targets,
            "exclusions": exclusions,
        },
        "correction": correction,
        "access_control": dict(access),
    }
    payload["reaction_input_hash"] = reaction_input_hash(payload)
    return payload
