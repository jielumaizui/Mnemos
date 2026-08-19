"""Production EventBus consumers for Wiki projection endpoints."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from core.cognitive.decision_trace import (
    DecisionCandidateEvaluation,
    DecisionRejectionEvaluation,
    MaterialActionAuthorization,
    MaterialActionObservation,
    MaterialActionPermit,
    MaterialActionRequest,
    ProjectContractDecisionContext,
    ProjectContractDecisionEvaluation,
    ProjectContractMaterialActionResolver,
    material_action_resolution_scope,
    require_material_action,
)
from core.cognitive.state_contract import sha256_json
from core.kia.kg_event_handler import KGEventHandler
from core.mnemos_bus import HandlerOutcome
from core.wiki_projection_lifecycle import WikiProjectionLedger


WIKI_PROJECTION_DECISION_CONTRACT_ID = (
    "project-contract:wiki-projection-required-consumers"
)
WIKI_PROJECTION_DECISION_CONTRACT_REVISION = (
    "mnemos.wiki_projection_material_effects.v1"
)
WIKI_PROJECTION_DECISION_CONTRACT_TEXT = (
    "A durable Wiki lifecycle mutation must reach every required consumer "
    "through exact pre action DecisionTrace commands."
)
WIKI_PROJECTION_HANDLER_CODE_HASH = sha256_json(
    {
        "module": "daemon.wiki_projection_handlers",
        "producer": "register_wiki_projection_handlers",
        "version": WIKI_PROJECTION_DECISION_CONTRACT_REVISION,
    }
)
WIKI_PROJECTION_MATERIAL_ACTION = "project_wiki_consumer"
WIKI_PROJECTION_MATERIAL_OWNER = "wiki_projection"
WIKI_PROJECTION_CONSUMERS = frozenset(
    {
        "knowledge_graph",
        "cognitive_graph",
        "relation_embeddings",
        "wiki_search_index",
        "wiki_metrics",
        "moc_navigation",
    }
)


def _outcome_payload(outcome: HandlerOutcome) -> dict[str, Any]:
    return {
        "disposition": outcome.disposition,
        "consumer": outcome.consumer,
        "reason": outcome.reason,
        "metadata": dict(outcome.metadata),
    }


def _outcome_from_payload(payload: dict[str, Any]) -> HandlerOutcome:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise RuntimeError("Wiki projection material outcome metadata is malformed")
    return HandlerOutcome(
        disposition=str(payload.get("disposition") or ""),
        consumer=str(payload.get("consumer") or ""),
        reason=str(payload.get("reason") or ""),
        metadata=dict(metadata),
    )


@dataclass
class _ProjectionEffectSession:
    recovered_outcome: HandlerOutcome | None
    authorization: MaterialActionAuthorization
    request: MaterialActionRequest
    _complete: Callable[[HandlerOutcome], None] | None = field(
        default=None,
        repr=False,
    )
    completed: bool = False

    def complete(self, outcome: HandlerOutcome) -> None:
        """Commit one non-recovered projection outcome exactly once."""

        if self.recovered_outcome is not None:
            raise RuntimeError("a recovered Wiki projection cannot execute again")
        if self.completed:
            raise RuntimeError("Wiki projection outcome was already completed")
        if self._complete is None:
            raise RuntimeError("Wiki projection completion callback is unavailable")
        self._complete(outcome)
        self.completed = True


class WikiProjectionEffectOracle:
    """Observe the exact at-most-once consumer effect journal and target."""

    owner = WIKI_PROJECTION_MATERIAL_OWNER
    action_type = WIKI_PROJECTION_MATERIAL_ACTION

    def __init__(
        self,
        *,
        ledger: WikiProjectionLedger,
        consumer: str,
        source_id: str,
        target_paths: tuple[Path, ...],
        target_ref: str,
        input_hash: str,
    ):
        self.ledger = ledger
        self.executor_id = str(consumer)
        self.consumer = str(consumer)
        self.source_id = str(source_id)
        self.target_paths = tuple(target_paths)
        self.target_ref = str(target_ref)
        self.input_hash = str(input_hash)
        self.last_outcome: HandlerOutcome | None = None

    def observe(
        self,
        permit: MaterialActionPermit,
    ) -> MaterialActionObservation | None:
        """Return the exact committed projection effect for recovery."""

        if (
            permit.target_ref != self.target_ref
            or permit.input_hash != self.input_hash
        ):
            raise PermissionError(
                "Wiki projection oracle does not match the exact command"
            )
        row = self.ledger.material_projection_effect(permit.effect_id)
        if row is None:
            self.last_outcome = None
            return None
        if (
            str(row["command_id"]) != permit.command_id
            or str(row["source_id"]) != self.source_id
            or str(row["consumer"]) != self.consumer
            or str(row["target_ref"]) != self.target_ref
            or str(row["input_hash"]) != self.input_hash
        ):
            raise RuntimeError(
                "Wiki projection material journal does not match its command"
            )
        try:
            outcome_payload = json.loads(str(row["outcome_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "Wiki projection material outcome is malformed"
            ) from exc
        if not isinstance(outcome_payload, dict):
            raise RuntimeError("Wiki projection material outcome must be an object")
        if sha256_json(outcome_payload) != str(row["outcome_hash"]):
            raise RuntimeError("Wiki projection material outcome hash mismatch")
        status = str(row["status"])
        before_hash = str(row["before_hash"])
        after_hash = str(row["after_hash"])
        current_hash = _projection_state_hash(self.target_paths)
        evidence = [
            f"target-oracle:{permit.effect_id}:wiki-projection:{self.consumer}",
            f"target-journal:wiki-projection:{permit.effect_id}",
        ]
        if status == "retryable":
            if current_hash != before_hash or after_hash != before_hash:
                raise RuntimeError(
                    "retryable Wiki projection has an unproven target delta"
                )
            self.last_outcome = None
            return None
        if status == "executing":
            if current_hash != before_hash:
                raise RuntimeError(
                    "Wiki projection crashed after an ambiguous target delta; "
                    "manual reconciliation is required"
                )
            self.last_outcome = HandlerOutcome.dead(
                self.consumer,
                "projection outcome unknown after crash",
                material_effect_id=permit.effect_id,
            )
            return MaterialActionObservation(
                status="dead_letter",
                before_hash=before_hash,
                after_hash=before_hash,
                evidence_refs=tuple(
                    (
                        *evidence,
                        f"attempted-effect:{permit.effect_id}",
                        f"retry-budget-exhausted:{permit.command_id}",
                    )
                ),
                reason_code="wiki_projection_outcome_unknown_after_crash",
                retry_exhausted=True,
                outcome="ambiguous Wiki projection was not executed twice",
                observed_at=str(row["started_at"]),
            )
        if current_hash != after_hash:
            raise RuntimeError("Wiki projection target drifted from its effect journal")
        self.last_outcome = _outcome_from_payload(outcome_payload)
        if status == "committed":
            evidence.append(f"target-after:{after_hash}")
            return MaterialActionObservation(
                status="committed",
                before_hash=before_hash,
                after_hash=after_hash,
                evidence_refs=tuple(evidence),
                outcome=f"Wiki projection {self.consumer} committed",
                observed_at=str(row["completed_at"]),
            )
        if status != "dead_letter" or before_hash != after_hash:
            raise RuntimeError("unsupported Wiki projection material status")
        evidence.extend(
            (
                f"attempted-effect:{permit.effect_id}",
                f"retry-budget-exhausted:{permit.command_id}",
            )
        )
        return MaterialActionObservation(
            status="dead_letter",
            before_hash=before_hash,
            after_hash=after_hash,
            evidence_refs=tuple(evidence),
            reason_code=str(row["reason_code"] or "projection_dead_letter"),
            retry_exhausted=True,
            outcome=f"Wiki projection {self.consumer} dead-lettered",
            observed_at=str(row["completed_at"]),
        )


def _projection_state_hash(paths: tuple[Path, ...]) -> str:
    manifest: list[dict[str, Any]] = []
    for root in paths:
        resolved = Path(root).expanduser().resolve(strict=False)
        candidates = (
            tuple(path for path in resolved.rglob("*") if path.is_file())
            if resolved.is_dir()
            else tuple(
                path
                for path in (
                    resolved,
                    Path(str(resolved) + "-wal"),
                    Path(str(resolved) + "-shm"),
                )
                if path.is_file()
            )
        )
        if not candidates:
            manifest.append({"path": str(resolved), "state": "absent"})
            continue
        for path in sorted(candidates, key=lambda item: str(item)):
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            manifest.append(
                {
                    "path": str(path),
                    "sha256": "sha256:" + digest.hexdigest(),
                    "size": path.stat().st_size,
                }
            )
    return str(sha256_json(manifest))


def wiki_projection_material_action_binding(
    *,
    source_id: str,
    source_hash: str,
    event_type: str,
    consumer: str,
) -> dict[str, str]:
    """Bind one required consumer projection to its source revision."""

    return {
        "target_ref": f"wiki-projection:{source_id}:{consumer}",
        "input_hash": sha256_json(
            {
                "schema_version": "mnemos.wiki_projection_material_input.v1",
                "source_id": source_id,
                "source_hash": source_hash,
                "event_type": event_type,
                "consumer": consumer,
            }
        ),
    }


def _validate_projection_authorization(
    authorization: MaterialActionAuthorization,
    request: MaterialActionRequest,
) -> MaterialActionPermit:
    if not isinstance(authorization, MaterialActionAuthorization):
        raise PermissionError(
            "Wiki projection requires canonical material authorization"
        )
    permit = authorization.permit
    actual_store = Path(
        authorization.coordinator.state_store.db_path
    ).expanduser().resolve(strict=False)
    expected_store = Path(str(request.expected_state_db)).expanduser().resolve(
        strict=False
    )
    if (
        permit.owner != request.owner
        or permit.executor_id != request.executor_id
        or permit.action_type != request.action_type
        or permit.target_ref != request.target_ref
        or permit.input_hash != request.input_hash
        or actual_store != expected_store
    ):
        raise PermissionError(
            "Wiki projection authorization does not match its exact request"
        )
    return permit


def _projection_target_paths(
    consumer: str,
    *,
    database_dir: Path,
    embedding_index_dir: Path,
    wiki_dir: Path,
) -> tuple[Path, ...]:
    """Return the complete durable target set owned by one projection."""

    return {
        "knowledge_graph": (
            database_dir / "knowledge_graph.db",
            database_dir / "blindspots.db",
            wiki_dir / "L2.4-KG",
        ),
        "cognitive_graph": (database_dir / "cognitive_graph.db",),
        "relation_embeddings": (
            database_dir / "knowledge_graph.db",
            embedding_index_dir,
        ),
        "wiki_search_index": (embedding_index_dir,),
        "wiki_metrics": (database_dir / "wiki_metrics.db",),
        "moc_navigation": (wiki_dir,),
    }[consumer]


def register_wiki_projection_handlers(
    event_bus: Any,
    config: Any,
    *,
    embedding_client: Any | None = None,
    projection_lifecycle: Any | None = None,
) -> None:
    """Register KG, embedding, navigation, search, and metrics consumers."""

    database_dir = Path(config.database_dir).expanduser()
    wiki_dir = Path(config.wiki_dir).expanduser()
    embedding_index_dir = database_dir / "embedding_index"
    kg_handler = KGEventHandler(
        db_path=database_dir / "knowledge_graph.db",
        wiki_base=wiki_dir,
        embedding_index_dir=embedding_index_dir,
        embedding_client=embedding_client,
        config=config,
        projection_lifecycle=projection_lifecycle,
    )

    @contextmanager
    def _material_projection_scope(
        event,
        consumer: str,
        *,
        nested_actions: tuple[MaterialActionRequest, ...] = (),
    ):
        payload = dict(event.payload or {})
        mutation_id = str(payload.get("mutation_id") or "")
        evidence_refs: tuple[str, ...]
        if event.event_type == "wiki_page_updated":
            receipt = WikiProjectionLedger(
                database_dir / "wiki_projection.db"
            ).mutation_receipt(mutation_id)
            if receipt is None:
                raise PermissionError(
                    "Wiki projection material decision requires a durable mutation"
                )
            expected = {
                "page_path": receipt.page_path,
                "previous_path": receipt.previous_path,
                "page_id": receipt.page_id,
                "page_revision": receipt.page_revision,
                "mutation_id": receipt.mutation_id,
                "mutation_type": receipt.mutation_type,
                "tombstone": receipt.tombstone,
            }
            drift = [key for key, value in expected.items() if payload.get(key) != value]
            if drift:
                raise PermissionError(
                    "Wiki projection event drifted from its lifecycle mutation: "
                    + ", ".join(sorted(drift))
                )
            source_id = f"wiki-mutation:{receipt.mutation_id}"
            source_revision_id = f"wiki-page-revision:{receipt.page_revision}"
            source_hash = sha256_json(receipt.to_dict())
            source_uri = f"wiki-mutation://{receipt.mutation_id}"
            created_at = receipt.created_at
            evidence_refs = (
                source_id,
                source_revision_id,
                f"wiki-page:{receipt.page_id}",
            )
            scope_prefix = source_id
        else:
            source_hash = sha256_json(
                {
                    "event_type": event.event_type,
                    "source": event.source,
                    "trace_id": event.trace_id,
                    "payload": payload,
                }
            )
            source_id = f"event:{event.trace_id}"
            source_revision_id = f"event-payload:{source_hash}"
            source_uri = f"event://{event.event_type}/{event.trace_id}"
            created_at = event.timestamp
            evidence_refs = (source_id, source_revision_id)
            scope_prefix = source_id
        projection_binding = wiki_projection_material_action_binding(
            source_id=source_id,
            source_hash=source_hash,
            event_type=event.event_type,
            consumer=consumer,
        )
        projection_request = MaterialActionRequest(
            owner=WIKI_PROJECTION_MATERIAL_OWNER,
            executor_id=consumer,
            action_type=WIKI_PROJECTION_MATERIAL_ACTION,
            target_ref=projection_binding["target_ref"],
            input_hash=projection_binding["input_hash"],
            expected_state_db=str(
                database_dir / "producer_consumer_ledger.db"
            ),
        )
        allowed_material_actions = tuple(
            _material_action_fact(request)
            for request in (projection_request, *nested_actions)
        )
        source_facts = {
            "schema_version": "mnemos.wiki_projection_evaluation_facts.v1",
            "event_type": event.event_type,
            "source_id": source_id,
            "source_revision_id": source_revision_id,
            "source_hash": source_hash,
            "evidence_refs": list(evidence_refs),
            "consumer": consumer,
            "allowed_material_actions": [
                dict(value) for value in allowed_material_actions
            ],
        }
        source_facts_hash = sha256_json(source_facts)

        def evaluate_request(
            request: MaterialActionRequest,
        ) -> ProjectContractDecisionEvaluation:
            """Admit only projection actions declared by this lifecycle event."""

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
            approved_key = "reconcile_verified_wiki_projection"
            rejected_key = "reject_projection_outside_lifecycle_event"
            common_refs = (request_ref, facts_ref, *evidence_refs)
            return ProjectContractDecisionEvaluation(
                request_binding_hash=request_hash,
                source_facts_hash=source_facts_hash,
                candidates=(
                    DecisionCandidateEvaluation(
                        key=approved_key,
                        summary=(
                            "Reconcile the lifecycle bound consumer projection."
                        ),
                        supporting_evidence=common_refs if approved else (),
                        opposing_evidence=() if approved else common_refs,
                        satisfies_value_keys=("safety", "project_contract"),
                    ),
                    DecisionCandidateEvaluation(
                        key=rejected_key,
                        summary=(
                            "Reject a projection outside the verified lifecycle event."
                        ),
                        supporting_evidence=common_refs if not approved else (),
                        opposing_evidence=() if not approved else common_refs,
                        satisfies_value_keys=("safety",),
                    ),
                ),
                selection_key=approved_key if approved else rejected_key,
                rejections=(
                    DecisionRejectionEvaluation(
                        candidate_key=rejected_key if approved else approved_key,
                        reason_code=(
                            "lifecycle_projection_binding_verified"
                            if approved
                            else "lifecycle_projection_binding_rejected"
                        ),
                        evidence_refs=common_refs,
                    ),
                ),
                expected_outcomes=(
                    {
                        "metric": (
                            "required_consumer_projection_receipt"
                            if approved
                            else "unbound_projection_effect_count"
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
                state_db_path=database_dir / "producer_consumer_ledger.db",
                contract_id=WIKI_PROJECTION_DECISION_CONTRACT_ID,
                contract_revision_id=WIKI_PROJECTION_DECISION_CONTRACT_REVISION,
                contract_text=WIKI_PROJECTION_DECISION_CONTRACT_TEXT,
                contract_evidence_ref=(
                    f"{WIKI_PROJECTION_DECISION_CONTRACT_ID}"
                    f"#{WIKI_PROJECTION_DECISION_CONTRACT_REVISION}"
                ),
                source_id=source_id,
                source_revision_id=source_revision_id,
                source_content_hash=source_hash,
                source_uri=source_uri,
                evidence_refs=evidence_refs,
                task=f"Project {event.event_type} into required consumers",
                goal=(
                    "Bring every durable Wiki projection consumer to the exact "
                    "state required by the authoritative lifecycle event."
                ),
                constraints=(
                    "The event must match its append-only lifecycle mutation.",
                    "Each material consumer effect must carry an exact permit.",
                ),
                created_at=created_at,
                scope_prefix=scope_prefix,
                producer="wiki-projection-handlers",
                producer_version=WIKI_PROJECTION_DECISION_CONTRACT_REVISION,
                producer_code_hash=WIKI_PROJECTION_HANDLER_CODE_HASH,
                evaluator_id="wiki-lifecycle-projection-evaluator",
                evaluator=evaluate_request,
            )
        )
        authorization = resolver(projection_request)
        permit = _validate_projection_authorization(
            authorization,
            projection_request,
        )
        target_paths = _projection_target_paths(
            consumer,
            database_dir=database_dir,
            embedding_index_dir=embedding_index_dir,
            wiki_dir=wiki_dir,
        )
        projection_ledger = WikiProjectionLedger(
            database_dir / "wiki_projection.db"
        )
        oracle = WikiProjectionEffectOracle(
            ledger=projection_ledger,
            consumer=consumer,
            source_id=source_id,
            target_paths=target_paths,
            target_ref=projection_request.target_ref,
            input_hash=projection_request.input_hash,
        )
        recovered = authorization.recover(oracle)
        if recovered is not None:
            if oracle.last_outcome is None and oracle.observe(permit) is None:
                raise RuntimeError(
                    "terminal Wiki projection lacks its target-local outcome"
                )
            recovered_session = _ProjectionEffectSession(
                recovered_outcome=oracle.last_outcome,
                authorization=authorization,
                request=projection_request,
                completed=True,
            )
            with material_action_resolution_scope(resolver):
                yield recovered_session
            return

        permit = require_material_action(
            authorization,
            owner=projection_request.owner,
            executor_id=projection_request.executor_id,
            action_type=projection_request.action_type,
            target_ref=projection_request.target_ref,
            input_hash=projection_request.input_hash,
            expected_state_db=projection_request.expected_state_db,
        )
        before_hash = _projection_state_hash(target_paths)
        projection_ledger.begin_material_projection_effect(
            effect_id=permit.effect_id,
            command_id=permit.command_id,
            source_id=source_id,
            consumer=consumer,
            target_ref=projection_request.target_ref,
            input_hash=projection_request.input_hash,
            before_hash=before_hash,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        def complete_projection(outcome: HandlerOutcome) -> None:
            """Commit the projection outcome and terminal material receipt."""

            if not isinstance(outcome, HandlerOutcome):
                raise TypeError("Wiki projection requires a typed HandlerOutcome")
            if outcome.consumer and outcome.consumer != consumer:
                raise ValueError("Wiki projection outcome belongs to another consumer")
            normalized = outcome if outcome.consumer else HandlerOutcome(
                disposition=outcome.disposition,
                consumer=consumer,
                reason=outcome.reason,
                metadata=dict(outcome.metadata),
            )
            after_hash = _projection_state_hash(target_paths)
            terminal = normalized.disposition not in {"retry", "defer"}
            effect_status = (
                "retryable"
                if not terminal
                else "dead_letter"
                if normalized.disposition == "dead"
                else "committed"
            )
            projection_ledger.finalize_material_projection_effect(
                effect_id=permit.effect_id,
                status=effect_status,
                after_hash=after_hash,
                reason_code=(
                    normalized.reason or normalized.disposition
                    if effect_status != "committed"
                    else ""
                ),
                outcome=_outcome_payload(normalized),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            if terminal:
                terminal_receipt = authorization.recover(oracle)
                expected_status = (
                    "dead_letter" if effect_status == "dead_letter" else "committed"
                )
                if (
                    terminal_receipt is None
                    or terminal_receipt.status != expected_status
                ):
                    raise RuntimeError(
                        "Wiki projection effect journal did not close its command"
                    )

        session = _ProjectionEffectSession(
            recovered_outcome=None,
            authorization=authorization,
            request=projection_request,
            _complete=complete_projection,
        )
        with material_action_resolution_scope(resolver):
            yield session
        if not session.completed:
            raise RuntimeError(
                "Wiki projection handler returned without a typed material outcome"
            )

    def _kg_handler(event):
        plan = kg_handler.plan_on_distilled(event.payload)
        nested_actions = kg_handler.material_action_requests(
            plan,
            state_db_path=database_dir / "producer_consumer_ledger.db",
        )
        with _material_projection_scope(
            event,
            "knowledge_graph",
            nested_actions=nested_actions,
        ) as projection:
            if projection.recovered_outcome is not None:
                return projection.recovered_outcome
            require_material_action(
                projection.authorization,
                owner=projection.request.owner,
                executor_id=projection.request.executor_id,
                action_type=projection.request.action_type,
                target_ref=projection.request.target_ref,
                input_hash=projection.request.input_hash,
                expected_state_db=projection.request.expected_state_db,
            )
            outcome = HandlerOutcome.from_result(
                kg_handler.on_distilled(event.payload, material_plan=plan),
                consumer="knowledge_graph",
            )
            projection.complete(outcome)
            return outcome

    def _kg_page_updated_handler(event):
        plan = kg_handler.plan_on_page_updated(event.payload)
        nested_actions = kg_handler.material_action_requests(
            plan,
            state_db_path=database_dir / "producer_consumer_ledger.db",
        )
        with _material_projection_scope(
            event,
            "knowledge_graph",
            nested_actions=nested_actions,
        ) as projection:
            if projection.recovered_outcome is not None:
                return projection.recovered_outcome
            require_material_action(
                projection.authorization,
                owner=projection.request.owner,
                executor_id=projection.request.executor_id,
                action_type=projection.request.action_type,
                target_ref=projection.request.target_ref,
                input_hash=projection.request.input_hash,
                expected_state_db=projection.request.expected_state_db,
            )
            outcome = HandlerOutcome.from_result(
                kg_handler.on_page_updated(event.payload, material_plan=plan),
                consumer="knowledge_graph",
            )
            projection.complete(outcome)
            return outcome

    def _metrics_page_updated_handler(event):
        from core.wiki_metrics import WikiMetrics

        with _material_projection_scope(event, "wiki_metrics") as projection:
            if projection.recovered_outcome is not None:
                return projection.recovered_outcome
            require_material_action(
                projection.authorization,
                owner=projection.request.owner,
                executor_id=projection.request.executor_id,
                action_type=projection.request.action_type,
                target_ref=projection.request.target_ref,
                input_hash=projection.request.input_hash,
                expected_state_db=projection.request.expected_state_db,
            )
            with WikiMetrics(
                db_path=str(database_dir / "wiki_metrics.db"),
                wiki_dir=str(wiki_dir),
            ) as metrics:
                result = metrics.reconcile_page_lifecycle(
                    page_path=str(event.payload.get("page_path") or ""),
                    previous_path=str(event.payload.get("previous_path") or ""),
                    mutation_type=str(event.payload.get("mutation_type") or "update"),
                )
            outcome = HandlerOutcome.from_result(result, consumer="wiki_metrics")
            projection.complete(outcome)
            return outcome

    def _relation_embeddings_handler(event):
        from core.kia.knowledge_graph import KnowledgeGraph

        with _material_projection_scope(
            event,
            "relation_embeddings",
        ) as projection:
            if projection.recovered_outcome is not None:
                return projection.recovered_outcome
            require_material_action(
                projection.authorization,
                owner=projection.request.owner,
                executor_id=projection.request.executor_id,
                action_type=projection.request.action_type,
                target_ref=projection.request.target_ref,
                input_hash=projection.request.input_hash,
                expected_state_db=projection.request.expected_state_db,
            )
            db_path = database_dir / "knowledge_graph.db"
            database_dir.mkdir(parents=True, exist_ok=True)
            kg = KnowledgeGraph(
                db_path=str(db_path),
                wiki_base=str(wiki_dir),
                embedding_index_dir=str(embedding_index_dir),
                embedding_client=embedding_client,
                config=config,
            )
            try:
                repair = kg.repair_relation_embedding_orphans()
                if repair["failed"]:
                    outcome = HandlerOutcome.retry(
                        "relation_embeddings",
                        "relation embedding repair failed",
                        **repair,
                    )
                    projection.complete(outcome)
                    return outcome
                health = kg.audit_relation_embedding_projection()
                if not health["ok"]:
                    rebuilt = kg.rebuild_relation_index()
                    health = kg.audit_relation_embedding_projection()
                    if int(rebuilt.get("failed", 0)) or not health["ok"]:
                        outcome = HandlerOutcome.retry(
                            "relation_embeddings",
                            "relation embedding projection is incomplete",
                            rebuild=rebuilt,
                            **health,
                        )
                        projection.complete(outcome)
                        return outcome
                outcome = HandlerOutcome.ack("relation_embeddings", **health)
                projection.complete(outcome)
                return outcome
            finally:
                kg.close()

    def _moc_navigation_handler(event):
        from core.trust.config import load_trusted_push_config
        from core.trust.vault_mutation_service import (
            recover_pending_trusted_markdown_effects,
        )
        from core.wiki_navigation import (
            apply_navigation_plan,
            navigation_material_action_requests,
            plan_navigation,
        )

        trusted_config = load_trusted_push_config(config, wiki_base=wiki_dir)
        recover_pending_trusted_markdown_effects(
            db_path=trusted_config.db_path,
            state_db_path=database_dir / "producer_consumer_ledger.db",
        )
        plan = plan_navigation(wiki_dir)
        nested_actions = navigation_material_action_requests(
            plan,
            state_db_path=database_dir / "producer_consumer_ledger.db",
        )
        with _material_projection_scope(
            event,
            "moc_navigation",
            nested_actions=nested_actions,
        ) as projection:
            if projection.recovered_outcome is not None:
                return projection.recovered_outcome
            require_material_action(
                projection.authorization,
                owner=projection.request.owner,
                executor_id=projection.request.executor_id,
                action_type=projection.request.action_type,
                target_ref=projection.request.target_ref,
                input_hash=projection.request.input_hash,
                expected_state_db=projection.request.expected_state_db,
            )
            result = apply_navigation_plan(plan)
            if int(result.get("proposed_pages", 0)):
                outcome = HandlerOutcome.defer(
                    "moc_navigation",
                    "navigation changes await trusted-push decision",
                    proposed_pages=result["proposed_pages"],
                    deferred_keys=result.get("proposal_ids", []),
                )
                projection.complete(outcome)
                return outcome
            outcome = HandlerOutcome.ack(
                "moc_navigation",
                indexed_pages=result["indexed_pages"],
                changed_pages=result["changed_pages"],
            )
            projection.complete(outcome)
            return outcome

    def _wiki_search_index_handler(event):
        from core.embeddings import EmbeddingIndexManager

        with _material_projection_scope(
            event,
            "wiki_search_index",
        ) as projection:
            if projection.recovered_outcome is not None:
                return projection.recovered_outcome
            require_material_action(
                projection.authorization,
                owner=projection.request.owner,
                executor_id=projection.request.executor_id,
                action_type=projection.request.action_type,
                target_ref=projection.request.target_ref,
                input_hash=projection.request.input_hash,
                expected_state_db=projection.request.expected_state_db,
            )
            manager = EmbeddingIndexManager(
                wiki_base=wiki_dir,
                index_dir=embedding_index_dir,
                client=embedding_client,
                config=config,
            )
            result = manager.build_index(force_full=False)
            if result.get("status") == "no_client":
                outcome = HandlerOutcome.retry(
                    "wiki_search_index", "embedding client unavailable", **result
                )
                projection.complete(outcome)
                return outcome
            coverage = manager.audit_coverage()
            if not coverage["ok"]:
                outcome = HandlerOutcome.retry(
                    "wiki_search_index", "search index coverage is incomplete", **coverage
                )
                projection.complete(outcome)
                return outcome
            outcome = HandlerOutcome.ack(
                "wiki_search_index", **result, coverage=coverage
            )
            projection.complete(outcome)
            return outcome

    event_bus.subscribe("knowledge_distilled", _kg_handler, consumer_id="knowledge_graph")
    event_bus.subscribe(
        "wiki_page_updated", _kg_page_updated_handler, consumer_id="knowledge_graph"
    )
    event_bus.subscribe(
        "wiki_page_updated", _relation_embeddings_handler, consumer_id="relation_embeddings"
    )
    event_bus.subscribe(
        "wiki_page_updated", _moc_navigation_handler, consumer_id="moc_navigation"
    )
    event_bus.subscribe(
        "wiki_page_updated", _wiki_search_index_handler, consumer_id="wiki_search_index"
    )
    event_bus.subscribe(
        "wiki_page_updated", _metrics_page_updated_handler, consumer_id="wiki_metrics"
    )


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
