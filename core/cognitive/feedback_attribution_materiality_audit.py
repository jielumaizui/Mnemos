"""Independent weak-feedback materiality recomputation for COG-038 audit."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


def independent_weak_materiality_valid(
    attribution: Mapping[str, Any],
    reactions_by_revision: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Recompute the 3-event, 2-identity, 24-hour weak proposal threshold."""

    refs = attribution.get("reaction_refs")
    materiality = attribution.get("materiality")
    if not isinstance(refs, list) or not isinstance(materiality, Mapping):
        return False
    selected: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        if not isinstance(ref, Mapping):
            return False
        revision_id = str(ref.get("revision_id") or "")
        row = reactions_by_revision.get(revision_id)
        if not revision_id or revision_id in seen or row is None:
            return False
        if str(ref.get("payload_hash") or "") != str(row.get("payload_hash") or ""):
            return False
        reaction = row.get("payload")
        if not isinstance(reaction, Mapping):
            return False
        reaction_attribution = reaction.get("attribution")
        if (
            not isinstance(reaction_attribution, Mapping)
            or reaction_attribution.get("evidence_class") != "weak_behavior"
        ):
            return False
        selected.append(reaction)
        seen.add(revision_id)
    if len(selected) < 3:
        return False
    try:
        observed = sorted(
            datetime.fromisoformat(
                str(item["observed_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            for item in selected
        )
    except (KeyError, TypeError, ValueError):
        return False
    sessions = {
        str(item.get("exposure", {}).get("session_id") or "")
        for item in selected
    }
    exposures = {
        str(item.get("exposure", {}).get("exposure_id") or "")
        for item in selected
    }
    if "" in exposures:
        return False
    span_seconds = int((observed[-1] - observed[0]).total_seconds())
    independence_keys = sorted(
        "session:"
        + str(item["exposure"]["session_id"])
        + "|exposure:"
        + str(item["exposure"]["exposure_id"])
        for item in selected
    )
    return bool(
        max(len(sessions), len(exposures)) >= 2
        and span_seconds >= 86400
        and materiality.get("observation_count") == len(selected)
        and materiality.get("distinct_session_count") == len(sessions)
        and materiality.get("distinct_exposure_count") == len(exposures)
        and materiality.get("span_seconds") == span_seconds
        and materiality.get("minimum_event_count") == 3
        and materiality.get("minimum_independence_count") == 2
        and materiality.get("minimum_span_seconds") == 86400
        and materiality.get("conflict_state") == "clear"
        and attribution.get("independence_keys") == independence_keys
    )
