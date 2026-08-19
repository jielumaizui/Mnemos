"""Read-only reconstruction of active and pending prior feedback effects."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from core.cognitive.state_contract import CognitiveStateRevision
from core.cognitive.state_store import CognitiveStateStore


_NEUTRALIZATION_OUTCOMES = frozenset({"suppressed", "revoked", "compensated"})
_ACTIVE_EFFECT_OUTCOMES = frozenset({"proposal_committed", "committed_effect"})


@dataclass(frozen=True)
class PriorFeedbackState:
    """Current unneutralized effects and never-executed commands for one chain."""

    active_effects_by_target: Mapping[str, Mapping[str, Any]]
    pending_commands: tuple[Mapping[str, Any], ...]


def inspect_prior_feedback_state(
    state: CognitiveStateStore,
    current_attribution: CognitiveStateRevision | None,
) -> PriorFeedbackState:
    """Independently rebuild live effect state across the full attribution chain."""

    if current_attribution is None:
        return PriorFeedbackState(MappingProxyType({}), ())
    chain = state.revision_chain(
        "feedback_attribution_record",
        current_attribution.object_id,
    )
    receipts = tuple(
        receipt
        for revision in chain
        for receipt in state.effect_receipts_for_revision(revision.revision_id)
    )
    neutralized_receipt_ids = {
        str(ref).removeprefix("feedback-prior-effect:")
        for receipt in receipts
        if receipt.get("consumption_outcome") in _NEUTRALIZATION_OUTCOMES
        for ref in receipt.get("evidence_refs", ())
        if str(ref).startswith("feedback-prior-effect:")
    }
    active: dict[str, Mapping[str, Any]] = {}
    for receipt in receipts:
        if (
            receipt.get("status") != "committed"
            or receipt.get("consumption_outcome") not in _ACTIVE_EFFECT_OUTCOMES
            or str(receipt["receipt_id"]) in neutralized_receipt_ids
        ):
            continue
        target_id = str(receipt["consumer_id"])
        if target_id in active:
            raise RuntimeError(
                "multiple active feedback effects require explicit reconciliation"
            )
        active[target_id] = receipt
    receipt_command_ids = {str(receipt["command_id"]) for receipt in receipts}
    pending = tuple(
        command
        for revision in chain
        for command in state.commands_for_revision(revision.revision_id)
        if str(command["command_id"]) not in receipt_command_ids
    )
    return PriorFeedbackState(MappingProxyType(active), pending)
