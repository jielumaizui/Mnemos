"""Retired legacy scorer-feedback persistence boundary.

Historical ``ground_truth_signals``, ``scorer_training_queue``, and
``scorer_feedback_events`` rows are inventory-only after COG-048.  Canonical
training admission is owned by ``TrainingGovernanceStore`` and no compatibility
writer is permitted here.
"""

from __future__ import annotations

from typing import Any


LEGACY_TRAINING_ERROR = "training_admission_receipt_required"


def upsert_ground_truth_with_provenance(*args: Any, **kwargs: Any) -> tuple[str, bool]:
    """Reject the retired caller-labelled ground-truth writer."""

    del args, kwargs
    raise PermissionError(f"{LEGACY_TRAINING_ERROR}:ground_truth_persistence")


def persist_identified_feedback(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Reject the retired reaction-to-training queue bridge."""

    del args, kwargs
    raise PermissionError(f"{LEGACY_TRAINING_ERROR}:identified_feedback")
