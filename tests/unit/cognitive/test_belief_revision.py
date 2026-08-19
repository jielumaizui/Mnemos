from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import sqlite3

import pytest

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore

_NOW = "2026-07-17T08:00:00+00:00"


def _principal(principal_id: str = "principal:belief-owner") -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id=principal_id,
        agent="codex",
        host_kind="test",
        capability_id="belief-test",
        capabilities=frozenset({"memory_write"}),
        allowed_projects=frozenset({"mnemos", "other"}),
        allowed_source_agents=frozenset({"codex"}),
    )


def _access(
    source_id: str,
    *,
    project: str = "mnemos",
    principal_id: str = "principal:belief-owner",
) -> dict[str, object]:
    return make_cognitive_access_envelope(
        owner_principal_id=principal_id,
        owner_agent="codex",
        scope_type="project",
        scope_id=project,
        project=project,
        purposes=("belief_read", "cognitive_state_write"),
        consent_provenance_refs=(source_id,),
        sensitivity="sensitive",
        retention_policy="cognitive_state",
        source_acl_lineage=("sha256:" + ("a" if project == "mnemos" else "b") * 64,),
    )


def _command(
    *,
    source_id: str,
    supporting: tuple[str, ...] = (),
    opposing: tuple[str, ...] = (),
    project: str = "mnemos",
    valid_until: str = "",
    expected_current_revision_id: str = "",
    correction_of_revision_id: str = "",
    withdrawn: tuple[str, ...] = (),
    correction_evidence_ref: str = "",
    confidence_method: str = "unscored",
    confidence: float | None = None,
    confidence_evidence: tuple[str, ...] = (),
    source_span_ids: tuple[str, ...] | None = None,
):
    from core.cognitive.belief_revision import BeliefRevisionCommand

    source_revision_id = f"revision:{source_id}"
    return BeliefRevisionCommand(
        claim="SQLite backups remain until their retention expiry.",
        claim_kind="fact",
        scope_type="project",
        scope_id=project,
        source_id=source_id,
        source_revision_id=source_revision_id,
        source_content_hash="sha256:" + source_id[-1] * 64,
        source_access_control=_access(source_id, project=project),
        source_span_ids=(
            source_span_ids if source_span_ids is not None else (f"{source_revision_id}#0:52",)
        ),
        supporting_evidence=supporting,
        opposing_evidence=opposing,
        withdrawn_evidence=withdrawn,
        confidence_method=confidence_method,
        confidence=confidence,
        confidence_evidence=confidence_evidence,
        valid_from=_NOW,
        valid_until=valid_until,
        invalidation_conditions=("retention policy changes",),
        expected_current_revision_id=expected_current_revision_id,
        correction_of_revision_id=correction_of_revision_id,
        correction_evidence_ref=correction_evidence_ref,
        created_at=_NOW,
    )


def _belief_store(tmp_path):
    from core.cognitive.belief_revision import BeliefRevisionStore

    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    return BeliefRevisionStore(CognitiveStateStore(db_path))


def test_first_revision_and_exact_replay_use_one_canonical_head(tmp_path):
    store = _belief_store(tmp_path)
    command = _command(source_id="source:1", supporting=("evidence:support:1",))

    first = store.revise(command, principal=_principal())
    replay = store.revise(command, principal=_principal())

    assert first.status == "committed"
    assert replay.status == "existing"
    assert replay.revision_id == first.revision_id
    explanation = store.explain(
        first.belief_id,
        principal=_principal(),
        narrowing=AccessNarrowing(project="mnemos"),
        now=datetime.fromisoformat(_NOW),
    )
    assert explanation.status == "ok"
    assert explanation.active is True
    assert explanation.stance == "supported"
    assert explanation.supporting_evidence == ("evidence:support:1",)
    assert explanation.opposing_evidence == ()
    assert explanation.scope == ("project", "mnemos")
    assert explanation.revision_lineage == (first.revision_id,)
    with sqlite3.connect(store.state_store.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_state_revisions "
                "WHERE object_type='belief_revision'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_state_heads " "WHERE object_type='belief_revision'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_state_outbox "
                "WHERE command_type='project_belief_revision'"
            ).fetchone()[0]
            == 1
        )


