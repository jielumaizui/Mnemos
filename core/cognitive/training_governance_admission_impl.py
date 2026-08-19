"""Private evidence resolution and sample projection implementation."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from core.cognitive.state_store import CognitiveStateStore

from core.access_policy import PrincipalEnvelope
from core.cognitive.access_control import (
    authorize_cognitive_write,
    cognitive_access_hash,
    derive_strictest_cognitive_access,
)
from core.cognitive.prediction_ledger import (
    PREDICTION_TERMINAL_COMMAND,
    PREDICTION_TERMINAL_CONSUMER,
    PredictionRecordStore,
)
from core.cognitive.prediction_outcome_support import reissue_objective_measurement
from core.cognitive.state_contract import (
    CognitiveStateRevision,
    LocalConsumerCommand,
    canonical_json,
    sha256_json,
    validate_cognitive_state_payload,
)
from core.cognitive.training_contract import (
    TRAINING_ADMISSION_SCHEMA_VERSION,
    derive_dataset_assignment,
    derive_feature_snapshot,
    derive_training_label,
    training_admission_input_hash,
)
from core.cognitive.training_governance_types import (
    FeedbackEvidence as _FeedbackEvidence,
    TRAINING_PROJECTION_COMMAND,
    TRAINING_PROJECTION_CONSUMER,
    TRAINING_PROJECTION_SCHEMA,
    TrainingEvidenceNotReady,
)
from core.scoring.feedback_provenance import build_training_feedback_proposal_owner
from core.scoring.training_schema import inspect_training_schema


class _TrainingGovernanceAdmissionImplementation:
    """Internal seam; callers continue to use TrainingGovernanceStore."""

    state: CognitiveStateStore
    database_dir: Path
    scoring_db_path: Path
    _clock: Callable[[], str]

    def _resolve_feedback_evidence(
        self,
        command_id: str,
        principal: PrincipalEnvelope,
        *,
        _governance_time: str = "",
    ) -> _FeedbackEvidence:
        normalized_id = str(command_id or "").strip()
        if not normalized_id:
            raise ValueError("training evidence command_id is required")
        if not isinstance(principal, PrincipalEnvelope):
            raise TypeError("training admission requires a server principal")
        command = self.state.command(normalized_id)
        if (
            command is None
            or command["consumer_id"] != "training_evidence"
            or command["command_type"] != "evaluate_feedback_target"
            or command["payload"].get("schema_version") != "mnemos.feedback_target_command.v1"
        ):
            raise ValueError("training evidence command contract mismatch")
        attribution = self.state.revision(str(command["revision_id"]))
        if (
            attribution is None
            or attribution.object_type != "feedback_attribution_record"
            or self.state.current_revision(
                "feedback_attribution_record",
                attribution.object_id,
            )
            != attribution
        ):
            raise ValueError("training evidence attribution is stale or unavailable")
        outcome_ref = command["payload"].get("objective_outcome_ref")
        if (
            attribution.payload["evidence_class"] != "objective_outcome"
            or attribution.payload["disposition"] != "objective_only"
            or not isinstance(outcome_ref, Mapping)
            or outcome_ref.get("state") != "available"
        ):
            raise ValueError("training admission requires a verified objective outcome")
        if command["payload"].get("eligible") is not True:
            raise ValueError("training objective command is not eligible")

        self.state.validate_feedback_effect_receipt(normalized_id)
        feedback_receipt = self.state.effect_receipt(normalized_id)
        if feedback_receipt is None or feedback_receipt["status"] != "committed":
            raise ValueError("training evidence lacks a committed COG-038 receipt")
        proposal_owner = build_training_feedback_proposal_owner(self.database_dir)
        domain_effect = proposal_owner.inspect_command_effect(command["payload"])
        if domain_effect is None or domain_effect.disposition != "proposal_committed":
            raise ValueError("training evidence lacks its reciprocal proposal proof")

        outcome = PredictionRecordStore(self.state).verify_outcome_revision(
            str(outcome_ref.get("revision_id") or "")
        )
        if (
            outcome.object_id != outcome_ref.get("outcome_id")
            or outcome.payload_hash != outcome_ref.get("payload_hash")
            or self.state.current_revision("outcome_measurement", outcome.object_id) != outcome
            or {
                "outcome_id": outcome.object_id,
                "revision_id": outcome.revision_id,
                "payload_hash": outcome.payload_hash,
            }
            not in [dict(item) for item in attribution.payload["outcome_refs"]]
        ):
            raise ValueError("training outcome is not the exact current attribution input")
        governance_time = _aware_timestamp(
            _governance_time or self._clock(),
            "training governance clock",
        )
        matured_at = _aware_timestamp(
            outcome.payload["maturity"]["matured_at"],
            "training outcome matured_at",
        )
        if governance_time < matured_at:
            raise TrainingEvidenceNotReady("outcome_not_mature")
        prediction_ref = outcome.payload["prediction_ref"]
        prediction = self.state.revision(str(prediction_ref["revision_id"]))
        if prediction is None or prediction.object_type != "prediction_record":
            raise ValueError("training prediction revision is unavailable")
        prediction_terminal = self._current_measured_prediction(
            prediction,
            outcome,
            governance_time=governance_time,
        )
        decision_ref = prediction.payload["decision_ref"]
        decision = self.state.revision(str(decision_ref["revision_id"]))
        if (
            decision is None
            or decision.object_type != "decision_trace"
            or decision.object_id != decision_ref["decision_id"]
            or decision.payload_hash != decision_ref["revision_hash"]
        ):
            raise ValueError("training prediction DecisionTrace binding is invalid")
        prediction_effect_receipt = self._prediction_effect_receipt(
            prediction,
            decision,
        )

        for source in (prediction, prediction_terminal, outcome, attribution):
            authorization = authorize_cognitive_write(
                source.payload["access_control"],
                principal=principal,
                scope_type=source.scope_type,
                scope_id=source.scope_id,
            )
            if not authorization.allowed:
                raise PermissionError(f"training admission access denied: {authorization.reason}")

        issuance = reissue_objective_measurement(
            state_store=self.state,
            prediction=prediction,
            outcome=outcome,
        )
        proposal_id, proposal_hash, domain_receipt_id, domain_receipt_hash = (
            self._domain_proposal_proof(command["payload"], domain_effect)
        )
        return _FeedbackEvidence(
            command=command,
            attribution=attribution,
            outcome=outcome,
            prediction=prediction,
            prediction_terminal=prediction_terminal,
            decision=decision,
            proposal_id=proposal_id,
            proposal_hash=proposal_hash,
            domain_effect=domain_effect,
            domain_receipt_id=domain_receipt_id,
            domain_receipt_hash=domain_receipt_hash,
            prediction_effect_receipt=prediction_effect_receipt,
            oracle_issuance_hash=issuance.issuance_hash,
        )

    def _build_admission_revision(
        self,
        evidence: _FeedbackEvidence,
        *,
        principal: PrincipalEnvelope,
        admission_id: str,
        sample_id: str,
        projection_command_key: str,
        projection_effect_id: str,
        projection_receipt_id: str,
        created_at: str,
    ) -> CognitiveStateRevision:
        del sample_id
        prediction = evidence.prediction
        prediction_terminal = evidence.prediction_terminal
        outcome = evidence.outcome
        attribution = evidence.attribution
        access = derive_strictest_cognitive_access(
            (
                prediction.payload["access_control"],
                prediction_terminal.payload["access_control"],
                outcome.payload["access_control"],
                attribution.payload["access_control"],
            ),
            owner_principal_id=principal.principal_id,
            owner_agent=principal.agent,
            scope_type=prediction.scope_type,
            scope_id=prediction.scope_id,
            purposes=("cognitive_state_read", "cognitive_state_write"),
            retention_policy="governed_training",
        )
        if access["scope"]["resolution"] != "resolved":
            raise PermissionError("training admission source ACLs are incompatible")
        feature_snapshot = derive_feature_snapshot(prediction.payload)
        label = derive_training_label(
            outcome.payload,
            outcome_revision_id=outcome.revision_id,
            outcome_payload_hash=outcome.payload_hash,
        )
        assignment = derive_dataset_assignment(
            subject=prediction.payload["subject"],
            scope={"type": prediction.scope_type, "id": prediction.scope_id},
        )
        prediction_effect = evidence.prediction_effect_receipt
        temporal = {
            "prediction_sealed_at": prediction.created_at,
            "prediction_terminal_at": str(
                prediction_terminal.payload["terminal"]["evaluated_at"]
            ),
            "effect_committed_at": str(prediction_effect["created_at"]),
            "window_starts_at": str(prediction.payload["evaluation_window"]["starts_at"]),
            "window_ends_at": str(prediction.payload["evaluation_window"]["ends_at"]),
            "outcome_observed_at": str(outcome.payload["observation_window"]["ends_at"]),
            "outcome_matured_at": str(outcome.payload["maturity"]["matured_at"]),
            "admission_effective_at": created_at,
            "maturity": "mature",
        }
        temporal["proof_hash"] = sha256_json(temporal)
        domain_decision = evidence.domain_effect.decision_trace_refs[0]
        domain_action = evidence.domain_effect.action_refs[0]
        action_ref = prediction.payload["action_ref"]
        payload: dict[str, Any] = {
            "access_control": access,
            "schema_version": TRAINING_ADMISSION_SCHEMA_VERSION,
            "admission_id": admission_id,
            "revision_state": "active",
            "input_set_hash": "",
            "supersedes_revision_id": "",
            "correction_of_revision_id": "",
            "training_evidence_ref": {
                "command_id": str(evidence.command["command_id"]),
                "command_payload_hash": str(evidence.command["payload_hash"]),
                "attribution_revision_id": attribution.revision_id,
                "attribution_payload_hash": attribution.payload_hash,
                "proposal_id": evidence.proposal_id,
                "proposal_hash": evidence.proposal_hash,
                "decision_id": domain_decision.id,
                "action_id": domain_action.id,
                "effect_id": str(evidence.domain_effect.target_effect_id),
                "receipt_id": evidence.domain_receipt_id,
                "receipt_hash": evidence.domain_receipt_hash,
            },
            "prediction_ref": {
                "object_id": prediction.object_id,
                "revision_id": prediction.revision_id,
                "payload_hash": prediction.payload_hash,
                "input_hash": str(prediction.payload["prediction_input_hash"]),
            },
            "prediction_terminal_ref": {
                "object_id": prediction_terminal.object_id,
                "revision_id": prediction_terminal.revision_id,
                "payload_hash": prediction_terminal.payload_hash,
                "terminal_state": str(
                    prediction_terminal.payload["terminal"]["state"]
                ),
                "outcome_revision_id": str(
                    prediction_terminal.payload["outcome_ref"]["revision_id"]
                ),
                "outcome_payload_hash": str(
                    prediction_terminal.payload["outcome_ref"]["payload_hash"]
                ),
            },
            "outcome_ref": {
                "object_id": outcome.object_id,
                "revision_id": outcome.revision_id,
                "payload_hash": outcome.payload_hash,
                "oracle_receipt_hash": evidence.oracle_issuance_hash,
            },
            "decision_ref": {
                "object_id": evidence.decision.object_id,
                "revision_id": evidence.decision.revision_id,
                "payload_hash": evidence.decision.payload_hash,
            },
            "material_effect_ref": {
                "action_id": str(action_ref["action_id"]),
                "effect_id": str(action_ref["effect_id"]),
                "effect_receipt_id": str(prediction_effect["receipt_id"]),
                "effect_receipt_hash": self._state_effect_receipt_hash(prediction_effect),
            },
            "delivery_ref": {
                "event_id": str(prediction.payload["delivery_ref"]["event_id"]),
                "payload_hash": str(prediction.payload["delivery_ref"]["event_payload_hash"]),
            },
            "subject": dict(prediction.payload["subject"]),
            "scope": {"type": prediction.scope_type, "id": prediction.scope_id},
            "principal_ref": {
                "principal_id": principal.principal_id,
                "authorization_ref": f"principal-capability:{principal.capability_id}",
            },
            "temporal_proof": temporal,
            "authority_proof": {
                "source_authority_catalog_hash": str(
                    outcome.payload["source_authority"]["source_authority_catalog_hash"]
                ),
                "raw_issuance_receipt_ref": (
                    "objective-oracle-issuance:" + evidence.oracle_issuance_hash
                ),
                "raw_issuance_receipt_hash": evidence.oracle_issuance_hash,
            },
            "feature_snapshot": feature_snapshot,
            "label": label,
            "evidence_quality": {
                "uncertainty": 0.0,
                "attribution": "direct_objective_measurement",
                "competing_causes": list(outcome.payload["attribution"]["competing_causes"]),
                "calibration_eligible": True,
                "exclusion_reason": "",
            },
            "dataset_assignment": assignment,
            "lifecycle_state": "admitted",
            "target_effect_refs": {
                "projection_command_key": projection_command_key,
                "projection_effect_id": projection_effect_id,
                "reciprocal_receipt_id": projection_receipt_id,
            },
        }
        payload["input_set_hash"] = training_admission_input_hash(payload)
        source_event_id = "training-admission-event-" + admission_id.rsplit("-", 1)[1]
        evidence_refs = tuple(
            dict.fromkeys(
                (
                    f"feedback-command:{evidence.command['command_id']}",
                    attribution.revision_id,
                    outcome.revision_id,
                    prediction.revision_id,
                    prediction_terminal.revision_id,
                    evidence.decision.revision_id,
                    str(evidence.domain_effect.target_receipt_ref),
                    f"objective-oracle-issuance:{evidence.oracle_issuance_hash}",
                )
            )
        )
        return CognitiveStateRevision.create(
            object_type="training_admission_record",
            object_id=admission_id,
            source_event_id=source_event_id,
            source_revision_id=str(evidence.command["command_id"]),
            source_content_hash=str(evidence.command["payload_hash"]),
            scope_type=prediction.scope_type,
            scope_id=prediction.scope_id,
            evidence_refs=evidence_refs,
            payload=payload,
            created_at=created_at,
        )

    def _admission_effective_at(self, evidence: _FeedbackEvidence) -> str:
        """Derive one replay-stable time after maturity and terminalization."""

        return max(
            _aware_timestamp(
                evidence.command["created_at"],
                "training intake created_at",
            ),
            _aware_timestamp(
                evidence.outcome.payload["maturity"]["matured_at"],
                "training outcome matured_at",
            ),
            _aware_timestamp(
                evidence.prediction_terminal.payload["terminal"]["evaluated_at"],
                "training prediction terminal evaluated_at",
            ),
        ).isoformat()

    def _current_measured_prediction(
        self,
        sealed: CognitiveStateRevision,
        outcome: CognitiveStateRevision,
        *,
        governance_time: datetime,
    ) -> CognitiveStateRevision:
        """Resolve the current measured head without reading post-outcome features."""

        current = self.state.current_revision("prediction_record", sealed.object_id)
        if current is None:
            raise ValueError("training current prediction is unavailable or tombstoned")
        validate_cognitive_state_payload("prediction_record", current.payload)
        if current.payload["revision_state"] != "terminal":
            raise TrainingEvidenceNotReady("prediction_not_terminal")
        terminal = current.payload["terminal"]
        outcome_ref = current.payload["outcome_ref"]
        evaluated_at = _aware_timestamp(
            terminal["evaluated_at"],
            "training prediction terminal evaluated_at",
        )
        matured_at = _aware_timestamp(
            outcome.payload["maturity"]["matured_at"],
            "training outcome matured_at",
        )
        if evaluated_at > governance_time:
            raise TrainingEvidenceNotReady("prediction_terminal_in_future")
        if (
            current.object_id != sealed.object_id
            or current.payload["prediction_input_hash"]
            != sealed.payload["prediction_input_hash"]
            or terminal["state"] != "measured"
            or evaluated_at < matured_at
            or outcome_ref
            != {
                "revision_id": outcome.revision_id,
                "payload_hash": outcome.payload_hash,
            }
            or current.payload["exposure"]["status"] != "proven"
            or tuple(current.payload["exposure"]["evidence_refs"])
            != tuple(outcome.payload["raw_evidence"]["refs"])
            or current.payload["attribution"]["method"]
            != outcome.payload["attribution"]["method"]
            or current.payload["attribution"]["competing_causes"]
            or current.payload["calibration"]
            != {"eligible": True, "exclusion_reason": ""}
        ):
            raise ValueError("training current prediction is not exact measured evidence")
        cursor = current
        seen: set[str] = set()
        while cursor.revision_id != sealed.revision_id:
            if cursor.revision_id in seen or not cursor.supersedes_revision_id:
                raise ValueError("training prediction terminal ancestry is invalid")
            seen.add(cursor.revision_id)
            prior = self.state.revision(cursor.supersedes_revision_id)
            if (
                prior is None
                or prior.object_type != "prediction_record"
                or prior.object_id != sealed.object_id
            ):
                raise ValueError("training prediction terminal ancestry is unavailable")
            cursor = prior
        self._validate_prediction_terminal_projection(current)
        return current

    def _validate_prediction_terminal_projection(
        self,
        terminal: CognitiveStateRevision,
    ) -> None:
        commands = tuple(
            command
            for command in self.state.commands_for_revision(terminal.revision_id)
            if command["consumer_id"] == PREDICTION_TERMINAL_CONSUMER
            and command["command_type"] == PREDICTION_TERMINAL_COMMAND
        )
        if len(commands) != 1:
            raise ValueError("training prediction terminal command gap")
        command = commands[0]
        recomputed = LocalConsumerCommand.create(
            revision_id=str(command["revision_id"]),
            consumer_id=str(command["consumer_id"]),
            command_type=str(command["command_type"]),
            payload=command["payload"],
            created_at=str(command["created_at"]),
        )
        payload = command["payload"]
        expected_payload = {
            "schema_version": "mnemos.prediction_terminal_projection.v1",
            "prediction_id": terminal.object_id,
            "terminal_revision_id": terminal.revision_id,
            "terminal_revision_hash": terminal.payload_hash,
            "terminal_state": terminal.payload["terminal"]["state"],
            "projection_effect_id": payload.get("projection_effect_id"),
        }
        before_hash = sha256_json(
            {"prediction_id": terminal.object_id, "state": "unprojected"}
        )
        after_hash = sha256_json(
            {
                "terminal_revision_id": terminal.revision_id,
                "terminal_revision_hash": terminal.payload_hash,
                "terminal_state": terminal.payload["terminal"]["state"],
            }
        )
        receipt = self.state.effect_receipt(str(command["command_id"]))
        if (
            recomputed.command_id != command["command_id"]
            or recomputed.payload_hash != command["payload_hash"]
            or command["payload"] != expected_payload
            or receipt is None
            or receipt["status"] != "committed"
            or receipt["target_effect_id"] != payload["projection_effect_id"]
            or receipt["before_hash"] != before_hash
            or receipt["after_hash"] != after_hash
            or tuple(receipt["evidence_refs"])
            != (
                f"prediction-terminal-command:{command['command_id']}",
                f"prediction-revision:{terminal.revision_id}",
                f"prediction-terminal-projection:{after_hash}",
            )
            or receipt["consumption_outcome"]
            != "deterministic prediction terminal read model available"
            or receipt["reason_code"]
        ):
            raise ValueError("training prediction terminal projection proof mismatch")

    def _prediction_effect_receipt(
        self,
        prediction: CognitiveStateRevision,
        decision: CognitiveStateRevision,
    ) -> dict[str, Any]:
        action_ref = prediction.payload["action_ref"]
        effect_id = str(action_ref["effect_id"])
        with sqlite3.connect(
            f"file:{self.state.db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT r.*, o.command_type, o.payload_json
                FROM cognitive_state_effect_receipts AS r
                JOIN cognitive_state_outbox AS o ON o.command_id=r.command_id
                WHERE r.target_effect_id=?
                """,
                (effect_id,),
            ).fetchall()
        if len(rows) != 1:
            raise ValueError("training prediction material effect receipt is unavailable")
        row = dict(rows[0])
        command_payload = json.loads(str(row.pop("payload_json")))
        row["command_payload"] = command_payload
        if (
            row["status"] != "committed"
            or row["command_type"] != "execute_material_action"
            or command_payload.get("decision_revision_id") != decision.revision_id
            or command_payload.get("action_id") != action_ref["action_id"]
            or command_payload.get("effect_id") != effect_id
        ):
            raise ValueError("training prediction material effect binding is invalid")
        action_specs = [
            item
            for item in decision.payload["action_specs"]
            if item["action_id"] == action_ref["action_id"] and item["effect_id"] == effect_id
        ]
        if len(action_specs) != 1:
            raise ValueError("training prediction material action is unavailable")
        return row

    def _domain_proposal_proof(
        self,
        command_payload: Mapping[str, Any],
        domain_effect: Any,
    ) -> tuple[str, str, str, str]:
        command_key = str(command_payload["command_key"])
        with sqlite3.connect(
            f"file:{self.scoring_db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            proposal = conn.execute(
                "SELECT * FROM training_feedback_proposals WHERE command_key=?",
                (command_key,),
            ).fetchone()
            receipt = conn.execute(
                "SELECT * FROM training_feedback_proposal_receipts WHERE command_key=?",
                (command_key,),
            ).fetchone()
        if proposal is None or receipt is None:
            raise ValueError("training proposal journal proof is unavailable")
        proposal_payload = json.loads(str(proposal["payload_json"]))
        if (
            sha256_json(proposal_payload) != str(proposal["payload_hash"])
            or str(receipt["state_id"]) != str(proposal["proposal_id"])
            or str(receipt["state_payload_hash"]) != str(proposal["payload_hash"])
            or str(receipt["target_effect_id"]) != str(domain_effect.target_effect_id)
            or str(receipt["disposition"]) != "proposal_committed"
            or proposal_payload.get("training_admitted") is not False
            or proposal_payload.get("direct_domain_update") is not False
        ):
            raise ValueError("training proposal journal proof is invalid")
        receipt_payload = {key: receipt[key] for key in receipt.keys()}
        return (
            str(proposal["proposal_id"]),
            str(proposal["payload_hash"]),
            str(receipt["receipt_id"]),
            sha256_json(receipt_payload),
        )

    def _projection_payload(
        self,
        revision: CognitiveStateRevision,
        *,
        sample_id: str,
        projection_command_key: str,
        projection_effect_id: str,
        projection_receipt_id: str,
    ) -> dict[str, Any]:
        payload = revision.payload
        return {
            "schema_version": TRAINING_PROJECTION_SCHEMA,
            "projection_command_key": projection_command_key,
            "admission_id": revision.object_id,
            "admission_revision_id": revision.revision_id,
            "admission_payload_hash": revision.payload_hash,
            "sample_id": sample_id,
            "dimension": payload["feature_snapshot"]["dimension"],
            "metric_id": payload["label"]["metric_id"],
            "feature_snapshot": dict(payload["feature_snapshot"]),
            "label_numeric": payload["label"]["numeric_value"],
            "label_value": payload["label"]["observed_value"],
            "dataset_assignment": dict(payload["dataset_assignment"]),
            "access_control_hash": cognitive_access_hash(payload["access_control"]),
            "projection_effect_id": projection_effect_id,
            "projection_receipt_id": projection_receipt_id,
        }

    def _apply_exclusion_projection(self, command: LocalConsumerCommand) -> None:
        payload = command.payload
        if (
            payload.get("schema_version") != "mnemos.governed_training_sample_exclusion.v1"
            or command.consumer_id != TRAINING_PROJECTION_CONSUMER
            or command.command_type != "exclude_governed_training_sample"
        ):
            raise ValueError("governed training exclusion command mismatch")
        self._validate_projection_schema()
        correction = self.state.revision(str(payload["correction_revision_id"]))
        if (
            correction is None
            or correction.object_type != "training_admission_record"
            or correction.object_id != payload["admission_id"]
            or correction.payload_hash != payload["correction_payload_hash"]
            or correction.payload["lifecycle_state"] != "excluded"
            or correction.payload["correction_of_revision_id"]
            != payload["original_admission_revision_id"]
        ):
            raise ValueError("governed training exclusion source mismatch")
        validate_cognitive_state_payload("training_admission_record", correction.payload)
        original_revision_id = str(payload["original_admission_revision_id"])
        original = self.state.revision(original_revision_id)
        if (
            original is None
            or original.object_type != "training_admission_record"
            or original.object_id != correction.object_id
            or original.payload["lifecycle_state"] != "admitted"
        ):
            raise ValueError("governed training exclusion original is invalid")

        sample_id = str(payload["sample_id"])
        prior_action_id = "training-sample-action-" + sample_id.rsplit("-", 1)[1]
        before_hash = sha256_json(
            {
                "sample_id": sample_id,
                "admission_revision_id": original_revision_id,
                "state": "admitted",
            }
        )
        after_hash = sha256_json(
            {
                "sample_id": sample_id,
                "admission_revision_id": original_revision_id,
                "correction_revision_id": correction.revision_id,
                "state": "excluded",
                "reason_code": payload["reason_code"],
            }
        )
        action_row = (
            str(payload["action_id"]),
            sample_id,
            original_revision_id,
            "exclude",
            str(payload["reason_code"]),
            prior_action_id,
            correction.payload_hash,
            command.created_at,
        )
        evidence_refs = [
            f"training-admission:{original_revision_id}",
            f"training-correction:{correction.revision_id}",
            f"corrected-outcome:{payload['corrected_outcome_revision_id']}",
        ]
        receipt_identity = {
            "receipt_id": str(payload["projection_receipt_id"]),
            "command_id": command.command_id,
            "admission_revision_id": original_revision_id,
            "sample_id": sample_id,
            "action_id": str(payload["action_id"]),
            "status": "revoked",
            "before_hash": before_hash,
            "after_hash": after_hash,
            "evidence_refs": evidence_refs,
        }
        projection_receipt_hash = sha256_json(receipt_identity)
        receipt_row = (
            receipt_identity["receipt_id"],
            command.command_id,
            original_revision_id,
            sample_id,
            receipt_identity["action_id"],
            "revoked",
            before_hash,
            after_hash,
            canonical_json(evidence_refs),
            projection_receipt_hash,
            command.created_at,
        )
        with sqlite3.connect(self.scoring_db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            schema = inspect_training_schema(conn)
            if not schema.ok:
                raise RuntimeError("governed training projection schema is not canonical")
            conn.execute("BEGIN IMMEDIATE")
            try:
                sample = conn.execute(
                    "SELECT admission_revision_id FROM governed_training_samples "
                    "WHERE sample_id=?",
                    (sample_id,),
                ).fetchone()
                prior_action = conn.execute(
                    "SELECT action_type FROM governed_training_sample_actions " "WHERE action_id=?",
                    (prior_action_id,),
                ).fetchone()
                if sample != (original_revision_id,) or prior_action != ("admit",):
                    raise RuntimeError("governed training exclusion prior proof mismatch")
                conn.execute(
                    "INSERT OR IGNORE INTO governed_training_sample_actions VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?)",
                    action_row,
                )
                conn.execute(
                    "INSERT OR IGNORE INTO governed_training_sample_receipts VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    receipt_row,
                )
                stored_action = conn.execute(
                    "SELECT * FROM governed_training_sample_actions WHERE action_id=?",
                    (str(payload["action_id"]),),
                ).fetchone()
                stored_receipt = conn.execute(
                    "SELECT * FROM governed_training_sample_receipts WHERE receipt_id=?",
                    (str(payload["projection_receipt_id"]),),
                ).fetchone()
                if (
                    stored_action is None
                    or tuple(stored_action) != action_row
                    or stored_receipt is None
                    or tuple(stored_receipt) != receipt_row
                ):
                    raise RuntimeError("immutable governed training exclusion conflict")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        existing_effect = self.state.effect_receipt(command.command_id)
        if existing_effect is None:
            self.state.record_effect_receipt(
                command.command_id,
                status="committed",
                target_effect_id=str(payload["projection_effect_id"]),
                before_hash=before_hash,
                after_hash=after_hash,
                evidence_refs=(
                    f"training-correction:{correction.revision_id}",
                    f"governed-training-sample:{sample_id}",
                    (
                        "governed-training-exclusion-receipt:"
                        + str(payload["projection_receipt_id"])
                        + ":"
                        + projection_receipt_hash
                    ),
                ),
                outcome="governed training sample exclusion committed",
                created_at=command.created_at,
            )
        elif (
            existing_effect["status"] != "committed"
            or existing_effect["target_effect_id"] != payload["projection_effect_id"]
            or existing_effect["before_hash"] != before_hash
            or existing_effect["after_hash"] != after_hash
        ):
            raise RuntimeError("governed training exclusion state receipt conflict")

    def _apply_projection(self, command: LocalConsumerCommand) -> None:
        payload = command.payload
        if (
            payload.get("schema_version") != TRAINING_PROJECTION_SCHEMA
            or command.consumer_id != TRAINING_PROJECTION_CONSUMER
            or command.command_type != TRAINING_PROJECTION_COMMAND
        ):
            raise ValueError("governed training projection command mismatch")
        self._validate_projection_schema()
        sample_id = str(payload["sample_id"])
        action_id = "training-sample-action-" + sample_id.rsplit("-", 1)[1]
        before_hash = sha256_json({"sample_id": sample_id, "state": "absent"})
        after_hash = sha256_json(
            {
                "sample_id": sample_id,
                "admission_revision_id": payload["admission_revision_id"],
                "admission_payload_hash": payload["admission_payload_hash"],
                "feature_snapshot_hash": payload["feature_snapshot"]["snapshot_hash"],
                "label_numeric": payload["label_numeric"],
                "dataset_split": payload["dataset_assignment"]["split"],
            }
        )
        created_at = command.created_at
        sample_row = (
            sample_id,
            str(payload["admission_revision_id"]),
            str(payload["admission_payload_hash"]),
            str(payload["dimension"]),
            str(payload["metric_id"]),
            canonical_json(payload["feature_snapshot"]),
            str(payload["feature_snapshot"]["snapshot_hash"]),
            int(payload["label_numeric"]),
            str(payload["label_value"]),
            str(payload["dataset_assignment"]["group_id"]),
            str(payload["dataset_assignment"]["group_hash"]),
            str(payload["dataset_assignment"]["split"]),
            str(payload["access_control_hash"]),
            created_at,
        )
        action_row = (
            action_id,
            sample_id,
            str(payload["admission_revision_id"]),
            "admit",
            "objective_outcome_verified",
            None,
            str(payload["admission_payload_hash"]),
            created_at,
        )
        receipt_identity = {
            "receipt_id": payload["projection_receipt_id"],
            "command_id": command.command_id,
            "admission_revision_id": payload["admission_revision_id"],
            "sample_id": sample_id,
            "action_id": action_id,
            "status": "committed",
            "before_hash": before_hash,
            "after_hash": after_hash,
            "evidence_refs": [
                f"training-admission:{payload['admission_revision_id']}",
                f"training-projection-command:{command.command_id}",
            ],
        }
        projection_receipt_hash = sha256_json(receipt_identity)
        receipt_row = (
            str(payload["projection_receipt_id"]),
            command.command_id,
            str(payload["admission_revision_id"]),
            sample_id,
            action_id,
            "committed",
            before_hash,
            after_hash,
            canonical_json(receipt_identity["evidence_refs"]),
            projection_receipt_hash,
            created_at,
        )
        with sqlite3.connect(self.scoring_db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            state = inspect_training_schema(conn)
            if not state.ok:
                raise RuntimeError("governed training projection schema is not canonical")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO governed_training_samples VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    sample_row,
                )
                conn.execute(
                    "INSERT OR IGNORE INTO governed_training_sample_actions VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?)",
                    action_row,
                )
                conn.execute(
                    "INSERT OR IGNORE INTO governed_training_sample_receipts VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    receipt_row,
                )
                stored_sample = conn.execute(
                    "SELECT * FROM governed_training_samples WHERE sample_id=?",
                    (sample_id,),
                ).fetchone()
                stored_action = conn.execute(
                    "SELECT * FROM governed_training_sample_actions WHERE action_id=?",
                    (action_id,),
                ).fetchone()
                stored_receipt = conn.execute(
                    "SELECT * FROM governed_training_sample_receipts WHERE receipt_id=?",
                    (str(payload["projection_receipt_id"]),),
                ).fetchone()
                if (
                    stored_sample is None
                    or tuple(stored_sample) != sample_row
                    or stored_action is None
                    or tuple(stored_action) != action_row
                    or stored_receipt is None
                    or tuple(stored_receipt) != receipt_row
                ):
                    raise RuntimeError("immutable governed training projection conflict")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        existing_effect = self.state.effect_receipt(command.command_id)
        if existing_effect is None:
            self.state.record_effect_receipt(
                command.command_id,
                status="committed",
                target_effect_id=str(payload["projection_effect_id"]),
                before_hash=before_hash,
                after_hash=after_hash,
                evidence_refs=(
                    f"training-admission:{payload['admission_revision_id']}",
                    f"governed-training-sample:{sample_id}",
                    (
                        "governed-training-receipt:"
                        + str(payload["projection_receipt_id"])
                        + ":"
                        + projection_receipt_hash
                    ),
                ),
                outcome="governed training sample projection committed",
                created_at=created_at,
            )
        elif (
            existing_effect["status"] != "committed"
            or existing_effect["target_effect_id"] != payload["projection_effect_id"]
            or existing_effect["before_hash"] != before_hash
            or existing_effect["after_hash"] != after_hash
        ):
            raise RuntimeError("governed training state receipt conflict")

    def _assert_exact_admission_projection(
        self,
        *,
        revision: CognitiveStateRevision,
        command: Mapping[str, Any],
        state_receipt: Mapping[str, Any] | None,
        sample: sqlite3.Row | None,
        action: sqlite3.Row | None,
        receipt: sqlite3.Row | None,
    ) -> None:
        """Recompute one active admission's complete reciprocal projection proof."""

        suffix = revision.object_id.rsplit("-", 1)[1]
        sample_id = "training-sample-" + suffix
        targets = revision.payload["target_effect_refs"]
        expected_projection = self._projection_payload(
            revision,
            sample_id=sample_id,
            projection_command_key=str(targets["projection_command_key"]),
            projection_effect_id=str(targets["projection_effect_id"]),
            projection_receipt_id=str(targets["reciprocal_receipt_id"]),
        )
        command_object = LocalConsumerCommand.create(
            revision_id=revision.revision_id,
            consumer_id=str(command["consumer_id"]),
            command_type=str(command["command_type"]),
            payload=command["payload"],
            created_at=str(command["created_at"]),
        )
        if (
            command_object.command_id != command["command_id"]
            or command["payload"] != expected_projection
        ):
            raise RuntimeError("training admission projection command mismatch")
        before_hash = sha256_json({"sample_id": sample_id, "state": "absent"})
        after_hash = sha256_json(
            {
                "sample_id": sample_id,
                "admission_revision_id": revision.revision_id,
                "admission_payload_hash": revision.payload_hash,
                "feature_snapshot_hash": revision.payload["feature_snapshot"]["snapshot_hash"],
                "label_numeric": revision.payload["label"]["numeric_value"],
                "dataset_split": revision.payload["dataset_assignment"]["split"],
            }
        )
        created_at = str(command["created_at"])
        action_id = "training-sample-action-" + suffix
        expected_sample = (
            sample_id,
            revision.revision_id,
            revision.payload_hash,
            str(expected_projection["dimension"]),
            str(expected_projection["metric_id"]),
            canonical_json(expected_projection["feature_snapshot"]),
            str(expected_projection["feature_snapshot"]["snapshot_hash"]),
            int(expected_projection["label_numeric"]),
            str(expected_projection["label_value"]),
            str(expected_projection["dataset_assignment"]["group_id"]),
            str(expected_projection["dataset_assignment"]["group_hash"]),
            str(expected_projection["dataset_assignment"]["split"]),
            str(expected_projection["access_control_hash"]),
            created_at,
        )
        expected_action = (
            action_id,
            sample_id,
            revision.revision_id,
            "admit",
            "objective_outcome_verified",
            None,
            revision.payload_hash,
            created_at,
        )
        evidence_refs = [
            f"training-admission:{revision.revision_id}",
            f"training-projection-command:{command['command_id']}",
        ]
        receipt_identity = {
            "receipt_id": str(expected_projection["projection_receipt_id"]),
            "command_id": str(command["command_id"]),
            "admission_revision_id": revision.revision_id,
            "sample_id": sample_id,
            "action_id": action_id,
            "status": "committed",
            "before_hash": before_hash,
            "after_hash": after_hash,
            "evidence_refs": evidence_refs,
        }
        receipt_hash = sha256_json(receipt_identity)
        expected_receipt = (
            receipt_identity["receipt_id"],
            receipt_identity["command_id"],
            revision.revision_id,
            sample_id,
            action_id,
            "committed",
            before_hash,
            after_hash,
            canonical_json(evidence_refs),
            receipt_hash,
            created_at,
        )
        expected_state_evidence = (
            f"training-admission:{revision.revision_id}",
            f"governed-training-sample:{sample_id}",
            (
                "governed-training-receipt:"
                + str(receipt_identity["receipt_id"])
                + ":"
                + receipt_hash
            ),
        )
        state_receipt_identity = {
            "command_id": str(command["command_id"]),
            "status": "committed",
            "target_effect_id": str(expected_projection["projection_effect_id"]),
            "before_hash": before_hash,
            "after_hash": after_hash,
            "evidence_refs": list(expected_state_evidence),
            "terminal_reason_code": "",
            "retry_exhausted": False,
        }
        expected_state_receipt_id = (
            "cogeffect-" + sha256_json(state_receipt_identity).split(":", 1)[1][:32]
        )
        if (
            sample is None
            or tuple(sample) != expected_sample
            or action is None
            or tuple(action) != expected_action
            or receipt is None
            or tuple(receipt) != expected_receipt
            or state_receipt is None
            or state_receipt.get("receipt_id") != expected_state_receipt_id
            or state_receipt.get("command_id") != command["command_id"]
            or state_receipt.get("revision_id") != revision.revision_id
            or state_receipt.get("event_id") != command["event_id"]
            or state_receipt.get("consumer_id") != command["consumer_id"]
            or state_receipt.get("status") != "committed"
            or state_receipt.get("target_effect_id") != expected_projection["projection_effect_id"]
            or state_receipt.get("before_hash") != before_hash
            or state_receipt.get("after_hash") != after_hash
            or tuple(state_receipt.get("evidence_refs") or ()) != expected_state_evidence
            or state_receipt.get("created_at") != command["created_at"]
        ):
            raise RuntimeError("training admission projection proof mismatch")
        self._assert_exact_admission_consumption(
            state_receipt,
            command=command,
            receipt_id=expected_state_receipt_id,
            target_effect_id=str(expected_projection["projection_effect_id"]),
            before_hash=before_hash,
            after_hash=after_hash,
            evidence_refs=expected_state_evidence,
        )

    def _assert_exact_admission_consumption(
        self,
        state_receipt: Mapping[str, Any],
        *,
        command: Mapping[str, Any],
        receipt_id: str,
        target_effect_id: str,
        before_hash: str,
        after_hash: str,
        evidence_refs: tuple[str, ...],
    ) -> None:
        """Verify the canonical consumption paired with an admission receipt."""

        consumption_id = str(state_receipt.get("consumption_id") or "")
        if not consumption_id:
            raise RuntimeError("training admission consumption proof mismatch")
        with sqlite3.connect(
            f"file:{self.state.db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT consumption.*, head.consumption_id AS head_consumption_id "
                "FROM cognitive_data_consumptions AS consumption "
                "LEFT JOIN cognitive_data_consumer_heads AS head "
                "ON head.event_id=consumption.event_id "
                "AND head.consumer_id=consumption.consumer_id "
                "WHERE consumption.consumption_id=?",
                (consumption_id,),
            ).fetchone()
        expected_effect_refs = (
            *evidence_refs,
            f"cognitive-effect-receipt:{receipt_id}",
        )
        expected_metadata = {
            "command_id": str(command["command_id"]),
            "effect_receipt_id": receipt_id,
            "terminal_reason_code": "",
            "retry_exhausted": False,
        }
        if (
            row is None
            or str(row["event_id"]) != command["event_id"]
            or str(row["consumer_id"]) != command["consumer_id"]
            or str(row["outcome"]) != "governed training sample projection committed"
            or str(row["status"]) != "committed"
            or str(row["target_effect_id"]) != target_effect_id
            or str(row["before_hash"]) != before_hash
            or str(row["after_hash"]) != after_hash
            or str(row["effect_evidence_refs"]) != canonical_json(list(expected_effect_refs))
            or int(row["action_changed"]) != int(before_hash != after_hash)
            or str(row["metadata"]) != canonical_json(expected_metadata)
            or str(row["idempotency_key"]) != f"cognitive-effect:{receipt_id}"
            or str(row["supersedes_consumption_id"] or "")
            or str(row["correction_of_consumption_id"] or "")
            or str(row["receipt_state"]) != "active"
            or str(row["created_at"]) != command["created_at"]
            or str(row["head_consumption_id"] or "") != consumption_id
        ):
            raise RuntimeError("training admission consumption proof mismatch")

    def _validate_projection_schema(self) -> None:
        if not self.scoring_db_path.is_file():
            raise RuntimeError(
                "governed training projection is not initialized; run reconciliation"
            )
        with sqlite3.connect(
            f"file:{self.scoring_db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            state = inspect_training_schema(conn)
        if not state.ok:
            raise RuntimeError(
                "governed training projection requires reconciliation: "
                f"classification={state.classification}"
            )

    @staticmethod
    def _state_effect_receipt_hash(receipt: Mapping[str, Any]) -> str:
        return sha256_json(
            {
                key: receipt[key]
                for key in (
                    "receipt_id",
                    "command_id",
                    "revision_id",
                    "status",
                    "target_effect_id",
                    "before_hash",
                    "after_hash",
                    "evidence_refs",
                    "created_at",
                )
            }
        )


def _aware_timestamp(value: Any, field_name: str) -> datetime:
    """Parse one exact timezone-aware timestamp for governance comparisons."""

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed
