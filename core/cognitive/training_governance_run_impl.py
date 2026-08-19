"""Private run, correction, deletion, and projection implementation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from core.cognitive.state_store import CognitiveStateStore

from core.cognitive.access_control import (
    cognitive_access_hash,
    derive_strictest_cognitive_access,
    make_cognitive_access_envelope,
)
from core.cognitive.state_contract import (
    CognitiveHeadPrecondition,
    CognitiveStateRevision,
    LocalConsumerCommand,
    canonical_json,
    sha256_json,
    validate_cognitive_state_payload,
)
from core.cognitive.training_contract import (
    FEATURE_NAMES,
    READINESS_POLICY_HASH,
    TRAINING_DIMENSION,
    TRAINING_RUN_SCHEMA_VERSION,
    training_admission_input_hash,
    training_run_input_hash,
)
from core.cognitive.training_governance_types import (
    TRAINING_PROJECTION_COMMAND,
    TRAINING_PROJECTION_CONSUMER,
    TrainingRunReceipt,
)
from core.ops.cognitive_data_contract import CognitiveDataEvent
from core.scoring.training_schema import inspect_training_schema


class _TrainingGovernanceRunImplementation:
    """Internal seam; callers continue to use TrainingGovernanceStore."""

    state: CognitiveStateStore
    scoring_db_path: Path
    _clock: Callable[[], str]

    if TYPE_CHECKING:

        def _aux_projection_rows(
            self,
            revision: CognitiveStateRevision,
            command: LocalConsumerCommand,
            *,
            run_before_hash: str,
        ) -> tuple[
            tuple[tuple[Any, ...], ...],
            tuple[tuple[Any, ...], ...],
            tuple[str, ...],
        ]: ...

        def _apply_exclusion_projection(self, command: LocalConsumerCommand) -> None: ...

        def _validate_projection_schema(self) -> None: ...

        def _verify_admission_upstream(
            self,
            revision: CognitiveStateRevision,
        ) -> None: ...

        def _assert_exact_admission_projection(
            self,
            *,
            revision: CognitiveStateRevision,
            command: Mapping[str, Any],
            state_receipt: Mapping[str, Any] | None,
            sample: sqlite3.Row | None,
            action: sqlite3.Row | None,
            receipt: sqlite3.Row | None,
        ) -> None: ...

    def _apply_tombstone_projection(
        self,
        *,
        command_id: str,
        target_revision_ids: tuple[str, ...],
        tombstone_hash: str,
        created_at: str,
    ) -> dict[str, Any]:
        """Append per-sample exclusion proof and remove affected model heads."""

        target_revision_ids_json = canonical_json(list(target_revision_ids))
        sample_ids: list[str] = []
        action_ids: list[str] = []
        receipt_ids: list[str] = []
        model_ids: tuple[str, ...] = ()
        remaining_heads = 0
        with sqlite3.connect(self.scoring_db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            schema = inspect_training_schema(conn)
            if not schema.ok:
                raise RuntimeError("governed training projection schema is not canonical")
            conn.execute("BEGIN IMMEDIATE")
            try:
                samples = conn.execute(
                    "SELECT sample_id, admission_revision_id "
                    "FROM governed_training_samples "
                    "WHERE admission_revision_id IN (SELECT value FROM json_each(?)) "
                    "ORDER BY sample_id",
                    (target_revision_ids_json,),
                ).fetchall()
                model_rows = conn.execute(
                    "SELECT DISTINCT model_id FROM ("
                    "SELECT m.model_id AS model_id "
                    "FROM governed_scorer_models AS m, "
                    "json_each(m.admission_revision_ids_json) AS admission "
                    "WHERE admission.value IN (SELECT value FROM json_each(?)) "
                    "UNION SELECT model_id FROM governed_scorer_models "
                    "WHERE run_revision_id IN (SELECT value FROM json_each(?))"
                    ") ORDER BY model_id",
                    (target_revision_ids_json, target_revision_ids_json),
                ).fetchall()
                model_ids = tuple(str(row["model_id"]) for row in model_rows)
                for sample in samples:
                    sample_id = str(sample["sample_id"])
                    admission_revision_id = str(sample["admission_revision_id"])
                    sample_ids.append(sample_id)
                    suffix = sha256_json({"command_id": command_id, "sample_id": sample_id}).split(
                        ":", 1
                    )[1][:32]
                    action_id = "training-sample-tombstone-action-" + suffix
                    receipt_id = "training-sample-tombstone-receipt-" + suffix
                    existing_action = conn.execute(
                        "SELECT * FROM governed_training_sample_actions WHERE action_id=?",
                        (action_id,),
                    ).fetchone()
                    existing_receipt = conn.execute(
                        "SELECT * FROM governed_training_sample_receipts WHERE receipt_id=?",
                        (receipt_id,),
                    ).fetchone()
                    if existing_action is not None or existing_receipt is not None:
                        if (
                            existing_action is None
                            or existing_receipt is None
                            or existing_action["sample_id"] != sample_id
                            or existing_action["admission_revision_id"] != admission_revision_id
                            or existing_action["action_type"] != "exclude"
                            or existing_action["reason_code"] != "subject_tombstone"
                            or existing_action["evidence_hash"] != tombstone_hash
                            or existing_receipt["command_id"] != command_id
                            or existing_receipt["sample_id"] != sample_id
                            or existing_receipt["action_id"] != action_id
                            or existing_receipt["status"] != "revoked"
                        ):
                            raise RuntimeError(
                                "immutable governed training tombstone replay conflict"
                            )
                        action_ids.append(action_id)
                        receipt_ids.append(receipt_id)
                        continue
                    latest = conn.execute(
                        "SELECT action_id, action_type FROM "
                        "governed_training_sample_actions WHERE sample_id=? "
                        "ORDER BY created_at DESC, action_id DESC LIMIT 1",
                        (sample_id,),
                    ).fetchone()
                    if latest is None:
                        raise RuntimeError("governed training tombstone lacks prior action")
                    before_hash = sha256_json(
                        {
                            "sample_id": sample_id,
                            "latest_action_id": str(latest["action_id"]),
                            "latest_action_type": str(latest["action_type"]),
                        }
                    )
                    after_hash = sha256_json(
                        {
                            "sample_id": sample_id,
                            "admission_revision_id": admission_revision_id,
                            "action_id": action_id,
                            "state": "excluded",
                            "reason_code": "subject_tombstone",
                            "tombstone_hash": tombstone_hash,
                        }
                    )
                    action_row = (
                        action_id,
                        sample_id,
                        admission_revision_id,
                        "exclude",
                        "subject_tombstone",
                        str(latest["action_id"]),
                        tombstone_hash,
                        created_at,
                    )
                    evidence_refs = [
                        f"tombstone-command:{command_id}",
                        f"training-admission:{admission_revision_id}",
                        f"governed-training-sample:{sample_id}",
                    ]
                    receipt_identity = {
                        "receipt_id": receipt_id,
                        "command_id": command_id,
                        "admission_revision_id": admission_revision_id,
                        "sample_id": sample_id,
                        "action_id": action_id,
                        "status": "revoked",
                        "before_hash": before_hash,
                        "after_hash": after_hash,
                        "evidence_refs": evidence_refs,
                    }
                    receipt_row = (
                        receipt_id,
                        command_id,
                        admission_revision_id,
                        sample_id,
                        action_id,
                        "revoked",
                        before_hash,
                        after_hash,
                        canonical_json(evidence_refs),
                        sha256_json(receipt_identity),
                        created_at,
                    )
                    conn.execute(
                        "INSERT INTO governed_training_sample_actions "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        action_row,
                    )
                    conn.execute(
                        "INSERT INTO governed_training_sample_receipts "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        receipt_row,
                    )
                    stored_action = conn.execute(
                        "SELECT * FROM governed_training_sample_actions WHERE action_id=?",
                        (action_id,),
                    ).fetchone()
                    stored_receipt = conn.execute(
                        "SELECT * FROM governed_training_sample_receipts WHERE receipt_id=?",
                        (receipt_id,),
                    ).fetchone()
                    if (
                        stored_action is None
                        or tuple(stored_action) != action_row
                        or stored_receipt is None
                        or tuple(stored_receipt) != receipt_row
                    ):
                        raise RuntimeError("immutable governed training tombstone conflict")
                    action_ids.append(action_id)
                    receipt_ids.append(receipt_id)
                if model_ids:
                    model_ids_json = canonical_json(list(model_ids))
                    conn.execute(
                        "DELETE FROM governed_scorer_model_heads "
                        "WHERE model_id IN (SELECT value FROM json_each(?))",
                        (model_ids_json,),
                    )
                    remaining_heads = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM governed_scorer_model_heads "
                            "WHERE model_id IN (SELECT value FROM json_each(?))",
                            (model_ids_json,),
                        ).fetchone()[0]
                    )
                if remaining_heads:
                    raise RuntimeError("governed training model head survived tombstone")
                oracle = {
                    "command_id": command_id,
                    "target_revision_ids": list(target_revision_ids),
                    "sample_ids": sample_ids,
                    "action_ids": action_ids,
                    "receipt_ids": receipt_ids,
                    "deactivated_model_ids": list(model_ids),
                    "remaining_model_head_count": remaining_heads,
                }
                projection_oracle_hash = sha256_json(oracle)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return {
            "status": "applied" if sample_ids or model_ids else "no_targets",
            "sample_count": len(sample_ids),
            "model_count": len(model_ids),
            "action_ids": tuple(action_ids),
            "receipt_ids": tuple(receipt_ids),
            "deactivated_model_ids": model_ids,
            "remaining_model_head_count": remaining_heads,
            "projection_oracle_hash": projection_oracle_hash,
        }

    def _training_command_for_outcome(self, outcome_revision_id: str) -> str:
        attributions = [
            revision
            for revision in self.state.current_revisions(object_type="feedback_attribution_record")
            if revision.source_revision_id == outcome_revision_id
            and any(
                item["revision_id"] == outcome_revision_id
                for item in revision.payload["outcome_refs"]
            )
        ]
        if len(attributions) != 1:
            raise ValueError("corrected outcome lacks one current COG-038 attribution")
        commands = [
            item
            for item in self.state.commands_for_revision(attributions[0].revision_id)
            if item["consumer_id"] == "training_evidence"
            and item["command_type"] == "evaluate_feedback_target"
            and item["payload"].get("objective_outcome_ref", {}).get("revision_id")
            == outcome_revision_id
        ]
        if len(commands) != 1:
            raise ValueError("corrected outcome lacks one COG-038 training command")
        return str(commands[0]["command_id"])

    def _mark_correction_pending_admission(
        self,
        admission: CognitiveStateRevision,
        *,
        corrected_outcome: CognitiveStateRevision,
        _failpoint: Callable[[str], None] | None = None,
    ) -> CognitiveStateRevision:
        original_revision_id = admission.revision_id
        suffix = sha256_json(
            {
                "admission_revision_id": original_revision_id,
                "corrected_outcome_revision_id": corrected_outcome.revision_id,
                "state": "correction_pending",
            }
        ).split(":", 1)[1][:32]
        created_at = self._clock()
        effect_id = "training-correction-pending-effect-" + suffix
        payload = json.loads(canonical_json(admission.payload))
        payload["revision_state"] = "corrected"
        payload["supersedes_revision_id"] = original_revision_id
        payload["correction_of_revision_id"] = original_revision_id
        payload["evidence_quality"]["exclusion_reason"] = "outcome_correction_pending"
        payload["lifecycle_state"] = "correction_pending"
        payload["target_effect_refs"] = {
            "projection_command_key": "training-correction-pending:" + suffix,
            "projection_effect_id": effect_id,
            "reciprocal_receipt_id": "training-correction-pending-receipt-" + suffix,
        }
        payload["input_set_hash"] = training_admission_input_hash(payload)
        event_id = "training-correction-pending-event-" + suffix
        evidence_refs = tuple(
            dict.fromkeys(
                (
                    *admission.evidence_refs,
                    admission.revision_id,
                    corrected_outcome.revision_id,
                )
            )
        )
        revision = CognitiveStateRevision.create(
            object_type="training_admission_record",
            object_id=admission.object_id,
            source_event_id=event_id,
            source_revision_id=corrected_outcome.revision_id,
            source_content_hash=corrected_outcome.payload_hash,
            scope_type=admission.scope_type,
            scope_id=admission.scope_id,
            evidence_refs=evidence_refs,
            payload=payload,
            supersedes_revision_id=original_revision_id,
            correction_of_revision_id=original_revision_id,
            created_at=created_at,
        )
        command = LocalConsumerCommand.create(
            revision_id=revision.revision_id,
            consumer_id=TRAINING_PROJECTION_CONSUMER,
            command_type="record_governed_training_correction_pending",
            payload={
                "schema_version": "mnemos.governed_training_correction_pending.v1",
                "admission_id": admission.object_id,
                "original_admission_revision_id": original_revision_id,
                "pending_revision_id": revision.revision_id,
                "pending_payload_hash": revision.payload_hash,
                "corrected_outcome_revision_id": corrected_outcome.revision_id,
                "effect_id": effect_id,
                "before_hash": admission.payload_hash,
                "after_hash": revision.payload_hash,
            },
            created_at=created_at,
        )
        event = CognitiveDataEvent(
            event_id=event_id,
            source_id=corrected_outcome.revision_id,
            asset_id=admission.object_id,
            source_kind="governed_training_correction_pending",
            source_uri=f"mnemos://training/admission/{admission.object_id}/correction-pending",
            content_hash=corrected_outcome.payload_hash,
            canonical_subject=f"training_admission_record:{admission.object_id}",
            data_type="training_admission_record",
            producer="training_governance_store",
            intended_consumers=(TRAINING_PROJECTION_CONSUMER,),
            privacy_level=str(payload["access_control"]["sensitivity"]),
            confidence=1.0,
            evidence_refs=evidence_refs,
            dedupe_key=(
                f"training-correction-pending:{original_revision_id}:"
                f"{corrected_outcome.revision_id}"
            ),
            created_at=created_at,
            retention_policy=str(payload["access_control"]["retention_policy"]),
            metadata={"revision_ids": [revision.revision_id]},
        )
        self.state.unit_of_work().commit(
            revisions=(revision,),
            event=event,
            commands=(command,),
            expected_heads=(
                CognitiveHeadPrecondition.create(
                    object_type=admission.object_type,
                    object_id=admission.object_id,
                    revision_id=admission.revision_id,
                ),
            ),
        )
        if _failpoint is not None:
            _failpoint("after_correction_pending_commit")
        self._apply_correction_pending(command)
        return revision

    def _apply_correction_pending(self, command: LocalConsumerCommand) -> None:
        payload = command.payload
        if (
            command.consumer_id != TRAINING_PROJECTION_CONSUMER
            or command.command_type != "record_governed_training_correction_pending"
            or payload.get("schema_version") != "mnemos.governed_training_correction_pending.v1"
        ):
            raise ValueError("governed training correction-pending command mismatch")
        pending = self.state.revision(str(payload["pending_revision_id"]))
        if (
            pending is None
            or pending.object_type != "training_admission_record"
            or pending.object_id != payload["admission_id"]
            or pending.payload_hash != payload["pending_payload_hash"]
            or pending.payload["lifecycle_state"] != "correction_pending"
            or pending.payload["correction_of_revision_id"]
            != payload["original_admission_revision_id"]
        ):
            raise ValueError("governed training correction-pending source mismatch")
        existing = self.state.effect_receipt(command.command_id)
        evidence_refs = (
            f"training-admission:{payload['original_admission_revision_id']}",
            f"training-correction-pending:{pending.revision_id}",
            f"corrected-outcome:{payload['corrected_outcome_revision_id']}",
        )
        if existing is None:
            self.state.record_effect_receipt(
                command.command_id,
                status="committed",
                target_effect_id=str(payload["effect_id"]),
                before_hash=str(payload["before_hash"]),
                after_hash=str(payload["after_hash"]),
                evidence_refs=evidence_refs,
                outcome="governed training correction marked pending",
                created_at=command.created_at,
            )
        elif (
            existing["status"] != "committed"
            or existing["target_effect_id"] != payload["effect_id"]
            or existing["before_hash"] != payload["before_hash"]
            or existing["after_hash"] != payload["after_hash"]
            or tuple(existing["evidence_refs"]) != evidence_refs
        ):
            raise RuntimeError("governed training correction-pending receipt conflict")

    def _exclude_corrected_admission(
        self,
        admission: CognitiveStateRevision,
        *,
        corrected_outcome: CognitiveStateRevision,
        _failpoint: Callable[[str], None] | None = None,
    ) -> CognitiveStateRevision:
        original_revision_id = str(
            admission.payload["correction_of_revision_id"] or admission.revision_id
        )
        original_admission = self.state.revision(original_revision_id)
        if (
            original_admission is None
            or original_admission.object_type != "training_admission_record"
            or original_admission.object_id != admission.object_id
            or original_admission.payload["lifecycle_state"] != "admitted"
            or admission.payload["lifecycle_state"] != "correction_pending"
        ):
            raise ValueError("training correction pending lineage is invalid")
        pending_commands = [
            item
            for item in self.state.commands_for_revision(admission.revision_id)
            if item["consumer_id"] == TRAINING_PROJECTION_CONSUMER
            and item["command_type"] == "record_governed_training_correction_pending"
        ]
        if len(pending_commands) != 1:
            raise RuntimeError("training correction-pending command gap")
        stored_pending = pending_commands[0]
        self._apply_correction_pending(
            LocalConsumerCommand.create(
                revision_id=admission.revision_id,
                consumer_id=str(stored_pending["consumer_id"]),
                command_type=str(stored_pending["command_type"]),
                payload=stored_pending["payload"],
                created_at=str(stored_pending["created_at"]),
            )
        )
        suffix = sha256_json(
            {
                "admission_revision_id": original_revision_id,
                "corrected_outcome_revision_id": corrected_outcome.revision_id,
            }
        ).split(":", 1)[1][:32]
        sample_id = "training-sample-" + admission.object_id.rsplit("-", 1)[1]
        action_id = "training-sample-exclude-action-" + suffix
        effect_id = "training-sample-exclude-effect-" + suffix
        receipt_id = "training-sample-exclude-receipt-" + suffix
        created_at = self._clock()
        payload = json.loads(canonical_json(admission.payload))
        payload["revision_state"] = "corrected"
        payload["supersedes_revision_id"] = admission.revision_id
        payload["correction_of_revision_id"] = original_revision_id
        payload["evidence_quality"]["exclusion_reason"] = "outcome_corrected"
        payload["lifecycle_state"] = "excluded"
        payload["target_effect_refs"] = {
            "projection_command_key": "training-exclusion:" + suffix,
            "projection_effect_id": effect_id,
            "reciprocal_receipt_id": receipt_id,
        }
        payload["input_set_hash"] = training_admission_input_hash(payload)
        event_id = "training-exclusion-event-" + suffix
        evidence_refs = tuple(
            dict.fromkeys(
                (
                    *admission.evidence_refs,
                    admission.revision_id,
                    corrected_outcome.revision_id,
                )
            )
        )
        revision = CognitiveStateRevision.create(
            object_type="training_admission_record",
            object_id=admission.object_id,
            source_event_id=event_id,
            source_revision_id=corrected_outcome.revision_id,
            source_content_hash=corrected_outcome.payload_hash,
            scope_type=admission.scope_type,
            scope_id=admission.scope_id,
            evidence_refs=evidence_refs,
            payload=payload,
            supersedes_revision_id=admission.revision_id,
            correction_of_revision_id=original_revision_id,
            created_at=created_at,
        )
        command = LocalConsumerCommand.create(
            revision_id=revision.revision_id,
            consumer_id=TRAINING_PROJECTION_CONSUMER,
            command_type="exclude_governed_training_sample",
            payload={
                "schema_version": "mnemos.governed_training_sample_exclusion.v1",
                "admission_id": admission.object_id,
                "original_admission_revision_id": original_revision_id,
                "correction_revision_id": revision.revision_id,
                "correction_payload_hash": revision.payload_hash,
                "corrected_outcome_revision_id": corrected_outcome.revision_id,
                "sample_id": sample_id,
                "action_id": action_id,
                "reason_code": "outcome_corrected",
                "projection_effect_id": effect_id,
                "projection_receipt_id": receipt_id,
            },
            created_at=created_at,
        )
        event = CognitiveDataEvent(
            event_id=event_id,
            source_id=corrected_outcome.revision_id,
            asset_id=admission.object_id,
            source_kind="governed_training_exclusion",
            source_uri=f"mnemos://training/admission/{admission.object_id}/exclusion",
            content_hash=corrected_outcome.payload_hash,
            canonical_subject=f"training_admission_record:{admission.object_id}",
            data_type="training_admission_record",
            producer="training_governance_store",
            intended_consumers=(TRAINING_PROJECTION_CONSUMER,),
            privacy_level=str(payload["access_control"]["sensitivity"]),
            confidence=1.0,
            evidence_refs=evidence_refs,
            dedupe_key=f"training-exclusion:{original_revision_id}:{corrected_outcome.revision_id}",
            created_at=created_at,
            retention_policy=str(payload["access_control"]["retention_policy"]),
            metadata={"revision_ids": [revision.revision_id]},
        )
        self.state.unit_of_work().commit(
            revisions=(revision,),
            event=event,
            commands=(command,),
            expected_heads=(
                CognitiveHeadPrecondition.create(
                    object_type=admission.object_type,
                    object_id=admission.object_id,
                    revision_id=admission.revision_id,
                ),
            ),
        )
        if _failpoint is not None:
            _failpoint("after_correction_exclusion_commit")
        self._apply_exclusion_projection(command)
        return revision

    def _ensure_exclusion_projection(
        self,
        admission: CognitiveStateRevision,
    ) -> None:
        if (
            admission.object_type != "training_admission_record"
            or admission.payload["lifecycle_state"] != "excluded"
        ):
            raise ValueError("training exclusion recovery requires an excluded admission")
        commands = [
            item
            for item in self.state.commands_for_revision(admission.revision_id)
            if item["consumer_id"] == TRAINING_PROJECTION_CONSUMER
            and item["command_type"] == "exclude_governed_training_sample"
        ]
        if len(commands) != 1:
            raise RuntimeError("training exclusion projection command gap")
        stored = commands[0]
        command = LocalConsumerCommand.create(
            revision_id=admission.revision_id,
            consumer_id=str(stored["consumer_id"]),
            command_type=str(stored["command_type"]),
            payload=stored["payload"],
            created_at=str(stored["created_at"]),
        )
        if command.command_id != stored["command_id"]:
            raise RuntimeError("training exclusion projection command identity mismatch")
        self._apply_exclusion_projection(command)

    def _stale_dependent_runs(
        self,
        admission_revision_id: str,
        *,
        _failpoint: Callable[[str], None] | None = None,
    ) -> None:
        stale_existing = [
            revision
            for revision in self.state.current_revisions(
                object_type="training_run_record"
            )
            if revision.payload["state"] == "stale"
            and admission_revision_id
            in {
                str(item["revision_id"])
                for item in revision.payload["admission_refs"]
            }
        ]
        for revision in sorted(
            stale_existing,
            key=lambda item: item.revision_id,
        ):
            self._ensure_stale_run_projection(revision)
        dependent = [
            revision
            for revision in self.state.current_revisions(object_type="training_run_record")
            if revision.payload["state"]
            in {"model_sealed", "sealed", "applied", "insufficient_sample"}
            and admission_revision_id
            in {str(item["revision_id"]) for item in revision.payload["admission_refs"]}
        ]
        for revision in sorted(dependent, key=lambda item: item.revision_id):
            suffix = sha256_json(
                {
                    "run_revision_id": revision.revision_id,
                    "excluded_admission_revision_id": admission_revision_id,
                }
            ).split(":", 1)[1][:32]
            payload = json.loads(canonical_json(revision.payload))
            payload["state"] = "stale"
            payload["supersedes_revision_id"] = revision.revision_id
            payload["material_effect_refs"] = {
                "action_id": "training-run-stale-action-" + suffix,
                "effect_id": "training-run-stale-effect-" + suffix,
            }
            payload["projection_receipt_ref"] = {
                "receipt_id": "governed-training-run-stale-receipt-" + suffix,
                "receipt_hash": sha256_json(
                    {
                        "run_id": revision.object_id,
                        "state": "stale",
                        "excluded_admission_revision_id": admission_revision_id,
                    }
                ),
            }
            payload["run_input_hash"] = training_run_input_hash(payload)
            created_at = self._clock()
            stale, command, event = self._run_revision_command_event(
                payload,
                source_revision_id=revision.revision_id,
                source_content_hash=revision.payload_hash,
                created_at=created_at,
                supersedes_revision_id=revision.revision_id,
            )
            self.state.unit_of_work().commit(
                revisions=(stale,),
                event=event,
                commands=(command,),
                expected_heads=(
                    CognitiveHeadPrecondition.create(
                        object_type=revision.object_type,
                        object_id=revision.object_id,
                        revision_id=revision.revision_id,
                    ),
                ),
            )
            if _failpoint is not None:
                _failpoint("after_training_run_stale_commit")
            self._apply_run_projection(command)

    def _ensure_stale_run_projection(
        self,
        revision: CognitiveStateRevision,
    ) -> None:
        commands = tuple(
            command
            for command in self.state.commands_for_revision(
                revision.revision_id
            )
            if command["consumer_id"] == TRAINING_PROJECTION_CONSUMER
            and command["command_type"] == "project_governed_training_run"
        )
        if len(commands) != 1:
            raise RuntimeError("stale governed run projection command gap")
        stored = commands[0]
        command = LocalConsumerCommand.create(
            revision_id=revision.revision_id,
            consumer_id=str(stored["consumer_id"]),
            command_type=str(stored["command_type"]),
            payload=stored["payload"],
            created_at=str(stored["created_at"]),
        )
        if command.command_id != stored["command_id"]:
            raise RuntimeError("stale governed run command identity mismatch")
        self._apply_run_projection(command)

    def _verified_current_admissions(
        self,
    ) -> tuple[tuple[CognitiveStateRevision, ...], list[dict[str, Any]]]:
        revisions = tuple(
            sorted(
                (
                    revision
                    for revision in self.state.current_revisions(
                        object_type="training_admission_record"
                    )
                    if revision.payload["lifecycle_state"] == "admitted"
                ),
                key=lambda item: item.revision_id,
            )
        )
        refs: list[dict[str, Any]] = []
        with sqlite3.connect(
            f"file:{self.scoring_db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            for revision in revisions:
                self._verify_admission_upstream(revision)
                validate_cognitive_state_payload(
                    "training_admission_record",
                    revision.payload,
                )
                commands = [
                    item
                    for item in self.state.commands_for_revision(revision.revision_id)
                    if item["consumer_id"] == TRAINING_PROJECTION_CONSUMER
                    and item["command_type"] == TRAINING_PROJECTION_COMMAND
                ]
                if len(commands) != 1:
                    raise RuntimeError("training admission projection command gap")
                command = commands[0]
                state_receipt = self.state.effect_receipt(str(command["command_id"]))
                sample = conn.execute(
                    "SELECT * FROM governed_training_samples " "WHERE admission_revision_id=?",
                    (revision.revision_id,),
                ).fetchone()
                sample_receipt = conn.execute(
                    "SELECT * FROM governed_training_sample_receipts "
                    "WHERE admission_revision_id=?",
                    (revision.revision_id,),
                ).fetchone()
                latest_action = conn.execute(
                    "SELECT * FROM governed_training_sample_actions "
                    "WHERE admission_revision_id=? "
                    "ORDER BY created_at DESC, action_id DESC LIMIT 1",
                    (revision.revision_id,),
                ).fetchone()
                payload = revision.payload
                self._assert_exact_admission_projection(
                    revision=revision,
                    command=command,
                    state_receipt=state_receipt,
                    sample=sample,
                    action=latest_action,
                    receipt=sample_receipt,
                )
                refs.append(
                    {
                        "revision_id": revision.revision_id,
                        "payload_hash": revision.payload_hash,
                        "feature_snapshot_hash": payload["feature_snapshot"]["snapshot_hash"],
                        "label_numeric": payload["label"]["numeric_value"],
                        "split": payload["dataset_assignment"]["split"],
                        "group_hash": payload["dataset_assignment"]["group_hash"],
                    }
                )
        return revisions, refs

    @staticmethod
    def _readiness_satisfied(admission_refs: list[dict[str, Any]]) -> bool:
        train_labels = [
            int(item["label_numeric"]) for item in admission_refs if item["split"] == "train"
        ]
        return bool(
            len(train_labels) >= 20
            and train_labels.count(0) >= 2
            and train_labels.count(1) >= 2
            and sum(item["split"] == "validation" for item in admission_refs) >= 2
            and sum(item["split"] == "holdout" for item in admission_refs) >= 2
        )

    @staticmethod
    def _fit_centroid(
        admissions: tuple[CognitiveStateRevision, ...],
    ) -> dict[str, Any]:
        train = [
            revision
            for revision in admissions
            if revision.payload["dataset_assignment"]["split"] == "train"
        ]
        by_label = {
            label: [
                revision
                for revision in train
                if revision.payload["label"]["numeric_value"] == label
            ]
            for label in (0, 1)
        }
        centroids: dict[int, list[float]] = {}
        for label, examples in by_label.items():
            if not examples:
                raise RuntimeError("training centroid class is empty")
            centroids[label] = [
                sum(
                    float(revision.payload["feature_snapshot"]["values"][name])
                    for revision in examples
                )
                / len(examples)
                for name in FEATURE_NAMES
            ]
        return {
            "feature_names": list(FEATURE_NAMES),
            "negative_centroid": centroids[0],
            "positive_centroid": centroids[1],
        }

    @staticmethod
    def _evaluation_report(
        admissions: tuple[CognitiveStateRevision, ...],
        *,
        split: str,
        model_blob: Mapping[str, Any],
        evaluated_after_model_sealed_at: str = "",
    ) -> dict[str, Any]:
        examples = [
            revision
            for revision in admissions
            if revision.payload["dataset_assignment"]["split"] == split
        ]
        negative = tuple(float(value) for value in model_blob["negative_centroid"])
        positive = tuple(float(value) for value in model_blob["positive_centroid"])
        predictions = []
        for revision in examples:
            vector = tuple(
                float(revision.payload["feature_snapshot"]["values"][name])
                for name in FEATURE_NAMES
            )
            negative_distance = sum(
                (value - center) ** 2 for value, center in zip(vector, negative)
            )
            positive_distance = sum(
                (value - center) ** 2 for value, center in zip(vector, positive)
            )
            predicted = 1 if positive_distance <= negative_distance else 0
            predictions.append(
                {
                    "revision_id": revision.revision_id,
                    "actual": int(revision.payload["label"]["numeric_value"]),
                    "predicted": predicted,
                }
            )
        report = {
            "schema_version": "mnemos.governed_training_evaluation.v1",
            "split": split,
            "example_count": len(examples),
            "correct": sum(item["actual"] == item["predicted"] for item in predictions),
            "predictions_hash": sha256_json(predictions),
        }
        return {
            "example_count": len(examples),
            "report_hash": sha256_json(report),
            **(
                {
                    "evaluated_after_model_sealed_at": evaluated_after_model_sealed_at,
                }
                if split == "holdout"
                else {}
            ),
        }

    def _current_model_ref(self) -> dict[str, str]:
        with sqlite3.connect(
            f"file:{self.scoring_db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT m.model_id, m.model_blob_hash
                FROM governed_scorer_model_heads AS h
                JOIN governed_scorer_models AS m ON m.model_id=h.model_id
                WHERE h.dimension=?
                """,
                (TRAINING_DIMENSION,),
            ).fetchone()
        if row is None:
            return {"model_id": "", "model_hash": ""}
        return {
            "model_id": str(row["model_id"]),
            "model_hash": str(row["model_blob_hash"]),
        }

    @staticmethod
    def _run_access_control(
        admissions: tuple[CognitiveStateRevision, ...],
    ) -> dict[str, Any]:
        if not admissions:
            return make_cognitive_access_envelope(
                owner_principal_id="system:training-governance",
                owner_agent="mnemos",
                scope_type="project",
                scope_id="mnemos",
                project="mnemos",
                purposes=("cognitive_state_read", "cognitive_state_write"),
                consent_provenance_refs=("system-policy:COG-048",),
                sensitivity="sensitive",
                retention_policy="governed_training_model",
                source_acl_lineage=(READINESS_POLICY_HASH,),
            )
        first = admissions[0].payload["access_control"]
        access = derive_strictest_cognitive_access(
            tuple(revision.payload["access_control"] for revision in admissions),
            owner_principal_id=str(first["owner"]["principal_id"]),
            owner_agent=str(first["owner"]["agent"]),
            scope_type="project",
            scope_id=str(first["scope"]["project"] or "mnemos"),
            purposes=("cognitive_state_read", "cognitive_state_write"),
            retention_policy="governed_training_model",
        )
        if access["scope"]["resolution"] != "resolved":
            raise PermissionError("governed training admissions have incompatible access contexts")
        return access

    @staticmethod
    def _run_payload(
        *,
        run_id: str,
        state: str,
        created_at: str,
        algorithm: Mapping[str, Any],
        admission_refs: list[dict[str, Any]],
        manifest: Mapping[str, Any],
        fit_input_hash: str,
        validation_report: Mapping[str, Any],
        holdout_report: Mapping[str, Any],
        parent_model_ref: Mapping[str, Any],
        model_artifact: Mapping[str, Any],
        bayesian_prior_artifact: Mapping[str, Any],
        rule_optimizer_artifact: Mapping[str, Any],
        access_control: Mapping[str, Any],
        supersedes_revision_id: str,
        rebuild_of_revision_id: str,
    ) -> dict[str, Any]:
        suffix = run_id.removeprefix("training-run-")
        payload: dict[str, Any] = {
            "access_control": dict(access_control),
            "schema_version": TRAINING_RUN_SCHEMA_VERSION,
            "run_id": run_id,
            "run_input_hash": "",
            "dimension": TRAINING_DIMENSION,
            "algorithm": dict(algorithm),
            "admission_refs": list(admission_refs),
            "dataset_manifest": dict(manifest),
            "fit_input_hash": fit_input_hash,
            "validation_report": dict(validation_report),
            "holdout_report": dict(holdout_report),
            "parent_model_ref": dict(parent_model_ref),
            "model_artifact": dict(model_artifact),
            "bayesian_prior_artifact": dict(bayesian_prior_artifact),
            "rule_optimizer_artifact": dict(rule_optimizer_artifact),
            "state": state,
            "material_effect_refs": {
                "action_id": f"training-run-{state}-action-{suffix}",
                "effect_id": f"training-run-{state}-effect-{suffix}",
            },
            "projection_receipt_ref": {
                "receipt_id": f"governed-training-run-{state}-receipt-{suffix}",
                "receipt_hash": sha256_json({"run_id": run_id, "state": state}),
            },
            "supersedes_revision_id": supersedes_revision_id,
            "rebuild_of_revision_id": rebuild_of_revision_id,
        }
        payload["run_input_hash"] = training_run_input_hash(payload)
        return payload

    def _run_revision_command_event(
        self,
        payload: Mapping[str, Any],
        *,
        source_revision_id: str,
        source_content_hash: str,
        created_at: str,
        supersedes_revision_id: str = "",
    ) -> tuple[CognitiveStateRevision, LocalConsumerCommand, CognitiveDataEvent]:
        run_id = str(payload["run_id"])
        state = str(payload["state"])
        suffix = run_id.removeprefix("training-run-")
        event_id = f"training-run-{state}-event-{suffix}"
        evidence_refs = tuple(
            [
                str(payload["dataset_manifest"]["manifest_hash"]),
                *(str(item["revision_id"]) for item in payload["admission_refs"]),
            ]
        )
        revision = CognitiveStateRevision.create(
            object_type="training_run_record",
            object_id=run_id,
            source_event_id=event_id,
            source_revision_id=source_revision_id,
            source_content_hash=source_content_hash,
            scope_type="project",
            scope_id="mnemos",
            evidence_refs=evidence_refs,
            payload=payload,
            supersedes_revision_id=supersedes_revision_id,
            created_at=created_at,
        )
        command = LocalConsumerCommand.create(
            revision_id=revision.revision_id,
            consumer_id=TRAINING_PROJECTION_CONSUMER,
            command_type="project_governed_training_run",
            payload={
                "schema_version": "mnemos.governed_training_run_projection.v1",
                "run_id": run_id,
                "run_revision_id": revision.revision_id,
                "run_payload_hash": revision.payload_hash,
                "state": state,
                "projection_effect_id": payload["material_effect_refs"]["effect_id"],
                "projection_receipt_id": payload["projection_receipt_ref"]["receipt_id"],
            },
            created_at=created_at,
        )
        event = CognitiveDataEvent(
            event_id=event_id,
            source_id=source_revision_id,
            asset_id=run_id,
            source_kind="governed_training_run",
            source_uri=f"mnemos://training/run/{run_id}/{state}",
            content_hash=source_content_hash,
            canonical_subject=f"training_run_record:{run_id}",
            data_type="training_run_record",
            producer="training_governance_store",
            intended_consumers=(TRAINING_PROJECTION_CONSUMER,),
            privacy_level=str(payload["access_control"]["sensitivity"]),
            confidence=1.0,
            evidence_refs=evidence_refs,
            dedupe_key=f"training-run:{run_id}:{state}",
            created_at=created_at,
            retention_policy=str(payload["access_control"]["retention_policy"]),
            metadata={"revision_ids": [revision.revision_id]},
        )
        return revision, command, event

    def _run_receipt_from_revision(
        self,
        revision: CognitiveStateRevision,
    ) -> TrainingRunReceipt:
        commands = [
            item
            for item in self.state.commands_for_revision(revision.revision_id)
            if item["command_type"] == "project_governed_training_run"
        ]
        if len(commands) != 1:
            raise RuntimeError("governed training run projection command gap")
        command = commands[0]
        command_object = LocalConsumerCommand.create(
            revision_id=revision.revision_id,
            consumer_id=str(command["consumer_id"]),
            command_type=str(command["command_type"]),
            payload=command["payload"],
            created_at=str(command["created_at"]),
        )
        self._apply_run_projection(command_object)
        state = str(revision.payload["state"])
        return TrainingRunReceipt(
            status=state,
            run_id=revision.object_id,
            run_revision_id=revision.revision_id,
            model_id=str(revision.payload["model_artifact"]["model_id"]),
            projection_command_id=str(command["command_id"]),
            projection_receipt_id=str(revision.payload["projection_receipt_ref"]["receipt_id"]),
        )

    def _apply_run_projection(self, command: LocalConsumerCommand) -> None:
        payload = command.payload
        if (
            payload.get("schema_version") != "mnemos.governed_training_run_projection.v1"
            or command.consumer_id != TRAINING_PROJECTION_CONSUMER
            or command.command_type != "project_governed_training_run"
        ):
            raise ValueError("governed training run projection command mismatch")
        self._validate_projection_schema()
        revision = self.state.revision(str(payload["run_revision_id"]))
        if (
            revision is None
            or revision.object_type != "training_run_record"
            or revision.object_id != payload["run_id"]
            or revision.payload_hash != payload["run_payload_hash"]
            or revision.payload["state"] != payload["state"]
        ):
            raise ValueError("governed training run projection source mismatch")
        validate_cognitive_state_payload("training_run_record", revision.payload)

        run = revision.payload
        state = str(run["state"])
        if state not in {
            "model_sealed",
            "sealed",
            "applied",
            "insufficient_sample",
            "stale",
        }:
            raise ValueError("governed training run state is not projectable")
        dimension = str(run["dimension"])
        model = run["model_artifact"]
        parent = run["parent_model_ref"]
        superseded = (
            self.state.revision(revision.supersedes_revision_id) if state == "stale" else None
        )
        receipt_model_id: str | None
        if state == "stale":
            if (
                superseded is None
                or superseded.object_type != "training_run_record"
                or superseded.object_id != revision.object_id
                or superseded.payload["state"]
                not in {"model_sealed", "sealed", "applied", "insufficient_sample"}
            ):
                raise ValueError("stale governed run lacks its exact prior revision")
            before_hash = sha256_json(
                {
                    "run_revision_id": superseded.revision_id,
                    "run_payload_hash": superseded.payload_hash,
                    "model_id": model["model_id"],
                    "model_hash": model["blob_hash"],
                    "state": superseded.payload["state"],
                }
            )
            after_hash = sha256_json(
                {
                    "run_revision_id": revision.revision_id,
                    "run_payload_hash": revision.payload_hash,
                    "state": "stale",
                }
            )
            receipt_status = "stale"
            receipt_model_id = (
                str(model["model_id"]) if superseded.payload["state"] == "applied" else None
            )
        else:
            before_hash = sha256_json(
                {
                    "dimension": dimension,
                    "model_id": parent["model_id"],
                    "model_hash": parent["model_hash"],
                }
            )
        if state == "applied":
            after_hash = sha256_json(
                {
                    "dimension": dimension,
                    "model_id": model["model_id"],
                    "model_hash": model["blob_hash"],
                    "run_revision_id": revision.revision_id,
                }
            )
            receipt_status = "committed"
            receipt_model_id = str(model["model_id"])
        elif state != "stale":
            after_hash = sha256_json(
                {
                    "run_revision_id": revision.revision_id,
                    "run_payload_hash": revision.payload_hash,
                    "state": state,
                }
            )
            receipt_status = state
            receipt_model_id = None

        evidence_refs = [
            f"training-run:{revision.revision_id}",
            f"training-manifest:{run['dataset_manifest']['manifest_hash']}",
            f"training-projection-command:{command.command_id}",
        ]
        if receipt_model_id:
            evidence_refs.append(f"governed-scorer-model:{receipt_model_id}")
        receipt_identity = {
            "receipt_id": str(payload["projection_receipt_id"]),
            "command_id": command.command_id,
            "run_revision_id": revision.revision_id,
            "run_payload_hash": revision.payload_hash,
            "model_id": receipt_model_id,
            "status": receipt_status,
            "action_id": str(run["material_effect_refs"]["action_id"]),
            "effect_id": str(run["material_effect_refs"]["effect_id"]),
            "before_hash": before_hash,
            "after_hash": after_hash,
            "evidence_refs": evidence_refs,
        }
        projection_receipt_hash = sha256_json(receipt_identity)
        receipt_row = (
            receipt_identity["receipt_id"],
            command.command_id,
            revision.revision_id,
            revision.payload_hash,
            receipt_model_id,
            receipt_status,
            receipt_identity["action_id"],
            receipt_identity["effect_id"],
            before_hash,
            after_hash,
            canonical_json(evidence_refs),
            projection_receipt_hash,
            command.created_at,
        )
        aux_effect_rows, aux_receipt_rows, aux_reciprocal_refs = self._aux_projection_rows(
            revision,
            command,
            run_before_hash=before_hash,
        )

        with sqlite3.connect(self.scoring_db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            schema = inspect_training_schema(conn)
            if not schema.ok:
                raise RuntimeError("governed training projection schema is not canonical")
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing_receipt = conn.execute(
                    "SELECT * FROM governed_training_run_receipts WHERE receipt_id=?",
                    (receipt_identity["receipt_id"],),
                ).fetchone()
                if existing_receipt is None and state == "applied":
                    current_head = conn.execute(
                        """
                        SELECT h.model_id, m.model_blob_hash
                        FROM governed_scorer_model_heads AS h
                        JOIN governed_scorer_models AS m ON m.model_id=h.model_id
                        WHERE h.dimension=?
                        """,
                        (dimension,),
                    ).fetchone()
                    expected_parent = (
                        None
                        if not parent["model_id"]
                        else (str(parent["model_id"]), str(parent["model_hash"]))
                    )
                    if current_head != expected_parent:
                        raise RuntimeError("governed training model head changed after run sealing")
                    model_row = (
                        str(model["model_id"]),
                        revision.revision_id,
                        revision.payload_hash,
                        canonical_json(run["dataset_manifest"]["admission_revision_ids"]),
                        dimension,
                        str(model["model_type"]),
                        canonical_json(model["blob"]),
                        str(model["blob_hash"]),
                        str(run["dataset_manifest"]["manifest_hash"]),
                        str(run["fit_input_hash"]),
                        str(run["validation_report"]["report_hash"]),
                        str(run["holdout_report"]["report_hash"]),
                        cognitive_access_hash(run["access_control"]),
                        str(parent["model_id"]) or None,
                        command.created_at,
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO governed_scorer_models VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        model_row,
                    )
                    stored_model = conn.execute(
                        "SELECT * FROM governed_scorer_models WHERE model_id=?",
                        (str(model["model_id"]),),
                    ).fetchone()
                    if stored_model is None or tuple(stored_model) != model_row:
                        raise RuntimeError("immutable governed scorer model conflict")
                    conn.execute(
                        """
                        INSERT INTO governed_scorer_model_heads(
                            dimension, model_id, run_revision_id, activated_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(dimension) DO UPDATE SET
                            model_id=excluded.model_id,
                            run_revision_id=excluded.run_revision_id,
                            activated_at=excluded.activated_at
                        """,
                        (
                            dimension,
                            str(model["model_id"]),
                            revision.revision_id,
                            command.created_at,
                        ),
                    )
                if existing_receipt is None and state == "stale" and receipt_model_id is not None:
                    conn.execute(
                        "DELETE FROM governed_scorer_model_heads "
                        "WHERE dimension=? AND model_id=?",
                        (dimension, receipt_model_id),
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO governed_training_run_receipts VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    receipt_row,
                )
                stored_receipt = conn.execute(
                    "SELECT * FROM governed_training_run_receipts WHERE receipt_id=?",
                    (receipt_identity["receipt_id"],),
                ).fetchone()
                if stored_receipt is None or tuple(stored_receipt) != receipt_row:
                    raise RuntimeError("immutable governed training run receipt conflict")
                for effect_row in aux_effect_rows:
                    conn.execute(
                        "INSERT OR IGNORE INTO governed_training_aux_effects VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        effect_row,
                    )
                    stored_effect = conn.execute(
                        "SELECT * FROM governed_training_aux_effects WHERE effect_id=?",
                        (effect_row[0],),
                    ).fetchone()
                    if stored_effect is None or tuple(stored_effect) != effect_row:
                        raise RuntimeError("immutable governed training auxiliary effect conflict")
                for aux_receipt_row in aux_receipt_rows:
                    conn.execute(
                        "INSERT OR IGNORE INTO governed_training_aux_receipts VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        aux_receipt_row,
                    )
                    stored_aux_receipt = conn.execute(
                        "SELECT * FROM governed_training_aux_receipts WHERE receipt_id=?",
                        (aux_receipt_row[0],),
                    ).fetchone()
                    if stored_aux_receipt is None or tuple(stored_aux_receipt) != aux_receipt_row:
                        raise RuntimeError("immutable governed training auxiliary receipt conflict")
                if state == "applied":
                    stored_head = conn.execute(
                        "SELECT dimension, model_id, run_revision_id, activated_at "
                        "FROM governed_scorer_model_heads WHERE dimension=?",
                        (dimension,),
                    ).fetchone()
                    expected_head = (
                        dimension,
                        str(model["model_id"]),
                        revision.revision_id,
                        command.created_at,
                    )
                    if stored_head != expected_head:
                        raise RuntimeError("governed scorer model head conflict")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        existing_effect = self.state.effect_receipt(command.command_id)
        if existing_effect is None:
            self.state.record_effect_receipt(
                command.command_id,
                status="committed",
                target_effect_id=str(run["material_effect_refs"]["effect_id"]),
                before_hash=before_hash,
                after_hash=after_hash,
                evidence_refs=(
                    f"training-run:{revision.revision_id}",
                    (
                        "governed-training-run-receipt:"
                        + str(receipt_identity["receipt_id"])
                        + ":"
                        + projection_receipt_hash
                    ),
                    *(evidence_refs[-1:] if receipt_model_id else ()),
                    *aux_reciprocal_refs,
                ),
                outcome=f"governed training run {state} projection committed",
                created_at=command.created_at,
            )
        else:
            expected_state_evidence_refs = (
                f"training-run:{revision.revision_id}",
                (
                    "governed-training-run-receipt:"
                    + str(receipt_identity["receipt_id"])
                    + ":"
                    + projection_receipt_hash
                ),
                *(evidence_refs[-1:] if receipt_model_id else ()),
                *aux_reciprocal_refs,
            )
            if (
                existing_effect["status"] != "committed"
                or existing_effect["target_effect_id"] != run["material_effect_refs"]["effect_id"]
                or existing_effect["before_hash"] != before_hash
                or existing_effect["after_hash"] != after_hash
                or tuple(existing_effect["evidence_refs"]) != expected_state_evidence_refs
            ):
                raise RuntimeError("governed training run state receipt conflict")
