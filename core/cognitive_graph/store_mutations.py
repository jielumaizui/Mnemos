"""Material mutation methods for the cognitive graph store."""

from __future__ import annotations

from contextlib import AbstractContextManager
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from core.cognitive.access_control import cognitive_access_hash
from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    require_material_action,
    resolve_material_action_recovery_authorization,
)
from core.cognitive.material_effect_ledger import (
    record_target_effect,
    recover_pending_target_effects,
    recover_recorded_target_effect,
)
from core.cognitive.state_contract import sha256_json
from core.trust.formal_cognitive_mutation import FormalCognitiveMutationJournal

from .models import CanonicalNode, CognitiveRelation

from .store_contracts import (
    COGNITIVE_CANONICAL_NODE_ACTION,
    COGNITIVE_RELATION_ACTION,
    COGNITIVE_RELATION_DELETE_ACTION,
    COGNITIVE_RELATION_EXECUTOR,
    COGNITIVE_RELATION_OWNER,
    COGNITIVE_RELATION_STALE_ACTION,
    CognitiveGraphCanonicalNodeEffectOracle,
    CognitiveGraphRelationDeleteEffectOracle,
    CognitiveGraphRelationEffectOracle,
    CognitiveGraphRelationStaleEffectOracle,
    _now,
    _parse_graph_access,
    _relation_id,
    _strictest_graph_access,
    cognitive_canonical_node_material_action_binding,
    cognitive_relation_delete_material_action_binding,
    cognitive_relation_material_action_binding,
    cognitive_relation_stale_material_action_binding,
)


