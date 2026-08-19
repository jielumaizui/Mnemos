"""Bounded real-domain capacity proof for canonical feedback replay."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from core.cognitive.feedback_attribution import FeedbackAttributionStore
from core.cognitive.feedback_contract import FEEDBACK_TARGETS
from core.cognitive.feedback_proposal_gate import (
    build_gated_feedback_target_adapters,
)
from tests.unit.cognitive.test_feedback_attribution_store import (
    _insert_real_compensation_workload,
    _insert_real_reaction_proposal_workload,
    _principal,
    _reaction_input,
    _store,
)


def test_real_replay_converges_for_reaction_proposal_and_compensation_capacity(
    tmp_path: Path,
) -> None:
    # The approved gate is >10,000 real commands in every replay phase.
    count = 1_501
    bootstrap, state = _store(tmp_path)
    source = bootstrap.record_reaction(_reaction_input(), _principal())
    bootstrap.replay_pending(limit=100)
    feedback = FeedbackAttributionStore(
        state,
        clock=lambda: "2026-07-18T00:00:02+00:00",
        target_adapters=build_gated_feedback_target_adapters(tmp_path),
    )
    attributions, target_command_ids = _insert_real_reaction_proposal_workload(
        feedback,
        state,
        source_reaction_revision_id=source.reaction_revision_id,
        source_attribution_revision_id=source.attribution_revision_id,
        count=count,
    )

    page_limit = 137
    proposal_replay = feedback.replay_pending(limit=page_limit)

    assert count * len(FEEDBACK_TARGETS) > 10_000
    assert proposal_replay.processed_count == count * len(FEEDBACK_TARGETS)
    assert proposal_replay.page_count > 1
    assert len(proposal_replay.command_ids) == len(set(proposal_replay.command_ids))
    assert set(proposal_replay.command_ids) == set(target_command_ids)
    assert target_command_ids[0] in proposal_replay.command_ids
    assert target_command_ids[-1] in proposal_replay.command_ids
    assert proposal_replay.dispositions.count("proposal_committed") == count
    assert proposal_replay.dispositions.count("intentional_skip") == count * 6
    assert len(state.current_revisions(object_type="user_reaction_event")) == count + 1
    assert not state.pending_commands()

    compensation_command_ids = _insert_real_compensation_workload(
        state,
        prior_attributions=attributions,
    )
    compensation_replay = feedback.replay_pending(limit=page_limit)
    exact_replay = feedback.replay_pending(limit=page_limit)

    assert count * 14 > 10_000
    assert compensation_replay.processed_count == count * 14
    assert compensation_replay.page_count > 1
    assert len(compensation_replay.command_ids) == len(
        set(compensation_replay.command_ids)
    )
    assert set(compensation_command_ids).issubset(compensation_replay.command_ids)
    assert compensation_command_ids[0] in compensation_replay.command_ids
    assert compensation_command_ids[-1] in compensation_replay.command_ids
    assert compensation_replay.dispositions.count("suppressed") == count
    assert compensation_replay.dispositions.count("intentional_skip") == count * 13
    assert exact_replay.processed_count == 0
    assert exact_replay.page_count == 0
    assert not state.pending_commands()
    assert state.integrity_report()["effect_receipt_reciprocity_gap"] == 0
    with sqlite3.connect(tmp_path / "cognitive_graph.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM belief_feedback_proposals"
        ).fetchone()[0] == count
        assert conn.execute(
            "SELECT COUNT(*) FROM belief_feedback_proposal_actions"
        ).fetchone()[0] == count
        assert conn.execute(
            "SELECT COUNT(*) FROM belief_feedback_proposal_receipts"
        ).fetchone()[0] == count * 2
