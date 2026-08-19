"""Domain-owned deletion adapters used by DataOwnershipManager."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Any, Callable, Mapping

from core.privacy.data_ownership_contracts import (
    DATA_DELETE_DECISION_CONTRACT_ID,
    DATA_DELETE_DECISION_CONTRACT_REVISION,
    DATA_DELETE_DECISION_CONTRACT_TEXT,
    DATA_DELETE_DECISION_PRODUCER_HASH,
    DataSubjectRef,
    _configured_cognitive_graph_db_paths,
    _configured_embedding_cache_db,
    _configured_event_bus_db,
    _configured_observation_db_paths,
    _configured_persona_db_paths,
    _configured_raw_event_db,
    _configured_reflection_db_paths,
    _configured_scoring_db_paths,
    _configured_wiki_metrics_db,
    _configured_wiki_projection_db,
    _database_dir,
    _hash_text,
    _vault_dir,
)

if TYPE_CHECKING:
    from core.cognitive.decision_trace import (
        MaterialActionAuthorization,
        MaterialActionRequest,
    )


class DataOwnershipDeletionAdaptersMixin:
    """Apply and verify deletion through each domain's canonical owner."""

    if TYPE_CHECKING:
        config: Any
        event_bus: Any | None

    def _plan_cognitive_state_tombstone(
        self,
        *,
        request_id: str,
        subject: DataSubjectRef,
        snapshot_ref: str,
    ) -> dict[str, Any]:
        """Create the canonical state tombstone before any physical deletion.

        A missing state ledger means there is no initialized state projection to
        tombstone. A malformed or incomplete initialized ledger is a hard
        deletion blocker; it must not be bypassed by deleting a different
        domain first.
        """

        state_path = _database_dir(self.config) / "producer_consumer_ledger.db"
        if not state_path.is_file():
            return {
                "status": "not_initialized",
                "target_count": 0,
                "required_consumers": [],
            }
        from core.cognitive.state_schema import CognitiveStateSchemaError
        from core.cognitive.state_store import CognitiveStateConflict, CognitiveStateStore

        try:
            store = CognitiveStateStore(self.config)
            plan = store.plan_subject_tombstone(
                request_id=request_id,
                scope_kind=subject.scope_kind,
                scope_value=subject.scope_value,
                snapshot_ref=snapshot_ref,
            )
        except (
            CognitiveStateConflict,
            CognitiveStateSchemaError,
            FileNotFoundError,
            OSError,
            sqlite3.Error,
            ValueError,
        ):
            return {
                "status": "blocked",
                "target_count": 0,
                "required_consumers": [],
                "error": "cognitive_state_tombstone_plan_failed",
            }
        if plan.status in {"committed", "existing"}:
            receipt_status = store.tombstone_status(plan.request_id)
            return {
                "status": "verified" if receipt_status["verified"] else "pending_consumer_receipts",
                "plan_status": plan.status,
                "control_revision_id": plan.control_revision_id,
                "target_count": len(plan.target_revision_ids),
                "command_count": len(plan.command_ids),
                "required_consumers": list(plan.required_consumers),
                "receipt_status": receipt_status["status"],
            }
        if plan.status == "no_targets":
            return {
                "status": "no_targets",
                "target_count": 0,
                "required_consumers": [],
            }
        return {
            "status": "blocked",
            "plan_status": plan.status,
            "target_count": len(plan.target_revision_ids),
            "required_consumers": list(plan.required_consumers),
            "error": "cognitive_state_tombstone_contract_incomplete",
        }

    def _apply_raw_subject_deletion(
        self,
        *,
        request_id: str,
        subject: DataSubjectRef,
    ) -> dict[str, Any]:
        """Apply the canonical Raw owner deletion without fabricating an owner DB.

        A missing Raw database means that domain was never initialized for the
        selected runtime.  A present but unreconcilable Raw database is a hard
        blocker: model-call deletion must not be presented as sufficient while
        canonical source bytes remain recoverable.
        """

        raw_path = _configured_raw_event_db(self.config)
        if not raw_path.is_file():
            return {
                "status": "not_initialized",
                "target_count": 0,
                "verified": True,
            }
        store = None
        try:
            from core.sync_framework.raw_event_store import RawEventStore

            store = RawEventStore(db_path=raw_path, config=self.config)
            result = dict(
                store.delete_subject_scope(
                    request_id=request_id,
                    scope_kind=subject.scope_kind,
                    scope_value=subject.scope_value,
                )
            )
            result["verified"] = (
                result.get("status") in {"applied", "existing", "no_targets"}
                and int(result.get("pending_dependent_consumers") or 0) == 0
            )
            return result
        except (OSError, PermissionError, sqlite3.Error, RuntimeError, ValueError):
            return {
                "status": "blocked",
                "target_count": 0,
                "error": "raw_subject_deletion_failed",
            }
        finally:
            if store is not None:
                store.close()

    @staticmethod
    def _consumer_access_log_result(raw_deletion: Mapping[str, Any]) -> dict[str, Any]:
        """Project Raw's access-log after-oracle as its own ownership domain.

        ``raw_access_log`` is physically owned by canonical Raw.  Giving it a
        second deletion writer would race the Raw receipt transaction, so this
        adapter only accepts Raw's typed after-oracle and never fabricates a
        standalone successful consumer-log proof.
        """

        status = str(raw_deletion.get("status") or "")
        if status == "not_initialized":
            return {
                "status": "not_initialized",
                "target_count": 0,
                "after_count": 0,
                "verified": True,
                "owner": "raw_event_store",
            }
        after_count = raw_deletion.get("access_log_after_count")
        verified = raw_deletion.get("consumer_access_log_verified") is True
        if status not in {"applied", "existing", "no_targets"} or not verified:
            return {
                "status": "blocked",
                "target_count": int(raw_deletion.get("access_log_deleted") or 0),
                "after_count": after_count,
                "verified": False,
                "owner": "raw_event_store",
                "error": "raw_access_log_after_oracle_unverified",
            }
        return {
            "status": status,
            "target_count": int(raw_deletion.get("access_log_deleted") or 0),
            "after_count": int(after_count or 0),
            "verified": True,
            "owner": "raw_event_store",
        }

    def _apply_embedding_cache_subject_deletion(
        self,
        *,
        request_id: str,
        subject: DataSubjectRef,
    ) -> dict[str, Any]:
        """Flush the only persistent embedding cache before source deletion.

        Cache rows do not retain source-asset provenance, so an exact subject
        filter would be a false assurance.  The cache owner deliberately
        performs a global, secure flush and proves the after oracle instead.
        """

        try:
            from core.embeddings.cache import EmbeddingCache

            return EmbeddingCache.delete_subject_scope(
                db_path=_configured_embedding_cache_db(self.config),
                request_id=request_id,
                scope_kind=subject.scope_kind,
                scope_value_hash=_hash_text(f"{subject.scope_kind}:{subject.scope_value}"),
            )
        except (OSError, PermissionError, sqlite3.Error, RuntimeError, ValueError):
            return {
                "status": "blocked",
                "target_count": 0,
                "verified": False,
                "error": "embedding_cache_subject_deletion_failed",
            }

    def _apply_agent_source_metadata_subject_deletion(
        self,
        *,
        request_id: str,
        subject: DataSubjectRef,
    ) -> dict[str, Any]:
        """Apply the formal SyncEngine metadata owner without guessing lineage.

        The owner only accepts ``all``, ``agent``, and ``session`` because
        those are the stable headers actually persisted by SyncEngine.  An
        aggregate audit row with no session key remains an explicit unresolved
        dependency, not a reason to erase a broader source or claim closure.
        """

        db_path = _database_dir(self.config) / "sync_log.db"
        if not db_path.is_file():
            return {
                "status": "not_initialized",
                "target_count": 0,
                "receipt_count": 0,
                "verified": True,
            }
        try:
            from core.sync_framework.agent_source_metadata_deletion import (
                delete_agent_source_metadata_subject_scope,
            )

            return dict(
                delete_agent_source_metadata_subject_scope(
                    db_path=db_path,
                    request_id=request_id,
                    scope_kind=subject.scope_kind,
                    scope_value=subject.scope_value,
                )
            )
        except (OSError, PermissionError, sqlite3.Error, RuntimeError, ValueError):
            return {
                "status": "blocked",
                "target_count": 0,
                "receipt_count": 0,
                "verified": False,
                "error": "agent_source_metadata_subject_deletion_failed",
            }

    def _apply_event_metadata_subject_deletion(
        self,
        *,
        request_id: str,
        subject: DataSubjectRef,
    ) -> dict[str, Any]:
        """Delete EventBus metadata through its one trace/provenance owner."""

        db_path = _configured_event_bus_db(self.config)
        if not db_path.is_file():
            return {
                "status": "not_initialized",
                "target_count": 0,
                "receipt_count": 0,
                "unresolved_legacy_count": 0,
                "verified": True,
            }
        try:
            from core.ops.event_subject_provenance import delete_event_subject_scope

            return dict(
                delete_event_subject_scope(
                    db_path=db_path,
                    request_id=request_id,
                    scope_kind=subject.scope_kind,
                    scope_value=subject.scope_value,
                )
            )
        except (OSError, PermissionError, sqlite3.Error, RuntimeError, ValueError):
            return {
                "status": "blocked",
                "target_count": 0,
                "receipt_count": 0,
                "verified": False,
                "error": "event_metadata_subject_deletion_failed",
            }

    def _apply_action_ledger_subject_deletion(
        self,
        *,
        request_id: str,
        subject: DataSubjectRef,
    ) -> dict[str, Any]:
        """Append exact ledger tombstones; immutable evidence rows stay intact."""

        db_path = _database_dir(self.config) / "action_ledger.db"
        if not db_path.is_file():
            return {
                "status": "not_initialized",
                "target_count": 0,
                "receipt_count": 0,
                "unresolved_legacy_count": 0,
                "verified": True,
            }
        try:
            from core.ops.action_ledger_subject_provenance import (
                delete_action_ledger_subject_scope,
            )

            return dict(
                delete_action_ledger_subject_scope(
                    db_path=db_path,
                    request_id=request_id,
                    scope_kind=subject.scope_kind,
                    scope_value=subject.scope_value,
                )
            )
        except (OSError, PermissionError, sqlite3.Error, RuntimeError, ValueError):
            return {
                "status": "blocked",
                "target_count": 0,
                "receipt_count": 0,
                "verified": False,
                "error": "action_ledger_subject_deletion_failed",
            }

    def _apply_governed_training_tombstone(
        self,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        """Close the governed-training consumer of one canonical tombstone."""

        empty = {
            "status": "not_initialized",
            "sample_count": 0,
            "model_count": 0,
            "receipt_ids": (),
            "verified": True,
        }
        from core.cognitive.state_store import CognitiveStateStore
        from core.cognitive.state_types import COGNITIVE_TOMBSTONE_COMMAND_TYPE
        from core.cognitive.training_governance import (
            TRAINING_PROJECTION_CONSUMER,
            TrainingGovernanceStore,
        )
        from core.scoring.training_schema import inspect_training_schema

        database_dir = _database_dir(self.config)
        state_path = database_dir / "producer_consumer_ledger.db"
        scoring_path = database_dir / "mnemos.db"
        if not state_path.is_file():
            return empty
        cognitive_request_id = "cog-" + request_id.removeprefix("delete-")
        with sqlite3.connect(
            f"file:{state_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as state_conn:
            command_ids = [
                str(row[0])
                for row in state_conn.execute(
                    "SELECT command_id FROM cognitive_state_outbox "
                    "WHERE consumer_id=? AND command_type=? "
                    "AND json_extract(payload_json, '$.request_id')=?",
                    (
                        TRAINING_PROJECTION_CONSUMER,
                        COGNITIVE_TOMBSTONE_COMMAND_TYPE,
                        cognitive_request_id,
                    ),
                ).fetchall()
            ]
        if len(command_ids) > 1:
            raise RuntimeError("multiple governed training tombstone commands")
        if not command_ids:
            return empty
        if not scoring_path.is_file():
            raise RuntimeError("governed training projection database is missing")
        with sqlite3.connect(
            f"file:{scoring_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as scoring_conn:
            if not inspect_training_schema(scoring_conn).ok:
                raise RuntimeError("governed training projection schema is invalid")
        state = CognitiveStateStore(self.config)
        applied = TrainingGovernanceStore(
            state,
            database_dir=database_dir,
        ).apply_tombstone_command(command_ids[0])
        return {
            **applied,
            "verified": (
                int(applied["remaining_model_head_count"]) == 0
                and len(applied["receipt_ids"]) == int(applied["sample_count"])
                and state.effect_receipt(command_ids[0]) is not None
            ),
        }

    def _apply_scoring_subject_deletion(
        self,
        *,
        request_id: str,
        subject: DataSubjectRef,
    ) -> dict[str, Any]:
        """Delete every declared scoring owner without guessing historical lineage."""

        paths = tuple(path for path in _configured_scoring_db_paths(self.config) if path.is_file())
        if not paths:
            return {
                "status": "not_initialized",
                "target_count": 0,
                "receipt_count": 0,
                "unresolved_legacy_count": 0,
                "verified": True,
            }
        try:
            governed = self._apply_governed_training_tombstone(
                request_id=request_id,
            )
            from core.scoring.subject_provenance import delete_scoring_subject_scope

            results = [
                dict(
                    delete_scoring_subject_scope(
                        db_path=path,
                        request_id=request_id,
                        scope_kind=subject.scope_kind,
                        scope_value=subject.scope_value,
                    )
                )
                for path in paths
            ]
        except (OSError, PermissionError, sqlite3.Error, RuntimeError, ValueError):
            return {
                "status": "blocked",
                "target_count": 0,
                "receipt_count": 0,
                "verified": False,
                "error": "scoring_subject_deletion_failed",
            }
        if governed.get("verified") is not True or any(
            result.get("status") in {"blocked", "unsupported_scope", "pending_checkpoint"}
            for result in results
        ):
            return {
                "status": "blocked",
                "target_count": (
                    int(governed.get("sample_count") or 0)
                    + int(governed.get("model_count") or 0)
                    + sum(int(result.get("target_count") or 0) for result in results)
                ),
                "receipt_count": sum(int(result.get("receipt_count") or 0) for result in results),
                "verified": False,
                "error": "scoring_subject_deletion_failed",
            }
        return {
            "status": (
                "applied"
                if governed.get("status") in {"applied", "existing"}
                or any(result.get("status") in {"applied", "existing"} for result in results)
                else "no_targets"
            ),
            "target_count": (
                int(governed.get("sample_count") or 0)
                + int(governed.get("model_count") or 0)
                + sum(int(result.get("target_count") or 0) for result in results)
            ),
            "receipt_count": (
                len(governed.get("receipt_ids") or ())
                + sum(int(result.get("receipt_count") or 0) for result in results)
            ),
            "governed_samples_excluded": int(governed.get("sample_count") or 0),
            "governed_models_deactivated": int(governed.get("model_count") or 0),
            "governed_projection_oracle_hash": str(governed.get("projection_oracle_hash") or ""),
            "training_samples_deleted": sum(
                int(result.get("training_samples_deleted") or 0) for result in results
            ),
            "ground_truth_deleted": sum(
                int(result.get("ground_truth_deleted") or 0) for result in results
            ),
            "search_sessions_deleted": sum(
                int(result.get("search_sessions_deleted") or 0) for result in results
            ),
            "feedback_events_deleted": sum(
                int(result.get("feedback_events_deleted") or 0) for result in results
            ),
            "models_invalidated": sum(
                int(result.get("models_invalidated") or 0) for result in results
            ),
            "bayesian_states_invalidated": sum(
                int(result.get("bayesian_states_invalidated") or 0) for result in results
            ),
            "bayesian_feedback_deleted": sum(
                int(result.get("bayesian_feedback_deleted") or 0) for result in results
            ),
            "feedback_prompts_deleted": sum(
                int(result.get("feedback_prompts_deleted") or 0) for result in results
            ),
            "unresolved_legacy_count": sum(
                int(result.get("unresolved_legacy_count") or 0) for result in results
            ),
            "verified": bool(governed.get("verified"))
            and all(bool(result.get("verified")) for result in results),
        }

    def _apply_persona_subject_deletion(
        self,
        *,
        request_id: str,
        subject: DataSubjectRef,
    ) -> dict[str, Any]:
        """Dispatch object-scoped deletion to every declared profile store."""

        paths = tuple(path for path in _configured_persona_db_paths(self.config) if path.is_file())
        if not paths:
            return {
                "status": "not_initialized",
                "target_count": 0,
                "receipt_count": 0,
                "unresolved_legacy_count": 0,
                "unmapped_legacy_persona_count": 0,
                "verified": True,
            }
        results: list[dict[str, Any]] = []
        for path in paths:
            try:
                from core.persona.profile_subject_deletion import delete_profile_subject_scope

                result = delete_profile_subject_scope(
                    db_path=path,
                    request_id=request_id,
                    scope_kind=subject.scope_kind,
                    scope_value=subject.scope_value,
                )
            except (OSError, PermissionError, sqlite3.Error, RuntimeError, ValueError):
                return {
                    "status": "blocked",
                    "target_count": 0,
                    "receipt_count": 0,
                    "verified": False,
                    "error": "persona_subject_deletion_failed",
                }
            results.append(dict(result))
        if any(result.get("status") in {"blocked", "unsupported_scope"} for result in results):
            return {
                "status": "blocked",
                "target_count": 0,
                "receipt_count": 0,
                "verified": False,
                "error": "persona_subject_deletion_failed",
            }
        target_count = sum(int(result.get("target_count") or 0) for result in results)
        receipt_count = sum(int(result.get("receipt_count") or 0) for result in results)
        if any(result.get("status") in {"applied", "existing"} for result in results):
            status = "applied"
        else:
            status = "no_targets"
        return {
            "status": status,
            "target_count": target_count,
            "receipt_count": receipt_count,
            "profile_signals_deleted": sum(
                int(result.get("profile_signals_deleted") or 0) for result in results
            ),
            "profile_assertions_deleted": sum(
                int(result.get("profile_assertions_deleted") or 0) for result in results
            ),
            "profile_read_authorizations_deleted": sum(
                int(result.get("profile_read_authorizations_deleted") or 0) for result in results
            ),
            "profile_usage_logs_deleted": sum(
                int(result.get("profile_usage_logs_deleted") or 0) for result in results
            ),
            "profile_usage_outboxes_deleted": sum(
                int(result.get("profile_usage_outboxes_deleted") or 0) for result in results
            ),
            "unresolved_legacy_count": sum(
                int(result.get("unresolved_legacy_count") or 0) for result in results
            ),
            "unmapped_legacy_persona_count": sum(
                int(result.get("unmapped_legacy_persona_count") or 0) for result in results
            ),
            "verified": all(bool(result.get("verified")) for result in results),
        }

    def _apply_reflection_subject_deletion(
        self,
        *,
        request_id: str,
        subject: DataSubjectRef,
    ) -> dict[str, Any]:
        """Dispatch deletion to every explicit, existing Reflection owner."""

        paths = tuple(
            path for path in _configured_reflection_db_paths(self.config) if path.is_file()
        )
        if not paths:
            return {
                "status": "not_initialized",
                "target_count": 0,
                "verified": True,
            }
        results: list[dict[str, Any]] = []
        for path in paths:
            try:
                from core.reflection.reflection_store import ReflectionStore

                result = ReflectionStore(str(path)).delete_subject_scope(
                    request_id=request_id,
                    scope_kind=subject.scope_kind,
                    scope_value=subject.scope_value,
                )
            except (OSError, PermissionError, sqlite3.Error, RuntimeError, ValueError):
                return {
                    "status": "blocked",
                    "target_count": 0,
                    "verified": False,
                    "error": "reflection_subject_deletion_failed",
                }
            results.append(dict(result))
        if any(result.get("status") == "blocked" for result in results):
            return {
                "status": "blocked",
                "target_count": 0,
                "verified": False,
                "error": "reflection_subject_deletion_failed",
            }
        target_count = sum(int(result.get("target_count") or 0) for result in results)
        receipt_count = sum(int(result.get("receipt_count") or 0) for result in results)
        if any(result.get("status") in {"applied", "existing"} for result in results):
            status = "applied"
        else:
            status = "no_targets"
        return {
            "status": status,
            "target_count": target_count,
            "receipt_count": receipt_count,
            "verified": all(bool(result.get("verified")) for result in results),
            "legacy_unscoped_layer5_count": sum(
                int(result.get("legacy_unscoped_layer5_count") or 0) for result in results
            ),
            "unresolved_legacy_record_count": sum(
                int(result.get("unresolved_legacy_record_count") or 0) for result in results
            ),
            "unresolved_legacy_shift_count": sum(
                int(result.get("unresolved_legacy_shift_count") or 0) for result in results
            ),
        }

    def _apply_cognitive_graph_subject_deletion(
        self,
        *,
        request_id: str,
        subject: DataSubjectRef,
    ) -> dict[str, Any]:
        """Dispatch ACL-scoped deletion to every explicit graph owner."""

        paths = tuple(
            path for path in _configured_cognitive_graph_db_paths(self.config) if path.is_file()
        )
        if not paths:
            return {
                "status": "not_initialized",
                "target_count": 0,
                "verified": True,
            }
        results: list[dict[str, Any]] = []
        for path in paths:
            try:
                from core.cognitive_graph.store import CognitiveGraphStore

                result = CognitiveGraphStore(str(path)).delete_subject_scope(
                    request_id=request_id,
                    scope_kind=subject.scope_kind,
                    scope_value=subject.scope_value,
                )
            except (OSError, PermissionError, sqlite3.Error, RuntimeError, ValueError):
                return {
                    "status": "blocked",
                    "target_count": 0,
                    "verified": False,
                    "error": "cognitive_graph_subject_deletion_failed",
                }
            results.append(dict(result))
        if any(result.get("status") == "blocked" for result in results):
            return {
                "status": "blocked",
                "target_count": 0,
                "verified": False,
                "error": "cognitive_graph_subject_deletion_failed",
            }
        target_count = sum(int(result.get("target_count") or 0) for result in results)
        receipt_count = sum(int(result.get("receipt_count") or 0) for result in results)
        if any(result.get("status") in {"applied", "existing"} for result in results):
            status = "applied"
        else:
            status = "no_targets"
        return {
            "status": status,
            "target_count": target_count,
            "receipt_count": receipt_count,
            "verified": all(bool(result.get("verified")) for result in results),
            "unresolved_legacy_count": sum(
                int(result.get("unresolved_legacy_count") or 0) for result in results
            ),
        }

    def _apply_observation_subject_deletion(
        self,
        *,
        request_id: str,
        subject: DataSubjectRef,
        cognitive_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Delete observations only after their state tombstone checkpoint."""

        if cognitive_state.get("status") == "pending_consumer_receipts":
            return {
                "status": "pending_checkpoint",
                "target_count": 0,
                "receipt_count": 0,
                "verified": False,
                "error": "cognitive_state_tombstone_pending",
            }
        results: list[dict[str, Any]] = []
        for path in _configured_observation_db_paths(self.config):
            if not path.is_file():
                continue
            try:
                from core.cognitive.observation_store import ObservationStore

                result = ObservationStore(str(path)).delete_subject_scope(
                    request_id=request_id,
                    scope_kind=subject.scope_kind,
                    scope_value=subject.scope_value,
                )
            except (OSError, PermissionError, RuntimeError, ValueError, sqlite3.Error):
                return {
                    "status": "blocked",
                    "target_count": 0,
                    "receipt_count": 0,
                    "verified": False,
                    "error": "observation_subject_deletion_failed",
                }
            results.append(dict(result))
        if not results:
            return {
                "status": "not_initialized",
                "target_count": 0,
                "receipt_count": 0,
                "verified": True,
            }
        blocked = any(
            result.get("status") in {"blocked", "unsupported_scope"} for result in results
        )
        target_count = sum(int(result.get("target_count") or 0) for result in results)
        receipt_count = sum(int(result.get("receipt_count") or 0) for result in results)
        after_count = sum(int(result.get("after_count") or 0) for result in results)
        unresolved = sum(int(result.get("unresolved_legacy_count") or 0) for result in results)
        if blocked:
            status = "blocked"
        elif any(result.get("status") == "applied" for result in results):
            status = "applied"
        elif any(result.get("status") == "existing" for result in results):
            status = "existing"
        else:
            status = "no_targets"
        return {
            "status": status,
            "target_count": target_count,
            "receipt_count": receipt_count,
            "after_count": after_count,
            "unresolved_legacy_count": unresolved,
            "verified": not blocked
            and unresolved == 0
            and after_count == 0
            and all(bool(result.get("verified")) for result in results),
        }

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
    ) -> dict[str, Any]:
        """Delete only formally registered Wiki pages through their ledger.

        A nonempty vault without its lifecycle owner is not treated as an
        empty domain.  It remains a hard privacy blocker until the explicit
        Wiki projection reconciliation/migration has established page IDs.
        """

        wiki_dir = _vault_dir(self.config, "mnemos")
        if wiki_dir is None or not wiki_dir.is_dir():
            return {
                "status": "not_initialized",
                "target_count": 0,
                "verified": True,
            }
        page_count = sum(
            1
            for path in wiki_dir.rglob("*.md")
            if path.is_file()
            and not any(part.startswith(".") for part in path.relative_to(wiki_dir).parts)
        )
        projection_db = _configured_wiki_projection_db(self.config)
        if not projection_db.is_file():
            if page_count == 0:
                return {
                    "status": "not_initialized",
                    "target_count": 0,
                    "verified": True,
                }
            return {
                "status": "blocked",
                "target_count": 0,
                "verified": False,
                "unregistered_page_count": page_count,
                "error": "wiki_projection_ledger_uninitialized",
            }
        if self.event_bus is None:
            return {
                "status": "blocked",
                "target_count": 0,
                "verified": False,
                "error": "wiki_event_bus_required",
            }
        event_projection_db = getattr(self.event_bus, "projection_db_path", None)
        if not isinstance(event_projection_db, (str, os.PathLike, Path)):
            return {
                "status": "blocked",
                "target_count": 0,
                "verified": False,
                "error": "wiki_event_bus_projection_identity_required",
            }
        if Path(event_projection_db).expanduser().resolve(strict=False) != (
            projection_db.expanduser().resolve(strict=False)
        ):
            return {
                "status": "blocked",
                "target_count": 0,
                "verified": False,
                "error": "wiki_event_bus_projection_mismatch",
            }
        try:
            from core.privacy.wiki_subject_deletion import WikiSubjectDeletionService

            return WikiSubjectDeletionService(
                wiki_dir=wiki_dir,
                projection_db_path=projection_db,
                event_bus=self.event_bus,
                material_action_resolver=material_action_resolver,
            ).delete_subject_scope(
                request_id=request_id,
                scope_kind=subject.scope_kind,
                scope_value=subject.scope_value,
            )
        except (OSError, PermissionError, RuntimeError, ValueError, sqlite3.Error):
            return {
                "status": "blocked",
                "target_count": 0,
                "verified": False,
                "error": "wiki_subject_deletion_failed",
            }

    def _wiki_delete_material_action_resolver(
        self,
        *,
        request_id: str,
        subject: DataSubjectRef,
        snapshot_verification: Mapping[str, Any],
    ) -> Callable[
        [MaterialActionRequest, Mapping[str, Any]],
        MaterialActionAuthorization,
    ]:
        """Bind each Wiki unlink to the verified ownership request facts."""

        from core.cognitive.decision_trace import (
            ProjectContractDecisionContext,
            ProjectContractMaterialActionResolver,
            build_exact_project_contract_evaluator,
        )

        snapshot_created_at = str(snapshot_verification.get("snapshot_created_at") or "").strip()
        snapshot_id_hash = str(snapshot_verification.get("snapshot_id_hash") or "").strip()
        manifest_sha256 = str(snapshot_verification.get("manifest_sha256") or "").strip()
        if not snapshot_created_at or not snapshot_id_hash or not manifest_sha256:
            raise PermissionError("verified data-delete snapshot lacks immutable decision identity")

        def resolve(
            request: MaterialActionRequest,
            deletion_receipt: Mapping[str, Any],
        ) -> MaterialActionAuthorization:
            """Authorize one unlink against its exact deletion receipt."""

            receipt_id = str(deletion_receipt.get("receipt_id") or "").strip()
            page_id = str(deletion_receipt.get("page_id") or "").strip()
            before_content_sha256 = str(deletion_receipt.get("before_content_sha256") or "").strip()
            receipt_request_id = str(deletion_receipt.get("request_id") or "").strip()
            if (
                not receipt_id
                or not page_id
                or len(before_content_sha256) != 64
                or receipt_request_id != request_id
            ):
                raise PermissionError(
                    "Wiki subject deletion receipt is not bound to the verified request"
                )
            source_facts = {
                "schema_version": "mnemos.data_delete_decision_facts.v1",
                "request_id": request_id,
                "scope_kind": subject.scope_kind,
                "scope_value_hash": _hash_text(subject.scope_value),
                "snapshot_id_hash": snapshot_id_hash,
                "snapshot_manifest_sha256": manifest_sha256,
                "receipt_id": receipt_id,
                "page_id": page_id,
                "before_content_sha256": before_content_sha256,
            }
            source_facts_hash, evaluator = build_exact_project_contract_evaluator(
                expected_request=request,
                source_facts=source_facts,
                decision_checks={
                    "receipt_matches_request": receipt_request_id == request_id,
                    "receipt_identity_is_bound": bool(receipt_id and page_id),
                    "before_hash_is_sha256": (
                        len(before_content_sha256) == 64
                        and all(
                            value in "0123456789abcdef" for value in before_content_sha256.lower()
                        )
                    ),
                    "snapshot_identity_is_bound": bool(snapshot_id_hash and manifest_sha256),
                    "canonical_state_store_is_bound": bool(str(request.expected_state_db).strip()),
                },
                approved_candidate_key="delete_receipted_subject_page",
                approved_candidate_summary=(
                    "Delete the exact Wiki page selected by the verified "
                    "subject-deletion receipt."
                ),
                rejected_candidate_key="retain_unbound_subject_page",
                rejected_candidate_summary=(
                    "Retain a Wiki page that is outside the verified deletion receipt."
                ),
                approved_reason_code="subject_deletion_binding_verified",
                rejected_reason_code="subject_deletion_binding_rejected",
                committed_metric="wiki_subject_delete_receipt",
                rejected_metric="unbound_wiki_subject_delete_count",
            )
            resolver = ProjectContractMaterialActionResolver(
                ProjectContractDecisionContext(
                    state_db_path=Path(request.expected_state_db),
                    contract_id=DATA_DELETE_DECISION_CONTRACT_ID,
                    contract_revision_id=DATA_DELETE_DECISION_CONTRACT_REVISION,
                    contract_text=DATA_DELETE_DECISION_CONTRACT_TEXT,
                    contract_evidence_ref=(
                        f"{DATA_DELETE_DECISION_CONTRACT_ID}"
                        f"#{DATA_DELETE_DECISION_CONTRACT_REVISION}"
                    ),
                    source_id=f"data-delete:{request_id}",
                    source_revision_id=(f"snapshot:{snapshot_id_hash}:wiki-receipt:{receipt_id}"),
                    source_content_hash=source_facts_hash,
                    source_uri=f"data-delete://{request_id}/{receipt_id}",
                    evidence_refs=(
                        f"data-delete:{request_id}",
                        f"snapshot:{snapshot_id_hash}",
                        f"wiki-subject-delete:{receipt_id}",
                    ),
                    task="Apply one verified Wiki subject deletion",
                    goal=(
                        "Remove only the exact lifecycle-owned page selected by "
                        "the confirmed data ownership request."
                    ),
                    constraints=(
                        "The ownership freeze and retained snapshot must remain valid.",
                        "The page target and before hash must match the typed receipt.",
                    ),
                    created_at=snapshot_created_at,
                    scope_prefix=f"data-delete:{request_id}",
                    producer="data-ownership-manager",
                    producer_version=DATA_DELETE_DECISION_CONTRACT_REVISION,
                    producer_code_hash=DATA_DELETE_DECISION_PRODUCER_HASH,
                    evaluator_id="data-delete-wiki-receipt-evaluator",
                    evaluator=evaluator,
                )
            )
            return resolver(request)

        return resolve

    def _apply_evidence_ref_deletion(
        self,
        *,
        subject: DataSubjectRef,
        wiki_deletion: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Verify the Wiki metrics evidence projection after a page deletion.

        Evidence refs in ``page_metrics`` are a required Wiki lifecycle
        projection, not a second source of delete commands.  This adapter
        therefore waits for the formal Wiki receipt gate, then checks that no
        metric row remains for the lifecycle receipt paths.  It returns only
        counts and opaque ownership status; paths never enter a deletion proof.
        """

        wiki_status = str(wiki_deletion.get("status") or "")
        if wiki_status == "not_initialized" and wiki_deletion.get("verified") is True:
            return {
                "status": "not_initialized",
                "target_count": 0,
                "after_count": 0,
                "verified": True,
                "owner": "wiki_metrics_lifecycle",
            }
        if wiki_status not in {"applied", "existing", "no_targets"}:
            return {
                "status": "blocked",
                "target_count": 0,
                "after_count": None,
                "verified": False,
                "owner": "wiki_metrics_lifecycle",
                "error": "wiki_subject_deletion_not_terminal",
            }
        if wiki_deletion.get("verified") is not True:
            return {
                "status": "pending_consumer_receipts",
                "target_count": int(wiki_deletion.get("receipt_count") or 0),
                "after_count": None,
                "verified": False,
                "owner": "wiki_metrics_lifecycle",
            }
        projection_db = _configured_wiki_projection_db(self.config)
        if not projection_db.is_file():
            return {
                "status": "blocked",
                "target_count": 0,
                "after_count": None,
                "verified": False,
                "owner": "wiki_metrics_lifecycle",
                "error": "wiki_projection_ledger_missing_for_evidence_after_oracle",
            }
        wiki_dir = _vault_dir(self.config, "mnemos")
        if wiki_dir is None:
            return {
                "status": "blocked",
                "target_count": 0,
                "after_count": None,
                "verified": False,
                "owner": "wiki_metrics_lifecycle",
                "error": "wiki_vault_missing_for_evidence_after_oracle",
            }
        try:
            from core.privacy.wiki_subject_deletion import subject_scope_hash
            from core.wiki_metrics_lifecycle import path_candidates
            from core.wiki_projection_lifecycle import WikiProjectionLedger

            ledger = WikiProjectionLedger(projection_db)
            receipts = ledger.subject_deletion_receipts_for_scope(
                scope_kind=subject.scope_kind,
                scope_value_hash=subject_scope_hash(
                    subject.scope_kind,
                    subject.scope_value,
                ),
            )
            if not receipts:
                return {
                    "status": "no_targets",
                    "target_count": 0,
                    "after_count": 0,
                    "verified": True,
                    "owner": "wiki_metrics_lifecycle",
                }
            candidates = sorted(
                {
                    candidate
                    for receipt in receipts
                    for candidate in path_candidates(
                        Path(wiki_dir),
                        str(receipt["page_path"]),
                    )
                    if candidate
                }
            )
            metrics_db = _configured_wiki_metrics_db(self.config)
            if not metrics_db.is_file():
                return {
                    "status": "blocked",
                    "target_count": len(receipts),
                    "after_count": None,
                    "verified": False,
                    "owner": "wiki_metrics_lifecycle",
                    "error": "wiki_metrics_after_oracle_unavailable",
                }
            with sqlite3.connect(
                metrics_db.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=5,
            ) as conn:
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='page_metrics'"
                ).fetchone()
                if table is None:
                    return {
                        "status": "blocked",
                        "target_count": len(receipts),
                        "after_count": None,
                        "verified": False,
                        "owner": "wiki_metrics_lifecycle",
                        "error": "wiki_metrics_schema_missing",
                    }
                placeholders = ",".join("?" for _ in candidates)
                after_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM page_metrics "
                        f"WHERE wiki_path IN ({placeholders})",  # nosec B608
                        tuple(candidates),
                    ).fetchone()[0]
                    or 0
                )
        except (OSError, PermissionError, RuntimeError, ValueError, sqlite3.Error):
            return {
                "status": "blocked",
                "target_count": int(wiki_deletion.get("receipt_count") or 0),
                "after_count": None,
                "verified": False,
                "owner": "wiki_metrics_lifecycle",
                "error": "wiki_metrics_after_oracle_failed",
            }
        return {
            "status": "applied" if wiki_status == "applied" else wiki_status,
            "target_count": len(receipts),
            "after_count": after_count,
            "verified": after_count == 0,
            "owner": "wiki_metrics_lifecycle",
        }
