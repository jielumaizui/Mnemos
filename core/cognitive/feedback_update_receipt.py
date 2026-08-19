"""Assemble the public feedback update DTO from canonical persisted owners."""

from __future__ import annotations

from typing import Any, Mapping

from core.cognitive.feedback_models import (
    CognitiveEntityReference,
    CognitiveUpdateReceipt,
    parse_feedback_entity_evidence_ref,
)


RECEIPT_SCHEMA_VERSION = "mnemos.feedback_cognitive_update_receipt.v1"


def build_cognitive_update_receipt(
    state: Any,
    command_id: str,
    *,
    command: Mapping[str, Any] | None = None,
    effect: Any = None,
    attribution: Any = None,
    disposition: str = "",
) -> CognitiveUpdateReceipt:
    """Re-read and combine the command, attribution, and reciprocal receipt."""

    command = command or state.command(str(command_id or ""))
    effect = effect or state.effect_receipt(str(command_id or ""))
    if command is None or effect is None:
        raise ValueError("feedback command lacks a canonical terminal receipt")
    attribution = attribution or state.revision(str(command["revision_id"]))
    effect_status = str(_effect_value(effect, "status") or "")
    failed_terminal = effect_status == "failed_terminal"
    if (
        attribution is None
        or attribution.object_type != "feedback_attribution_record"
        or (
            not failed_terminal
            and attribution.payload_hash
            != str(command["payload"].get("attribution_payload_hash") or "")
        )
    ):
        raise ValueError("feedback update receipt attribution binding mismatch")
    terminal_disposition = str(disposition or "") or _terminal_disposition(effect)
    evidence_refs = tuple(
        str(ref) for ref in (_effect_value(effect, "evidence_refs") or ())
    )
    if terminal_disposition == "proposal_committed":
        decision_refs, action_refs = _material_entity_refs(evidence_refs)
    elif terminal_disposition == "committed_effect":
        decision_refs = _reaction_entity_refs(state, attribution, "decision_ref")
        action_refs = _reaction_entity_refs(state, attribution, "action_ref")
    else:
        decision_refs = ()
        action_refs = ()
    if terminal_disposition in {"committed_effect", "proposal_committed"} and not (
        decision_refs and action_refs
    ):
        raise ValueError("material feedback effect lacks DecisionTrace and action refs")
    reciprocal_refs = tuple(
        ref
        for ref in evidence_refs
        if ref.startswith("domain-feedback-receipt:")
    )
    if terminal_disposition in {
        "committed_effect",
        "proposal_committed",
        "suppressed",
        "revoked",
        "compensated",
    } and len(reciprocal_refs) != 1:
        raise ValueError("feedback update receipt lacks exact reciprocal domain ref")
    payload = dict(command["payload"])
    superseded = tuple(
        value
        for value in (
            _ref("cognitive-effect-receipt", payload.get("prior_effect_receipt_id")),
            _ref("feedback-target-effect", payload.get("prior_target_effect_id")),
            _ref("feedback-command", payload.get("prior_command_id")),
        )
        if value
    )
    neutralized = (
        (
            "cognitive-effect-receipt:"
            + str(_effect_value(effect, "receipt_id")),
            "feedback-target-effect:"
            + str(_effect_value(effect, "target_effect_id")),
        )
        if terminal_disposition in {"suppressed", "revoked", "compensated"}
        else ()
    )
    return CognitiveUpdateReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        command_id=str(command["command_id"]),
        target_command_hash=str(command["payload_hash"]),
        target_id=str(command["consumer_id"]),
        attribution_revision_id=attribution.revision_id,
        attribution_payload_hash=attribution.payload_hash,
        disposition=terminal_disposition,
        effect_receipt_id=str(_effect_value(effect, "receipt_id")),
        target_effect_id=str(_effect_value(effect, "target_effect_id")),
        before_hash=str(_effect_value(effect, "before_hash")),
        after_hash=str(_effect_value(effect, "after_hash")),
        decision_trace_refs=decision_refs,
        action_refs=action_refs,
        reciprocal_receipt_refs=reciprocal_refs,
        superseded_effect_refs=superseded,
        neutralized_effect_refs=neutralized,
    )


