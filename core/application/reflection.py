# -*- coding: utf-8 -*-
"""Reflection application service used by the integration facade."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any, Dict

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import make_cognitive_access_envelope
from core.reflection.reflection_store import REFLECTION_OBJECT_PURPOSES


def _reflection_request_access(
    *,
    principal: PrincipalEnvelope | None,
    narrowing: AccessNarrowing | None,
    text: str,
) -> tuple[dict[str, Any] | None, str]:
    """Create a source ACL only for a server-authenticated scoped request."""

    if principal is None:
        return None, "principal_required"
    effective = narrowing or AccessNarrowing()
    session_id = str(effective.session_id or "").strip()
    project = str(effective.project or "").strip().lower()
    if not session_id and not project:
        return None, "scope_required"
    request_hash = hashlib.sha256(str(text).encode("utf-8")).hexdigest()
    scope_type = "session" if session_id else "project"
    scope_id = session_id or project
    return (
        make_cognitive_access_envelope(
            owner_principal_id=principal.principal_id,
            owner_agent=principal.agent,
            scope_type=scope_type,
            scope_id=scope_id,
            session_id=session_id,
            project=project,
            purposes=REFLECTION_OBJECT_PURPOSES,
            consent_provenance_refs=(f"reflection-request:{request_hash}",),
            sensitivity="sensitive",
            retention_policy="reflection_retention",
            source_acl_lineage=(f"sha256:{request_hash}",),
            # The canonical Raw Observation owner is an agent-scoped source.
            # Keep the live request at that same strict scope so combined
            # ReflectionRecords remain usable by the exact authenticated agent
            # and project/session, never public or cross-agent.
            visibility="agent",
        ),
        "authorized",
    )


class ReflectionApplicationService:
    """Default implementation for reflection-facing facade operations."""

    def __init__(self, logger: logging.Logger | None = None):
        self._logger = logger or logging.getLogger(__name__)

    @staticmethod
    def build_reflect_tool_result(result, route) -> Dict:
        """Build the MCP response shared by reflection tools."""
        insight = result.insight
        return {
            "success": True,
            "triggered": result.triggered,
            "route": route.to_dict(),
            "record_id": result.record.id if result.record else None,
            "insight_summary": (insight.summary if insight else ""),
            "key_points": (insight.key_points if insight else []),
            "confidence": (insight.confidence if insight else 0.0),
            "prompt_used": (insight.prompt_used if insight else ""),
            "llm_called": (insight.llm_called if insight else False),
            "llm_error": (insight.llm_error if insight else ""),
            "feedback_messages": result.feedback_messages,
        }

    def get_reflection_engine(self, use_llm: bool = True):
        """Construct the ACL-safe ReflectionEngine for integration requests."""
        from core.reflection.reflection_engine import ReflectionEngine

        # The historic Layer-5 consumers write persona/KIA/policy/JSONL data
        # without carrying a source ACL or typed deletion receipt.  The old
        # ReflectionExporter likewise writes Wiki Markdown outside the
        # projection lifecycle.  Do not activate either from MCP until each
        # target is routed through its ACL-aware command/receipt owner.
        return ReflectionEngine(
            register_default_consumers=False,
            export_to_wiki=False,
            use_llm=use_llm,
        )

    def reflect_on_input(
        self,
        text: str,
        auto_llm: bool = True,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Trigger reflection from a user input."""
        from core.reflection.reflection_router import ReflectionRouter

        source_access_control, access_reason = _reflection_request_access(
            principal=principal,
            narrowing=narrowing,
            text=text,
        )
        if source_access_control is None:
            return {"success": False, "error": access_reason, "record_id": None}

        router = ReflectionRouter()
        route = router.route(text)

        engine = self.get_reflection_engine(use_llm=auto_llm)
        result = engine.reflect_on_user_input(
            text,
            principal=principal,
            narrowing=narrowing,
            source_access_control=source_access_control,
        )
        if route.should_reflect and not result.triggered:
            result = engine.reflect_manually(
                text,
                principal=principal,
                narrowing=narrowing,
                source_access_control=source_access_control,
            )

        return self.build_reflect_tool_result(result, route)

    def reflect_manually(
        self,
        query: str = "",
        auto_llm: bool = True,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Manually trigger a generic reflection run."""
        from core.config import get_config
        from core.reflection.reflection_router import ReflectionRouter

        cfg = get_config()
        if not query:
            query = cfg.get("reflection.manual_query", "分析最近认知与决策模式")

        source_access_control, access_reason = _reflection_request_access(
            principal=principal,
            narrowing=narrowing,
            text=query,
        )
        if source_access_control is None:
            return {"success": False, "error": access_reason, "record_id": None}

        router = ReflectionRouter()
        route = router.route(query)

        engine = self.get_reflection_engine(use_llm=auto_llm)
        result = engine.reflect_manually(
            query,
            principal=principal,
            narrowing=narrowing,
            source_access_control=source_access_control,
        )

        return self.build_reflect_tool_result(result, route)

    def reflection_feedback(
        self,
        reflection_id: str,
        feedback_type: str,
        comment: str = "",
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
        supersedes_event_id: str = "",
        correction_target_ref: str = "",
        correction_reason: str = "",
    ) -> Dict:
        """Submit user feedback for a reflection."""
        from core.reflection.models import FeedbackType

        try:
            fb_type = FeedbackType(feedback_type)
        except ValueError as exc:
            return {
                "success": False,
                "error": f"invalid feedback_type: {exc}",
            }

        engine = self.get_reflection_engine()
        record, access_summary = engine.ref_store.authorized_get_by_id(
            reflection_id,
            principal=principal,
            narrowing=narrowing,
            purpose="reflection_feedback",
        )
        if record is None:
            return {
                "success": False,
                "reflection_id": reflection_id,
                "error": "access_denied",
                "access": access_summary,
            }
        if principal is None:
            return {"success": False, "error": "principal_required"}
        from core.cognitive.feedback_entrypoints import record_reflection_feedback
        from core.runtime_paths import RuntimePaths

        result = record_reflection_feedback(
            database_dir=RuntimePaths.from_config().database_dir,
            reflection_id=reflection_id,
            feedback_type=fb_type.value,
            comment=comment,
            record_snapshot=record.to_dict(),
            access_control=record.access_control,
            principal=principal,
            supersedes_event_id=supersedes_event_id,
            correction_target_ref=correction_target_ref,
            correction_reason=correction_reason,
        )
        return {
            **result,
            "reflection_id": reflection_id,
            "feedback_type": fb_type.value,
            "message": "canonical feedback recorded",
        }

    def reflection_pending(
        self,
        hours_since: float = 24,
        limit: int = 20,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Return reflections waiting for user feedback."""
        from core.reflection.reflection_engine import ReflectionEngine

        engine = ReflectionEngine()
        records, access_summary = engine.ref_store.authorized_get_latest(
            principal=principal,
            narrowing=narrowing,
            purpose="reflection_read",
            limit=max(0, int(limit)) * 2,
        )
        cutoff = datetime.now() - timedelta(hours=max(0.0, float(hours_since)))
        pending = [
            record
            for record in records
            if record.user_feedback is None
            and record.created_at >= cutoff
            and record.insight is not None
        ][: max(0, int(limit))]
        return {
            "success": True,
            "count": len(pending),
            "access_filter": access_summary,
            "pending": [
                {
                    "id": record.id,
                    "created_at": record.created_at.isoformat() if record.created_at else "",
                    "trigger": record.trigger.value,
                    "user_query": record.user_query,
                    "insight_summary": record.insight.summary if record.insight else "",
                }
                for record in pending
            ],
        }
