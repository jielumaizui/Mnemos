from __future__ import annotations

from copy import deepcopy

import pytest

from core.cognitive.state_contract import CognitiveStateRevision, LocalConsumerCommand
from core.cognitive.feedback_contract import reaction_input_hash
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore
from core.ops.cognitive_data_contract import CognitiveDataEvent
from tests.unit.cognitive.feedback_attribution_fixtures import (
    attribution_payload,
    reaction_payload,
)


def _reaction_revision(*, kind: str = "accept") -> CognitiveStateRevision:
    return CognitiveStateRevision.create(
        object_type="user_reaction_event",
        object_id="reaction-" + "1" * 32,
        source_event_id="feedback-source-event",
        source_revision_id="raw-feedback-revision",
        source_content_hash="sha256:" + "2" * 64,
        scope_type="session",
        scope_id="session-feedback",
        evidence_refs=("raw-event:feedback#0:8",),
        payload=reaction_payload(kind=kind),
        created_at="2026-07-18T00:00:01+00:00",
    )


def test_unknown_reaction_kind_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported_reaction_kind"):
        _reaction_revision(kind="surprise_positive")


def test_reaction_fact_must_match_the_registered_kind() -> None:
    payload = deepcopy(reaction_payload())
    payload["interaction"] = {
        "kind": "ignore",
        "observed_facts": [{"name": "accepted", "value": True}],
    }

    with pytest.raises(ValueError, match="reaction observed facts do not match"):
        CognitiveStateRevision.create(
            object_type="user_reaction_event",
            object_id="reaction-" + "1" * 32,
            source_event_id="feedback-source-event",
            source_revision_id="raw-feedback-revision",
            source_content_hash="sha256:" + "2" * 64,
            scope_type="session",
            scope_id="session-feedback",
            evidence_refs=("raw-event:feedback#0:8",),
            payload=payload,
            created_at="2026-07-18T00:00:01+00:00",
        )


def test_skeletal_legacy_reaction_payload_is_rejected() -> None:
    payload = {
        "access_control": reaction_payload()["access_control"],
        "delivery_ref": {"event_id": "delivery-1"},
        "principal_ref": {"principal_id": "user:feedback-test"},
        "interaction": {
            "kind": "accept",
            "observed_facts": [{"name": "accepted", "value": True}],
        },
    }

    with pytest.raises(ValueError, match="schema_version"):
        CognitiveStateRevision.create(
            object_type="user_reaction_event",
            object_id="reaction-" + "1" * 32,
            source_event_id="feedback-source-event",
            source_revision_id="raw-feedback-revision",
            source_content_hash="sha256:" + "2" * 64,
            scope_type="session",
            scope_id="session-feedback",
            evidence_refs=("raw-event:feedback#0:8",),
            payload=payload,
        )


def test_reaction_input_hash_covers_observed_identity_and_evidence() -> None:
    payload = deepcopy(reaction_payload())
    payload["delivery_ref"]["event_id"] = "delivery-tampered"

    with pytest.raises(ValueError, match="reaction input hash mismatch"):
        CognitiveStateRevision.create(
            object_type="user_reaction_event",
            object_id="reaction-" + "1" * 32,
            source_event_id="feedback-source-event",
            source_revision_id="raw-feedback-revision",
            source_content_hash="sha256:" + "2" * 64,
            scope_type="session",
            scope_id="session-feedback",
            evidence_refs=("raw-event:feedback#0:8",),
            payload=payload,
        )


def test_canonical_state_accepts_a_complete_feedback_attribution_record() -> None:
    revision = CognitiveStateRevision.create(
        object_type="feedback_attribution_record",
        object_id="feedback-attribution-" + "d" * 32,
        source_event_id="feedback-source-event",
        source_revision_id="reaction-revision:" + "8" * 32,
        source_content_hash="sha256:" + "9" * 64,
        scope_type="session",
        scope_id="session-feedback",
        evidence_refs=("reaction-revision:" + "8" * 32,),
        payload=attribution_payload(),
        created_at="2026-07-18T00:00:01+00:00",
    )

    assert revision.schema_version == "mnemos.feedback_attribution_record.v1"


