"""Canonical append-only BeliefRevision state over CognitiveStateStore."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Callable, Mapping, Sequence

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import (
    authorize_cognitive_access,
    authorize_cognitive_write,
    cognitive_access_hash,
    derive_strictest_cognitive_access,
    validate_cognitive_access_envelope,
)
from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionRequest,
    authorize_exact_project_contract_action,
    material_action_resolution_scope,
)
from core.cognitive_graph.store import (
    COGNITIVE_CANONICAL_NODE_ACTION,
    COGNITIVE_RELATION_ACTION,
    COGNITIVE_RELATION_EXECUTOR,
    COGNITIVE_RELATION_OWNER,
    COGNITIVE_RELATION_STALE_ACTION,
)
from core.cognitive.state_contract import (
    COGNITIVE_OBJECT_SCHEMA_VERSIONS,
    CognitiveStateRevision,
    LocalConsumerCommand,
    canonical_json,
    sha256_json,
    validate_cognitive_state_payload,
)
from core.cognitive.state_store import CognitiveStateStore
from core.ops.cognitive_data_contract import CognitiveDataEvent

BELIEF_CONSUMER = "cognitive_graph"
BELIEF_COMMAND_TYPE = "project_belief_revision"
_CLAIM_KINDS = frozenset({"fact", "hypothesis", "preference", "policy", "decision_assumption"})
BELIEF_GRAPH_DECISION_CONTRACT_ID = "project-contract:belief-revision-graph-projection"
BELIEF_GRAPH_DECISION_CONTRACT_REVISION = "mnemos.belief_revision_graph_material_effects.v1"
BELIEF_GRAPH_DECISION_CONTRACT_TEXT = (
    "A canonical BeliefRevision command or deterministic validity reconciliation "
    "may project only its exact structural relation effects into CognitiveGraph."
)
BELIEF_GRAPH_DECISION_PRODUCER_HASH = sha256_json(
    {
        "module": "core.cognitive.belief_revision",
        "producer": "BeliefRevisionProjector",
        "version": BELIEF_GRAPH_DECISION_CONTRACT_REVISION,
    }
)


@dataclass(frozen=True)
class BeliefRevisionCommand:
    """Caller evidence for one system-owned scoped belief revision."""

    claim: str
    claim_kind: str
    scope_type: str
    scope_id: str
    source_id: str
    source_revision_id: str
    source_content_hash: str
    source_access_control: Mapping[str, Any]
    source_span_ids: tuple[str, ...] = ()
    supporting_evidence: tuple[str, ...] = ()
    opposing_evidence: tuple[str, ...] = ()
    withdrawn_evidence: tuple[str, ...] = ()
    confidence_method: str = "unscored"
    confidence: float | None = None
    confidence_evidence: tuple[str, ...] = ()
    uncertainty_reasons: tuple[str, ...] = ()
    valid_from: str = ""
    valid_until: str = ""
    invalidation_conditions: tuple[str, ...] = ()
    expected_current_revision_id: str = ""
    correction_of_revision_id: str = ""
    correction_evidence_ref: str = ""
    disposition: str = ""
    proposal_id: str = ""
    journal_id: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class BeliefRevisionReceipt:
    status: str
    event_id: str
    belief_id: str
    claim_id: str
    revision_id: str
    command_id: str
    projection_effect_id: str
    transaction_hash: str


@dataclass(frozen=True)
class BeliefExplanation:
    status: str
    belief_id: str = ""
    claim_id: str = ""
    claim: str = ""
    claim_kind: str = ""
    stance: str = ""
    active: bool = False
    inactive_reason: str = ""
    scope: tuple[str, str] = ("", "")
    valid_from: str = ""
    valid_until: str = ""
    supporting_evidence: tuple[str, ...] = ()
    opposing_evidence: tuple[str, ...] = ()
    withdrawn_evidence: tuple[str, ...] = ()
    confidence_method: str = ""
    confidence: float | None = None
    uncertainty: Mapping[str, Any] = field(default_factory=dict)
    invalidation_conditions: tuple[str, ...] = ()
    current_revision_id: str = ""
    revision_lineage: tuple[str, ...] = ()
    omitted_history_count: int = 0


class BeliefRevisionStore:
    """Deep module owning identity, conflicts, authorization, and explanation."""

    def __init__(self, state_store: CognitiveStateStore):
        self.state_store = state_store

    def revise(
        self,
        command: BeliefRevisionCommand,
        *,
        principal: PrincipalEnvelope | None,
        _failpoint: Callable[[str], None] | None = None,
    ) -> BeliefRevisionReceipt:
        normalized = _normalize_command(command)
        source_access = validate_cognitive_access_envelope(
            command.source_access_control,
            expected_scope_type=normalized["scope_type"],
            expected_scope_id=normalized["scope_id"],
        )
        if normalized["source_id"] not in source_access["consent"]["provenance_refs"]:
            raise ValueError("source access consent does not bind source_id")
        write_decision = authorize_cognitive_write(
            source_access,
            principal=principal,
            scope_type=normalized["scope_type"],
            scope_id=normalized["scope_id"],
        )
        if not write_decision.allowed:
            raise PermissionError(f"belief source access denied: {write_decision.reason}")
        assert principal is not None

        claim_id, belief_id = _belief_identity(
            normalized["claim"],
            normalized["scope_type"],
            normalized["scope_id"],
        )
        event_id = _belief_event_id(normalized, claim_id=claim_id, belief_id=belief_id)
        replay = self._receipt_for_event(event_id)
        if replay is not None:
            return replay

        current = self.state_store.current_revision("belief_revision", belief_id)
        expected_current = normalized["expected_current_revision_id"]
        if current is None:
            if expected_current:
                raise RuntimeError("expected current revision does not exist")
        else:
            if not expected_current:
                raise RuntimeError("expected current revision is required")
            if expected_current != current.revision_id:
                raise RuntimeError("expected current revision does not match canonical head")

        correction_target = normalized["correction_of_revision_id"]
        withdrawals = set(normalized["withdrawn_evidence"])
        if withdrawals or normalized["disposition"]:
            if current is None or correction_target != current.revision_id:
                raise ValueError("correction must reference the current belief revision")
            if not normalized["correction_evidence_ref"]:
                raise ValueError("correction evidence is required")
            if normalized["correction_evidence_ref"] not in set(
                source_access["consent"]["provenance_refs"]
            ):
                raise ValueError("correction evidence is not bound by the source ACL")
        elif correction_target:
            raise ValueError("correction target requires a withdrawal or disposition")

        current_payload = dict(current.payload) if current is not None else {}
        current_support = set(current_payload.get("supporting_evidence", ()))
        current_opposition = set(current_payload.get("opposing_evidence", ()))
        current_withdrawn = set(current_payload.get("withdrawn_evidence", ()))
        if not withdrawals <= (current_support | current_opposition):
            raise ValueError("withdrawn evidence is not active in the current revision")
        supporting = (current_support | set(normalized["supporting_evidence"])) - withdrawals
        opposing = (current_opposition | set(normalized["opposing_evidence"])) - withdrawals
        if supporting & opposing:
            raise ValueError("one evidence ref cannot support and oppose the same belief")
        withdrawn = current_withdrawn | withdrawals
        stance = _derive_stance(
            supporting,
            opposing,
            disposition=normalized["disposition"],
        )

        source_controls = [source_access]
        if current is not None:
            source_controls.insert(
                0,
                validate_cognitive_access_envelope(current.payload["access_control"]),
            )
        access_control = derive_strictest_cognitive_access(
            source_controls,
            owner_principal_id=principal.principal_id,
            owner_agent=principal.agent,
            scope_type=normalized["scope_type"],
            scope_id=normalized["scope_id"],
            purposes=("belief_read",),
            retention_policy=str(source_access["retention_policy"]),
        )
        if access_control["scope"]["resolution"] != "resolved":
            raise PermissionError("belief sources have incompatible authorization scopes")

        uncertainty_reasons = set(normalized["uncertainty_reasons"])
        if normalized["confidence_method"] == "unscored":
            uncertainty_reasons.add("confidence_not_measured")
        if stance == "disputed":
            uncertainty_reasons.add("conflicting_evidence")
        projection_effect_id = (
            "belief-effect-"
            + _digest({"event_id": event_id, "belief_id": belief_id, "consumer": BELIEF_CONSUMER})[
                :32
            ]
        )
        valid_until = normalized["valid_until"] or str(current_payload.get("valid_until") or "")
        invalidation_conditions = tuple(
            sorted(
                set(current_payload.get("invalidation_conditions", ()))
                | set(normalized["invalidation_conditions"])
            )
        )
        payload = {
            "schema_version": COGNITIVE_OBJECT_SCHEMA_VERSIONS["belief_revision"],
            "belief_id": belief_id,
            "claim_id": claim_id,
            "claim": normalized["claim"],
            "claim_kind": normalized["claim_kind"],
            "stance": stance,
            "supporting_evidence": sorted(supporting),
            "opposing_evidence": sorted(opposing),
            "withdrawn_evidence": sorted(withdrawn),
            "confidence_method": normalized["confidence_method"],
            "confidence": normalized["confidence"],
            "confidence_evidence": list(normalized["confidence_evidence"]),
            "uncertainty": {
                "status": "uncertain" if uncertainty_reasons else "bounded",
                "reasons": sorted(uncertainty_reasons),
            },
            "valid_from": normalized["valid_from"],
            "valid_until": valid_until,
            "invalidation_conditions": list(invalidation_conditions),
            "admission_refs": {
                "proposal_id": normalized["proposal_id"],
                "journal_id": normalized["journal_id"],
                "projection_effect_id": projection_effect_id,
            },
            "supersedes_revision_id": current.revision_id if current is not None else "",
            "correction_of_revision_id": correction_target,
            "access_control": access_control,
        }
        validate_cognitive_state_payload("belief_revision", payload)
        evidence_refs = tuple(
            sorted(
                {
                    normalized["source_id"],
                    *normalized["source_span_ids"],
                    *supporting,
                    *opposing,
                    *withdrawn,
                    *normalized["confidence_evidence"],
                    *(
                        (normalized["correction_evidence_ref"],)
                        if normalized["correction_evidence_ref"]
                        else ()
                    ),
                }
            )
        )
        revision = CognitiveStateRevision.create(
            object_type="belief_revision",
            object_id=belief_id,
            source_event_id=event_id,
            source_revision_id=normalized["source_revision_id"],
            source_content_hash=normalized["source_content_hash"],
            scope_type=normalized["scope_type"],
            scope_id=normalized["scope_id"],
            evidence_refs=evidence_refs,
            payload=payload,
            supersedes_revision_id=current.revision_id if current is not None else "",
            correction_of_revision_id=correction_target,
            created_at=normalized["created_at"],
        )
        event = CognitiveDataEvent(
            event_id=event_id,
            source_id=normalized["source_id"],
            asset_id=normalized["source_content_hash"],
            source_kind="belief_revision",
            source_uri=f"mnemos://source/{_digest(normalized['source_id'])[:32]}",
            content_hash=normalized["source_content_hash"],
            canonical_subject=f"belief_revision:{belief_id}",
            data_type="belief_revision",
            producer="belief_revision_store",
            intended_consumers=(BELIEF_CONSUMER,),
            privacy_level=str(access_control["sensitivity"]),
            confidence=float(normalized["confidence"] or 0.0),
            evidence_refs=evidence_refs,
            dedupe_key=f"belief-revision:{event_id}",
            created_at=normalized["created_at"],
            retention_policy=str(access_control["retention_policy"]),
            metadata={
                "revision_ids": [revision.revision_id],
                "contract_version": COGNITIVE_OBJECT_SCHEMA_VERSIONS["belief_revision"],
                "access_control_hash": cognitive_access_hash(access_control),
            },
        )
        outbox = LocalConsumerCommand.create(
            revision_id=revision.revision_id,
            consumer_id=BELIEF_CONSUMER,
            command_type=BELIEF_COMMAND_TYPE,
            payload={
                "belief_id": belief_id,
                "claim_id": claim_id,
                "revision_id": revision.revision_id,
                "revision_hash": revision.payload_hash,
                "projection_effect_id": projection_effect_id,
            },
            created_at=normalized["created_at"],
        )
        committed = self.state_store.unit_of_work().commit(
            revisions=(revision,),
            event=event,
            commands=(outbox,),
            failpoint=_failpoint,
        )
        return BeliefRevisionReceipt(
            status=committed.status,
            event_id=event_id,
            belief_id=belief_id,
            claim_id=claim_id,
            revision_id=revision.revision_id,
            command_id=outbox.command_id,
            projection_effect_id=projection_effect_id,
            transaction_hash=committed.transaction_hash,
        )

    def explain(
        self,
        belief_id: str,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        now: datetime | None = None,
    ) -> BeliefExplanation:
        normalized_id = str(belief_id or "").strip()
        revisions, access = self.state_store.authorized_current_revisions(
            principal=principal,
            narrowing=narrowing,
            purpose="belief_read",
            object_type="belief_revision",
            object_id=normalized_id,
        )
        if not revisions:
            return BeliefExplanation(
                status=("access_denied" if access["candidate_count"] else "not_found"),
                belief_id=normalized_id,
            )
        revision = revisions[0]
        return self._explanation(
            revision,
            principal=principal,
            narrowing=narrowing,
            now=now,
        )

    def list_active(
        self,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        now: datetime | None = None,
        scope_type: str = "",
        scope_id: str = "",
    ) -> tuple[BeliefExplanation, ...]:
        revisions, _ = self.state_store.authorized_current_revisions(
            principal=principal,
            narrowing=narrowing,
            purpose="belief_read",
            object_type="belief_revision",
            scope_type=scope_type,
            scope_id=scope_id,
        )
        explanations = tuple(
            self._explanation(
                revision,
                principal=principal,
                narrowing=narrowing,
                now=now,
            )
            for revision in revisions
        )
        return tuple(value for value in explanations if value.active)

    def _explanation(
        self,
        revision: CognitiveStateRevision,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
        now: datetime | None,
    ) -> BeliefExplanation:
        payload = dict(revision.payload)
        validate_cognitive_state_payload("belief_revision", payload)
        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        inactive_reason = ""
        if payload["stance"] in {"unknown", "deprecated"}:
            inactive_reason = str(payload["stance"])
        elif current_time < _timestamp(payload["valid_from"]):
            inactive_reason = "not_yet_valid"
        elif payload["valid_until"] and current_time >= _timestamp(payload["valid_until"]):
            inactive_reason = "expired"
        lineage, omitted = self._authorized_history(
            revision.object_id,
            principal=principal,
            narrowing=narrowing,
        )
        return BeliefExplanation(
            status="ok",
            belief_id=str(payload["belief_id"]),
            claim_id=str(payload["claim_id"]),
            claim=str(payload["claim"]),
            claim_kind=str(payload["claim_kind"]),
            stance=str(payload["stance"]),
            active=not inactive_reason,
            inactive_reason=inactive_reason,
            scope=(revision.scope_type, revision.scope_id),
            valid_from=str(payload["valid_from"]),
            valid_until=str(payload["valid_until"]),
            supporting_evidence=tuple(payload["supporting_evidence"]),
            opposing_evidence=tuple(payload["opposing_evidence"]),
            withdrawn_evidence=tuple(payload["withdrawn_evidence"]),
            confidence_method=str(payload["confidence_method"]),
            confidence=(
                float(payload["confidence"]) if payload["confidence"] is not None else None
            ),
            uncertainty=dict(payload["uncertainty"]),
            invalidation_conditions=tuple(payload["invalidation_conditions"]),
            current_revision_id=revision.revision_id,
            revision_lineage=lineage,
            omitted_history_count=omitted,
        )

    def _authorized_history(
        self,
        belief_id: str,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None,
    ) -> tuple[tuple[str, ...], int]:
        authorized_ids: list[str] = []
        omitted = 0
        with self.state_store._connect(read_only=True) as conn:  # noqa: SLF001
            rows = conn.execute(
                """
                SELECT revision_id, scope_type, scope_id,
                       json_extract(payload_json, '$.access_control') AS access_json
                FROM cognitive_state_revisions
                WHERE object_type='belief_revision' AND object_id=?
                  AND admission_state='active'
                ORDER BY revision_no
                """,
                (belief_id,),
            ).fetchall()
        for row in rows:
            try:
                access = validate_cognitive_access_envelope(
                    json.loads(str(row["access_json"] or "")),
                    expected_scope_type=str(row["scope_type"]),
                    expected_scope_id=str(row["scope_id"]),
                )
                decision = authorize_cognitive_access(
                    access,
                    principal=principal,
                    narrowing=narrowing,
                    purpose="belief_read",
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                decision = None
            if decision is not None and decision.allowed:
                authorized_ids.append(str(row["revision_id"]))
            else:
                omitted += 1
        return tuple(authorized_ids), omitted

    def _receipt_for_event(self, event_id: str) -> BeliefRevisionReceipt | None:
        if not self.state_store.db_path.is_file():
            return None
        with self.state_store._connect(read_only=True) as conn:  # noqa: SLF001
            row = conn.execute(
                """
                SELECT revision_id FROM cognitive_state_revisions
                WHERE object_type='belief_revision' AND source_event_id=?
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                return None
            revision_id = str(row["revision_id"])
            command = conn.execute(
                """
                SELECT command_id, payload_json FROM cognitive_state_outbox
                WHERE revision_id=? AND consumer_id=? AND command_type=?
                """,
                (revision_id, BELIEF_CONSUMER, BELIEF_COMMAND_TYPE),
            ).fetchone()
        revision = self.state_store.revision(revision_id)
        if revision is None or command is None:
            raise RuntimeError("belief replay lacks a committed revision or outbox")
        payload = dict(revision.payload)
        command_payload = json.loads(str(command["payload_json"]))
        command_id = str(command["command_id"])
        transaction_hash = sha256_json(
            {
                "event_id": event_id,
                "revision_ids": [revision_id],
                "command_ids": [command_id],
            }
        )
        return BeliefRevisionReceipt(
            status="existing",
            event_id=event_id,
            belief_id=str(payload["belief_id"]),
            claim_id=str(payload["claim_id"]),
            revision_id=revision_id,
            command_id=command_id,
            projection_effect_id=str(command_payload["projection_effect_id"]),
            transaction_hash=transaction_hash,
        )


