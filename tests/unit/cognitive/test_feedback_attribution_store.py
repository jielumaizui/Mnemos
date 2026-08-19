from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.feedback_attribution import (
    FeedbackTargetEffect,
    FeedbackAttributionStore,
    UserReactionInput,
)
from core.cognitive.feedback_attribution_audit import audit_feedback_attribution
from core.cognitive.feedback_contract import (
    FEEDBACK_TARGETS,
    attribution_input_set_hash,
    reaction_input_hash,
    validate_feedback_attribution_payload,
    validate_user_reaction_payload,
)
from core.cognitive.feedback_command_failure import derive_feedback_command_failure
from core.cognitive.feedback_entrypoints import (
    record_dialog_decision_feedback,
    record_recap_feedback,
)
from core.cognitive.feedback_identity import (
    attribution_principal_ref,
    feedback_attribution_id,
)
from core.cognitive.feedback_owner_identity import CanonicalFeedbackOwner
from core.cognitive.feedback_proposal_gate import (
    build_gated_feedback_target_adapters,
)
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_contract import (
    CognitiveStateRevision,
    LocalConsumerCommand,
    canonical_json,
    sha256_json,
)
from core.cognitive.state_store import CognitiveStateStore, CognitiveStateUnitOfWork
from core.ops.cognitive_data_contract import CognitiveDataEvent
from core.ops.cognitive_event_ledger import insert_data_event_in_connection
from tests.unit.cognitive.feedback_attribution_fixtures import access_control


REPO_ROOT = Path(__file__).resolve().parents[3]


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="user:feedback-test",
        agent="mnemos",
        host_kind="test",
        capability_id="feedback-test",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _reaction_input() -> UserReactionInput:
    return UserReactionInput(
        source_event_id="source-feedback-1",
        source_revision_id="raw-feedback-1",
        source_content_hash="sha256:" + "1" * 64,
        observed_at="2026-07-18T00:00:00+00:00",
        scope_type="session",
        scope_id="session-feedback",
        source_channel="predictive_push",
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
        exposure_id="exposure-1",
        interface_id="predictive-push-card",
    )


def _store(tmp_path: Path) -> tuple[FeedbackAttributionStore, CognitiveStateStore]:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    state = CognitiveStateStore(db_path)
    return (
        FeedbackAttributionStore(
            state,
            clock=lambda: "2026-07-18T00:00:01+00:00",
        ),
        state,
    )


def _insert_malformed_commands(
    state: CognitiveStateStore,
    *,
    source_command_id: str,
    count: int,
) -> tuple[str, ...]:
    source = state.command(source_command_id)
    assert source is not None
    source_revision = state.revision(str(source["revision_id"]))
    assert source_revision is not None
    event_id = f"feedback-load-event-{count}"
    content_hash = sha256_json({"feedback_load_command_count": count})
    payload = dict(source_revision.payload)
    payload["attribution_id"] = f"feedback-attribution-load-{count}"
    revision = CognitiveStateRevision.create(
        object_type="feedback_attribution_record",
        object_id=str(payload["attribution_id"]),
        source_event_id=event_id,
        source_revision_id=f"feedback-load:{count}",
        source_content_hash=content_hash,
        scope_type=source_revision.scope_type,
        scope_id=source_revision.scope_id,
        evidence_refs=(source_revision.revision_id,),
        payload=payload,
        created_at="2026-07-18T00:00:00+00:00",
    )
    commands = []
    for index in range(count):
        consumer_id = f"invalid-feedback-target-{index:05d}"
        payload = {"target_id": consumer_id, "malformed_index": index}
        commands.append(
            LocalConsumerCommand.create(
                revision_id=revision.revision_id,
                consumer_id=consumer_id,
                command_type="invalid_feedback_command",
                payload=payload,
                created_at="2026-07-18T00:00:00+00:00",
            )
        )
    event = CognitiveDataEvent(
        event_id=event_id,
        source_id=f"feedback-load:{count}",
        asset_id=revision.object_id,
        source_kind="feedback_replay_load_fixture",
        source_uri=f"mnemos://feedback/load/{count}",
        content_hash=content_hash,
        canonical_subject=f"feedback_attribution_record:{revision.object_id}",
        data_type="feedback_attribution_record",
        producer="feedback_attribution_test",
        intended_consumers=tuple(command.consumer_id for command in commands),
        privacy_level="private",
        confidence=1.0,
        evidence_refs=(source_revision.revision_id,),
        dedupe_key=f"feedback-load:{count}",
        created_at="2026-07-18T00:00:00+00:00",
        retention_policy="test",
        metadata={"revision_ids": [revision.revision_id]},
    )
    state.unit_of_work().commit(
        revisions=(revision,),
        event=event,
        commands=tuple(commands),
    )
    return tuple(command.command_id for command in commands)


def _insert_real_reaction_proposal_workload(
    feedback: FeedbackAttributionStore,
    state: CognitiveStateStore,
    *,
    source_reaction_revision_id: str,
    source_attribution_revision_id: str,
    count: int,
) -> tuple[
    tuple[CognitiveStateRevision, ...],
    tuple[str, ...],
]:
    """Seed canonical reactions/attributions; production replay does all effects."""

    source_reaction = state.revision(source_reaction_revision_id)
    source_attribution = state.revision(source_attribution_revision_id)
    assert source_reaction is not None
    assert source_attribution is not None
    fixtures: list[
        tuple[
            CognitiveStateRevision,
            CognitiveStateRevision,
            CognitiveDataEvent,
            tuple[LocalConsumerCommand, ...],
        ]
    ] = []
    for index in range(count):
        suffix = f"{count}-{index:05d}"
        event_id = f"feedback-capacity-event-{suffix}"
        source_hash = sha256_json({"feedback_capacity_reaction": suffix})
        identity = source_hash.split(":", 1)[1][:32]
        reaction_payload = json.loads(json.dumps(dict(source_reaction.payload)))
        reaction_id = f"reaction-{identity}"
        reaction_payload["reaction_id"] = reaction_id
        reaction_payload["source_event_ref"] = {
            "event_id": event_id,
            "source_revision_id": f"feedback-capacity-source:{suffix}",
            "content_hash": source_hash,
        }
        reaction_payload["subject_ref"] = {
            "type": "delivery",
            "id": f"delivery-capacity-{suffix}",
        }
        reaction_payload["delivery_ref"]["event_id"] = f"delivery-capacity-{suffix}"
        reaction_payload["display_ref"]["display_id"] = f"display-capacity-{suffix}"
        reaction_payload["exposure"]["exposure_id"] = f"exposure-capacity-{suffix}"
        reaction_payload["reaction_input_hash"] = reaction_input_hash(reaction_payload)
        validate_user_reaction_payload(reaction_payload)
        reaction = CognitiveStateRevision.create(
            object_type="user_reaction_event",
            object_id=reaction_id,
            source_event_id=event_id,
            source_revision_id=f"feedback-capacity-source:{suffix}",
            source_content_hash=source_hash,
            scope_type=source_reaction.scope_type,
            scope_id=source_reaction.scope_id,
            evidence_refs=source_reaction.evidence_refs,
            payload=reaction_payload,
            created_at="2026-07-18T00:00:00+00:00",
        )
        attribution_payload = json.loads(
            json.dumps(dict(source_attribution.payload))
        )
        attribution_id = feedback_attribution_id(
            subject_ref=reaction_payload["subject_ref"],
            scope_type=reaction.scope_type,
            scope_id=reaction.scope_id,
            principal_ref=attribution_principal_ref(
                reaction_payload["access_control"]
            ),
        )
        attribution_payload["attribution_id"] = attribution_id
        attribution_payload["subject_ref"] = dict(reaction_payload["subject_ref"])
        attribution_payload["reaction_refs"] = [
            {
                "reaction_id": reaction.object_id,
                "revision_id": reaction.revision_id,
                "payload_hash": reaction.payload_hash,
            }
        ]
        attribution_payload["independence_keys"] = [
            f"session:session-feedback|exposure:exposure-capacity-{suffix}"
        ]
        attribution_payload["materiality"]["decision"] = "proposal_eligible"
        attribution_payload["disposition"] = "proposal_eligible"
        attribution_payload["post_neutralization_disposition"] = (
            "proposal_eligible"
        )
        attribution_payload["target_dispositions"] = [
            {
                "target_id": target_id,
                "eligible": target_id == "belief_correction_proposal",
                "exclusion_reason": (
                    "" if target_id == "belief_correction_proposal"
                    else "capacity_workload_non_selected_target"
                ),
                "command_ref": {
                    "command_key": (
                        f"feedback-capacity-target:{target_id}:{suffix}"
                    ),
                    "command_type": "evaluate_feedback_target",
                },
            }
            for target_id in FEEDBACK_TARGETS
        ]
        attribution_payload["supersedes_revision_id"] = ""
        attribution_payload["correction_of_revision_id"] = ""
        attribution_payload["input_set_hash"] = attribution_input_set_hash(
            attribution_payload
        )
        validate_feedback_attribution_payload(attribution_payload)
        attribution = CognitiveStateRevision.create(
            object_type="feedback_attribution_record",
            object_id=attribution_id,
            source_event_id=event_id,
            source_revision_id=f"feedback-capacity-source:{suffix}",
            source_content_hash=source_hash,
            scope_type=source_attribution.scope_type,
            scope_id=source_attribution.scope_id,
            evidence_refs=(reaction.revision_id,),
            payload=attribution_payload,
            created_at="2026-07-18T00:00:00+00:00",
        )
        commands = feedback._target_commands(
            attribution,
            recorded_at="2026-07-18T00:00:00+00:00",
        )
        event = CognitiveDataEvent(
            event_id=event_id,
            source_id=f"feedback-capacity-source:{suffix}",
            asset_id=attribution.object_id,
            source_kind="feedback_replay_capacity_fixture",
            source_uri=f"mnemos://feedback/capacity/{suffix}",
            content_hash=source_hash,
            canonical_subject=f"feedback_attribution_record:{attribution.object_id}",
            data_type="feedback_attribution_record",
            producer="feedback_attribution_test",
            intended_consumers=FEEDBACK_TARGETS,
            privacy_level="private",
            confidence=1.0,
            evidence_refs=(reaction.revision_id,),
            dedupe_key=f"feedback-capacity:{suffix}",
            created_at="2026-07-18T00:00:00+00:00",
            retention_policy="test",
            metadata={
                "revision_ids": [reaction.revision_id, attribution.revision_id]
            },
        )
        fixtures.append((reaction, attribution, event, commands))
    unit = state.unit_of_work()
    conn = state._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for reaction, attribution, event, commands in fixtures:
            unit._insert_revision(conn, reaction)
            unit._insert_revision(conn, attribution)
            insert_data_event_in_connection(
                conn,
                event,
                lifecycle_status="produced",
                allow_semantic=True,
            )
            for command in commands:
                unit._insert_outbox(conn, event.event_id, command)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return (
        tuple(attribution for _, attribution, _, _ in fixtures),
        tuple(
            command.command_id
            for _, _, _, commands in fixtures
            for command in commands
        ),
    )


