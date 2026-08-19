# -*- coding: utf-8 -*-
"""
CognitiveGraphUpdater — 跨层认知图同步器

职责：
- 订阅 EventBus 中各认知层事件，翻译为跨层关系写入 cognitive_graph.db
- 消费 sync_outbox 作为事件驱动失效时的兜底
- 维护 canonical_nodes，复用 store 的去重能力

事件 -> 关系映射：
- knowledge_distilled / distill_complete: session -> wiki pages -> kg entities
- reflection.completed: session/obs -> reflection record, reflection -> insights/shifts
- observation.updated (预留): obs -> dimension file, obs -> source session/wiki
- persona.updated (预留): feedback persona -> related wiki/pages
- wiki_page_updated: wiki page -> kg entities
- cognition_episode_committed: committed typed IDs -> evidence/decision/action/outcome

注意：
- 处理函数内不抛异常，避免拖垮 EventBus 重试机制
- 所有写入通过 CognitiveGraphStore 完成
- 对缺失 producer 的事件，updater 提供手动触发入口，方便 daemon 兜底调用
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from core.cognitive.decision_trace import (
    DecisionCandidateEvaluation,
    DecisionRejectionEvaluation,
    MaterialActionAuthorization,
    MaterialActionCoordinator,
    MaterialActionRequest,
    ProjectContractDecisionContext,
    ProjectContractDecisionEvaluation,
    ProjectContractMaterialActionResolver,
    resolve_material_action_authorization,
)
from core.cognitive.state_contract import sha256_json
from core.cognitive.state_store import CognitiveStateStore
from core.mnemos_bus import Event, EventBus, HandlerOutcome, get_event_bus
from core.wiki_projection_lifecycle import WikiProjectionLedger

from .models import CognitiveRelation
from .store import (
    COGNITIVE_RELATION_ACTION,
    COGNITIVE_RELATION_EXECUTOR,
    COGNITIVE_RELATION_OWNER,
    COGNITIVE_RELATION_STALE_ACTION,
    CognitiveGraphStore,
    cognitive_relation_material_action_binding,
)

EVENT_PROCESSING_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    sqlite3.Error,
)

# Constants extracted from magic numbers
OUTBOX_STATS_LIMIT = 10000

logger = logging.getLogger(__name__)

COGNITIVE_GRAPH_EVENT_DECISION_CONTRACT_ID = "project-contract:cognitive-graph-event-projection"
COGNITIVE_GRAPH_EVENT_DECISION_CONTRACT_REVISION = (
    "mnemos.cognitive_graph_event_material_effects.v1"
)
COGNITIVE_GRAPH_EVENT_DECISION_CONTRACT_TEXT = (
    "A validated cognitive event may project only its exact typed relations "
    "through pre action DecisionTrace commands."
)
COGNITIVE_GRAPH_UPDATER_CODE_HASH = sha256_json(
    {
        "module": "core.cognitive_graph.updater",
        "producer": "CognitiveGraphUpdater",
        "version": COGNITIVE_GRAPH_EVENT_DECISION_CONTRACT_REVISION,
    }
)


# 事件类型常量
EVENT_KNOWLEDGE_DISTILLED = "knowledge_distilled"
EVENT_DISTILL_COMPLETE = "distill_complete"
EVENT_WIKI_PAGE_UPDATED = "wiki_page_updated"
EVENT_REFLECTION_COMPLETED = "reflection.completed"
EVENT_OBSERVATION_UPDATED = "observation.updated"
EVENT_PERSONA_UPDATED = "persona.updated"
EVENT_COGNITION_EPISODE_COMMITTED = "cognition_episode_committed"

# 关系类型常量
REL_DERIVED_FROM = "derived_from"
REL_OBSERVED_IN = "observed_in"
REL_RELATED_TO = "related_to"
REL_INFLUENCED_BY = "influenced_by"
REL_CONTRADICTS = "contradicts"

RELATION_TYPE_ALIASES = {
    "contradict": REL_CONTRADICTS,
    "contradicts": REL_CONTRADICTS,
}


def _wiki_urn(path: str) -> str:
    """将绝对/相对路径转换为 wiki:// URN"""
    if not path:
        return ""
    p = Path(path)
    if not p.is_absolute():
        return f"wiki://{p.as_posix().lstrip('./')}"
    # 优先从文件名推相对路径
    parts = list(p.parts)
    if "mnemos" in parts:
        idx = parts.index("mnemos")
        rel = "/".join(parts[idx + 1 :])
        return f"wiki://{rel}"
    if "Documents" in parts and "raw" in parts:
        idx = parts.index("raw")
        rel = "/".join(parts[idx + 1 :])
        return f"wiki://raw/{rel}"
    return f"wiki://{p.name}"