class BeliefRevisionProjector:
    """Rebuildable CognitiveGraph consumer with exact state-store receipts.

    Graph writes deliberately use the graph store's existing object-level ACL
    and subject-deletion adapters.  The graph is not a second belief owner: it
    contains opaque identity nodes and structural relations only, while every
    semantic body remains in ``CognitiveStateStore``.
    """

    def __init__(
        self,
        state_store: CognitiveStateStore,
        graph_store: Any,
    ) -> None:
        self.state_store = state_store
        self.graph_store = graph_store

    def _authorize_graph_action(
        self,
        request: MaterialActionRequest,
        *,
        source_namespace: str,
        source_facts: Mapping[str, Any],
        evidence_refs: tuple[str, ...],
        created_at: str,
        task: str,
        goal: str,
    ) -> MaterialActionAuthorization:
        state_db_path = (
            Path(self.graph_store.db_path).parent / "producer_consumer_ledger.db"
        ).resolve(strict=False)
        actual_state_db = Path(str(request.expected_state_db)).resolve(strict=False)
        if (
            request.owner != COGNITIVE_RELATION_OWNER
            or request.executor_id != COGNITIVE_RELATION_EXECUTOR
            or request.action_type
            not in {
                COGNITIVE_CANONICAL_NODE_ACTION,
                COGNITIVE_RELATION_ACTION,
                COGNITIVE_RELATION_STALE_ACTION,
            }
            or actual_state_db != state_db_path
        ):
            raise PermissionError(
                "belief projection requested an unsupported graph material action"
            )
        exact_facts = {
            **dict(source_facts),
            "schema_version": "mnemos.belief_graph_material_facts.v1",
            "material_request": {
                "owner": request.owner,
                "executor_id": request.executor_id,
                "action_type": request.action_type,
                "target_ref": request.target_ref,
                "input_hash": request.input_hash,
                "expected_state_db": str(state_db_path),
            },
        }
        return authorize_exact_project_contract_action(
            expected_request=request,
            state_db_path=state_db_path,
            contract_id=BELIEF_GRAPH_DECISION_CONTRACT_ID,
            contract_revision_id=BELIEF_GRAPH_DECISION_CONTRACT_REVISION,
            contract_text=BELIEF_GRAPH_DECISION_CONTRACT_TEXT,
            source_namespace=source_namespace,
            source_facts=exact_facts,
            decision_checks={
                "supported_graph_action": request.action_type
                in {
                    COGNITIVE_CANONICAL_NODE_ACTION,
                    COGNITIVE_RELATION_ACTION,
                    COGNITIVE_RELATION_STALE_ACTION,
                },
                "canonical_state_store_binding": actual_state_db == state_db_path,
            },
            evidence_refs=evidence_refs,
            task=task,
            goal=goal,
            constraints=(
                "The canonical belief revision or validity snapshot must remain exact.",
                "Only canonical-node upserts and relation upsert/stale transitions "
                "derived by projector code are allowed.",
                "Each graph target must close an independent reciprocal effect receipt.",
            ),
            created_at=created_at,
            producer="belief-revision-projector",
            producer_version=BELIEF_GRAPH_DECISION_CONTRACT_REVISION,
            producer_code_hash=BELIEF_GRAPH_DECISION_PRODUCER_HASH,
            evaluator_id="belief-graph-material-evaluator",
            approved_candidate_key="apply_exact_belief_graph_projection",
            approved_candidate_summary=(
                "Apply the exact structural relation transition derived from canonical belief state."
            ),
            rejected_candidate_key="retain_current_graph_projection",
            rejected_candidate_summary=(
                "Retain current graph state when the belief source or relation binding drifts."
            ),
            approved_reason_code="belief_graph_binding_verified",
            rejected_reason_code="belief_graph_binding_rejected",
            committed_metric="belief_graph_material_effect_committed",
            rejected_metric="unbound_belief_graph_effect_count",
        )

    def process_pending(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> dict[str, int]:
        committed = 0
        failed = 0
        commands = self.state_store.pending_commands(BELIEF_CONSUMER)[: max(0, limit)]
        for command in commands:
            if str(command["command_type"]) != BELIEF_COMMAND_TYPE:
                continue
            try:
                self.process_command(str(command["command_id"]), now=now)
                committed += 1
            except (OSError, RuntimeError, TypeError, ValueError, PermissionError):
                failed += 1
        remaining = sum(
            1
            for command in self.state_store.pending_commands(BELIEF_CONSUMER)
            if str(command["command_type"]) == BELIEF_COMMAND_TYPE
        )
        self.reconcile_validity(now=now)
        return {"committed": committed, "failed": failed, "pending": remaining}

    def process_command(
        self,
        command_id: str,
        *,
        now: datetime | None = None,
        _failpoint: Callable[[str], None] | None = None,
    ) -> Any:
        command = self._pending_command(command_id)
        if str(command["command_type"]) != BELIEF_COMMAND_TYPE:
            raise ValueError("command is not a belief projection")
        command_payload = command["payload"]
        if not isinstance(command_payload, Mapping):
            raise ValueError("belief projection command payload is invalid")
        revision_id = _required(command_payload.get("revision_id"), "revision_id")
        belief_id = _required(command_payload.get("belief_id"), "belief_id")
        projection_effect_id = _required(
            command_payload.get("projection_effect_id"),
            "projection_effect_id",
        )
        revision = self.state_store.revision(revision_id)
        if revision is None:
            raise RuntimeError("belief projection revision is unavailable")
        payload = dict(revision.payload)
        validate_cognitive_state_payload("belief_revision", payload)
        if (
            revision.object_type != "belief_revision"
            or revision.object_id != belief_id
            or revision.revision_id != str(command["revision_id"])
            or revision.payload_hash != str(command_payload.get("revision_hash") or "")
            or payload["belief_id"] != belief_id
            or payload["claim_id"] != command_payload.get("claim_id")
            or payload["admission_refs"]["projection_effect_id"] != projection_effect_id
        ):
            raise RuntimeError("belief projection identity binding failed")
        self._verify_event_acl_hash(revision)

        current = self.state_store.current_revision("belief_revision", belief_id)
        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        is_active_head = bool(
            current is not None
            and current.revision_id == revision_id
            and _revision_is_active(payload, current_time)
        )
        before_hash = self.projection_hash(belief_id)
        projection_facts = {
            "source_kind": "belief_projection_command",
            "command_id": str(command["command_id"]),
            "revision_id": revision.revision_id,
            "revision_hash": revision.payload_hash,
            "belief_id": belief_id,
            "projection_effect_id": projection_effect_id,
            "is_active_head": is_active_head,
            "evaluated_at": current_time.isoformat(),
            "before_hash": before_hash,
        }
        resolver = lambda request: self._authorize_graph_action(  # noqa: E731
            request,
            source_namespace="belief-graph-command",
            source_facts=projection_facts,
            evidence_refs=(
                f"belief-command:{command['command_id']}",
                f"belief-revision:{revision.revision_id}",
                f"belief-projection-effect:{projection_effect_id}",
            ),
            created_at=revision.created_at,
            task=f"Project belief revision {revision.revision_id}",
            goal=(
                "Make CognitiveGraph exactly reflect the canonical belief revision "
                "and its current-head disposition."
            ),
        )
        with material_action_resolution_scope(resolver):
            self._project_revision(
                revision,
                access_control=payload["access_control"],
                is_active_head=is_active_head,
            )
        if _failpoint is not None:
            _failpoint("after_projection")
        after_hash = self.projection_hash(belief_id)
        effect = self.state_store.record_effect_receipt(
            str(command["command_id"]),
            status="committed",
            target_effect_id=projection_effect_id,
            before_hash=before_hash,
            after_hash=after_hash,
            evidence_refs=(
                f"belief-command:{command['command_id']}",
                f"belief-revision:{revision_id}",
                f"graph-projection:{after_hash}",
            ),
            outcome="belief graph projection committed",
            created_at=revision.created_at,
        )
        if _failpoint is not None:
            _failpoint("after_receipt")
        return effect

    def suppress_inactive_heads(self, *, now: datetime | None = None) -> int:
        """Mark projected heads stale when canonical validity no longer permits use."""

        return self.reconcile_validity(now=now)["suppressed"]

    def reconcile_validity(self, *, now: datetime | None = None) -> dict[str, int]:
        """Refresh only already-projected head visibility from immutable valid time.

        Missing graph relations are intentionally not synthesized here: their
        projection command must remain pending until it earns an effect receipt.
        """

        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        relations = self.graph_store.get_relations(
            relation_type="current_belief_revision",
            include_stale=False,
            limit=10000,
        )
        current_revisions = tuple(self.state_store.current_revisions(object_type="belief_revision"))
        validity_snapshot = {
            "schema_version": "mnemos.belief_validity_graph_snapshot.v1",
            "evaluated_at": current_time.isoformat(),
            "active_graph_heads": [
                {
                    "relation_id": relation.id,
                    "source": relation.source,
                    "target": relation.target,
                }
                for relation in relations
            ],
            "canonical_heads": [
                {
                    "belief_id": revision.object_id,
                    "revision_id": revision.revision_id,
                    "payload_hash": revision.payload_hash,
                    "valid_from": revision.payload.get("valid_from", ""),
                    "valid_until": revision.payload.get("valid_until", ""),
                }
                for revision in current_revisions
            ],
        }
        validity_hash = sha256_json(validity_snapshot)

        def resolve_validity_action(
            request: MaterialActionRequest,
        ) -> MaterialActionAuthorization:
            """Authorize one graph-validity action against this frozen snapshot."""

            return self._authorize_graph_action(
                request,
                source_namespace="belief-graph-validity",
                source_facts={
                    **validity_snapshot,
                    "validity_snapshot_hash": validity_hash,
                },
                evidence_refs=(f"belief-validity-snapshot:{validity_hash}",),
                created_at=current_time.isoformat(),
                task="Reconcile projected belief-head validity",
                goal=(
                    "Expose exactly the canonical belief heads active at the "
                    "evaluated validity instant."
                ),
            )

        suppressed = 0
        activated = 0
        with material_action_resolution_scope(resolve_validity_action):
            for relation in relations:
                if not relation.source.startswith("belief://") or not relation.target.startswith(
                    "belief-revision://"
                ):
                    if self.graph_store.mark_stale(relation.id):
                        suppressed += 1
                    continue
                belief_id = relation.source.split("://", 1)[1]
                revision_id = relation.target.split("://", 1)[1]
                current = self.state_store.current_revision(
                    "belief_revision",
                    belief_id,
                )
                if (
                    current is None
                    or current.revision_id != revision_id
                    or not _revision_is_active(current.payload, current_time)
                ) and self.graph_store.mark_stale(relation.id):
                    suppressed += 1
            for current in current_revisions:
                if not _revision_is_active(current.payload, current_time):
                    continue
                belief_urn = _belief_urn(current.object_id)
                revision_urn = _revision_urn(current.revision_id)
                projected = self.graph_store.get_relations(
                    source=belief_urn,
                    target=revision_urn,
                    relation_type="current_belief_revision",
                    include_stale=True,
                    limit=2,
                )
                if not projected or not projected[0].stale:
                    continue
                self.graph_store.add_relations_atomic(
                    [
                        {
                            "source": belief_urn,
                            "target": revision_urn,
                            "relation_type": "current_belief_revision",
                            "strength": 1.0,
                            "confidence": 1.0,
                            "source_layer": "belief",
                            "target_layer": "belief_revision",
                            "access_control": current.payload["access_control"],
                        }
                    ]
                )
                activated += 1
        return {"activated": activated, "suppressed": suppressed}

    def projection_hash(self, belief_id: str) -> str:
        """Hash one belief's graph view without timestamps or semantic body bytes."""

        belief_urn = _belief_urn(_required(belief_id, "belief_id"))
        with self.graph_store._conn() as conn:  # noqa: SLF001
            head_rows = conn.execute(
                """
                SELECT id, source, target, relation_type, strength, confidence,
                       source_layer, target_layer, stale, access_control
                FROM cognitive_relations
                WHERE source=? AND relation_type='current_belief_revision'
                ORDER BY id
                """,
                (belief_urn,),
            ).fetchall()
            revision_urns = tuple(sorted(str(row["target"]) for row in head_rows))
            relation_rows = list(head_rows)
            if revision_urns:
                placeholders = ",".join("?" for _ in revision_urns)
                relation_rows.extend(
                    conn.execute(
                        f"""
                        SELECT id, source, target, relation_type, strength, confidence,
                               source_layer, target_layer, stale, access_control
                        FROM cognitive_relations
                        WHERE (source IN ({placeholders}) OR target IN ({placeholders}))
                          AND NOT (source=? AND relation_type='current_belief_revision')
                        ORDER BY id
                        """,  # nosec B608 - placeholders are generated, values are bound
                        (*revision_urns, *revision_urns, belief_urn),
                    ).fetchall()
                )
            node_rows = conn.execute("""
                SELECT canonical_id, canonical_name, aliases, source_ids, access_control
                FROM canonical_nodes
                WHERE source_ids LIKE '%belief://%'
                   OR source_ids LIKE '%belief-revision://%'
                ORDER BY canonical_id
                """).fetchall()
        relevant_urns = {belief_urn, *revision_urns}
        nodes: list[dict[str, Any]] = []
        for row in node_rows:
            source_ids = tuple(json.loads(str(row["source_ids"] or "[]")))
            if not relevant_urns.intersection(source_ids):
                continue
            nodes.append(
                {
                    "canonical_id": str(row["canonical_id"]),
                    "canonical_name": str(row["canonical_name"]),
                    "aliases": json.loads(str(row["aliases"] or "[]")),
                    "source_ids": list(source_ids),
                    "access_control": json.loads(str(row["access_control"] or "{}")),
                }
            )
        relations = [
            {
                "id": str(row["id"]),
                "source": str(row["source"]),
                "target": str(row["target"]),
                "relation_type": str(row["relation_type"]),
                "strength": float(row["strength"]),
                "confidence": float(row["confidence"]),
                "source_layer": str(row["source_layer"] or ""),
                "target_layer": str(row["target_layer"] or ""),
                "stale": int(row["stale"]),
                "access_control": json.loads(str(row["access_control"] or "{}")),
            }
            for row in sorted(relation_rows, key=lambda value: str(value["id"]))
        ]
        return sha256_json({"belief_id": belief_id, "nodes": nodes, "relations": relations})

    def _project_revision(
        self,
        revision: CognitiveStateRevision,
        *,
        access_control: Mapping[str, Any],
        is_active_head: bool,
    ) -> None:
        payload = revision.payload
        belief_urn = _belief_urn(revision.object_id)
        revision_urn = _revision_urn(revision.revision_id)
        self._ensure_node(
            canonical_name=f"belief:{revision.object_id}",
            source_id=belief_urn,
            access_control=access_control,
        )
        self._ensure_node(
            canonical_name=f"belief-revision:{revision.revision_id}",
            source_id=revision_urn,
            access_control=access_control,
        )
        relations: list[dict[str, Any]] = [
            {
                "source": revision_urn,
                "target": belief_urn,
                "relation_type": "revision_of_belief",
                "strength": 1.0,
                "confidence": 1.0,
                "source_layer": "belief_revision",
                "target_layer": "belief",
                "access_control": access_control,
            }
        ]
        if revision.supersedes_revision_id:
            relations.append(
                {
                    "source": revision_urn,
                    "target": _revision_urn(revision.supersedes_revision_id),
                    "relation_type": "supersedes_belief_revision",
                    "strength": 1.0,
                    "confidence": 1.0,
                    "source_layer": "belief_revision",
                    "target_layer": "belief_revision",
                    "access_control": access_control,
                }
            )
        if revision.correction_of_revision_id:
            relations.append(
                {
                    "source": revision_urn,
                    "target": _revision_urn(revision.correction_of_revision_id),
                    "relation_type": "corrects_belief_revision",
                    "strength": 1.0,
                    "confidence": 1.0,
                    "source_layer": "belief_revision",
                    "target_layer": "belief_revision",
                    "access_control": access_control,
                }
            )
        for relation_type, evidence_refs in (
            ("supported_by_evidence", payload["supporting_evidence"]),
            ("opposed_by_evidence", payload["opposing_evidence"]),
            ("withdrew_evidence", payload["withdrawn_evidence"]),
        ):
            relations.extend(
                {
                    "source": revision_urn,
                    "target": _evidence_urn(str(evidence_ref)),
                    "relation_type": relation_type,
                    "strength": 1.0,
                    "confidence": 1.0,
                    "source_layer": "belief_revision",
                    "target_layer": "evidence",
                    "access_control": access_control,
                }
                for evidence_ref in evidence_refs
            )
        self._ensure_relations(relations)

        existing_heads = self.graph_store.get_relations(
            source=belief_urn,
            relation_type="current_belief_revision",
            include_stale=False,
            limit=10000,
        )
        for existing in existing_heads:
            if existing.target != revision_urn or not is_active_head:
                self.graph_store.mark_stale(existing.id)
        head_relation = {
            "source": belief_urn,
            "target": revision_urn,
            "relation_type": "current_belief_revision",
            "strength": 1.0,
            "confidence": 1.0,
            "source_layer": "belief",
            "target_layer": "belief_revision",
            "access_control": access_control,
        }
        projected_head = self._ensure_relations(
            [head_relation],
            desired_stale=not is_active_head,
        )[0]
        if not is_active_head and not projected_head.stale:
            self.graph_store.mark_stale(projected_head.id)

    def _ensure_node(
        self,
        *,
        canonical_name: str,
        source_id: str,
        access_control: Mapping[str, Any],
    ) -> None:
        canonical_id = self.graph_store._canonical_id(canonical_name)  # noqa: SLF001
        expected_access = _projection_access(access_control)
        existing = self.graph_store.get_canonical_node(canonical_id)
        if existing is not None:
            if (
                existing.canonical_name == canonical_name
                and source_id in existing.source_ids
                and (
                    cognitive_access_hash(existing.access_control)
                    == cognitive_access_hash(expected_access)
                    or (
                        canonical_name.startswith("belief:")
                        and _same_access_boundary(
                            existing.access_control,
                            expected_access,
                        )
                    )
                )
            ):
                return
            raise RuntimeError("belief projection node conflicts with existing graph state")
        self.graph_store.add_canonical_node(
            canonical_name=canonical_name,
            canonical_id=canonical_id,
            source_ids=[source_id],
            access_control=access_control,
        )

    def _ensure_relations(
        self,
        relations: Sequence[Mapping[str, Any]],
        *,
        desired_stale: bool = False,
    ) -> list[Any]:
        results: list[Any] = []
        missing: list[dict[str, Any]] = []
        for raw_relation in relations:
            relation = dict(raw_relation)
            relation_id = _projection_relation_id(
                str(relation["source"]),
                str(relation["target"]),
                str(relation["relation_type"]),
            )
            existing = self.graph_store.get_relation(relation_id)
            if existing is None:
                missing.append(relation)
                continue
            expected_access = _projection_access(relation["access_control"])
            if (
                float(existing.strength) != float(relation["strength"])
                or float(existing.confidence) != float(relation["confidence"])
                or existing.source_layer != str(relation["source_layer"])
                or existing.target_layer != str(relation["target_layer"])
                or cognitive_access_hash(existing.access_control)
                != cognitive_access_hash(expected_access)
            ):
                raise RuntimeError("belief projection relation conflicts with existing graph state")
            if bool(existing.stale) != desired_stale:
                if desired_stale:
                    self.graph_store.mark_stale(existing.id)
                    existing = self.graph_store.get_relation(existing.id)
                    assert existing is not None
                else:
                    missing.append(relation)
                    continue
            results.append(existing)
        if missing:
            results.extend(self.graph_store.add_relations_atomic(missing))
        by_id = {relation.id: relation for relation in results}
        return [
            by_id[
                _projection_relation_id(
                    str(relation["source"]),
                    str(relation["target"]),
                    str(relation["relation_type"]),
                )
            ]
            for relation in relations
        ]

    def _verify_event_acl_hash(self, revision: CognitiveStateRevision) -> None:
        with self.state_store._connect(read_only=True) as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT metadata FROM cognitive_data_events WHERE event_id=?",
                (revision.source_event_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("belief projection source event is unavailable")
        try:
            metadata = json.loads(str(row["metadata"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("belief projection event metadata is invalid") from exc
        if (
            metadata.get("revision_ids") != [revision.revision_id]
            or metadata.get("contract_version")
            != COGNITIVE_OBJECT_SCHEMA_VERSIONS["belief_revision"]
            or metadata.get("access_control_hash")
            != cognitive_access_hash(revision.payload["access_control"])
        ):
            raise RuntimeError("belief projection event ACL binding failed")

    def _pending_command(self, command_id: str) -> dict[str, Any]:
        normalized = _required(command_id, "command_id")
        command = next(
            (
                value
                for value in self.state_store.pending_commands(BELIEF_CONSUMER)
                if str(value["command_id"]) == normalized
            ),
            None,
        )
        if command is None:
            raise ValueError("belief projection command is not pending")
        return command


def _normalize_command(command: BeliefRevisionCommand) -> dict[str, Any]:
    if not isinstance(command, BeliefRevisionCommand):
        raise TypeError("command must be BeliefRevisionCommand")
    claim = str(command.claim)
    if not claim.strip():
        raise ValueError("belief claim is required")
    claim_kind = str(command.claim_kind).strip()
    if claim_kind not in _CLAIM_KINDS:
        raise ValueError("unsupported belief claim kind")
    scope_type = _required(command.scope_type, "scope_type")
    scope_id = _required(command.scope_id, "scope_id")
    source_id = _required(command.source_id, "source_id")
    source_revision_id = _required(command.source_revision_id, "source_revision_id")
    source_content_hash = _required(command.source_content_hash, "source_content_hash")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", source_content_hash):
        raise ValueError("source_content_hash must be a full SHA-256")
    created_at = _required(command.created_at, "created_at")
    _timestamp(created_at)
    valid_from = str(command.valid_from or created_at)
    _timestamp(valid_from)
    valid_until = str(command.valid_until or "")
    if valid_until:
        _timestamp(valid_until)
    confidence_method = _required(command.confidence_method, "confidence_method")
    confidence_evidence = _refs(command.confidence_evidence, "confidence_evidence")
    if confidence_method == "unscored":
        if command.confidence is not None or confidence_evidence:
            raise ValueError("unscored belief cannot carry confidence evidence")
        confidence: float | None = None
    else:
        if command.confidence is None or not confidence_evidence:
            raise ValueError("scored belief requires confidence evidence")
        confidence = float(command.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("belief confidence must be between 0 and 1")
    disposition = str(command.disposition or "")
    if disposition not in {"", "deprecated"}:
        raise ValueError("unsupported belief disposition")
    invalidation_conditions = _refs(
        command.invalidation_conditions,
        "invalidation_conditions",
    )
    if not invalidation_conditions:
        raise ValueError("belief invalidation conditions are required")
    source_span_ids = _refs(command.source_span_ids, "source_span_ids")
    for span_id in source_span_ids:
        match = re.fullmatch(
            re.escape(source_revision_id) + r"#([0-9]+):([0-9]+)",
            span_id,
        )
        if match is None or int(match.group(2)) <= int(match.group(1)):
            raise ValueError("belief source_span_ids must be exact spans of source_revision_id")
    return {
        "claim": claim,
        "claim_kind": claim_kind,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "source_id": source_id,
        "source_revision_id": source_revision_id,
        "source_content_hash": source_content_hash,
        "source_span_ids": source_span_ids,
        "supporting_evidence": _refs(command.supporting_evidence, "supporting_evidence"),
        "opposing_evidence": _refs(command.opposing_evidence, "opposing_evidence"),
        "withdrawn_evidence": _refs(command.withdrawn_evidence, "withdrawn_evidence"),
        "confidence_method": confidence_method,
        "confidence": confidence,
        "confidence_evidence": confidence_evidence,
        "uncertainty_reasons": _refs(command.uncertainty_reasons, "uncertainty_reasons"),
        "valid_from": valid_from,
        "valid_until": valid_until,
        "invalidation_conditions": invalidation_conditions,
        "expected_current_revision_id": str(command.expected_current_revision_id or ""),
        "correction_of_revision_id": str(command.correction_of_revision_id or ""),
        "correction_evidence_ref": str(command.correction_evidence_ref or "").strip(),
        "disposition": disposition,
        "proposal_id": str(command.proposal_id or ""),
        "journal_id": str(command.journal_id or ""),
        "created_at": created_at,
    }


def _belief_identity(claim: str, scope_type: str, scope_id: str) -> tuple[str, str]:
    comparison = " ".join(unicodedata.normalize("NFKC", claim).split())
    claim_id = "claim-" + _digest(comparison)[:32]
    belief_id = (
        "belief-"
        + _digest({"scope_type": scope_type, "scope_id": scope_id, "claim_id": claim_id})[:32]
    )
    return claim_id, belief_id


def _belief_event_id(
    normalized: Mapping[str, Any],
    *,
    claim_id: str,
    belief_id: str,
) -> str:
    semantic = {
        key: value for key, value in normalized.items() if key != "expected_current_revision_id"
    }
    semantic.update({"claim_id": claim_id, "belief_id": belief_id})
    return "belief-event-" + _digest(semantic)[:32]


def _derive_stance(
    supporting: set[str],
    opposing: set[str],
    *,
    disposition: str,
) -> str:
    if disposition == "deprecated":
        return "deprecated"
    if supporting and opposing:
        return "disputed"
    if supporting:
        return "supported"
    if opposing:
        return "refuted"
    return "unknown"


def _refs(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field_name} must be a sequence")
    normalized = tuple(sorted(set(str(value).strip() for value in values)))
    if any(not value for value in normalized):
        raise ValueError(f"{field_name} contains a blank item")
    return normalized


def _required(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("belief timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _revision_is_active(payload: Mapping[str, Any], now: datetime) -> bool:
    if str(payload["stance"]) in {"unknown", "deprecated"}:
        return False
    if now < _timestamp(payload["valid_from"]):
        return False
    return not payload["valid_until"] or now < _timestamp(payload["valid_until"])


def _same_access_boundary(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    first = validate_cognitive_access_envelope(left)
    second = validate_cognitive_access_envelope(right)
    fields = (
        "schema_version",
        "owner",
        "scope",
        "purposes",
        "sensitivity",
        "retention_policy",
        "redaction_policy",
        "visibility",
        "declassification",
    )
    return all(first[field] == second[field] for field in fields) and (
        first["consent"]["status"] == second["consent"]["status"]
    )


def _projection_access(source: Mapping[str, Any]) -> dict[str, Any]:
    normalized = validate_cognitive_access_envelope(source)
    return derive_strictest_cognitive_access(
        (normalized,),
        owner_principal_id=str(normalized["owner"]["principal_id"]),
        owner_agent=str(normalized["owner"]["agent"]),
        scope_type=str(normalized["scope"]["scope_type"]),
        scope_id=str(normalized["scope"]["scope_id"]),
        purposes=("cognitive_graph_read",),
        retention_policy="cognitive_graph_retention",
    )


def _projection_relation_id(source: str, target: str, relation_type: str) -> str:
    material = f"{source}|{target}|{relation_type}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _belief_urn(belief_id: str) -> str:
    return f"belief://{belief_id}"


def _revision_urn(revision_id: str) -> str:
    return f"belief-revision://{revision_id}"


def _evidence_urn(evidence_ref: str) -> str:
    return f"evidence://{_digest(evidence_ref)}"


def _digest(value: Any) -> str:
    if isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = canonical_json(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "BELIEF_COMMAND_TYPE",
    "BELIEF_CONSUMER",
    "BeliefExplanation",
    "BeliefRevisionCommand",
    "BeliefRevisionProjector",
    "BeliefRevisionReceipt",
    "BeliefRevisionStore",
]