def _insert_real_compensation_workload(
    state: CognitiveStateStore,
    *,
    prior_attributions: tuple[CognitiveStateRevision, ...],
) -> tuple[str, ...]:
    """Append seven correction-stage commands for each committed proposal."""

    with state._connect() as conn:
        conn.row_factory = sqlite3.Row
        proposal_effects = {
            str(row["revision_id"]): dict(row)
            for row in conn.execute(
                """
                SELECT e.*
                FROM cognitive_state_effect_receipts AS e
                JOIN cognitive_data_consumptions AS c
                  ON c.consumption_id=e.consumption_id
                WHERE e.consumer_id='belief_correction_proposal'
                  AND e.status='committed'
                  AND c.outcome='proposal_committed'
                """
            )
        }
    assert set(proposal_effects) == {
        prior.revision_id for prior in prior_attributions
    }
    fixtures: list[
        tuple[
            CognitiveStateRevision,
            CognitiveDataEvent,
            tuple[LocalConsumerCommand, ...],
        ]
    ] = []
    for index, prior in enumerate(prior_attributions):
        payload = json.loads(json.dumps(dict(prior.payload)))
        payload["materiality"]["decision"] = "correction_pending"
        payload["disposition"] = "correction_pending"
        payload["post_neutralization_disposition"] = "record_only"
        payload["target_dispositions"] = [
            {
                "target_id": target_id,
                "eligible": False,
                "exclusion_reason": (
                    "prior_effect_requires_neutralization"
                    if target_id == "belief_correction_proposal"
                    else "no_prior_active_effect"
                ),
                "command_ref": {
                    "command_key": (
                        f"feedback-capacity-neutralize:{target_id}:{index:05d}"
                    ),
                    "command_type": (
                        "neutralize_feedback_effect"
                        if target_id == "belief_correction_proposal"
                        else "evaluate_feedback_target"
                    ),
                },
            }
            for target_id in FEEDBACK_TARGETS
        ]
        payload["supersedes_revision_id"] = prior.revision_id
        payload["correction_of_revision_id"] = prior.revision_id
        payload["input_set_hash"] = attribution_input_set_hash(payload)
        validate_feedback_attribution_payload(payload)
        source_hash = sha256_json(
            {"feedback_capacity_compensation": prior.revision_id}
        )
        event_id = f"feedback-capacity-correction-{index:05d}"
        correction = CognitiveStateRevision.create(
            object_type="feedback_attribution_record",
            object_id=prior.object_id,
            source_event_id=event_id,
            source_revision_id=f"feedback-capacity-correction:{index:05d}",
            source_content_hash=source_hash,
            scope_type=prior.scope_type,
            scope_id=prior.scope_id,
            evidence_refs=(prior.revision_id,),
            payload=payload,
            supersedes_revision_id=prior.revision_id,
            correction_of_revision_id=prior.revision_id,
            created_at="2026-07-18T00:00:01+00:00",
        )
        prior_effect = proposal_effects[prior.revision_id]
        neutralization_command = LocalConsumerCommand.create(
            revision_id=correction.revision_id,
            consumer_id="belief_correction_proposal",
            command_type="neutralize_feedback_effect",
            payload={
                "schema_version": "mnemos.feedback_neutralization_command.v1",
                "attribution_revision_id": correction.revision_id,
                "attribution_payload_hash": correction.payload_hash,
                "target_id": "belief_correction_proposal",
                "command_key": (
                    "feedback-capacity-neutralize:belief_correction_proposal:"
                    f"{index:05d}"
                ),
                "prior_attribution_revision_id": prior.revision_id,
                "prior_effect_receipt_id": str(prior_effect["receipt_id"]),
                "prior_command_id": str(prior_effect["command_id"]),
                "prior_target_effect_id": str(prior_effect["target_effect_id"]),
                "prior_before_hash": str(prior_effect["before_hash"]),
                "prior_after_hash": str(prior_effect["after_hash"]),
                "neutralization_kind": "suppress",
            },
            created_at="2026-07-18T00:00:01+00:00",
        )
        skip_commands = tuple(
            LocalConsumerCommand.create(
                revision_id=correction.revision_id,
                consumer_id=target_id,
                command_type="evaluate_feedback_target",
                payload={
                    "schema_version": "mnemos.feedback_target_command.v1",
                    "attribution_revision_id": correction.revision_id,
                    "attribution_payload_hash": correction.payload_hash,
                    "input_set_hash": correction.payload["input_set_hash"],
                    "target_id": target_id,
                    "eligible": False,
                    "exclusion_reason": "no_prior_active_effect",
                    "command_key": next(
                        str(item["command_ref"]["command_key"])
                        for item in correction.payload["target_dispositions"]
                        if item["target_id"] == target_id
                    ),
                    "effect_kind": "intentional_skip",
                    "required_target_ids": list(FEEDBACK_TARGETS),
                },
                created_at="2026-07-18T00:00:01+00:00",
            )
            for target_id in FEEDBACK_TARGETS
            if target_id != "belief_correction_proposal"
        )
        event = CognitiveDataEvent(
            event_id=event_id,
            source_id=prior.revision_id,
            asset_id=correction.object_id,
            source_kind="feedback_capacity_correction_fixture",
            source_uri=f"mnemos://feedback/capacity/correction/{index:05d}",
            content_hash=source_hash,
            canonical_subject=f"feedback_attribution_record:{correction.object_id}",
            data_type="feedback_attribution_record",
            producer="feedback_attribution_test",
            intended_consumers=FEEDBACK_TARGETS,
            privacy_level="private",
            confidence=1.0,
            evidence_refs=(prior.revision_id,),
            dedupe_key=f"feedback-capacity-correction:{prior.revision_id}",
            created_at="2026-07-18T00:00:01+00:00",
            retention_policy="test",
            metadata={"revision_ids": [correction.revision_id]},
        )
        fixtures.append(
            (correction, event, (neutralization_command, *skip_commands))
        )
    unit = state.unit_of_work()
    conn = state._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for revision, event, commands in fixtures:
            unit._insert_revision(conn, revision)
            insert_data_event_in_connection(
                conn,
                event,
                lifecycle_status="produced",
                allow_semantic=True,
            )
            for command in commands:
                unit._insert_outbox(conn, event.event_id, command)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return tuple(
        command.command_id
        for _, _, commands in fixtures
        for command in commands
    )


