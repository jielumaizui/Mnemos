"""Shared immutable types for the canonical cognitive state store."""

from __future__ import annotations

from dataclasses import dataclass


class CognitiveStateConflict(RuntimeError):
    """A revision, head or idempotency key conflicts with immutable history."""


COGNITIVE_TOMBSTONE_COMMAND_TYPE = "tombstone_cognitive_state"
COGNITIVE_TOMBSTONE_SCHEMA_VERSION = "mnemos.cognitive_tombstone.v1"


@dataclass(frozen=True)
class CognitiveEffectReceipt:
    """Immutable terminal effect binding for one cognitive command."""

    receipt_id: str
    command_id: str
    revision_id: str
    event_id: str
    consumer_id: str
    consumption_id: str
    status: str
    target_effect_id: str
    before_hash: str
    after_hash: str
    evidence_refs: tuple[str, ...]
    reason_code: str
    retry_exhausted: bool
    created_at: str


@dataclass(frozen=True)
class CognitiveTombstonePlan:
    """Immutable deletion plan stored through the canonical state outbox."""

    status: str
    request_id: str
    subject_hash: str
    target_revision_ids: tuple[str, ...]
    control_revision_id: str
    command_ids: tuple[str, ...]
    required_consumers: tuple[str, ...]
    before_hash: str
    tombstone_hash: str
