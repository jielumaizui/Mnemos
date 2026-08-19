"""Independent private-owner audit for canonical feedback attributions."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, cast


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def audit_attribution_principals(
    attributions: list[dict[str, Any]],
    reactions: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    metrics: dict[str, int],
    denominators: dict[str, int],
) -> None:
    """Recompute private owner, identity, scope, and exact source bindings."""

    reactions_by_revision = {
        str(item["revision_id"]): item for item in reactions
    }
    outcomes_by_revision = {
        str(item["revision_id"]): item for item in outcomes
    }
    expected = 0
    verified = 0
    valid_reactions: set[str] = set()
    for reaction in reactions:
        expected += 1
        if _reaction_binding_valid(reaction):
            verified += 1
            valid_reactions.add(str(reaction["revision_id"]))
        else:
            metrics["attribution_principal_binding_gap"] += 1
    for attribution in attributions:
        payload = attribution["payload"]
        reaction_refs = payload.get("reaction_refs") or ()
        outcome_refs = payload.get("outcome_refs") or ()
        expected += 1 + len(reaction_refs) + len(outcome_refs)
        access = payload.get("access_control")
        owner = access.get("owner") if isinstance(access, Mapping) else None
        if not isinstance(owner, Mapping) or set(owner) != {"principal_id", "agent"}:
            metrics["attribution_principal_binding_gap"] += (
                1 + len(reaction_refs) + len(outcome_refs)
            )
            continue
        principal_ref = {
            "principal_id": str(owner.get("principal_id") or ""),
            "agent": str(owner.get("agent") or "").lower(),
        }
        identity = {
            "subject_ref": payload.get("subject_ref"),
            "scope_type": attribution.get("scope_type"),
            "scope_id": attribution.get("scope_id"),
            "principal_ref": principal_ref,
        }
        expected_object_id = "feedback-attribution-" + _sha256_json(
            identity
        ).split(":", 1)[1][:32]
        if (
            principal_ref["principal_id"]
            and principal_ref["agent"]
            and str(attribution.get("object_id")) == expected_object_id
        ):
            verified += 1
        else:
            metrics["attribution_principal_binding_gap"] += 1
        for ref in reaction_refs:
            bound_reaction = reactions_by_revision.get(
                str(ref.get("revision_id") or "")
            )
            if bound_reaction is None:
                metrics["attribution_principal_binding_gap"] += 1
                continue
            reaction_payload = bound_reaction["payload"]
            reaction_access = reaction_payload.get("access_control")
            reaction_owner = (
                reaction_access.get("owner")
                if isinstance(reaction_access, Mapping)
                else None
            )
            reaction_principal = reaction_payload.get("principal_ref")
            valid = (
                isinstance(reaction_owner, Mapping)
                and isinstance(reaction_principal, Mapping)
                and {
                    "principal_id": str(reaction_owner.get("principal_id") or ""),
                    "agent": str(reaction_owner.get("agent") or "").lower(),
                }
                == principal_ref
                and {
                    "principal_id": str(
                        reaction_principal.get("principal_id") or ""
                    ),
                    "agent": str(reaction_principal.get("agent") or "").lower(),
                }
                == principal_ref
                and bound_reaction.get("scope_type")
                == attribution.get("scope_type")
                and bound_reaction.get("scope_id") == attribution.get("scope_id")
                and str(ref.get("reaction_id") or "")
                == bound_reaction.get("object_id")
                and str(ref.get("payload_hash") or "")
                == bound_reaction.get("payload_hash")
                and str(bound_reaction.get("revision_id")) in valid_reactions
                and reaction_payload.get("subject_ref") == payload.get("subject_ref")
            )
            if valid:
                verified += 1
            else:
                metrics["attribution_principal_binding_gap"] += 1
        for ref in outcome_refs:
            outcome = outcomes_by_revision.get(str(ref.get("revision_id") or ""))
            if outcome is None:
                metrics["attribution_principal_binding_gap"] += 1
                continue
            outcome_payload = outcome["payload"]
            outcome_access = outcome_payload.get("access_control")
            outcome_owner = (
                outcome_access.get("owner")
                if isinstance(outcome_access, Mapping)
                else None
            )
            valid = (
                isinstance(outcome_owner, Mapping)
                and {
                    "principal_id": str(outcome_owner.get("principal_id") or ""),
                    "agent": str(outcome_owner.get("agent") or "").lower(),
                }
                == principal_ref
                and outcome.get("scope_type") == attribution.get("scope_type")
                and outcome.get("scope_id") == attribution.get("scope_id")
                and outcome_payload.get("subject") == payload.get("subject_ref")
                and str(ref.get("outcome_id") or "") == outcome.get("object_id")
                and str(ref.get("payload_hash") or "") == outcome.get("payload_hash")
                and _access_scope_matches_revision(outcome_access, outcome)
            )
            if valid:
                verified += 1
            else:
                metrics["attribution_principal_binding_gap"] += 1
    denominators["attribution_principal_binding_expected_count"] = expected
    denominators["attribution_principal_binding_verified_count"] = verified


def _reaction_binding_valid(reaction: Mapping[str, Any]) -> bool:
    payload = reaction.get("payload")
    if not isinstance(payload, Mapping):
        return False
    access = payload.get("access_control")
    owner = access.get("owner") if isinstance(access, Mapping) else None
    principal = payload.get("principal_ref")
    source = payload.get("source_event_ref")
    subject = payload.get("subject_ref")
    if not all(
        isinstance(item, Mapping)
        for item in (owner, principal, source, subject)
    ):
        return False
    owner = cast(Mapping[str, Any], owner)
    principal = cast(Mapping[str, Any], principal)
    source = cast(Mapping[str, Any], source)
    subject = cast(Mapping[str, Any], subject)
    owner_ref = {
        "principal_id": str(owner.get("principal_id") or ""),
        "agent": str(owner.get("agent") or "").lower(),
    }
    principal_ref = {
        "principal_id": str(principal.get("principal_id") or ""),
        "agent": str(principal.get("agent") or "").lower(),
    }
    source_channel = str(payload.get("source_channel") or "")
    delivery = payload.get("delivery_ref")
    search = payload.get("search_ref")
    delivery_id = (
        str(delivery.get("event_id") or "")
        if isinstance(delivery, Mapping)
        else ""
    )
    search_exposure = (
        str(search.get("exposure_id") or "")
        if isinstance(search, Mapping)
        else ""
    )
    subject_binding = (
        f"{subject.get('type')}:{subject.get('id')}"
        if source_channel
        in {
            "reflection",
            "dialog_decision_push",
            "dialog_reminder",
            "retrospective",
        }
        else ""
    )
    binding = (
        delivery_id
        or search_exposure
        or subject_binding
        or str(source.get("event_id") or "")
    )
    identity = {
        "principal_id": principal_ref["principal_id"],
        "scope_type": str(reaction.get("scope_type") or ""),
        "scope_id": str(reaction.get("scope_id") or ""),
        "subject_ref": dict(subject),
        "source_channel": source_channel,
        "binding": binding,
    }
    expected_id = "reaction-" + _sha256_json(identity).split(":", 1)[1][:32]
    return bool(
        principal_ref == owner_ref
        and owner_ref["principal_id"]
        and owner_ref["agent"]
        and reaction.get("object_id") == expected_id
        and payload.get("reaction_id") == expected_id
        and reaction.get("source_revision_id") == source.get("source_revision_id")
        and reaction.get("source_content_hash") == source.get("content_hash")
        and _access_scope_matches_revision(access, reaction)
    )


def _access_scope_matches_revision(
    access: Any,
    revision: Mapping[str, Any],
) -> bool:
    if not isinstance(access, Mapping):
        return False
    scope = access.get("scope")
    if not isinstance(scope, Mapping):
        return False
    return bool(
        str(scope.get("scope_type") or scope.get("type") or "")
        == str(revision.get("scope_type") or "")
        and str(scope.get("scope_id") or scope.get("id") or "")
        == str(revision.get("scope_id") or "")
    )
