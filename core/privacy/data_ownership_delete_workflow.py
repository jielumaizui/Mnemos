"""Confirmed deletion orchestration across all declared data owners."""

from __future__ import annotations

import sqlite3
import uuid
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from core.cognitive.decision_trace import (
        MaterialActionAuthorization,
        MaterialActionRequest,
    )

from core.privacy.data_ownership_contracts import (
    DATA_DOMAINS,
    DataDeleteRequest,
    DataSubjectRef,
    DeletionProof,
    _database_dir,
    _deletion_operation_id,
    _hash_text,
    _now_iso,
    parse_scope,
)


class DataOwnershipDeletionWorkflowMixin:
    """Sequence freeze/snapshot checks, owner adapters, and aggregate proof."""

    if TYPE_CHECKING:
        config: Any

        def _record_request(
            self,
            request_type: str,
            subject: DataSubjectRef,
            payload: Mapping[str, Any],
        ) -> str: ...

        def _has_freeze(self, subject: DataSubjectRef) -> bool: ...

        def _plan_cognitive_state_tombstone(
            self,
            *,
            request_id: str,
            subject: DataSubjectRef,
            snapshot_ref: str,
        ) -> dict[str, Any]: ...

        def _apply_governed_training_tombstone(
            self,
            *,
            request_id: str,
        ) -> dict[str, Any]: ...

        def _apply_observation_subject_deletion(
            self,
            *,
            request_id: str,
            subject: DataSubjectRef,
            cognitive_state: Mapping[str, Any],
        ) -> dict[str, Any]: ...

        def _apply_embedding_cache_subject_deletion(
            self,
            *,
            request_id: str,
            subject: DataSubjectRef,
        ) -> dict[str, Any]: ...

        def _apply_raw_subject_deletion(
            self,
            *,
            request_id: str,
            subject: DataSubjectRef,
        ) -> dict[str, Any]: ...

        @staticmethod
        def _consumer_access_log_result(
            raw_deletion: Mapping[str, Any],
        ) -> dict[str, Any]: ...

        def _apply_agent_source_metadata_subject_deletion(
            self,
            *,
            request_id: str,
            subject: DataSubjectRef,
        ) -> dict[str, Any]: ...

        def _apply_persona_subject_deletion(
            self,
            *,
            request_id: str,
            subject: DataSubjectRef,
        ) -> dict[str, Any]: ...

        def _apply_reflection_subject_deletion(
            self,
            *,
            request_id: str,
            subject: DataSubjectRef,
        ) -> dict[str, Any]: ...

        def _apply_cognitive_graph_subject_deletion(
            self,
            *,
            request_id: str,
            subject: DataSubjectRef,
        ) -> dict[str, Any]: ...

        def _apply_wiki_subject_deletion(
            self,
            *,
            request_id: str,
            subject: DataSubjectRef,
            material_action_resolver: (
                Callable[
                    [MaterialActionRequest, Mapping[str, Any]],
                    MaterialActionAuthorization,
                ]
                | None
            ) = None,
        ) -> dict[str, Any]: ...

        def _wiki_delete_material_action_resolver(
            self,
            *,
            request_id: str,
            subject: DataSubjectRef,
            snapshot_verification: Mapping[str, Any],
        ) -> Callable[
            [MaterialActionRequest, Mapping[str, Any]],
            MaterialActionAuthorization,
        ]: ...

        def _apply_evidence_ref_deletion(
            self,
            *,
            subject: DataSubjectRef,
            wiki_deletion: Mapping[str, Any],
        ) -> dict[str, Any]: ...

        def _apply_event_metadata_subject_deletion(
            self,
            *,
            request_id: str,
            subject: DataSubjectRef,
        ) -> dict[str, Any]: ...

        def _apply_action_ledger_subject_deletion(
            self,
            *,
            request_id: str,
            subject: DataSubjectRef,
        ) -> dict[str, Any]: ...

        def _apply_scoring_subject_deletion(
            self,
            *,
            request_id: str,
            subject: DataSubjectRef,
        ) -> dict[str, Any]: ...

    def delete(
        self,
        scope: str,
        *,
        dry_run: bool = True,
        apply: bool = False,
        confirm: bool = False,
        snapshot_ref: str = "",
    ) -> DataDeleteRequest | DeletionProof:
        kind, value = parse_scope(scope)
        subject = DataSubjectRef(kind, value)
        impacts = (
            "raw rows and raw projection files",
            "wiki pages with matching source refs",
            "persona and reflection derived records",
            "search indexes, KG relations, scorecard aggregates",
            "prompt summaries and consumer access logs",
        )
        if dry_run or not apply:
            request = DataDeleteRequest(
                request_id=f"delete-{uuid.uuid4().hex[:12]}",
                subject=subject,
                status="dry_run_planned",
                created_at=_now_iso(),
                dry_run=True,
                affected_domains=tuple(sorted(DATA_DOMAINS)),
                derived_impacts=impacts,
                requires_freeze=True,
                requires_snapshot=True,
                requires_confirmation=True,
                action_ledger_ref=str(_database_dir(self.config) / "action_ledger.db"),
            )
            self._record_request("delete", subject, request.as_dict())
            return request
        if not confirm:
            raise PermissionError("delete apply requires confirmation")
        if not self._has_freeze(subject):
            raise PermissionError("delete apply requires a prior freeze request")
        if not snapshot_ref:
            raise PermissionError("delete apply requires snapshot_ref")
        from core.backup.snapshot_manager import MnemosSnapshotManager

        snapshot_verification = MnemosSnapshotManager(self.config).verify_data_delete_snapshot(
            snapshot_ref,
            scope_kind=subject.scope_kind,
            scope_value=subject.scope_value,
        )
        if not snapshot_verification["valid"]:
            errors = ",".join(snapshot_verification["errors"])
            raise PermissionError(f"delete apply requires a valid retained snapshot: {errors}")
        from core.ops.runtime_flow_telemetry import (
            record_runtime_produced,
            runtime_item_id,
        )

        deletion_flow_item_id = runtime_item_id(
            "data-delete", subject.scope_kind, _hash_text(subject.scope_value), snapshot_ref
        )
        record_runtime_produced(
            "data_inventory_to_delete_proof",
            source="core/privacy/data_ownership.py",
            item_id=deletion_flow_item_id,
            intended_consumers=["core/privacy/data_ownership.py:DeletionProof"],
            metadata={"transition": "delete_preconditions_verified"},
            config_or_path=_database_dir(self.config),
        )
        deletion_request_id = _deletion_operation_id(subject, snapshot_ref)
        cognitive_state = self._plan_cognitive_state_tombstone(
            request_id=f"cog-{deletion_request_id.removeprefix('delete-')}",
            subject=subject,
            snapshot_ref=snapshot_ref,
        )
        if cognitive_state["status"] == "blocked":
            proof = DeletionProof(
                proof_id=f"proof-{uuid.uuid4().hex[:12]}",
                subject_hash=_hash_text(f"{subject.scope_kind}:{subject.scope_value}"),
                status="blocked",
                deleted_at=_now_iso(),
                affected_domains=("cognitive_state",),
                affected_consumers=tuple(
                    cognitive_state.get("required_consumers") or ("cognitive_state_store",)
                ),
                verification_results={
                    "snapshot": snapshot_verification,
                    "mode": "blocked_before_physical_deletion",
                    "cognitive_state": cognitive_state,
                },
            )
            self._record_request("delete_proof", subject, proof.as_dict())
            return proof
        try:
            self._apply_governed_training_tombstone(
                request_id=deletion_request_id,
            )
        except (OSError, PermissionError, sqlite3.Error, RuntimeError, ValueError):
            proof = DeletionProof(
                proof_id=f"proof-{uuid.uuid4().hex[:12]}",
                subject_hash=_hash_text(f"{subject.scope_kind}:{subject.scope_value}"),
                status="blocked",
                deleted_at=_now_iso(),
                affected_domains=("cognitive_state", "scoring"),
                affected_consumers=("governed_training_projection",),
                verification_results={
                    "snapshot": snapshot_verification,
                    "mode": "blocked_at_governed_training_tombstone",
                    "cognitive_state": cognitive_state,
                    "scoring": {
                        "status": "blocked",
                        "verified": False,
                        "error": "governed_training_tombstone_failed",
                    },
                },
            )
            self._record_request("delete_proof", subject, proof.as_dict())
            return proof
        try:
            if cognitive_state.get("status") == "pending_consumer_receipts":
                from core.cognitive.state_store import CognitiveStateStore
                from core.cognitive.tombstone_consumer_coordinator import (
                    apply_receipt_only_cognitive_tombstones,
                )

                cognitive_request_id = "cog-" + deletion_request_id.removeprefix("delete-")
                receipt_only_checkpoint = apply_receipt_only_cognitive_tombstones(
                    CognitiveStateStore(self.config),
                    request_id=cognitive_request_id,
                )
                if (
                    receipt_only_checkpoint["verified"] is not True
                    and not receipt_only_checkpoint["unsupported_consumers"]
                ):
                    raise RuntimeError("cognitive tombstone consumer receipts remain incomplete")
        except (
            OSError,
            PermissionError,
            sqlite3.Error,
            RuntimeError,
            ValueError,
        ):
            proof = DeletionProof(
                proof_id=f"proof-{uuid.uuid4().hex[:12]}",
                subject_hash=_hash_text(f"{subject.scope_kind}:{subject.scope_value}"),
                status="blocked",
                deleted_at=_now_iso(),
                affected_domains=("cognitive_state",),
                affected_consumers=tuple(
                    consumer
                    for consumer in cognitive_state.get(
                        "required_consumers",
                        (),
                    )
                    if consumer != "governed_training_projection"
                ),
                verification_results={
                    "snapshot": snapshot_verification,
                    "mode": "blocked_at_cognitive_tombstone_consumers",
                    "cognitive_state": cognitive_state,
                    "receipt_only_consumers": {
                        "status": "blocked",
                        "verified": False,
                        "error": "cognitive_tombstone_consumer_failed",
                    },
                },
            )
            self._record_request("delete_proof", subject, proof.as_dict())
            return proof
        cognitive_state = self._plan_cognitive_state_tombstone(
            request_id=f"cog-{deletion_request_id.removeprefix('delete-')}",
            subject=subject,
            snapshot_ref=snapshot_ref,
        )
        observation_deletion = self._apply_observation_subject_deletion(
            request_id=deletion_request_id,
            subject=subject,
            cognitive_state=cognitive_state,
        )
        if observation_deletion.get("status") in {"blocked", "unsupported_scope"}:
            proof = DeletionProof(
                proof_id=f"proof-{uuid.uuid4().hex[:12]}",
                subject_hash=_hash_text(f"{subject.scope_kind}:{subject.scope_value}"),
                status="blocked",
                deleted_at=_now_iso(),
                affected_domains=("observation",),
                affected_consumers=(
                    "preflight_inject",
                    "reflection",
                    "cognitive_state",
                ),
                verification_results={
                    "snapshot": snapshot_verification,
                    "mode": "blocked_before_physical_deletion",
                    "cognitive_state": cognitive_state,
                    "observation": observation_deletion,
                },
            )
            self._record_request("delete_proof", subject, proof.as_dict())
            return proof
        embedding_cache_deletion = self._apply_embedding_cache_subject_deletion(
            request_id=deletion_request_id,
            subject=subject,
        )
        if embedding_cache_deletion.get("status") in {"blocked", "pending_checkpoint"}:
            proof = DeletionProof(
                proof_id=f"proof-{uuid.uuid4().hex[:12]}",
                subject_hash=_hash_text(f"{subject.scope_kind}:{subject.scope_value}"),
                status="blocked",
                deleted_at=_now_iso(),
                affected_domains=("embedding_cache",),
                affected_consumers=(
                    "embedding_index",
                    "context_aware_search",
                    "preflight_inject",
                ),
                verification_results={
                    "snapshot": snapshot_verification,
                    "mode": "blocked_before_raw_deletion",
                    "cognitive_state": cognitive_state,
                    "observation": observation_deletion,
                    "embedding_cache": embedding_cache_deletion,
                },
            )
            self._record_request("delete_proof", subject, proof.as_dict())
            return proof
        raw_deletion = self._apply_raw_subject_deletion(
            request_id=deletion_request_id,
            subject=subject,
        )
        if raw_deletion.get("status") == "blocked":
            proof = DeletionProof(
                proof_id=f"proof-{uuid.uuid4().hex[:12]}",
                subject_hash=_hash_text(f"{subject.scope_kind}:{subject.scope_value}"),
                status="blocked",
                deleted_at=_now_iso(),
                affected_domains=("raw",),
                affected_consumers=("raw_projection", "search", "distill"),
                verification_results={
                    "snapshot": snapshot_verification,
                    "mode": "blocked_before_model_ledger_deletion",
                    "cognitive_state": cognitive_state,
                    "observation": observation_deletion,
                    "embedding_cache": embedding_cache_deletion,
                    "raw": raw_deletion,
                },
            )
            self._record_request("delete_proof", subject, proof.as_dict())
            return proof
        consumer_access_log_deletion = self._consumer_access_log_result(raw_deletion)
        if consumer_access_log_deletion.get("status") == "blocked":
            blocked_affected_domains = {"consumer_access_log"}
            if raw_deletion.get("status") in {"applied", "existing"}:
                blocked_affected_domains.add("raw")
            proof = DeletionProof(
                proof_id=f"proof-{uuid.uuid4().hex[:12]}",
                subject_hash=_hash_text(f"{subject.scope_kind}:{subject.scope_value}"),
                status="blocked",
                deleted_at=_now_iso(),
                affected_domains=tuple(sorted(blocked_affected_domains)),
                affected_consumers=("privacy_audit", "data_delete", "scorecard"),
                verification_results={
                    "snapshot": snapshot_verification,
                    "mode": "blocked_after_raw_deletion",
                    "cognitive_state": cognitive_state,
                    "observation": observation_deletion,
                    "embedding_cache": embedding_cache_deletion,
                    "raw": raw_deletion,
                    "consumer_access_log": consumer_access_log_deletion,
                },
            )
            self._record_request("delete_proof", subject, proof.as_dict())
            return proof
        agent_source_metadata_deletion = self._apply_agent_source_metadata_subject_deletion(
            request_id=deletion_request_id,
            subject=subject,
        )
        if agent_source_metadata_deletion.get("status") in {
            "blocked",
            "unsupported_scope",
            "pending_checkpoint",
        }:
            blocked_domains = {"agent_source_metadata"}
            if raw_deletion.get("status") in {"applied", "existing"}:
                blocked_domains.add("raw")
            if embedding_cache_deletion.get("status") in {"applied", "existing"}:
                blocked_domains.add("embedding_cache")
            proof = DeletionProof(
                proof_id=f"proof-{uuid.uuid4().hex[:12]}",
                subject_hash=_hash_text(f"{subject.scope_kind}:{subject.scope_value}"),
                status="blocked",
                deleted_at=_now_iso(),
                affected_domains=tuple(sorted(blocked_domains)),
                affected_consumers=("sync", "backfill", "agent_kit"),
                verification_results={
                    "snapshot": snapshot_verification,
                    "mode": "blocked_before_persona_deletion",
                    "cognitive_state": cognitive_state,
                    "observation": observation_deletion,
                    "embedding_cache": embedding_cache_deletion,
                    "raw": raw_deletion,
                    "consumer_access_log": consumer_access_log_deletion,
                    "agent_source_metadata": agent_source_metadata_deletion,
                },
            )
            self._record_request("delete_proof", subject, proof.as_dict())
            return proof
        persona_deletion = self._apply_persona_subject_deletion(
            request_id=deletion_request_id,
            subject=subject,
        )
        if persona_deletion.get("status") == "blocked":
            blocked_domains = {"persona"}
            if raw_deletion.get("status") in {"applied", "existing"}:
                blocked_domains.add("raw")
            if embedding_cache_deletion.get("status") in {"applied", "existing"}:
                blocked_domains.add("embedding_cache")
            proof = DeletionProof(
                proof_id=f"proof-{uuid.uuid4().hex[:12]}",
                subject_hash=_hash_text(f"{subject.scope_kind}:{subject.scope_value}"),
                status="blocked",
                deleted_at=_now_iso(),
                affected_domains=tuple(sorted(blocked_domains)),
                affected_consumers=(
                    "persona_prompt",
                    "delivery_router",
                    "preflight_inject",
                ),
                verification_results={
                    "snapshot": snapshot_verification,
                    "mode": "blocked_before_model_ledger_deletion",
                    "cognitive_state": cognitive_state,
                    "observation": observation_deletion,
                    "embedding_cache": embedding_cache_deletion,
                    "raw": raw_deletion,
                    "persona": persona_deletion,
                },
            )
            self._record_request("delete_proof", subject, proof.as_dict())
            return proof
        reflection_deletion = self._apply_reflection_subject_deletion(
            request_id=deletion_request_id,
            subject=subject,
        )
        if reflection_deletion.get("status") == "blocked":
            blocked_domains = {"reflection"}
            if raw_deletion.get("status") in {"applied", "existing"}:
                blocked_domains.add("raw")
            if embedding_cache_deletion.get("status") in {"applied", "existing"}:
                blocked_domains.add("embedding_cache")
            if persona_deletion.get("status") in {"applied", "existing"}:
                blocked_domains.add("persona")
            proof = DeletionProof(
                proof_id=f"proof-{uuid.uuid4().hex[:12]}",
                subject_hash=_hash_text(f"{subject.scope_kind}:{subject.scope_value}"),
                status="blocked",
                deleted_at=_now_iso(),
                affected_domains=tuple(sorted(blocked_domains)),
                affected_consumers=(
                    "guard_check",
                    "check_pending_recaps",
                    "preflight_inject",
                ),
                verification_results={
                    "snapshot": snapshot_verification,
                    "mode": "blocked_before_model_ledger_deletion",
                    "cognitive_state": cognitive_state,
                    "observation": observation_deletion,
                    "embedding_cache": embedding_cache_deletion,
                    "raw": raw_deletion,
                    "persona": persona_deletion,
                    "reflection": reflection_deletion,
                },
            )
            self._record_request("delete_proof", subject, proof.as_dict())
            return proof
        cognitive_graph_deletion = self._apply_cognitive_graph_subject_deletion(
            request_id=deletion_request_id,
            subject=subject,
        )
        if cognitive_graph_deletion.get("status") == "blocked":
            blocked_domains = {"cognitive_graph"}
            if raw_deletion.get("status") in {"applied", "existing"}:
                blocked_domains.add("raw")
            if embedding_cache_deletion.get("status") in {"applied", "existing"}:
                blocked_domains.add("embedding_cache")
            if reflection_deletion.get("status") in {"applied", "existing"}:
                blocked_domains.add("reflection")
            if persona_deletion.get("status") in {"applied", "existing"}:
                blocked_domains.add("persona")
            proof = DeletionProof(
                proof_id=f"proof-{uuid.uuid4().hex[:12]}",
                subject_hash=_hash_text(f"{subject.scope_kind}:{subject.scope_value}"),
                status="blocked",
                deleted_at=_now_iso(),
                affected_domains=tuple(sorted(blocked_domains)),
                affected_consumers=(
                    "cognitive_graph",
                    "preflight_inject",
                    "relation_embeddings",
                ),
                verification_results={
                    "snapshot": snapshot_verification,
                    "mode": "blocked_before_model_ledger_deletion",
                    "cognitive_state": cognitive_state,
                    "observation": observation_deletion,
                    "embedding_cache": embedding_cache_deletion,
                    "raw": raw_deletion,
                    "persona": persona_deletion,
                    "reflection": reflection_deletion,
                    "cognitive_graph": cognitive_graph_deletion,
                },
            )
            self._record_request("delete_proof", subject, proof.as_dict())
            return proof
        wiki_deletion = self._apply_wiki_subject_deletion(
            request_id=deletion_request_id,
            subject=subject,
            material_action_resolver=self._wiki_delete_material_action_resolver(
                request_id=deletion_request_id,
                subject=subject,
                snapshot_verification=snapshot_verification,
            ),
        )
        if wiki_deletion.get("status") == "blocked":
            blocked_domains = {"wiki"}
            if raw_deletion.get("status") in {"applied", "existing"}:
                blocked_domains.add("raw")
            if embedding_cache_deletion.get("status") in {"applied", "existing"}:
                blocked_domains.add("embedding_cache")
            if reflection_deletion.get("status") in {"applied", "existing"}:
                blocked_domains.add("reflection")
            if cognitive_graph_deletion.get("status") in {"applied", "existing"}:
                blocked_domains.add("cognitive_graph")
            if persona_deletion.get("status") in {"applied", "existing"}:
                blocked_domains.add("persona")
            proof = DeletionProof(
                proof_id=f"proof-{uuid.uuid4().hex[:12]}",
                subject_hash=_hash_text(f"{subject.scope_kind}:{subject.scope_value}"),
                status="blocked",
                deleted_at=_now_iso(),
                affected_domains=tuple(sorted(blocked_domains)),
                affected_consumers=(
                    "knowledge_graph",
                    "cognitive_graph",
                    "relation_embeddings",
                    "wiki_search_index",
                    "wiki_metrics",
                    "moc_navigation",
                ),
                verification_results={
                    "snapshot": snapshot_verification,
                    "mode": "blocked_before_model_ledger_deletion",
                    "cognitive_state": cognitive_state,
                    "observation": observation_deletion,
                    "embedding_cache": embedding_cache_deletion,
                    "raw": raw_deletion,
                    "persona": persona_deletion,
                    "reflection": reflection_deletion,
                    "cognitive_graph": cognitive_graph_deletion,
                    "wiki": wiki_deletion,
                },
            )
            self._record_request("delete_proof", subject, proof.as_dict())
            return proof
        evidence_refs_deletion = self._apply_evidence_ref_deletion(
            subject=subject,
            wiki_deletion=wiki_deletion,
        )
        event_metadata_deletion = self._apply_event_metadata_subject_deletion(
            request_id=deletion_request_id,
            subject=subject,
        )
        action_ledger_deletion = self._apply_action_ledger_subject_deletion(
            request_id=deletion_request_id,
            subject=subject,
        )
        scoring_deletion = self._apply_scoring_subject_deletion(
            request_id=deletion_request_id,
            subject=subject,
        )
        from core.runtime_paths import RuntimePaths
        from core.telemetry.prompt_call_log import ModelCallLedger, ModelCallLedgerInvariantError

        ledger_path = RuntimePaths.from_config(self.config).model_call_ledger_db
        try:
            ledger_deletion = ModelCallLedger(
                ledger_path,
                config=self.config,
                initialize=False,
            ).delete_subject_scope(
                subject.scope_kind,
                subject.scope_value,
            )
        except ModelCallLedgerInvariantError:
            # A malformed existing ledger must not be opened through a
            # read-no-create bypass.  Keep the ownership delete blocked and
            # require the separately backup-gated reconciliation path.
            ledger_deletion = {
                "status": "blocked",
                "matched_run_count": 0,
                "deleted_entry_count": 0,
                "deleted_run_count": 0,
                "error": "model_call_ledger_schema_invalid",
            }
        deletion_owner_blocked = any(
            result.get("status") in {"blocked", "unsupported_scope", "pending_checkpoint"}
            for result in (
                event_metadata_deletion,
                action_ledger_deletion,
                scoring_deletion,
            )
        )
        ledger_blocked = ledger_deletion.get("status") == "blocked" or deletion_owner_blocked
        affected_domain_set = {"model_call_ledger"}
        resolved_domains = {"model_call_ledger"}
        if cognitive_state.get("status") in {"not_initialized", "no_targets", "verified"}:
            resolved_domains.add("cognitive_state")
        if observation_deletion.get("status") in {"applied", "existing"}:
            affected_domain_set.add("observation")
        if observation_deletion.get("verified") is True:
            resolved_domains.add("observation")
        if embedding_cache_deletion.get("status") in {"applied", "existing"}:
            affected_domain_set.add("embedding_cache")
        if embedding_cache_deletion.get("verified") is True:
            resolved_domains.add("embedding_cache")
        if raw_deletion.get("status") in {"applied", "existing"}:
            affected_domain_set.add("raw")
        if raw_deletion.get("verified") is True:
            resolved_domains.add("raw")
        if consumer_access_log_deletion.get("target_count"):
            affected_domain_set.add("consumer_access_log")
        if consumer_access_log_deletion.get("verified") is True:
            resolved_domains.add("consumer_access_log")
        if agent_source_metadata_deletion.get("status") in {"applied", "existing"}:
            affected_domain_set.add("agent_source_metadata")
        if agent_source_metadata_deletion.get("verified") is True:
            resolved_domains.add("agent_source_metadata")
        if persona_deletion.get("status") in {"applied", "existing"}:
            affected_domain_set.add("persona")
        if persona_deletion.get("verified") is True:
            resolved_domains.add("persona")
        if reflection_deletion.get("status") in {"applied", "existing"}:
            affected_domain_set.add("reflection")
        if reflection_deletion.get("verified") is True:
            resolved_domains.add("reflection")
        if cognitive_graph_deletion.get("status") in {"applied", "existing"}:
            affected_domain_set.add("cognitive_graph")
        if cognitive_graph_deletion.get("verified") is True:
            resolved_domains.add("cognitive_graph")
        if wiki_deletion.get("status") in {"applied", "existing"}:
            affected_domain_set.add("wiki")
        if wiki_deletion.get("verified") is True:
            resolved_domains.add("wiki")
        if evidence_refs_deletion.get("verified") is True:
            resolved_domains.add("evidence_refs")
        if evidence_refs_deletion.get("verified") is True and evidence_refs_deletion.get(
            "target_count"
        ):
            affected_domain_set.add("evidence_refs")
        if event_metadata_deletion.get("status") in {"applied", "existing"}:
            affected_domain_set.add("metadata")
        if event_metadata_deletion.get("verified") is True:
            resolved_domains.add("metadata")
        if action_ledger_deletion.get("status") in {"applied", "existing"}:
            affected_domain_set.add("action_ledger")
        if action_ledger_deletion.get("verified") is True:
            resolved_domains.add("action_ledger")
        if scoring_deletion.get("status") in {"applied", "existing"}:
            affected_domain_set.add("scoring")
        if scoring_deletion.get("verified") is True:
            resolved_domains.add("scoring")
        remaining_domains = tuple(sorted(DATA_DOMAINS - resolved_domains))
        all_domains_terminal = not ledger_blocked and not remaining_domains
        affected_domains = tuple(sorted(affected_domain_set))
        affected_consumers = ["distill", "cost_audit", "quality_gate"]
        if "embedding_cache" in affected_domain_set:
            affected_consumers.extend(
                ("embedding_index", "context_aware_search", "preflight_inject")
            )
        if "raw" in affected_domain_set:
            affected_consumers.extend(("raw_projection", "search"))
        if "consumer_access_log" in affected_domain_set:
            affected_consumers.extend(("privacy_audit", "data_delete", "scorecard"))
        if "agent_source_metadata" in affected_domain_set:
            affected_consumers.extend(("sync", "backfill", "agent_kit"))
        if "persona" in affected_domain_set:
            affected_consumers.extend(("persona_prompt", "delivery_router", "preflight_inject"))
        if "observation" in affected_domain_set:
            affected_consumers.extend(("preflight_inject", "reflection", "cognitive_state"))
        if "reflection" in affected_domain_set:
            affected_consumers.extend(("guard_check", "check_pending_recaps", "preflight_inject"))
        if "cognitive_graph" in affected_domain_set:
            affected_consumers.extend(
                ("cognitive_graph", "preflight_inject", "relation_embeddings")
            )
        if "wiki" in affected_domain_set:
            affected_consumers.extend(
                (
                    "knowledge_graph",
                    "cognitive_graph",
                    "relation_embeddings",
                    "wiki_search_index",
                    "wiki_metrics",
                    "moc_navigation",
                )
            )
        if "evidence_refs" in affected_domain_set:
            affected_consumers.append("wiki_metrics")
        if "metadata" in affected_domain_set:
            affected_consumers.extend(("events", "doctor"))
        if "action_ledger" in affected_domain_set:
            affected_consumers.extend(("doctor", "health", "audit_scripts"))
        if "scoring" in affected_domain_set:
            affected_consumers.extend(("scorecard", "quality_gate", "adaptive_config"))
        proof = DeletionProof(
            proof_id=f"proof-{uuid.uuid4().hex[:12]}",
            subject_hash=_hash_text(f"{subject.scope_kind}:{subject.scope_value}"),
            # A proof becomes verified only after every declared owner reports
            # a terminal after-oracle.  Any unresolved or blocked owner keeps
            # the aggregate state nonterminal.
            status=(
                "blocked"
                if ledger_blocked
                else "verified" if all_domains_terminal else "partially_deleted"
            ),
            deleted_at=_now_iso(),
            affected_domains=affected_domains,
            affected_consumers=tuple(sorted(affected_consumers)),
            verification_results={
                "snapshot": snapshot_verification,
                "mode": (
                    "verified_physical_deletion"
                    if all_domains_terminal
                    else "partial_physical_deletion"
                ),
                "cognitive_state": cognitive_state,
                "observation": observation_deletion,
                "embedding_cache": embedding_cache_deletion,
                "raw": raw_deletion,
                "consumer_access_log": consumer_access_log_deletion,
                "agent_source_metadata": agent_source_metadata_deletion,
                "persona": persona_deletion,
                "reflection": reflection_deletion,
                "cognitive_graph": cognitive_graph_deletion,
                "wiki": wiki_deletion,
                "evidence_refs": evidence_refs_deletion,
                "metadata": event_metadata_deletion,
                "action_ledger": action_ledger_deletion,
                "scoring": scoring_deletion,
                "model_call_ledger": ledger_deletion,
                "remaining_unimplemented_domains": remaining_domains,
            },
        )
        self._record_request("delete_proof", subject, proof.as_dict())
        if not ledger_blocked:
            from core.ops.runtime_flow_telemetry import record_runtime_consumed

            record_runtime_consumed(
                "data_inventory_to_delete_proof",
                source="core/privacy/data_ownership.py:DeletionProof",
                item_id=deletion_flow_item_id,
                metadata={
                    "transition": (
                        "verified_physical_deletion"
                        if all_domains_terminal
                        else "partial_deletion_adapters_applied"
                    ),
                    "proof_id": proof.proof_id,
                },
                config_or_path=_database_dir(self.config),
            )
        return proof
