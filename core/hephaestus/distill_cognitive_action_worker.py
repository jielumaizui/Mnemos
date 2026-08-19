"""Lease/retry worker for real distill cognitive-action effects."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Mapping

from core.cognitive.decision_trace import (
    DecisionCandidateEvaluation,
    DecisionRejectionEvaluation,
    MaterialActionRequest,
    ProjectContractDecisionContext,
    ProjectContractDecisionEvaluation,
    ProjectContractMaterialActionResolver,
    material_action_resolution_scope,
)
from core.hephaestus.cognitive_action_targets import (
    CognitiveActionTargetDispatcher,
    CognitiveActionTargetError,
)
from core.hephaestus.distill_action_store import (
    ARTIFACT_SCHEMA_VERSION,
    DistillActionStateError,
    DistillActionStore,
    canonical_json,
    sha256_json,
)
from core.privacy.content_redaction import REDACTION_POLICY


DISTILL_ACTION_DECISION_CONTRACT_ID = (
    "project-contract:distill-cognitive-action-dispatch"
)
DISTILL_ACTION_DECISION_CONTRACT_REVISION = (
    "mnemos.distill_cognitive_action_dispatch.v1"
)
DISTILL_ACTION_DECISION_CONTRACT_TEXT = (
    "A validated authority admitted distillation action may invoke only its "
    "exact canonical target under a pre action DecisionTrace permit."
)
DISTILL_ACTION_WORKER_CODE_HASH = sha256_json(
    {
        "module": "core.hephaestus.distill_cognitive_action_worker",
        "producer": "DistillCognitiveActionWorker",
        "version": DISTILL_ACTION_DECISION_CONTRACT_REVISION,
    }
)


class CognitiveActionArtifactError(ValueError):
    """The durable command artifact is invalid and must never be applied."""


class DistillCognitiveActionWorker:
    """Consume commands only after a target service returns a real effect."""

    def __init__(
        self,
        db_path: Path,
        *,
        database_dir: Path,
        wiki_dir: Path | None = None,
        dispatcher: CognitiveActionTargetDispatcher | None = None,
        worker_id: str = "",
        lease_seconds: int = 60,
        max_attempts: int = 3,
    ):
        self.db_path = Path(db_path)
        self.database_dir = Path(database_dir)
        self.store = DistillActionStore(self.db_path)
        self.planner = CognitiveActionTargetDispatcher(
            database_dir=self.database_dir,
            wiki_dir=wiki_dir,
        )
        self.dispatcher = dispatcher or self.planner
        self.worker_id = worker_id or f"cognitive-worker-{uuid.uuid4().hex}"
        self.lease_seconds = max(1, int(lease_seconds))
        self.max_attempts = max(1, int(max_attempts))

    def process_queued(
        self,
        *,
        limit: int = 100,
        action_id: str = "",
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "processed": 0,
            "applied": 0,
            "retry": 0,
            "dead": 0,
            "items": [],
        }
        for _ in range(max(0, int(limit))):
            row = self.store.claim_next(
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
                action_id=action_id,
            )
            if row is None:
                break
            item = self._process_row(row)
            result["processed"] += 1
            result[item["status"]] += 1
            result["items"].append(item)
            if action_id:
                break
        return result

    def _process_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        action_id = str(row["cognitive_action_id"])
        try:
            artifact = self._validated_artifact(row)
            target_plan = self.planner.prepare(row, artifact)
            allowed_material_actions = tuple(
                _material_action_fact(request)
                for request in self.planner.material_action_requests(
                    row,
                    target_plan,
                )
            )
            evidence_refs = tuple(
                dict.fromkeys(
                    str(value)
                    for value in (
                        artifact.get("evidence_refs")
                        or artifact.get("source_event_ids")
                        or ()
                    )
                    if str(value)
                )
            )
            source_facts = {
                "schema_version": "mnemos.distill_action_evaluation_facts.v1",
                "cognitive_action_id": action_id,
                "cognitive_action": str(row["cognitive_action"]),
                "artifact_hash": str(row["artifact_hash"]),
                "artifact_schema_version": str(artifact["schema_version"]),
                "mapping_quality": str(artifact["mapping_quality"]),
                "source_event_ids": list(artifact["source_event_ids"]),
                "evidence_refs": list(evidence_refs),
                "allowed_material_actions": [
                    dict(value) for value in allowed_material_actions
                ],
            }
            source_facts_hash = sha256_json(source_facts)

            def evaluate_request(
                request: MaterialActionRequest,
            ) -> ProjectContractDecisionEvaluation:
                """Admit only material actions declared by the distilled artifact."""

                request_hash = sha256_json(
                    {
                        "owner": request.owner,
                        "executor_id": request.executor_id,
                        "action_type": request.action_type,
                        "target_ref": request.target_ref,
                        "input_hash": request.input_hash,
                    }
                )
                request_ref = f"request-binding:{request_hash}"
                facts_ref = f"source-facts:{source_facts_hash}"
                approved = _material_action_fact(request) in allowed_material_actions
                approved_key = "dispatch_authority_admitted_target"
                rejected_key = "quarantine_unbound_distill_action"
                common_refs = (
                    request_ref,
                    facts_ref,
                    *evidence_refs,
                )
                selection_key = approved_key if approved else rejected_key
                return ProjectContractDecisionEvaluation(
                    request_binding_hash=request_hash,
                    source_facts_hash=source_facts_hash,
                    candidates=(
                        DecisionCandidateEvaluation(
                            key=approved_key,
                            summary=(
                                "Dispatch the authority admitted artifact to its "
                                "event bound target family."
                            ),
                            supporting_evidence=common_refs if approved else (),
                            opposing_evidence=() if approved else common_refs,
                            satisfies_value_keys=("safety", "project_contract"),
                        ),
                        DecisionCandidateEvaluation(
                            key=rejected_key,
                            summary=(
                                "Quarantine a request that is outside the admitted "
                                "artifact and target family."
                            ),
                            supporting_evidence=common_refs if not approved else (),
                            opposing_evidence=() if not approved else common_refs,
                            satisfies_value_keys=("safety",),
                        ),
                    ),
                    selection_key=selection_key,
                    rejections=(
                        DecisionRejectionEvaluation(
                            candidate_key=(
                                rejected_key if approved else approved_key
                            ),
                            reason_code=(
                                "artifact_target_binding_verified"
                                if approved
                                else "artifact_target_binding_rejected"
                            ),
                            evidence_refs=common_refs,
                        ),
                    ),
                    expected_outcomes=(
                        {
                            "metric": (
                                "target_receipt_committed"
                                if approved
                                else "target_effect_count"
                            ),
                            "operator": "equals",
                            "value": 1 if approved else 0,
                        },
                    ),
                    approval_decision="approved" if approved else "rejected",
                    approval_evidence_ref=facts_ref,
                )

            resolver = ProjectContractMaterialActionResolver(
                ProjectContractDecisionContext(
                    state_db_path=self.database_dir / "producer_consumer_ledger.db",
                    contract_id=DISTILL_ACTION_DECISION_CONTRACT_ID,
                    contract_revision_id=DISTILL_ACTION_DECISION_CONTRACT_REVISION,
                    contract_text=DISTILL_ACTION_DECISION_CONTRACT_TEXT,
                    contract_evidence_ref=(
                        f"{DISTILL_ACTION_DECISION_CONTRACT_ID}"
                        f"#{DISTILL_ACTION_DECISION_CONTRACT_REVISION}"
                    ),
                    source_id=f"distill-cognitive-action:{action_id}",
                    source_revision_id=f"artifact:{row['artifact_hash']}",
                    source_content_hash=str(row["artifact_hash"]),
                    source_uri=f"distill-action://{action_id}",
                    evidence_refs=evidence_refs,
                    task=f"Dispatch cognitive action {row['cognitive_action']}",
                    goal=(
                        "Apply the validated cognitive action to its sole canonical "
                        "target with an independently readable reciprocal receipt."
                    ),
                    constraints=(
                        "The artifact identity and admitted evidence must remain exact.",
                        "The target service must sign the resulting effect.",
                    ),
                    created_at=str(artifact["created_at"]),
                    scope_prefix=f"distill-action:{action_id}",
                    producer="distill-cognitive-action-worker",
                    producer_version=DISTILL_ACTION_DECISION_CONTRACT_REVISION,
                    producer_code_hash=DISTILL_ACTION_WORKER_CODE_HASH,
                    evaluator_id="distill-authority-admission-evaluator",
                    evaluator=evaluate_request,
                )
            )
            with material_action_resolution_scope(resolver):
                if self.dispatcher is self.planner:
                    effect = self.planner.apply_prepared(
                        row,
                        artifact,
                        target_plan,
                    )
                else:
                    effect = self.dispatcher.apply(row, artifact)
            self.store.complete_effect(
                row=row,
                worker_id=self.worker_id,
                effect=effect,
            )
            return {
                "cognitive_action_id": action_id,
                "cognitive_action": str(row["cognitive_action"]),
                "status": "applied",
                "effect_id": effect.effect_id,
                "error": "",
            }
        except CognitiveActionArtifactError as exc:
            status = self.store.fail_attempt(
                row=row,
                worker_id=self.worker_id,
                error=str(exc),
                retryable=False,
                max_attempts=self.max_attempts,
            )
            return self._failed_item(row, status, str(exc))
        except CognitiveActionTargetError as exc:
            status = self.store.fail_attempt(
                row=row,
                worker_id=self.worker_id,
                error=str(exc),
                retryable=exc.retryable,
                max_attempts=self.max_attempts,
            )
            return self._failed_item(row, status, str(exc))
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RuntimeError) as exc:
            status = self.store.fail_attempt(
                row=row,
                worker_id=self.worker_id,
                error=f"{type(exc).__name__}: {exc}",
                retryable=not isinstance(exc, DistillActionStateError),
                max_attempts=self.max_attempts,
            )
            return self._failed_item(row, status, f"{type(exc).__name__}: {exc}")

    def _validated_artifact(self, row: Mapping[str, Any]) -> dict[str, Any]:
        try:
            artifact = json.loads(str(row.get("artifact_payload") or ""))
        except json.JSONDecodeError as exc:
            raise CognitiveActionArtifactError("artifact payload is not valid JSON") from exc
        if not isinstance(artifact, dict):
            raise CognitiveActionArtifactError("artifact payload must be an object")
        if sha256_json(artifact) != str(row.get("artifact_hash") or ""):
            raise CognitiveActionArtifactError("artifact hash mismatch")
        if artifact.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
            raise CognitiveActionArtifactError("unsupported artifact schema")
        bindings = {
            "cognitive_action_id": row.get("cognitive_action_id"),
            "distill_action_id": row.get("distill_action_id"),
            "session_id": row.get("session_id"),
            "claim_id": row.get("claim_id"),
            "cognitive_action": row.get("cognitive_action"),
            "episode_id": row.get("episode_id"),
            "input_spec_hash": row.get("input_spec_hash"),
            "extraction_output_hash": row.get("extraction_output_hash"),
        }
        drift = [
            key
            for key, expected in bindings.items()
            if str(artifact.get(key) or "") != str(expected or "")
        ]
        if drift:
            raise CognitiveActionArtifactError(
                "artifact identity drift: " + ", ".join(sorted(drift))
            )
        source_event_ids = artifact.get("source_event_ids")
        if not isinstance(source_event_ids, list) or not source_event_ids:
            raise CognitiveActionArtifactError("artifact source_event_ids are missing")
        if len(source_event_ids) != len(set(str(value) for value in source_event_ids)):
            raise CognitiveActionArtifactError("artifact source_event_ids are duplicated")
        claim = artifact.get("claim")
        if not isinstance(claim, Mapping):
            raise CognitiveActionArtifactError("artifact claim is missing")
        if str(claim.get("claim_id") or "") != str(row.get("claim_id") or ""):
            raise CognitiveActionArtifactError("artifact claim binding mismatch")
        for evidence in claim.get("evidence") or []:
            if not isinstance(evidence, Mapping):
                raise CognitiveActionArtifactError("artifact claim evidence is invalid")
            if str(evidence.get("source_event_id") or "") not in {
                str(value) for value in source_event_ids
            }:
                raise CognitiveActionArtifactError(
                    "artifact evidence is outside the admitted source set"
                )
        fragment_ids = artifact.get("fragment_ids")
        try:
            stored_fragment_ids = json.loads(str(row.get("fragment_ids") or "[]"))
        except json.JSONDecodeError as exc:
            raise CognitiveActionArtifactError("stored fragment mapping is invalid") from exc
        if (
            not isinstance(fragment_ids, list)
            or not fragment_ids
            or fragment_ids != stored_fragment_ids
            or len(fragment_ids) != len(set(str(value) for value in fragment_ids))
        ):
            raise CognitiveActionArtifactError("artifact fragment mapping mismatch")
        refs = artifact.get("fragment_refs")
        if not isinstance(refs, list) or {
            str(ref.get("fragment_id") or "")
            for ref in refs
            if isinstance(ref, Mapping)
        } != set(str(value) for value in fragment_ids):
            raise CognitiveActionArtifactError("artifact fragment references are incomplete")
        claim_id = str(row.get("claim_id") or "")
        if any(
            claim_id not in [str(value) for value in ref.get("claim_ids") or []]
            for ref in refs
            if isinstance(ref, Mapping)
        ):
            raise CognitiveActionArtifactError("artifact fragment does not bind the claim")
        mapping_quality = str(artifact.get("mapping_quality") or "")
        if mapping_quality == "exact":
            if any(
                str(ref.get("content_hash") or "").startswith("sha256:") is False
                for ref in refs
                if isinstance(ref, Mapping)
            ):
                raise CognitiveActionArtifactError("exact fragment content hash is missing")
        elif mapping_quality == "legacy_artifact_projection":
            legacy = artifact.get("legacy_reconciliation")
            if not isinstance(legacy, Mapping):
                raise CognitiveActionArtifactError("legacy reconciliation proof is missing")
            if legacy.get("source_schema") != "mnemos.distill_cognitive_action.v1":
                raise CognitiveActionArtifactError("legacy source schema is unsupported")
            if str(legacy.get("source_artifact_hash") or "") != str(
                artifact.get("extraction_output_hash") or ""
            ).removeprefix("legacy-artifact:"):
                raise CognitiveActionArtifactError("legacy source artifact hash mismatch")
            if not str(artifact.get("input_spec_hash") or "").startswith("legacy-input:"):
                raise CognitiveActionArtifactError("legacy input identity is not explicit")
            if not str(artifact.get("extraction_output_hash") or "").startswith(
                "legacy-artifact:"
            ):
                raise CognitiveActionArtifactError("legacy artifact identity is not explicit")
            if any(
                ref.get("source_kind") != "legacy_v1_artifact_projection"
                for ref in refs
                if isinstance(ref, Mapping)
            ):
                raise CognitiveActionArtifactError("legacy fragment projection is unlabeled")
        else:
            raise CognitiveActionArtifactError("artifact mapping quality is unsupported")
        acl = artifact.get("acl")
        expected_acl = {
            "visibility": "private",
            "owner": "local_user",
            "redaction_policy": REDACTION_POLICY,
            "encryption": "none",
        }
        if acl != expected_acl:
            raise CognitiveActionArtifactError("artifact ACL/redaction contract mismatch")
        if canonical_json(acl) != str(row.get("acl_payload") or ""):
            raise CognitiveActionArtifactError("stored artifact ACL mismatch")
        parent = self.store.get_action(str(row["distill_action_id"]))
        if parent is None or parent.get("result_status") != "applied":
            raise CognitiveActionArtifactError("parent action is not committed")

        artifact_path = Path(str(row.get("artifact_path") or ""))
        if artifact_path.is_file():
            try:
                materialized = json.loads(artifact_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CognitiveActionArtifactError(
                    "materialized artifact is unreadable"
                ) from exc
            if not isinstance(materialized, Mapping) or sha256_json(materialized) != str(
                row["artifact_hash"]
            ):
                raise CognitiveActionArtifactError(
                    "materialized artifact does not match the durable command"
                )
        return artifact

    @staticmethod
    def _failed_item(
        row: Mapping[str, Any],
        status: str,
        error: str,
    ) -> dict[str, Any]:
        return {
            "cognitive_action_id": str(row["cognitive_action_id"]),
            "cognitive_action": str(row["cognitive_action"]),
            "status": status,
            "effect_id": "",
            "error": error,
        }


def _material_action_fact(request: MaterialActionRequest) -> dict[str, str]:
    expected_state_db = str(request.expected_state_db or "")
    if expected_state_db:
        expected_state_db = str(
            Path(expected_state_db).expanduser().resolve(strict=False)
        )
    return {
        "owner": request.owner,
        "executor_id": request.executor_id,
        "action_type": request.action_type,
        "target_ref": request.target_ref,
        "input_hash": request.input_hash,
        "expected_state_db": expected_state_db,
    }
