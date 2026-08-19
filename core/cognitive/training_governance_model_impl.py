"""Governed model build, seal, load, stale, and apply lifecycle."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from core.cognitive.state_store import CognitiveStateStore
    from core.ops.cognitive_data_contract import CognitiveDataEvent

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import authorize_cognitive_access, cognitive_access_hash
from core.cognitive.state_contract import (
    CognitiveHeadPrecondition,
    CognitiveStateRevision,
    LocalConsumerCommand,
    canonical_json,
    sha256_json,
    validate_cognitive_state_payload,
)
from core.cognitive.training_contract import (
    READINESS_POLICY_HASH,
    TRAINING_ALGORITHM_CODE_HASH,
    TRAINING_ALGORITHM_CONFIG_HASH,
    TRAINING_ALGORITHM_SPEC_HASH,
    TRAINING_ALGORITHM_VERSION,
    TRAINING_DIMENSION,
    derive_bayesian_prior_artifact,
    derive_rule_optimizer_artifact,
    governed_training_examples,
    training_dataset_manifest,
    training_fit_input_hash,
    training_run_input_hash,
)
from core.cognitive.training_governance_types import (
    GovernedModelSnapshot,
    TRAINING_PROJECTION_CONSUMER,
    TrainingRunReceipt,
)
from core.scoring.training_schema import inspect_training_schema


class _TrainingGovernanceModelImplementation:
    """Internal model lifecycle seam used by TrainingGovernanceStore."""

    if TYPE_CHECKING:
        state: CognitiveStateStore
        scoring_db_path: Path
        _clock: Callable[[], str]

        def _assert_migration_clear(self) -> None: ...

        def _verified_current_admissions(
            self,
        ) -> tuple[tuple[CognitiveStateRevision, ...], list[dict[str, Any]]]: ...

        @staticmethod
        def _aux_projection_rows(
            revision: CognitiveStateRevision,
            command: LocalConsumerCommand,
            *,
            run_before_hash: str,
        ) -> tuple[
            tuple[tuple[Any, ...], ...],
            tuple[tuple[Any, ...], ...],
            tuple[str, ...],
        ]: ...

        def _run_receipt_from_revision(
            self,
            revision: CognitiveStateRevision,
        ) -> TrainingRunReceipt: ...

        def _current_model_ref(self) -> dict[str, str]: ...

        @staticmethod
        def _readiness_satisfied(admission_refs: list[dict[str, Any]]) -> bool: ...

        @staticmethod
        def _run_access_control(
            admissions: tuple[CognitiveStateRevision, ...],
        ) -> dict[str, Any]: ...

        @staticmethod
        def _fit_centroid(
            admissions: tuple[CognitiveStateRevision, ...],
        ) -> dict[str, Any]: ...

        @staticmethod
        def _evaluation_report(
            admissions: tuple[CognitiveStateRevision, ...],
            *,
            split: str,
            model_blob: Mapping[str, Any],
            evaluated_after_model_sealed_at: str = "",
        ) -> dict[str, Any]: ...

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
        ) -> dict[str, Any]: ...

        def _run_revision_command_event(
            self,
            payload: Mapping[str, Any],
            *,
            source_revision_id: str,
            source_content_hash: str,
            created_at: str,
            supersedes_revision_id: str = "",
        ) -> tuple[CognitiveStateRevision, LocalConsumerCommand, CognitiveDataEvent]: ...

        def _apply_run_projection(self, command: LocalConsumerCommand) -> None: ...

    def load_applied_model(
        self,
        run_revision_id: str,
        principal: PrincipalEnvelope,
    ) -> GovernedModelSnapshot:
        """Load one exact current governed model or fail closed."""

        self._assert_migration_clear()
        if not isinstance(principal, PrincipalEnvelope):
            raise TypeError("governed model load requires a server principal")
        revision = self.state.revision(str(run_revision_id or ""))
        if (
            revision is None
            or revision.object_type != "training_run_record"
            or revision.payload["state"] != "applied"
            or self.state.current_revision("training_run_record", revision.object_id) != revision
        ):
            raise ValueError("governed model run is not current and applied")
        validate_cognitive_state_payload("training_run_record", revision.payload)
        self._validate_durable_model_seal_lineage(revision)
        access = revision.payload["access_control"]
        authorization = authorize_cognitive_access(
            access,
            principal=principal,
            narrowing=AccessNarrowing(
                session_id=str(access["scope"]["session_id"]),
                project=str(access["scope"]["project"]),
            ),
            purpose="cognitive_state_read",
        )
        if not authorization.allowed:
            raise PermissionError(f"governed model access denied: {authorization.reason}")
        _admissions, current_refs = self._verified_current_admissions()
        if current_refs != list(revision.payload["admission_refs"]):
            raise RuntimeError("governed model admission manifest is stale")
        commands = [
            item
            for item in self.state.commands_for_revision(revision.revision_id)
            if item["consumer_id"] == TRAINING_PROJECTION_CONSUMER
            and item["command_type"] == "project_governed_training_run"
        ]
        if len(commands) != 1:
            raise RuntimeError("governed model run command gap")
        command = commands[0]
        state_receipt = self.state.effect_receipt(str(command["command_id"]))
        if state_receipt is None or state_receipt["status"] != "committed":
            raise RuntimeError("governed model reciprocal state receipt gap")

        model = revision.payload["model_artifact"]
        with sqlite3.connect(
            f"file:{self.scoring_db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            schema = inspect_training_schema(conn)
            if not schema.ok:
                raise RuntimeError("governed training projection schema is not canonical")
            row = conn.execute(
                """
                SELECT m.*, h.dimension AS head_dimension,
                       h.model_id AS head_model_id,
                       h.run_revision_id AS head_run_revision_id,
                       r.receipt_id AS receipt_id,
                       r.command_id AS receipt_command_id,
                       r.run_payload_hash AS receipt_run_payload_hash,
                       r.model_id AS receipt_model_id,
                       r.status AS receipt_status,
                       r.action_id AS receipt_action_id,
                       r.effect_id AS receipt_effect_id,
                       r.before_hash AS receipt_before_hash,
                       r.after_hash AS receipt_after_hash,
                       r.evidence_refs_json AS receipt_evidence_refs_json,
                       r.receipt_hash AS receipt_hash
                FROM governed_scorer_models AS m
                JOIN governed_scorer_model_heads AS h ON h.model_id=m.model_id
                JOIN governed_training_run_receipts AS r
                  ON r.run_revision_id=m.run_revision_id
                WHERE m.model_id=?
                """,
                (str(model["model_id"]),),
            ).fetchone()
            aux_effect_rows = conn.execute(
                "SELECT * FROM governed_training_aux_effects "
                "WHERE run_revision_id=? ORDER BY effect_kind",
                (revision.revision_id,),
            ).fetchall()
            aux_receipt_rows = conn.execute(
                "SELECT * FROM governed_training_aux_receipts "
                "WHERE run_revision_id=? ORDER BY effect_kind",
                (revision.revision_id,),
            ).fetchall()
        if row is None:
            raise RuntimeError("governed model projection or active head is unavailable")
        stored_blob = json.loads(str(row["model_blob_json"]))
        expected_admission_ids = revision.payload["dataset_manifest"]["admission_revision_ids"]
        parent = revision.payload["parent_model_ref"]
        run_before_hash = sha256_json(
            {
                "dimension": revision.payload["dimension"],
                "model_id": parent["model_id"],
                "model_hash": parent["model_hash"],
            }
        )
        run_after_hash = sha256_json(
            {
                "dimension": revision.payload["dimension"],
                "model_id": model["model_id"],
                "model_hash": model["blob_hash"],
                "run_revision_id": revision.revision_id,
            }
        )
        run_evidence_refs = [
            f"training-run:{revision.revision_id}",
            ("training-manifest:" + str(revision.payload["dataset_manifest"]["manifest_hash"])),
            f"training-projection-command:{command['command_id']}",
            f"governed-scorer-model:{model['model_id']}",
        ]
        run_receipt_identity = {
            "receipt_id": str(revision.payload["projection_receipt_ref"]["receipt_id"]),
            "command_id": str(command["command_id"]),
            "run_revision_id": revision.revision_id,
            "run_payload_hash": revision.payload_hash,
            "model_id": str(model["model_id"]),
            "status": "committed",
            "action_id": str(revision.payload["material_effect_refs"]["action_id"]),
            "effect_id": str(revision.payload["material_effect_refs"]["effect_id"]),
            "before_hash": run_before_hash,
            "after_hash": run_after_hash,
            "evidence_refs": run_evidence_refs,
        }
        expected_run_receipt_hash = sha256_json(run_receipt_identity)
        if (
            row["run_revision_id"] != revision.revision_id
            or row["run_payload_hash"] != revision.payload_hash
            or json.loads(str(row["admission_revision_ids_json"])) != expected_admission_ids
            or row["dimension"] != revision.payload["dimension"]
            or row["model_type"] != model["model_type"]
            or stored_blob != model["blob"]
            or row["model_blob_hash"] != model["blob_hash"]
            or sha256_json(stored_blob) != model["blob_hash"]
            or row["dataset_manifest_hash"] != revision.payload["dataset_manifest"]["manifest_hash"]
            or row["fit_input_hash"] != revision.payload["fit_input_hash"]
            or row["validation_report_hash"] != revision.payload["validation_report"]["report_hash"]
            or row["holdout_report_hash"] != revision.payload["holdout_report"]["report_hash"]
            or row["access_control_hash"] != cognitive_access_hash(access)
            or (row["parent_model_id"] or "") != revision.payload["parent_model_ref"]["model_id"]
            or row["head_dimension"] != revision.payload["dimension"]
            or row["head_model_id"] != model["model_id"]
            or row["head_run_revision_id"] != revision.revision_id
            or row["receipt_command_id"] != command["command_id"]
            or row["receipt_run_payload_hash"] != revision.payload_hash
            or row["receipt_model_id"] != model["model_id"]
            or row["receipt_status"] != "committed"
            or row["receipt_id"] != run_receipt_identity["receipt_id"]
            or row["receipt_action_id"] != run_receipt_identity["action_id"]
            or row["receipt_effect_id"] != run_receipt_identity["effect_id"]
            or row["receipt_before_hash"] != run_before_hash
            or row["receipt_after_hash"] != run_after_hash
            or json.loads(str(row["receipt_evidence_refs_json"])) != run_evidence_refs
            or row["receipt_hash"] != expected_run_receipt_hash
        ):
            raise RuntimeError("governed model projection proof mismatch")
        command_object = LocalConsumerCommand.create(
            revision_id=revision.revision_id,
            consumer_id=str(command["consumer_id"]),
            command_type=str(command["command_type"]),
            payload=command["payload"],
            created_at=str(command["created_at"]),
        )
        expected_effect_rows, expected_aux_receipt_rows, aux_reciprocal_refs = (
            self._aux_projection_rows(
                revision,
                command_object,
                run_before_hash=run_before_hash,
            )
        )
        if [tuple(item) for item in aux_effect_rows] != list(expected_effect_rows) or [
            tuple(item) for item in aux_receipt_rows
        ] != list(expected_aux_receipt_rows):
            raise RuntimeError("governed training auxiliary projection proof mismatch")
        state_evidence_refs = tuple(str(value) for value in state_receipt["evidence_refs"])
        expected_state_evidence_refs = (
            f"training-run:{revision.revision_id}",
            (
                "governed-training-run-receipt:"
                + str(run_receipt_identity["receipt_id"])
                + ":"
                + expected_run_receipt_hash
            ),
            f"governed-scorer-model:{model['model_id']}",
            *aux_reciprocal_refs,
        )
        if (
            state_receipt["command_id"] != command["command_id"]
            or state_receipt["revision_id"] != revision.revision_id
            or state_receipt["event_id"] != command["event_id"]
            or state_receipt["consumer_id"] != command["consumer_id"]
            or state_receipt["target_effect_id"]
            != revision.payload["material_effect_refs"]["effect_id"]
            or state_receipt["before_hash"] != run_before_hash
            or state_receipt["after_hash"] != run_after_hash
            or state_evidence_refs != expected_state_evidence_refs
        ):
            raise RuntimeError("governed model reciprocal state receipt mismatch")
        bayesian_prior = dict(revision.payload["bayesian_prior_artifact"])
        rule_optimizer = dict(revision.payload["rule_optimizer_artifact"])
        return GovernedModelSnapshot(
            dimension=str(revision.payload["dimension"]),
            model_id=str(model["model_id"]),
            run_revision_id=revision.revision_id,
            model_type=str(model["model_type"]),
            model_blob=stored_blob,
            model_blob_hash=str(model["blob_hash"]),
            bayesian_prior=bayesian_prior,
            bayesian_prior_hash=str(bayesian_prior["artifact_hash"]),
            rule_optimizer=rule_optimizer,
            rule_optimizer_hash=str(rule_optimizer["artifact_hash"]),
        )

    def build_ready_run(
        self,
        dimension: str,
        now: str | None = None,
    ) -> TrainingRunReceipt:
        """Seal one deterministic current-manifest run or an insufficient result."""

        self._assert_migration_clear()
        return self._build_ready_run(
            dimension,
            now=now,
            rebuild_of_revision_id="",
        )

    def _build_ready_run(
        self,
        dimension: str,
        *,
        now: str | None,
        rebuild_of_revision_id: str,
    ) -> TrainingRunReceipt:

        if str(dimension or "").strip() != TRAINING_DIMENSION:
            raise ValueError("unknown governed training dimension")
        admissions, admission_refs = self._verified_current_admissions()
        manifest = training_dataset_manifest(admission_refs)
        fit_input_hash = training_fit_input_hash(admission_refs)
        algorithm = {
            "name": "governed_binary_centroid",
            "version": TRAINING_ALGORITHM_VERSION,
            "code_hash": TRAINING_ALGORITHM_CODE_HASH,
            "spec_hash": TRAINING_ALGORITHM_SPEC_HASH,
            "config_hash": TRAINING_ALGORITHM_CONFIG_HASH,
            "readiness_policy_hash": READINESS_POLICY_HASH,
            "selection_input_hash": fit_input_hash,
        }
        matching_runs = [
            revision
            for revision in self.state.current_revisions(object_type="training_run_record")
            if revision.payload["dimension"] == TRAINING_DIMENSION
            and revision.payload["algorithm"] == algorithm
            and revision.payload["dataset_manifest"]["manifest_hash"] == manifest["manifest_hash"]
            and revision.payload["rebuild_of_revision_id"] == rebuild_of_revision_id
            and revision.payload["state"]
            in {"model_sealed", "insufficient_sample", "sealed", "applied"}
        ]
        if len(matching_runs) > 1:
            raise RuntimeError("multiple current governed runs match one manifest")
        if matching_runs:
            existing = matching_runs[0]
            if existing.payload["state"] == "model_sealed":
                return self._finalize_durable_model_seal(existing, admissions)
            return self._run_receipt_from_revision(existing)

        parent_model_ref = self._current_model_ref()
        run_suffix = sha256_json(
            {
                "dimension": TRAINING_DIMENSION,
                "algorithm": algorithm,
                "dataset_manifest_hash": manifest["manifest_hash"],
                "parent_model_ref": parent_model_ref,
                "rebuild_of_revision_id": rebuild_of_revision_id,
            }
        ).split(":", 1)[1][:32]
        run_id = "training-run-" + run_suffix
        ready = self._readiness_satisfied(admission_refs)
        current = self.state.current_revision("training_run_record", run_id)
        if current is not None:
            if current.payload["dataset_manifest"]["manifest_hash"] != manifest[
                "manifest_hash"
            ] or current.payload["state"] not in {
                "model_sealed",
                "insufficient_sample",
                "sealed",
                "applied",
            }:
                raise RuntimeError("current governed training run conflicts with manifest")
            if current.payload["state"] == "model_sealed":
                return self._finalize_durable_model_seal(current, admissions)
            return self._run_receipt_from_revision(current)

        created_at = str(now or self._clock())
        access = self._run_access_control(admissions)
        if ready:
            model_blob = self._fit_centroid(admissions)
            model_blob_hash = sha256_json(model_blob)
            model_id = (
                "governed-model-"
                + sha256_json(
                    {
                        "run_id": run_id,
                        "manifest_hash": manifest["manifest_hash"],
                        "model_blob_hash": model_blob_hash,
                    }
                ).split(":", 1)[1][:32]
            )
            model_artifact: dict[str, Any] = {
                "model_id": model_id,
                "model_type": "binary_feature_centroid",
                "blob": model_blob,
                "blob_hash": model_blob_hash,
                "serialization": "canonical_json_v1",
                "sealed_at": created_at,
            }
            train_examples = governed_training_examples(admissions)
            bayesian_prior_artifact = derive_bayesian_prior_artifact(
                run_id=run_id,
                examples=train_examples,
            )
            rule_optimizer_artifact = derive_rule_optimizer_artifact(
                run_id=run_id,
                examples=train_examples,
            )
            validation_report = self._evaluation_report(
                admissions,
                split="validation",
                model_blob=model_blob,
            )
            holdout_report = {
                "example_count": manifest["counts"]["holdout"],
                "report_hash": sha256_json(
                    {
                        "status": "pending_after_durable_model_seal",
                        "split": "holdout",
                        "count": manifest["counts"]["holdout"],
                        "model_blob_hash": model_blob_hash,
                    }
                ),
                "evaluated_after_model_sealed_at": "",
            }
            state = "model_sealed"
        else:
            model_id = ""
            model_artifact = {
                "model_id": "",
                "model_type": "",
                "blob": {},
                "blob_hash": "",
                "serialization": "",
                "sealed_at": "",
            }
            validation_report = {
                "example_count": manifest["counts"]["validation"],
                "report_hash": sha256_json(
                    {
                        "status": "insufficient_sample",
                        "split": "validation",
                        "count": manifest["counts"]["validation"],
                    }
                ),
            }
            holdout_report = {
                "example_count": manifest["counts"]["holdout"],
                "report_hash": sha256_json(
                    {
                        "status": "insufficient_sample",
                        "split": "holdout",
                        "count": manifest["counts"]["holdout"],
                    }
                ),
                "evaluated_after_model_sealed_at": "",
            }
            bayesian_prior_artifact = {}
            rule_optimizer_artifact = {}
            state = "insufficient_sample"

        payload = self._run_payload(
            run_id=run_id,
            state=state,
            created_at=created_at,
            algorithm=algorithm,
            admission_refs=admission_refs,
            manifest=manifest,
            fit_input_hash=fit_input_hash,
            validation_report=validation_report,
            holdout_report=holdout_report,
            parent_model_ref=parent_model_ref,
            model_artifact=model_artifact,
            bayesian_prior_artifact=bayesian_prior_artifact,
            rule_optimizer_artifact=rule_optimizer_artifact,
            access_control=access,
            supersedes_revision_id="",
            rebuild_of_revision_id=rebuild_of_revision_id,
        )
        revision, command, event = self._run_revision_command_event(
            payload,
            source_revision_id=str(manifest["manifest_hash"]),
            source_content_hash=str(manifest["manifest_hash"]),
            created_at=created_at,
        )
        self.state.unit_of_work().commit(
            revisions=(revision,),
            event=event,
            commands=(command,),
        )
        self._apply_run_projection(command)
        if state == "model_sealed":
            return self._finalize_durable_model_seal(revision, admissions)
        return TrainingRunReceipt(
            status=state,
            run_id=run_id,
            run_revision_id=revision.revision_id,
            model_id=model_id,
            projection_command_id=command.command_id,
            projection_receipt_id=str(payload["projection_receipt_ref"]["receipt_id"]),
        )

    def _finalize_durable_model_seal(
        self,
        model_seal: CognitiveStateRevision,
        admissions: tuple[CognitiveStateRevision, ...],
    ) -> TrainingRunReceipt:
        current = self.state.current_revision("training_run_record", model_seal.object_id)
        if current != model_seal or model_seal.payload["state"] != "model_sealed":
            if current is not None and current.payload["state"] in {"sealed", "applied"}:
                return self._run_receipt_from_revision(current)
            raise RuntimeError("durable model seal is not current")
        self._run_receipt_from_revision(model_seal)
        evaluated_at = self._clock()
        sealed_at = str(model_seal.payload["model_artifact"]["sealed_at"])
        if datetime.fromisoformat(evaluated_at) < datetime.fromisoformat(sealed_at):
            raise RuntimeError("training evaluation clock precedes durable model seal")
        holdout_report = self._evaluation_report(
            admissions,
            split="holdout",
            model_blob=model_seal.payload["model_artifact"]["blob"],
            evaluated_after_model_sealed_at=evaluated_at,
        )
        payload = json.loads(canonical_json(model_seal.payload))
        payload["state"] = "sealed"
        payload["holdout_report"] = holdout_report
        payload["supersedes_revision_id"] = model_seal.revision_id
        suffix = model_seal.object_id.removeprefix("training-run-")
        payload["material_effect_refs"] = {
            "action_id": "training-run-sealed-action-" + suffix,
            "effect_id": "training-run-sealed-effect-" + suffix,
        }
        payload["projection_receipt_ref"] = {
            "receipt_id": "governed-training-run-sealed-receipt-" + suffix,
            "receipt_hash": sha256_json({"run_id": model_seal.object_id, "state": "sealed"}),
        }
        payload["run_input_hash"] = training_run_input_hash(payload)
        revision, command, event = self._run_revision_command_event(
            payload,
            source_revision_id=model_seal.revision_id,
            source_content_hash=model_seal.payload_hash,
            created_at=evaluated_at,
            supersedes_revision_id=model_seal.revision_id,
        )
        self.state.unit_of_work().commit(
            revisions=(revision,),
            event=event,
            commands=(command,),
            expected_heads=(
                CognitiveHeadPrecondition.create(
                    object_type=model_seal.object_type,
                    object_id=model_seal.object_id,
                    revision_id=model_seal.revision_id,
                ),
            ),
        )
        self._apply_run_projection(command)
        return TrainingRunReceipt(
            status="sealed",
            run_id=revision.object_id,
            run_revision_id=revision.revision_id,
            model_id=str(revision.payload["model_artifact"]["model_id"]),
            projection_command_id=command.command_id,
            projection_receipt_id=str(revision.payload["projection_receipt_ref"]["receipt_id"]),
        )

    def _validate_durable_model_seal_lineage(
        self,
        revision: CognitiveStateRevision,
    ) -> CognitiveStateRevision:
        if revision.payload["state"] == "applied":
            sealed = self.state.revision(revision.supersedes_revision_id)
            if (
                sealed is None
                or sealed.object_type != "training_run_record"
                or sealed.object_id != revision.object_id
                or sealed.payload["state"] != "sealed"
                or revision.source_revision_id != sealed.revision_id
                or revision.source_content_hash != sealed.payload_hash
            ):
                raise RuntimeError("applied run lacks its exact evaluated seal")
        elif revision.payload["state"] == "sealed":
            sealed = revision
        else:
            raise ValueError("durable model seal lineage requires an evaluated run")
        model_seal = self.state.revision(sealed.supersedes_revision_id)
        if (
            model_seal is None
            or model_seal.object_type != "training_run_record"
            or model_seal.object_id != sealed.object_id
            or model_seal.payload["state"] != "model_sealed"
            or sealed.source_revision_id != model_seal.revision_id
            or sealed.source_content_hash != model_seal.payload_hash
        ):
            raise RuntimeError("evaluated run lacks its exact durable model seal")
        stable_fields = (
            "access_control",
            "run_id",
            "dimension",
            "algorithm",
            "admission_refs",
            "dataset_manifest",
            "fit_input_hash",
            "validation_report",
            "parent_model_ref",
            "model_artifact",
            "bayesian_prior_artifact",
            "rule_optimizer_artifact",
            "rebuild_of_revision_id",
        )
        lineage_pairs = [(sealed, model_seal)]
        if revision.payload["state"] == "applied":
            lineage_pairs.append((revision, sealed))
        for successor, predecessor in lineage_pairs:
            if any(
                successor.payload[field] != predecessor.payload[field] for field in stable_fields
            ):
                raise RuntimeError("governed run changed after durable model sealing")
        if (
            model_seal.payload["holdout_report"]["evaluated_after_model_sealed_at"]
            or not sealed.payload["holdout_report"]["evaluated_after_model_sealed_at"]
        ):
            raise RuntimeError("governed holdout seal lineage is invalid")
        self._run_receipt_from_revision(model_seal)
        return model_seal

    def rebuild_stale_dimension(self, dimension: str) -> TrainingRunReceipt:
        """Rebuild a stale dimension from the complete current admission manifest."""

        self._assert_migration_clear()
        if str(dimension or "").strip() != TRAINING_DIMENSION:
            raise ValueError("unknown governed training dimension")
        stale = sorted(
            (
                revision
                for revision in self.state.current_revisions(object_type="training_run_record")
                if revision.payload["dimension"] == TRAINING_DIMENSION
                and revision.payload["state"] == "stale"
            ),
            key=lambda item: (item.created_at, item.revision_id),
        )
        if not stale:
            raise ValueError("governed training dimension has no stale run")
        sealed = self._build_ready_run(
            dimension,
            now=self._clock(),
            rebuild_of_revision_id=stale[-1].revision_id,
        )
        if sealed.status in {"insufficient_sample", "applied"}:
            return sealed
        return self.apply_run(sealed.run_revision_id)

    def apply_run(self, run_revision_id: str) -> TrainingRunReceipt:
        """Promote a sealed fixed-algorithm run without consulting holdout results."""

        self._assert_migration_clear()
        sealed = self.state.revision(str(run_revision_id or ""))
        if sealed is None or sealed.object_type != "training_run_record":
            raise ValueError("governed training run revision is unavailable")
        current = self.state.current_revision("training_run_record", sealed.object_id)
        if current is not None and current.payload["state"] == "applied":
            if current.supersedes_revision_id != sealed.revision_id:
                raise RuntimeError("applied governed training run lineage mismatch")
            return self._run_receipt_from_revision(current)
        if current != sealed or sealed.payload["state"] != "sealed":
            raise ValueError("only the current sealed training run can be applied")
        validate_cognitive_state_payload("training_run_record", sealed.payload)
        self._validate_durable_model_seal_lineage(sealed)
        payload = json.loads(canonical_json(sealed.payload))
        payload["state"] = "applied"
        payload["supersedes_revision_id"] = sealed.revision_id
        suffix = sealed.object_id.removeprefix("training-run-")
        payload["material_effect_refs"] = {
            "action_id": "training-run-apply-action-" + suffix,
            "effect_id": "training-run-apply-effect-" + suffix,
        }
        payload["projection_receipt_ref"] = {
            "receipt_id": "governed-training-run-applied-receipt-" + suffix,
            "receipt_hash": sha256_json({"run_id": sealed.object_id, "state": "applied"}),
        }
        payload["run_input_hash"] = training_run_input_hash(payload)
        revision, command, event = self._run_revision_command_event(
            payload,
            source_revision_id=sealed.revision_id,
            source_content_hash=sealed.payload_hash,
            created_at=self._clock(),
            supersedes_revision_id=sealed.revision_id,
        )
        self.state.unit_of_work().commit(
            revisions=(revision,),
            event=event,
            commands=(command,),
        )
        self._apply_run_projection(command)
        return TrainingRunReceipt(
            status="applied",
            run_id=revision.object_id,
            run_revision_id=revision.revision_id,
            model_id=str(revision.payload["model_artifact"]["model_id"]),
            projection_command_id=command.command_id,
            projection_receipt_id=str(revision.payload["projection_receipt_ref"]["receipt_id"]),
        )
