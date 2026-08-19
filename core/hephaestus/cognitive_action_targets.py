"""Real target-service effects for admitted distill cognitive actions.

Each adapter calls the existing Observation, Reflection, PolicyPatch, or
Knowledge Graph write service.  A small reciprocal ledger lives in the target
database itself, so ``distill_actions.db`` cannot self-sign an effect.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from core.cognitive.observation_calibration_schema import CALIBRATION_COLUMN_DDL
from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionRequest,
    MaterialActionTerminal,
    require_material_action,
    resolve_material_action_recovery_authorization,
)
from core.hephaestus.distill_action_store import (
    ARTIFACT_SCHEMA_VERSION,
    CognitiveEffectCommit,
    canonical_json,
    now_utc,
    sha256_json,
    stable_id,
)
from core.privacy.content_redaction import REDACTION_POLICY, redact_persistence_value


TARGET_RECEIPT_SCHEMA_VERSION = "mnemos.cognitive_action_target_receipt.v1"
LEGACY_TARGET_STATE_HASH_CONTRACT_VERSION = "mnemos.cognitive_action_target_state.v1"
INTERMEDIATE_TARGET_STATE_HASH_CONTRACT_VERSION = (
    "mnemos.cognitive_action_target_state.v2"
)
TARGET_STATE_HASH_CONTRACT_VERSION = "mnemos.cognitive_action_target_state.v3"
ABSENT_TARGET_HASH = sha256_json({"target_state": "absent"})
OBSERVATION_CALIBRATION_OWNED_FIELDS = frozenset(CALIBRATION_COLUMN_DDL)
OBSERVATION_ACTION_OWNED_FIELDS = (
    "id",
    "dimension",
    "observation_type",
    "value",
    "unit",
    "source_type",
    "source_path",
    "source_id",
    "evidence",
    "observed_at",
    "period_start",
    "period_end",
    "content_source",
    "user_intent_signal",
    "created_at",
)
OBSERVATION_JSON_FIELDS = frozenset({"value", "evidence"})
OBSERVATION_TIMESTAMP_FIELDS = frozenset(
    {"observed_at", "period_start", "period_end", "created_at"}
)
DISTILL_TARGET_OWNER = "distill_cognitive_action_target"
DISTILL_TARGET_EXECUTOR = "distill_cognitive_action_dispatcher"
DISTILL_TARGET_ACTION = "apply_distill_cognitive_action"


class CognitiveActionTargetError(RuntimeError):
    """A target service rejected or failed to durably commit an effect."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class _TargetPlan:
    target: str
    db_path: Path
    object_id: str
    effect_id: str
    expected_delta_hash: str
    locator: Mapping[str, Any]
    read_state: Callable[[], Mapping[str, Any] | None]
    owned_by_action: Callable[[Mapping[str, Any]], bool]
    initialize_service: Callable[[], None]
    apply_service: Callable[[], None]
    nested_material_actions: tuple[MaterialActionRequest, ...] = ()


TARGET_RECEIPT_SQL = """
CREATE TABLE IF NOT EXISTS cognitive_action_target_effect_intents (
    effect_id TEXT PRIMARY KEY,
    cognitive_action_id TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    target_object_id TEXT NOT NULL,
    before_hash TEXT NOT NULL,
    expected_delta_hash TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cognitive_action_target_receipts (
    effect_id TEXT PRIMARY KEY,
    cognitive_action_id TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    target_object_id TEXT NOT NULL,
    before_hash TEXT NOT NULL,
    after_hash TEXT NOT NULL,
    expected_delta_hash TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
"""