def test_attribution_requires_one_disposition_for_every_registered_target() -> None:
    payload = deepcopy(attribution_payload())
    payload["target_dispositions"].pop()

    with pytest.raises(ValueError, match="target dispositions are incomplete"):
        CognitiveStateRevision.create(
            object_type="feedback_attribution_record",
            object_id="feedback-attribution-" + "d" * 32,
            source_event_id="feedback-source-event",
            source_revision_id="reaction-revision:" + "8" * 32,
            source_content_hash="sha256:" + "9" * 64,
            scope_type="session",
            scope_id="session-feedback",
            evidence_refs=("reaction-revision:" + "8" * 32,),
            payload=payload,
        )


def test_single_weak_reaction_cannot_become_proposal_eligible() -> None:
    payload = deepcopy(attribution_payload())
    payload["evidence_class"] = "weak_behavior"
    payload["disposition"] = "proposal_eligible"
    payload["post_neutralization_disposition"] = "proposal_eligible"
    payload["materiality"]["decision"] = "proposal_eligible"

    with pytest.raises(ValueError, match="weak feedback materiality threshold"):
        CognitiveStateRevision.create(
            object_type="feedback_attribution_record",
            object_id="feedback-attribution-" + "d" * 32,
            source_event_id="feedback-source-event",
            source_revision_id="reaction-revision:" + "8" * 32,
            source_content_hash="sha256:" + "9" * 64,
            scope_type="session",
            scope_id="session-feedback",
            evidence_refs=("reaction-revision:" + "8" * 32,),
            payload=payload,
        )


def test_fresh_canonical_store_persists_feedback_attribution_head(tmp_path) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    revision = CognitiveStateRevision.create(
        object_type="feedback_attribution_record",
        object_id="feedback-attribution-" + "d" * 32,
        source_event_id="feedback-source-event",
        source_revision_id="reaction-revision:" + "8" * 32,
        source_content_hash="sha256:" + "9" * 64,
        scope_type="session",
        scope_id="session-feedback",
        evidence_refs=("reaction-revision:" + "8" * 32,),
        payload=attribution_payload(),
        created_at="2026-07-18T00:00:01+00:00",
    )
    event = CognitiveDataEvent(
        event_id="feedback-source-event",
        source_id="reaction-revision:" + "8" * 32,
        asset_id=revision.object_id,
        source_kind="feedback_attribution",
        source_uri="mnemos://feedback/attribution/" + revision.object_id,
        content_hash="sha256:" + "9" * 64,
        canonical_subject="feedback_attribution_record:" + revision.object_id,
        data_type="feedback_attribution_record",
        producer="feedback_attribution_store",
        intended_consumers=("feedback_attribution_audit",),
        privacy_level="private",
        confidence=1.0,
        evidence_refs=revision.evidence_refs,
        dedupe_key="feedback-attribution:" + revision.object_id,
        created_at=revision.created_at,
        retention_policy="cognitive_state",
        metadata={"revision_ids": [revision.revision_id]},
    )
    store = CognitiveStateStore(db_path)
    command = LocalConsumerCommand.create(
        revision_id=revision.revision_id,
        consumer_id="feedback_attribution_audit",
        command_type="verify_feedback_attribution",
        payload={"revision_id": revision.revision_id},
        created_at=revision.created_at,
    )

    receipt = store.unit_of_work().commit(
        revisions=(revision,),
        event=event,
        commands=(command,),
    )

    assert receipt.status == "committed"
    assert store.current_revision("feedback_attribution_record", revision.object_id) == revision


