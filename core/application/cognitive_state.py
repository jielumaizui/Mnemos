"""High-leverage application operations for canonical cognitive state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import (
    authorize_cognitive_write,
    cognitive_access_hash,
    derive_strictest_cognitive_access,
    validate_cognitive_access_envelope,
)
from core.cognitive.prediction_ledger import (
    PREDICTION_CORRECTION_COMMAND,
    PREDICTION_CORRECTION_CONSUMER,
    PREDICTION_OUTCOME_COMMAND,
    PREDICTION_OUTCOME_CONSUMER,
    PredictionRecordStore,
    TaskResultOracle,
)
from core.cognitive.state_contract import (
    COGNITIVE_STATE_CONTRACT_VERSION,
    CognitiveHeadPrecondition,
    CognitiveStateRevision,
    LocalConsumerCommand,
    canonical_json,
    sha256_json,
)
from core.cognitive.state_store import CognitiveStateConflict, CognitiveStateStore
from core.evidence.source_authority import (
    SourceAuthorityCatalog,
    load_source_authority_raw_snapshot,
)
from core.ops.cognitive_data_contract import CognitiveDataEvent

READ_MODEL_SCHEMA_VERSION = "mnemos.cognitive_state_read_model.v1"
DECISION_RECEIPT_SCHEMA_VERSION = "mnemos.decision_receipt.v1"
OUTCOME_RECEIPT_SCHEMA_VERSION = "mnemos.outcome_receipt.v1"
DEFAULT_STATE_CONSUMERS = ("wiki", "cognitive_graph")


class CognitiveStateApplicationService:
    """Dict-facing adapter over the typed state store and its UnitOfWork."""

    def __init__(self, db_path_or_config: Path | str | Any):
        self.store = (
            db_path_or_config
            if isinstance(db_path_or_config, CognitiveStateStore)
            else CognitiveStateStore(db_path_or_config)
        )

    def build_cognitive_state(
        self,
        context: Mapping[str, Any] | None = None,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        purpose: str = "cognitive_state_read",
    ) -> dict[str, Any]:
        """Build a zero-write read model through the object-ACL retrieval seam."""

        filters = dict(context or {})
        if not self.store.db_path.is_file():
            return {
                "success": True,
                "schema_version": READ_MODEL_SCHEMA_VERSION,
                "status": "not_initialized",
                "zero_write": True,
                "items": [],
                "state_hash": sha256_json([]),
                "source_completeness": "database_not_initialized",
                "access": {
                    "candidate_count": 0,
                    "authorized_count": 0,
                    "denied_by_reason": {},
                },
            }
        try:
            revisions, access = self.store.authorized_current_revisions(
                principal=principal,
                narrowing=narrowing,
                purpose=purpose,
                object_type=str(filters.get("object_type") or ""),
                object_id=str(filters.get("object_id") or ""),
                scope_type=str(filters.get("scope_type") or ""),
                scope_id=str(filters.get("scope_id") or ""),
            )
        except FileNotFoundError:
            return {
                "success": True,
                "schema_version": READ_MODEL_SCHEMA_VERSION,
                "status": "not_initialized",
                "zero_write": True,
                "items": [],
                "state_hash": sha256_json([]),
                "source_completeness": "database_not_initialized",
                "access": {
                    "candidate_count": 0,
                    "authorized_count": 0,
                    "denied_by_reason": {},
                },
            }
        if not revisions and access["denied_by_reason"]:
            return {
                "success": True,
                "schema_version": READ_MODEL_SCHEMA_VERSION,
                "status": "access_denied",
                "zero_write": True,
                "items": [],
                "state_hash": sha256_json([]),
                "source_completeness": "acl_filtered_before_payload_fetch",
                "access": access,
            }
        items = [_revision_payload(revision) for revision in revisions]
        return {
            "success": True,
            "schema_version": READ_MODEL_SCHEMA_VERSION,
            "status": "available",
            "zero_write": True,
            "items": items,
            "state_hash": sha256_json(items),
            "source_completeness": "canonical_authorized_current_revisions",
            "access": access,
        }

    def revise_belief(
        self,
        request: Mapping[str, Any],
        *,
        principal: PrincipalEnvelope | None,
    ) -> dict[str, Any]:
        """Append one system-identified belief revision from an authorized source."""

        if not isinstance(request, Mapping):
            raise ValueError("belief request must be an object")
        forbidden = {"belief_id", "claim_id", "stance", "access_control"}
        supplied = forbidden.intersection(request)
        if supplied:
            raise ValueError(
                "belief identity, stance, and access_control are server-owned: "
                + ", ".join(sorted(supplied))
            )
        from core.cognitive.belief_revision import (
            BeliefRevisionCommand,
            BeliefRevisionStore,
        )

        source = _source_contract(request.get("source"))
        scope_type, scope_id = _scope_contract(request.get("scope"))
        confidence_raw = request.get("confidence")
        command = BeliefRevisionCommand(
            claim=_required_text(request.get("claim"), "claim"),
            claim_kind=_required_text(request.get("claim_kind"), "claim_kind"),
            scope_type=scope_type,
            scope_id=scope_id,
            source_id=str(source["source_id"]),
            source_revision_id=str(source["source_revision_id"]),
            source_content_hash=str(source["content_hash"]),
            source_access_control=source["access_control"],
            supporting_evidence=_text_sequence(
                request.get("supporting_evidence"),
                "supporting_evidence",
            ),
            opposing_evidence=_text_sequence(
                request.get("opposing_evidence"),
                "opposing_evidence",
            ),
            withdrawn_evidence=_text_sequence(
                request.get("withdrawn_evidence"),
                "withdrawn_evidence",
            ),
            confidence_method=str(request.get("confidence_method") or "unscored"),
            confidence=(float(confidence_raw) if confidence_raw is not None else None),
            confidence_evidence=_text_sequence(
                request.get("confidence_evidence"),
                "confidence_evidence",
            ),
            uncertainty_reasons=_text_sequence(
                request.get("uncertainty_reasons"),
                "uncertainty_reasons",
            ),
            valid_from=str(request.get("valid_from") or source["created_at"]),
            valid_until=str(request.get("valid_until") or ""),
            invalidation_conditions=_text_sequence(
                request.get("invalidation_conditions"),
                "invalidation_conditions",
            ),
            expected_current_revision_id=str(
                request.get("expected_current_revision_id") or ""
            ),
            correction_of_revision_id=str(
                request.get("correction_of_revision_id") or ""
            ),
            correction_evidence_ref=str(
                request.get("correction_evidence_ref") or ""
            ),
            disposition=str(request.get("disposition") or ""),
            proposal_id=str(request.get("proposal_id") or ""),
            journal_id=str(request.get("journal_id") or ""),
            created_at=str(source["created_at"]),
        )
        receipt = BeliefRevisionStore(self.store).revise(
            command,
            principal=principal,
        )
        return {
            "success": True,
            "schema_version": "mnemos.belief_revision_receipt.v1",
            **asdict(receipt),
        }

    def explain_belief(
        self,
        belief_id: str,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
    ) -> dict[str, Any]:
        """Return one ACL-filtered canonical belief explanation."""

        from core.cognitive.belief_revision import BeliefRevisionStore

        explanation = BeliefRevisionStore(self.store).explain(
            belief_id,
            principal=principal,
            narrowing=narrowing,
        )
        return {
            "success": True,
            "schema_version": "mnemos.belief_explanation.v1",
            "status": explanation.status,
            "belief": asdict(explanation),
        }

    def record_decision(
        self,
        trace: Mapping[str, Any],
        *,
        principal: PrincipalEnvelope | None,
        source_authority_catalog: SourceAuthorityCatalog,
    ) -> dict[str, Any]:
        """Seal a system-owned ValueContext, snapshot, and DecisionTrace."""

        from core.cognitive.decision_trace import DecisionTraceStore

        from core.persona.challenge_queue import current_persona_revision_binding

        persona_revision = current_persona_revision_binding(
            self.store.db_path.parent / "user_signals.db"
        )
        return DecisionTraceStore(self.store).seal(
            trace,
            principal=principal,
            source_authority_catalog=source_authority_catalog,
            persona_revision=persona_revision,
        ).to_dict()

    def apply_outcome(
        self,
        feedback: Mapping[str, Any],
        *,
        principal: PrincipalEnvelope | None,
        source_authority_catalog: SourceAuthorityCatalog,
    ) -> dict[str, Any]:
        """Admit one independently evidenced outcome bound to an open prediction."""

        if not isinstance(feedback, Mapping):
            raise ValueError("feedback must be an object")
        if "access_control" in feedback:
            raise ValueError("operation access_control is server-derived from source.access_control")
        if "outcome_id" in feedback:
            raise ValueError("outcome identity is server-owned")
        if "intended_consumers" in feedback:
            raise ValueError("outcome projection ownership is server-owned")
        prediction_revision_id = _required_text(
            feedback.get("prediction_revision_id"),
            "prediction_revision_id",
        )
        prediction = self.store.revision(prediction_revision_id)
        if prediction is None or prediction.object_type != "prediction_record":
            raise ValueError("prediction revision is unavailable")
        if prediction.payload["revision_state"] != "open":
            raise ValueError("outcome must bind an open prediction revision")
        if principal is None:
            raise PermissionError("outcome attribution requires an authenticated principal")
        from core.cognitive.delivery_router import verify_delivery_presentation

        presentation_proof = verify_delivery_presentation(
            self.store.db_path.parent / "delivery_events.db",
            delivery_event_id=str(prediction.payload["delivery_ref"]["event_id"]),
            principal=principal,
            expected_delivery_hash=str(
                prediction.payload["delivery_ref"]["event_payload_hash"]
            ),
        )
        if not presentation_proof["ok"]:
            raise ValueError(
                "outcome requires acknowledged delivery presentation: "
                + str(presentation_proof["reason"])
            )
        source = _source_contract(feedback.get("source"))
        scope_type, scope_id = _scope_contract(feedback.get("scope"))
        if (scope_type, scope_id) != (prediction.scope_type, prediction.scope_id):
            raise ValueError("outcome scope does not match its prediction")
        source_access_control = _derive_operation_access_control(
            source,
            principal=principal,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        prediction_access = validate_cognitive_access_envelope(
            prediction.payload["access_control"],
            expected_scope_type=scope_type,
            expected_scope_id=scope_id,
        )
        assert principal is not None
        access_control = derive_strictest_cognitive_access(
            (prediction_access, source_access_control),
            owner_principal_id=principal.principal_id,
            owner_agent=principal.agent,
            scope_type=scope_type,
            scope_id=scope_id,
            purposes=("cognitive_state_read", "cognitive_state_write"),
            retention_policy=str(source_access_control["retention_policy"]),
        )
        if access_control["scope"]["resolution"] != "resolved":
            raise PermissionError("outcome sources have incompatible authorization scopes")
        correction_of_revision_id = str(
            feedback.get("correction_of_revision_id") or ""
        ).strip()
        prior_outcome: CognitiveStateRevision | None = None
        prior_prediction_terminal_revision_id = ""
        if correction_of_revision_id:
            prior_outcome = self.store.revision(correction_of_revision_id)
            if (
                prior_outcome is None
                or prior_outcome.object_type != "outcome_measurement"
            ):
                raise ValueError("corrected outcome revision is unavailable")
            current_outcome = self.store.current_revision(
                "outcome_measurement",
                prior_outcome.object_id,
            )
            if (
                current_outcome is None
                or (
                    current_outcome.revision_id != prior_outcome.revision_id
                    and (
                        current_outcome.correction_of_revision_id
                        != prior_outcome.revision_id
                        or current_outcome.supersedes_revision_id
                        != prior_outcome.revision_id
                    )
                )
            ):
                raise ValueError("outcome correction must supersede the current revision")
            if prior_outcome.payload["prediction_ref"]["revision_id"] != (
                prediction.revision_id
            ):
                raise ValueError("outcome correction binds a different prediction")
            prediction_head = self.store.current_revision(
                "prediction_record",
                prediction.object_id,
            )
            if (
                prediction_head is None
                or prediction_head.payload["revision_state"] != "terminal"
            ):
                raise ValueError(
                    "outcome correction requires the current terminal prediction"
                )
            prior_outcome_ref = {
                "revision_id": prior_outcome.revision_id,
                "payload_hash": prior_outcome.payload_hash,
            }
            prior_prediction_terminal: CognitiveStateRevision | None
            if prediction_head.payload["outcome_ref"] == prior_outcome_ref:
                prior_prediction_terminal = prediction_head
            else:
                prior_prediction_terminal = self.store.revision(
                    prediction_head.correction_of_revision_id
                )
                if (
                    current_outcome.revision_id == prior_outcome.revision_id
                    or prior_prediction_terminal is None
                    or prior_prediction_terminal.object_type
                    != "prediction_record"
                    or prior_prediction_terminal.object_id
                    != prediction.object_id
                    or prior_prediction_terminal.payload["outcome_ref"]
                    != prior_outcome_ref
                    or prediction_head.payload["outcome_ref"]
                    != {
                        "revision_id": current_outcome.revision_id,
                        "payload_hash": current_outcome.payload_hash,
                    }
                ):
                    raise ValueError(
                        "outcome correction prediction lineage is not replayable"
                    )
            prior_prediction_terminal_revision_id = (
                prior_prediction_terminal.revision_id
            )
        measurement = feedback.get("measurement")
        if not isinstance(measurement, Mapping):
            raise ValueError("measurement must be an object")
        if set(measurement) != {"source_authority"}:
            raise ValueError(
                "outcome measurement is oracle-issued; caller measurement fields "
                "are server-owned"
            )
        source_authority = measurement.get("source_authority")
        if not isinstance(source_authority, Mapping):
            raise ValueError("measurement.source_authority must be an object")
        if set(source_authority) != {"source_authority_id"}:
            raise ValueError("source authority identity is selected by exact catalog id")
        source_authority_catalog.require_admissible()
        if len(source_authority_catalog.entries) != 1:
            raise ValueError("outcome source authority catalog must contain one exact Raw span")
        authority_id = _required_text(
            source_authority.get("source_authority_id"),
            "measurement.source_authority.source_authority_id",
        )
        authority_entry = source_authority_catalog.get(authority_id)
        if authority_entry is None:
            raise ValueError("measurement source authority is absent from the catalog")
        if authority_entry.source_event_id != str(source["source_revision_id"]):
            raise ValueError("measurement source authority binds a different source revision")
        if authority_entry.source_revision_sha256 != str(source["content_hash"]):
            raise ValueError("measurement source authority revision hash mismatch")
        configured_raw_db = ""
        store_config = self.store.config
        if store_config is not None:
            try:
                configured_raw_db = str(
                    store_config.get("raw_event_store.db_path", "") or ""
                )
            except (AttributeError, TypeError):
                configured_raw_db = ""
        raw_db_path = Path(configured_raw_db).expanduser() if configured_raw_db else (
            self.store.db_path.parent / "raw_events.db"
        )
        raw_snapshot = load_source_authority_raw_snapshot(
            authority_entry,
            raw_db_path,
        )
        if raw_snapshot is None:
            raise ValueError("measurement source authority Raw span did not verify")
        if authority_entry.authority.value != "tool_observation":
            raise ValueError("measurement source authority is ineligible")
        issuance = TaskResultOracle.issue(
            source=source,
            prediction=prediction,
            authority_entry=authority_entry,
            raw_snapshot=raw_snapshot,
        )
        issued_measurement = dict(issuance.measurement)
        canonical_method = dict(issuance.measurement_method)
        expected_authority = {
            "source_authority_id": authority_entry.source_authority_id,
            "source_authority_catalog_hash": source_authority_catalog.catalog_hash,
            "source_authority_catalog": source_authority_catalog.canonical_payload(),
            "source_authority_entry": authority_entry.canonical_payload(),
            "authority": authority_entry.authority.value,
            "source_id": str(source["source_id"]),
            "source_revision_id": str(source["source_revision_id"]),
            "content_hash": str(source["content_hash"]),
        }
        raw_evidence = issued_measurement.get("raw_evidence")
        if not isinstance(raw_evidence, Mapping) or authority_entry.content_sha256 not in set(
            str(value) for value in raw_evidence.get("content_hashes", ())
        ):
            raise ValueError("measurement raw evidence does not bind the authority span")
        observation_window = issued_measurement.get("observation_window")
        if not isinstance(observation_window, Mapping):
            raise ValueError("measurement.observation_window must be an object")
        observation_starts = _parse_timestamp(
            observation_window.get("starts_at"),
            "measurement.observation_window.starts_at",
        )
        observation_ends = _parse_timestamp(
            observation_window.get("ends_at"),
            "measurement.observation_window.ends_at",
        )
        prediction_starts = _parse_timestamp(
            prediction.payload["evaluation_window"]["starts_at"],
            "prediction.evaluation_window.starts_at",
        )
        prediction_ends = _parse_timestamp(
            prediction.payload["evaluation_window"]["ends_at"],
            "prediction.evaluation_window.ends_at",
        )
        if (
            observation_starts < prediction_starts
            or observation_ends > prediction_ends
        ):
            raise ValueError("outcome observation is outside the prediction window")
        outcome_id = (
            prior_outcome.object_id
            if prior_outcome is not None
            else "outcome-" + sha256_json(
                {
                    "prediction_id": prediction.object_id,
                    "metric_id": prediction.payload["metric"]["metric_id"],
                    "unit": prediction.payload["metric"]["unit"],
                }
            ).split(":", 1)[1][:32]
        )
        payload = {
            "schema_version": "mnemos.outcome_measurement.v1",
            "outcome_id": outcome_id,
            "prediction_ref": {
                "prediction_id": prediction.object_id,
                "revision_id": prediction.revision_id,
                "prediction_input_hash": prediction.payload["prediction_input_hash"],
            },
            "decision_ref": dict(prediction.payload["decision_ref"]),
            "action_ref": dict(prediction.payload["action_ref"]),
            "delivery_ref": dict(prediction.payload["delivery_ref"]),
            "presentation_ref": {
                "state": "available",
                "receipt_hash": str(presentation_proof["presentation_receipt"]["receipt_hash"]),
                "rendered_content_hash": str(
                    presentation_proof["presentation_receipt"]["rendered_content_hash"]
                ),
                "delivery_event_hash": str(presentation_proof["delivery_effect_hash"]),
            },
            "subject": dict(prediction.payload["subject"]),
            "metric": {
                "metric_id": prediction.payload["metric"]["metric_id"],
                "unit": prediction.payload["metric"]["unit"],
            },
            "baseline": prediction.payload["metric"]["baseline"],
            "observed_value": issued_measurement.get("observed_value"),
            "observation_window": issued_measurement.get("observation_window"),
            "maturity": issued_measurement.get("maturity"),
            "raw_evidence": issued_measurement.get("raw_evidence"),
            "measurement_method": canonical_method,
            "uncertainty": issued_measurement.get("uncertainty"),
            "attribution": issued_measurement.get("attribution"),
            "source_authority": expected_authority,
            "supersedes_revision_id": correction_of_revision_id,
            "correction_of_revision_id": correction_of_revision_id,
            "access_control": access_control,
        }
        event_id = _semantic_event_id(
            operation="apply_outcome",
            object_id=outcome_id,
            source=source,
            semantic_input={"outcome": payload, "access_control": access_control},
        )
        revision = CognitiveStateRevision.create(
            object_type="outcome_measurement",
            object_id=outcome_id,
            source_event_id=event_id,
            source_revision_id=str(source["source_revision_id"]),
            source_content_hash=str(source["content_hash"]),
            scope_type=scope_type,
            scope_id=scope_id,
            evidence_refs=tuple(str(value) for value in source["evidence_refs"]),
            payload=payload,
            supersedes_revision_id=correction_of_revision_id,
            correction_of_revision_id=correction_of_revision_id,
            created_at=str(source["created_at"]),
        )
        current_outcome = self.store.current_revision(
            "outcome_measurement",
            outcome_id,
        )
        if (
            prior_outcome is None
            and current_outcome is not None
            and current_outcome.revision_id != revision.revision_id
        ):
            raise CognitiveStateConflict(
                "a second independent outcome requires an explicit correction"
            )
        if (
            prior_outcome is not None
            and current_outcome is not None
            and current_outcome.revision_id
            not in {prior_outcome.revision_id, revision.revision_id}
        ):
            raise CognitiveStateConflict(
                "outcome correction is already bound to different semantics"
            )
        projection_effect_id = "prediction-outcome-effect-" + sha256_json(
            {
                "outcome_id": outcome_id,
                "revision_id": revision.revision_id,
                "payload_hash": revision.payload_hash,
            }
        ).split(":", 1)[1][:32]
        outcome_projection_payload = {
            "schema_version": "mnemos.prediction_outcome_projection.v2",
            "outcome_id": outcome_id,
            "outcome_revision_id": revision.revision_id,
            "outcome_revision_hash": revision.payload_hash,
            "prediction_id": prediction.object_id,
            "prediction_revision_id": prediction.revision_id,
            "correction_of_outcome_revision_id": correction_of_revision_id,
            "prior_prediction_terminal_revision_id": (
                prior_prediction_terminal_revision_id
            ),
            "oracle_issuance_hash": issuance.issuance_hash,
            "oracle_source_revision_id": str(source["source_revision_id"]),
            "oracle_source_content_hash": str(source["content_hash"]),
            "projection_effect_id": projection_effect_id,
        }
        command_specs: list[tuple[str, str, Mapping[str, Any]]] = [
            (
                PREDICTION_OUTCOME_CONSUMER,
                PREDICTION_OUTCOME_COMMAND,
                outcome_projection_payload,
            )
        ]
        if correction_of_revision_id:
            correction_effect_id = (
                "prediction-terminal-correction-effect-"
                + sha256_json(
                    {
                        "outcome_revision_id": revision.revision_id,
                        "prior_prediction_terminal_revision_id": (
                            prior_prediction_terminal_revision_id
                        ),
                    }
                ).split(":", 1)[1][:32]
            )
            prior_terminal = self.store.revision(
                prior_prediction_terminal_revision_id
            )
            if prior_terminal is None:
                raise ValueError(
                    "outcome correction prior terminal is unavailable"
                )
            command_specs.append(
                (
                    PREDICTION_CORRECTION_CONSUMER,
                    PREDICTION_CORRECTION_COMMAND,
                    {
                        "schema_version": (
                            "mnemos.prediction_terminal_correction.v1"
                        ),
                        "outcome_revision_id": revision.revision_id,
                        "outcome_revision_hash": revision.payload_hash,
                        "correction_of_outcome_revision_id": (
                            correction_of_revision_id
                        ),
                        "prediction_id": prediction.object_id,
                        "prior_prediction_terminal_revision_id": (
                            prior_terminal.revision_id
                        ),
                        "prior_prediction_terminal_hash": (
                            prior_terminal.payload_hash
                        ),
                        "correction_effect_id": correction_effect_id,
                    },
                )
            )
        commit = self._commit(
            event_id=event_id,
            primary_revision=revision,
            revisions=(revision,),
            source=source,
            command_specs=tuple(command_specs),
            expected_heads=(
                (
                    CognitiveHeadPrecondition.create(
                        object_type="outcome_measurement",
                        object_id=outcome_id,
                        revision_id=correction_of_revision_id,
                    ),
                )
                if correction_of_revision_id
                and (
                    current_outcome is None
                    or current_outcome.revision_id == correction_of_revision_id
                )
                else ()
            ),
        )
        self._ensure_outcome_projection_receipt(revision.revision_id)
        if correction_of_revision_id:
            self._ensure_prediction_correction_receipt(
                revision.revision_id
            )
        return {
            "success": True,
            "schema_version": OUTCOME_RECEIPT_SCHEMA_VERSION,
            "status": commit.status,
            "event_id": commit.event_id,
            "transaction_hash": commit.transaction_hash,
            "revision_ids": list(commit.revision_ids),
            "outbox_ids": list(commit.outbox_ids),
            "outcome": _revision_payload(revision),
        }

    def reconcile_outcome_projections(self, limit: int = 100) -> dict[str, Any]:
        """Replay one bounded page of durable Outcome projection obligations."""

        bounded_limit = int(limit)
        if bounded_limit <= 0 or bounded_limit > 1000:
            raise ValueError("outcome projection reconciliation limit must be in [1, 1000]")
        selected = (
            self.store.pending_commands(PREDICTION_OUTCOME_CONSUMER)
            + self.store.pending_commands(PREDICTION_CORRECTION_CONSUMER)
        )[:bounded_limit]
        committed: list[str] = []
        failed: list[str] = []
        for command in selected:
            command_id = str(command["command_id"])
            try:
                if command["command_type"] == PREDICTION_OUTCOME_COMMAND:
                    self._ensure_outcome_projection_receipt(
                        str(command["revision_id"])
                    )
                elif command["command_type"] == PREDICTION_CORRECTION_COMMAND:
                    self._ensure_prediction_correction_receipt(
                        str(command["revision_id"])
                    )
                else:
                    raise ValueError("unknown outcome projection command")
            except (OSError, RuntimeError, TypeError, ValueError):
                failed.append(command_id)
            else:
                committed.append(command_id)
        return {
            "selected": len(selected),
            "committed": len(committed),
            "failed": len(failed),
            "remaining": len(
                self.store.pending_commands(PREDICTION_OUTCOME_CONSUMER)
            )
            + len(self.store.pending_commands(PREDICTION_CORRECTION_CONSUMER)),
            "committed_command_ids": committed,
            "failed_command_ids": failed,
        }

    def _commit(
        self,
        *,
        event_id: str,
        primary_revision: CognitiveStateRevision,
        revisions: Sequence[CognitiveStateRevision],
        source: Mapping[str, Any],
        command_specs: Sequence[tuple[str, str, Mapping[str, Any]]],
        expected_heads: Sequence[CognitiveHeadPrecondition] = (),
    ):
        revision_ids = tuple(revision.revision_id for revision in revisions)
        event = CognitiveDataEvent(
            event_id=event_id,
            source_id=str(source["source_id"]),
            asset_id=str(source.get("asset_id") or ""),
            source_kind=str(source["source_kind"]),
            source_uri=str(source["source_uri"]),
            content_hash=str(source["content_hash"]),
            canonical_subject=f"{primary_revision.object_type}:{primary_revision.object_id}",
            data_type=primary_revision.object_type,
            producer="cognitive_state_store",
            intended_consumers=tuple(
                consumer_id
                for consumer_id, _command_type, _payload in command_specs
            ),
            privacy_level=str(source.get("privacy_level") or "private"),
            confidence=float(source.get("confidence", 1.0)),
            evidence_refs=tuple(str(value) for value in source["evidence_refs"]),
            dedupe_key=f"cognitive-state:{event_id}",
            created_at=str(source["created_at"]),
            retention_policy=str(source.get("retention_policy") or "cognitive_state"),
            metadata={
                "revision_ids": list(revision_ids),
                "contract_version": COGNITIVE_STATE_CONTRACT_VERSION,
                "access_control_hash": cognitive_access_hash(primary_revision.payload["access_control"]),
            },
        )
        commands = tuple(
            LocalConsumerCommand.create(
                revision_id=primary_revision.revision_id,
                consumer_id=consumer_id,
                command_type=command_type,
                payload=dict(command_payload),
                created_at=str(source["created_at"]),
            )
            for consumer_id, command_type, command_payload in command_specs
        )
        return self.store.unit_of_work().commit(
            revisions=tuple(revisions),
            event=event,
            commands=commands,
            expected_heads=tuple(expected_heads),
        )

    def _ensure_outcome_projection_receipt(self, revision_id: str) -> None:
        """Close the canonical outcome read-model command with exact evidence."""

        with self.store._connect(read_only=True) as conn:  # noqa: SLF001
            row = conn.execute(
                """
                SELECT o.command_id, o.payload_json,
                       r.command_id AS receipt_command_id
                FROM cognitive_state_outbox AS o
                LEFT JOIN cognitive_state_effect_receipts AS r
                  ON r.command_id=o.command_id
                WHERE o.revision_id=? AND o.command_type=?
                """,
                (revision_id, PREDICTION_OUTCOME_COMMAND),
            ).fetchone()
        if row is None or row["receipt_command_id"] is not None:
            return
        payload = json.loads(str(row["payload_json"]))
        before_hash = sha256_json(
            {"outcome_id": payload["outcome_id"], "state": "unprojected"}
        )
        after_hash = str(payload["outcome_revision_hash"])
        issuance_hash = str(payload["oracle_issuance_hash"])
        source_revision_id = str(payload["oracle_source_revision_id"])
        source_content_hash = str(payload["oracle_source_content_hash"])
        command_id = str(row["command_id"])
        self.store.record_effect_receipt(
            command_id,
            status="committed",
            target_effect_id=str(payload["projection_effect_id"]),
            before_hash=before_hash,
            after_hash=after_hash,
            evidence_refs=(
                f"prediction-outcome-command:{command_id}",
                f"outcome-revision:{revision_id}",
                f"prediction-revision:{payload['prediction_revision_id']}",
                f"objective-oracle-issuance:{issuance_hash}",
                "objective-oracle-source:"
                f"{source_revision_id}:{source_content_hash}",
                f"prediction-outcome-projection:{after_hash}",
            ),
            outcome="canonical prediction outcome read model available",
        )

    def _ensure_prediction_correction_receipt(
        self,
        outcome_revision_id: str,
    ) -> None:
        """Close one durable terminal-correction command after exact projection."""

        with self.store._connect(read_only=True) as conn:  # noqa: SLF001
            row = conn.execute(
                """
                SELECT o.command_id, o.payload_json,
                       r.command_id AS receipt_command_id
                FROM cognitive_state_outbox AS o
                LEFT JOIN cognitive_state_effect_receipts AS r
                  ON r.command_id=o.command_id
                WHERE o.revision_id=? AND o.command_type=?
                """,
                (outcome_revision_id, PREDICTION_CORRECTION_COMMAND),
            ).fetchone()
        if row is None or row["receipt_command_id"] is not None:
            return
        payload = json.loads(str(row["payload_json"]))
        prior_terminal = self.store.revision(
            str(payload["prior_prediction_terminal_revision_id"])
        )
        if (
            prior_terminal is None
            or prior_terminal.payload_hash
            != payload["prior_prediction_terminal_hash"]
        ):
            raise ValueError("prediction correction prior terminal mismatch")
        corrected = self._ensure_corrected_prediction_terminal(
            outcome_revision_id=outcome_revision_id,
            prior_terminal_revision_id=prior_terminal.revision_id,
        )
        terminal_commands = tuple(
            command
            for command in self.store.commands_for_revision(
                corrected.revision_id
            )
            if command["command_type"] == "project_prediction_terminal"
        )
        if len(terminal_commands) != 1:
            raise RuntimeError("corrected prediction terminal command gap")
        terminal_receipt = self.store.effect_receipt(
            str(terminal_commands[0]["command_id"])
        )
        if terminal_receipt is None:
            raise RuntimeError("corrected prediction terminal receipt gap")
        command_id = str(row["command_id"])
        self.store.record_effect_receipt(
            command_id,
            status="committed",
            target_effect_id=str(payload["correction_effect_id"]),
            before_hash=prior_terminal.payload_hash,
            after_hash=corrected.payload_hash,
            evidence_refs=(
                f"prediction-terminal-correction-command:{command_id}",
                f"outcome-revision:{outcome_revision_id}",
                "prior-prediction-terminal:"
                f"{prior_terminal.revision_id}:{prior_terminal.payload_hash}",
                "corrected-prediction-terminal:"
                f"{corrected.revision_id}:{corrected.payload_hash}",
                "prediction-terminal-effect-receipt:"
                + str(terminal_receipt["receipt_id"]),
            ),
            outcome="corrected prediction terminal available",
        )

    def _ensure_corrected_prediction_terminal(
        self,
        *,
        outcome_revision_id: str,
        prior_terminal_revision_id: str,
    ) -> CognitiveStateRevision:
        """Append or verify the exact terminal correction before receipt closure."""

        outcome = self.store.revision(str(outcome_revision_id or ""))
        prior_terminal = self.store.revision(
            str(prior_terminal_revision_id or "")
        )
        if (
            outcome is None
            or outcome.object_type != "outcome_measurement"
            or prior_terminal is None
            or prior_terminal.object_type != "prediction_record"
            or outcome.correction_of_revision_id
            != prior_terminal.payload["outcome_ref"]["revision_id"]
            or outcome.payload["correction_of_revision_id"]
            != outcome.correction_of_revision_id
        ):
            raise ValueError("corrected outcome terminal source is invalid")
        current = self.store.current_revision(
            "prediction_record",
            prior_terminal.object_id,
        )
        if current is None:
            raise ValueError("corrected outcome prediction head is unavailable")
        ledger = PredictionRecordStore(self.store)
        if current.revision_id == prior_terminal.revision_id:
            access = validate_cognitive_access_envelope(
                outcome.payload["access_control"],
                expected_scope_type=outcome.scope_type,
                expected_scope_id=outcome.scope_id,
            )
            project = str(access["scope"]["project"])
            principal = PrincipalEnvelope(
                principal_id=str(access["owner"]["principal_id"]),
                agent=str(access["owner"]["agent"]),
                host_kind="prediction_outcome_projection",
                capability_id="durable-outcome-terminal-correction",
                capabilities=frozenset({"memory_read", "memory_write"}),
                allowed_projects=frozenset({project}),
            )
            receipt = ledger.correct_terminal(
                prior_terminal.object_id,
                {
                    "correction_of_revision_id": prior_terminal.revision_id,
                    "outcome_revision_id": outcome.revision_id,
                    "evaluated_at": outcome.payload["maturity"]["matured_at"],
                },
                principal,
            )
            corrected = self.store.revision(receipt.revision_id)
        else:
            corrected = current
            ledger.finalize(
                current.object_id,
                {},
                current.payload["terminal"]["evaluated_at"],
            )
        if (
            corrected is None
            or corrected.correction_of_revision_id
            != prior_terminal.revision_id
            or corrected.supersedes_revision_id != prior_terminal.revision_id
            or corrected.payload["revision_state"] != "terminal"
            or corrected.payload["outcome_ref"]
            != {
                "revision_id": outcome.revision_id,
                "payload_hash": outcome.payload_hash,
            }
            or self.store.current_revision(
                "prediction_record",
                prior_terminal.object_id,
            )
            != corrected
        ):
            raise ValueError("corrected outcome prediction terminal mismatch")
        return corrected


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    text = _required_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed


def _source_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("source must be an object")
    source = dict(value)
    for field_name in (
        "source_id",
        "source_revision_id",
        "source_kind",
        "source_uri",
        "content_hash",
        "created_at",
    ):
        source[field_name] = _required_text(source.get(field_name), f"source.{field_name}")
    refs = source.get("evidence_refs")
    if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
        raise ValueError("source.evidence_refs must be a sequence")
    source["evidence_refs"] = tuple(
        _required_text(value, "source.evidence_refs") for value in refs
    )
    if not source["evidence_refs"]:
        raise ValueError("source.evidence_refs must be non-empty")
    if not isinstance(source.get("access_control"), Mapping):
        raise ValueError("source.access_control must be an object")
    confidence = float(source.get("confidence", 1.0))
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("source.confidence must be between 0 and 1")
    source["confidence"] = confidence
    return source


def _scope_contract(value: Any) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("scope must be an object")
    return (
        _required_text(value.get("type"), "scope.type"),
        _required_text(value.get("id"), "scope.id"),
    )


def _text_sequence(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field_name} must be a sequence")
    return tuple(_required_text(item, field_name) for item in value)


def _derive_operation_access_control(
    source: Mapping[str, Any],
    *,
    principal: PrincipalEnvelope | None,
    scope_type: str,
    scope_id: str,
) -> dict[str, Any]:
    """Derive an operation ACL from an authorized source, never caller output."""

    source_access = validate_cognitive_access_envelope(
        source["access_control"],
        expected_scope_type=scope_type,
        expected_scope_id=scope_id,
    )
    source_id = str(source["source_id"])
    if source_id not in set(source_access["consent"]["provenance_refs"]):
        raise ValueError("source access consent does not bind the source_id")
    write_decision = authorize_cognitive_write(
        source_access,
        principal=principal,
        scope_type=scope_type,
        scope_id=scope_id,
    )
    if not write_decision.allowed:
        raise ValueError(f"source access denied: {write_decision.reason}")
    assert principal is not None
    return derive_strictest_cognitive_access(
        (source_access,),
        owner_principal_id=principal.principal_id,
        owner_agent=principal.agent,
        scope_type=scope_type,
        scope_id=scope_id,
        purposes=("cognitive_state_read", "cognitive_state_write"),
        retention_policy=str(source_access["retention_policy"]),
    )


def _consumers(value: Any) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_STATE_CONSUMERS
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("intended_consumers must be a sequence")
    consumers = tuple(_required_text(item, "intended_consumers") for item in value)
    if not consumers or len(set(consumers)) != len(consumers):
        raise ValueError("intended_consumers must be non-empty and unique")
    return consumers


def _revision_request(value: Any, *, field_name: str, default_object_id: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    request = dict(value)
    payload = request.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError(f"{field_name}.payload must be an object")
    request["payload"] = dict(payload)
    request["object_id"] = str(request.get("object_id") or default_object_id)
    return request


def _create_revision(
    *,
    object_type: str,
    request: Mapping[str, Any],
    source: Mapping[str, Any],
    event_id: str,
    scope_type: str,
    scope_id: str,
    access_control: Mapping[str, Any],
) -> CognitiveStateRevision:
    payload = dict(request["payload"])
    if "access_control" in payload:
        raise ValueError("revision payload cannot override operation access_control")
    payload["access_control"] = dict(access_control)
    return CognitiveStateRevision.create(
        object_type=object_type,
        object_id=str(request["object_id"]),
        source_event_id=event_id,
        source_revision_id=str(source["source_revision_id"]),
        source_content_hash=str(source["content_hash"]),
        scope_type=scope_type,
        scope_id=scope_id,
        evidence_refs=tuple(str(value) for value in source["evidence_refs"]),
        payload=payload,
        supersedes_revision_id=str(request.get("supersedes_revision_id") or ""),
        correction_of_revision_id=str(request.get("correction_of_revision_id") or ""),
        created_at=str(source["created_at"]),
    )


def _semantic_event_id(
    *,
    operation: str,
    object_id: str,
    source: Mapping[str, Any],
    semantic_input: Any,
) -> str:
    identity = {
        "operation": operation,
        "object_id": object_id,
        "source_id": source["source_id"],
        "source_revision_id": source["source_revision_id"],
        "content_hash": source["content_hash"],
        "semantic_input_hash": sha256_json(semantic_input),
    }
    return "cogevent-" + sha256_json(identity).split(":", 1)[1][:32]


def _revision_payload(revision: CognitiveStateRevision) -> dict[str, Any]:
    return {
        "revision_id": revision.revision_id,
        "object_type": revision.object_type,
        "object_id": revision.object_id,
        "schema_version": revision.schema_version,
        "payload": dict(revision.payload),
        "payload_hash": revision.payload_hash,
        "evidence_refs": list(revision.evidence_refs),
        "evidence_hash": revision.evidence_hash,
        "source_event_id": revision.source_event_id,
        "source_revision_id": revision.source_revision_id,
        "source_content_hash": revision.source_content_hash,
        "scope_type": revision.scope_type,
        "scope_id": revision.scope_id,
        "supersedes_revision_id": revision.supersedes_revision_id,
        "correction_of_revision_id": revision.correction_of_revision_id,
        "admission_state": revision.admission_state,
        "redaction_policy": revision.redaction_policy,
        "redaction_counts": dict(revision.redaction_counts),
        "created_at": revision.created_at,
        "canonical_payload_hash": sha256_json(dict(revision.payload)),
        "canonical_payload": canonical_json(revision.payload),
    }