def test_record_reaction_atomically_emits_complete_attribution_commands(
    tmp_path: Path,
) -> None:
    feedback, state = _store(tmp_path)

    receipt = feedback.record_reaction(_reaction_input(), _principal())

    assert receipt.status == "committed"
    assert receipt.disposition == "record_only"
    assert len(state.current_revisions(object_type="user_reaction_event")) == 1
    assert len(state.current_revisions(object_type="feedback_attribution_record")) == 1
    commands = state.pending_commands()
    assert len(commands) == 7
    assert {command["consumer_id"] for command in commands} == {
        "belief_correction_proposal",
        "delivery_state",
        "persona_proposal",
        "policy_proposal",
        "reflection_evidence",
        "training_evidence",
        "trust_proposal",
    }
    assert {command["payload"]["eligible"] for command in commands} == {False}


def test_verify_reports_exact_pending_then_complete_target_closure(
    tmp_path: Path,
) -> None:
    feedback, _state = _store(tmp_path)
    recorded = feedback.record_reaction(_reaction_input(), _principal())

    pending = feedback.verify(recorded.reaction_revision_id, _principal())
    feedback.replay_pending(limit=100)
    complete = feedback.verify(recorded.reaction_revision_id, _principal())

    assert pending.status == "verified_pending"
    assert pending.verified_target_count == 0
    assert pending.pending_target_ids == FEEDBACK_TARGETS
    assert complete.status == "verified_complete"
    assert complete.verified_target_count == len(FEEDBACK_TARGETS)
    assert complete.pending_target_ids == ()


def test_reaction_contract_rejects_malformed_causal_context_even_with_new_hash(
    tmp_path: Path,
) -> None:
    feedback, state = _store(tmp_path)
    recorded = feedback.record_reaction(_reaction_input(), _principal())
    revision = state.revision(recorded.reaction_revision_id)
    assert revision is not None
    base = json.loads(json.dumps(dict(revision.payload)))
    malformed_payloads = []

    malformed = json.loads(json.dumps(base))
    malformed["display_ref"]["display_id"] = ""
    malformed_payloads.append(malformed)

    malformed = json.loads(json.dumps(base))
    malformed["search_ref"] = {
        "state": "available",
        "session_id": "guessed-session",
        "result_id": "",
        "exposure_id": "",
        "unavailable_reason": "",
    }
    malformed_payloads.append(malformed)

    malformed = json.loads(json.dumps(base))
    malformed["observation_window"]["ends_at"] = "2026-07-17T23:59:59+00:00"
    malformed_payloads.append(malformed)

    malformed = json.loads(json.dumps(base))
    malformed["exposure"]["exposure_id"] = ""
    malformed_payloads.append(malformed)

    malformed = json.loads(json.dumps(base))
    malformed["competing_causes"] = [{"cause": "guessed"}]
    malformed_payloads.append(malformed)

    malformed = json.loads(json.dumps(base))
    malformed["source_completeness"] = {"state": "incomplete", "missing_refs": []}
    malformed_payloads.append(malformed)

    for malformed in malformed_payloads:
        malformed["reaction_input_hash"] = reaction_input_hash(malformed)
        with pytest.raises(ValueError):
            validate_user_reaction_payload(malformed)


def test_verify_rejects_an_extra_unregistered_attribution_command(
    tmp_path: Path,
) -> None:
    feedback, state = _store(tmp_path)
    recorded = feedback.record_reaction(_reaction_input(), _principal())
    feedback.replay_pending(limit=100)
    source = state.command(recorded.command_ids[0])
    assert source is not None
    extra = LocalConsumerCommand.create(
        revision_id=recorded.attribution_revision_id,
        consumer_id="unknown_feedback_target",
        command_type="evaluate_feedback_target",
        payload={"target_id": "unknown_feedback_target"},
        created_at="2026-07-20T00:00:00+00:00",
    )
    with sqlite3.connect(state.db_path) as conn:
        conn.execute(
            "INSERT INTO cognitive_state_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                extra.command_id,
                extra.revision_id,
                source["event_id"],
                extra.consumer_id,
                extra.command_type,
                canonical_json(extra.payload),
                extra.payload_hash,
                extra.created_at,
            ),
        )

    with pytest.raises(ValueError, match="command registry mismatch"):
        feedback.verify(recorded.reaction_revision_id, _principal())


def test_exact_reaction_replay_returns_existing_without_duplicate_work(
    tmp_path: Path,
) -> None:
    feedback, state = _store(tmp_path)
    first = feedback.record_reaction(_reaction_input(), _principal())

    replay = feedback.record_reaction(_reaction_input(), _principal())

    assert replay.status == "existing"
    assert replay.event_id == first.event_id
    assert replay.reaction_revision_id == first.reaction_revision_id
    assert replay.attribution_revision_id == first.attribution_revision_id
    assert replay.command_ids == first.command_ids
    assert len(state.current_revisions(object_type="user_reaction_event")) == 1
    assert len(state.current_revisions(object_type="feedback_attribution_record")) == 1
    assert len(state.pending_commands()) == 7


def test_private_attribution_identity_isolated_by_principal(
    tmp_path: Path,
) -> None:
    feedback, state = _store(tmp_path)
    first = feedback.record_reaction(_reaction_input(), _principal())
    second_principal = replace(_principal(), principal_id="user:feedback-other")
    second_access = make_cognitive_access_envelope(
        owner_principal_id=second_principal.principal_id,
        owner_agent=second_principal.agent,
        scope_type="session",
        scope_id="session-feedback",
        session_id="session-feedback",
        project="mnemos",
        purposes=("cognitive_state_read", "cognitive_state_write"),
        consent_provenance_refs=("raw-event:feedback#0:8",),
        sensitivity="sensitive",
        retention_policy="inherit_source",
        source_acl_lineage=("sha256:" + "a" * 64,),
    )

    second = feedback.record_reaction(
        replace(_reaction_input(), access_control=second_access),
        second_principal,
    )

    assert second.attribution_id != first.attribution_id
    first_attribution = state.revision(first.attribution_revision_id)
    second_attribution = state.revision(second.attribution_revision_id)
    assert first_attribution is not None
    assert second_attribution is not None
    assert first_attribution.payload["reaction_refs"] == [
        {
            "reaction_id": first.reaction_id,
            "revision_id": first.reaction_revision_id,
            "payload_hash": state.revision(first.reaction_revision_id).payload_hash,
        }
    ]
    assert second_attribution.payload["reaction_refs"] == [
        {
            "reaction_id": second.reaction_id,
            "revision_id": second.reaction_revision_id,
            "payload_hash": state.revision(second.reaction_revision_id).payload_hash,
        }
    ]
    assert first_attribution.payload["access_control"]["owner"]["principal_id"] == (
        _principal().principal_id
    )
    assert second_attribution.payload["access_control"]["owner"]["principal_id"] == (
        second_principal.principal_id
    )


def test_recap_feedback_rejects_caller_scope_rebinding(tmp_path: Path) -> None:
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")

    with pytest.raises(PermissionError, match="session scope does not match"):
        record_recap_feedback(
            database_dir=tmp_path,
            recap_snapshot={
                "recap_id": "recap-scope-bound",
                "session_id": "source-session",
                "project": "mnemos",
            },
            feedback_type="useful",
            comment="useful",
            principal=_principal(),
            narrowing=AccessNarrowing(
                session_id="caller-selected-session",
                project="mnemos",
            ),
        )


