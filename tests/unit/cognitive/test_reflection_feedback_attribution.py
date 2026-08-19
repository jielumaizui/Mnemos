from __future__ import annotations

import pytest

from core.access_policy import PrincipalEnvelope
from core.cognitive.feedback_entrypoints import record_reflection_feedback
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore
from core.reflection.reflection_store import REFLECTION_OBJECT_PURPOSES
from core.cognitive.access_control import make_cognitive_access_envelope


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="user:reflection-feedback",
        agent="codex",
        host_kind="test",
        capability_id="reflection-feedback",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _access() -> dict:
    return make_cognitive_access_envelope(
        owner_principal_id=_principal().principal_id,
        owner_agent="codex",
        scope_type="session",
        scope_id="reflection-session",
        session_id="reflection-session",
        project="mnemos",
        purposes=REFLECTION_OBJECT_PURPOSES,
        consent_provenance_refs=("reflection:record-1",),
        sensitivity="sensitive",
        retention_policy="reflection_retention",
        source_acl_lineage=("sha256:" + "1" * 64,),
        visibility="private",
    )


def _record(tmp_path, feedback_type: str, **kwargs):
    return record_reflection_feedback(
        database_dir=tmp_path,
        reflection_id="reflection-1",
        feedback_type=feedback_type,
        comment="explicit user feedback",
        record_snapshot={
            "id": "reflection-1",
            "insight": {"summary": "bounded reflection snapshot"},
        },
        access_control=_access(),
        principal=_principal(),
        **kwargs,
    )


def test_reflection_feedback_records_one_canonical_reaction_and_no_legacy_row(
    tmp_path,
):
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")

    first = _record(tmp_path, "accurate")
    replay = _record(tmp_path, "accurate")

    assert first["success"] is True
    assert first["disposition"] == "record_only"
    assert replay["reaction_revision_id"] == first["reaction_revision_id"]
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    assert len(state.current_revisions(object_type="user_reaction_event")) == 1
    assert not (tmp_path / "reflections.db").exists()
    assert not (tmp_path / "trust_decisions.db").exists()
    assert not (tmp_path / "mnemos.db").exists()


def test_changed_reflection_feedback_requires_exact_latest_event(tmp_path):
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")
    first = _record(tmp_path, "accurate")

    with pytest.raises(ValueError, match="stale_reaction_supersedes"):
        _record(tmp_path, "irrelevant")

    changed = _record(
        tmp_path,
        "irrelevant",
        supersedes_event_id=first["feedback_event_id"],
    )
    assert changed["success"] is True


def test_initial_inaccurate_reflection_creates_proposals_not_domain_updates(tmp_path):
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")

    result = _record(tmp_path, "inaccurate")

    assert result["disposition"] == "proposal_eligible"
    assert result["terminal_receipt_count"] == 7
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    effects = state.effect_receipts_for_revision(result["attribution_revision_id"])
    assert {effect["consumption_outcome"] for effect in effects} == {
        "proposal_committed"
    }
    assert not state.current_revisions(object_type="outcome_measurement")
