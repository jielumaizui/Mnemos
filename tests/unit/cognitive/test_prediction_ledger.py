"""Canonical PredictionRecord lifecycle and delivery binding tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType

import pytest

from core.access_policy import PrincipalEnvelope
from core.application.cognitive_state import CognitiveStateApplicationService
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.delivery_router import DeliveryBudgetPolicy, KnowledgeDeliveryRouter
from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionCoordinator,
)
from core.cognitive.prediction_ledger import (
    MaturityBatchReceipt,
    PredictionRecordStore,
    TASK_RESULT_OBSERVATION_SCHEMA,
    TASK_RESULT_ORACLE_ISSUER_ID,
)
from core.cognitive.state_contract import (
    CognitiveStateRevision,
    LocalConsumerCommand,
    canonical_json,
    sha256_json,
    validate_cognitive_state_payload,
)
from core.cognitive.feedback_contract import FEEDBACK_TARGETS, reaction_input_hash
from core.cognitive.feedback_attribution import FeedbackAttributionStore
from core.cognitive.feedback_attribution_audit import audit_feedback_attribution
from core.cognitive.feedback_proposal_gate import (
    build_gated_feedback_target_adapters,
)
from core.cognitive.state_store import CognitiveStateConflict, CognitiveStateStore
from core.cognitive.trust_scorer import TrustDecision
from core.evidence.source_authority import SourceAuthorityCatalog
from core.sync_framework.raw_event_store import RawEventStore
from core.ops.cognitive_data_contract import CognitiveDataEvent
from daemon.prediction_service import run_service as run_prediction_service
from tests.unit.cognitive.feedback_attribution_fixtures import reaction_payload


class _Trust:
    def __init__(self, decision: str = "deliver") -> None:
        self.decision = decision

    def decide(self, **kwargs):
        return TrustDecision(
            decision_id="trust-prediction-test",
            source=kwargs["source"],
            subject=kwargs["subject"],
            action=kwargs["action"],
            decision=self.decision,
            reason="test_policy",
            trust_score=0.9 if self.decision == "deliver" else 0.1,
            task_fit_score=kwargs["task_fit_score"],
            interruption_cost=kwargs["interruption_cost"],
            outcome_score=0.0,
            evidence_refs=list(kwargs.get("evidence_refs") or []),
            metadata={},
        )


def _router(tmp_path, *, decision: str = "deliver", window_hours: int = 168):
    return KnowledgeDeliveryRouter(
        db_path=tmp_path / "delivery_events.db",
        database_dir=tmp_path,
        config={"prediction.predictive_delivery_window_hours": window_hours},
        policy=DeliveryBudgetPolicy(same_topic_cooldown_hours=0),
        trust_scorer=_Trust(decision),
    )


def _predictive_access(subject: str):
    principal = _principal("system:prediction-test")
    return principal, make_cognitive_access_envelope(
        owner_principal_id=principal.principal_id,
        owner_agent=principal.agent,
        scope_type="topic",
        scope_id=subject,
        session_id="prediction-test-session",
        project="mnemos",
        purposes=(
            "cognitive_state_read",
            "cognitive_state_write",
            "prediction_read",
        ),
        consent_provenance_refs=(f"wiki:{subject}",),
        sensitivity="sensitive",
        retention_policy="prediction_source",
        source_acl_lineage=(f"sha256:{hashlib.sha256(subject.encode()).hexdigest()}",),
    )


def _route(router, *, requested_level: str = "hint"):
    principal, source_access = _predictive_access("prediction-ledger")
    return router.route_candidate(
        source="predictive_push",
        subject="prediction-ledger",
        channel="predictive_push",
        target="03-Tech/prediction-ledger.md",
        evidence_refs=["wiki:prediction-ledger"],
        task_fit_score=0.9,
        requested_level=requested_level,
        cooldown_key="prediction-ledger",
        source_access_control=source_access,
        principal=principal,
    )


def _principal(principal_id: str) -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id=principal_id,
        agent="mnemos",
        host_kind="daemon",
        capability_id="prediction-test",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _record_reaction_exposure(state: CognitiveStateStore, prediction) -> None:
    created_at = (
        datetime.fromisoformat(prediction.payload["evaluation_window"]["starts_at"])
        + timedelta(minutes=1)
    ).isoformat()
    event_id = "reaction-event-" + prediction.object_id.removeprefix("prediction-")
    source_hash = sha256_json(
        {
            "event_id": event_id,
            "delivery_event_id": prediction.payload["delivery_ref"]["event_id"],
        }
    )
    payload = reaction_payload(kind="opened")
    access_control = dict(prediction.payload["access_control"])
    payload.update(
        {
            "reaction_id": "reaction-" + prediction.object_id.removeprefix("prediction-"),
            "source_event_ref": {
                "event_id": event_id,
                "source_revision_id": f"reaction-source:{event_id}",
                "content_hash": source_hash,
            },
            "observed_at": created_at,
            "recorded_at": created_at,
            "principal_ref": {
                "principal_id": str(access_control["owner"]["principal_id"]),
                "agent": str(access_control["owner"]["agent"]),
                "authorization_ref": "authz:prediction-test",
            },
            "scope": {
                "type": prediction.scope_type,
                "id": prediction.scope_id,
                "project": str(access_control["scope"]["project"]),
                "session_id": str(access_control["scope"]["session_id"]),
            },
            "source_channel": "predictive_push",
            "authority_class": "tool_observation",
            "subject_ref": dict(prediction.payload["subject"]),
            "prediction_ref": {
                "state": "available",
                "id": prediction.object_id,
                "revision_id": prediction.revision_id,
                "content_hash": prediction.payload_hash,
                "unavailable_reason": "",
            },
            "delivery_ref": {
                "state": "available",
                "event_id": prediction.payload["delivery_ref"]["event_id"],
                "event_payload_hash": prediction.payload["delivery_ref"][
                    "event_payload_hash"
                ],
                "unavailable_reason": "",
            },
            "display_ref": {
                "state": "available",
                "display_id": prediction.payload["delivery_ref"]["event_id"],
                "content_hash": prediction.payload["delivery_ref"]["event_payload_hash"],
                "unavailable_reason": "",
            },
            "interaction": {
                "kind": "opened",
                "observed_facts": [{"name": "opened", "value": True}],
            },
            "evidence": {
                "refs": [
                    f"delivery-event:{prediction.payload['delivery_ref']['event_id']}"
                ],
                "content_hashes": [prediction.payload["delivery_ref"]["event_payload_hash"]],
            },
            "observation_window": {
                "starts_at": created_at,
                "ends_at": created_at,
                "status": "closed",
            },
            "exposure": {
                "session_id": str(access_control["scope"]["session_id"]),
                "exposure_id": prediction.payload["delivery_ref"]["event_id"],
                "interface_id": "predictive-push-card",
                "was_visible": True,
            },
            "access_control": access_control,
        }
    )
    payload["reaction_input_hash"] = reaction_input_hash(payload)
    revision = CognitiveStateRevision.create(
        object_type="user_reaction_event",
        object_id="reaction-" + prediction.object_id.removeprefix("prediction-"),
        source_event_id=event_id,
        source_revision_id=f"reaction-source:{event_id}",
        source_content_hash=source_hash,
        scope_type=prediction.scope_type,
        scope_id=prediction.scope_id,
        evidence_refs=(f"delivery-event:{prediction.payload['delivery_ref']['event_id']}",),
        payload=payload,
        created_at=created_at,
    )
    event = CognitiveDataEvent(
        event_id=event_id,
        source_id="user-reaction:test",
        asset_id=revision.object_id,
        source_kind="user_reaction",
        source_uri=f"mnemos://reaction/{revision.object_id}",
        content_hash=source_hash,
        canonical_subject=f"user_reaction_event:{revision.object_id}",
        data_type="user_reaction_event",
        producer="prediction-test",
        intended_consumers=("prediction_ledger",),
        privacy_level="private",
        confidence=1.0,
        evidence_refs=revision.evidence_refs,
        dedupe_key=f"prediction-test:{event_id}",
        created_at=created_at,
        retention_policy="prediction_ledger",
        metadata={"revision_ids": [revision.revision_id]},
    )
    command = LocalConsumerCommand.create(
        revision_id=revision.revision_id,
        consumer_id="prediction_ledger",
        command_type="record_prediction_exposure",
        payload={"revision_id": revision.revision_id},
        created_at=created_at,
    )
    state.unit_of_work().commit(
        revisions=(revision,),
        event=event,
        commands=(command,),
    )


def _objective_outcome_request(
    prediction,
    raw_db_path,
    *,
    observed_value: str = "useful",
    competing_causes: tuple[str, ...] = (),
    authority: str = "tool_observation",
    source_suffix: str = "1",
    observed_hours: int = 1,
    maturity_delay_hours: int = 0,
    correction_of_revision_id: str = "",
    omit_competing_cause_evidence: bool = False,
    acknowledge_delivery: bool = True,
):
    starts_at = datetime.fromisoformat(
        prediction.payload["evaluation_window"]["starts_at"]
    )
    observed_at = starts_at + timedelta(hours=observed_hours)
    prediction_access = prediction.payload["access_control"]
    principal_id = str(prediction_access["owner"]["principal_id"])
    principal_agent = str(prediction_access["owner"]["agent"])
    project = str(prediction_access["scope"]["project"])
    source_id = f"objective-measurement:test:{source_suffix}"
    source_content = f"objective measurement result {source_suffix}"
    role = {
        "tool_observation": "tool",
        "assistant_inference": "assistant",
        "explicit_user": "user",
        "system_policy": "system",
        "project_contract": "system",
    }[authority]
    observation_window = {
        "starts_at": starts_at.isoformat(),
        "ends_at": observed_at.isoformat(),
    }
    evidence_ref = f"tool-result:{source_id}"
    task_result_observation = {
        "schema_version": TASK_RESULT_OBSERVATION_SCHEMA,
        "issuer_id": TASK_RESULT_ORACLE_ISSUER_ID,
        "prediction_revision_id": prediction.revision_id,
        "prediction_input_hash": str(prediction.payload["prediction_input_hash"]),
        "source_id": source_id,
        "observed_value": observed_value,
        "observation_window": observation_window,
        "maturity": {
            "matured_at": (
                observed_at + timedelta(hours=maturity_delay_hours)
            ).isoformat(),
            "is_mature": True,
        },
        "evidence_refs": [evidence_ref],
        "uncertainty": {"kind": "categorical_exact", "value": None},
        "attribution": {
            "method": "single_intervention_window",
            "confidence": 1.0,
            "competing_causes": [
                {
                    "cause": cause,
                    "evidence_refs": (
                        [] if omit_competing_cause_evidence else [evidence_ref]
                    ),
                }
                for cause in competing_causes
            ],
            "evidence_refs": [evidence_ref],
        },
    }
    tool_results = (
        [
            {
                "tool_name": TASK_RESULT_ORACLE_ISSUER_ID,
                "content": canonical_json(task_result_observation),
            }
        ]
        if role == "tool"
        else []
    )
    source_content = canonical_json(task_result_observation)
    authority_content = (
        canonical_json(tool_results) if role == "tool" else source_content
    )
    raw_store = RawEventStore(db_path=raw_db_path)
    source_revision_id = raw_store.upsert_turn(
        source_agent="prediction-test",
        session_id="prediction-outcomes",
        turn_number=int(observed_hours),
        user_content=source_content if role == "user" else "",
        assistant_content=source_content if role == "assistant" else "",
        tool_results=tool_results,
        timestamp=observed_at.isoformat(),
        metadata={
            "native_event_id": source_id,
        },
    )
    raw_header = raw_store.get_revision_header(source_revision_id)
    raw_store.close()
    assert raw_header is not None
    source_hash = "sha256:" + str(raw_header["content_hash"]).removeprefix("sha256:")
    catalog = SourceAuthorityCatalog.from_messages(
        (
            {
                "role": role,
                "content": authority_content,
                "source_authority": authority,
                "source_span": {
                    "revision_id": source_revision_id,
                    "role": role,
                    "span_start": 0,
                    "span_end": len(authority_content),
                    "content_hash": source_hash,
                },
            },
        ),
        allowed_source_event_ids=(source_revision_id,),
    )
    authority_id = catalog.entries[0].source_authority_id
    source_access = make_cognitive_access_envelope(
        owner_principal_id=principal_id,
        owner_agent=principal_agent,
        scope_type=prediction.scope_type,
        scope_id=prediction.scope_id,
        session_id=str(prediction_access["scope"]["session_id"]),
        project=project,
        purposes=("cognitive_state_read", "cognitive_state_write", "prediction_read"),
        consent_provenance_refs=(source_id,),
        sensitivity="sensitive",
        retention_policy="prediction_ledger",
        source_acl_lineage=(source_hash,),
    )
    request = {
        "prediction_revision_id": prediction.revision_id,
        "scope": {"type": prediction.scope_type, "id": prediction.scope_id},
        "source": {
            "source_id": source_id,
            "source_revision_id": source_revision_id,
            "source_kind": "objective_measurement",
            "source_uri": f"oracle://{source_id}",
            "content_hash": source_hash,
            "created_at": observed_at.isoformat(),
            "evidence_refs": [evidence_ref],
            "access_control": source_access,
        },
        "measurement": {
            "source_authority": {"source_authority_id": authority_id},
        },
    }
    if correction_of_revision_id:
        request["correction_of_revision_id"] = correction_of_revision_id
    delivery_principal: dict[str, object] = {}
    with sqlite3.connect(Path(raw_db_path).parent / "delivery_events.db") as conn:
        row = conn.execute(
            "SELECT metadata_json FROM delivery_events WHERE event_id=?",
            (str(prediction.payload["delivery_ref"]["event_id"]),),
        ).fetchone()
    if row is not None:
        try:
            metadata = json.loads(str(row[0] or "{}"))
            candidate = metadata.get("delivery_principal")
            if isinstance(candidate, dict):
                delivery_principal = candidate
        except (TypeError, ValueError, json.JSONDecodeError):
            delivery_principal = {}
    principal = PrincipalEnvelope(
        principal_id=principal_id,
        agent=principal_agent,
        host_kind="daemon",
        capability_id=str(delivery_principal.get("capability_id") or "prediction-test"),
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({project}),
    )
    if acknowledge_delivery:
        KnowledgeDeliveryRouter(
            db_path=Path(raw_db_path).parent / "delivery_events.db",
            database_dir=Path(raw_db_path).parent,
        ).record_presentation(
            str(prediction.payload["delivery_ref"]["event_id"]),
            host_agent=principal.agent,
            rendered_content_hash=sha256_json(
                {
                    "fixture": "objective_outcome_presentation",
                    "prediction_revision_id": prediction.revision_id,
                    "delivery_event_id": prediction.payload["delivery_ref"]["event_id"],
                }
            ),
        )
    return request, principal, observed_at, catalog


def test_predictive_delivery_seals_prediction_in_decision_transaction(tmp_path):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    predictions = state.current_revisions(object_type="prediction_record")
    traces = state.current_revisions(object_type="decision_trace")

    assert len(predictions) == len(traces) == 1
    prediction = predictions[0]
    trace = traces[0]
    assert prediction.source_event_id == trace.source_event_id
    assert trace.payload["prediction_refs"] == [
        {
            "prediction_id": prediction.object_id,
            "prediction_plan_hash": prediction.payload["prediction_plan_hash"],
        }
    ]
    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        command = json.loads(
            conn.execute(
                "SELECT payload_json FROM cognitive_state_outbox "
                "WHERE command_type='execute_material_action'"
            ).fetchone()[0]
        )
    assert command["prediction_refs"] == [
        {
            "prediction_id": prediction.object_id,
            "prediction_plan_hash": prediction.payload["prediction_plan_hash"],
            "prediction_revision_id": prediction.revision_id,
            "prediction_revision_hash": prediction.payload_hash,
        }
    ]


def test_predictive_plan_and_atomic_decision_share_one_pre_effect_timestamp(
    tmp_path,
    monkeypatch,
):
    timestamps = iter(
        (
            "2026-07-19T08:07:01+00:00",
            "2026-07-19T08:07:02+00:00",
            "2026-07-19T08:07:03+00:00",
            "2026-07-19T08:07:04+00:00",
        )
    )
    monkeypatch.setattr(
        "core.cognitive.delivery_router._now",
        lambda: next(timestamps),
    )

    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    decision = state.current_revisions(object_type="decision_trace")[0]

    assert decision.created_at == prediction.created_at
    assert (
        prediction.payload["evaluation_window"]["starts_at"]
        == prediction.created_at
    )


def test_silent_delivery_still_seals_a_pre_effect_prediction(tmp_path):
    decision = _route(_router(tmp_path), requested_level="silent")
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]

    assert decision.delivered_level == "silent"
    assert prediction.payload["route_disposition"] == "silent"
    assert prediction.payload["delivery_ref"]["event_id"] == decision.event_id
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM delivery_events").fetchone()
    metadata = json.loads(str(row["metadata_json"]))
    assert metadata["prediction_record"]["prediction_revision_id"] == (
        prediction.revision_id
    )
    assert str(row["created_at"]) >= prediction.payload["evaluation_window"][
        "starts_at"
    ]
    assert decision.event_id == prediction.payload["delivery_ref"]["event_id"]


def test_suppressed_predictive_route_seals_before_projection_and_has_receipt(tmp_path):
    decision = _route(_router(tmp_path, decision="suppress"))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]

    assert prediction.payload["route_disposition"] == "suppress"
    assert prediction.payload["action_ref"] == {"action_id": "", "effect_id": ""}
    assert not state.current_revisions(object_type="decision_trace")
    with sqlite3.connect(tmp_path / "producer_consumer_ledger.db") as conn:
        row = conn.execute(
            """
            SELECT o.command_id, r.status, r.after_hash
            FROM cognitive_state_outbox AS o
            JOIN cognitive_state_effect_receipts AS r ON r.command_id=o.command_id
            WHERE o.command_type='project_prediction_delivery'
            """
        ).fetchone()
    assert row is not None and row[1] == "committed"
    assert row[2] == prediction.payload["delivery_ref"]["event_payload_hash"]
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        metadata = json.loads(
            conn.execute("SELECT metadata_json FROM delivery_events").fetchone()[0]
        )
    assert metadata["prediction_record"]["prediction_revision_id"] == (
        prediction.revision_id
    )
    assert decision.decision == "suppress"


def test_prediction_plan_rejects_access_control_replacement(tmp_path, monkeypatch):
    original = PredictionRecordStore.prepare_route_prediction

    def replace_access_control(self, route_facts):
        plan = original(self, route_facts)
        widened = json.loads(canonical_json(plan.access_control))
        widened["sensitivity"] = "private"
        return replace(plan, access_control=MappingProxyType(widened))

    monkeypatch.setattr(
        PredictionRecordStore,
        "prepare_route_prediction",
        replace_access_control,
    )

    with pytest.raises(ValueError, match="access control"):
        _route(_router(tmp_path, decision="suppress"))


def test_prediction_plan_rejects_rehashed_system_semantics(tmp_path, monkeypatch):
    original = PredictionRecordStore.prepare_route_prediction

    def forge_predicted_value(self, route_facts):
        plan = original(self, route_facts)
        forged_seed = {
            "schema_version": "mnemos.prediction_plan.v1",
            "delivery_event_id": plan.delivery_event_id,
            "delivery_event_payload_hash": plan.delivery_event_payload_hash,
            "route_disposition": plan.route_disposition,
            "predicted_value": "not_useful",
            "score_band": plan.score_band,
            "starts_at": plan.starts_at,
            "ends_at": plan.ends_at,
            "window_config_hash": plan.window_config_hash,
            "scope_type": plan.scope_type,
            "scope_id": plan.scope_id,
            "access_control_hash": plan.access_control_hash,
        }
        prediction_id = (
            "prediction-"
            + sha256_json(forged_seed).split(":", 1)[1][:32]
        )
        return replace(
            plan,
            predicted_value="not_useful",
            prediction_id=prediction_id,
            prediction_plan_hash=sha256_json(
                {**forged_seed, "prediction_id": prediction_id}
            ),
        )

    monkeypatch.setattr(
        PredictionRecordStore,
        "prepare_route_prediction",
        forge_predicted_value,
    )

    with pytest.raises(ValueError, match="canonical semantics"):
        _route(_router(tmp_path, decision="deliver"))


def test_suppressed_prediction_recovers_crash_after_seal_before_event(
    tmp_path,
    monkeypatch,
):
    router = _router(tmp_path, decision="suppress")
    original = router._log_nonmaterial_event
    calls = 0

    def crash_once(decision):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected crash after prediction seal")
        return original(decision)

    monkeypatch.setattr(router, "_log_nonmaterial_event", crash_once)
    with pytest.raises(OSError, match="after prediction seal"):
        _route(router)
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM delivery_events").fetchone() == (0,)

    replay = _route(router)

    assert replay.event_id == prediction.payload["delivery_ref"]["event_id"]
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM delivery_events").fetchone() == (1,)
    assert not state.pending_commands("prediction_delivery_projection")


def test_delivered_prediction_recovers_crash_after_atomic_seal_before_event(
    tmp_path,
    monkeypatch,
):
    router = _router(tmp_path)
    original = router._log_event
    calls = 0

    def crash_once(decision):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected crash after atomic prediction seal")
        return original(decision)

    monkeypatch.setattr(router, "_log_event", crash_once)
    with pytest.raises(OSError, match="after atomic prediction seal"):
        _route(router)
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    assert len(state.pending_commands()) == 1
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM delivery_events").fetchone() == (0,)

    replay = _route(router)

    assert replay.event_id == prediction.payload["delivery_ref"]["event_id"]
    assert not state.pending_commands()
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM delivery_events").fetchone() == (1,)


def test_delivered_prediction_recovers_crash_after_event_before_receipt(
    tmp_path,
    monkeypatch,
):
    router = _router(tmp_path)
    original = MaterialActionAuthorization.record_terminal
    calls = 0

    def crash_once(self, terminal):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected crash after delivery event")
        return original(self, terminal)

    monkeypatch.setattr(MaterialActionAuthorization, "record_terminal", crash_once)
    with pytest.raises(OSError, match="after delivery event"):
        _route(router)
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM delivery_events").fetchone() == (1,)
    assert len(state.pending_commands()) == 1

    replay = _route(router)

    assert replay.event_id == prediction.payload["delivery_ref"]["event_id"]
    assert not state.pending_commands()
    with sqlite3.connect(tmp_path / "delivery_events.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM delivery_events").fetchone() == (1,)


def test_eligible_outcome_closes_measured_and_builds_calibration(tmp_path):
    router = _router(tmp_path)
    delivery = _route(router)
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, observed_at, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
        acknowledge_delivery=False,
    )
    with pytest.raises(ValueError, match="acknowledged delivery presentation"):
        CognitiveStateApplicationService(state).apply_outcome(
            request,
            principal=principal,
            source_authority_catalog=catalog,
        )
    router.record_presentation(
        delivery.event_id,
        host_agent=principal.agent,
        rendered_content_hash="sha256:" + "b" * 64,
    )
    result = CognitiveStateApplicationService(state).apply_outcome(
        request,
        principal=principal,
        source_authority_catalog=catalog,
    )
    assert result["success"] is True
    outcome = state.current_revisions(object_type="outcome_measurement")[0]
    assert outcome.payload["presentation_ref"]["receipt_hash"].startswith("sha256:")
    invalid_payload = json.loads(json.dumps(dict(outcome.payload)))
    invalid_payload["presentation_ref"]["receipt_hash"] = ""
    with pytest.raises(ValueError, match="outcome presentation receipt"):
        validate_cognitive_state_payload("outcome_measurement", invalid_payload)
    with sqlite3.connect(state.db_path) as conn:
        row = conn.execute(
            "SELECT r.evidence_refs FROM cognitive_state_effect_receipts AS r "
            "JOIN cognitive_state_outbox AS o ON o.command_id=r.command_id "
            "WHERE o.command_type='project_prediction_outcome'"
        ).fetchone()
    assert row is not None
    receipt_refs = tuple(json.loads(str(row[0])))
    assert any(value.startswith("objective-oracle-issuance:sha256:") for value in receipt_refs)
    assert any(value.startswith("objective-oracle-source:") for value in receipt_refs)

    receipt = PredictionRecordStore(state).finalize(
        prediction.object_id,
        {},
        observed_at,
    )
    assert receipt.terminal_state == "measured"
    terminal = state.current_revision("prediction_record", prediction.object_id)
    assert terminal is not None
    assert terminal.payload["error"] == {"kind": "categorical_miss", "value": 0}
    report = PredictionRecordStore(state).calibration_report()
    assert report.status == "ok"
    assert report.measured == report.calibration_eligible == report.correct == 1
    assert report.accuracy == 1.0


def test_feedback_attribution_accepts_only_committed_objective_measurement(
    tmp_path,
):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, observed_at, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
    )
    CognitiveStateApplicationService(state).apply_outcome(
        request,
        principal=principal,
        source_authority_catalog=catalog,
    )
    outcome = state.current_revisions(object_type="outcome_measurement")[0]

    receipt = FeedbackAttributionStore(
        state,
        clock=lambda: observed_at.isoformat(),
    ).record_objective_outcome(outcome, principal)

    assert receipt.disposition == "objective_only"
    attribution = state.revision(receipt.attribution_revision_id)
    assert attribution is not None
    assert attribution.payload["reaction_refs"] == []
    assert attribution.payload["outcome_refs"] == [
        {
            "outcome_id": outcome.object_id,
            "revision_id": outcome.revision_id,
            "payload_hash": outcome.payload_hash,
        }
    ]
    commands = [
        item
        for item in state.pending_commands()
        if item["revision_id"] == receipt.attribution_revision_id
    ]
    assert {
        item["consumer_id"]
        for item in commands
        if item["command_type"] == "evaluate_feedback_target"
        and item["consumer_id"] in FEEDBACK_TARGETS
        and item["payload"]["eligible"]
    } == {"reflection_evidence", "training_evidence"}


def test_feedback_audit_recomputes_objective_owner_binding(tmp_path):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, observed_at, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
    )
    CognitiveStateApplicationService(state).apply_outcome(
        request,
        principal=principal,
        source_authority_catalog=catalog,
    )
    outcome = state.current_revisions(object_type="outcome_measurement")[0]
    FeedbackAttributionStore(
        state,
        clock=lambda: observed_at.isoformat(),
    ).record_objective_outcome(outcome, principal)
    with sqlite3.connect(state.db_path) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM cognitive_state_revisions "
                "WHERE revision_id=?",
                (outcome.revision_id,),
            ).fetchone()[0]
        )
        payload["access_control"]["owner"]["principal_id"] = "user:forged-owner"
        conn.execute("DROP TRIGGER cognitive_state_revisions_no_update")
        conn.execute(
            "UPDATE cognitive_state_revisions SET payload_json=? "
            "WHERE revision_id=?",
            (
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                outcome.revision_id,
            ),
        )
        conn.executescript(
            """
            CREATE TRIGGER cognitive_state_revisions_no_update
            BEFORE UPDATE ON cognitive_state_revisions BEGIN
                SELECT RAISE(ABORT, 'cognitive_state_revisions are immutable');
            END;
            """
        )

    report = audit_feedback_attribution(
        database_dir=tmp_path,
        repo_root=Path(__file__).resolve().parents[3],
    )

    assert report["ok"] is False
    assert report["metrics"]["attribution_principal_binding_gap"] > 0


def test_calibration_report_fails_closed_when_objective_raw_is_unavailable(tmp_path):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    raw_db = tmp_path / "raw_events.db"
    request, principal, observed_at, catalog = _objective_outcome_request(
        prediction,
        raw_db,
    )
    CognitiveStateApplicationService(state).apply_outcome(
        request,
        principal=principal,
        source_authority_catalog=catalog,
    )
    ledger = PredictionRecordStore(state)
    ledger.finalize(prediction.object_id, {}, observed_at)
    raw_db.rename(raw_db.with_suffix(".offline"))

    with pytest.raises(FileNotFoundError):
        ledger.calibration_report()


def test_outcome_receipt_crash_stays_ineligible_until_exact_replay(
    tmp_path,
    monkeypatch,
):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, observed_at, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
    )
    service = CognitiveStateApplicationService(state)
    original = CognitiveStateApplicationService._ensure_outcome_projection_receipt
    calls = 0

    def crash_once(self, revision_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated outcome receipt crash")
        return original(self, revision_id)

    monkeypatch.setattr(
        CognitiveStateApplicationService,
        "_ensure_outcome_projection_receipt",
        crash_once,
    )
    with pytest.raises(RuntimeError, match="outcome receipt crash"):
        service.apply_outcome(
            request,
            principal=principal,
            source_authority_catalog=catalog,
        )

    ledger = PredictionRecordStore(state)
    assert len(state.current_revisions(object_type="outcome_measurement")) == 1
    assert ledger._eligible_outcomes(prediction) == ()

    replay = service.apply_outcome(
        request,
        principal=principal,
        source_authority_catalog=catalog,
    )
    assert replay["status"] in {"committed", "existing"}
    assert len(ledger._eligible_outcomes(prediction)) == 1
    assert ledger.finalize(
        prediction.object_id,
        {},
        observed_at,
    ).terminal_state == "measured"


def test_outcome_cannot_finalize_before_its_declared_maturity(tmp_path):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, observed_at, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
        maturity_delay_hours=2,
    )
    CognitiveStateApplicationService(state).apply_outcome(
        request,
        principal=principal,
        source_authority_catalog=catalog,
    )
    ledger = PredictionRecordStore(state)

    with pytest.raises(ValueError, match="declared maturity"):
        ledger.finalize(prediction.object_id, {}, observed_at)
    matured_at = observed_at + timedelta(hours=2)
    assert ledger.finalize(prediction.object_id, {}, matured_at).terminal_state == "measured"


def test_mature_route_without_exposure_is_censored_not_success(tmp_path):
    _route(_router(tmp_path, window_hours=1))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    mature_at = datetime.fromisoformat(
        prediction.payload["evaluation_window"]["ends_at"]
    ) + timedelta(seconds=1)

    receipt = PredictionRecordStore(state).finalize(
        prediction.object_id,
        {},
        mature_at,
    )

    assert receipt.terminal_state == "censored"
    report = PredictionRecordStore(state).calibration_report()
    assert report.status == "insufficient_sample"
    assert report.censored == 1
    assert report.accuracy is None


def test_terminal_exact_replay_succeeds_and_different_replay_conflicts(tmp_path):
    _route(_router(tmp_path, window_hours=1))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    mature_at = datetime.fromisoformat(
        prediction.payload["evaluation_window"]["ends_at"]
    ) + timedelta(seconds=1)
    ledger = PredictionRecordStore(state)

    terminal = ledger.finalize(prediction.object_id, {}, mature_at)
    replay = ledger.finalize(prediction.object_id, {}, mature_at)

    assert replay.status == "existing"
    assert replay.revision_id == terminal.revision_id
    _record_reaction_exposure(state, prediction)
    with pytest.raises(CognitiveStateConflict, match="different immutable"):
        ledger.finalize(prediction.object_id, {}, mature_at)


def test_terminal_receipt_crash_is_repaired_by_exact_replay(tmp_path, monkeypatch):
    _route(_router(tmp_path, window_hours=1))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    mature_at = datetime.fromisoformat(
        prediction.payload["evaluation_window"]["ends_at"]
    ) + timedelta(seconds=1)
    ledger = PredictionRecordStore(state)
    original = PredictionRecordStore._ensure_terminal_projection_receipt
    crashed = False

    def crash_once(self, revision_id):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("simulated terminal receipt crash")
        return original(self, revision_id)

    monkeypatch.setattr(
        PredictionRecordStore,
        "_ensure_terminal_projection_receipt",
        crash_once,
    )
    with pytest.raises(RuntimeError, match="terminal receipt crash"):
        ledger.finalize(prediction.object_id, {}, mature_at)
    assert state.pending_commands("prediction_calibration_read_model")

    replay = ledger.finalize(prediction.object_id, {}, mature_at)

    assert replay.status == "existing"
    assert not state.pending_commands("prediction_calibration_read_model")


def test_terminal_projection_receipt_rejects_forged_command_binding(
    tmp_path,
    monkeypatch,
):
    _route(_router(tmp_path, window_hours=1))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    mature_at = datetime.fromisoformat(
        prediction.payload["evaluation_window"]["ends_at"]
    ) + timedelta(seconds=1)

    def crash_before_receipt(self, revision_id):
        raise RuntimeError("simulated terminal receipt crash")

    monkeypatch.setattr(
        PredictionRecordStore,
        "_ensure_terminal_projection_receipt",
        crash_before_receipt,
    )
    with pytest.raises(RuntimeError, match="terminal receipt crash"):
        PredictionRecordStore(state).finalize(prediction.object_id, {}, mature_at)
    command = state.pending_commands("prediction_calibration_read_model")[0]

    with pytest.raises(ValueError, match="target does not match"):
        state.record_effect_receipt(
            command["command_id"],
            status="committed",
            target_effect_id="forged-terminal-effect",
            before_hash="sha256:" + "1" * 64,
            after_hash="sha256:" + "2" * 64,
            evidence_refs=("forged-terminal-evidence",),
        )


def test_mature_exposure_without_objective_outcome_is_unknown(tmp_path):
    _route(_router(tmp_path, window_hours=1))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    mature_at = datetime.fromisoformat(
        prediction.payload["evaluation_window"]["ends_at"]
    ) + timedelta(seconds=1)
    _record_reaction_exposure(state, prediction)

    receipt = PredictionRecordStore(state).finalize(
        prediction.object_id,
        {},
        mature_at,
    )

    assert receipt.terminal_state == "unknown"
    terminal = state.current_revision("prediction_record", prediction.object_id)
    assert terminal is not None
    assert terminal.payload["outcome_ref"] == {"revision_id": "", "payload_hash": ""}
    assert terminal.payload["error"] == {"kind": "none", "value": None}


def test_outcome_exact_replay_is_idempotent_and_projection_is_closed(tmp_path):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, _, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
    )
    service = CognitiveStateApplicationService(state)

    first = service.apply_outcome(
        request, principal=principal, source_authority_catalog=catalog
    )
    replay = service.apply_outcome(
        request, principal=principal, source_authority_catalog=catalog
    )

    assert replay["revision_ids"] == first["revision_ids"]
    assert len(state.current_revisions(object_type="outcome_measurement")) == 1
    assert not state.pending_commands("prediction_outcome_projection")


def test_second_independent_outcome_requires_explicit_correction(tmp_path):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    first, principal, _, first_catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
        source_suffix="first",
    )
    second, _, _, second_catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
        source_suffix="second",
        observed_hours=2,
    )
    service = CognitiveStateApplicationService(state)
    service.apply_outcome(
        first,
        principal=principal,
        source_authority_catalog=first_catalog,
    )

    with pytest.raises(CognitiveStateConflict, match="explicit correction"):
        service.apply_outcome(
            second,
            principal=principal,
            source_authority_catalog=second_catalog,
        )

    assert len(state.current_revisions(object_type="outcome_measurement")) == 1


def test_corrected_outcome_appends_outcome_and_terminal_revisions(
    tmp_path,
    monkeypatch,
):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    first_request, principal, observed_at, first_catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
    )
    service = CognitiveStateApplicationService(state)
    first = service.apply_outcome(
        first_request,
        principal=principal,
        source_authority_catalog=first_catalog,
    )
    first_outcome_id = first["revision_ids"][0]
    first_outcome = state.revision(first_outcome_id)
    assert first_outcome is not None
    terminal = PredictionRecordStore(state).finalize(
        prediction.object_id,
        {},
        observed_at,
    )
    correction_request, _, corrected_at, correction_catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
        observed_value="not_useful",
        source_suffix="c",
        observed_hours=2,
        correction_of_revision_id=first_outcome_id,
    )

    ensure_correction = service._ensure_prediction_correction_receipt  # noqa: SLF001

    def crash_after_outcome_commit(_outcome_revision_id):
        raise RuntimeError("injected prediction correction crash")

    monkeypatch.setattr(
        service,
        "_ensure_prediction_correction_receipt",
        crash_after_outcome_commit,
    )
    with pytest.raises(RuntimeError, match="prediction correction crash"):
        service.apply_outcome(
            correction_request,
            principal=principal,
            source_authority_catalog=correction_catalog,
        )
    corrected_outcome = state.current_revision(
        "outcome_measurement",
        first_outcome.object_id,
    )
    assert corrected_outcome is not None
    corrected_outcome_id = corrected_outcome.revision_id
    read_only_principal = PrincipalEnvelope(
        principal_id=principal.principal_id,
        agent=principal.agent,
        host_kind=principal.host_kind,
        capability_id="prediction-read-only-test",
        capabilities=frozenset({"memory_read"}),
        allowed_projects=principal.allowed_projects,
    )
    correction_command = {
        "correction_of_revision_id": terminal.revision_id,
        "outcome_revision_id": corrected_outcome_id,
        "evaluated_at": corrected_at.isoformat(),
    }
    with pytest.raises(
        PermissionError,
        match="principal_write_capability_missing",
    ):
        PredictionRecordStore(state).correct_terminal(
            prediction.object_id,
            correction_command,
            read_only_principal,
        )
    monkeypatch.setattr(
        service,
        "_ensure_prediction_correction_receipt",
        ensure_correction,
    )
    recovery = service.reconcile_outcome_projections()
    assert recovery["committed"] == 1
    assert recovery["failed"] == recovery["remaining"] == 0

    outcome_revision = state.revision(corrected_outcome_id)
    assert outcome_revision is not None
    assert outcome_revision.correction_of_revision_id == first_outcome_id
    assert outcome_revision.supersedes_revision_id == first_outcome_id
    current = state.current_revision("prediction_record", prediction.object_id)
    assert current is not None
    assert current.correction_of_revision_id == terminal.revision_id
    assert current.payload["outcome_ref"]["revision_id"] == corrected_outcome_id
    assert current.payload["error"]["value"] == 1

    corrected_terminal_id = current.revision_id
    second_request, _, _second_at, second_catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
        observed_value="useful",
        source_suffix="d",
        observed_hours=3,
        correction_of_revision_id=corrected_outcome_id,
    )
    second_outcome = service.apply_outcome(
        second_request,
        principal=principal,
        source_authority_catalog=second_catalog,
    )
    second_terminal = state.current_revision("prediction_record", prediction.object_id)
    assert second_terminal is not None
    assert second_terminal.correction_of_revision_id == corrected_terminal_id
    assert second_terminal.payload["outcome_ref"]["revision_id"] == (
        second_outcome["revision_ids"][0]
    )
    assert second_terminal.payload["error"]["value"] == 0


def test_feedback_audit_recomputes_objective_outcome_payload_hash(tmp_path):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, observed_at, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
    )
    CognitiveStateApplicationService(state).apply_outcome(
        request,
        principal=principal,
        source_authority_catalog=catalog,
    )
    outcome = state.current_revisions(object_type="outcome_measurement")[0]
    owner = FeedbackAttributionStore(
        state,
        clock=lambda: observed_at.isoformat(),
        target_adapters=build_gated_feedback_target_adapters(tmp_path),
    )
    owner.record_objective_outcome(outcome, principal)
    owner.replay_pending(limit=100)
    with sqlite3.connect(state.db_path) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM cognitive_state_revisions "
                "WHERE revision_id=?",
                (outcome.revision_id,),
            ).fetchone()[0]
        )
        payload["observed_value"] = "forged-opposite-result"
        conn.execute("DROP TRIGGER cognitive_state_revisions_no_update")
        conn.execute(
            "UPDATE cognitive_state_revisions SET payload_json=? "
            "WHERE revision_id=?",
            (canonical_json(payload), outcome.revision_id),
        )

    report = audit_feedback_attribution(
        database_dir=tmp_path,
        repo_root=Path(__file__).resolve().parents[3],
    )

    assert report["ok"] is False
    assert report["metrics"]["feedback_schema_registry_mismatch"] > 0


def test_confounded_measurement_is_excluded_from_calibration(tmp_path):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, observed_at, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
        competing_causes=("concurrent policy change",),
    )
    CognitiveStateApplicationService(state).apply_outcome(
        request,
        principal=principal,
        source_authority_catalog=catalog,
    )

    receipt = PredictionRecordStore(state).finalize(
        prediction.object_id,
        {},
        observed_at,
    )
    report = PredictionRecordStore(state).calibration_report()

    assert receipt.terminal_state == "confounded"
    assert report.confounded == 1
    assert report.calibration_eligible == 0
    assert report.exclusions == {"confounded_attribution": 1}


def test_calibration_report_exposes_recomputable_identity_and_coverage(tmp_path):
    _route(_router(tmp_path, window_hours=1))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    mature_at = datetime.fromisoformat(
        prediction.payload["evaluation_window"]["ends_at"]
    ) + timedelta(seconds=1)
    PredictionRecordStore(state).finalize(prediction.object_id, {}, mature_at)

    report = PredictionRecordStore(state).calibration_report()

    assert report.method_version == "v1"
    assert report.code_hash.startswith("sha256:")
    assert report.spec_hash.startswith("sha256:")
    assert report.coverage_ratios == {
        "measured": 0.0,
        "unknown": 0.0,
        "censored": 1.0,
        "confounded": 0.0,
    }


def test_calibration_report_applies_timestamp_filters(tmp_path):
    _route(_router(tmp_path, window_hours=1))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    mature_at = datetime.fromisoformat(
        prediction.payload["evaluation_window"]["ends_at"]
    ) + timedelta(seconds=1)
    ledger = PredictionRecordStore(state)
    ledger.finalize(prediction.object_id, {}, mature_at)

    included = ledger.calibration_report(
        {
            "starts_at": (mature_at - timedelta(seconds=1)).isoformat(),
            "ends_at": (mature_at + timedelta(seconds=1)).isoformat(),
        }
    )
    excluded = ledger.calibration_report(
        {"starts_at": (mature_at + timedelta(seconds=1)).isoformat()}
    )

    assert included.censored == 1
    assert excluded.censored == 0


@pytest.mark.parametrize("statistic", ("brier_score", "ece"))
def test_non_probability_score_band_rejects_probability_statistics(
    tmp_path,
    statistic,
):
    _route(_router(tmp_path))
    ledger = PredictionRecordStore(tmp_path / "producer_consumer_ledger.db")

    with pytest.raises(ValueError, match="probabilistic prediction method"):
        ledger.calibration_report({"statistics": [statistic]})


def test_confounded_outcome_requires_explicit_raw_attribution_evidence(tmp_path):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, _, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
        competing_causes=("concurrent policy change",),
        omit_competing_cause_evidence=True,
    )

    with pytest.raises(ValueError, match="per-cause evidence"):
        CognitiveStateApplicationService(state).apply_outcome(
            request,
            principal=principal,
            source_authority_catalog=catalog,
        )


def test_outcome_authority_must_be_selected_from_the_exact_source_catalog(tmp_path):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, _, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
    )
    wrong_request, _, _, wrong_catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
        source_suffix="wrong",
    )
    del wrong_request
    self_reported = json.loads(json.dumps(request))
    self_reported["measurement"]["source_authority"] = {
        "authority": "tool_observation"
    }
    service = CognitiveStateApplicationService(state)

    with pytest.raises(ValueError, match="exact catalog id"):
        service.apply_outcome(
            self_reported,
            principal=principal,
            source_authority_catalog=catalog,
        )
    with pytest.raises(ValueError, match="absent from the catalog"):
        service.apply_outcome(
            request,
            principal=principal,
            source_authority_catalog=wrong_catalog,
        )
    assert not state.current_revisions(object_type="outcome_measurement")


def test_reaction_source_cannot_masquerade_as_objective_measurement(tmp_path):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, _, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
    )
    request["source"]["source_kind"] = "user_reaction"
    request["source"]["source_uri"] = "reaction://clicked"
    request["source"]["evidence_refs"] = ["user-reaction:clicked"]

    with pytest.raises(ValueError, match="objective measurement source"):
        CognitiveStateApplicationService(state).apply_outcome(
            request,
            principal=principal,
            source_authority_catalog=catalog,
        )

    assert not state.current_revisions(object_type="outcome_measurement")


def test_material_permit_rejects_prediction_revision_hash_drift(tmp_path):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    with sqlite3.connect(state.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT command_id, payload_json FROM cognitive_state_outbox "
            "WHERE command_type='execute_material_action'"
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row["payload_json"]))
        payload["prediction_refs"][0]["prediction_revision_hash"] = (
            "sha256:" + "f" * 64
        )
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='cognitive_state_outbox_no_update'"
        ).fetchone()[0]
        conn.execute("DROP TRIGGER cognitive_state_outbox_no_update")
        conn.execute(
            "UPDATE cognitive_state_outbox SET payload_json=?, payload_hash=? "
            "WHERE command_id=?",
            (
                canonical_json(payload),
                sha256_json(payload),
                str(row["command_id"]),
            ),
        )
        conn.execute(str(trigger_sql))

    with pytest.raises(RuntimeError, match="prediction revision hash"):
        MaterialActionCoordinator(state).bind_for_recovery(
            str(row["command_id"]),
            executor_id=str(payload["executor"]),
        )


@pytest.mark.parametrize("field", ("metric", "subject", "access_control"))
def test_outcome_rejects_caller_owned_prediction_bindings(tmp_path, field):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, _, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
    )
    request["measurement"][field] = {"tampered": True}

    with pytest.raises(ValueError, match="server-owned"):
        CognitiveStateApplicationService(state).apply_outcome(
            request,
            principal=principal,
            source_authority_catalog=catalog,
        )


def test_outcome_rejects_caller_supplied_measurement_semantics(tmp_path):
    _route(_router(tmp_path))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    request, principal, _, catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
    )
    request["measurement"]["observed_value"] = "not_useful"
    request["measurement"]["measurement_method"] = {
        "method_id": "task_result_oracle"
    }

    with pytest.raises(ValueError, match="oracle-issued"):
        CognitiveStateApplicationService(state).apply_outcome(
            request,
            principal=principal,
            source_authority_catalog=catalog,
        )

    assert not state.current_revisions(object_type="outcome_measurement")


def test_outcome_rejects_late_window_and_ineligible_authority(tmp_path):
    _route(_router(tmp_path, window_hours=1))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    late, principal, _, late_catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
        observed_hours=2,
    )
    ineligible, _, _, ineligible_catalog = _objective_outcome_request(
        prediction,
        tmp_path / "raw_events.db",
        authority="assistant_inference",
    )
    service = CognitiveStateApplicationService(state)

    with pytest.raises(ValueError, match="outside the prediction window"):
        service.apply_outcome(
            late, principal=principal, source_authority_catalog=late_catalog
        )
    with pytest.raises(ValueError, match="source authority is ineligible"):
        service.apply_outcome(
            ineligible,
            principal=principal,
            source_authority_catalog=ineligible_catalog,
        )
    assert not state.current_revisions(object_type="outcome_measurement")


def test_prediction_maturity_daemon_is_bounded_and_restart_safe(tmp_path):
    class _Config:
        database_dir = tmp_path

        @staticmethod
        def get(key, default=None):
            values = {
                "daemon.services.prediction_maturity": True,
                "prediction.maturity_batch_limit": 1,
                "prediction.predictive_delivery_window_hours": 1,
            }
            return values.get(key, default)

    router = KnowledgeDeliveryRouter(
        db_path=tmp_path / "delivery_events.db",
        database_dir=tmp_path,
        config=_Config(),
        policy=DeliveryBudgetPolicy(same_topic_cooldown_hours=0),
        trust_scorer=_Trust("deliver"),
    )
    first = _route(router)
    principal, source_access = _predictive_access("prediction-ledger-two")
    second = router.route_candidate(
        source="predictive_push",
        subject="prediction-ledger-two",
        channel="predictive_push",
        target="03-Tech/prediction-ledger-two.md",
        evidence_refs=["wiki:prediction-ledger-two"],
        task_fit_score=0.9,
        cooldown_key="prediction-ledger-two",
        source_access_control=source_access,
        principal=principal,
    )
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    mature_at = max(
        datetime.fromisoformat(
            value.payload["evaluation_window"]["ends_at"]
        )
        for value in state.current_revisions(object_type="prediction_record")
    ) + timedelta(seconds=1)
    errors = []

    one = run_prediction_service(errors.append, config=_Config(), now=mature_at)
    two = run_prediction_service(errors.append, config=_Config(), now=mature_at)
    replay = run_prediction_service(errors.append, config=_Config(), now=mature_at)

    assert first.event_id != second.event_id
    assert one["selected"] == two["selected"] == 1
    assert one["remaining_mature_open"] == 1
    assert two["remaining_mature_open"] == 0
    assert replay["selected"] == 0
    assert errors == []


def test_prediction_maturity_batch_terminalizes_permanent_object_failure(
    tmp_path, monkeypatch
):
    router = _router(tmp_path, window_hours=1)
    _route(router)
    principal, source_access = _predictive_access("prediction-ledger-two")
    router.route_candidate(
        source="predictive_push",
        subject="prediction-ledger-two",
        channel="predictive_push",
        target="03-Tech/prediction-ledger-two.md",
        evidence_refs=["wiki:prediction-ledger-two"],
        task_fit_score=0.9,
        cooldown_key="prediction-ledger-two",
        source_access_control=source_access,
        principal=principal,
    )
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    predictions = state.current_revisions(object_type="prediction_record")
    failed_prediction_id = predictions[0].object_id
    mature_at = max(
        datetime.fromisoformat(value.payload["evaluation_window"]["ends_at"])
        for value in predictions
    ) + timedelta(seconds=1)
    original = PredictionRecordStore.finalize

    def fail_one_object(self, prediction_id, *args, **kwargs):
        if prediction_id == failed_prediction_id:
            raise ValueError("simulated permanent semantic failure")
        return original(self, prediction_id, *args, **kwargs)

    monkeypatch.setattr(PredictionRecordStore, "finalize", fail_one_object)
    ledger = PredictionRecordStore(state)

    first = ledger.reconcile_matured(mature_at, limit=10)
    replay = ledger.reconcile_matured(mature_at, limit=10)

    assert first.selected == 2
    assert first.failed == 1
    assert first.terminal_failed == 1
    assert first.retryable_failed == 0
    assert first.terminal_failed_prediction_ids == (failed_prediction_id,)
    assert first.remaining_mature_open == 0
    assert len(first.revision_ids) == 2
    failed = state.current_revision("prediction_record", failed_prediction_id)
    assert failed is not None
    assert failed.payload["revision_state"] == "terminal"
    assert failed.payload["terminal"]["reason"].startswith(
        "maturity_permanent_failure:ValueError:"
    )
    assert replay.selected == 0
    assert replay.failed == 0
    assert replay.remaining_mature_open == 0


def test_prediction_maturity_batch_retries_only_transient_store_failure(
    tmp_path, monkeypatch
):
    router = _router(tmp_path, window_hours=1)
    _route(router)
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    mature_at = datetime.fromisoformat(
        prediction.payload["evaluation_window"]["ends_at"]
    ) + timedelta(seconds=1)
    original = PredictionRecordStore.finalize
    failed_once = False

    def fail_once(self, prediction_id, *args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise OSError("simulated transient store outage")
        return original(self, prediction_id, *args, **kwargs)

    monkeypatch.setattr(PredictionRecordStore, "finalize", fail_once)
    ledger = PredictionRecordStore(state)

    first = ledger.reconcile_matured(mature_at, limit=10)
    replay = ledger.reconcile_matured(mature_at, limit=10)

    assert first.failed == first.retryable_failed == 1
    assert first.terminal_failed == 0
    assert first.retryable_failed_prediction_ids == (prediction.object_id,)
    assert first.remaining_mature_open == 1
    assert replay.failed == 0
    assert replay.remaining_mature_open == 0


def test_prediction_maturity_retries_when_objective_raw_store_is_unavailable(
    tmp_path,
):
    _route(_router(tmp_path, window_hours=1))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    raw_db = tmp_path / "raw_events.db"
    request, principal, _, catalog = _objective_outcome_request(
        prediction,
        raw_db,
    )
    CognitiveStateApplicationService(state).apply_outcome(
        request,
        principal=principal,
        source_authority_catalog=catalog,
    )
    mature_at = datetime.fromisoformat(
        prediction.payload["evaluation_window"]["ends_at"]
    ) + timedelta(seconds=1)
    unavailable_raw = raw_db.with_suffix(".offline")
    raw_db.rename(unavailable_raw)
    ledger = PredictionRecordStore(state)

    first = ledger.reconcile_matured(mature_at, limit=10)

    assert first.selected == 1
    assert first.retryable_failed == 1
    assert first.terminal_failed == 0
    assert first.remaining_mature_open == 1
    assert state.current_revision(
        "prediction_record", prediction.object_id
    ).payload["revision_state"] == "open"

    unavailable_raw.rename(raw_db)
    replay = ledger.reconcile_matured(mature_at, limit=10)
    assert replay.measured == 1
    assert replay.failed == 0
    assert replay.remaining_mature_open == 0


def test_prediction_maturity_terminalizes_permanent_sqlite_integrity_error(
    tmp_path,
    monkeypatch,
):
    _route(_router(tmp_path, window_hours=1))
    state = CognitiveStateStore(tmp_path / "producer_consumer_ledger.db")
    prediction = state.current_revisions(object_type="prediction_record")[0]
    mature_at = datetime.fromisoformat(
        prediction.payload["evaluation_window"]["ends_at"]
    ) + timedelta(seconds=1)

    def fail_integrity(self, prediction_id, *args, **kwargs):
        raise sqlite3.IntegrityError("simulated permanent sqlite constraint failure")

    monkeypatch.setattr(PredictionRecordStore, "finalize", fail_integrity)
    receipt = PredictionRecordStore(state).reconcile_matured(mature_at, limit=10)

    assert receipt.retryable_failed == 0
    assert receipt.terminal_failed == 1
    assert receipt.remaining_mature_open == 0
    terminal = state.current_revision("prediction_record", prediction.object_id)
    assert terminal is not None
    assert terminal.payload["terminal"]["reason"].startswith(
        "maturity_permanent_failure:IntegrityError:"
    )


def test_prediction_maturity_daemon_reports_failed_batch_as_degraded(
    tmp_path, monkeypatch
):
    class _Config:
        database_dir = tmp_path

        @staticmethod
        def get(key, default=None):
            return {
                "daemon.services.prediction_maturity": True,
                "prediction.maturity_batch_limit": 10,
            }.get(key, default)

    _route(_router(tmp_path, window_hours=1))
    prediction_id = CognitiveStateStore(
        tmp_path / "producer_consumer_ledger.db"
    ).current_revisions(object_type="prediction_record")[0].object_id
    monkeypatch.setattr(
        PredictionRecordStore,
        "reconcile_matured",
        lambda self, now=None, limit=100: MaturityBatchReceipt(
            selected=1,
            measured=0,
            unknown=0,
            censored=1,
            confounded=0,
            existing=0,
            failed=1,
            remaining_mature_open=0,
            revision_ids=("cogrev-terminal-failure",),
            failed_prediction_ids=(prediction_id,),
            terminal_failed=1,
            terminal_failed_prediction_ids=(prediction_id,),
        ),
    )

    result = run_prediction_service(
        lambda *_args: None,
        config=_Config(),
    )

    assert result["status"] == "degraded"
    assert result["reason"] == "maturity_batch_has_failures"
    assert result["terminal_failed"] == 1