def test_belief_revision_rejects_span_from_another_source_revision(tmp_path):
    store = _belief_store(tmp_path)

    with pytest.raises(ValueError, match="exact spans of source_revision_id"):
        store.revise(
            _command(
                source_id="source:1",
                source_span_ids=("revision:source:other#0:52",),
            ),
            principal=_principal(),
        )


def test_opposing_evidence_creates_disputed_revision_without_silent_overwrite(tmp_path):
    store = _belief_store(tmp_path)
    first = store.revise(
        _command(source_id="source:1", supporting=("evidence:support:1",)),
        principal=_principal(),
    )
    disputed = store.revise(
        _command(
            source_id="source:2",
            opposing=("evidence:oppose:1",),
            expected_current_revision_id=first.revision_id,
        ),
        principal=_principal(),
    )

    explanation = store.explain(
        disputed.belief_id,
        principal=_principal(),
        narrowing=AccessNarrowing(project="mnemos"),
    )
    assert disputed.revision_id != first.revision_id
    assert explanation.stance == "disputed"
    assert explanation.supporting_evidence == ("evidence:support:1",)
    assert explanation.opposing_evidence == ("evidence:oppose:1",)
    assert explanation.revision_lineage == (first.revision_id, disputed.revision_id)
    assert explanation.current_revision_id == disputed.revision_id


def test_same_claim_forks_by_scope_and_private_acl_blocks_other_principal(tmp_path):
    store = _belief_store(tmp_path)
    mnemos = store.revise(
        _command(source_id="source:1", supporting=("evidence:mnemos",)),
        principal=_principal(),
    )
    other = store.revise(
        _command(
            source_id="source:2",
            project="other",
            supporting=("evidence:other",),
        ),
        principal=_principal(),
    )

    assert mnemos.belief_id != other.belief_id
    denied = store.explain(
        mnemos.belief_id,
        principal=_principal("principal:not-owner"),
        narrowing=AccessNarrowing(project="mnemos"),
    )
    wrong_scope = store.explain(
        mnemos.belief_id,
        principal=_principal(),
        narrowing=AccessNarrowing(project="other"),
    )
    assert denied.status == "access_denied"
    assert wrong_scope.status == "access_denied"
    assert "SQLite backups" not in denied.claim
    assert "SQLite backups" not in wrong_scope.claim


def test_expiry_exits_active_retrieval_without_marking_revision_false(tmp_path):
    store = _belief_store(tmp_path)
    expired_at = datetime.fromisoformat(_NOW) + timedelta(minutes=1)
    receipt = store.revise(
        _command(
            source_id="source:1",
            supporting=("evidence:support:1",),
            valid_until=expired_at.isoformat(),
        ),
        principal=_principal(),
    )

    explanation = store.explain(
        receipt.belief_id,
        principal=_principal(),
        narrowing=AccessNarrowing(project="mnemos"),
        now=expired_at + timedelta(seconds=1),
    )
    active = store.list_active(
        principal=_principal(),
        narrowing=AccessNarrowing(project="mnemos"),
        now=expired_at + timedelta(seconds=1),
    )
    assert explanation.status == "ok"
    assert explanation.active is False
    assert explanation.inactive_reason == "expired"
    assert explanation.stance == "supported"
    assert active == ()


def test_authoritative_correction_withdraws_evidence_but_preserves_lineage(tmp_path):
    store = _belief_store(tmp_path)
    first = store.revise(
        _command(source_id="source:1", supporting=("evidence:support:1",)),
        principal=_principal(),
    )
    corrected = store.revise(
        _command(
            source_id="source:2",
            expected_current_revision_id=first.revision_id,
            correction_of_revision_id=first.revision_id,
            withdrawn=("evidence:support:1",),
            correction_evidence_ref="source:2",
        ),
        principal=_principal(),
    )

    explanation = store.explain(
        corrected.belief_id,
        principal=_principal(),
        narrowing=AccessNarrowing(project="mnemos"),
    )
    assert explanation.stance == "unknown"
    assert explanation.active is False
    assert explanation.inactive_reason == "unknown"
    assert explanation.supporting_evidence == ()
    assert explanation.withdrawn_evidence == ("evidence:support:1",)
    assert explanation.revision_lineage == (first.revision_id, corrected.revision_id)


