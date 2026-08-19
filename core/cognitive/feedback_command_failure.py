"""Deterministic, independently reproducible feedback command failures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from core.cognitive.feedback_contract import FEEDBACK_TARGETS
from core.cognitive.feedback_models import CognitiveUpdateReceipt
from core.cognitive.state_contract import sha256_json
from core.cognitive.feedback_target_execution import inspect_existing_domain_effect


@dataclass(frozen=True)
class FeedbackCommandFailureProof:
    """Proof that a command is structurally impossible to execute."""

    command_id: str
    attribution_revision_id: str
    reason_code: str
    proof_hash: str


def derive_feedback_command_failure(
    state: Any,
    command_id: str,
) -> FeedbackCommandFailureProof | None:
    """Return a proof only for immutable envelope/attribution corruption."""

    command = state.command(str(command_id or ""))
    if command is None:
        return None
    payload = command.get("payload")
    reason = _deterministic_failure_reason(state, command, payload)
    if not reason:
        return None
    revision_id = str(command.get("revision_id") or "")
    attribution = state.revision(revision_id)
    identity = {
        "schema_version": "mnemos.feedback_command_failure.v1",
        "command_id": str(command.get("command_id") or ""),
        "command_type": str(command.get("command_type") or ""),
        "consumer_id": str(command.get("consumer_id") or ""),
        "revision_id": revision_id,
        "payload_hash": str(command.get("payload_hash") or ""),
        "attribution_payload_hash": (
            "" if attribution is None else attribution.payload_hash
        ),
        "reason_code": reason,
    }
    return FeedbackCommandFailureProof(
        command_id=str(command["command_id"]),
        attribution_revision_id=revision_id,
        reason_code=reason,
        proof_hash=sha256_json(identity),
    )


def _record_permanent_feedback_failure(
    owner: Any,
    command_id: str,
    error: Exception,
) -> CognitiveUpdateReceipt:
    """Close only an owner-caught, independently reproducible structural failure."""

    from core.cognitive.feedback_update_receipt import (
        build_cognitive_update_receipt,
    )

    if not owner._feedback_failure_context_matches(command_id):
        raise PermissionError(
            "feedback permanent failure requires active owner processing context"
        ) from error
    command = owner.state.command(command_id)
    if command is None:
        raise ValueError("feedback target command does not exist") from error
    proof = derive_feedback_command_failure(owner.state, command_id)
    if proof is None:
        raise error
    existing_effect = inspect_existing_domain_effect(owner.state, command_id)
    if existing_effect is not None:
        raise RuntimeError(
            "feedback structural failure conflicts with an existing domain effect"
        ) from error
    effect = owner.state._record_feedback_terminal_failure(
        command_id,
        proof=proof,
        created_at=owner._clock(),
    )
    attribution = owner.state.revision(str(command["revision_id"]))
    return build_cognitive_update_receipt(
        owner.state,
        command_id,
        command=command,
        effect=effect,
        attribution=attribution,
        disposition="failed_terminal",
    )


def _deterministic_failure_reason(
    state: Any,
    command: Mapping[str, Any],
    payload: Any,
) -> str:
    command_type = str(command.get("command_type") or "")
    target_id = str(command.get("consumer_id") or "")
    revision_id = str(command.get("revision_id") or "")
    if command_type not in {
        "evaluate_feedback_target",
        "neutralize_feedback_effect",
    }:
        return "unsupported_command_type"
    if target_id not in FEEDBACK_TARGETS:
        return "unregistered_target"
    if not isinstance(payload, Mapping):
        return "payload_not_mapping"
    if payload.get("target_id") != target_id:
        return "target_binding_mismatch"
    attribution = state.revision(revision_id)
    if attribution is None or attribution.object_type != "feedback_attribution_record":
        return "attribution_missing"
    if payload.get("attribution_revision_id") != revision_id:
        return "attribution_binding_mismatch"
    if command_type == "neutralize_feedback_effect":
        if (
            payload.get("schema_version")
            != "mnemos.feedback_neutralization_command.v1"
            or payload.get("neutralization_kind")
            not in {"suppress", "revoke", "compensate"}
        ):
            return "neutralization_contract_mismatch"
        return ""
    if tuple(payload.get("required_target_ids") or ()) != FEEDBACK_TARGETS:
        return "target_registry_mismatch"
    if attribution.payload.get("input_set_hash") != payload.get("input_set_hash"):
        return "input_set_hash_mismatch"
    rows = [
        item
        for item in attribution.payload.get("target_dispositions") or ()
        if isinstance(item, Mapping) and item.get("target_id") == target_id
    ]
    if len(rows) != 1:
        return "target_disposition_missing"
    row = rows[0]
    if (
        row.get("eligible") != payload.get("eligible")
        or row.get("exclusion_reason") != payload.get("exclusion_reason")
        or row.get("command_ref", {}).get("command_key")
        != payload.get("command_key")
    ):
        return "target_disposition_binding_mismatch"
    return ""
