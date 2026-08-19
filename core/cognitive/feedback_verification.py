"""Read-only exact closure verification for canonical feedback."""

from __future__ import annotations

from typing import Any

from core.access_policy import PrincipalEnvelope
from core.cognitive.access_control import (
    authorize_cognitive_write,
    validate_cognitive_access_envelope,
)
from core.cognitive.feedback_contract import FEEDBACK_TARGETS
from core.cognitive.feedback_identity import (
    attribution_principal_ref,
    feedback_attribution_id,
)
from core.cognitive.feedback_models import FeedbackVerification


def verify_feedback_attribution(
    state: Any,
    reaction_revision_id: str,
    principal: PrincipalEnvelope,
) -> FeedbackVerification:
    """Verify exact private reaction binding and its current target closure."""

    reaction = state.revision(str(reaction_revision_id or ""))
    if reaction is None or reaction.object_type != "user_reaction_event":
        raise ValueError("canonical reaction revision does not exist")
    access = validate_cognitive_access_envelope(
        reaction.payload["access_control"],
        expected_scope_type=reaction.scope_type,
        expected_scope_id=reaction.scope_id,
    )
    authorization = authorize_cognitive_write(
        access,
        principal=principal,
        scope_type=reaction.scope_type,
        scope_id=reaction.scope_id,
    )
    if not authorization.allowed:
        raise PermissionError(
            f"feedback verification access denied: {authorization.reason}"
        )
    attribution_id = feedback_attribution_id(
        subject_ref=reaction.payload["subject_ref"],
        scope_type=reaction.scope_type,
        scope_id=reaction.scope_id,
        principal_ref=attribution_principal_ref(access),
    )
    attribution = state.current_revision(
        "feedback_attribution_record",
        attribution_id,
    )
    matching_refs = (
        []
        if attribution is None
        else [
            ref
            for ref in attribution.payload["reaction_refs"]
            if ref.get("revision_id") == reaction.revision_id
            and ref.get("reaction_id") == reaction.object_id
            and ref.get("payload_hash") == reaction.payload_hash
        ]
    )
    if attribution is None or len(matching_refs) != 1:
        raise ValueError("reaction lacks its exact current attribution")
    commands = state.commands_for_revision(attribution.revision_id)
    command_targets = tuple(str(command["consumer_id"]) for command in commands)
    if (
        len(commands) != len(FEEDBACK_TARGETS)
        or len(set(command_targets)) != len(FEEDBACK_TARGETS)
        or set(command_targets) != set(FEEDBACK_TARGETS)
    ):
        raise ValueError("feedback attribution command registry mismatch")
    by_target = {
        str(command["consumer_id"]): command for command in commands
    }
    closed: set[str] = set()
    for target_id in FEEDBACK_TARGETS:
        try:
            state.validate_feedback_effect_receipt(
                str(by_target[target_id]["command_id"])
            )
        except (ValueError, KeyError, TypeError):
            continue
        closed.add(target_id)
    pending = tuple(target for target in FEEDBACK_TARGETS if target not in closed)
    return FeedbackVerification(
        status="verified_complete" if not pending else "verified_pending",
        reaction_revision_id=reaction.revision_id,
        attribution_id=attribution.object_id,
        attribution_revision_id=attribution.revision_id,
        verified_target_count=len(FEEDBACK_TARGETS) - len(pending),
        pending_target_ids=pending,
    )
