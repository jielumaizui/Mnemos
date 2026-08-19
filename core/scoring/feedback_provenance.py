"""Scorer-owned boundaries for quarantined legacy feedback provenance."""

from __future__ import annotations

from pathlib import Path


QUARANTINED_FEEDBACK_QUEUE_SQL = """
    (session_id LIKE 'feedback-%' OR
     COALESCE(json_extract(features_json, '$.source'), '') IN
     ('push_feedback','search_click','search_ignore','dialog_reminder',
      'reflection_feedback','delivery_feedback'))
"""
QUARANTINED_FEEDBACK_GROUND_TRUTH_SQL = """
    (signal_type IN ('search_click','search_ignore') OR session_id LIKE 'feedback-%')
"""


def build_training_feedback_proposal_owner(database_dir: Path):
    """Return the scorer-owned evidence proposal journal without admitting labels."""

    from core.cognitive.feedback_target_registry import (
        build_registered_feedback_proposal_owner,
    )

    return build_registered_feedback_proposal_owner(database_dir, "training_evidence")