def build_ineligible_cognitive_update_receipt(
    command: Mapping[str, Any],
    closed: Mapping[str, Any],
) -> CognitiveUpdateReceipt:
    """Build one skip DTO from an already transactionally verified batch row."""

    payload = dict(command.get("payload") or {})
    return CognitiveUpdateReceipt(
        schema_version=RECEIPT_SCHEMA_VERSION,
        command_id=str(closed["command_id"]),
        target_command_hash=str(command["payload_hash"]),
        target_id=str(closed["target_id"]),
        attribution_revision_id=str(closed["attribution_revision_id"]),
        attribution_payload_hash=str(payload["attribution_payload_hash"]),
        disposition="intentional_skip",
        effect_receipt_id=str(closed["effect_receipt_id"]),
        target_effect_id=str(closed["target_effect_id"]),
        before_hash=str(closed["before_hash"]),
        after_hash=str(closed["after_hash"]),
        decision_trace_refs=(),
        action_refs=(),
        reciprocal_receipt_refs=(),
        superseded_effect_refs=(),
        neutralized_effect_refs=(),
    )


def _terminal_disposition(effect: Any) -> str:
    status = str(_effect_value(effect, "status") or "")
    if status == "intentional_skip":
        return "intentional_skip"
    if status == "failed_terminal":
        return "failed_terminal"
    outcome = str(_effect_value(effect, "consumption_outcome") or "")
    if outcome not in {
        "committed_effect",
        "proposal_committed",
        "suppressed",
        "revoked",
        "compensated",
    }:
        raise ValueError("feedback update receipt terminal disposition is invalid")
    return outcome


def _reaction_entity_refs(
    state: Any,
    attribution: Any,
    field_name: str,
) -> tuple[CognitiveEntityReference, ...]:
    refs: dict[tuple[str, str, str], CognitiveEntityReference] = {}
    for reaction_ref in attribution.payload.get("reaction_refs") or ():
        revision = state.revision(str(reaction_ref.get("revision_id") or ""))
        if revision is None:
            continue
        value = revision.payload.get(field_name)
        if not isinstance(value, Mapping) or value.get("state") != "available":
            continue
        identity = (
            str(value.get("id") or ""),
            str(value.get("revision_id") or ""),
            str(value.get("content_hash") or ""),
        )
        if all(identity):
            refs[identity] = CognitiveEntityReference(*identity)
    return tuple(refs[key] for key in sorted(refs))


def _material_entity_refs(
    evidence_refs: tuple[str, ...],
) -> tuple[
    tuple[CognitiveEntityReference, ...],
    tuple[CognitiveEntityReference, ...],
]:
    decisions: dict[tuple[str, str, str], CognitiveEntityReference] = {}
    actions: dict[tuple[str, str, str], CognitiveEntityReference] = {}
    for evidence_ref in evidence_refs:
        parsed = parse_feedback_entity_evidence_ref(evidence_ref)
        if parsed is None:
            continue
        kind, reference = parsed
        identity = (reference.id, reference.revision_id, reference.content_hash)
        (decisions if kind == "decision_trace" else actions)[identity] = reference
    return (
        tuple(decisions[key] for key in sorted(decisions)),
        tuple(actions[key] for key in sorted(actions)),
    )


def _ref(kind: str, value: Any) -> str:
    normalized = str(value or "").strip()
    return f"{kind}:{normalized}" if normalized else ""


def _effect_value(effect: Any, field_name: str) -> Any:
    if isinstance(effect, Mapping):
        return effect.get(field_name)
    return getattr(effect, field_name, None)
