"""Canonical DecisionTrace transaction and verification store."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import (
    authorize_cognitive_write,
    cognitive_access_hash,
    validate_cognitive_access_envelope,
)
from core.cognitive.decision_snapshot_access import (
    authorize_decision_snapshot_sources,
    derive_decision_snapshot_access,
)
from core.cognitive.state_contract import (
    COGNITIVE_OBJECT_SCHEMA_VERSIONS,
    CognitiveHeadPrecondition,
    CognitiveStateRevision,
    sha256_json,
)
from core.cognitive.state_store import CognitiveStateConflict, CognitiveStateStore
from core.cognitive.prediction_ledger import PredictionPlan, PredictionRecordStore
from core.cognitive.persona_challenge_contract import (
    build_persona_challenge_command,
)
from core.evidence.source_authority import SourceAuthorityCatalog
from core.ops.cognitive_data_contract import CognitiveDataEvent

from core.cognitive.decision_trace_contracts import (
    DecisionSealReceipt,
    DecisionVerification,
    _digest,
    _raise_unavailable_revision,
    _required,
    _required_dead_letter_supersessions,
    _revision_from_row,
)
from core.cognitive.decision_trace_payloads import (
    _action_command,
    _decision_evidence_refs,
    _decision_payload,
    _material_action_specs,
    _normalize_command,
    _snapshot_payload,
    _value_context_id,
    _value_context_payload,
)
from core.cognitive.decision_trace_verification import (
    _verify_decision_bundle,
    _verify_prediction_refs,
)


class DecisionTraceStore:
    """Deep module that seals values, state, a decision, and action commands."""

    def __init__(self, state_store: CognitiveStateStore):
        self.state_store = state_store

    def seal(
        self,
        command: Mapping[str, Any],
        *,
        principal: PrincipalEnvelope | None,
        source_authority_catalog: SourceAuthorityCatalog,
        prediction_plan: PredictionPlan | None = None,
        prediction_config: Any | None = None,
        persona_revision: Mapping[str, str] | None = None,
        _failpoint: Callable[[str], None] | None = None,
    ) -> DecisionSealReceipt:
        """Atomically seal value, snapshot, decision, and action commands."""

        if not isinstance(source_authority_catalog, SourceAuthorityCatalog):
            raise TypeError("typed SourceAuthorityCatalog is required")
        source_authority_catalog.require_admissible()
        normalized = _normalize_command(
            command,
            source_authority_catalog=source_authority_catalog,
        )
        required_supersessions = _required_dead_letter_supersessions(
            self.state_store.db_path,
            normalized["actions"],
        )
        if tuple(normalized["supersedes_decision_revision_ids"]) != (required_supersessions):
            raise ValueError(
                "material retry must explicitly supersede each latest dead-letter decision"
            )
        source_access = validate_cognitive_access_envelope(
            normalized["source"]["access_control"],
            expected_scope_type=normalized["scope_type"],
            expected_scope_id=normalized["scope_id"],
        )
        if normalized["source"]["source_id"] not in set(
            source_access["consent"]["provenance_refs"]
        ):
            raise ValueError("source access consent does not bind source_id")
        write = authorize_cognitive_write(
            source_access,
            principal=principal,
            scope_type=normalized["scope_type"],
            scope_id=normalized["scope_id"],
        )
        if not write.allowed:
            raise PermissionError(f"decision source access denied: {write.reason}")
        assert principal is not None

        source_scope = source_access["scope"]
        authorized_sources, access_summary = authorize_decision_snapshot_sources(
            self.state_store,
            principal=principal,
            narrowing=AccessNarrowing(
                session_id=str(source_scope["session_id"]),
                project=str(source_scope["project"]),
            ),
            scope_type=normalized["scope_type"],
            scope_id=normalized["scope_id"],
        )
        consumed = tuple(item.revision for item in authorized_sources)
        access_control = derive_decision_snapshot_access(
            source_access,
            authorized_sources,
            owner_principal_id=principal.principal_id,
            owner_agent=principal.agent,
            scope_type=normalized["scope_type"],
            scope_id=normalized["scope_id"],
            retention_policy=str(source_access["retention_policy"]),
        )
        if access_control["scope"]["resolution"] != "resolved":
            raise PermissionError("decision sources have incompatible authorization scopes")

        semantic_input = {
            key: value for key, value in normalized.items() if key not in {"created_at"}
        }
        semantic_input["decision_snapshot_sources"] = [
            {
                "object_type": item.revision.object_type,
                "object_id": item.revision.object_id,
                "revision_id": item.revision.revision_id,
                "payload_hash": item.revision.payload_hash,
                "source_read_purpose": item.source_read_purpose,
                "access_control_hash": item.access_control_hash,
                "source_purpose_contract_hash": item.source_purpose_contract_hash,
            }
            for item in authorized_sources
        ]
        semantic_input["access_control_hash"] = cognitive_access_hash(access_control)
        if persona_revision is not None:
            semantic_input["profile_revision_refs"] = [
                str(persona_revision["revision_id"])
            ]
        decision_id = "decision-" + _digest(semantic_input)[:32]
        event_id = (
            "cogevent-"
            + _digest(
                {
                    "operation": "record_decision",
                    "decision_id": decision_id,
                    "idempotency_key": normalized["idempotency_key"],
                    "source_revision_id": normalized["source"]["source_revision_id"],
                    "source_content_hash": normalized["source"]["content_hash"],
                    "semantic_input_hash": sha256_json(semantic_input),
                }
            )[:32]
        )
        existing_event_id = self._event_for_idempotency_key(normalized["idempotency_key"])
        if existing_event_id and existing_event_id != event_id:
            raise CognitiveStateConflict(
                "decision idempotency key is already bound to different semantics"
            )
        replay = self._receipt_for_event(event_id)
        if replay is not None:
            return replay

        current_value = self.state_store.current_revision(
            "value_context",
            _value_context_id(normalized["scope_type"], normalized["scope_id"]),
        )
        value_payload = _value_context_payload(
            normalized,
            access_control=access_control,
            supersedes_revision_id=(current_value.revision_id if current_value is not None else ""),
        )
        evidence_refs = _decision_evidence_refs(normalized)
        value_revision = CognitiveStateRevision.create(
            object_type="value_context",
            object_id=_value_context_id(
                normalized["scope_type"],
                normalized["scope_id"],
            ),
            source_event_id=event_id,
            source_revision_id=normalized["source"]["source_revision_id"],
            source_content_hash=normalized["source"]["content_hash"],
            scope_type=normalized["scope_type"],
            scope_id=normalized["scope_id"],
            evidence_refs=evidence_refs,
            payload=value_payload,
            supersedes_revision_id=(current_value.revision_id if current_value is not None else ""),
            created_at=normalized["created_at"],
        )

        snapshot_payload = _snapshot_payload(
            normalized,
            value_revision=value_revision,
            consumed=authorized_sources,
            access_summary=access_summary,
            access_control=access_control,
            evidence_refs=evidence_refs,
            profile_revision_refs=(
                (str(persona_revision["revision_id"]),)
                if persona_revision is not None
                else ()
            ),
        )
        snapshot_revision = CognitiveStateRevision.create(
            object_type="cognitive_state_snapshot",
            object_id=str(snapshot_payload["snapshot_id"]),
            source_event_id=event_id,
            source_revision_id=normalized["source"]["source_revision_id"],
            source_content_hash=normalized["source"]["content_hash"],
            scope_type=normalized["scope_type"],
            scope_id=normalized["scope_id"],
            evidence_refs=evidence_refs,
            payload=snapshot_payload,
            created_at=normalized["created_at"],
        )

        action_specs = _material_action_specs(normalized, decision_id=decision_id)
        prediction_refs: tuple[Mapping[str, str], ...]
        if prediction_plan is not None:
            if not isinstance(prediction_plan, PredictionPlan):
                raise TypeError("typed PredictionPlan is required")
            if len(action_specs) != 1:
                raise ValueError("predictive delivery decision requires exactly one action")
            prediction_refs = (prediction_plan.decision_ref(),)
        else:
            prediction_refs = ()
        decision_payload = _decision_payload(
            normalized,
            decision_id=decision_id,
            value_revision=value_revision,
            snapshot_revision=snapshot_revision,
            action_specs=action_specs,
            access_control=access_control,
            prediction_refs=prediction_refs,
        )
        decision_revision = CognitiveStateRevision.create(
            object_type="decision_trace",
            object_id=decision_id,
            source_event_id=event_id,
            source_revision_id=normalized["source"]["source_revision_id"],
            source_content_hash=normalized["source"]["content_hash"],
            scope_type=normalized["scope_type"],
            scope_id=normalized["scope_id"],
            evidence_refs=evidence_refs,
            payload=decision_payload,
            created_at=normalized["created_at"],
        )
        prediction_revisions = (
            (
                PredictionRecordStore(
                    self.state_store,
                    config=prediction_config,
                ).build_atomic_revision(
                    prediction_plan,
                    event_id=event_id,
                    source_revision_id=normalized["source"]["source_revision_id"],
                    source_content_hash=normalized["source"]["content_hash"],
                    decision_revision=decision_revision,
                    action_spec=action_specs[0],
                    access_control=access_control,
                    created_at=normalized["created_at"],
                ),
            )
            if prediction_plan is not None
            else ()
        )
        action_commands = tuple(
            _action_command(
                decision_revision,
                value_revision=value_revision,
                snapshot_revision=snapshot_revision,
                action=action,
                prediction_revisions=prediction_revisions,
                prediction_plan=prediction_plan,
                created_at=normalized["created_at"],
            )
            for action in action_specs
        )
        challenge_command = None
        if (
            persona_revision is not None
            and str(decision_payload["decision_state"]) == "approved"
        ):
            challenge_command = build_persona_challenge_command(
                decision_revision_id=decision_revision.revision_id,
                decision_id=decision_revision.object_id,
                decision_hash=decision_revision.payload_hash,
                candidates=decision_payload["candidates"],
                persona_revision=persona_revision,
                principal={
                    "principal_id": principal.principal_id,
                    "agent": principal.agent,
                },
                scope={
                    "type": decision_revision.scope_type,
                    "id": decision_revision.scope_id,
                },
                created_at=normalized["created_at"],
            )
        commands = (
            (*action_commands, challenge_command)
            if challenge_command is not None
            else action_commands
        )
        consumers = tuple(command.consumer_id for command in commands)
        event = CognitiveDataEvent(
            event_id=event_id,
            source_id=normalized["source"]["source_id"],
            asset_id=normalized["source"]["content_hash"],
            source_kind="material_decision",
            source_uri=normalized["source"]["source_uri"],
            content_hash=normalized["source"]["content_hash"],
            canonical_subject=f"decision_trace:{decision_id}",
            data_type="decision_trace",
            producer="decision_trace_store",
            intended_consumers=consumers,
            privacy_level=str(access_control["sensitivity"]),
            confidence=float(normalized["source"]["confidence"]),
            evidence_refs=evidence_refs,
            dedupe_key=f"decision-trace:{normalized['idempotency_key']}",
            created_at=normalized["created_at"],
            retention_policy=str(access_control["retention_policy"]),
            metadata={
                "revision_ids": [
                    value_revision.revision_id,
                    snapshot_revision.revision_id,
                    decision_revision.revision_id,
                    *(value.revision_id for value in prediction_revisions),
                ],
                "contract_version": COGNITIVE_OBJECT_SCHEMA_VERSIONS["decision_trace"],
                "access_control_hash": cognitive_access_hash(access_control),
            },
        )
        revisions = (
            value_revision,
            snapshot_revision,
            *prediction_revisions,
            decision_revision,
        )
        try:
            committed = self.state_store.unit_of_work().commit(
                revisions=revisions,
                event=event,
                commands=commands,
                expected_heads=tuple(
                    CognitiveHeadPrecondition.create(
                        object_type=revision.object_type,
                        object_id=revision.object_id,
                        revision_id=revision.revision_id,
                    )
                    for revision in consumed
                ),
                failpoint=_failpoint,
            )
        except CognitiveStateConflict:
            concurrent_replay = self._receipt_for_event(event_id)
            if concurrent_replay is not None:
                return concurrent_replay
            raise
        return DecisionSealReceipt(
            status=committed.status,
            event_id=event_id,
            transaction_hash=committed.transaction_hash,
            revision_ids=committed.revision_ids,
            command_ids=committed.outbox_ids,
            value_context=value_revision,
            snapshot=snapshot_revision,
            decision=decision_revision,
            predictions=prediction_revisions,
        )

    def verify(
        self,
        decision_revision_id: str,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None = None,
    ) -> DecisionVerification:
        """Authorize, hydrate, and recompute one complete DecisionTrace bundle."""

        decision, reason = self.state_store.authorized_revision(
            decision_revision_id,
            principal=principal,
            narrowing=narrowing,
            purpose="cognitive_state_read",
        )
        if decision is None:
            _raise_unavailable_revision("decision", reason)
        assert decision is not None
        snapshot_id = _required(
            decision.payload.get("snapshot_revision_id"),
            "snapshot_revision_id",
        )
        value_id = _required(
            decision.payload.get("value_context_revision_id"),
            "value_context_revision_id",
        )
        snapshot, snapshot_reason = self.state_store.authorized_revision(
            snapshot_id,
            principal=principal,
            narrowing=narrowing,
            purpose="cognitive_state_read",
        )
        if snapshot is None:
            _raise_unavailable_revision("snapshot", snapshot_reason)
        value_context, value_reason = self.state_store.authorized_revision(
            value_id,
            principal=principal,
            narrowing=narrowing,
            purpose="cognitive_state_read",
        )
        if value_context is None:
            _raise_unavailable_revision("ValueContext", value_reason)
        assert snapshot is not None and value_context is not None
        _verify_decision_bundle(
            self.state_store,
            decision=decision,
            snapshot=snapshot,
            value_context=value_context,
        )
        action_ids = tuple(str(value) for value in decision.payload["action_refs"])
        effect_ids = tuple(str(value) for value in decision.payload["effect_refs"])
        prediction_revision_ids = _verify_prediction_refs(
            self.state_store,
            decision,
        )
        return DecisionVerification(
            status="verified",
            decision_revision_id=decision.revision_id,
            snapshot_revision_id=snapshot.revision_id,
            value_context_revision_id=value_context.revision_id,
            action_ids=action_ids,
            effect_ids=effect_ids,
            prediction_revision_ids=prediction_revision_ids,
            bundle_hash=sha256_json(
                {
                    "decision_revision_id": decision.revision_id,
                    "decision_hash": decision.payload_hash,
                    "snapshot_revision_id": snapshot.revision_id,
                    "snapshot_hash": snapshot.payload_hash,
                    "value_context_revision_id": value_context.revision_id,
                    "value_context_hash": value_context.payload_hash,
                    "action_ids": list(action_ids),
                    "effect_ids": list(effect_ids),
                    "prediction_revision_ids": list(prediction_revision_ids),
                }
            ),
        )

    def _event_for_idempotency_key(self, idempotency_key: str) -> str:
        if not self.state_store.db_path.is_file():
            return ""
        with self.state_store._connect(read_only=True) as conn:  # noqa: SLF001
            rows = conn.execute(
                """
                SELECT event_id FROM cognitive_data_events
                WHERE dedupe_key=? ORDER BY event_id
                """,
                (f"decision-trace:{idempotency_key}",),
            ).fetchall()
        if len(rows) > 1:
            raise CognitiveStateConflict("decision idempotency key has multiple canonical events")
        return str(rows[0]["event_id"]) if rows else ""

    def _receipt_for_event(self, event_id: str) -> DecisionSealReceipt | None:
        if not self.state_store.db_path.is_file():
            return None
        with self.state_store._connect(read_only=True) as conn:  # noqa: SLF001
            event = conn.execute(
                "SELECT event_id FROM cognitive_data_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if event is None:
                return None
            rows = conn.execute(
                """
                SELECT * FROM cognitive_state_revisions
                WHERE source_event_id=?
                ORDER BY object_type, revision_id
                """,
                (event_id,),
            ).fetchall()
            command_rows = conn.execute(
                """
                SELECT command_id FROM cognitive_state_outbox
                WHERE event_id=?
                ORDER BY CASE WHEN command_type=? THEN 0 ELSE 1 END, command_id
                """,
                (event_id, "execute_material_action"),
            ).fetchall()
        revisions = {
            revision.object_type: revision for revision in (_revision_from_row(row) for row in rows)
        }
        required = {"value_context", "cognitive_state_snapshot", "decision_trace"}
        if not required.issubset(revisions) or set(revisions) - required - {"prediction_record"}:
            raise RuntimeError("decision replay lacks its committed revision or outbox")
        decision_state = str(revisions["decision_trace"].payload["decision_state"])
        if (decision_state == "approved" and not command_rows) or (
            decision_state == "rejected" and command_rows
        ):
            raise RuntimeError("decision replay action cardinality is invalid")
        command_ids = tuple(str(row["command_id"]) for row in command_rows)
        revision_ids = tuple(
            revision.revision_id
            for revision in (
                revisions["value_context"],
                revisions["cognitive_state_snapshot"],
                revisions["decision_trace"],
            )
        )
        predictions = (revisions["prediction_record"],) if "prediction_record" in revisions else ()
        revision_ids = (*revision_ids, *(value.revision_id for value in predictions))
        return DecisionSealReceipt(
            status="existing",
            event_id=event_id,
            transaction_hash=sha256_json(
                {
                    "event_id": event_id,
                    "revision_ids": sorted(revision_ids),
                    "command_ids": sorted(command_ids),
                }
            ),
            revision_ids=revision_ids,
            command_ids=command_ids,
            value_context=revisions["value_context"],
            snapshot=revisions["cognitive_state_snapshot"],
            decision=revisions["decision_trace"],
            predictions=predictions,
        )
