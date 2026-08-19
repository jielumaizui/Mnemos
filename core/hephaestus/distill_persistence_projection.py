"""Prepare a privacy-safe action sink projection after immutable admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from core.hephaestus.distillation_models import DistillationResult, KnowledgeFragment
from core.hephaestus.raw_provenance import preflight_chunked_write_provenance
from core.privacy.content_redaction import redact_persistence_value


@dataclass(frozen=True)
class ActionSinkProjection:
    """Detached payload plus provenance remapped to its redacted claim objects."""

    payload: Mapping[str, Any]
    claims: tuple[Mapping[str, Any], ...]
    claim_page_refs: Mapping[int, tuple[dict[str, Any], ...]]


def build_action_sink_projection(
    result: DistillationResult,
    fragments: Sequence[KnowledgeFragment],
) -> ActionSinkProjection:
    """Prove original claims first, then redact only their durable projection."""

    admitted_payload = result.structured_output or {}
    admitted_claims = tuple(
        claim
        for claim in admitted_payload.get("claims", [])
        if isinstance(claim, Mapping)
    )
    admitted_refs = preflight_chunked_write_provenance(
        result,
        admitted_claims,
        fragments,
    )
    redacted = redact_persistence_value(admitted_payload).value
    payload = redacted if isinstance(redacted, Mapping) else {}
    claims = tuple(
        claim for claim in payload.get("claims", []) if isinstance(claim, Mapping)
    )
    if len(claims) != len(admitted_claims):
        raise ValueError("redacted claim projection changed the admitted claim count")
    return ActionSinkProjection(
        payload=payload,
        claims=claims,
        claim_page_refs={
            id(redacted_claim): admitted_refs.get(id(admitted_claim), ())
            for admitted_claim, redacted_claim in zip(admitted_claims, claims)
        },
    )
