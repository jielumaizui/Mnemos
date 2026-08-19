"""Immutable DTOs and adapter protocol for canonical feedback attribution."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import json
import re
from typing import Any, Mapping, Protocol


def _unavailable_entity_ref() -> dict[str, str]:
    return {
        "state": "unavailable",
        "id": "",
        "revision_id": "",
        "content_hash": "",
        "unavailable_reason": "not_applicable",
    }


def _unavailable_delivery_ref() -> dict[str, str]:
    return {
        "state": "unavailable",
        "event_id": "",
        "event_payload_hash": "",
        "unavailable_reason": "not_applicable",
    }


def _unavailable_display_ref() -> dict[str, str]:
    return {
        "state": "unavailable",
        "display_id": "",
        "content_hash": "",
        "unavailable_reason": "not_applicable",
    }


def _unavailable_search_ref() -> dict[str, str]:
    return {
        "state": "unavailable",
        "session_id": "",
        "result_id": "",
        "exposure_id": "",
        "unavailable_reason": "not_a_search_interaction",
    }


@dataclass(frozen=True)
class UserReactionInput:
    """Observed facts accepted at the feedback attribution seam."""

    source_event_id: str
    source_revision_id: str
    source_content_hash: str
    observed_at: str
    scope_type: str
    scope_id: str
    source_channel: str
    subject_ref: Mapping[str, Any]
    kind: str
    evidence_refs: tuple[str, ...]
    evidence_content_hashes: tuple[str, ...]
    access_control: Mapping[str, Any]
    delivery_ref: Mapping[str, Any] = field(default_factory=_unavailable_delivery_ref)
    display_ref: Mapping[str, Any] = field(default_factory=_unavailable_display_ref)
    search_ref: Mapping[str, Any] = field(default_factory=_unavailable_search_ref)
    decision_ref: Mapping[str, Any] = field(default_factory=_unavailable_entity_ref)
    prediction_ref: Mapping[str, Any] = field(default_factory=_unavailable_entity_ref)
    action_ref: Mapping[str, Any] = field(default_factory=_unavailable_entity_ref)
    exposure_id: str = ""
    interface_id: str = ""
    was_visible: bool = True
    observed_value: Any = True
    supersedes_event_id: str = ""
    correction_of_event_id: str = ""
    correction_target_ref: str = ""
    correction_reason: str = ""


@dataclass(frozen=True)
class ReactionReceipt:
    """Stable receipt for one reaction and attribution revision."""

    status: str
    event_id: str
    reaction_id: str
    reaction_revision_id: str
    attribution_id: str
    attribution_revision_id: str
    command_ids: tuple[str, ...]
    disposition: str


@dataclass(frozen=True)
class CognitiveEntityReference:
    """Typed identity of one decision or action evidence object."""

    id: str
    revision_id: str
    content_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "revision_id": self.revision_id,
            "content_hash": self.content_hash,
        }


_COGNITIVE_ENTITY_REF_KINDS = frozenset({"decision_trace", "action"})
_SHA256_REF = re.compile(r"sha256:[0-9a-f]{64}\Z")


def feedback_entity_evidence_ref(
    kind: str,
    reference: CognitiveEntityReference,
) -> str:
    """Encode one typed cognitive entity reference as a durable evidence ref."""

    normalized_kind = str(kind or "").strip()
    if normalized_kind not in _COGNITIVE_ENTITY_REF_KINDS:
        raise ValueError("feedback cognitive entity reference kind is invalid")
    if (
        not reference.id
        or not reference.revision_id
        or not _SHA256_REF.fullmatch(reference.content_hash)
    ):
        raise ValueError("feedback cognitive entity reference is incomplete")
    raw = json.dumps(
        reference.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"feedback-{normalized_kind}-ref:{encoded}"


def parse_feedback_entity_evidence_ref(
    value: str,
) -> tuple[str, CognitiveEntityReference] | None:
    """Decode a durable feedback DecisionTrace/action evidence reference."""

    normalized = str(value or "")
    for kind in sorted(_COGNITIVE_ENTITY_REF_KINDS):
        prefix = f"feedback-{kind}-ref:"
        if not normalized.startswith(prefix):
            continue
        encoded = normalized[len(prefix) :]
        if not encoded:
            raise ValueError("feedback cognitive entity evidence ref is empty")
        padding = "=" * (-len(encoded) % 4)
        try:
            payload = json.loads(
                base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
            )
        except (
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            binascii.Error,
        ) as exc:
            raise ValueError(
                "feedback cognitive entity evidence ref is invalid"
            ) from exc
        if not isinstance(payload, Mapping) or set(payload) != {
            "id",
            "revision_id",
            "content_hash",
        }:
            raise ValueError("feedback cognitive entity evidence ref is invalid")
        reference = CognitiveEntityReference(
            id=str(payload["id"]),
            revision_id=str(payload["revision_id"]),
            content_hash=str(payload["content_hash"]),
        )
        if (
            not reference.id
            or not reference.revision_id
            or not _SHA256_REF.fullmatch(reference.content_hash)
        ):
            raise ValueError("feedback cognitive entity evidence ref is incomplete")
        return kind, reference
    return None


@dataclass(frozen=True)
class CognitiveUpdateReceipt:
    """Public, deterministic view of one canonical feedback target closure."""

    schema_version: str
    command_id: str
    target_command_hash: str
    target_id: str
    attribution_revision_id: str
    attribution_payload_hash: str
    disposition: str
    effect_receipt_id: str
    target_effect_id: str
    before_hash: str
    after_hash: str
    decision_trace_refs: tuple[CognitiveEntityReference, ...]
    action_refs: tuple[CognitiveEntityReference, ...]
    reciprocal_receipt_refs: tuple[str, ...]
    superseded_effect_refs: tuple[str, ...]
    neutralized_effect_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "command_id": self.command_id,
            "target_command_hash": self.target_command_hash,
            "target_id": self.target_id,
            "attribution_revision_id": self.attribution_revision_id,
            "attribution_payload_hash": self.attribution_payload_hash,
            "disposition": self.disposition,
            "effect_receipt_id": self.effect_receipt_id,
            "target_effect_id": self.target_effect_id,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "decision_trace_refs": [
                item.to_dict() for item in self.decision_trace_refs
            ],
            "action_refs": [item.to_dict() for item in self.action_refs],
            "reciprocal_receipt_refs": list(self.reciprocal_receipt_refs),
            "superseded_effect_refs": list(self.superseded_effect_refs),
            "neutralized_effect_refs": list(self.neutralized_effect_refs),
        }


# Compatibility name for internal callers written before the public DTO landed.
TargetDispositionReceipt = CognitiveUpdateReceipt


@dataclass(frozen=True)
class ReplayBatchReceipt:
    """Bounded replay outcome containing only feedback commands."""

    processed_count: int
    page_count: int
    command_ids: tuple[str, ...]
    dispositions: tuple[str, ...]


@dataclass(frozen=True)
class FeedbackVerification:
    """Independent closure summary for one attribution revision."""

    status: str
    reaction_revision_id: str
    attribution_id: str
    attribution_revision_id: str
    verified_target_count: int
    pending_target_ids: tuple[str, ...]


@dataclass(frozen=True)
class AttributionReceipt:
    """Receipt for the current canonical feedback attribution state."""

    status: str
    attribution_id: str
    attribution_revision_id: str
    command_ids: tuple[str, ...]
    disposition: str
    training_admission_command_id: str = ""


@dataclass(frozen=True)
class FeedbackTargetEffect:
    """Reciprocal target-local proof returned by one domain adapter."""

    target_id: str
    target_effect_id: str
    disposition: str
    before_hash: str
    after_hash: str
    target_receipt_ref: str
    decision_trace_refs: tuple[CognitiveEntityReference, ...] = ()
    action_refs: tuple[CognitiveEntityReference, ...] = ()


@dataclass(frozen=True)
class FeedbackProposalTerminalProof:
    """Exact material terminal proof returned by a proposal gate."""

    effect_id: str
    before_hash: str
    after_hash: str


class FeedbackProposalGate(Protocol):
    """Authorize and seal one exact proposal material effect."""

    proposal: Mapping[str, Any]
    proposal_id: str
    material_command_id: str
    material_effect_id: str
    decision_trace_refs: tuple[CognitiveEntityReference, ...]
    action_refs: tuple[CognitiveEntityReference, ...]

    def terminal_proof(self) -> FeedbackProposalTerminalProof | None: ...

    def validate(self) -> None: ...

    def record_committed(
        self,
        *,
        before_hash: str,
        after_hash: str,
        target_receipt_ref: str,
        created_at: str,
    ) -> None: ...


class FeedbackProposalGateFactory(Protocol):
    """Build one domain-specific trusted proposal gate."""

    def __call__(
        self,
        *,
        database_dir: Any,
        target_id: str,
        owner_id: str,
        gate_contract_id: str,
        proposal: Mapping[str, Any],
    ) -> FeedbackProposalGate: ...


class FeedbackTargetAdapter(Protocol):
    """Apply, neutralize, and verify one domain-owned target effect."""

    def apply(self, command: Mapping[str, Any]) -> FeedbackTargetEffect: ...

    def neutralize(self, command: Mapping[str, Any]) -> FeedbackTargetEffect: ...

    def recover_command_effect(
        self,
        command: Mapping[str, Any],
    ) -> FeedbackTargetEffect | None: ...

    def inspect_command_effect(
        self,
        command: Mapping[str, Any],
    ) -> FeedbackTargetEffect | None: ...

    def verify(self, effect: FeedbackTargetEffect) -> bool: ...

    def verify_command_effect(
        self,
        command: Mapping[str, Any],
        effect: FeedbackTargetEffect,
    ) -> bool: ...
