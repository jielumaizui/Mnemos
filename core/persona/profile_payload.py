"""Rendering for the ACL-filtered user cognitive-profile payload."""

from __future__ import annotations

from typing import Any, Dict, List


def clamp_confidence(value: Any) -> float:
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.0


def build_profile_v2_payload(
    assertions: List[Dict[str, Any]],
    *,
    active_signal_count: int | None = None,
) -> Dict[str, Any]:
    """Render only ACL-filtered profile assertions."""

    source = "profile_assertions" if assertions else "none"

    buckets: Dict[str, List[Dict[str, Any]]] = {
        "persona_claims": [],
        "behavior_signals": [],
        "intent_patterns": [],
        "decision_preferences": [],
        "judgment_standards": [],
        "interaction_contracts": [],
        "risk_boundaries": [],
        "negative_feedback": [],
        "current_goal_state": [],
        "cognitive_flywheel_inputs": [],
    }
    dimension_to_bucket = {
        "persona_claim": "persona_claims",
        "behavior_signal": "behavior_signals",
        "behavior_pattern": "behavior_signals",
        "intent_pattern": "intent_patterns",
        "decision_preference": "decision_preferences",
        "judgment_standard": "judgment_standards",
        "interaction_contract": "interaction_contracts",
        "risk_boundary": "risk_boundaries",
        "negative_feedback": "negative_feedback",
        "current_goal_state": "current_goal_state",
        "cognitive_flywheel_input": "cognitive_flywheel_inputs",
        "reflection_feedback_decision_style": "decision_preferences",
        "reflection_interest": "current_goal_state",
        "cognitive_shift": "cognitive_flywheel_inputs",
    }

    evidence_refs: List[str] = []
    confidence_values: List[float] = []
    for assertion in assertions:
        entry = {
            "assertion_id": assertion.get("assertion_id", ""),
            "dimension": assertion.get("dimension", ""),
            "claim": assertion.get("claim", ""),
            "confidence": clamp_confidence(assertion.get("confidence", 0.0)),
            "privacy_level": assertion.get("privacy_level", "local"),
            "evidence_refs": assertion.get("evidence_refs", []),
            "revision_policy": assertion.get("revision_policy", "revise_on_contradiction"),
            "last_verified_at": assertion.get("last_verified_at", ""),
            "status": assertion.get("status", "active"),
        }
        bucket = dimension_to_bucket.get(str(assertion.get("dimension", "")), "persona_claims")
        buckets[bucket].append(entry)
        evidence_refs.extend(str(ref) for ref in entry["evidence_refs"])
        confidence_values.append(entry["confidence"])

    confidence = (
        round(sum(confidence_values) / len(confidence_values), 3) if confidence_values else 0.0
    )
    return {
        "schema_version": "mnemos.user_cognitive_profile.v2",
        "status": "active" if assertions else "empty",
        "profile_assertions": assertions,
        "profile_signals": {
            "active_count": (
                active_signal_count if active_signal_count is not None else len(assertions)
            ),
            "source": source,
        },
        "confidence": confidence,
        "evidence_refs": sorted(set(evidence_refs)),
        **buckets,
    }