def test_correction_evidence_must_be_bound_by_the_authorized_source_acl(tmp_path):
    store = _belief_store(tmp_path)
    first = store.revise(
        _command(source_id="source:1", supporting=("evidence:support:1",)),
        principal=_principal(),
    )

    with pytest.raises(ValueError, match="source ACL"):
        store.revise(
            _command(
                source_id="source:2",
                expected_current_revision_id=first.revision_id,
                correction_of_revision_id=first.revision_id,
                withdrawn=("evidence:support:1",),
                correction_evidence_ref="unbound:assertion",
            ),
            principal=_principal(),
        )

    assert (
        store.state_store.current_revision("belief_revision", first.belief_id).revision_id
        == first.revision_id
    )


def test_stale_expected_head_fails_without_partial_event_or_outbox(tmp_path):
    store = _belief_store(tmp_path)
    first = store.revise(
        _command(source_id="source:1", supporting=("evidence:support:1",)),
        principal=_principal(),
    )
    store.revise(
        _command(
            source_id="source:2",
            supporting=("evidence:support:2",),
            expected_current_revision_id=first.revision_id,
        ),
        principal=_principal(),
    )
    stale = _command(
        source_id="source:3",
        opposing=("evidence:oppose:1",),
        expected_current_revision_id=first.revision_id,
    )

    with pytest.raises(RuntimeError, match="expected current revision"):
        store.revise(stale, principal=_principal())

    with sqlite3.connect(store.state_store.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_state_revisions "
                "WHERE object_type='belief_revision'"
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_data_events " "WHERE data_type='belief_revision'"
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_state_outbox "
                "WHERE command_type='project_belief_revision'"
            ).fetchone()[0]
            == 2
        )


def test_caller_cannot_submit_stance_or_belief_identity(tmp_path):
    store = _belief_store(tmp_path)
    command = _command(source_id="source:1", supporting=("evidence:support:1",))

    with pytest.raises(TypeError):
        replace(command, stance="supported")
    with pytest.raises(TypeError):
        replace(command, belief_id="caller-controlled")
    assert (
        store.list_active(
            principal=_principal(),
            narrowing=AccessNarrowing(project="mnemos"),
        )
        == ()
    )


def test_scored_confidence_requires_evidence_and_preserves_numeric_zero(tmp_path):
    store = _belief_store(tmp_path)

    with pytest.raises(ValueError, match="confidence evidence"):
        store.revise(
            _command(
                source_id="source:1",
                supporting=("evidence:support:1",),
                confidence_method="frequency_v1",
                confidence=0.5,
            ),
            principal=_principal(),
        )

    receipt = store.revise(
        _command(
            source_id="source:2",
            opposing=("evidence:oppose:1",),
            confidence_method="frequency_v1",
            confidence=0.0,
            confidence_evidence=("measurement:denominator:1",),
        ),
        principal=_principal(),
    )
    explanation = store.explain(
        receipt.belief_id,
        principal=_principal(),
        narrowing=AccessNarrowing(project="mnemos"),
        now=datetime.fromisoformat(_NOW),
    )
    assert explanation.confidence_method == "frequency_v1"
    assert explanation.confidence == 0.0
    assert "confidence_not_measured" not in explanation.uncertainty["reasons"]


@pytest.mark.parametrize("boundary", ("after_revision", "after_event", "after_outbox"))
def test_unit_of_work_failpoints_roll_back_every_canonical_row(tmp_path, boundary):
    store = _belief_store(tmp_path)

    def failpoint(name: str) -> None:
        if name == boundary:
            raise RuntimeError(f"fault:{boundary}")

    with pytest.raises(RuntimeError, match=f"fault:{boundary}"):
        store.revise(
            _command(source_id="source:1", supporting=("evidence:support:1",)),
            principal=_principal(),
            _failpoint=failpoint,
        )

    with sqlite3.connect(store.state_store.db_path) as conn:
        for table in (
            "cognitive_state_revisions",
            "cognitive_state_heads",
            "cognitive_data_events",
            "cognitive_state_outbox",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
