"""Reflection-owned boundaries for quarantined legacy feedback provenance."""

from __future__ import annotations

from pathlib import Path


# Legacy feedback projections are identified by their retired object type.  A
# source_event_id is ordinary lineage for live Layer-5 objects and must not make
# an otherwise valid experience or shift disappear from active reads.
ACTIVE_LAYER5_EXPERIENCE_SQL = "type<>'outcome_feedback'"
ACTIVE_COGNITIVE_SHIFT_SQL = "shift_type<>'outcome_feedback'"


def build_reflection_feedback_proposal_owner(database_dir: Path):
    """Return the reflection-owned evidence proposal journal."""

    from core.cognitive.feedback_target_registry import (
        build_registered_feedback_proposal_owner,
    )

    return build_registered_feedback_proposal_owner(
        database_dir,
        "reflection_evidence",
    )