class CognitiveActionTargetDispatcher:
    """Dispatch one validated artifact to its sole canonical target service."""

    def __init__(self, *, database_dir: Path, wiki_dir: Path | None = None):
        self.database_dir = Path(database_dir)
        self.wiki_dir = Path(wiki_dir) if wiki_dir else self.database_dir.parent / "wiki"

    def apply(
        self,
        row: Mapping[str, Any],
        artifact: Mapping[str, Any],
    ) -> CognitiveEffectCommit:
        return self.apply_prepared(row, artifact, self.prepare(row, artifact))

    def prepare(
        self,
        row: Mapping[str, Any],
        artifact: Mapping[str, Any],
    ) -> _TargetPlan:
        """Derive the immutable target plan without executing any target effect."""

        action = str(row.get("cognitive_action") or "")
        if action in {"create_observation", "record_reinforcement"}:
            return self._observation_plan(row, artifact)
        elif action == "create_reflection_seed":
            return self._reflection_plan(row, artifact)
        elif action in {
            "propose_policy_patch",
            "propose_methodology",
            "propose_pitfall_pattern",
        }:
            return self._policy_patch_plan(row, artifact)
        elif action == "update_relation":
            return self._relation_plan(row, artifact)
        raise CognitiveActionTargetError(
            f"unsupported cognitive action: {action}",
            retryable=False,
        )

    def material_action_requests(
        self,
        row: Mapping[str, Any],
        plan: _TargetPlan,
    ) -> tuple[MaterialActionRequest, ...]:
        """Return the complete exact action set implied by one frozen plan."""

        action_id = str(row["cognitive_action_id"])
        artifact_hash = str(row["artifact_hash"])
        target_ref = f"distill-target:{action_id}:{plan.target}:{plan.object_id}"
        input_hash = sha256_json(
            {
                "schema_version": "mnemos.distill_target_material_input.v1",
                "cognitive_action_id": action_id,
                "cognitive_action": str(row["cognitive_action"]),
                "artifact_hash": artifact_hash,
                "target": plan.target,
                "target_object_id": plan.object_id,
                "expected_delta_hash": plan.expected_delta_hash,
            }
        )
        state_db = str(self.database_dir / "producer_consumer_ledger.db")
        outer = MaterialActionRequest(
            owner=DISTILL_TARGET_OWNER,
            executor_id=DISTILL_TARGET_EXECUTOR,
            action_type=DISTILL_TARGET_ACTION,
            target_ref=target_ref,
            input_hash=input_hash,
            expected_state_db=state_db,
        )
        return (outer, *plan.nested_material_actions)

    def apply_prepared(
        self,
        row: Mapping[str, Any],
        artifact: Mapping[str, Any],
        plan: _TargetPlan,
    ) -> CognitiveEffectCommit:
        """Execute the exact plan that was frozen into the decision source facts."""

        return self._execute_plan(row, artifact, plan)

    def _execute_plan(
        self,
        row: Mapping[str, Any],
        artifact: Mapping[str, Any],
        plan: _TargetPlan,
    ) -> CognitiveEffectCommit:
        action_id = str(row["cognitive_action_id"])
        artifact_hash = str(row["artifact_hash"])
        material_request = self.material_action_requests(row, plan)[0]
        authorization, permit = resolve_material_action_recovery_authorization(
            None,
            owner=material_request.owner,
            executor_id=material_request.executor_id,
            action_type=material_request.action_type,
            target_ref=material_request.target_ref,
            input_hash=material_request.input_hash,
            expected_state_db=self.database_dir / "producer_consumer_ledger.db",
        )
        existing = _existing_target_receipt(
            plan.db_path,
            row=row,
            plan=plan,
            artifact_hash=artifact_hash,
        )
        if existing is not None:
            self._record_material_terminal(
                authorization,
                permit.effect_id,
                existing,
            )
            return existing

        permit = require_material_action(
            authorization,
            owner=material_request.owner,
            executor_id=material_request.executor_id,
            action_type=material_request.action_type,
            target_ref=material_request.target_ref,
            input_hash=material_request.input_hash,
            expected_state_db=self.database_dir / "producer_consumer_ledger.db",
        )
        plan.db_path.parent.mkdir(parents=True, exist_ok=True)
        plan.initialize_service()

        current = plan.read_state()
        current_hash = target_state_hash(plan.target, current)
        with _connect(plan.db_path) as conn:
            _ensure_target_receipt_schema(conn)
            conn.execute(
                """
                INSERT INTO cognitive_action_target_effect_intents (
                    effect_id, cognitive_action_id, action, target,
                    target_object_id, before_hash, expected_delta_hash,
                    artifact_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(effect_id) DO NOTHING
                """,
                (
                    plan.effect_id,
                    action_id,
                    str(row["cognitive_action"]),
                    plan.target,
                    plan.object_id,
                    current_hash,
                    plan.expected_delta_hash,
                    artifact_hash,
                    now_utc(),
                ),
            )
            intent = conn.execute(
                "SELECT * FROM cognitive_action_target_effect_intents WHERE effect_id=?",
                (plan.effect_id,),
            ).fetchone()
            conn.commit()
        if intent is None:
            raise CognitiveActionTargetError("target effect intent was not persisted")
        before_hash = str(intent["before_hash"])

        # Crash recovery: the target service committed after the intent but
        # before its reciprocal receipt.  Prove ownership and finalize without
        # applying the effect a second time.
        current = plan.read_state()
        current_hash = target_state_hash(plan.target, current)
        if current_hash != before_hash:
            if current is None or not plan.owned_by_action(current):
                raise CognitiveActionTargetError(
                    "target changed after intent without matching action ownership",
                    retryable=False,
                )
            committed = self._commit_target_receipt(
                row=row,
                artifact=artifact,
                plan=plan,
                before_hash=before_hash,
                after_hash=current_hash,
            )
            self._record_material_terminal(
                authorization,
                permit.effect_id,
                committed,
            )
            return committed

        try:
            plan.apply_service()
        except CognitiveActionTargetError:
            raise
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RuntimeError) as exc:
            raise CognitiveActionTargetError(
                f"{plan.target} service failed: {type(exc).__name__}: {exc}"
            ) from exc

        after = plan.read_state()
        after_hash = target_state_hash(plan.target, after)
        if after is None or not plan.owned_by_action(after):
            raise CognitiveActionTargetError(
                f"{plan.target} service returned without an owned target object"
            )
        if after_hash == before_hash:
            raise CognitiveActionTargetError(
                f"{plan.target} service committed no observable delta",
                retryable=False,
            )
        committed = self._commit_target_receipt(
            row=row,
            artifact=artifact,
            plan=plan,
            before_hash=before_hash,
            after_hash=after_hash,
        )
        self._record_material_terminal(
            authorization,
            permit.effect_id,
            committed,
        )
        return committed

    @staticmethod
    def _record_material_terminal(
        authorization: MaterialActionAuthorization,
        material_effect_id: str,
        committed: CognitiveEffectCommit,
    ) -> None:
        authorization.record_terminal(
            MaterialActionTerminal(
                status="committed",
                target_effect_id=material_effect_id,
                before_hash=committed.before_hash,
                after_hash=committed.after_hash,
                evidence_refs=(
                    f"material-command:{authorization.permit.command_id}",
                    (
                        "decision-revision:"
                        f"{authorization.permit.decision_revision_id}"
                    ),
                    f"material-effect:{material_effect_id}",
                    f"target-after:{committed.after_hash}",
                    (
                        "target-journal:distill-cognitive-action:"
                        f"{committed.effect_id}:{committed.after_hash}"
                    ),
                ),
                outcome="distillation cognitive action target committed",
                created_at=committed.committed_at,
            )
        )

    @staticmethod
    def _commit_target_receipt(
        *,
        row: Mapping[str, Any],
        artifact: Mapping[str, Any],
        plan: _TargetPlan,
        before_hash: str,
        after_hash: str,
    ) -> CognitiveEffectCommit:
        committed_at = now_utc()
        detail = {
            "artifact_schema_version": artifact.get("schema_version"),
            "episode_id": artifact.get("episode_id"),
            "fragment_ids": artifact.get("fragment_ids"),
            "target_locator": dict(plan.locator),
            "target_state_hash_contract": TARGET_STATE_HASH_CONTRACT_VERSION,
        }
        with _connect(plan.db_path) as conn:
            _ensure_target_receipt_schema(conn)
            conn.execute(
                """
                INSERT INTO cognitive_action_target_receipts (
                    effect_id, cognitive_action_id, action, target,
                    target_object_id, before_hash, after_hash,
                    expected_delta_hash, artifact_hash, committed_at,
                    schema_version, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(effect_id) DO NOTHING
                """,
                (
                    plan.effect_id,
                    str(row["cognitive_action_id"]),
                    str(row["cognitive_action"]),
                    plan.target,
                    plan.object_id,
                    before_hash,
                    after_hash,
                    plan.expected_delta_hash,
                    str(row["artifact_hash"]),
                    committed_at,
                    TARGET_RECEIPT_SCHEMA_VERSION,
                    canonical_json(detail),
                ),
            )
            stored = conn.execute(
                "SELECT * FROM cognitive_action_target_receipts WHERE effect_id=?",
                (plan.effect_id,),
            ).fetchone()
            conn.commit()
        if stored is None:
            raise CognitiveActionTargetError("target reciprocal receipt was not persisted")
        return _receipt_from_row(stored, plan.db_path)

    def _observation_plan(
        self,
        row: Mapping[str, Any],
        artifact: Mapping[str, Any],
    ) -> _TargetPlan:
        from core.cognitive.observation_store import ObservationStore

        action_id = str(row["cognitive_action_id"])
        observation = _observation_from_action(row, artifact)
        object_id = observation.id
        db_path = self.database_dir / "observations.db"
        expected_owned_state = expected_action_owned_target_state(
            "observation_store",
            row,
            artifact,
        )

        def read_state() -> Mapping[str, Any] | None:
            if not db_path.exists():
                return None
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                try:
                    found = conn.execute(
                        "SELECT * FROM observations WHERE id=?", (object_id,)
                    ).fetchone()
                except sqlite3.Error:
                    return None
            return dict(found) if found else None

        def apply_service() -> None:
            outcome = ObservationStore(str(db_path)).save(observation)
            if outcome not in {"inserted", "updated", "unchanged"}:
                raise CognitiveActionTargetError(f"unexpected ObservationStore result: {outcome}")

        def initialize_service() -> None:
            """Initialize the canonical observation store before receipt DDL."""

            ObservationStore(str(db_path))

        return _TargetPlan(
            target="observation_store",
            db_path=db_path,
            object_id=object_id,
            effect_id=stable_id("effect", action_id, "observation_store"),
            expected_delta_hash=sha256_json(observation.to_dict()),
            locator={"table": "observations", "id": object_id},
            read_state=read_state,
            owned_by_action=lambda state: (
                project_action_owned_target_state("observation_store", state)
                == expected_owned_state
            ),
            initialize_service=initialize_service,
            apply_service=apply_service,
        )

    def _reflection_plan(
        self,
        row: Mapping[str, Any],
        artifact: Mapping[str, Any],
    ) -> _TargetPlan:
        from core.reflection.models import (
            InsightSnapshot,
            ReflectionRecord,
        )
        from core.reflection.reflection_store import ReflectionStore

        action_id = str(row["cognitive_action_id"])
        claim = _claim(artifact)
        object_id = stable_id("refl", action_id, size=16)
        db_path = self.database_dir / "reflections.db"
        timestamp = _parse_datetime(str(artifact.get("created_at") or ""))
        dimension = _observation_dimension(str(claim.get("claim_type") or "")).value
        record = ReflectionRecord(
            id=object_id,
            created_at=timestamp,
            trigger=_reflection_trigger(str(claim.get("claim_type") or "")),
            trigger_event=str(claim.get("claim_text") or ""),
            user_query=str(claim.get("claim_text") or ""),
            mirror_dimensions=[dimension],
            insight=InsightSnapshot(
                summary=str(claim.get("claim_text") or ""),
                key_points=[str(claim.get("claim_text") or "")],
                dimensions_involved=[dimension],
            ),
            temporal_context={
                "source": "distill_cognitive_action",
                "source_event_ids": list(artifact.get("source_event_ids") or []),
            },
            internal_validation={
                "cognitive_action_id": action_id,
                "artifact_hash": str(row["artifact_hash"]),
            },
        )

        def read_state() -> Mapping[str, Any] | None:
            if not db_path.exists():
                return None
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                try:
                    found = conn.execute(
                        "SELECT * FROM reflection_records WHERE id=?", (object_id,)
                    ).fetchone()
                except sqlite3.Error:
                    return None
            return dict(found) if found else None

        def owned(state: Mapping[str, Any]) -> bool:
            try:
                validation = json.loads(str(state.get("internal_validation") or "{}"))
            except json.JSONDecodeError:
                return False
            return str(validation.get("cognitive_action_id") or "") == action_id

        def initialize_service() -> None:
            """Initialize the canonical reflection store before receipt DDL."""

            ReflectionStore(str(db_path))

        def apply_service() -> None:
            ReflectionStore(str(db_path)).save_record(record)

        return _TargetPlan(
            target="reflection_store",
            db_path=db_path,
            object_id=object_id,
            effect_id=stable_id("effect", action_id, "reflection_store"),
            expected_delta_hash=sha256_json(record.to_dict()),
            locator={"table": "reflection_records", "id": object_id},
            read_state=read_state,
            owned_by_action=owned,
            initialize_service=initialize_service,
            apply_service=apply_service,
        )

    def _policy_patch_plan(
        self,
        row: Mapping[str, Any],
        artifact: Mapping[str, Any],
    ) -> _TargetPlan:
        from core.cognitive.policy_patch import (
            POLICY_PATCH_EXECUTOR,
            POLICY_PATCH_OWNER,
            POLICY_PATCH_PROPOSE_ACTION,
            PolicyPatchOptions,
            PolicyPatchStore,
            policy_patch_proposal_binding,
            policy_patch_id,
        )

        action_id = str(row["cognitive_action_id"])
        action = str(row["cognitive_action"])
        claim = _claim(artifact)
        db_path = self.database_dir / "policy_patches.db"
        scope_data = claim.get("scope")
        scope_data = scope_data if isinstance(scope_data, Mapping) else {}
        domain = str(scope_data.get("domain") or "general")
        created = _parse_datetime(str(artifact.get("created_at") or ""))
        lesson = {
            "source_type": "distill_cognitive_action",
            "source_id": action_id,
            "task_type": domain,
            "subtype": action.removeprefix("propose_"),
            "scope": domain,
            "severity": "high" if action == "propose_pitfall_pattern" else "medium",
            "content": str(claim.get("claim_text") or ""),
            "trigger": domain,
            "confidence": float(claim.get("confidence") or 0.0),
            "evidence_refs": list(artifact.get("evidence_refs") or []),
            "expires_at": (created + timedelta(days=90)).isoformat(timespec="seconds"),
            "metadata": {
                "cognitive_action_id": action_id,
                "artifact_hash": str(row["artifact_hash"]),
            },
        }
        options = PolicyPatchOptions(
            database_dir=self.database_dir,
            db_path=db_path,
            enabled=True,
            ttl_days=90,
            min_confidence=0.0,
            max_active=5,
        )
        object_id = policy_patch_id(
            "distill_cognitive_action",
            action_id,
            domain,
            action.removeprefix("propose_"),
            str(claim.get("claim_text") or ""),
        )

        def read_state() -> Mapping[str, Any] | None:
            if not db_path.exists():
                return None
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                try:
                    found = conn.execute(
                        "SELECT * FROM policy_patches WHERE patch_id=?",
                        (object_id,),
                    ).fetchone()
                except sqlite3.Error:
                    return None
            return dict(found) if found else None

        def apply_service() -> None:
            store = PolicyPatchStore(options=options)
            if store.propose(lesson) is None:
                raise CognitiveActionTargetError("PolicyPatchStore did not return a patch")

        def initialize_service() -> None:
            """Initialize the policy-patch owner before reciprocal receipt DDL."""

            PolicyPatchStore(options=options)

        def owned(state: Mapping[str, Any]) -> bool:
            try:
                metadata = json.loads(str(state.get("metadata_json") or "{}"))
            except json.JSONDecodeError:
                return False
            return str(metadata.get("cognitive_action_id") or "") == action_id

        nested_binding = policy_patch_proposal_binding(lesson, options)
        if nested_binding is None:
            raise CognitiveActionTargetError(
                "policy patch plan is not eligible for its canonical sink",
                retryable=False,
            )
        return _TargetPlan(
            target="policy_patch_store",
            db_path=db_path,
            object_id=object_id,
            effect_id=stable_id("effect", action_id, "policy_patch_store"),
            expected_delta_hash=sha256_json(lesson),
            locator={"table": "policy_patches", "id": object_id},
            read_state=read_state,
            owned_by_action=owned,
            initialize_service=initialize_service,
            apply_service=apply_service,
            nested_material_actions=(
                MaterialActionRequest(
                    owner=POLICY_PATCH_OWNER,
                    executor_id=POLICY_PATCH_EXECUTOR,
                    action_type=POLICY_PATCH_PROPOSE_ACTION,
                    target_ref=nested_binding["target_ref"],
                    input_hash=nested_binding["input_hash"],
                    expected_state_db=str(
                        self.database_dir / "producer_consumer_ledger.db"
                    ),
                ),
            ),
        )

    def _relation_plan(
        self,
        row: Mapping[str, Any],
        artifact: Mapping[str, Any],
    ) -> _TargetPlan:
        from core.kia.relation_manager import (
            KG_RELATION_ACTION,
            KG_RELATION_EXECUTOR,
            KG_RELATION_OWNER,
            RelationManager,
        )
        from core.kia.relation_schema import Relation, RelationEvidence, RelationType

        action_id = str(row["cognitive_action_id"])
        claim = _claim(artifact)
        relation = claim.get("relation_to_existing")
        relation = relation if isinstance(relation, Mapping) else {}
        parent_targets = [
            str(value) for value in artifact.get("parent_target_pages") or [] if value
        ]
        existing_targets = [str(value) for value in relation.get("target_pages") or [] if value]
        source = (
            parent_targets[0]
            if parent_targets
            else f"distill:{artifact.get('session_id')}:{claim.get('claim_id')}"
        )
        target = existing_targets[0] if existing_targets else "mnemos:cognitive-relations"
        relation_type = _relation_type(str(relation.get("type") or "related"))
        reason = str(
            relation.get("reason")
            or relation.get("delta_text")
            or claim.get("claim_text")
            or ""
        )
        reason = f"[cognitive_action:{action_id}] {reason}"
        db_path = self.database_dir / "knowledge_graph.db"
        object_id = stable_id("kgrel", source, target, relation_type, size=20)
        planned_relation = Relation(
            source=source,
            target=target,
            relation_type=RelationType(relation_type),
            confidence=float(claim.get("confidence") or 0.0),
            source_method="distill",
            evidence=[
                RelationEvidence(
                    evidence_type="distill_extraction",
                    content=reason,
                )
            ],
        )
        nested_binding = RelationManager.relation_material_action_binding(
            planned_relation,
            reason="relation_manager.add_from_distill",
        )

        def read_state() -> Mapping[str, Any] | None:
            if not db_path.exists():
                return None
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                try:
                    rel = conn.execute(
                        """
                        SELECT * FROM relations
                        WHERE source=? AND target=? AND relation_type=?
                        """,
                        (source, target, relation_type),
                    ).fetchone()
                except sqlite3.Error:
                    return None
                if rel is None:
                    return None
                evidence = conn.execute(
                    """
                    SELECT evidence_type, content FROM relation_evidence
                    WHERE relation_id=? ORDER BY id
                    """,
                    (int(rel["id"]),),
                ).fetchall()
            payload = dict(rel)
            payload["evidence"] = [dict(item) for item in evidence]
            return payload

        def apply_service() -> None:
            manager = RelationManager(str(db_path))
            created = manager.add_from_distill(
                {
                    "relations": [
                        {
                            "source": source,
                            "target": target,
                            "type": relation_type,
                            "confidence": float(claim.get("confidence") or 0.0),
                            "reason": reason,
                        }
                    ]
                }
            )
            if len(created) != 1:
                raise CognitiveActionTargetError(
                    "RelationManager did not commit the requested relation"
                )

        def initialize_service() -> None:
            """Initialize the relation owner before reciprocal receipt DDL."""

            RelationManager(str(db_path))

        return _TargetPlan(
            target="knowledge_graph",
            db_path=db_path,
            object_id=object_id,
            effect_id=stable_id("effect", action_id, "knowledge_graph"),
            expected_delta_hash=sha256_json(
                {
                    "source": source,
                    "target": target,
                    "relation_type": relation_type,
                    "evidence": reason,
                }
            ),
            locator={
                "table": "relations",
                "source": source,
                "target": target,
                "relation_type": relation_type,
            },
            read_state=read_state,
            owned_by_action=lambda state: any(
                f"[cognitive_action:{action_id}]" in str(item.get("content") or "")
                for item in state.get("evidence", [])
                if isinstance(item, Mapping)
            ),
            initialize_service=initialize_service,
            apply_service=apply_service,
            nested_material_actions=(
                MaterialActionRequest(
                    owner=KG_RELATION_OWNER,
                    executor_id=KG_RELATION_EXECUTOR,
                    action_type=KG_RELATION_ACTION,
                    target_ref=nested_binding["target_ref"],
                    input_hash=nested_binding["input_hash"],
                    expected_state_db=str(
                        self.database_dir / "producer_consumer_ledger.db"
                    ),
                ),
            ),
        )


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_target_receipt_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(TARGET_RECEIPT_SQL)