class CognitiveGraphMutationMixin:
    """Guarded relation and canonical-node mutation boundaries."""

    if TYPE_CHECKING:
        db_path: Path

        def _conn(self) -> AbstractContextManager[sqlite3.Connection]: ...

        def _assert_write_not_frozen(
            self,
            access_control: Mapping[str, Any],
        ) -> None: ...

        @staticmethod
        def _object_subject_deleted(
            conn: sqlite3.Connection,
            *,
            object_type: str,
            object_id: str,
        ) -> bool: ...

        @staticmethod
        def _canonical_name_from_urn(urn: str) -> str: ...

        @staticmethod
        def _row_to_relation(row: sqlite3.Row) -> CognitiveRelation: ...

        def get_relation(self, rel_id: str) -> CognitiveRelation | None: ...

        @staticmethod
        def _canonical_id(name: str) -> str: ...

        def get_canonical_node(self, canonical_id: str) -> CanonicalNode | None: ...

        @classmethod
        def _get_canonical_node_raw(
            cls,
            conn: sqlite3.Connection,
            canonical_id: str,
        ) -> CanonicalNode | None: ...

        @staticmethod
        def _row_to_canonical_node(row: sqlite3.Row) -> CanonicalNode: ...

    @classmethod
    def _relation_effect_hash_in_conn(
        cls,
        conn: sqlite3.Connection,
        relation_id: str,
        source: str,
        target: str,
    ) -> str:
        canonical_names = sorted(
            {
                name
                for name in (
                    cls._canonical_name_from_urn(source),
                    cls._canonical_name_from_urn(target),
                )
                if name
            }
        )
        relation = conn.execute(
            "SELECT * FROM cognitive_relations WHERE id=?",
            (relation_id,),
        ).fetchone()
        nodes = []
        for name in canonical_names:
            row = conn.execute(
                "SELECT * FROM canonical_nodes WHERE canonical_name=?",
                (name,),
            ).fetchone()
            nodes.append(dict(row) if row is not None else None)
        return sha256_json(
            {
                "relation": dict(relation) if relation is not None else None,
                "canonical_nodes": nodes,
            }
        )

    def _relation_effect_hash(
        self,
        relation_id: str,
        source: str,
        target: str,
    ) -> str:
        with self._conn() as conn:
            return self._relation_effect_hash_in_conn(
                conn,
                relation_id,
                source,
                target,
            )

    def _record_relation_upsert_projection(
        self,
        material_action: MaterialActionAuthorization,
        *,
        relation_id: str,
        source: str,
        target: str,
        relation_type: str,
        strength: float,
        confidence: float,
        source_layer: str,
        target_layer: str,
        candidate_access: Mapping[str, Any],
    ) -> None:
        permit = material_action.permit
        FormalCognitiveMutationJournal.for_database(self.db_path).record(
            asset_kind="cognitive_graph_relation",
            action=COGNITIVE_RELATION_ACTION,
            target_ref=relation_id,
            actor="system",
            decision=permit.decision_revision_id,
            reason="cognitive_graph.add_relation",
            evidence_refs=(
                f"material-command:{permit.command_id}",
                f"decision-revision:{permit.decision_revision_id}",
                f"material-effect:{permit.effect_id}",
                f"source:{source}",
                f"target:{target}",
                f"relation_type:{relation_type}",
            ),
            metadata={
                "access_control_hash": cognitive_access_hash(candidate_access),
                "strength": strength,
                "confidence": confidence,
                "source_layer": source_layer,
                "target_layer": target_layer,
            },
            material_action=material_action,
        )

    # ───────────────────────────────
    # 关系操作
    # ───────────────────────────────

    def add_relation(
        self,
        source: str,
        target: str,
        relation_type: str,
        strength: float = 0.5,
        confidence: float = 0.5,
        source_layer: str = "",
        target_layer: str = "",
        access_control: Mapping[str, Any] | None = None,
        material_action: MaterialActionAuthorization | None = None,
    ) -> CognitiveRelation:
        """Upsert one relation while preserving the strictest source ACL."""
        rel_id = _relation_id(source, target, relation_type)
        now = _now()
        candidate_access = _strictest_graph_access(
            [access_control] if access_control is not None else [],
            object_ref=f"relation:{rel_id}",
        )
        binding = cognitive_relation_material_action_binding(
            source=source,
            target=target,
            relation_type=relation_type,
            strength=strength,
            confidence=confidence,
            source_layer=source_layer,
            target_layer=target_layer,
            access_control=access_control,
        )
        recover_pending_target_effects(
            state_db_path=self.db_path.parent / "producer_consumer_ledger.db",
            oracle=CognitiveGraphRelationEffectOracle(self.db_path),
            target_ref=binding["target_ref"],
        )
        # A privacy freeze is a hard precondition for issuing a new material
        # decision.  Check it before an ambient resolver can seal a DecisionTrace;
        # an explicitly supplied authorization may still be recovering an effect
        # that committed before the freeze, so that path is handled below.
        if material_action is None:
            self._assert_write_not_frozen(candidate_access)
        material_action, permit = resolve_material_action_recovery_authorization(
            material_action,
            owner=COGNITIVE_RELATION_OWNER,
            executor_id=COGNITIVE_RELATION_EXECUTOR,
            action_type=COGNITIVE_RELATION_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=self.db_path.parent / "producer_consumer_ledger.db",
        )

        def record_projection() -> None:
            """Publish the relation receipt after its target-local effect."""

            self._record_relation_upsert_projection(
                material_action,
                relation_id=rel_id,
                source=source,
                target=target,
                relation_type=relation_type,
                strength=strength,
                confidence=confidence,
                source_layer=source_layer,
                target_layer=target_layer,
                candidate_access=candidate_access,
            )

        if recover_recorded_target_effect(
            material_action,
            CognitiveGraphRelationEffectOracle(self.db_path),
        ):
            with self._conn() as conn:
                existing = conn.execute(
                    "SELECT * FROM cognitive_relations WHERE id=?",
                    (rel_id,),
                ).fetchone()
            if existing is None:
                raise RuntimeError("recovered cognitive relation has no target row")
            record_projection()
            return self._row_to_relation(existing)
        permit = require_material_action(
            material_action,
            owner=COGNITIVE_RELATION_OWNER,
            executor_id=COGNITIVE_RELATION_EXECUTOR,
            action_type=COGNITIVE_RELATION_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=self.db_path.parent / "producer_consumer_ledger.db",
        )
        self._assert_write_not_frozen(candidate_access)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                before_hash = self._relation_effect_hash_in_conn(
                    conn,
                    rel_id,
                    source,
                    target,
                )
                if self._object_subject_deleted(
                    conn,
                    object_type="relation",
                    object_id=rel_id,
                ):
                    raise PermissionError(
                        "cognitive relation is subject-deleted and cannot be restored"
                    )
                existing = conn.execute(
                    "SELECT access_control FROM cognitive_relations WHERE id=?",
                    (rel_id,),
                ).fetchone()
                effective_access = (
                    _strictest_graph_access(
                        [
                            _parse_graph_access(
                                existing["access_control"],
                                f"relation:{rel_id}",
                            ),
                            candidate_access,
                        ],
                        object_ref=f"relation:{rel_id}",
                    )
                    if existing is not None
                    else candidate_access
                )
                conn.execute(
                    """INSERT INTO cognitive_relations
                       (id, source, target, relation_type, strength, confidence,
                        source_layer, target_layer, created_at, updated_at, stale,
                        access_control)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           strength=excluded.strength,
                           confidence=excluded.confidence,
                           source_layer=excluded.source_layer,
                           target_layer=excluded.target_layer,
                           updated_at=excluded.updated_at,
                           stale=0,
                           access_control=excluded.access_control""",
                    (
                        rel_id,
                        source,
                        target,
                        relation_type,
                        strength,
                        confidence,
                        source_layer,
                        target_layer,
                        now,
                        now,
                        0,
                        json.dumps(
                            effective_access,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )
                for urn in (source, target):
                    name = self._canonical_name_from_urn(urn)
                    if name:
                        self._upsert_canonical_node_in_conn(
                            conn,
                            canonical_name=name,
                            source_ids=[urn],
                            access_control=effective_access,
                        )
                relation_row = conn.execute(
                    "SELECT * FROM cognitive_relations WHERE id=?",
                    (rel_id,),
                ).fetchone()
                after_hash = self._relation_effect_hash_in_conn(
                    conn,
                    rel_id,
                    source,
                    target,
                )
                record_target_effect(
                    conn,
                    permit,
                    status="committed",
                    before_hash=before_hash,
                    after_hash=after_hash,
                    evidence_refs=(
                        f"target-after:{after_hash}",
                        f"target-journal:cognitive-graph:{rel_id}:{after_hash}",
                    ),
                    outcome="cognitive graph relation committed",
                    observed_at=_now(),
                )
                conn.commit()
            except (
                sqlite3.Error,
                ValueError,
                TypeError,
                PermissionError,
                RuntimeError,
            ):
                conn.rollback()
                raise
        rel = self._row_to_relation(relation_row)
        if not recover_recorded_target_effect(
            material_action,
            CognitiveGraphRelationEffectOracle(self.db_path),
        ):
            raise RuntimeError("cognitive graph effect journal was not recoverable")
        record_projection()

        return rel  # type: ignore[return-value]

    def _record_relation_transition_projection(
        self,
        material_action: MaterialActionAuthorization,
        *,
        action: str,
        relation_id: str,
        reason: str,
        metadata: Mapping[str, Any],
    ) -> None:
        permit = material_action.permit
        FormalCognitiveMutationJournal.for_database(self.db_path).record(
            asset_kind="cognitive_graph_relation",
            action=action,
            target_ref=relation_id,
            actor="system",
            decision=permit.decision_revision_id,
            reason=reason,
            evidence_refs=(
                f"material-command:{permit.command_id}",
                f"decision-revision:{permit.decision_revision_id}",
                f"material-effect:{permit.effect_id}",
                f"relation:{relation_id}",
            ),
            metadata=dict(metadata),
            material_action=material_action,
        )

    @staticmethod
    def _canonical_node_effect_hash_in_conn(
        conn: sqlite3.Connection,
        canonical_id: str,
    ) -> str:
        row = conn.execute(
            "SELECT * FROM canonical_nodes WHERE canonical_id=?",
            (canonical_id,),
        ).fetchone()
        if row is None:
            return sha256_json({"canonical_node": None})
        payload = dict(row)
        embedding = payload.get("embedding")
        if isinstance(embedding, bytes):
            payload["embedding"] = "sha256:" + hashlib.sha256(embedding).hexdigest()
        return sha256_json({"canonical_node": payload})

    def _record_canonical_node_projection(
        self,
        material_action: MaterialActionAuthorization,
        *,
        canonical_id: str,
        canonical_name: str,
        aliases: Sequence[str],
        source_ids: Sequence[str],
        embedding: bytes | None,
        candidate_access: Mapping[str, Any],
    ) -> None:
        permit = material_action.permit
        FormalCognitiveMutationJournal.for_database(self.db_path).record(
            asset_kind="cognitive_graph_canonical_node",
            action=COGNITIVE_CANONICAL_NODE_ACTION,
            target_ref=canonical_id,
            actor="system",
            decision=permit.decision_revision_id,
            reason="cognitive_graph.add_canonical_node",
            evidence_refs=(
                f"material-command:{permit.command_id}",
                f"decision-revision:{permit.decision_revision_id}",
                f"material-effect:{permit.effect_id}",
                f"canonical-node:{canonical_id}",
            ),
            metadata={
                "canonical_name": canonical_name,
                "aliases": sorted({str(value) for value in aliases}),
                "source_ids": sorted({str(value) for value in source_ids}),
                "embedding_hash": (
                    "sha256:" + hashlib.sha256(embedding).hexdigest()
                    if embedding is not None
                    else ""
                ),
                "access_control_hash": cognitive_access_hash(candidate_access),
            },
            material_action=material_action,
        )

    def mark_stale(
        self,
        rel_id: str,
        *,
        material_action: MaterialActionAuthorization | None = None,
    ) -> bool:
        """Mark one exact relation stale behind a canonical material command."""

        binding = cognitive_relation_stale_material_action_binding(rel_id)
        state_db_path = self.db_path.parent / "producer_consumer_ledger.db"
        oracle = CognitiveGraphRelationStaleEffectOracle(self.db_path)
        recover_pending_target_effects(
            state_db_path=state_db_path,
            oracle=oracle,
            target_ref=binding["target_ref"],
        )
        resolved: MaterialActionAuthorization | None = None
        permit = None
        if material_action is not None:
            resolved, permit = resolve_material_action_recovery_authorization(
                material_action,
                owner=COGNITIVE_RELATION_OWNER,
                executor_id=COGNITIVE_RELATION_EXECUTOR,
                action_type=COGNITIVE_RELATION_STALE_ACTION,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
                expected_state_db=state_db_path,
            )
            if recover_recorded_target_effect(resolved, oracle):
                relation = self.get_relation(rel_id)
                if relation is None or not relation.stale:
                    raise RuntimeError("recovered stale-relation effect is absent from its target")
                self._record_relation_transition_projection(
                    resolved,
                    action=COGNITIVE_RELATION_STALE_ACTION,
                    relation_id=rel_id,
                    reason="cognitive_graph.mark_stale",
                    metadata={"relation_id": rel_id, "desired_stale": True},
                )
                return True

        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cognitive_relations WHERE id=?",
                (rel_id,),
            ).fetchone()
        if row is None or bool(row["stale"]):
            return False
        if resolved is None:
            resolved, permit = resolve_material_action_recovery_authorization(
                None,
                owner=COGNITIVE_RELATION_OWNER,
                executor_id=COGNITIVE_RELATION_EXECUTOR,
                action_type=COGNITIVE_RELATION_STALE_ACTION,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
                expected_state_db=state_db_path,
            )
        assert permit is not None
        require_material_action(
            resolved,
            owner=COGNITIVE_RELATION_OWNER,
            executor_id=COGNITIVE_RELATION_EXECUTOR,
            action_type=COGNITIVE_RELATION_STALE_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=state_db_path,
        )
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = conn.execute(
                    "SELECT * FROM cognitive_relations WHERE id=? AND stale=0",
                    (rel_id,),
                ).fetchone()
                if current is None:
                    raise RuntimeError("authorized stale-relation target is no longer active")
                before_hash = self._relation_effect_hash_in_conn(
                    conn,
                    rel_id,
                    str(current["source"]),
                    str(current["target"]),
                )
                conn.execute(
                    "UPDATE cognitive_relations SET stale=1, updated_at=? WHERE id=?",
                    (_now(), rel_id),
                )
                after_hash = self._relation_effect_hash_in_conn(
                    conn,
                    rel_id,
                    str(current["source"]),
                    str(current["target"]),
                )
                record_target_effect(
                    conn,
                    permit,
                    status="committed",
                    before_hash=before_hash,
                    after_hash=after_hash,
                    evidence_refs=(
                        f"target-after:{after_hash}",
                        f"target-journal:cognitive-graph-stale:{rel_id}:{after_hash}",
                    ),
                    outcome="cognitive graph relation marked stale",
                    observed_at=_now(),
                )
                conn.commit()
            except (sqlite3.Error, ValueError, TypeError, RuntimeError):
                conn.rollback()
                raise
        if not recover_recorded_target_effect(resolved, oracle):
            raise RuntimeError("cognitive graph stale effect was not recoverable")
        self._record_relation_transition_projection(
            resolved,
            action=COGNITIVE_RELATION_STALE_ACTION,
            relation_id=rel_id,
            reason="cognitive_graph.mark_stale",
            metadata={"relation_id": rel_id, "desired_stale": True},
        )
        return True

    def delete_relation(
        self,
        rel_id: str,
        *,
        material_action: MaterialActionAuthorization | None = None,
    ) -> bool:
        """Delete one exact relation behind a canonical material command."""

        binding = cognitive_relation_delete_material_action_binding(rel_id)
        state_db_path = self.db_path.parent / "producer_consumer_ledger.db"
        oracle = CognitiveGraphRelationDeleteEffectOracle(self.db_path)
        recover_pending_target_effects(
            state_db_path=state_db_path,
            oracle=oracle,
            target_ref=binding["target_ref"],
        )
        resolved: MaterialActionAuthorization | None = None
        permit = None
        if material_action is not None:
            resolved, permit = resolve_material_action_recovery_authorization(
                material_action,
                owner=COGNITIVE_RELATION_OWNER,
                executor_id=COGNITIVE_RELATION_EXECUTOR,
                action_type=COGNITIVE_RELATION_DELETE_ACTION,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
                expected_state_db=state_db_path,
            )
            if recover_recorded_target_effect(resolved, oracle):
                if self.get_relation(rel_id) is not None:
                    raise RuntimeError("recovered delete-relation effect remains in its target")
                self._record_relation_transition_projection(
                    resolved,
                    action=COGNITIVE_RELATION_DELETE_ACTION,
                    relation_id=rel_id,
                    reason="cognitive_graph.delete_relation",
                    metadata={"relation_id": rel_id, "desired_state": "absent"},
                )
                return True

        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM cognitive_relations WHERE id=?",
                (rel_id,),
            ).fetchone()
        if row is None:
            return False
        if resolved is None:
            resolved, permit = resolve_material_action_recovery_authorization(
                None,
                owner=COGNITIVE_RELATION_OWNER,
                executor_id=COGNITIVE_RELATION_EXECUTOR,
                action_type=COGNITIVE_RELATION_DELETE_ACTION,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
                expected_state_db=state_db_path,
            )
        assert permit is not None
        require_material_action(
            resolved,
            owner=COGNITIVE_RELATION_OWNER,
            executor_id=COGNITIVE_RELATION_EXECUTOR,
            action_type=COGNITIVE_RELATION_DELETE_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=state_db_path,
        )
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = conn.execute(
                    "SELECT * FROM cognitive_relations WHERE id=?",
                    (rel_id,),
                ).fetchone()
                if current is None:
                    raise RuntimeError("authorized delete-relation target is no longer present")
                before_hash = self._relation_effect_hash_in_conn(
                    conn,
                    rel_id,
                    str(current["source"]),
                    str(current["target"]),
                )
                conn.execute(
                    "DELETE FROM cognitive_relations WHERE id=?",
                    (rel_id,),
                )
                after_hash = self._relation_effect_hash_in_conn(
                    conn,
                    rel_id,
                    str(current["source"]),
                    str(current["target"]),
                )
                record_target_effect(
                    conn,
                    permit,
                    status="committed",
                    before_hash=before_hash,
                    after_hash=after_hash,
                    evidence_refs=(
                        f"target-after:{after_hash}",
                        f"target-journal:cognitive-graph-delete:{rel_id}:{after_hash}",
                    ),
                    outcome="cognitive graph relation deleted",
                    observed_at=_now(),
                )
                conn.commit()
            except (sqlite3.Error, ValueError, TypeError, RuntimeError):
                conn.rollback()
                raise
        if not recover_recorded_target_effect(resolved, oracle):
            raise RuntimeError("cognitive graph delete effect was not recoverable")
        self._record_relation_transition_projection(
            resolved,
            action=COGNITIVE_RELATION_DELETE_ACTION,
            relation_id=rel_id,
            reason="cognitive_graph.delete_relation",
            metadata={"relation_id": rel_id, "desired_state": "absent"},
        )
        return True

    def add_canonical_node(
        self,
        canonical_name: str,
        canonical_id: Optional[str] = None,
        aliases: Optional[List[str]] = None,
        source_ids: Optional[List[str]] = None,
        embedding: Optional[bytes] = None,
        access_control: Mapping[str, Any] | None = None,
        material_action: MaterialActionAuthorization | None = None,
    ) -> CanonicalNode:
        """Upsert one canonical node behind an exact material command."""
        resolved_id = canonical_id or self._canonical_id(canonical_name)
        normalized_aliases = list(aliases or ())
        normalized_source_ids = list(source_ids or ())
        candidate_access = _strictest_graph_access(
            [access_control] if access_control is not None else [],
            object_ref=f"canonical:{resolved_id}",
        )
        binding = cognitive_canonical_node_material_action_binding(
            canonical_name=canonical_name,
            canonical_id=resolved_id,
            aliases=normalized_aliases,
            source_ids=normalized_source_ids,
            embedding=embedding,
            access_control=access_control,
        )
        state_db_path = self.db_path.parent / "producer_consumer_ledger.db"
        oracle = CognitiveGraphCanonicalNodeEffectOracle(self.db_path)
        recover_pending_target_effects(
            state_db_path=state_db_path,
            oracle=oracle,
            target_ref=binding["target_ref"],
        )
        if material_action is None:
            self._assert_write_not_frozen(candidate_access)
        material_action, permit = resolve_material_action_recovery_authorization(
            material_action,
            owner=COGNITIVE_RELATION_OWNER,
            executor_id=COGNITIVE_RELATION_EXECUTOR,
            action_type=COGNITIVE_CANONICAL_NODE_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=state_db_path,
        )
        if recover_recorded_target_effect(material_action, oracle):
            existing = self.get_canonical_node(resolved_id)
            if existing is None:
                raise RuntimeError("recovered canonical-node effect is absent from its target")
            self._record_canonical_node_projection(
                material_action,
                canonical_id=resolved_id,
                canonical_name=canonical_name,
                aliases=normalized_aliases,
                source_ids=normalized_source_ids,
                embedding=embedding,
                candidate_access=candidate_access,
            )
            return existing
        require_material_action(
            material_action,
            owner=COGNITIVE_RELATION_OWNER,
            executor_id=COGNITIVE_RELATION_EXECUTOR,
            action_type=COGNITIVE_CANONICAL_NODE_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=state_db_path,
        )
        self._assert_write_not_frozen(candidate_access)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                before_hash = self._canonical_node_effect_hash_in_conn(
                    conn,
                    resolved_id,
                )
                node = self._upsert_canonical_node_in_conn(
                    conn,
                    canonical_name=canonical_name,
                    canonical_id=resolved_id,
                    aliases=normalized_aliases,
                    source_ids=normalized_source_ids,
                    embedding=embedding,
                    # The transaction helper owns ACL projection.  Passing its
                    # already-projected result here would project a second time,
                    # append the first projection hash to lineage, and make a
                    # crash-before-receipt replay conflict with its own write.
                    access_control=access_control,
                )
                after_hash = self._canonical_node_effect_hash_in_conn(
                    conn,
                    resolved_id,
                )
                record_target_effect(
                    conn,
                    permit,
                    status="committed",
                    before_hash=before_hash,
                    after_hash=after_hash,
                    evidence_refs=(
                        f"target-after:{after_hash}",
                        f"target-journal:cognitive-node:{resolved_id}:{after_hash}",
                    ),
                    outcome="cognitive graph canonical node committed",
                    observed_at=_now(),
                )
                conn.commit()
            except (
                sqlite3.Error,
                ValueError,
                TypeError,
                PermissionError,
                RuntimeError,
            ):
                conn.rollback()
                raise
        if not recover_recorded_target_effect(material_action, oracle):
            raise RuntimeError("canonical-node effect journal was not recoverable")
        self._record_canonical_node_projection(
            material_action,
            canonical_id=resolved_id,
            canonical_name=canonical_name,
            aliases=normalized_aliases,
            source_ids=normalized_source_ids,
            embedding=embedding,
            candidate_access=candidate_access,
        )
        return node

    def _upsert_canonical_node_in_conn(
        self,
        conn: sqlite3.Connection,
        *,
        canonical_name: str,
        canonical_id: Optional[str] = None,
        aliases: Optional[List[str]] = None,
        source_ids: Optional[List[str]] = None,
        embedding: Optional[bytes] = None,
        access_control: Mapping[str, Any] | None = None,
    ) -> CanonicalNode:
        """Upsert the relation-owned node inside the caller's transaction."""

        resolved_id = canonical_id or self._canonical_id(canonical_name)
        now = _now()
        candidate_access = _strictest_graph_access(
            [access_control] if access_control is not None else [],
            object_ref=f"canonical:{resolved_id}",
        )
        if self._object_subject_deleted(
            conn,
            object_type="canonical_node",
            object_id=resolved_id,
        ):
            raise PermissionError(
                "canonical cognitive node is subject-deleted and cannot be restored"
            )
        existing = self._get_canonical_node_raw(conn, resolved_id)
        if existing:
            merged_aliases = sorted(set(aliases or ()) | set(existing.aliases))
            merged_source_ids = sorted(set(source_ids or ()) | set(existing.source_ids))
            effective_access = _strictest_graph_access(
                [existing.access_control, candidate_access],
                object_ref=f"canonical:{resolved_id}",
            )
        else:
            merged_aliases = sorted(set(aliases or ()))
            merged_source_ids = sorted(set(source_ids or ()))
            effective_access = candidate_access
        conn.execute(
            """INSERT INTO canonical_nodes
               (canonical_id, canonical_name, aliases, source_ids, embedding,
                created_at, updated_at, access_control)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(canonical_id) DO UPDATE SET
                   canonical_name=excluded.canonical_name,
                   aliases=excluded.aliases,
                   source_ids=excluded.source_ids,
                   embedding=COALESCE(excluded.embedding, canonical_nodes.embedding),
                   updated_at=excluded.updated_at,
                   access_control=excluded.access_control""",
            (
                resolved_id,
                canonical_name,
                json.dumps(merged_aliases, ensure_ascii=False),
                json.dumps(merged_source_ids, ensure_ascii=False),
                embedding,
                now,
                now,
                json.dumps(effective_access, ensure_ascii=False, sort_keys=True),
            ),
        )
        row = conn.execute(
            "SELECT * FROM canonical_nodes WHERE canonical_id=?",
            (resolved_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("canonical node upsert did not persist its target")
        return self._row_to_canonical_node(row)

    def add_relations_atomic(
        self,
        relations: Iterable[Dict[str, Any]],
        *,
        material_action_resolver: (
            Callable[[Mapping[str, str]], MaterialActionAuthorization] | None
        ) = None,
    ) -> List[CognitiveRelation]:
        """Atomically upsert an exact, individually authorized relation batch."""

        state_db_path = self.db_path.parent / "producer_consumer_ledger.db"
        oracle = CognitiveGraphRelationEffectOracle(self.db_path)
        plans: list[dict[str, Any]] = []
        for raw in relations:
            rel = dict(raw)
            source = str(rel["source"])
            target = str(rel["target"])
            relation_type = str(rel["relation_type"])
            strength = float(rel.get("strength", 0.5))
            confidence = float(rel.get("confidence", 0.5))
            source_layer = str(rel.get("source_layer", ""))
            target_layer = str(rel.get("target_layer", ""))
            access_control = rel.get("access_control")
            rel_id = _relation_id(source, target, relation_type)
            candidate_access = _strictest_graph_access(
                [access_control] if access_control is not None else [],
                object_ref=f"relation:{rel_id}",
            )
            binding = cognitive_relation_material_action_binding(
                source=source,
                target=target,
                relation_type=relation_type,
                strength=strength,
                confidence=confidence,
                source_layer=source_layer,
                target_layer=target_layer,
                access_control=access_control,
            )
            recover_pending_target_effects(
                state_db_path=state_db_path,
                oracle=oracle,
                target_ref=binding["target_ref"],
            )
            supplied = (
                material_action_resolver(binding) if material_action_resolver is not None else None
            )
            authorization, permit = resolve_material_action_recovery_authorization(
                supplied,
                owner=COGNITIVE_RELATION_OWNER,
                executor_id=COGNITIVE_RELATION_EXECUTOR,
                action_type=COGNITIVE_RELATION_ACTION,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
                expected_state_db=state_db_path,
            )
            recovered = recover_recorded_target_effect(authorization, oracle)
            if not recovered:
                self._assert_write_not_frozen(candidate_access)
                require_material_action(
                    authorization,
                    owner=COGNITIVE_RELATION_OWNER,
                    executor_id=COGNITIVE_RELATION_EXECUTOR,
                    action_type=COGNITIVE_RELATION_ACTION,
                    target_ref=binding["target_ref"],
                    input_hash=binding["input_hash"],
                    expected_state_db=state_db_path,
                )
            plans.append(
                {
                    "relation_id": rel_id,
                    "source": source,
                    "target": target,
                    "relation_type": relation_type,
                    "strength": strength,
                    "confidence": confidence,
                    "source_layer": source_layer,
                    "target_layer": target_layer,
                    "access_control": access_control,
                    "candidate_access": candidate_access,
                    "authorization": authorization,
                    "permit": permit,
                    "binding": binding,
                    "recovered": recovered,
                }
            )
        if not plans:
            return []

        pending = [plan for plan in plans if not plan["recovered"]]
        if pending:
            with self._conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                now = _now()
                try:
                    for plan in pending:
                        rel_id = str(plan["relation_id"])
                        source = str(plan["source"])
                        target = str(plan["target"])
                        binding = plan["binding"]
                        require_material_action(
                            plan["authorization"],
                            owner=COGNITIVE_RELATION_OWNER,
                            executor_id=COGNITIVE_RELATION_EXECUTOR,
                            action_type=COGNITIVE_RELATION_ACTION,
                            target_ref=str(binding["target_ref"]),
                            input_hash=str(binding["input_hash"]),
                            expected_state_db=state_db_path,
                        )
                        if self._object_subject_deleted(
                            conn,
                            object_type="relation",
                            object_id=rel_id,
                        ):
                            raise PermissionError(
                                "cognitive relation is subject-deleted and cannot be restored"
                            )
                        before_hash = self._relation_effect_hash_in_conn(
                            conn,
                            rel_id,
                            source,
                            target,
                        )
                        existing = conn.execute(
                            "SELECT access_control FROM cognitive_relations WHERE id=?",
                            (rel_id,),
                        ).fetchone()
                        effective_access = (
                            _strictest_graph_access(
                                [
                                    _parse_graph_access(
                                        existing["access_control"],
                                        f"relation:{rel_id}",
                                    ),
                                    plan["candidate_access"],
                                ],
                                object_ref=f"relation:{rel_id}",
                            )
                            if existing is not None
                            else plan["candidate_access"]
                        )
                        conn.execute(
                            """INSERT INTO cognitive_relations
                               (id, source, target, relation_type, strength, confidence,
                                source_layer, target_layer, created_at, updated_at, stale,
                                access_control)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(id) DO UPDATE SET
                                   strength=excluded.strength,
                                   confidence=excluded.confidence,
                                   source_layer=excluded.source_layer,
                                   target_layer=excluded.target_layer,
                                   updated_at=excluded.updated_at,
                                   stale=0,
                                   access_control=excluded.access_control""",
                            (
                                rel_id,
                                source,
                                target,
                                plan["relation_type"],
                                plan["strength"],
                                plan["confidence"],
                                plan["source_layer"],
                                plan["target_layer"],
                                now,
                                now,
                                0,
                                json.dumps(
                                    effective_access,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ),
                            ),
                        )
                        for urn in (source, target):
                            name = self._canonical_name_from_urn(urn)
                            if name:
                                self._upsert_canonical_node_in_conn(
                                    conn,
                                    canonical_name=name,
                                    source_ids=[urn],
                                    access_control=effective_access,
                                )
                        after_hash = self._relation_effect_hash_in_conn(
                            conn,
                            rel_id,
                            source,
                            target,
                        )
                        record_target_effect(
                            conn,
                            plan["permit"],
                            status="committed",
                            before_hash=before_hash,
                            after_hash=after_hash,
                            evidence_refs=(
                                f"target-after:{after_hash}",
                                f"target-journal:cognitive-graph:{rel_id}:{after_hash}",
                            ),
                            outcome="cognitive graph relation batch committed",
                            observed_at=_now(),
                        )
                    conn.commit()
                except (
                    sqlite3.Error,
                    ValueError,
                    TypeError,
                    PermissionError,
                    RuntimeError,
                ):
                    conn.rollback()
                    raise

        for plan in pending:
            if not recover_recorded_target_effect(plan["authorization"], oracle):
                raise RuntimeError("cognitive graph batch effect journal was not recoverable")
        for plan in plans:
            relation = self.get_relation(str(plan["relation_id"]))
            if relation is None:
                raise RuntimeError("authorized cognitive graph batch relation is absent")
            self._record_relation_upsert_projection(
                plan["authorization"],
                relation_id=str(plan["relation_id"]),
                source=str(plan["source"]),
                target=str(plan["target"]),
                relation_type=str(plan["relation_type"]),
                strength=float(plan["strength"]),
                confidence=float(plan["confidence"]),
                source_layer=str(plan["source_layer"]),
                target_layer=str(plan["target_layer"]),
                candidate_access=plan["candidate_access"],
            )
        relation_ids = [str(plan["relation_id"]) for plan in plans]
        return [
            relation
            for relation_id in relation_ids
            if (relation := self.get_relation(relation_id)) is not None
        ]
