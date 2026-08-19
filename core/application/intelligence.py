# -*- coding: utf-8 -*-
"""Context-intelligence application service used by the integration facade."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import sqlite3
from typing import Any, Dict

from core.access_policy import (
    AccessNarrowing,
    PrincipalEnvelope,
    filter_authorized_items,
)


def _predictive_route_source_access(
    *,
    principal: PrincipalEnvelope,
    page_id: str,
    frontmatter: Dict[str, Any],
    topic: str,
) -> dict[str, Any]:
    """Project one authorized Wiki ACL into an exact cognitive source ACL."""

    from core.cognitive.access_control import make_cognitive_access_envelope
    from core.cognitive.state_contract import sha256_json

    normalized_page_id = str(page_id or "").strip()
    normalized_topic = str(topic or normalized_page_id).strip().lower()
    source_agent = str(frontmatter.get("source_agent") or "").strip().lower()
    source_scope = str(frontmatter.get("scope") or "").strip().lower()
    if not normalized_page_id or not normalized_topic or not source_agent:
        raise ValueError("authorized predictive source ACL is incomplete")
    acl_snapshot_hash = sha256_json(
        {
            "page_id": normalized_page_id,
            "frontmatter": dict(frontmatter),
        }
    )
    if source_scope == "private":
        source_session = str(frontmatter.get("session_id") or "").strip()
        source_project = str(frontmatter.get("project") or "").strip().lower()
        visibility = "private"
    elif source_scope == "project":
        source_session = ""
        source_project = str(frontmatter.get("project") or "").strip().lower()
        visibility = "project"
    else:
        # Agent/framework/global pages have no narrower source-session field.
        # Bind the projection to this exact canonical Wiki ACL snapshot so the
        # cognitive object cannot float into another source or caller scope.
        source_session = "wiki-source:" + acl_snapshot_hash.split(":", 1)[1][:32]
        source_project = ""
        visibility = "agent"
    resolved = bool(source_session or source_project)
    return make_cognitive_access_envelope(
        owner_principal_id=principal.principal_id,
        owner_agent=source_agent,
        scope_type="topic",
        scope_id=normalized_topic,
        session_id=source_session,
        project=source_project,
        purposes=(
            "cognitive_state_read",
            "cognitive_state_write",
            "prediction_read",
        ),
        consent_provenance_refs=(
            (
                f"wiki-page:{normalized_page_id}",
                f"wiki-acl:{acl_snapshot_hash}",
            )
            if resolved
            else ()
        ),
        sensitivity="sensitive" if resolved else "restricted",
        retention_policy="prediction_ledger",
        source_acl_lineage=(acl_snapshot_hash,),
        visibility=visibility if resolved else "restricted",
        scope_resolution="resolved" if resolved else "restricted_unknown",
        consent_status="granted" if resolved else "restricted_unknown",
    )


class IntelligenceApplicationService:
    """Default implementation for context intelligence facade operations."""

    def __init__(self, logger: logging.Logger | None = None):
        self._logger = logger or logging.getLogger(__name__)

    def context_aware_search(
        self,
        query: str,
        limit: int = 10,
        working_dir: str = "",
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Search knowledge with context, profile, and access-policy filtering."""
        from core.app.context_search import ContextAwareSearch

        narrowing = narrowing or AccessNarrowing()
        _authorized_surface, preflight_summary = self._authorized_wiki_surface(
            principal,
            narrowing,
        )

        context = {}
        if working_dir:
            context["working_dir"] = working_dir

        search = ContextAwareSearch()
        results = search.search(
            query,
            context=context,
            limit=limit,
            principal=principal,
            narrowing=narrowing,
        )

        serialized = [
            {
                "page_path": result.page_path,
                "title": result.title,
                "snippet": result.snippet,
                "score": round(result.score, 3),
                "heat_level": getattr(result, "heat_level", "cold"),
                "heat_score": round(float(getattr(result, "heat_score", 0.0) or 0.0), 1),
                "last_accessed": getattr(result, "last_accessed", ""),
                "match_source": getattr(result, "match_source", ""),
                "match_reason": result.match_reason,
                "source": getattr(result, "source", ""),
                "relevance": round(getattr(result, "relevance", 0.0), 3),
                "confidence": round(getattr(result, "confidence", 0.0), 3),
                "freshness": round(getattr(result, "freshness", 0.0), 3),
                "continuity": round(getattr(result, "continuity", 0.0), 3),
                "persona_score": round(getattr(result, "persona_score", 0.0), 3),
                "score_breakdown": getattr(result, "score_breakdown", {}) or {},
                "matched_terms": list(getattr(result, "matched_terms", []) or []),
                "scope": getattr(result, "scope", ""),
                "source_agent": getattr(result, "source_agent", ""),
                "session_id": getattr(result, "session_id", ""),
                "project": getattr(result, "project", ""),
                "tags": list(getattr(result, "tags", []) or []),
                "acl_schema_version": getattr(result, "acl_schema_version", 0),
                "acl_metadata_complete": getattr(
                    result,
                    "acl_metadata_complete",
                    False,
                ),
                "acl_reconciliation_status": getattr(
                    result,
                    "acl_reconciliation_status",
                    "",
                ),
                "result_kind": getattr(result, "result_kind", "wiki_page"),
                "object_type": getattr(result, "object_type", ""),
                "object_id": getattr(result, "object_id", ""),
                "revision_id": getattr(result, "revision_id", ""),
                "matched_field": getattr(result, "matched_field", ""),
                "source_revision_id": getattr(result, "source_revision_id", ""),
                "source_span_ids": list(getattr(result, "source_span_ids", []) or []),
                "acl_decision": getattr(result, "acl_decision", ""),
                "supersedes_revision_id": getattr(result, "supersedes_revision_id", ""),
                "is_current": getattr(result, "is_current", True),
            }
            for result in results
        ]
        wiki_serialized = [item for item in serialized if item["result_kind"] == "wiki_page"]
        cognitive_serialized = {
            item["page_path"]: item for item in serialized if item["result_kind"] != "wiki_page"
        }
        wiki_serialized, wiki_access_summary = filter_authorized_items(
            wiki_serialized,
            principal,
            narrowing,
        )
        cognitive_results, cognitive_access = self._reauthorize_cognitive_results(
            search,
            results,
            principal=principal,
            narrowing=narrowing,
        )
        authorized_cognitive_paths = {result.page_path for result in cognitive_results}
        serialized = wiki_serialized + [
            item
            for path, item in cognitive_serialized.items()
            if path in authorized_cognitive_paths
        ]
        access_summary = dict(preflight_summary)
        for reason, count in wiki_access_summary.items():
            access_summary[reason] = access_summary.get(reason, 0) + int(count)
        for reason, count in cognitive_access.items():
            prefixed_reason = f"cognitive_{reason}"
            access_summary[prefixed_reason] = (
                access_summary.get(prefixed_reason, 0) + int(count)
            )
        authorized_paths = {item["page_path"] for item in serialized}
        authorized_results = [result for result in results if result.page_path in authorized_paths]
        rerank = getattr(search, "rerank_authorized", None)
        if callable(rerank) and authorized_results:
            authorized_results = rerank(
                query,
                authorized_results,
                limit=limit,
            )
            authorized_paths_in_order = [result.page_path for result in authorized_results]
            serialized_by_path = {item["page_path"]: item for item in serialized}
            serialized = [
                serialized_by_path[path]
                for path in authorized_paths_in_order
                if path in serialized_by_path
            ]
        trace_getter = getattr(search, "get_last_query_trace", None)
        query_trace = trace_getter() if callable(trace_getter) else {}
        degraded_reasons = list(query_trace.get("degraded_reasons", []) or [])
        if authorized_results:
            search.record_authorized_search(
                query,
                authorized_results,
                principal=principal,
                narrowing=narrowing,
            )

        interaction_preferences, preference_access = self._active_interaction_preferences(
            principal=principal,
            narrowing=narrowing,
        )
        for reason, count in preference_access.items():
            access_summary[reason] = access_summary.get(reason, 0) + count

        return {
            "success": True,
            "query": query,
            "results": serialized,
            "count": len(serialized),
            "access_filter": access_summary,
            "query_trace": query_trace,
            "degraded": bool(query_trace.get("degraded", False)),
            "degraded_reasons": degraded_reasons,
            "interaction_preferences": interaction_preferences,
        }

    @staticmethod
    def _active_interaction_preferences(
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> tuple[list[dict[str, Any]], Dict[str, int]]:
        """Read exact-scope active preferences without initializing a store."""

        from core.cognitive.user_model_asset_store import InteractionPreferenceStore
        from core.config import get_config

        store = InteractionPreferenceStore(
            get_config().database_dir / "interaction_preferences.db"
        )
        if "memory_read" not in principal.capabilities:
            return [], {"interaction_preference_capability_denied": 1}
        state = store.schema_status()
        if state["status"] == "uninitialized":
            return [], {"interaction_preference_store_uninitialized": 1}
        if not state["ok"]:
            return [], {"interaction_preference_store_unavailable": 1}
        now = datetime.now(timezone.utc)
        accepted: list[dict[str, Any]] = []
        rejected = 0
        for preference in store.current_preferences():
            try:
                expires_at = datetime.fromisoformat(preference.expires_at)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
            except ValueError:
                rejected += 1
                continue
            scope = preference.scope
            principal_matches = not scope.principal_id or (
                scope.principal_id == principal.principal_id
            )
            if scope.scope_type == "session":
                scope_matches = bool(narrowing.session_id) and (
                    scope.scope_id == narrowing.session_id
                )
            elif scope.scope_type == "project":
                requested_project = narrowing.project or (
                    next(iter(principal.allowed_projects))
                    if len(principal.allowed_projects) == 1
                    else ""
                )
                scope_matches = bool(requested_project) and scope.scope_id == requested_project
            elif scope.scope_type == "user":
                scope_matches = scope.scope_id == principal.principal_id
            else:
                scope_matches = False
            if (
                preference.status != "active"
                or expires_at <= now
                or not principal_matches
                or not scope_matches
                or "context_search" not in preference.consumers
            ):
                rejected += 1
                continue
            accepted.append(
                {
                    "asset_id": preference.asset_id,
                    "revision_id": preference.revision_id,
                    "dimension": preference.dimension,
                    "value": preference.value,
                    "confidence": preference.confidence,
                    "scope_type": scope.scope_type,
                    "scope_id": scope.scope_id,
                    "expires_at": preference.expires_at,
                }
            )
        summary = {"interaction_preference_authorized": len(accepted)}
        if rejected:
            summary["interaction_preference_rejected"] = rejected
        return accepted, summary

    @staticmethod
    def _reauthorize_cognitive_results(
        search: Any,
        results: list[Any],
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> tuple[list[Any], Dict[str, int]]:
        candidates = [
            result
            for result in results
            if getattr(result, "result_kind", "wiki_page") != "wiki_page"
        ]
        if not candidates:
            return [], {}
        reauthorize = getattr(search, "reauthorize_cognitive_result", None)
        if not callable(reauthorize):
            return [], {"cognitive_reauthorization_unavailable": len(candidates)}
        authorized = []
        summary: Dict[str, int] = {}
        for result in candidates:
            try:
                allowed, reason = reauthorize(
                    result,
                    principal=principal,
                    narrowing=narrowing,
                )
            except (FileNotFoundError, OSError, RuntimeError, ValueError, sqlite3.Error):
                allowed, reason = False, "cognitive_reauthorization_invalid"
            normalized_reason = str(reason or "identity_mismatch")
            summary[normalized_reason] = summary.get(normalized_reason, 0) + 1
            if allowed:
                authorized.append(result)
        return authorized, summary

    @staticmethod
    def _authorized_wiki_surface(
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> tuple[bool, Dict[str, Any]]:
        """Fail closed before constructing indexes when no page is readable."""
        from core.config import get_config
        from core.frontmatter import read_frontmatter_only

        wiki_dir = Path(get_config().wiki_dir)
        items = []
        if wiki_dir.is_dir():
            for page in wiki_dir.rglob("*.md"):
                try:
                    frontmatter = read_frontmatter_only(page, errors="ignore")
                except (OSError, ValueError):
                    continue
                items.append(
                    {
                        "page_path": str(page),
                        "scope": frontmatter.get("scope", ""),
                        "source_agent": frontmatter.get("source_agent", ""),
                        "session_id": frontmatter.get("session_id", ""),
                        "project": frontmatter.get("project", ""),
                        "acl_schema_version": frontmatter.get("acl_schema_version", 0),
                        "acl_metadata_complete": frontmatter.get("acl_metadata_complete", False),
                        "acl_reconciliation_status": frontmatter.get(
                            "acl_reconciliation_status", ""
                        ),
                    }
                )
        authorized, summary = filter_authorized_items(items, principal, narrowing)
        return bool(authorized), summary

    def intent_route(self, user_input: str, working_dir: str = "") -> Dict:
        """Route user intent with rules and LLM fallback."""
        from core.app.intent_router import IntentRouter

        router = IntentRouter()
        context = {"working_dir": working_dir} if working_dir else {}
        decision = router.route(user_input, context=context)

        result = {
            "success": True,
            "intent": decision.intent,
            "confidence": round(decision.confidence, 2),
            "data_source": decision.data_source,
            "matched_keywords": decision.matched_keywords,
            "needs_correction": decision.needs_correction,
            "llm_fallback": decision.llm_fallback,
            "route_tools": decision.route_tools,
            "fallback_tools": decision.fallback_tools,
            "explanation": decision.explanation,
        }
        if decision.needs_correction:
            result["suggested_action"] = (
                "意图较模糊，请向用户确认真实意图；确认后可调用 intent_correct 写入纠正记录。"
            )
        elif decision.llm_fallback:
            result["suggested_action"] = "规则匹配置信度较低，已由 LLM 兜底分类。"
        return result

    def intent_correct(self, user_input: str, original_intent: str, corrected_intent: str) -> Dict:
        """Record a corrected intent."""
        from core.app.intent_router import IntentRouter

        valid_intents = {
            "recap",
            "system_status",
            "persona",
            "mixed_recall",
            "recall",
            "ignore_push",
            "knowledge",
            "task",
            "chat",
        }
        if original_intent not in valid_intents:
            return {
                "success": False,
                "error": f"original_intent 必须是 {valid_intents} 之一",
            }
        if corrected_intent not in valid_intents:
            return {
                "success": False,
                "error": f"corrected_intent 必须是 {valid_intents} 之一",
            }

        router = IntentRouter()
        router.correct(user_input, original_intent, corrected_intent)
        return {
            "success": True,
            "user_input": user_input,
            "original_intent": original_intent,
            "corrected_intent": corrected_intent,
        }

    def blindspot_check(
        self,
        query: str,
        session_id: str = "",
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Check whether a query exposes a knowledge blind spot."""
        from core.app.blindspot_asset_schema import BlindspotAssetSchemaError
        from core.app.blindspot_discovery import BlindspotDiscovery
        from core.app.blindspot_response_builder import BlindspotResponseBuilder

        if principal is None:
            return {
                "success": False,
                "code": "principal_required",
                "blindspot_found": False,
            }
        try:
            bd = BlindspotDiscovery()
        except BlindspotAssetSchemaError as exc:
            return {
                "success": False,
                "code": "knowledge_gap_schema_reconciliation_required",
                "blindspot_found": False,
                "degraded": True,
                "degraded_reasons": [str(exc)],
            }
        result = bd.check_blind_spot(
            query,
            session_id=session_id or None,
            principal=principal,
            narrowing=narrowing or AccessNarrowing(),
        )

        base: Dict[str, Any] = {
            "success": True,
            "degraded": result.degraded,
        }
        if result.degraded:
            base["degraded_reasons"] = result.degraded_reasons

        if not result.reminder:
            base.update(
                {
                    "blindspot_found": False,
                    "message": "未发现盲点",
                }
            )
            return base

        tool_result = BlindspotResponseBuilder.build_tool_result(
            result.reminder,
            suggested_query=result.suggested_query,
            degraded=result.degraded,
            degraded_reasons=result.degraded_reasons,
        )
        base.update(tool_result)
        return base

    def predictive_push(
        self,
        user_input: str,
        working_dir: str = "",
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Return predictive knowledge pushes for the current context."""
        from core.access_policy import authorize_item
        from core.config import get_config
        from core.frontmatter import read_frontmatter_only
        from core.kia.reminder_engine import ReminderEngine

        if principal is None:
            return {
                "success": False,
                "code": "principal_required",
                "push_available": False,
            }
        effective_narrowing = narrowing or AccessNarrowing()
        engine = ReminderEngine()
        wiki_root = Path(get_config().wiki_dir).expanduser().resolve()
        access_summary: Dict[str, int] = {}
        access_cache: Dict[str, bool] = {}
        frontmatter_cache: Dict[str, Dict[str, Any]] = {}

        def authorized_page_path(page_path: str) -> bool:
            raw_path = Path(str(page_path or ""))
            full_path = raw_path if raw_path.is_absolute() else wiki_root / raw_path
            try:
                resolved = full_path.resolve()
                relative = resolved.relative_to(wiki_root).as_posix()
            except (OSError, ValueError):
                reason = "acl_metadata_missing"
                access_summary[reason] = access_summary.get(reason, 0) + 1
                return False
            if relative in access_cache:
                return access_cache[relative]
            try:
                frontmatter = read_frontmatter_only(resolved, errors="ignore")
            except (OSError, ValueError):
                reason = "acl_metadata_missing"
                access_summary[reason] = access_summary.get(reason, 0) + 1
                access_cache[relative] = False
                return False
            decision = authorize_item(
                principal,
                {"page_id": relative, "frontmatter": frontmatter},
                effective_narrowing,
            )
            access_summary[decision.reason] = access_summary.get(decision.reason, 0) + 1
            access_cache[relative] = decision.allowed
            if decision.allowed:
                frontmatter_cache[relative] = dict(frontmatter)
            return access_cache[relative]

        def authorized_candidate(reminder: Any) -> bool:
            return authorized_page_path(str(reminder.page_path or ""))

        reminders = engine.contextual_reminders(
            user_input,
            recent_context=working_dir or "",
            candidate_filter=authorized_candidate,
            candidate_path_filter=authorized_page_path,
        )

        if not reminders:
            return {
                "success": True,
                "push_available": False,
                "message": "无推送信号",
                "access_filter": access_summary,
            }

        try:
            from core.cognitive.delivery_router import KnowledgeDeliveryRouter

            delivery_router = KnowledgeDeliveryRouter()
            gated_reminders = []
            suppressed_delivery_decisions = []
            for reminder in reminders:
                topic = (reminder.title or reminder.page_path or "").strip().lower()
                relative_page = Path(str(reminder.page_path or "")).as_posix()
                source_access_control = _predictive_route_source_access(
                    principal=principal,
                    page_id=relative_page,
                    frontmatter=frontmatter_cache[relative_page],
                    topic=topic,
                )
                delivery_decision = delivery_router.route_candidate(
                    source="predictive_push",
                    subject=topic,
                    channel="predictive_push",
                    target=reminder.page_path,
                    evidence_refs=[reminder.page_path] if reminder.page_path else [],
                    task_fit_score=_safe_float(reminder.confidence, default=0.5),
                    requested_level=_delivery_level_for_priority(reminder.priority),
                    task_key=working_dir or "global",
                    cooldown_key=topic,
                    active_risk=False,
                    source_access_control=source_access_control,
                    principal=principal,
                    metadata={
                        "page_path": reminder.page_path,
                        "title": reminder.title,
                        "reason": reminder.reason,
                        "priority": reminder.priority,
                        "working_dir": working_dir,
                        "principal_id": principal.principal_id,
                        "principal_agent": principal.agent,
                        "project": effective_narrowing.project,
                        "session_id": effective_narrowing.session_id,
                    },
                )
                decision_dict = delivery_decision.to_dict()
                if delivery_decision.decision == "deliver":
                    gated_reminders.append((reminder, decision_dict))
                else:
                    suppressed_delivery_decisions.append(decision_dict)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
            PermissionError,
        ):
            self._logger.warning("[agora] predictive_push delivery router 不可用", exc_info=True)
            return {
                "success": True,
                "push_available": False,
                "message": "delivery router 不可用，已取消主动推送",
                "trust_gate_available": False,
                "delivery_router_available": False,
                "access_filter": access_summary,
            }

        if not gated_reminders:
            return {
                "success": True,
                "push_available": False,
                "message": "delivery router 已抑制主动推送",
                "trust_gate_available": True,
                "delivery_router_available": True,
                "suppressed_count": len(suppressed_delivery_decisions),
                "delivery_decisions": suppressed_delivery_decisions,
                "trust_decisions": [
                    item.get("metadata", {}).get("trust_decision", {})
                    for item in suppressed_delivery_decisions
                ],
                "access_filter": access_summary,
            }

        reminders = [item[0] for item in gated_reminders]
        delivered_delivery_decisions = [item[1] for item in gated_reminders]

        try:
            from core.kia.teiresias import (
                KnowledgeMatch,
                PredictivePushEngine,
                PushDecision,
            )

            pp_engine = PredictivePushEngine()
            decision = PushDecision(
                should_push=True,
                reason=f"predictive_push: {len(reminders)} 条上下文提醒",
                matches=[
                    KnowledgeMatch(
                        page_path=reminder.page_path,
                        page_title=reminder.title,
                        match_score=reminder.confidence,
                        match_reason=reminder.reason,
                        push_priority=reminder.priority,
                    )
                    for reminder in reminders
                ],
            )
            pp_engine.record_push(decision)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            self._logger.debug("[agora] predictive_push 历史记录失败", exc_info=True)

        return {
            "success": True,
            "push_available": True,
            "pushes": [
                {
                    "topic": (reminder.title or "").strip().lower(),
                    "title": reminder.title,
                    "page_path": reminder.page_path,
                    "reason": reminder.reason,
                    "confidence": round(reminder.confidence, 2),
                    "delivery_event_id": delivery_decision["event_id"],
                    "delivery_decision": delivery_decision,
                    "trust_decision_id": delivery_decision["trust_decision_id"],
                    "trust_decision": delivery_decision.get("metadata", {}).get(
                        "trust_decision",
                        {},
                    ),
                }
                for reminder, delivery_decision in zip(reminders, delivered_delivery_decisions)
            ],
            "count": len(reminders),
            "trust_gate_available": True,
            "delivery_router_available": True,
            "suppressed_count": len(suppressed_delivery_decisions),
            "access_filter": access_summary,
        }

    def push_feedback(
        self,
        topic: str,
        action: str,
        delivery_event_id: str,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
        supersedes_event_id: str = "",
        correction_target_ref: str = "",
        correction_reason: str = "",
    ) -> Dict:
        """Process one principal-bound feedback event through required consumers."""
        from core.cognitive.feedback_entrypoints import record_predictive_feedback
        from core.runtime_paths import RuntimePaths

        return record_predictive_feedback(
            database_dir=RuntimePaths.from_config().database_dir,
            topic=topic,
            action=action,
            delivery_event_id=delivery_event_id,
            principal=principal,
            narrowing=narrowing,
            supersedes_event_id=supersedes_event_id,
            correction_target_ref=correction_target_ref,
            correction_reason=correction_reason,
        )

    def freshness_check(
        self,
        entity_name: str,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> Dict:
        """Check knowledge freshness without mutating the page."""
        from core.access_policy import authorize_item
        from core.app.freshness_alert import FreshnessAlertChecker
        from core.config import get_config
        from core.frontmatter import read_frontmatter_only

        checker = FreshnessAlertChecker()
        wiki_root = Path(get_config().wiki_dir).expanduser().resolve()
        access_reason = ""

        def authorized_candidate(page: Dict[str, Any]) -> bool:
            nonlocal access_reason
            raw_path = Path(str(page.get("path") or ""))
            try:
                relative_path = str(raw_path.resolve().relative_to(wiki_root))
            except (OSError, ValueError):
                access_reason = "acl_metadata_missing"
                return False
            try:
                frontmatter = read_frontmatter_only(raw_path.resolve(), errors="ignore")
            except (OSError, ValueError):
                access_reason = "acl_metadata_missing"
                return False
            decision = authorize_item(
                principal,
                {"page_id": relative_path, "frontmatter": frontmatter},
                narrowing,
            )
            access_reason = decision.reason
            return decision.allowed

        result = checker.check_knowledge_freshness(
            entity_name,
            candidate_filter=authorized_candidate,
        )

        if not result:
            return {
                "success": True,
                "status": "fresh",
                "fresh": True,
                "message": f"「{entity_name}」知识新鲜",
            }

        if result.status == "access_denied":
            return {
                "success": False,
                "status": "access_denied",
                "code": access_reason or "access_denied",
                "fresh": False,
            }

        if result.status in ("not_found", "error"):
            return {
                "success": True,
                "status": result.status,
                "fresh": False,
                "message": result.message,
            }

        if result.status == "fresh":
            return {
                "success": True,
                "status": "fresh",
                "fresh": True,
                "message": result.message,
            }

        response = {
            "success": True,
            "status": "stale",
            "fresh": False,
            "entity_name": result.entity_name,
            "alert_type": result.alert_type,
            "message": result.message,
            "confidence": round(result.confidence, 2),
            "current_version": result.current_version,
            "latest_version": result.latest_version,
            "refresh_available": True,
            "suggested_action": (
                "知识可能已过期，请通过具备写权限的维护工作流刷新，" "或手动检查更新。"
            ),
        }

        return response


def _delivery_level_for_priority(priority: str) -> str:
    return {"high": "hint", "medium": "hint", "low": "silent"}.get(
        str(priority or "").strip().lower(),
        "hint",
    )


def _safe_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
