"""Governed admission and projection owner for COG-048 training evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping

from core.access_policy import PrincipalEnvelope
from core.cognitive.access_control import (
    cognitive_access_hash,
    validate_cognitive_access_envelope,
)
from core.cognitive.feedback_contract import FEEDBACK_TARGETS
from core.cognitive.state_contract import (
    CognitiveStateRevision,
    LocalConsumerCommand,
    sha256_json,
    validate_cognitive_state_payload,
)
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.state_types import (
    COGNITIVE_TOMBSTONE_COMMAND_TYPE,
    COGNITIVE_TOMBSTONE_SCHEMA_VERSION,
)
from core.cognitive.training_contract import (
    TRAINING_ADMISSION_COMMAND,
    TRAINING_ADMISSION_CONSUMER,
    TRAINING_ADMISSION_SUPERSEDED_REASON,
    validate_training_admission_intake_payload,
)
from core.cognitive.training_migration_barrier import (
    assert_training_governance_enabled,
)
from core.cognitive.training_governance_admission_impl import (
    _TrainingGovernanceAdmissionImplementation,
)
from core.cognitive.training_governance_aux_impl import (
    _TrainingGovernanceAuxImplementation,
)
from core.cognitive.training_governance_run_impl import (
    _TrainingGovernanceRunImplementation,
)
from core.cognitive.training_governance_model_impl import (
    _TrainingGovernanceModelImplementation,
)
from core.ops.cognitive_data_contract import CognitiveDataEvent
from core.cognitive.training_governance_types import (
    TRAINING_PROJECTION_COMMAND,
    TRAINING_PROJECTION_CONSUMER,
    TrainingAdmissionIntakeReceipt,
    TrainingAdmissionReconciliationReport,
    TrainingAdmissionReceipt,
    TrainingEvidenceNotReady,
    TrainingReconciliationReport,
)


class TrainingGovernanceStore(
    _TrainingGovernanceAdmissionImplementation,
    _TrainingGovernanceRunImplementation,
    _TrainingGovernanceModelImplementation,
    _TrainingGovernanceAuxImplementation,
):
    """Resolve identities, revalidate truth, derive data, and close receipts."""

    def __init__(
        self,
        state_store: CognitiveStateStore,
        *,
        database_dir: Path,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(state_store, CognitiveStateStore):
            raise TypeError("TrainingGovernanceStore requires CognitiveStateStore")
        self.state = state_store
        self.database_dir = Path(database_dir).expanduser()
        if self.state.db_path.parent.resolve() != self.database_dir.resolve():
            raise ValueError("training governance database directory mismatch")
        self.scoring_db_path = self.database_dir / "mnemos.db"
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self._validate_projection_schema()

    def _assert_migration_clear(self) -> None:
        assert_training_governance_enabled(self.database_dir)

    def process_admission_intake(
        self,
        command_id: str,
        *,
        _failpoint: Callable[[str], None] | None = None,
    ) -> TrainingAdmissionIntakeReceipt:
        """Consume one durable attribution-owned admission obligation."""

        self._assert_migration_clear()
        intake = self._validated_admission_intake(
            command_id,
            require_current=False,
        )
        source_outcome = self.state.revision(
            str(intake["payload"]["outcome_ref"]["revision_id"])
        )
        current_outcome = (
            None
            if source_outcome is None
            else self.state.current_revision(
                "outcome_measurement",
                source_outcome.object_id,
            )
        )
        if current_outcome != source_outcome:
            if (
                source_outcome is None
                or current_outcome is None
                or not self._outcome_correction_descends_from(
                    current_outcome,
                    source_outcome,
                )
            ):
                raise ValueError(
                    "training admission intake source lineage is stale"
                )
            return self._close_superseded_admission_intake(
                intake,
                corrected_outcome=current_outcome,
            )
        intake = self._validated_admission_intake(command_id)
        if not self._feedback_manifest_is_terminal(intake):
            return TrainingAdmissionIntakeReceipt(
                status="deferred",
                command_id=str(intake["command_id"]),
            )
        target_command_id = str(
            intake["payload"]["training_target_ref"]["command_id"]
        )
        try:
            outcome = self.state.revision(
                str(intake["payload"]["outcome_ref"]["revision_id"])
            )
            if (
                outcome is not None
                and outcome.correction_of_revision_id
            ):
                admission = self._admit_corrected_intake(
                    intake,
                    corrected_outcome=outcome,
                    _failpoint=_failpoint,
                )
            else:
                admission = self.admit_training_evidence(
                    target_command_id,
                    _failpoint=_failpoint,
                )
        except TrainingEvidenceNotReady:
            return TrainingAdmissionIntakeReceipt(
                status="deferred",
                command_id=str(intake["command_id"]),
            )
        _call_training_failpoint(
            _failpoint,
            "before_admission_intake_terminal_receipt",
        )
        before_hash = sha256_json(
            {"command_id": intake["command_id"], "state": "pending"}
        )
        after_hash = sha256_json(
            {
                "command_id": intake["command_id"],
                "admission_revision_id": admission.admission_revision_id,
                "sample_id": admission.sample_id,
                "projection_command_id": admission.projection_command_id,
                "projection_receipt_id": admission.projection_receipt_id,
                "state": "committed",
            }
        )
        effect_id = (
            "governed-training-admission-intake-effect-"
            + str(intake["command_id"]).removeprefix("cogcmd-")
        )
        evidence_refs = (
            f"training-admission-intake:{intake['command_id']}",
            "feedback-attribution:"
            + str(intake["payload"]["attribution_ref"]["revision_id"]),
            "outcome-measurement:"
            + str(intake["payload"]["outcome_ref"]["revision_id"]),
            f"feedback-command:{target_command_id}",
            f"training-admission:{admission.admission_revision_id}",
            f"governed-training-sample:{admission.sample_id}",
            "governed-training-receipt:" + admission.projection_receipt_id,
        )
        existing = self.state.effect_receipt(str(intake["command_id"]))
        if existing is None:
            terminal = self.state.record_effect_receipt(
                str(intake["command_id"]),
                status="committed",
                target_effect_id=effect_id,
                before_hash=before_hash,
                after_hash=after_hash,
                evidence_refs=evidence_refs,
                outcome="governed training admission intake committed",
                created_at=str(intake["created_at"]),
            )
            receipt_id = terminal.receipt_id
        else:
            if (
                existing["status"] != "committed"
                or existing["target_effect_id"] != effect_id
                or existing["before_hash"] != before_hash
                or existing["after_hash"] != after_hash
                or tuple(existing["evidence_refs"]) != evidence_refs
            ):
                raise RuntimeError("governed training admission intake receipt conflict")
            receipt_id = str(existing["receipt_id"])
        return TrainingAdmissionIntakeReceipt(
            status="committed",
            command_id=str(intake["command_id"]),
            admission_id=admission.admission_id,
            admission_revision_id=admission.admission_revision_id,
            sample_id=admission.sample_id,
            dataset_split=admission.dataset_split,
            projection_command_id=admission.projection_command_id,
            projection_receipt_id=admission.projection_receipt_id,
            intake_effect_receipt_id=receipt_id,
        )

    def _admit_corrected_intake(
        self,
        intake: Mapping[str, Any],
        *,
        corrected_outcome: CognitiveStateRevision,
        _failpoint: Callable[[str], None] | None = None,
    ) -> TrainingAdmissionReceipt:
        """Converge exclusion and stale effects before corrected admission."""

        if (
            corrected_outcome.object_type != "outcome_measurement"
            or self.state.current_revision(
                "outcome_measurement",
                corrected_outcome.object_id,
            )
            != corrected_outcome
        ):
            raise ValueError("training correction outcome is not current")
        prior_outcome_revision_id = str(
            corrected_outcome.correction_of_revision_id or ""
        )
        if (
            not prior_outcome_revision_id
            or corrected_outcome.supersedes_revision_id
            != prior_outcome_revision_id
            or corrected_outcome.payload["correction_of_revision_id"]
            != prior_outcome_revision_id
            or corrected_outcome.payload["supersedes_revision_id"]
            != prior_outcome_revision_id
        ):
            raise ValueError("training correction lacks exact outcome lineage")
        prior_outcome = self.state.revision(prior_outcome_revision_id)
        if (
            prior_outcome is None
            or prior_outcome.object_type != "outcome_measurement"
            or prior_outcome.object_id != corrected_outcome.object_id
            or prior_outcome.payload["prediction_ref"]
            != corrected_outcome.payload["prediction_ref"]
        ):
            raise ValueError("training correction prior outcome binding is invalid")
        self._supersede_prior_admission_intakes(
            prior_outcome,
            corrected_outcome=corrected_outcome,
        )
        _call_training_failpoint(
            _failpoint,
            "after_prior_training_intake_supersession",
        )
        target_command_id = str(
            intake["payload"]["training_target_ref"]["command_id"]
        )
        principal = self._principal_for_admission_intake(intake)
        evidence = self._resolve_feedback_evidence(
            target_command_id,
            principal,
        )
        if evidence.outcome != corrected_outcome:
            raise ValueError("training correction command binds a different outcome")
        affected = [
            revision
            for revision in self.state.current_revisions(
                object_type="training_admission_record"
            )
            if revision.payload["outcome_ref"]["revision_id"]
            == prior_outcome_revision_id
        ]
        if len(affected) > 1:
            raise ValueError("training correction has multiple affected admissions")
        if affected:
            old_current = affected[0]
            if old_current.payload["lifecycle_state"] == "admitted":
                old_current = self._mark_correction_pending_admission(
                    old_current,
                    corrected_outcome=corrected_outcome,
                    _failpoint=_failpoint,
                )
            _call_training_failpoint(
                _failpoint,
                "after_training_correction_pending",
            )
            if old_current.payload["lifecycle_state"] == "correction_pending":
                old_current = self._exclude_corrected_admission(
                    old_current,
                    corrected_outcome=corrected_outcome,
                    _failpoint=_failpoint,
                )
            elif old_current.payload["lifecycle_state"] == "excluded":
                self._ensure_exclusion_projection(old_current)
            else:
                raise RuntimeError(
                    "training correction admission is not safely excluded"
                )
            _call_training_failpoint(
                _failpoint,
                "after_training_correction_exclusion",
            )
            original_revision_id = str(
                old_current.payload["correction_of_revision_id"]
                or old_current.revision_id
            )
            self._stale_dependent_runs(
                original_revision_id,
                _failpoint=_failpoint,
            )
            _call_training_failpoint(
                _failpoint,
                "after_training_correction_stale",
            )
        return self.admit_training_evidence(
            target_command_id,
            _failpoint=_failpoint,
        )

    def _supersede_prior_admission_intakes(
        self,
        prior_outcome: CognitiveStateRevision,
        *,
        corrected_outcome: CognitiveStateRevision,
    ) -> None:
        for command in self.state.pending_commands(
            TRAINING_ADMISSION_CONSUMER
        ):
            payload = command.get("payload") or {}
            if (
                command.get("command_type") == TRAINING_ADMISSION_COMMAND
                and payload.get("outcome_ref", {}).get("revision_id")
                == prior_outcome.revision_id
            ):
                intake = self._validated_admission_intake(
                    str(command["command_id"]),
                    require_current=False,
                )
                self._close_superseded_admission_intake(
                    intake,
                    corrected_outcome=corrected_outcome,
                )

    def _close_superseded_admission_intake(
        self,
        intake: Mapping[str, Any],
        *,
        corrected_outcome: CognitiveStateRevision,
    ) -> TrainingAdmissionIntakeReceipt:
        command_id = str(intake["command_id"])
        source_outcome_revision_id = str(
            intake["payload"]["outcome_ref"]["revision_id"]
        )
        source_outcome = self.state.revision(source_outcome_revision_id)
        if (
            source_outcome is None
            or not self._outcome_correction_descends_from(
                corrected_outcome,
                source_outcome,
            )
        ):
            raise ValueError(
                "training intake supersession lacks exact outcome correction"
            )
        unchanged_hash = str(intake["payload_hash"])
        target_effect_id = (
            "governed-training-admission-intake-superseded-"
            + command_id.removeprefix("cogcmd-")
        )
        evidence_refs = (
            f"training-admission-intake:{command_id}",
            f"outcome-measurement:{source_outcome.revision_id}",
            f"corrected-outcome:{corrected_outcome.revision_id}",
            f"no-effect-oracle:{command_id}:{unchanged_hash}",
        )
        existing = self.state.effect_receipt(command_id)
        if existing is None:
            terminal = self.state.record_effect_receipt(
                command_id,
                status="rejected",
                target_effect_id=target_effect_id,
                before_hash=unchanged_hash,
                after_hash=unchanged_hash,
                evidence_refs=evidence_refs,
                outcome="superseded_before_admission",
                terminal_reason_code=(
                    TRAINING_ADMISSION_SUPERSEDED_REASON
                ),
                created_at=corrected_outcome.created_at,
            )
            receipt_id = terminal.receipt_id
        else:
            if (
                existing["status"] != "rejected"
                or existing["target_effect_id"] != target_effect_id
                or existing["before_hash"] != unchanged_hash
                or existing["after_hash"] != unchanged_hash
                or tuple(existing["evidence_refs"]) != evidence_refs
                or existing["reason_code"]
                != TRAINING_ADMISSION_SUPERSEDED_REASON
            ):
                raise RuntimeError(
                    "training admission intake supersession receipt conflict"
                )
            receipt_id = str(existing["receipt_id"])
        return TrainingAdmissionIntakeReceipt(
            status="superseded",
            command_id=command_id,
            intake_effect_receipt_id=receipt_id,
        )

    def _outcome_correction_descends_from(
        self,
        current: CognitiveStateRevision,
        ancestor: CognitiveStateRevision,
    ) -> bool:
        if (
            current.object_type != "outcome_measurement"
            or ancestor.object_type != "outcome_measurement"
            or current.object_id != ancestor.object_id
        ):
            return False
        cursor = current
        seen: set[str] = set()
        while cursor.revision_id != ancestor.revision_id:
            if (
                cursor.revision_id in seen
                or not cursor.correction_of_revision_id
                or cursor.supersedes_revision_id
                != cursor.correction_of_revision_id
                or cursor.payload["correction_of_revision_id"]
                != cursor.correction_of_revision_id
                or cursor.payload["supersedes_revision_id"]
                != cursor.correction_of_revision_id
            ):
                return False
            seen.add(cursor.revision_id)
            parent = self.state.revision(
                cursor.correction_of_revision_id
            )
            if (
                parent is None
                or parent.object_type != "outcome_measurement"
                or parent.object_id != ancestor.object_id
            ):
                return False
            cursor = parent
        return True

    def validate_admission_projection(
        self,
        admission_revision_id: str,
    ) -> Mapping[str, Any]:
        """Independently re-prove one admission's complete sample projection."""

        self._assert_migration_clear()
        admission = self.state.revision(str(admission_revision_id or ""))
        if (
            admission is None
            or admission.object_type != "training_admission_record"
            or self.state.current_revision(
                "training_admission_record",
                admission.object_id,
            )
            != admission
        ):
            raise RuntimeError("training admission projection source is not current")
        validate_cognitive_state_payload(
            "training_admission_record",
            admission.payload,
        )
        commands = tuple(
            command
            for command in self.state.commands_for_revision(
                admission.revision_id
            )
            if command["consumer_id"] == TRAINING_PROJECTION_CONSUMER
            and command["command_type"] == TRAINING_PROJECTION_COMMAND
        )
        if len(commands) != 1:
            raise RuntimeError("training admission projection command gap")
        command = commands[0]
        sample_id = str(command["payload"]["sample_id"])
        action_id = "training-sample-action-" + sample_id.rsplit("-", 1)[1]
        projection_receipt_id = str(
            command["payload"]["projection_receipt_id"]
        )
        with sqlite3.connect(
            f"file:{self.scoring_db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            sample = conn.execute(
                "SELECT * FROM governed_training_samples WHERE sample_id=?",
                (sample_id,),
            ).fetchone()
            action = conn.execute(
                "SELECT * FROM governed_training_sample_actions WHERE action_id=?",
                (action_id,),
            ).fetchone()
            receipt = conn.execute(
                "SELECT * FROM governed_training_sample_receipts WHERE receipt_id=?",
                (projection_receipt_id,),
            ).fetchone()
        self._assert_exact_admission_projection(
            revision=admission,
            command=command,
            state_receipt=self.state.effect_receipt(
                str(command["command_id"])
            ),
            sample=sample,
            action=action,
            receipt=receipt,
        )
        return command

    def reconcile_admission_intakes(
        self,
        limit: int,
    ) -> TrainingAdmissionReconciliationReport:
        """Replay one bounded page of durable admission obligations."""

        self._assert_migration_clear()
        bounded_limit = int(limit)
        if bounded_limit <= 0 or bounded_limit > 1000:
            raise ValueError("training admission reconciliation limit must be in [1, 1000]")
        pending = self.state.pending_commands(TRAINING_ADMISSION_CONSUMER)[
            :bounded_limit
        ]
        committed: list[str] = []
        superseded: list[str] = []
        deferred: list[str] = []
        failed: list[str] = []
        for stored in pending:
            command_id = str(stored["command_id"])
            try:
                result = self.process_admission_intake(command_id)
            except (
                KeyError,
                PermissionError,
                TypeError,
                ValueError,
                RuntimeError,
                sqlite3.Error,
            ):
                failed.append(command_id)
            else:
                if result.status == "committed":
                    committed.append(command_id)
                elif result.status == "superseded":
                    superseded.append(command_id)
                elif result.status == "deferred":
                    deferred.append(command_id)
                else:
                    failed.append(command_id)
        return TrainingAdmissionReconciliationReport(
            scanned=len(pending),
            committed=len(committed),
            superseded=len(superseded),
            deferred=len(deferred),
            failed=len(failed),
            remaining=len(self.state.pending_commands(TRAINING_ADMISSION_CONSUMER)),
            committed_command_ids=tuple(committed),
            superseded_command_ids=tuple(superseded),
            deferred_command_ids=tuple(deferred),
            failed_command_ids=tuple(failed),
        )

    def _validated_admission_intake(
        self,
        command_id: str,
        *,
        require_current: bool = True,
    ) -> Mapping[str, Any]:
        normalized_id = str(command_id or "").strip()
        command = self.state.command(normalized_id)
        if (
            command is None
            or command["consumer_id"] != TRAINING_ADMISSION_CONSUMER
            or command["command_type"] != TRAINING_ADMISSION_COMMAND
        ):
            raise ValueError("training admission intake command contract mismatch")
        validate_training_admission_intake_payload(command["payload"])
        recomputed = LocalConsumerCommand.create(
            revision_id=str(command["revision_id"]),
            consumer_id=str(command["consumer_id"]),
            command_type=str(command["command_type"]),
            payload=command["payload"],
            created_at=str(command["created_at"]),
        )
        if (
            recomputed.command_id != command["command_id"]
            or recomputed.payload_hash != command["payload_hash"]
        ):
            raise ValueError("training admission intake command identity mismatch")
        attribution = self.state.revision(str(command["revision_id"]))
        attribution_ref = command["payload"]["attribution_ref"]
        outcome_ref = command["payload"]["outcome_ref"]
        outcome = self.state.revision(str(outcome_ref["revision_id"]))
        if (
            attribution is None
            or attribution.object_type != "feedback_attribution_record"
            or attribution.object_id != attribution_ref["object_id"]
            or attribution.revision_id != attribution_ref["revision_id"]
            or attribution.payload_hash != attribution_ref["payload_hash"]
            or outcome is None
            or outcome.object_type != "outcome_measurement"
            or outcome.object_id != outcome_ref["object_id"]
            or outcome.payload_hash != outcome_ref["payload_hash"]
        ):
            raise ValueError("training admission intake source lineage is stale")
        if require_current and (
            self.state.current_revision(
                "feedback_attribution_record",
                attribution.object_id,
            )
            != attribution
            or self.state.current_revision(
                "outcome_measurement",
                outcome.object_id,
            )
            != outcome
        ):
            raise ValueError("training admission intake source lineage is stale")
        authority = outcome.payload["source_authority"]
        expected_authority_refs = sorted(
            {
                str(authority["source_authority_id"]),
                str(authority["source_id"]),
                str(authority["source_revision_id"]),
                str(authority["source_authority_catalog_hash"]),
            }
        )
        if (
            command["payload"]["source_authority_refs"]
            != expected_authority_refs
            or command["payload"]["correction_lineage"]
            != {
                "supersedes_revision_id": attribution.supersedes_revision_id,
                "correction_of_revision_id": attribution.correction_of_revision_id,
            }
            or {
                "outcome_id": outcome.object_id,
                "revision_id": outcome.revision_id,
                "payload_hash": outcome.payload_hash,
            }
            not in [dict(item) for item in attribution.payload["outcome_refs"]]
        ):
            raise ValueError("training admission intake provenance binding mismatch")
        return command

    def _feedback_manifest_is_terminal(
        self,
        intake: Mapping[str, Any],
    ) -> bool:
        manifest = intake["payload"]["required_feedback_commands"]
        expected_ids = {str(row["command_id"]) for row in manifest}
        actual_feedback = tuple(
            command
            for command in self.state.commands_for_revision(
                str(intake["revision_id"])
            )
            if command["consumer_id"] in FEEDBACK_TARGETS
            and command["command_type"]
            in {"evaluate_feedback_target", "neutralize_feedback_effect"}
        )
        if {str(command["command_id"]) for command in actual_feedback} != expected_ids:
            raise ValueError("training admission feedback manifest is incomplete")
        for row in manifest:
            command_id = str(row["command_id"])
            command = self.state.command(command_id)
            if (
                command is None
                or command["revision_id"] != intake["revision_id"]
                or command["consumer_id"] != row["consumer_id"]
                or command["command_type"] != row["command_type"]
                or command["payload_hash"] != row["payload_hash"]
            ):
                raise ValueError("training admission feedback command binding mismatch")
            if self.state.effect_receipt(command_id) is None:
                return False
            self.state.validate_feedback_effect_receipt(command_id)
        return True

    def _principal_for_admission_intake(
        self,
        intake: Mapping[str, Any],
    ) -> PrincipalEnvelope:
        attribution = self.state.revision(str(intake["revision_id"]))
        if attribution is None:
            raise ValueError("training admission attribution is unavailable")
        access = validate_cognitive_access_envelope(
            attribution.payload["access_control"],
            expected_scope_type=attribution.scope_type,
            expected_scope_id=attribution.scope_id,
        )
        payload = intake["payload"]
        source_identity = payload["source_identity"]
        source_access = payload["source_access"]
        if (
            source_identity
            != {
                "principal_id": access["owner"]["principal_id"],
                "agent": access["owner"]["agent"],
            }
            or source_access["access_control_hash"]
            != cognitive_access_hash(access)
            or source_access["scope_type"] != attribution.scope_type
            or source_access["scope_id"] != attribution.scope_id
            or source_access["project"] != access["scope"]["project"]
            or source_access["session_id"] != access["scope"]["session_id"]
            or source_access["visibility"] != access["visibility"]
            or source_access["consent_status"] != access["consent"]["status"]
            or source_access["sensitivity"] != access["sensitivity"]
            or source_access["retention_policy"] != access["retention_policy"]
        ):
            raise PermissionError("training admission intake source access mismatch")
        project = str(access["scope"]["project"])
        return PrincipalEnvelope(
            principal_id=str(source_identity["principal_id"]),
            agent=str(source_identity["agent"]),
            host_kind="training_governance",
            capability_id="durable-training-admission-intake",
            capabilities=frozenset({"memory_read", "memory_write"}),
            allowed_projects=frozenset({project}),
        )

    def _matching_admission_intake(
        self,
        target_command: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        matches = []
        for command in self.state.commands_for_revision(
            str(target_command["revision_id"])
        ):
            if (
                command["consumer_id"] != TRAINING_ADMISSION_CONSUMER
                or command["command_type"] != TRAINING_ADMISSION_COMMAND
            ):
                continue
            validate_training_admission_intake_payload(command["payload"])
            target_ref = command["payload"]["training_target_ref"]
            if (
                target_ref["command_id"] == target_command["command_id"]
                and target_ref["payload_hash"] == target_command["payload_hash"]
            ):
                matches.append(command)
        if len(matches) != 1:
            raise ValueError("training evidence requires one durable admission intake")
        return self._validated_admission_intake(str(matches[0]["command_id"]))

    def _verify_admission_upstream(
        self,
        revision: CognitiveStateRevision,
    ) -> None:
        """Re-derive one admitted sample from its complete current evidence chain."""

        payload = revision.payload
        target_command_id = str(
            payload["training_evidence_ref"]["command_id"]
        )
        target_command = self.state.command(target_command_id)
        if target_command is None:
            raise ValueError("training admission upstream command is unavailable")
        intake = self._matching_admission_intake(target_command)
        intake_command_id = str(intake["command_id"])
        intake_receipt = self.state.effect_receipt(intake_command_id)
        if intake_receipt is None or intake_receipt["status"] != "committed":
            raise ValueError("training admission intake is not terminally committed")
        self.state.validate_training_admission_intake_receipt(
            intake_command_id
        )
        principal = self._principal_for_admission_intake(intake)
        evidence = self._resolve_feedback_evidence(
            target_command_id,
            principal,
            _governance_time=revision.created_at,
        )
        suffix = revision.object_id.rsplit("-", 1)[-1]
        targets = payload["target_effect_refs"]
        expected = self._build_admission_revision(
            evidence,
            principal=principal,
            admission_id=revision.object_id,
            sample_id="training-sample-" + suffix,
            projection_command_key=str(
                targets["projection_command_key"]
            ),
            projection_effect_id=str(targets["projection_effect_id"]),
            projection_receipt_id=str(targets["reciprocal_receipt_id"]),
            created_at=revision.created_at,
        )
        if expected != revision:
            raise ValueError(
                "training admission does not match its current upstream chain"
            )

    def admit_training_evidence(
        self,
        command_id: str,
        *,
        _failpoint: Callable[[str], None] | None = None,
    ) -> TrainingAdmissionReceipt:
        """Admit one COG-038 objective command from its durable ACL binding."""

        self._assert_migration_clear()
        target_command = self.state.command(str(command_id or ""))
        if target_command is None:
            raise ValueError("training evidence command is unavailable")
        intake = self._matching_admission_intake(target_command)
        if not self._feedback_manifest_is_terminal(intake):
            raise ValueError("training admission feedback manifest is not terminal")
        resolved_principal = self._principal_for_admission_intake(intake)
        evidence = self._resolve_feedback_evidence(
            command_id,
            resolved_principal,
        )
        suffix = sha256_json(
            {
                "command_id": evidence.command["command_id"],
                "outcome_revision_id": evidence.outcome.revision_id,
                "prediction_revision_id": evidence.prediction.revision_id,
                "prediction_terminal_revision_id": (
                    evidence.prediction_terminal.revision_id
                ),
            }
        ).split(":", 1)[1][:32]
        admission_id = "training-admission-" + suffix
        sample_id = "training-sample-" + suffix
        command_key = "training-projection:" + suffix
        projection_effect_id = "governed-training-sample-effect-" + suffix
        projection_receipt_id = "governed-training-sample-receipt-" + suffix

        revision = self._build_admission_revision(
            evidence,
            principal=resolved_principal,
            admission_id=admission_id,
            sample_id=sample_id,
            projection_command_key=command_key,
            projection_effect_id=projection_effect_id,
            projection_receipt_id=projection_receipt_id,
            created_at=self._admission_effective_at(evidence),
        )
        command = LocalConsumerCommand.create(
            revision_id=revision.revision_id,
            consumer_id=TRAINING_PROJECTION_CONSUMER,
            command_type=TRAINING_PROJECTION_COMMAND,
            payload=self._projection_payload(
                revision,
                sample_id=sample_id,
                projection_command_key=command_key,
                projection_effect_id=projection_effect_id,
                projection_receipt_id=projection_receipt_id,
            ),
            created_at=revision.created_at,
        )
        event = CognitiveDataEvent(
            event_id=revision.source_event_id,
            source_id=str(evidence.command["command_id"]),
            asset_id=admission_id,
            source_kind="governed_training_admission",
            source_uri=f"mnemos://training/admission/{admission_id}",
            content_hash=str(evidence.command["payload_hash"]),
            canonical_subject=f"training_admission_record:{admission_id}",
            data_type="training_admission_record",
            producer="training_governance_store",
            intended_consumers=(TRAINING_PROJECTION_CONSUMER,),
            privacy_level=str(revision.payload["access_control"]["sensitivity"]),
            confidence=1.0,
            evidence_refs=revision.evidence_refs,
            dedupe_key=f"training-admission:{admission_id}",
            created_at=revision.created_at,
            retention_policy=str(revision.payload["access_control"]["retention_policy"]),
            metadata={
                "revision_ids": [revision.revision_id],
                "access_control_hash": cognitive_access_hash(revision.payload["access_control"]),
            },
        )
        current = self.state.current_revision("training_admission_record", admission_id)
        if current is None:
            self.state.unit_of_work().commit(
                revisions=(revision,),
                event=event,
                commands=(command,),
            )
        elif current != revision:
            raise RuntimeError("immutable training admission replay conflict")
        _call_training_failpoint(_failpoint, "after_admission_revision_commit")
        self._apply_projection(command)
        _call_training_failpoint(_failpoint, "after_scoring_sample_projection")
        return TrainingAdmissionReceipt(
            status="committed",
            admission_id=admission_id,
            admission_revision_id=revision.revision_id,
            sample_id=sample_id,
            dataset_split=str(revision.payload["dataset_assignment"]["split"]),
            projection_command_id=command.command_id,
            projection_receipt_id=projection_receipt_id,
        )

    def reconcile_pending(self, limit: int) -> TrainingReconciliationReport:
        """Retry only already-committed governed projection commands."""

        self._assert_migration_clear()
        bounded_limit = int(limit)
        if bounded_limit <= 0 or bounded_limit > 1000:
            raise ValueError("training reconciliation limit must be in [1, 1000]")
        pending = self.state.pending_commands(TRAINING_PROJECTION_CONSUMER)[:bounded_limit]
        projected: list[str] = []
        failed: list[str] = []
        for stored in pending:
            command = LocalConsumerCommand.create(
                revision_id=str(stored["revision_id"]),
                consumer_id=str(stored["consumer_id"]),
                command_type=str(stored["command_type"]),
                payload=stored["payload"],
                created_at=str(stored["created_at"]),
            )
            if command.command_id != stored["command_id"]:
                failed.append(str(stored["command_id"]))
                continue
            try:
                if command.command_type == TRAINING_PROJECTION_COMMAND:
                    self._apply_projection(command)
                elif command.command_type == "exclude_governed_training_sample":
                    self._apply_exclusion_projection(command)
                elif command.command_type == "project_governed_training_run":
                    self._apply_run_projection(command)
                elif command.command_type == "record_governed_training_correction_pending":
                    self._apply_correction_pending(command)
                elif command.command_type == COGNITIVE_TOMBSTONE_COMMAND_TYPE:
                    self.apply_tombstone_command(command.command_id)
                else:
                    raise ValueError("unknown governed training projection command")
            except (KeyError, TypeError, ValueError, RuntimeError, sqlite3.Error):
                failed.append(command.command_id)
            else:
                projected.append(command.command_id)
        remaining = len(self.state.pending_commands(TRAINING_PROJECTION_CONSUMER))
        return TrainingReconciliationReport(
            scanned=len(pending),
            projected=len(projected),
            failed=len(failed),
            remaining=remaining,
            projected_command_ids=tuple(projected),
            failed_command_ids=tuple(failed),
        )

    def apply_tombstone_command(self, command_id: str) -> dict[str, Any]:
        """Exclude governed samples and deactivate models for one COG-043 tombstone."""

        self._assert_migration_clear()
        normalized = str(command_id or "").strip()
        command = self.state.command(normalized)
        if command is None:
            raise ValueError("training tombstone command is unavailable")
        payload = command["payload"]
        target_ids = tuple(sorted(str(value) for value in payload.get("target_revision_ids", ())))
        if (
            command["consumer_id"] != TRAINING_PROJECTION_CONSUMER
            or command["command_type"] != COGNITIVE_TOMBSTONE_COMMAND_TYPE
            or payload.get("schema_version") != COGNITIVE_TOMBSTONE_SCHEMA_VERSION
            or not target_ids
            or len(target_ids) != len(set(target_ids))
            or TRAINING_PROJECTION_CONSUMER not in set(payload.get("required_consumers") or ())
            or not str(payload.get("request_id") or "")
            or not str(payload.get("before_hash") or "").startswith("sha256:")
            or not str(payload.get("tombstone_hash") or "").startswith("sha256:")
        ):
            raise ValueError("governed training tombstone command mismatch")
        self._validate_projection_schema()
        result = self._apply_tombstone_projection(
            command_id=normalized,
            target_revision_ids=target_ids,
            tombstone_hash=str(payload["tombstone_hash"]),
            created_at=str(command["created_at"]),
        )
        expected_target = f"tombstone:{TRAINING_PROJECTION_CONSUMER}:{payload['request_id']}"
        evidence_refs = (
            f"tombstone-command:{normalized}",
            "tombstone-oracle:governed-training:" + str(result["projection_oracle_hash"]),
            *(
                "governed-training-tombstone-receipt:" + str(receipt_id)
                for receipt_id in result["receipt_ids"]
            ),
        )
        existing = self.state.effect_receipt(normalized)
        if existing is None:
            self.state.record_effect_receipt(
                normalized,
                status="committed",
                target_effect_id=expected_target,
                before_hash=str(payload["before_hash"]),
                after_hash=str(payload["tombstone_hash"]),
                evidence_refs=evidence_refs,
                outcome="governed training projection tombstoned",
                created_at=str(command["created_at"]),
            )
        elif (
            existing["status"] != "committed"
            or existing["target_effect_id"] != expected_target
            or existing["before_hash"] != payload["before_hash"]
            or existing["after_hash"] != payload["tombstone_hash"]
            or tuple(existing["evidence_refs"]) != evidence_refs
        ):
            raise RuntimeError("governed training tombstone receipt conflict")
        return result


def _call_training_failpoint(
    failpoint: Callable[[str], None] | None,
    name: str,
) -> None:
    if failpoint is not None:
        failpoint(name)
