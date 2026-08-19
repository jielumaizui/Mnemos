"""Canonical-resolution checks for feedback reaction causal references."""

from __future__ import annotations

from typing import Any, Mapping

from core.cognitive.state_contract import sha256_json


def validate_reaction_causal_refs(state: Any, payload: Mapping[str, Any]) -> None:
    """Require every available reaction entity ref to resolve exactly."""

    decision = _available_revision(
        state,
        payload["decision_ref"],
        object_type="decision_trace",
        field_name="decision_ref",
    )
    prediction = _available_revision(
        state,
        payload["prediction_ref"],
        object_type="prediction_record",
        field_name="prediction_ref",
    )
    if prediction is not None:
        _validate_prediction_access(prediction, payload)
    action_ref = payload["action_ref"]
    if action_ref["state"] == "available":
        if decision is None or action_ref["revision_id"] != decision.revision_id:
            raise ValueError("reaction action_ref lacks its exact decision revision")
        matches = [
            dict(item)
            for item in decision.payload.get("action_specs") or ()
            if isinstance(item, Mapping) and item.get("action_id") == action_ref["id"]
        ]
        if len(matches) != 1 or sha256_json(matches[0]) != action_ref["content_hash"]:
            raise ValueError("reaction action_ref does not resolve canonically")
    if prediction is not None and decision is not None:
        prediction_decision = prediction.payload.get("decision_ref")
        if not isinstance(prediction_decision, Mapping) or (
            prediction_decision.get("decision_id") != decision.object_id
            or prediction_decision.get("revision_id") != decision.revision_id
            or prediction_decision.get("revision_hash") != decision.payload_hash
        ):
            raise ValueError("reaction prediction_ref does not bind its decision_ref")
    if prediction is not None and action_ref["state"] == "available":
        prediction_action = prediction.payload.get("action_ref")
        if (
            not isinstance(prediction_action, Mapping)
            or prediction_action.get("action_id") != action_ref["id"]
        ):
            raise ValueError("reaction prediction_ref does not bind its action_ref")


def _available_revision(
    state: Any,
    ref: Mapping[str, Any],
    *,
    object_type: str,
    field_name: str,
) -> Any | None:
    if ref["state"] != "available":
        return None
    revision = state.revision(str(ref["revision_id"]))
    if (
        revision is None
        or revision.object_type != object_type
        or revision.object_id != ref["id"]
        or revision.payload_hash != ref["content_hash"]
    ):
        raise ValueError(f"reaction {field_name} does not resolve canonically")
    return revision


def _validate_prediction_access(prediction: Any, payload: Mapping[str, Any]) -> None:
    access = prediction.payload.get("access_control")
    if not isinstance(access, Mapping):
        raise ValueError("reaction prediction_ref lacks canonical access binding")
    owner = access.get("owner")
    scope = access.get("scope")
    reaction_owner = payload["principal_ref"]
    reaction_scope = payload["scope"]
    if (
        not isinstance(owner, Mapping)
        or not isinstance(scope, Mapping)
        or owner.get("principal_id") != reaction_owner["principal_id"]
        or str(owner.get("agent") or "").lower()
        != str(reaction_owner["agent"]).lower()
        or str(scope.get("project") or "").lower()
        != str(reaction_scope["project"]).lower()
        or str(scope.get("session_id") or "") != str(reaction_scope["session_id"])
    ):
        raise ValueError("reaction prediction_ref access binding is invalid")