def test_explicit_correction_requires_exact_supersedes_and_target_refs() -> None:
    payload = deepcopy(reaction_payload(kind="inaccurate"))
    payload["interaction"]["observed_facts"] = [
        {"name": "inaccurate", "value": True}
    ]
    payload["reaction_input_hash"] = reaction_input_hash(payload)

    with pytest.raises(ValueError, match="reaction correction lineage is incomplete"):
        CognitiveStateRevision.create(
            object_type="user_reaction_event",
            object_id="reaction-" + "1" * 32,
            source_event_id="feedback-source-event",
            source_revision_id="raw-feedback-revision",
            source_content_hash="sha256:" + "2" * 64,
            scope_type="session",
            scope_id="session-feedback",
            evidence_refs=("raw-event:feedback#0:8",),
            payload=payload,
        )


def test_reaction_declares_the_complete_fixed_downstream_registry() -> None:
    payload = deepcopy(reaction_payload())
    payload["downstream"]["required_targets"].pop()

    with pytest.raises(ValueError, match="reaction downstream registry is incomplete"):
        CognitiveStateRevision.create(
            object_type="user_reaction_event",
            object_id="reaction-" + "1" * 32,
            source_event_id="feedback-source-event",
            source_revision_id="raw-feedback-revision",
            source_content_hash="sha256:" + "2" * 64,
            scope_type="session",
            scope_id="session-feedback",
            evidence_refs=("raw-event:feedback#0:8",),
            payload=payload,
        )


def test_attribution_input_set_hash_binds_current_reaction_refs() -> None:
    payload = deepcopy(attribution_payload())
    payload["reaction_refs"][0]["payload_hash"] = "sha256:" + "f" * 64

    with pytest.raises(ValueError, match="attribution input set hash mismatch"):
        CognitiveStateRevision.create(
            object_type="feedback_attribution_record",
            object_id="feedback-attribution-" + "d" * 32,
            source_event_id="feedback-source-event",
            source_revision_id="reaction-revision:" + "8" * 32,
            source_content_hash="sha256:" + "9" * 64,
            scope_type="session",
            scope_id="session-feedback",
            evidence_refs=("reaction-revision:" + "8" * 32,),
            payload=payload,
        )


def test_reaction_schema_and_evidence_identity_are_strict() -> None:
    schema_drift = deepcopy(reaction_payload())
    schema_drift["schema_version"] = "mnemos.user_reaction_event.v0"
    schema_drift["reaction_input_hash"] = reaction_input_hash(schema_drift)
    evidence_drift = deepcopy(reaction_payload())
    evidence_drift["evidence"]["content_hashes"] = []
    evidence_drift["reaction_input_hash"] = reaction_input_hash(evidence_drift)

    with pytest.raises(ValueError, match="user_reaction_event schema_version mismatch"):
        _create_reaction_from_payload(schema_drift)
    with pytest.raises(ValueError, match="reaction evidence is incomplete"):
        _create_reaction_from_payload(evidence_drift)


def test_reaction_recording_time_cannot_precede_observation() -> None:
    payload = deepcopy(reaction_payload())
    payload["recorded_at"] = "2026-07-17T23:59:59+00:00"

    with pytest.raises(ValueError, match="reaction timestamp order is invalid"):
        _create_reaction_from_payload(payload)


def test_available_delivery_ref_requires_exact_content_identity() -> None:
    payload = deepcopy(reaction_payload())
    payload["delivery_ref"]["event_payload_hash"] = ""
    payload["reaction_input_hash"] = reaction_input_hash(payload)

    with pytest.raises(ValueError, match="reaction delivery ref is invalid"):
        _create_reaction_from_payload(payload)


def _create_reaction_from_payload(payload: dict) -> CognitiveStateRevision:
    return CognitiveStateRevision.create(
        object_type="user_reaction_event",
        object_id="reaction-" + "1" * 32,
        source_event_id="feedback-source-event",
        source_revision_id="raw-feedback-revision",
        source_content_hash="sha256:" + "2" * 64,
        scope_type="session",
        scope_id="session-feedback",
        evidence_refs=("raw-event:feedback#0:8",),
        payload=payload,
    )