def _existing_target_receipt(
    db_path: Path,
    *,
    row: Mapping[str, Any],
    plan: _TargetPlan,
    artifact_hash: str,
) -> CognitiveEffectCommit | None:
    """Read an exact target receipt without creating or migrating its store."""

    if not db_path.is_file():
        return None
    with sqlite3.connect(
        f"file:{db_path.resolve(strict=True)}?mode=ro",
        uri=True,
    ) as conn:
        conn.row_factory = sqlite3.Row
        table = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='cognitive_action_target_receipts'"""
        ).fetchone()
        if table is None:
            return None
        receipt_row = conn.execute(
            "SELECT * FROM cognitive_action_target_receipts WHERE effect_id=?",
            (plan.effect_id,),
        ).fetchone()
    if receipt_row is None:
        return None
    expected = {
        "effect_id": plan.effect_id,
        "cognitive_action_id": str(row["cognitive_action_id"]),
        "action": str(row["cognitive_action"]),
        "target": plan.target,
        "target_object_id": plan.object_id,
        "expected_delta_hash": plan.expected_delta_hash,
        "artifact_hash": artifact_hash,
        "schema_version": TARGET_RECEIPT_SCHEMA_VERSION,
    }
    stored = dict(receipt_row)
    if any(str(stored.get(key) or "") != value for key, value in expected.items()):
        raise CognitiveActionTargetError(
            "existing target receipt does not match its frozen action plan",
            retryable=False,
        )
    if str(stored.get("before_hash") or "") == str(
        stored.get("after_hash") or ""
    ):
        raise CognitiveActionTargetError(
            "existing target receipt contains no observable delta",
            retryable=False,
        )
    return _receipt_from_row(receipt_row, db_path)


def _receipt_from_row(row: sqlite3.Row, db_path: Path) -> CognitiveEffectCommit:
    try:
        detail = json.loads(str(row["detail"] or "{}"))
    except json.JSONDecodeError:
        detail = {}
    effect_id = str(row["effect_id"])
    return CognitiveEffectCommit(
        effect_id=effect_id,
        target=str(row["target"]),
        target_object_id=str(row["target_object_id"]),
        before_hash=str(row["before_hash"]),
        after_hash=str(row["after_hash"]),
        expected_delta_hash=str(row["expected_delta_hash"]),
        reciprocal_receipt=(
            f"{db_path.name}:cognitive_action_target_receipts:{effect_id}"
        ),
        receipt_db_path=str(db_path),
        committed_at=str(row["committed_at"]),
        detail=detail if isinstance(detail, Mapping) else {},
    )


def project_action_owned_target_state(
    target: str,
    value: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Project one target row onto fields owned by the cognitive action.

    Calibration owns its binding columns on an Observation independently of the
    distillation action that created the row.  Those columns may therefore
    change without invalidating the action's durable target-effect receipt.
    """

    if value is None:
        return None
    if target != "observation_store":
        return dict(value)
    missing = [field for field in OBSERVATION_ACTION_OWNED_FIELDS if field not in value]
    if missing:
        raise CognitiveActionTargetError(
            "observation target state is missing action-owned fields: "
            + ", ".join(missing),
            retryable=False,
        )
    projected: dict[str, Any] = {}
    for field in OBSERVATION_ACTION_OWNED_FIELDS:
        field_value = value[field]
        if field in OBSERVATION_JSON_FIELDS and isinstance(field_value, str):
            try:
                field_value = json.loads(field_value)
            except json.JSONDecodeError as exc:
                raise CognitiveActionTargetError(
                    f"observation {field} is not valid JSON",
                    retryable=False,
                ) from exc
        if field in OBSERVATION_TIMESTAMP_FIELDS:
            field_value = _canonical_action_timestamp(field, field_value)
        projected[field] = field_value
    return projected