def test_dialog_feedback_scope_is_bound_to_persisted_object_identity(
    tmp_path: Path,
) -> None:
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")
    snapshot = {"proposal_id": "proposal-scope-bound", "revision": 1}
    first = record_dialog_decision_feedback(
        database_dir=tmp_path,
        proposal_snapshot=snapshot,
        action="reject",
        reason="not relevant",
        principal=_principal(),
        narrowing=AccessNarrowing(project="mnemos"),
    )
    replay = record_dialog_decision_feedback(
        database_dir=tmp_path,
        proposal_snapshot=snapshot,
        action="reject",
        reason="not relevant",
        principal=_principal(),
        narrowing=AccessNarrowing(session_id="caller-selected-session"),
    )

    assert replay["attribution_revision_id"] == first["attribution_revision_id"]
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    attribution = state.revision(first["attribution_revision_id"])
    assert attribution is not None
    assert attribution.scope_type == "session"
    assert attribution.scope_id == "dialog_decision_push:proposal-scope-bound"
    assert attribution.payload["scope"]["session_id"] == attribution.scope_id
    assert attribution.payload["scope"]["project"] == ""


def test_changed_action_requires_latest_event_and_appends_both_chains(
    tmp_path: Path,
) -> None:
    feedback, state = _store(tmp_path)
    first = feedback.record_reaction(_reaction_input(), _principal())
    changed = replace(
        _reaction_input(),
        source_event_id="source-feedback-2",
        source_revision_id="raw-feedback-2",
        source_content_hash="sha256:" + "5" * 64,
        observed_at="2026-07-18T00:00:00+00:00",
        kind="dismiss",
    )

    with pytest.raises(ValueError, match="stale_reaction_supersedes"):
        feedback.record_reaction(changed, _principal())

    second = feedback.record_reaction(
        replace(changed, supersedes_event_id=first.event_id),
        _principal(),
    )

    assert second.status == "committed"
    reaction = state.current_revision("user_reaction_event", first.reaction_id)
    attribution = state.current_revision(
        "feedback_attribution_record",
        first.attribution_id,
    )
    assert reaction is not None
    assert reaction.supersedes_revision_id == first.reaction_revision_id
    assert attribution is not None
    assert attribution.supersedes_revision_id == first.attribution_revision_id
    assert len(state.pending_commands()) == 7
    superseded = tuple(state.effect_receipt(command_id) for command_id in first.command_ids)
    assert all(receipt is not None for receipt in superseded)
    assert {str(receipt["status"]) for receipt in superseded if receipt} == {"rejected"}
    assert {
        str(receipt["consumption_outcome"])
        for receipt in superseded
        if receipt
    } == {"superseded_before_effect"}


def test_correction_rolls_back_old_command_closure_with_new_revisions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    feedback, state = _store(tmp_path)
    first = feedback.record_reaction(_reaction_input(), _principal())
    original_commit = CognitiveStateUnitOfWork.commit

    def crash_after_closure(unit, **kwargs):
        if kwargs.get("superseded_feedback_command_ids"):
            def failpoint(phase: str) -> None:
                if phase == "after_feedback_command_supersession":
                    raise OSError("injected correction transaction crash")

            kwargs["failpoint"] = failpoint
        return original_commit(unit, **kwargs)

    monkeypatch.setattr(CognitiveStateUnitOfWork, "commit", crash_after_closure)
    changed = replace(
        _reaction_input(),
        source_event_id="rollback-feedback",
        source_revision_id="rollback-feedback-revision",
        source_content_hash="sha256:" + "0" * 64,
        kind="dismiss",
        supersedes_event_id=first.event_id,
    )

    with pytest.raises(OSError, match="correction transaction crash"):
        feedback.record_reaction(changed, _principal())

    assert state.current_revision(
        "user_reaction_event", first.reaction_id
    ).revision_id == first.reaction_revision_id
    assert state.current_revision(
        "feedback_attribution_record", first.attribution_id
    ).revision_id == first.attribution_revision_id
    assert {item["command_id"] for item in state.pending_commands()} == set(
        first.command_ids
    )
    assert all(state.effect_receipt(command_id) is None for command_id in first.command_ids)


