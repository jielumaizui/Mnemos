from __future__ import annotations

from datetime import datetime, timedelta
import sqlite3

import pytest

from core.access_policy import PrincipalEnvelope
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.belief_revision import (
    BeliefRevisionCommand,
    BeliefRevisionProjector,
    BeliefRevisionStore,
)
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive_graph.store import CognitiveGraphStore

NOW = "2026-07-17T08:00:00+00:00"


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="principal:belief-owner",
        agent="codex",
        host_kind="test",
        capability_id="belief-projection-test",
        capabilities=frozenset({"memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
        allowed_source_agents=frozenset({"codex"}),
    )


def _command(
    source_no: int,
    *,
    supporting: tuple[str, ...] = (),
    opposing: tuple[str, ...] = (),
    expected: str = "",
    valid_from: str = NOW,
    valid_until: str = "",
) -> BeliefRevisionCommand:
    source_id = f"source:{source_no}"
    access = make_cognitive_access_envelope(
        owner_principal_id="principal:belief-owner",
        owner_agent="codex",
        scope_type="project",
        scope_id="mnemos",
        project="mnemos",
        purposes=("belief_read", "cognitive_state_write"),
        consent_provenance_refs=(source_id,),
        sensitivity="sensitive",
        retention_policy="cognitive_state",
        source_acl_lineage=("sha256:" + str(source_no) * 64,),
    )
    return BeliefRevisionCommand(
        claim="SQLite backups remain until their retention expiry.",
        claim_kind="fact",
        scope_type="project",
        scope_id="mnemos",
        source_id=source_id,
        source_revision_id=f"revision:{source_no}",
        source_content_hash="sha256:" + str(source_no) * 64,
        source_access_control=access,
        supporting_evidence=supporting,
        opposing_evidence=opposing,
        invalidation_conditions=("retention policy changes",),
        valid_from=valid_from,
        valid_until=valid_until,
        expected_current_revision_id=expected,
        created_at=NOW,
    )


def _stores(tmp_path):
    state_path = tmp_path / "producer_consumer_ledger.db"
    graph_path = tmp_path / "cognitive_graph.db"
    initialize_cognitive_state_schema(state_path)
    state = CognitiveStateStore(state_path)
    graph = CognitiveGraphStore(str(graph_path))
    return BeliefRevisionStore(state), BeliefRevisionProjector(state, graph), graph


def test_projection_preserves_history_and_has_one_non_stale_current_head(tmp_path):
    beliefs, projector, graph = _stores(tmp_path)
    first = beliefs.revise(
        _command(1, supporting=("evidence:support:1",)),
        principal=_principal(),
    )
    second = beliefs.revise(
        _command(
            2,
            opposing=("evidence:oppose:1",),
            expected=first.revision_id,
        ),
        principal=_principal(),
    )

    result = projector.process_pending(now=datetime.fromisoformat(NOW))

    assert result == {"committed": 2, "failed": 0, "pending": 0}
    assert projector.projection_hash(second.belief_id).startswith("sha256:")
    with sqlite3.connect(graph.db_path) as conn:
        current = conn.execute(
            "SELECT target FROM cognitive_relations "
            "WHERE source=? AND relation_type='current_belief_revision' AND stale=0",
            (f"belief://{second.belief_id}",),
        ).fetchall()
        assert current == [(f"belief-revision://{second.revision_id}",)]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_relations "
                "WHERE source=? AND relation_type='current_belief_revision'",
                (f"belief://{second.belief_id}",),
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_relations "
                "WHERE source=? AND target=? "
                "AND relation_type='supersedes_belief_revision' AND stale=0",
                (
                    f"belief-revision://{second.revision_id}",
                    f"belief-revision://{first.revision_id}",
                ),
            ).fetchone()[0]
            == 1
        )
        node_count = conn.execute(
            "SELECT COUNT(*) FROM canonical_nodes WHERE source_ids LIKE '%belief%'"
        ).fetchone()[0]
        assert node_count >= 3