def _canonical_action_timestamp(field: str, value: Any) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if field == "created_at":
            raise CognitiveActionTargetError(
                "observation created_at is empty",
                retryable=False,
            )
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise CognitiveActionTargetError(
                f"observation {field} is not a valid ISO timestamp",
                retryable=False,
            ) from exc
    else:
        raise CognitiveActionTargetError(
            f"observation {field} is not a timestamp",
            retryable=False,
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _project_intermediate_target_state(
    target: str,
    value: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    projected = dict(value)
    if target == "observation_store":
        for field in OBSERVATION_CALIBRATION_OWNED_FIELDS:
            projected.pop(field, None)
    return projected


def target_state_hash(target: str, value: Mapping[str, Any] | None) -> str:
    projected = project_action_owned_target_state(target, value)
    return str(ABSENT_TARGET_HASH if projected is None else sha256_json(projected))


def target_state_hash_for_contract(
    target: str,
    value: Mapping[str, Any] | None,
    contract_version: str,
) -> str:
    """Hash a target under the exact contract recorded by its receipt."""

    if contract_version == TARGET_STATE_HASH_CONTRACT_VERSION:
        return target_state_hash(target, value)
    if contract_version == INTERMEDIATE_TARGET_STATE_HASH_CONTRACT_VERSION:
        projected = _project_intermediate_target_state(target, value)
    elif contract_version in {"", LEGACY_TARGET_STATE_HASH_CONTRACT_VERSION}:
        projected = None if value is None else dict(value)
    else:
        raise CognitiveActionTargetError(
            f"unknown target-state hash contract: {contract_version}",
            retryable=False,
        )
    return str(ABSENT_TARGET_HASH if projected is None else sha256_json(projected))


def _observation_from_action(
    row: Mapping[str, Any],
    artifact: Mapping[str, Any],
):
    from core.cognitive.models import Observation, ObservationType, SourceType
    from core.cognitive.sources import ContentSource, UserIntent

    action_id = str(row["cognitive_action_id"])
    claim = _claim(artifact)
    action = str(row["cognitive_action"])
    behavior = artifact.get("user_behavior_intent")
    behavior = behavior if isinstance(behavior, Mapping) else {}
    try:
        content_source = ContentSource(str(behavior.get("content_source") or "unknown"))
    except ValueError:
        content_source = ContentSource.UNKNOWN
    try:
        user_intent = UserIntent(str(behavior.get("user_intent_signal") or "unknown"))
    except ValueError:
        user_intent = UserIntent.UNKNOWN
    timestamp = _parse_datetime(str(artifact.get("created_at") or ""))
    return Observation(
        id=stable_id("obs", action_id, size=16),
        dimension=_observation_dimension(str(claim.get("claim_type") or "")),
        observation_type=ObservationType.PATTERN,
        value={
            "cognitive_action_id": action_id,
            "signal_kind": "reinforcement" if action == "record_reinforcement" else "claim",
            "claim_text": str(claim.get("claim_text") or ""),
            "claim_type": str(claim.get("claim_type") or ""),
            "scope": dict(claim.get("scope") or {}),
        },
        confidence=float(claim.get("confidence") or 0.0),
        source_type=SourceType.RAW,
        source_path=f"distill_action:{action_id}",
        source_id=action_id,
        evidence=_evidence_quotes(claim),
        observed_at=timestamp,
        content_source=content_source,
        user_intent_signal=user_intent,
        created_at=timestamp,
        updated_at=timestamp,
    )


def expected_action_owned_target_state(
    target: str,
    row: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Reconstruct the exact current action-owned state from immutable inputs."""

    if target != "observation_store":
        raise CognitiveActionTargetError(
            f"semantic reconstruction is unsupported for target: {target}",
            retryable=False,
        )
    observation = _observation_from_action(row, artifact)
    redacted = redact_persistence_value(
        {
            "value": observation.value,
            "evidence": observation.evidence,
            "source_path": observation.source_path,
            "user_notes": observation.user_notes,
        }
    ).value
    if not isinstance(redacted, Mapping):
        raise CognitiveActionTargetError(
            "redacted observation action state is not an object",
            retryable=False,
        )
    persisted = {
        "id": observation.id,
        "dimension": observation.dimension.value,
        "observation_type": observation.observation_type.value,
        "value": redacted["value"],
        "unit": observation.unit,
        "source_type": observation.source_type.value,
        "source_path": str(redacted["source_path"]),
        "source_id": observation.source_id,
        "evidence": list(redacted["evidence"]),
        "observed_at": (
            observation.observed_at.isoformat() if observation.observed_at else None
        ),
        "period_start": (
            observation.period_start.isoformat() if observation.period_start else None
        ),
        "period_end": observation.period_end.isoformat() if observation.period_end else None,
        "content_source": observation.content_source.value,
        "user_intent_signal": observation.user_intent_signal.value,
        "created_at": observation.created_at.isoformat(),
    }
    projected = project_action_owned_target_state(target, persisted)
    if projected is None:
        raise CognitiveActionTargetError(
            "expected observation action state is absent",
            retryable=False,
        )
    return projected


def validated_cognitive_action_artifact(
    row: Mapping[str, Any],
) -> dict[str, Any]:
    """Decode and verify the immutable command artifact and its fixed ACL."""

    try:
        artifact = json.loads(str(row.get("artifact_payload") or ""))
        fragment_ids = json.loads(str(row.get("fragment_ids") or "[]"))
        acl = json.loads(str(row.get("acl_payload") or "{}"))
    except (json.JSONDecodeError, TypeError) as exc:
        raise CognitiveActionTargetError(
            "cognitive action artifact JSON is invalid",
            retryable=False,
        ) from exc
    if not isinstance(artifact, dict) or sha256_json(artifact) != row.get("artifact_hash"):
        raise CognitiveActionTargetError(
            "cognitive action artifact hash is invalid",
            retryable=False,
        )
    if artifact.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise CognitiveActionTargetError(
            "cognitive action artifact schema is invalid",
            retryable=False,
        )
    bindings = (
        "cognitive_action_id",
        "distill_action_id",
        "session_id",
        "claim_id",
        "cognitive_action",
        "episode_id",
        "input_spec_hash",
        "extraction_output_hash",
    )
    if any(str(artifact.get(key) or "") != str(row.get(key) or "") for key in bindings):
        raise CognitiveActionTargetError(
            "cognitive action artifact identity is invalid",
            retryable=False,
        )
    if artifact.get("fragment_ids") != fragment_ids or not fragment_ids:
        raise CognitiveActionTargetError(
            "cognitive action artifact fragments are invalid",
            retryable=False,
        )
    expected_acl = {
        "visibility": "private",
        "owner": "local_user",
        "redaction_policy": REDACTION_POLICY,
        "encryption": "none",
    }
    if acl != expected_acl or artifact.get("acl") != expected_acl:
        raise CognitiveActionTargetError(
            "cognitive action artifact ACL is invalid",
            retryable=False,
        )
    return artifact


def _claim(artifact: Mapping[str, Any]) -> Mapping[str, Any]:
    claim = artifact.get("claim")
    if not isinstance(claim, Mapping):
        raise CognitiveActionTargetError("artifact claim is missing", retryable=False)
    return claim


def _parse_datetime(value: str) -> datetime:
    text = value.strip()
    if not text:
        raise CognitiveActionTargetError(
            "cognitive action artifact created_at is empty",
            retryable=False,
        )
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CognitiveActionTargetError(
            "cognitive action artifact created_at is invalid",
            retryable=False,
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _evidence_quotes(claim: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for item in claim.get("evidence") or []:
        if isinstance(item, Mapping) and str(item.get("quote") or "").strip():
            result.append(str(item["quote"]).strip())
    return result or [str(claim.get("claim_text") or "")]


def _observation_dimension(claim_type: str):
    from core.cognitive.models import Dimension

    if claim_type in {"decision", "constraint", "preference"}:
        return Dimension.DECISIONS
    if claim_type == "relationship":
        return Dimension.RELATIONSHIPS
    if claim_type in {"procedure", "pattern", "anti_pattern"}:
        return Dimension.ACTIONS
    if claim_type == "meta":
        return Dimension.GROWTH
    return Dimension.ATTENTION


def _reflection_trigger(claim_type: str):
    from core.reflection.models import ReflectionTrigger

    if claim_type in {"decision", "constraint", "preference"}:
        return ReflectionTrigger.MAJOR_DECISION
    if claim_type in {"procedure", "pattern", "anti_pattern"}:
        return ReflectionTrigger.NEW_PROJECT
    if claim_type == "relationship":
        return ReflectionTrigger.RELATIONSHIP_CHANGE
    return ReflectionTrigger.MANUAL


def _relation_type(value: str) -> str:
    return {
        "new": "related_to",
        "same": "related_to",
        "extends": "extends",
        "refines": "builds_on",
        "specializes": "specializes",
        "example": "instance_of",
        "related": "related_to",
        "contradicts": "contradicts",
        "supersedes": "supercedes",
    }.get(value, "related_to")