def _session_urn(session_id: str) -> str:
    return f"session://{session_id}"


def _kg_urn(name: str) -> str:
    return f"kg://{name.strip()}"


def _obs_urn(obs_id: str) -> str:
    return f"obs://{obs_id}"


def _ref_urn(record_id: str) -> str:
    return f"ref://{record_id}"


def _feedback_urn(version: str = "latest") -> str:
    return f"feedback://persona/{version}"


def _normalize_relation_type(raw_type: Any) -> str:
    normalized = str(raw_type or REL_RELATED_TO).replace("-", "_").lower()
    return RELATION_TYPE_ALIASES.get(normalized, normalized)


class CognitiveGraphUpdater:
    """跨层认知图同步器"""

    def __init__(
        self,
        store: Optional[CognitiveGraphStore] = None,
        bus: Optional[EventBus] = None,
        material_action_resolver: Optional[
            Callable[
                [Mapping[str, Any], Mapping[str, str]],
                MaterialActionAuthorization,
            ]
        ] = None,
    ):
        self.store = store or CognitiveGraphStore()
        self.bus = bus
        self._material_action_resolver = material_action_resolver
        self._subscribed = False

    @staticmethod
    def _event_access_control(payload: Dict[str, Any]) -> Dict[str, Any] | None:
        """Extract the producer-owned ACL without fabricating a fallback."""

        raw = payload.get("access_control") or payload.get("source_access_control")
        return dict(raw) if isinstance(raw, dict) else None

    @staticmethod
    def _event_payload(event: Event) -> Dict[str, Any]:
        payload = dict(event.payload or {})
        payload["_material_decision_source"] = {
            "event_type": event.event_type,
            "source": event.source,
            "trace_id": event.trace_id,
            "timestamp": event.timestamp,
            "subject_provenance": dict(event.subject_provenance or {}),
        }
        return payload

    def _event_material_action(
        self,
        payload: Dict[str, Any],
        binding: Mapping[str, str],
        *,
        action_type: str = COGNITIVE_RELATION_ACTION,
    ) -> MaterialActionAuthorization:
        if action_type not in {
            COGNITIVE_RELATION_ACTION,
            COGNITIVE_RELATION_STALE_ACTION,
        }:
            raise PermissionError("cognitive graph event requested an unsupported material action")
        event_context = payload.get("_material_decision_source")
        if not isinstance(event_context, Mapping):
            raise PermissionError("cognitive graph material projection requires an Event source")
        event_type = str(event_context.get("event_type") or "")
        trace_id = str(event_context.get("trace_id") or "")
        timestamp = str(event_context.get("timestamp") or "")
        source_payload = {
            key: value for key, value in payload.items() if key != "_material_decision_source"
        }
        evidence_refs: tuple[str, ...]
        if event_type == EVENT_WIKI_PAGE_UPDATED:
            mutation_id = str(source_payload.get("mutation_id") or "")
            receipt = WikiProjectionLedger(
                self.store.db_path.parent / "wiki_projection.db"
            ).mutation_receipt(mutation_id)
            if receipt is None:
                raise PermissionError("cognitive graph Wiki projection lacks a durable mutation")
            expected = {
                "page_path": receipt.page_path,
                "previous_path": receipt.previous_path,
                "page_id": receipt.page_id,
                "page_revision": receipt.page_revision,
                "mutation_id": receipt.mutation_id,
                "mutation_type": receipt.mutation_type,
                "tombstone": receipt.tombstone,
            }
            drift = [key for key, value in expected.items() if source_payload.get(key) != value]
            if drift:
                raise PermissionError(
                    "cognitive graph Wiki event drifted from its mutation: "
                    + ", ".join(sorted(drift))
                )
            source_id = f"wiki-mutation:{receipt.mutation_id}"
            source_revision_id = f"wiki-page-revision:{receipt.page_revision}"
            source_hash = sha256_json(receipt.to_dict())
            source_uri = f"wiki-mutation://{receipt.mutation_id}"
            timestamp = receipt.created_at
            evidence_refs = (
                source_id,
                source_revision_id,
                f"wiki-page:{receipt.page_id}",
            )
        else:
            if not event_type or not trace_id or not timestamp:
                raise PermissionError("cognitive graph event source identity is incomplete")
            source_hash = sha256_json(
                {
                    "event_type": event_type,
                    "source": event_context.get("source"),
                    "trace_id": trace_id,
                    "payload": source_payload,
                }
            )
            source_id = f"event:{trace_id}"
            source_revision_id = f"event-payload:{source_hash}"
            source_uri = f"event://{event_type}/{trace_id}"
            evidence_refs = (source_id, source_revision_id)
        source_facts_hash = sha256_json(
            {
                "schema_version": "mnemos.cognitive_graph_evaluation_facts.v1",
                "event_type": event_type,
                "source_id": source_id,
                "source_revision_id": source_revision_id,
                "source_hash": source_hash,
                "action_type": action_type,
                "relation_binding": dict(binding),
                "evidence_refs": list(evidence_refs),
            }
        )

        def evaluate_request(
            request: MaterialActionRequest,
        ) -> ProjectContractDecisionEvaluation:
            """Evaluate one graph request against this immutable event fact set."""

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
            approved = (
                request.owner == COGNITIVE_RELATION_OWNER
                and request.executor_id == COGNITIVE_RELATION_EXECUTOR
                and request.action_type == action_type
                and request.target_ref == str(binding["target_ref"])
                and request.input_hash == str(binding["input_hash"])
            )
            approved_key = (
                "persist_event_bound_cognitive_relation"
                if action_type == COGNITIVE_RELATION_ACTION
                else "stale_event_bound_cognitive_relation"
            )
            rejected_key = (
                "reject_relation_outside_event_binding"
                if action_type == COGNITIVE_RELATION_ACTION
                else "retain_relation_without_lifecycle_evidence"
            )
            common_refs = (request_ref, facts_ref, *evidence_refs)
            return ProjectContractDecisionEvaluation(
                request_binding_hash=request_hash,
                source_facts_hash=source_facts_hash,
                candidates=(
                    DecisionCandidateEvaluation(
                        key=approved_key,
                        summary=(
                            "Persist the exact relation derived from this event."
                            if action_type == COGNITIVE_RELATION_ACTION
                            else "Mark the exact event-invalidated relation stale."
                        ),
                        supporting_evidence=common_refs if approved else (),
                        opposing_evidence=() if approved else common_refs,
                        satisfies_value_keys=("safety", "project_contract"),
                    ),
                    DecisionCandidateEvaluation(
                        key=rejected_key,
                        summary=(
                            "Reject a relation not derived from this event."
                            if action_type == COGNITIVE_RELATION_ACTION
                            else "Retain a relation not invalidated by this event."
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
                            "event_relation_binding_verified"
                            if approved
                            else "event_relation_binding_rejected"
                        ),
                        evidence_refs=common_refs,
                    ),
                ),
                expected_outcomes=(
                    {
                        "metric": (
                            "cognitive_relation_receipt"
                            if approved
                            else "unbound_relation_effect_count"
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
                state_db_path=self.store.db_path.parent / "producer_consumer_ledger.db",
                contract_id=COGNITIVE_GRAPH_EVENT_DECISION_CONTRACT_ID,
                contract_revision_id=(COGNITIVE_GRAPH_EVENT_DECISION_CONTRACT_REVISION),
                contract_text=COGNITIVE_GRAPH_EVENT_DECISION_CONTRACT_TEXT,
                contract_evidence_ref=(
                    f"{COGNITIVE_GRAPH_EVENT_DECISION_CONTRACT_ID}"
                    f"#{COGNITIVE_GRAPH_EVENT_DECISION_CONTRACT_REVISION}"
                ),
                source_id=source_id,
                source_revision_id=source_revision_id,
                source_content_hash=source_hash,
                source_uri=source_uri,
                evidence_refs=evidence_refs,
                task=f"Project {event_type} into the cognitive graph",
                goal=(
                    "Persist only the exact typed relation derived from the "
                    "authoritative cognitive event."
                ),
                constraints=(
                    "The event source and relation binding must remain exact.",
                    "The cognitive graph effect must be independently receipted.",
                ),
                created_at=timestamp,
                scope_prefix=source_id,
                producer="cognitive-graph-updater",
                producer_version=(COGNITIVE_GRAPH_EVENT_DECISION_CONTRACT_REVISION),
                producer_code_hash=COGNITIVE_GRAPH_UPDATER_CODE_HASH,
                evaluator_id="cognitive-event-relation-evaluator",
                evaluator=evaluate_request,
            )
        )
        return resolver(
            MaterialActionRequest(
                owner=COGNITIVE_RELATION_OWNER,
                executor_id=COGNITIVE_RELATION_EXECUTOR,
                action_type=action_type,
                target_ref=str(binding["target_ref"]),
                input_hash=str(binding["input_hash"]),
                expected_state_db=str(self.store.db_path.parent / "producer_consumer_ledger.db"),
            )
        )

    def _add_relation(self, payload: Dict[str, Any], **kwargs: Any) -> CognitiveRelation:
        """Resolve the exact canonical command before deriving one relation."""

        access_control = self._event_access_control(payload)
        binding = cognitive_relation_material_action_binding(
            **kwargs,
            access_control=access_control,
        )
        if self._material_action_resolver is not None:
            authorization = self._material_action_resolver(payload, binding)
        else:
            command_map = payload.get("material_action_commands")
            if isinstance(command_map, Mapping):
                command_id = str(command_map.get(binding["target_ref"]) or "").strip()
                if not command_id:
                    raise PermissionError("cognitive graph event lacks the exact relation command")
                authorization = MaterialActionCoordinator(
                    CognitiveStateStore(self.store.db_path.parent)
                ).bind(
                    command_id,
                    executor_id=COGNITIVE_RELATION_EXECUTOR,
                )
            else:
                if isinstance(payload.get("_material_decision_source"), Mapping):
                    authorization = self._event_material_action(payload, binding)
                else:
                    authorization, _ = resolve_material_action_authorization(
                        None,
                        owner=COGNITIVE_RELATION_OWNER,
                        executor_id=COGNITIVE_RELATION_EXECUTOR,
                        action_type=COGNITIVE_RELATION_ACTION,
                        target_ref=binding["target_ref"],
                        input_hash=binding["input_hash"],
                        expected_state_db=self.store.db_path.parent / "producer_consumer_ledger.db",
                    )

        return self.store.add_relation(
            **kwargs,
            access_control=access_control,
            material_action=authorization,
        )

    def project_committed_episode_relations(
        self,
        event: Event,
        relations: List[Mapping[str, Any]],
        *,
        access_control: Mapping[str, Any],
        fail_after: int = 0,
    ) -> List[CognitiveRelation]:
        """Project a system-derived relation manifest without model inference.

        Every relation is still authorized by the existing event-bound project
        contract and committed through ``CognitiveGraphStore``'s reciprocal
        target-effect journal.  A caller may retry the same immutable event;
        already journaled relations recover rather than execute twice.
        """

        if event.event_type != EVENT_COGNITION_EPISODE_COMMITTED:
            raise ValueError("cognition episode relation projection event mismatch")
        payload = self._event_payload(event)
        payload["access_control"] = dict(access_control)
        added: List[CognitiveRelation] = []
        for ordinal, raw_relation in enumerate(relations, start=1):
            if fail_after and ordinal > fail_after:
                raise RuntimeError("injected cognition episode relation failure")
            relation = dict(raw_relation)
            added.append(
                self._add_relation(
                    payload,
                    source=str(relation["source"]),
                    target=str(relation["target"]),
                    relation_type=str(relation["relation_type"]),
                    strength=float(relation.get("strength", 1.0)),
                    confidence=float(relation.get("confidence", 1.0)),
                    source_layer=str(relation.get("source_layer") or "cognition"),
                    target_layer=str(relation.get("target_layer") or "cognition"),
                )
            )
        return added

    # ───────────────────────────────
    # 订阅 / 取消订阅
    # ───────────────────────────────

    def subscribe(self, bus: Optional[EventBus] = None):
        """注册到 EventBus"""
        if self._subscribed:
            return
        target = bus or self.bus or get_event_bus()
        target.subscribe(
            EVENT_KNOWLEDGE_DISTILLED,
            self.on_knowledge_distilled,
            consumer_id="cognitive_graph:knowledge_distilled",
        )
        target.subscribe(
            EVENT_DISTILL_COMPLETE,
            self.on_distill_complete,
            consumer_id="cognitive_graph:distill_complete",
        )
        target.subscribe(
            EVENT_WIKI_PAGE_UPDATED,
            self.on_wiki_page_updated,
            consumer_id="cognitive_graph",
        )
        target.subscribe(
            EVENT_REFLECTION_COMPLETED,
            self.on_reflection_completed,
            consumer_id="cognitive_graph:reflection_completed",
        )
        target.subscribe(
            EVENT_OBSERVATION_UPDATED,
            self.on_observation_updated,
            consumer_id="cognitive_graph:observation_updated",
        )
        target.subscribe(
            EVENT_PERSONA_UPDATED,
            self.on_persona_updated,
            consumer_id="cognitive_graph:persona_updated",
        )
        self.bus = target
        self._subscribed = True
        logger.info("[CognitiveGraphUpdater] 已订阅跨层事件")

    # ───────────────────────────────
    # 事件处理器
    # ───────────────────────────────

    def on_knowledge_distilled(self, event: Event):
        """蒸馏完成：session -> wiki pages; session -> kg entities; wiki pages -> kg entities"""
        try:
            payload = self._event_payload(event)
            session_id = payload.get("session_id", "")
            session_urn = _session_urn(session_id) if session_id else ""
            wiki_pages = payload.get("wiki_pages", [])
            kg_input = payload.get("kg_input", {}) or {}
            entities = kg_input.get("entities", [])
            relations = kg_input.get("relations", [])

            added: List[CognitiveRelation] = []
            # session -> wiki pages
            if session_urn:
                for page_path in wiki_pages:
                    rel = self._add_relation(
                        payload,
                        source=session_urn,
                        target=_wiki_urn(page_path),
                        relation_type=REL_DERIVED_FROM,
                        strength=0.9,
                        confidence=0.85,
                        source_layer="session",
                        target_layer="wiki",
                    )
                    added.append(rel)
                    # 反向：wiki derived_from session
                    self._add_relation(
                        payload,
                        source=_wiki_urn(page_path),
                        target=session_urn,
                        relation_type=REL_DERIVED_FROM,
                        strength=0.9,
                        confidence=0.85,
                        source_layer="wiki",
                        target_layer="session",
                    )

            # session -> kg entities
            if session_urn:
                for entity_name in entities:
                    rel = self._add_relation(
                        payload,
                        source=session_urn,
                        target=_kg_urn(entity_name),
                        relation_type=REL_RELATED_TO,
                        strength=0.7,
                        confidence=0.75,
                        source_layer="session",
                        target_layer="kg",
                    )
                    added.append(rel)

            # kg relations
            for rel_data in relations:
                if not isinstance(rel_data, dict):
                    continue
                src = rel_data.get("source", "")
                tgt = rel_data.get("target", "")
                rtype = rel_data.get("type") or rel_data.get("relation_type") or REL_RELATED_TO
                confidence = float(rel_data.get("confidence", 0.5))
                strength = float(rel_data.get("strength", confidence))
                self._add_relation(
                    payload,
                    source=_kg_urn(src),
                    target=_kg_urn(tgt),
                    relation_type=_normalize_relation_type(rtype),
                    strength=strength,
                    confidence=confidence,
                    source_layer="kg",
                    target_layer="kg",
                )

            # wiki pages -> kg entities (从页面内容提取的实体与页面关联)
            for page_path in wiki_pages:
                wiki_urn = _wiki_urn(page_path)
                for entity_name in entities:
                    self._add_relation(
                        payload,
                        source=wiki_urn,
                        target=_kg_urn(entity_name),
                        relation_type=REL_RELATED_TO,
                        strength=0.65,
                        confidence=0.7,
                        source_layer="wiki",
                        target_layer="kg",
                    )

            logger.debug(
                "[CognitiveGraphUpdater] knowledge_distilled 写入 %s 条关系",
                len(added),
            )
        except EVENT_PROCESSING_ERRORS as e:
            logger.warning("[CognitiveGraphUpdater] knowledge_distilled 处理失败: %s", e)
            self._add_outbox(EVENT_KNOWLEDGE_DISTILLED, event.payload)

    def on_distill_complete(self, event: Event):
        """单页面蒸馏完成：session -> wiki page"""
        try:
            payload = self._event_payload(event)
            session_id = payload.get("session_id", "")
            page_path = payload.get("page_path", "")
            if not session_id or not page_path:
                return
            self._add_relation(
                payload,
                source=_session_urn(session_id),
                target=_wiki_urn(page_path),
                relation_type=REL_DERIVED_FROM,
                strength=0.95,
                confidence=0.9,
                source_layer="session",
                target_layer="wiki",
            )
        except EVENT_PROCESSING_ERRORS as e:
            logger.warning("[CognitiveGraphUpdater] distill_complete 处理失败: %s", e)
            self._add_outbox(EVENT_DISTILL_COMPLETE, event.payload)

    def on_wiki_page_updated(self, event: Event):
        """Apply explicit page lifecycle semantics without creating self loops."""
        try:
            payload = self._event_payload(event)
            page_path = payload.get("page_path", "")
            if not page_path:
                return HandlerOutcome.dead("cognitive_graph", "missing page_path")
            mutation_type = str(payload.get("mutation_type") or "update")
            if mutation_type in {"move", "delete"}:
                result = self.store.reconcile_wiki_page(
                    previous_path=str(payload.get("previous_path") or page_path),
                    page_path=str(page_path),
                    mutation_type=mutation_type,
                    material_action_resolver=(
                        lambda action_type, binding: self._event_material_action(
                            payload,
                            binding,
                            action_type=action_type,
                        )
                    ),
                )
                return HandlerOutcome.ack("cognitive_graph", **result)
            logger.debug("[CognitiveGraphUpdater] wiki_page_updated: %s", page_path)
            return HandlerOutcome.noop(
                "cognitive_graph",
                "wiki page update creates no cognitive self relation",
                page_revision=payload.get("page_revision"),
            )
        except EVENT_PROCESSING_ERRORS as e:
            logger.warning("[CognitiveGraphUpdater] wiki_page_updated 处理失败: %s", e)
            self._add_outbox(EVENT_WIKI_PAGE_UPDATED, event.payload)
            return HandlerOutcome.retry("cognitive_graph", str(e), outbox_recorded=True)

    def on_reflection_completed(self, event: Event):
        """反思完成：session/obs -> reflection record; reflection -> insights"""
        try:
            payload = self._event_payload(event)
            record_id = payload.get("record_id", "")
            if not record_id:
                return
            ref_urn = _ref_urn(record_id)

            # reflection -> related insight_summary (作为 wiki 节点)
            summary = payload.get("insight_summary", "")
            if summary:
                self._add_relation(
                    payload,
                    source=ref_urn,
                    target=f"wiki://L4-Reflections/insights/{record_id}.md",
                    relation_type=REL_DERIVED_FROM,
                    strength=0.85,
                    confidence=0.8,
                    source_layer="reflection",
                    target_layer="wiki",
                )

            # reflection -> feedback/persona (反思结果影响画像)
            self._add_relation(
                payload,
                source=ref_urn,
                target=_feedback_urn(),
                relation_type=REL_INFLUENCED_BY,
                strength=0.6,
                confidence=0.7,
                source_layer="reflection",
                target_layer="feedback",
            )

            logger.debug(
                "[CognitiveGraphUpdater] reflection.completed 写入 record_id=%s",
                record_id,
            )
        except EVENT_PROCESSING_ERRORS as e:
            logger.warning("[CognitiveGraphUpdater] reflection.completed 处理失败: %s", e)
            self._add_outbox(EVENT_REFLECTION_COMPLETED, event.payload)

    def on_observation_updated(self, event: Event):
        """观察更新：obs -> dimension file; obs -> source session"""
        try:
            payload = self._event_payload(event)
            observation_ids = payload.get("observation_ids", [])
            wiki_path = payload.get("wiki_path", "")
            wiki_urn = _wiki_urn(wiki_path) if wiki_path else ""
            for obs_id in observation_ids:
                obs_urn = _obs_urn(obs_id)
                if wiki_urn:
                    self._add_relation(
                        payload,
                        source=obs_urn,
                        target=wiki_urn,
                        relation_type=REL_OBSERVED_IN,
                        strength=0.8,
                        confidence=0.8,
                        source_layer="observation",
                        target_layer="wiki",
                    )
                # obs 与 session 关联（若 payload 有 session_id）
                session_id = payload.get("session_id")
                if session_id:
                    self._add_relation(
                        payload,
                        source=obs_urn,
                        target=_session_urn(session_id),
                        relation_type=REL_OBSERVED_IN,
                        strength=0.75,
                        confidence=0.75,
                        source_layer="observation",
                        target_layer="session",
                    )
        except EVENT_PROCESSING_ERRORS as e:
            logger.warning("[CognitiveGraphUpdater] observation.updated 处理失败: %s", e)
            self._add_outbox(EVENT_OBSERVATION_UPDATED, event.payload)

    def on_persona_updated(self, event: Event):
        """画像更新：feedback persona -> related wiki page"""
        try:
            payload = self._event_payload(event)
            wiki_path = payload.get("wiki_path", "")
            version = payload.get("version", "latest")
            feedback_urn = _feedback_urn(str(version))
            if wiki_path:
                self._add_relation(
                    payload,
                    source=feedback_urn,
                    target=_wiki_urn(wiki_path),
                    relation_type=REL_DERIVED_FROM,
                    strength=0.9,
                    confidence=0.85,
                    source_layer="feedback",
                    target_layer="wiki",
                )
        except EVENT_PROCESSING_ERRORS as e:
            logger.warning("[CognitiveGraphUpdater] persona.updated 处理失败: %s", e)
            self._add_outbox(EVENT_PERSONA_UPDATED, event.payload)

    # ───────────────────────────────
    # Outbox 兜底
    # ───────────────────────────────

    def _add_outbox(self, event_type: str, payload: Dict[str, Any]):
        """事件处理失败时写入 outbox"""
        try:
            self.store.add_sync_outbox(event_type, payload or {})
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as e:
            logger.error("[CognitiveGraphUpdater] outbox 写入失败: %s", e)

    def process_outbox(self, limit: int = 100) -> Dict[str, int]:
        """消费未处理的 outbox 条目"""
        pending = self.store.fetch_outbox(unprocessed_only=True, limit=limit)
        processed = 0
        failed = 0
        for item in pending:
            try:
                event = Event(
                    event_type=item.event_type,
                    source="cognitive_graph.outbox",
                    payload=item.payload,
                )
                handler = self._get_handler(item.event_type)
                if handler:
                    handler(event)
                    self.store.mark_outbox_processed(item.id)
                    processed += 1
                else:
                    logger.warning(
                        "[CognitiveGraphUpdater] 未知 outbox 事件类型: %s", item.event_type
                    )
                    failed += 1
            except EVENT_PROCESSING_ERRORS as e:
                logger.warning("[CognitiveGraphUpdater] outbox 处理失败 id=%s: %s", item.id, e)
                failed += 1
        return {"processed": processed, "failed": failed}

    def _get_handler(self, event_type: str):
        handlers = {
            EVENT_KNOWLEDGE_DISTILLED: self.on_knowledge_distilled,
            EVENT_DISTILL_COMPLETE: self.on_distill_complete,
            EVENT_WIKI_PAGE_UPDATED: self.on_wiki_page_updated,
            EVENT_REFLECTION_COMPLETED: self.on_reflection_completed,
            EVENT_OBSERVATION_UPDATED: self.on_observation_updated,
            EVENT_PERSONA_UPDATED: self.on_persona_updated,
        }
        return handlers.get(event_type)

    # ───────────────────────────────
    # 兜底重建
    # ───────────────────────────────

    def reconcile(self) -> Dict[str, Any]:
        """兜底重建：处理 outbox 并返回统计"""
        outbox_stats = self.process_outbox(limit=OUTBOX_STATS_LIMIT)
        db_stats = self.store.rebuild_missing_relations()
        return {
            "outbox": outbox_stats,
            "db": db_stats,
            "stats": self.store.get_stats(),
        }
