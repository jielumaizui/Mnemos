from __future__ import annotations

import pytest

from core.access_policy import PrincipalEnvelope
from core.cognitive.feedback_attribution import UserReactionInput
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore
from tests.unit.cognitive.feedback_attribution_fixtures import access_control


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="user:feedback-test",
        agent="mnemos",
        host_kind="test",
        capability_id="feedback-test",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _reaction() -> UserReactionInput:
    return UserReactionInput(
        source_event_id="outcome-compat-reaction-1",
        source_revision_id="raw-outcome-compat-reaction-1",
        source_content_hash="sha256:" + "1" * 64,
        observed_at="2026-07-18T00:00:00+00:00",
        scope_type="session",
        scope_id="session-feedback",
        source_channel="outcome_compatibility",
        subject_ref={"type": "delivery", "id": "delivery-1"},
        kind="accept",
        evidence_refs=("raw-event:feedback#0:8",),
        evidence_content_hashes=("sha256:" + "2" * 64,),
        access_control=access_control(),
        delivery_ref={
            "state": "available",
            "event_id": "delivery-1",
            "event_payload_hash": "sha256:" + "3" * 64,
            "unavailable_reason": "",
        },
        display_ref={
            "state": "available",
            "display_id": "display-1",
            "content_hash": "sha256:" + "4" * 64,
            "unavailable_reason": "",
        },
        exposure_id="display-1",
        interface_id="compatibility-test",
    )


def test_ambiguous_outcome_recorder_signature_fails_closed_without_fanout(
    tmp_path,
):
    from core.app.outcome_recorder import OutcomeRecorder

    recorder = OutcomeRecorder(database_dir=tmp_path)

    with pytest.raises(RuntimeError, match="ambiguous_outcome_recorder_signature_retired"):
        recorder.record_outcome(
            source="push_feedback",
            subject="docker",
            action="accept",
            dimension="profile",
        )

    assert list(tmp_path.iterdir()) == []


def test_typed_reaction_delegates_once_without_label_or_domain_writes(tmp_path):
    from core.app.outcome_recorder import OutcomeRecorder

    state_db = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    recorder = OutcomeRecorder(state_db=state_db)

    first = recorder.record_reaction(_reaction(), principal=_principal())
    replay = recorder.record_reaction(_reaction(), principal=_principal())

    assert first["success"] is True
    assert first["derived_label"] is None
    assert first["direct_domain_updates"] == 0
    assert replay["status"] == "existing"
    state = CognitiveStateStore(state_db)
    assert len(state.current_revisions(object_type="user_reaction_event")) == 1
    assert len(state.current_revisions(object_type="feedback_attribution_record")) == 1
    assert not (tmp_path / "mnemos.db").exists()
    assert not (tmp_path / "reflections.db").exists()
    assert not (tmp_path / "rule_weight_optimizer.db").exists()
    assert not (tmp_path / "delivery_events.db").exists()
