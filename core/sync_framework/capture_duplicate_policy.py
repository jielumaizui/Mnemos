"""The one canonical idempotency contract for Capture queue work."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


POLICY_VERSION = "mnemos.capture_duplicate_policy.v1"


class CaptureDuplicatePolicyError(ValueError):
    """Raised when a caller cannot prove a canonical Capture identity."""


@dataclass(frozen=True)
class CaptureIdempotencyKey:
    """A permanent idempotency receipt identity for one Raw revision."""

    source_agent: str
    raw_revision_id: str
    replay_generation: int
    value: str


class CaptureDuplicatePolicy:
    """Map one canonical Raw revision to exactly one capture generation.

    Queue payload retention is deliberately absent from this calculation.  A
    retry of the same canonical Raw revision is a duplicate forever; a
    requested downstream replay must choose a positive, explicit generation.
    """

    @staticmethod
    def build(
        *,
        source_agent: str,
        raw_revision_id: str,
        replay_generation: int = 0,
    ) -> CaptureIdempotencyKey:
        source = str(source_agent or "").strip()
        revision = str(raw_revision_id or "").strip()
        if not source:
            raise CaptureDuplicatePolicyError("source_agent is required")
        if not revision:
            raise CaptureDuplicatePolicyError("canonical raw_revision_id is required")
        if isinstance(replay_generation, bool) or not isinstance(replay_generation, int):
            raise CaptureDuplicatePolicyError("replay_generation must be an integer")
        if replay_generation < 0:
            raise CaptureDuplicatePolicyError("replay_generation must be non-negative")
        material = f"{POLICY_VERSION}\0{source}\0{revision}\0{replay_generation}"
        value = "capture-idem-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]
        return CaptureIdempotencyKey(source, revision, replay_generation, value)

    @staticmethod
    def require_explicit_replay_generation(replay_generation: int) -> None:
        """Reject a replay request that could masquerade as normal capture."""
        if isinstance(replay_generation, bool) or not isinstance(replay_generation, int):
            raise CaptureDuplicatePolicyError("replay_generation must be an integer")
        if replay_generation <= 0:
            raise CaptureDuplicatePolicyError(
                "an explicit replay requires replay_generation >= 1"
            )
