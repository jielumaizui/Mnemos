"""Authenticated adapter from retrospective feedback to canonical attribution."""

from __future__ import annotations

from typing import Any

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.app.recap_feedback import RecapFeedbackOutbox
from core.app.retrospective_consumption_router import RetrospectiveConsumptionRouter
from core.cognitive.feedback_entrypoints import record_recap_feedback


def route_authenticated_recap_feedback(
    *,
    session: Any,
    feedback_type: str,
    comment: str,
    source_agent: str,
    supersedes_event_id: str,
    principal: PrincipalEnvelope,
    narrowing: AccessNarrowing,
) -> dict[str, Any]:
    """Record one canonical reaction, then bind the recap correction outbox."""

    from core.config import get_config

    recap_snapshot = {
        "recap_id": session.recap_id,
        "task_id": session.task_id,
        "state": session.state,
        "owner_agent": session.owner_agent,
        "source_agent": session.source_agent,
        "source_agents": list(session.source_agents),
        "session_id": session.session_id,
        "mode": session.mode,
        "topic": session.topic,
        "project": session.project,
        "task_type": session.task_type,
        "subtype": session.subtype,
        "answers": dict(session.answers),
        "draft": session.draft.to_dict() if session.draft else None,
        "finalized_page": session.finalized_page,
        "completion_receipt": dict(session.completion_receipt),
        "evidence_refs": list(session.evidence_refs),
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }
    database_dir = get_config().database_dir
    canonical_supersedes = ""
    if supersedes_event_id:
        prior_binding = RecapFeedbackOutbox(
            database_dir / "recap_tasks.db"
        ).canonical_feedback(supersedes_event_id)
        canonical_supersedes = str(prior_binding.get("feedback_event_id") or "")
    canonical = record_recap_feedback(
        database_dir=database_dir,
        recap_snapshot=recap_snapshot,
        feedback_type=feedback_type,
        comment=comment,
        principal=principal,
        narrowing=narrowing,
        supersedes_event_id=canonical_supersedes,
    )
    result = RetrospectiveConsumptionRouter().route_feedback(
        recap_id=session.recap_id,
        feedback_type=feedback_type,
        comment=comment,
        source_agent=source_agent,
        supersedes_ref=supersedes_event_id,
        canonical_feedback=canonical,
    )
    response = {
        "success": bool(result["terminal"]),
        **result,
        "canonical_feedback": canonical,
    }
    if not result["terminal"]:
        response["error"] = "required recap correction receipts are incomplete"
    return response
