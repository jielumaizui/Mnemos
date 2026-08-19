"""Immutable materiality and configuration identity for feedback attribution."""

from __future__ import annotations

from core.cognitive.feedback_contract import FEEDBACK_TARGET_REGISTRY_HASH
from core.cognitive.state_contract import sha256_json


FEEDBACK_MATERIALITY_VERSION = "mnemos.feedback_materiality.v1"
FEEDBACK_ATTRIBUTION_METHOD = "conservative_feedback_attribution"
FEEDBACK_ATTRIBUTION_SPEC = {
    "version": "v1",
    "materiality_version": FEEDBACK_MATERIALITY_VERSION,
    "weak_minimum_event_count": 3,
    "weak_minimum_independence_count": 2,
    "weak_minimum_span_seconds": 86400,
    "weak_proposal_mode": "record_only_due_to_source_authority",
    "objective_source": "mnemos.outcome_measurement.v1",
    "training_mode": "proposal_only_until_cog048",
}
FEEDBACK_ATTRIBUTION_SPEC_HASH = sha256_json(FEEDBACK_ATTRIBUTION_SPEC)
FEEDBACK_ATTRIBUTION_CONFIG_HASH = sha256_json(
    {
        "materiality": FEEDBACK_ATTRIBUTION_SPEC,
        "target_registry_hash": FEEDBACK_TARGET_REGISTRY_HASH,
    }
)