def test_weak_feedback_becomes_proposal_eligible_at_global_threshold(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    state = CognitiveStateStore(db_path)
    feedback = FeedbackAttributionStore(
        state,
        clock=lambda: "2026-07-20T00:00:00+00:00",
    )

    def weak(index: int, observed_at: str) -> UserReactionInput:
        return replace(
            _reaction_input(),
            source_event_id=f"search-feedback-{index}",
            source_revision_id=f"search-feedback-revision-{index}",
            source_content_hash="sha256:" + str(index) * 64,
            observed_at=observed_at,
            source_channel="context_search",
            kind="clicked",
            delivery_ref={
                "state": "unavailable",
                "event_id": "",
                "event_payload_hash": "",
                "unavailable_reason": "search_result_interaction",
            },
            search_ref={
                "state": "available",
                "session_id": "session-feedback",
                "result_id": f"result-{index}",
                "exposure_id": f"search-exposure-{index}",
                "unavailable_reason": "",
            },
            exposure_id=f"search-exposure-{index}",
            interface_id="context-search-result",
        )

    first = feedback.record_reaction(
        weak(1, "2026-07-18T00:00:00+00:00"),
        _principal(),
    )
    second = feedback.record_reaction(
        weak(2, "2026-07-18T23:00:00+00:00"),
        _principal(),
    )
    third = feedback.record_reaction(
        weak(3, "2026-07-19T00:00:01+00:00"),
        _principal(),
    )

    assert first.disposition == "record_only"
    assert second.disposition == "record_only"
    assert third.disposition == "proposal_eligible"
    third_commands = [
        command
        for command in state.pending_commands()
        if command["revision_id"] == third.attribution_revision_id
    ]
    assert len(third_commands) == 7
    assert {command["payload"]["eligible"] for command in third_commands} == {True}
    assert {command["payload"]["exclusion_reason"] for command in third_commands} == {
        ""
    }
    audit = audit_feedback_attribution(database_dir=tmp_path, repo_root=REPO_ROOT)
    assert audit["metrics"]["auto_update_from_weak_single_signal"] == 0


def test_project_weak_feedback_uses_independent_exposures_without_session_id(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    state = CognitiveStateStore(db_path)
    feedback = FeedbackAttributionStore(
        state,
        clock=lambda: "2026-07-20T00:00:00+00:00",
    )
    project_access = make_cognitive_access_envelope(
        owner_principal_id="user:feedback-test",
        owner_agent="mnemos",
        scope_type="project",
        scope_id="mnemos",
        session_id="",
        project="mnemos",
        purposes=("cognitive_state_read", "cognitive_state_write"),
        consent_provenance_refs=("raw-event:feedback#0:8",),
        sensitivity="sensitive",
        retention_policy="inherit_source",
        source_acl_lineage=("sha256:" + "a" * 64,),
    )
    receipt = None
    for index, observed_at in enumerate(
        (
            "2026-07-18T00:00:00+00:00",
            "2026-07-18T23:00:00+00:00",
            "2026-07-19T00:00:01+00:00",
        ),
        start=1,
    ):
        receipt = feedback.record_reaction(
            replace(
                _reaction_input(),
                source_event_id=f"project-search-feedback-{index}",
                source_revision_id=f"project-search-revision-{index}",
                source_content_hash=sha256_json({"project_search": index}),
                observed_at=observed_at,
                scope_type="project",
                scope_id="mnemos",
                source_channel="context_search",
                kind="clicked",
                access_control=project_access,
                delivery_ref={
                    "state": "unavailable",
                    "event_id": "",
                    "event_payload_hash": "",
                    "unavailable_reason": "search_result_interaction",
                },
                search_ref={
                    "state": "available",
                    "session_id": "project-search-session",
                    "result_id": f"project-result-{index}",
                    "exposure_id": f"project-exposure-{index}",
                    "unavailable_reason": "",
                },
                exposure_id=f"project-exposure-{index}",
                interface_id="context-search-result",
            ),
            _principal(),
        )

    assert receipt is not None
    assert receipt.disposition == "proposal_eligible"
    audit = audit_feedback_attribution(database_dir=tmp_path, repo_root=REPO_ROOT)
    assert audit["metrics"]["auto_update_from_weak_single_signal"] == 0


def test_reaction_rejects_available_entity_ref_without_canonical_revision(
    tmp_path: Path,
) -> None:
    feedback, _state = _store(tmp_path)
    forged = replace(
        _reaction_input(),
        decision_ref={
            "state": "available",
            "id": "decision-does-not-exist",
            "revision_id": "cogrev-" + "d" * 32,
            "content_hash": "sha256:" + "e" * 64,
            "unavailable_reason": "",
        },
    )

    with pytest.raises(ValueError, match="does not resolve canonically"):
        feedback.record_reaction(forged, _principal())


@pytest.mark.parametrize(
    ("observed_at", "session_ids", "exposure_ids"),
    (
        (
            ("2026-07-18T00:00:00+00:00", "2026-07-19T01:00:00+00:00"),
            ("session-feedback", "session-feedback"),
            ("exposure-1", "exposure-2"),
        ),
        (
            (
                "2026-07-18T00:00:00+00:00",
                "2026-07-18T12:00:00+00:00",
                "2026-07-18T23:59:59+00:00",
            ),
            ("session-feedback", "session-feedback", "session-feedback"),
            ("exposure-1", "exposure-2", "exposure-3"),
        ),
    ),
)
def test_weak_feedback_stays_record_only_below_any_global_threshold(
    tmp_path: Path,
    observed_at: tuple[str, ...],
    session_ids: tuple[str, ...],
    exposure_ids: tuple[str, ...],
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    state = CognitiveStateStore(db_path)
    feedback = FeedbackAttributionStore(
        state,
        clock=lambda: "2026-07-20T00:00:00+00:00",
    )
    receipt = None
    for index, timestamp in enumerate(observed_at, start=1):
        receipt = feedback.record_reaction(
            replace(
                _reaction_input(),
                source_event_id=f"weak-boundary-{index}",
                source_revision_id=f"weak-boundary-revision-{index}",
                source_content_hash=sha256_json({"weak_boundary": index}),
                observed_at=timestamp,
                source_channel="context_search",
                kind="clicked",
                delivery_ref={
                    "state": "unavailable",
                    "event_id": "",
                    "event_payload_hash": "",
                    "unavailable_reason": "search_result_interaction",
                },
                search_ref={
                    "state": "available",
                    "session_id": session_ids[index - 1],
                    "result_id": f"result-{index}",
                    "exposure_id": exposure_ids[index - 1],
                    "unavailable_reason": "",
                },
                exposure_id=exposure_ids[index - 1],
                interface_id="context-search-result",
            ),
            _principal(),
        )

    assert receipt is not None
    assert receipt.disposition == "record_only"
    commands = [
        command
        for command in state.pending_commands()
        if command["revision_id"] == receipt.attribution_revision_id
    ]
    assert len(commands) == 7
    assert {command["payload"]["eligible"] for command in commands} == {False}


def test_weak_feedback_requires_two_independent_session_or_exposure_identities(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    state = CognitiveStateStore(db_path)
    feedback = FeedbackAttributionStore(
        state,
        clock=lambda: "2026-07-20T00:00:00+00:00",
    )
    receipt = None
    for index, timestamp in enumerate(
        (
            "2026-07-18T00:00:00+00:00",
            "2026-07-18T12:00:00+00:00",
            "2026-07-19T00:00:01+00:00",
        ),
        start=1,
    ):
        delivery_hash = sha256_json({"weak_delivery": index})
        receipt = feedback.record_reaction(
            replace(
                _reaction_input(),
                source_event_id=f"weak-independence-{index}",
                source_revision_id=f"weak-independence-revision-{index}",
                source_content_hash=sha256_json({"weak_independence": index}),
                observed_at=timestamp,
                source_channel="delivery_feedback",
                kind="opened",
                delivery_ref={
                    "state": "available",
                    "event_id": f"delivery-independence-{index}",
                    "event_payload_hash": delivery_hash,
                    "unavailable_reason": "",
                },
                display_ref={
                    "state": "available",
                    "display_id": "shared-display",
                    "content_hash": delivery_hash,
                    "unavailable_reason": "",
                },
                search_ref={
                    "state": "unavailable",
                    "session_id": "",
                    "result_id": "",
                    "exposure_id": "",
                    "unavailable_reason": "not_a_search_interaction",
                },
                exposure_id="shared-exposure",
                interface_id="delivery-card",
            ),
            _principal(),
        )

    assert receipt is not None
    assert receipt.disposition == "record_only"
    attribution = state.revision(receipt.attribution_revision_id)
    assert attribution is not None
    assert attribution.payload["materiality"]["observation_count"] == 3
    assert attribution.payload["materiality"]["distinct_session_count"] == 1
    assert attribution.payload["materiality"]["distinct_exposure_count"] == 1


def test_ineligible_target_closes_with_canonical_intentional_skip(
    tmp_path: Path,
) -> None:
    feedback, state = _store(tmp_path)
    recorded = feedback.record_reaction(_reaction_input(), _principal())

    disposition = feedback.process_command(recorded.command_ids[0])

    assert disposition.disposition == "intentional_skip"
    assert disposition.target_id == "belief_correction_proposal"
    assert disposition.before_hash == disposition.after_hash
    assert len(state.pending_commands()) == 6
    assert state.integrity_report()["effect_receipt_reciprocity_gap"] == 0


def test_generic_receipt_api_rejects_forged_successful_feedback_effect(
    tmp_path: Path,
) -> None:
    feedback, state = _store(tmp_path)
    first = feedback.record_reaction(_reaction_input(), _principal())
    correction = feedback.correct_reaction(
        replace(
            _reaction_input(),
            source_event_id="forged-receipt-correction",
            source_revision_id="forged-receipt-correction-revision",
            source_content_hash="sha256:" + "f" * 64,
            kind="inaccurate",
            supersedes_event_id=first.event_id,
            correction_of_event_id=first.event_id,
            correction_target_ref="delivery:delivery-1",
            correction_reason="the prior output is inaccurate",
        ),
        _principal(),
    )
    command_id = correction.command_ids[0]

    with pytest.raises(PermissionError, match="specialized receipt closure"):
        state.record_effect_receipt(
            command_id,
            status="committed",
            target_effect_id="forged-domain-effect",
            before_hash="sha256:" + "1" * 64,
            after_hash="sha256:" + "2" * 64,
            evidence_refs=(
                f"feedback-command:{command_id}",
                "domain-feedback-receipt:belief_correction_proposal:forged",
            ),
            outcome="proposal_committed",
        )

    assert state.effect_receipt(command_id) is None


def test_generic_receipt_api_rejects_forged_feedback_skip(
    tmp_path: Path,
) -> None:
    feedback, state = _store(tmp_path)
    recorded = feedback.record_reaction(_reaction_input(), _principal())
    command_id = recorded.command_ids[0]

    with pytest.raises(PermissionError, match="specialized receipt closure"):
        state.record_effect_receipt(
            command_id,
            status="intentional_skip",
            target_effect_id="forged-feedback-skip",
            before_hash="sha256:" + "1" * 64,
            after_hash="sha256:" + "1" * 64,
            evidence_refs=(f"feedback-command:{command_id}",),
            outcome="caller_selected_skip",
            terminal_reason_code="feedback_target_ineligible",
        )

    assert state.effect_receipt(command_id) is None


def test_valid_feedback_command_cannot_self_sign_a_terminal_failure(
    tmp_path: Path,
) -> None:
    feedback, state = _store(tmp_path)
    recorded = feedback.record_reaction(_reaction_input(), _principal())
    command_id = recorded.command_ids[0]

    assert derive_feedback_command_failure(state, command_id) is None
    with pytest.raises(PermissionError, match="canonical owner context"):
        state._record_feedback_terminal_failure(
            command_id,
            proof=None,
        )

    assert state.effect_receipt(command_id) is None


def test_structural_failure_requires_available_domain_oracle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    feedback, state = _store(tmp_path)
    source = feedback.record_reaction(_reaction_input(), _principal())
    _, command_ids = _insert_real_reaction_proposal_workload(
        feedback,
        state,
        source_reaction_revision_id=source.reaction_revision_id,
        source_attribution_revision_id=source.attribution_revision_id,
        count=1,
    )
    source_command = next(
        state.command(command_id)
        for command_id in command_ids
        if state.command(command_id)["consumer_id"]
        == "belief_correction_proposal"
    )
    source_revision = state.revision(str(source_command["revision_id"]))
    assert source_revision is not None
    event_id = "feedback-structural-domain-conflict"
    content_hash = sha256_json({"event_id": event_id})
    attribution_payload = json.loads(json.dumps(dict(source_revision.payload)))
    attribution_payload["attribution_id"] = event_id
    attribution = CognitiveStateRevision.create(
        object_type="feedback_attribution_record",
        object_id=event_id,
        source_event_id=event_id,
        source_revision_id=event_id,
        source_content_hash=content_hash,
        scope_type=source_revision.scope_type,
        scope_id=source_revision.scope_id,
        evidence_refs=(source_revision.revision_id,),
        payload=attribution_payload,
        created_at="2026-07-18T00:00:00+00:00",
    )
    command_payload = dict(source_command["payload"])
    command_payload.update(
        {
            "attribution_revision_id": attribution.revision_id,
            "attribution_payload_hash": attribution.payload_hash,
            "command_key": "feedback-structural-domain-conflict",
            "required_target_ids": ["belief_correction_proposal"],
        }
    )
    malformed = LocalConsumerCommand.create(
        revision_id=attribution.revision_id,
        consumer_id="belief_correction_proposal",
        command_type="evaluate_feedback_target",
        payload=command_payload,
        created_at="2026-07-18T00:00:00+00:00",
    )
    event = CognitiveDataEvent(
        event_id=event_id,
        source_id=event_id,
        asset_id=attribution.object_id,
        source_kind="feedback_structural_failure_fixture",
        source_uri=f"mnemos://feedback/{event_id}",
        content_hash=content_hash,
        canonical_subject=f"feedback_attribution_record:{attribution.object_id}",
        data_type="feedback_attribution_record",
        producer="feedback_attribution_test",
        intended_consumers=("belief_correction_proposal",),
        privacy_level="private",
        confidence=1.0,
        evidence_refs=(source_revision.revision_id,),
        dedupe_key=event_id,
        created_at="2026-07-18T00:00:00+00:00",
        retention_policy="test",
        metadata={"revision_ids": [attribution.revision_id]},
    )
    state.unit_of_work().commit(
        revisions=(attribution,),
        event=event,
        commands=(malformed,),
    )
    adapters = build_gated_feedback_target_adapters(tmp_path)
    domain_effect = adapters["belief_correction_proposal"].apply(command_payload)
    assert adapters["belief_correction_proposal"].verify_command_effect(
        command_payload,
        domain_effect,
    )

    class UnavailableOracle:
        def inspect_command_effect(self, command):
            raise sqlite3.DatabaseError("target oracle unavailable")

        monkeypatch.setattr(
            "core.cognitive.feedback_target_registry."
            "build_registered_feedback_proposal_owner",
            lambda _database_dir, _target_id: UnavailableOracle(),
        )
    with pytest.raises(sqlite3.DatabaseError, match="oracle unavailable"):
        feedback.process_command(malformed.command_id)
    assert state.effect_receipt(malformed.command_id) is None
    assert malformed.command_id in {
        str(item["command_id"])
        for item in state.pending_commands("belief_correction_proposal")
    }
    monkeypatch.undo()

    with pytest.raises(RuntimeError, match="existing domain effect"):
        feedback.process_command(malformed.command_id)

    assert state.effect_receipt(malformed.command_id) is None
    assert malformed.command_id in {
        str(item["command_id"])
        for item in state.pending_commands("belief_correction_proposal")
    }


def _eligible_correction_command(
    tmp_path: Path,
    *,
    adapter: object,
) -> tuple[FeedbackAttributionStore, CognitiveStateStore, str]:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    state = CognitiveStateStore(db_path)
    adapters = build_gated_feedback_target_adapters(tmp_path)
    adapters["belief_correction_proposal"] = adapter
    owner = FeedbackAttributionStore(state, target_adapters=adapters)
    first = owner.record_reaction(_reaction_input(), _principal())
    correction = owner.correct_reaction(
        replace(
            _reaction_input(),
            source_event_id="target-attempt-failure",
            source_revision_id="target-attempt-failure-revision",
            source_content_hash="sha256:" + "9" * 64,
            kind="inaccurate",
            supersedes_event_id=first.event_id,
            correction_of_event_id=first.event_id,
            correction_target_ref="delivery:delivery-1#claim",
            correction_reason="wrong claim",
        ),
        _principal(),
    )
    command = next(
        item
        for item in state.pending_commands("belief_correction_proposal")
        if item["revision_id"] == correction.attribution_revision_id
    )
    command_id = str(command["command_id"])
    return owner, state, command_id


def test_transient_target_failure_cannot_be_self_signed_terminal(
    tmp_path: Path,
) -> None:
    class TransientFailureAdapter:
        def apply(self, command):
            raise OSError("temporary target outage")

        def neutralize(self, command):  # pragma: no cover - apply path only
            raise AssertionError("neutralization is not expected")

    owner, state, command_id = _eligible_correction_command(
        tmp_path,
        adapter=TransientFailureAdapter(),
    )

    with pytest.raises(OSError, match="temporary target outage"):
        owner.process_command(command_id)
    with pytest.raises(
        PermissionError,
        match="active owner processing context",
    ):
        owner._record_permanent_failure(
            command_id,
            RuntimeError("caller selected terminal failure"),
        )

    assert state.feedback_command_attempt(command_id) is not None
    assert state.effect_receipt(command_id) is None
    assert command_id in {
        str(item["command_id"])
        for item in state.pending_commands("belief_correction_proposal")
    }


def test_feedback_owner_capability_rejects_proxy_and_spoofed_subclass(
    tmp_path: Path,
) -> None:
    _owner, state = _store(tmp_path)

    class ProxyOwner:
        def __init__(self) -> None:
            self.state = state

    with pytest.raises(PermissionError, match="canonical owner type"):
        state._bind_feedback_owner_capability(ProxyOwner())
    with pytest.raises(TypeError, match="cannot be subclassed"):
        type(
            "FeedbackAttributionStore",
            (CanonicalFeedbackOwner,),
            {"__module__": "core.cognitive.feedback_attribution"},
        )


def test_target_side_effect_then_exception_never_synthesizes_unchanged(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "target-side-effect.marker"

    class MutatingFailureAdapter:
        def apply(self, command):
            marker.write_text("MUTATED", encoding="utf-8")
            raise ValueError("receipt serialization failed after mutation")

        def neutralize(self, command):  # pragma: no cover - apply path only
            raise AssertionError("neutralization is not expected")

    owner, state, command_id = _eligible_correction_command(
        tmp_path,
        adapter=MutatingFailureAdapter(),
    )

    with pytest.raises(ValueError, match="serialization failed after mutation"):
        owner.process_command(command_id)

    assert marker.read_text(encoding="utf-8") == "MUTATED"
    assert state.feedback_command_attempt(command_id) is not None
    assert state.effect_receipt(command_id) is None


def test_committed_target_effect_is_recovered_from_domain_oracle(
    tmp_path: Path,
) -> None:
    actual = build_gated_feedback_target_adapters(tmp_path)[
        "belief_correction_proposal"
    ]

    class CrashAfterCommitAdapter:
        def apply(self, command):
            actual.apply(command)
            raise ValueError("caller lost returned domain receipt")

        def neutralize(self, command):  # pragma: no cover - apply path only
            raise AssertionError("neutralization is not expected")

        def recover_command_effect(self, command):
            return actual.recover_command_effect(command)

    owner, state, command_id = _eligible_correction_command(
        tmp_path,
        adapter=CrashAfterCommitAdapter(),
    )

    receipt = owner.process_command(command_id)

    assert receipt.disposition == "proposal_committed"
    assert state.effect_receipt(command_id)["status"] == "committed"


def test_feedback_effect_receipt_rejects_caller_asserted_domain_proof(
    tmp_path: Path,
) -> None:
    feedback, state = _store(tmp_path)
    first = feedback.record_reaction(_reaction_input(), _principal())
    correction = feedback.correct_reaction(
        replace(
            _reaction_input(),
            source_event_id="forged-domain-correction",
            source_revision_id="forged-domain-correction-revision",
            source_content_hash="sha256:" + "e" * 64,
            kind="inaccurate",
            supersedes_event_id=first.event_id,
            correction_of_event_id=first.event_id,
            correction_target_ref="delivery:delivery-1#claim",
            correction_reason="wrong claim",
        ),
        _principal(),
    )
    command = next(
        item
        for item in state.pending_commands("belief_correction_proposal")
        if item["revision_id"] == correction.attribution_revision_id
    )
    command_id = str(command["command_id"])
    forged = FeedbackTargetEffect(
        target_id="belief_correction_proposal",
        target_effect_id="domain-feedback-effect-forged",
        disposition="proposal_committed",
        before_hash="sha256:" + "1" * 64,
        after_hash="sha256:" + "2" * 64,
        target_receipt_ref=(
            "domain-feedback-receipt:belief_correction_proposal:forged"
        ),
    )

    with pytest.raises(ValueError, match="reciprocal receipt verification"):
        state._record_feedback_effect_receipt(
            command_id,
            effect=forged,
            attribution_revision_id=correction.attribution_revision_id,
        )

    assert state.effect_receipt(command_id) is None


def test_pending_replay_uses_stable_batches_and_converges_exactly(
    tmp_path: Path,
) -> None:
    feedback, state = _store(tmp_path)
    feedback.record_reaction(_reaction_input(), _principal())

    first = feedback.replay_pending(limit=3)
    empty = feedback.replay_pending(limit=3)

    assert first.processed_count == 7
    assert empty.processed_count == 0
    assert not state.pending_commands()
    assert state.integrity_report()["effect_receipt_reciprocity_gap"] == 0


def test_pending_replay_does_not_consume_unrelated_canonical_commands(
    tmp_path: Path,
) -> None:
    feedback, state = _store(tmp_path)
    recorded = feedback.record_reaction(_reaction_input(), _principal())
    source = state.command(recorded.command_ids[0])
    assert source is not None
    unrelated = LocalConsumerCommand.create(
        revision_id=recorded.reaction_revision_id,
        consumer_id="wiki_projection",
        command_type="publish_wiki_projection",
        payload={"page_ref": "wiki:test"},
        created_at="2026-07-18T00:00:00+00:00",
    )
    with sqlite3.connect(state.db_path) as conn:
        conn.execute(
            "INSERT INTO cognitive_state_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                unrelated.command_id,
                unrelated.revision_id,
                source["event_id"],
                unrelated.consumer_id,
                unrelated.command_type,
                canonical_json(unrelated.payload),
                unrelated.payload_hash,
                unrelated.created_at,
            ),
        )

    replay = feedback.replay_pending(limit=2)

    assert replay.processed_count == 7
    assert state.effect_receipt(unrelated.command_id) is None
    assert {item["command_id"] for item in state.pending_commands()} == {
        unrelated.command_id
    }


def test_permanent_command_failure_is_terminal_and_does_not_abort_replay(
    tmp_path: Path,
) -> None:
    feedback, state = _store(tmp_path)
    recorded = feedback.record_reaction(_reaction_input(), _principal())
    malformed_id = _insert_malformed_commands(
        state,
        source_command_id=recorded.command_ids[0],
        count=1,
    )[0]
    proof = derive_feedback_command_failure(state, malformed_id)
    assert proof is not None
    from core.cognitive.feedback_attribution import (
        _ACTIVE_FEEDBACK_FAILURE_CONTEXT,
    )

    forged = _ACTIVE_FEEDBACK_FAILURE_CONTEXT.set(
        (0, id(state), malformed_id, object())
    )
    try:
        with pytest.raises(PermissionError, match="canonical owner context"):
            state._record_feedback_terminal_failure(
                malformed_id,
                proof=proof,
            )
    finally:
        _ACTIVE_FEEDBACK_FAILURE_CONTEXT.reset(forged)

    replay = feedback.replay_pending(limit=2)

    assert replay.processed_count == 8
    assert replay.dispositions.count("failed_terminal") == 1
    malformed = state.effect_receipt(malformed_id)
    assert malformed is not None
    assert malformed["status"] == "failed_terminal"
    assert malformed["before_hash"] == malformed["after_hash"]
    assert not state.pending_commands()


def test_exact_user_correction_appends_lineage_without_claiming_outcome(
    tmp_path: Path,
) -> None:
    feedback, state = _store(tmp_path)
    first = feedback.record_reaction(_reaction_input(), _principal())
    feedback.replay_pending(limit=10)
    correction = replace(
        _reaction_input(),
        source_event_id="source-feedback-correction",
        source_revision_id="raw-feedback-correction",
        source_content_hash="sha256:" + "6" * 64,
        kind="inaccurate",
        supersedes_event_id=first.event_id,
        correction_of_event_id=first.event_id,
        correction_target_ref="delivery:delivery-1#claim-1",
        correction_reason="The displayed claim is factually wrong.",
    )

    receipt = feedback.correct_reaction(correction, _principal())

    reaction = state.current_revision("user_reaction_event", first.reaction_id)
    attribution = state.current_revision(
        "feedback_attribution_record",
        first.attribution_id,
    )
    assert receipt.status == "committed"
    assert receipt.disposition == "proposal_eligible"
    assert reaction is not None
    assert reaction.correction_of_revision_id == first.reaction_revision_id
    assert attribution is not None
    assert attribution.payload["outcome_refs"] == []
    assert not state.current_revisions(object_type="outcome_measurement")


class _ProposalAdapter:
    def __init__(self, database_dir: Path) -> None:
        self.owner = build_gated_feedback_target_adapters(database_dir)[
            "belief_correction_proposal"
        ]
        self.effects: dict[str, FeedbackTargetEffect] = {}

    def apply(self, command: dict) -> FeedbackTargetEffect:
        effect = self.owner.apply(command)
        self.effects[effect.target_receipt_ref] = effect
        return effect

    def verify(self, effect: FeedbackTargetEffect) -> bool:
        return self.owner.verify(effect)

    def verify_command_effect(
        self,
        command: dict,
        effect: FeedbackTargetEffect,
    ) -> bool:
        return self.owner.verify_command_effect(command, effect)

    def neutralize(self, command: dict) -> FeedbackTargetEffect:
        effect = self.owner.neutralize(command)
        self.effects[effect.target_receipt_ref] = effect
        return effect


def test_eligible_target_requires_verified_reciprocal_proposal_receipt(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    state = CognitiveStateStore(db_path)
    adapter = _ProposalAdapter(tmp_path)
    adapters = build_gated_feedback_target_adapters(tmp_path)
    adapters["belief_correction_proposal"] = adapter
    feedback = FeedbackAttributionStore(
        state,
        clock=lambda: "2026-07-20T00:00:00+00:00",
        target_adapters=adapters,
    )

    first = feedback.record_reaction(_reaction_input(), _principal())
    correction_input = replace(
        _reaction_input(),
        source_event_id="eligible-feedback-correction",
        source_revision_id="eligible-feedback-correction-revision",
        source_content_hash="sha256:" + "a" * 64,
        kind="inaccurate",
        supersedes_event_id=first.event_id,
        correction_of_event_id=first.event_id,
        correction_target_ref="delivery:delivery-1#claim-1",
        correction_reason="The displayed claim is factually wrong.",
    )
    eligible = feedback.correct_reaction(
        correction_input,
        _principal(),
    )
    command = next(
        item
        for item in state.pending_commands("belief_correction_proposal")
        if item["revision_id"] == eligible.attribution_revision_id
    )

    receipt = feedback.process_command(str(command["command_id"]))

    assert receipt.disposition == "proposal_committed"
    assert receipt.schema_version == "mnemos.feedback_cognitive_update_receipt.v1"
    assert receipt.target_command_hash == command["payload_hash"]
    assert receipt.attribution_payload_hash.startswith("sha256:")
    assert len(receipt.reciprocal_receipt_refs) == 1
    assert receipt.reciprocal_receipt_refs[0].startswith(
        "domain-feedback-receipt:belief_correction_proposal:"
    )
    assert receipt.superseded_effect_refs == ()
    assert receipt.neutralized_effect_refs == ()
    assert len(receipt.decision_trace_refs) == 1
    assert len(receipt.action_refs) == 1
    assert receipt.decision_trace_refs[0].revision_id.startswith("cogrev-")
    assert receipt.action_refs[0].id.startswith("material-action-")
    assert receipt.to_dict()["target_command_hash"] == command["payload_hash"]
    assert receipt.before_hash != receipt.after_hash
    assert len(adapter.effects) == 1
    assert state.integrity_report()["effect_receipt_reciprocity_gap"] == 0


def test_correction_blocks_replacement_until_prior_effect_is_neutralized(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    state = CognitiveStateStore(db_path)
    adapter = _ProposalAdapter(tmp_path)
    feedback = FeedbackAttributionStore(
        state,
        clock=lambda: "2026-07-20T00:00:00+00:00",
        target_adapters={"belief_correction_proposal": adapter},
    )

    first = feedback.record_reaction(_reaction_input(), _principal())
    first_correction = replace(
        _reaction_input(),
        source_event_id="correctable-feedback-first-correction",
        source_revision_id="correctable-feedback-first-correction-revision",
        source_content_hash="sha256:" + "a" * 64,
        kind="inaccurate",
        supersedes_event_id=first.event_id,
        correction_of_event_id=first.event_id,
        correction_target_ref="delivery:delivery-1#claim-1",
        correction_reason="The displayed claim is factually wrong.",
    )
    eligible = feedback.correct_reaction(first_correction, _principal())
    proposal_command = next(
        item
        for item in state.pending_commands("belief_correction_proposal")
        if item["revision_id"] == eligible.attribution_revision_id
    )
    feedback.process_command(str(proposal_command["command_id"]))
    correction_input = replace(
        first_correction,
        source_event_id="correctable-feedback-correction",
        source_revision_id="correctable-feedback-correction-revision",
        source_content_hash="sha256:" + "b" * 64,
        kind="outdated",
        supersedes_event_id=eligible.event_id,
        correction_of_event_id=eligible.event_id,
        correction_reason="The displayed claim has become stale.",
    )

    correction = feedback.correct_reaction(correction_input, _principal())
    correction_commands = [
        item
        for item in state.pending_commands()
        if item["revision_id"] == correction.attribution_revision_id
    ]

    assert len(correction_commands) == len(FEEDBACK_TARGETS)
    assert sum(
        item["command_type"] == "neutralize_feedback_effect"
        for item in correction_commands
    ) == 1
    assert sum(
        item["command_type"] == "evaluate_feedback_target"
        and item["payload"]["eligible"] is False
        for item in correction_commands
    ) == 6
    pending = feedback.reconcile_subject(
        {"type": "delivery", "id": "delivery-1"},
        "2026-07-20T00:00:00+00:00",
    )
    assert pending.status == "compensation_pending"
    assert pending.command_ids == ()
    neutralization_command = next(
        item
        for item in correction_commands
        if item["command_type"] == "neutralize_feedback_effect"
    )
    neutralized = feedback.process_command(
        str(neutralization_command["command_id"])
    )
    assert neutralized.disposition == "suppressed"
    assert len(neutralized.superseded_effect_refs) == 3
    assert len(neutralized.neutralized_effect_refs) == 2
    still_pending = feedback.reconcile_subject(
        {"type": "delivery", "id": "delivery-1"},
        "2026-07-20T00:00:00+00:00",
    )
    assert still_pending.status == "compensation_pending"
    for command in correction_commands:
        if command["command_type"] == "evaluate_feedback_target":
            skipped = feedback.process_command(str(command["command_id"]))
            assert skipped.disposition == "intentional_skip"
    assert len(adapter.effects) == 2
    activated = feedback.reconcile_subject(
        {"type": "delivery", "id": "delivery-1"},
        "2026-07-20T00:00:01+00:00",
    )
    replacement_commands = [
        item
        for item in state.pending_commands()
        if item["revision_id"] == activated.attribution_revision_id
    ]
    assert activated.status == "committed"
    assert activated.disposition == "proposal_eligible"
    assert len(replacement_commands) == 7
    assert {item["command_type"] for item in replacement_commands} == {
        "evaluate_feedback_target"
    }


def test_replay_reaches_correction_fixed_point_and_exact_replay_is_empty(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    state = CognitiveStateStore(db_path)
    adapter = _ProposalAdapter(tmp_path)
    adapters = build_gated_feedback_target_adapters(tmp_path)
    adapters["belief_correction_proposal"] = adapter
    feedback = FeedbackAttributionStore(
        state,
        clock=lambda: "2026-07-20T00:00:00+00:00",
        target_adapters=adapters,
    )
    first = feedback.record_reaction(_reaction_input(), _principal())
    eligible = feedback.correct_reaction(
        replace(
            _reaction_input(),
            source_event_id="fixed-point-first-correction",
            source_revision_id="fixed-point-first-correction-revision",
            source_content_hash="sha256:" + "c" * 64,
            kind="inaccurate",
            supersedes_event_id=first.event_id,
            correction_of_event_id=first.event_id,
            correction_target_ref="delivery:delivery-1#claim-1",
            correction_reason="The displayed claim is factually wrong.",
        ),
        _principal(),
    )
    proposal_command = next(
        command
        for command in state.pending_commands("belief_correction_proposal")
        if command["revision_id"] == eligible.attribution_revision_id
    )
    feedback.process_command(str(proposal_command["command_id"]))
    correction = feedback.correct_reaction(
        replace(
            _reaction_input(),
            source_event_id="fixed-point-second-correction",
            source_revision_id="fixed-point-second-correction-revision",
            source_content_hash="sha256:" + "d" * 64,
            kind="outdated",
            supersedes_event_id=eligible.event_id,
            correction_of_event_id=eligible.event_id,
            correction_target_ref="delivery:delivery-1#claim-1",
            correction_reason="The correction has become stale.",
        ),
        _principal(),
    )

    converged = feedback.replay_pending(limit=3)
    exact_replay = feedback.replay_pending(limit=3)
    current = state.revision_chain(
        "feedback_attribution_record", correction.attribution_id
    )[-1]

    assert converged.processed_count == len(FEEDBACK_TARGETS) * 2
    assert set(converged.dispositions) == {
        "intentional_skip",
        "suppressed",
        "proposal_committed",
    }
    assert current.payload["disposition"] == "proposal_eligible"
    assert state.pending_commands() == []
    assert exact_replay.processed_count == 0
    assert len(adapter.effects) == 3


def test_neutralization_can_reconcile_to_record_only_without_replacement_effect(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    state = CognitiveStateStore(db_path)
    adapter = _ProposalAdapter(tmp_path)
    feedback = FeedbackAttributionStore(
        state,
        clock=lambda: "2026-07-20T00:00:00+00:00",
        target_adapters={"belief_correction_proposal": adapter},
    )
    first = feedback.record_reaction(_reaction_input(), _principal())
    eligible = feedback.correct_reaction(
        replace(
            _reaction_input(),
            source_event_id="record-only-first-correction",
            source_revision_id="record-only-first-correction-revision",
            source_content_hash="sha256:" + "e" * 64,
            kind="inaccurate",
            supersedes_event_id=first.event_id,
            correction_of_event_id=first.event_id,
            correction_target_ref="delivery:delivery-1#claim-1",
            correction_reason="The displayed claim is factually wrong.",
        ),
        _principal(),
    )
    proposal_command = next(
        command
        for command in state.pending_commands("belief_correction_proposal")
        if command["revision_id"] == eligible.attribution_revision_id
    )
    feedback.process_command(str(proposal_command["command_id"]))
    record_only = feedback.record_reaction(
        replace(
            _reaction_input(),
            source_event_id="record-only-preference",
            source_revision_id="record-only-preference-revision",
            source_content_hash="sha256:" + "f" * 64,
            kind="dismiss",
            supersedes_event_id=eligible.event_id,
        ),
        _principal(),
    )

    replay = feedback.replay_pending(limit=4)
    current = state.revision_chain(
        "feedback_attribution_record", record_only.attribution_id
    )[-1]

    assert record_only.disposition == "correction_pending"
    assert current.payload["post_neutralization_disposition"] == "record_only"
    assert current.payload["disposition"] == "record_only"
    assert replay.dispositions.count("suppressed") == 1
    assert replay.dispositions.count("intentional_skip") == (
        len(FEEDBACK_TARGETS) * 2 - 1
    )
    assert len(adapter.effects) == 2
    assert state.pending_commands() == []