def test_projection_crash_before_receipt_stays_pending_and_retry_is_idempotent(tmp_path):
    beliefs, projector, graph = _stores(tmp_path)
    receipt = beliefs.revise(
        _command(1, supporting=("evidence:support:1",)),
        principal=_principal(),
    )

    def failpoint(name: str) -> None:
        if name == "after_projection":
            raise RuntimeError("fault:after_projection")

    with pytest.raises(RuntimeError, match="fault:after_projection"):
        projector.process_command(receipt.command_id, _failpoint=failpoint)
    projected_hash = projector.projection_hash(receipt.belief_id)
    assert len(beliefs.state_store.pending_commands("cognitive_graph")) == 1

    effect = projector.process_command(receipt.command_id)

    assert effect.status == "committed"
    assert effect.target_effect_id == receipt.projection_effect_id
    assert projector.projection_hash(receipt.belief_id) == projected_hash
    assert beliefs.state_store.pending_commands("cognitive_graph") == []
    with sqlite3.connect(graph.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_relations "
                "WHERE source=? AND relation_type='current_belief_revision'",
                (f"belief://{receipt.belief_id}",),
            ).fetchone()[0]
            == 1
        )


def test_generic_receipt_api_cannot_forge_a_belief_projection_effect(tmp_path):
    beliefs, _, _ = _stores(tmp_path)
    receipt = beliefs.revise(
        _command(1, supporting=("evidence:support:1",)),
        principal=_principal(),
    )

    with pytest.raises(ValueError, match="belief projection"):
        beliefs.state_store.record_effect_receipt(
            receipt.command_id,
            status="committed",
            target_effect_id="forged-effect",
            before_hash="sha256:" + "a" * 64,
            after_hash="sha256:" + "b" * 64,
            evidence_refs=("forged:evidence",),
        )

    assert len(beliefs.state_store.pending_commands("cognitive_graph")) == 1


def test_expiry_reconciliation_suppresses_projected_head_without_refuting_it(tmp_path):
    beliefs, projector, graph = _stores(tmp_path)
    expires = datetime.fromisoformat(NOW) + timedelta(minutes=1)
    receipt = beliefs.revise(
        _command(
            1,
            supporting=("evidence:support:1",),
            valid_until=expires.isoformat(),
        ),
        principal=_principal(),
    )
    projector.process_pending(now=datetime.fromisoformat(NOW))

    assert projector.suppress_inactive_heads(now=expires + timedelta(seconds=1)) == 1

    with sqlite3.connect(graph.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_relations "
                "WHERE source=? AND relation_type='current_belief_revision' AND stale=0",
                (f"belief://{receipt.belief_id}",),
            ).fetchone()[0]
            == 0
        )
    revision = beliefs.state_store.revision(receipt.revision_id)
    assert revision is not None
    assert revision.payload["stance"] == "supported"


def test_validity_reconciliation_activates_a_projected_not_yet_valid_head(tmp_path):
    beliefs, projector, graph = _stores(tmp_path)
    begins = datetime.fromisoformat(NOW) + timedelta(minutes=1)
    receipt = beliefs.revise(
        _command(
            1,
            supporting=("evidence:support:1",),
            valid_from=begins.isoformat(),
        ),
        principal=_principal(),
    )
    projector.process_pending(now=datetime.fromisoformat(NOW))
    with sqlite3.connect(graph.db_path) as conn:
        assert (
            conn.execute(
                "SELECT stale FROM cognitive_relations "
                "WHERE source=? AND relation_type='current_belief_revision'",
                (f"belief://{receipt.belief_id}",),
            ).fetchone()[0]
            == 1
        )

    result = projector.reconcile_validity(now=begins + timedelta(seconds=1))

    assert result == {"activated": 1, "suppressed": 0}
    with sqlite3.connect(graph.db_path) as conn:
        assert (
            conn.execute(
                "SELECT stale FROM cognitive_relations "
                "WHERE source=? AND relation_type='current_belief_revision'",
                (f"belief://{receipt.belief_id}",),
            ).fetchone()[0]
            == 0
        )
