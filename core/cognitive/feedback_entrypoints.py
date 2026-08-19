"""Thin application adapters into the canonical feedback attribution owner."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.feedback_attribution import (
    FeedbackAttributionStore,
    UserReactionInput,
)
from core.cognitive.feedback_migration_barrier import (
    assert_feedback_writes_enabled,
)
from core.cognitive.state_contract import sha256_json
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.feedback_proposal_gate import (
    build_gated_feedback_target_adapters,
)
from core.cognitive.delivery_router import (
    verify_delivery_presentation,
)


def _terminal_receipts_payload(receipts: list[Any]) -> list[dict[str, Any]]:
    """Serialize the complete public CognitiveUpdateReceipt contract."""

    return [item.to_dict() for item in receipts]


def record_predictive_feedback(
    *,
    database_dir: Path,
    topic: str,
    action: str,
    delivery_event_id: str,
    principal: PrincipalEnvelope,
    narrowing: AccessNarrowing,
    supersedes_event_id: str = "",
    correction_target_ref: str = "",
    correction_reason: str = "",
) -> dict[str, Any]:
    """Validate one delivered item and record one canonical user reaction."""

    assert_feedback_writes_enabled(database_dir)

    normalized_topic = str(topic or "").strip().lower()
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {
        "accept",
        "ignore",
        "dismiss",
        "inaccurate",
        "outdated",
    }:
        return {
            "success": False,
            "error": "action 必须是 accept / ignore / dismiss / inaccurate / outdated 之一",
        }
    if not normalized_topic:
        return {"success": False, "error": "topic 不能为空"}
    if normalized_action in {"inaccurate", "outdated"} and (
        not supersedes_event_id or not correction_target_ref or not correction_reason
    ):
        return {
            "success": False,
            "reason": "correction_requires_latest_event_target_and_reason",
        }
    delivery = _delivery_binding(
        Path(database_dir) / "delivery_events.db",
        delivery_event_id=delivery_event_id,
        topic=normalized_topic,
        principal=principal,
    )
    if "reason" in delivery:
        return {"success": False, "reason": delivery["reason"]}
    state = CognitiveStateStore(Path(database_dir) / "producer_consumer_ledger.db")
    prediction = _prediction_binding(state, delivery_event_id)
    if not prediction:
        return {"success": False, "reason": "canonical_prediction_not_found"}
    prediction_principal = dict(prediction.get("principal_ref") or {})
    if (
        prediction_principal.get("principal_id") != principal.principal_id
        or prediction_principal.get("agent") != principal.agent
    ):
        return {"success": False, "reason": "prediction_principal_mismatch"}
    prediction_scope = dict(prediction.get("access_scope") or {})
    canonical_session = str(prediction_scope.get("session_id") or "").strip()
    canonical_project = str(prediction_scope.get("project") or "").strip().lower()
    requested_session = str(narrowing.session_id or "").strip()
    requested_project = str(narrowing.project or "").strip().lower()
    if (
        requested_session
        and requested_session != canonical_session
        or requested_project
        and requested_project != canonical_project
    ):
        return {"success": False, "reason": "prediction_scope_mismatch"}
    effective_narrowing = AccessNarrowing(
        session_id=canonical_session,
        project=canonical_project,
    )
    delivery_hash = str(
        prediction.get("delivery_event_payload_hash")
        or sha256_json(delivery["delivery_snapshot"])
    )
    presentation = dict(delivery["presentation_receipt"])
    source_snapshot = {
        "schema_version": "mnemos.predictive_feedback_input.v1",
        "topic": normalized_topic,
        "action": normalized_action,
        "delivery_event_id": delivery_event_id,
        "delivery_event_payload_hash": delivery_hash,
        "presentation_receipt_hash": presentation["receipt_hash"],
        "rendered_content_hash": presentation["rendered_content_hash"],
        "principal_id": principal.principal_id,
        "principal_agent": principal.agent,
        "project": effective_narrowing.project,
        "session_id": effective_narrowing.session_id,
        "supersedes_event_id": supersedes_event_id,
        "correction_target_ref": correction_target_ref,
        "correction_reason": correction_reason,
    }
    source_hash = sha256_json(source_snapshot)
    source_identity = source_hash.split(":", 1)[1][:32]
    scope_type, scope_id = _feedback_scope(effective_narrowing)
    access = make_cognitive_access_envelope(
        owner_principal_id=principal.principal_id,
        owner_agent=principal.agent,
        scope_type=scope_type,
        scope_id=scope_id,
        session_id=effective_narrowing.session_id,
        project=effective_narrowing.project,
        purposes=("cognitive_state_read", "cognitive_state_write"),
        consent_provenance_refs=(
            f"delivery-event:{delivery_event_id}",
            f"feedback-input:{source_hash}",
        ),
        sensitivity="sensitive",
        retention_policy="feedback_attribution",
        source_acl_lineage=(source_hash,),
        visibility="private",
    )
    matching_replays = tuple(
        revision
        for revision in state.current_revisions(object_type="user_reaction_event")
        if revision.payload["delivery_ref"]["event_id"] == delivery_event_id
        and revision.payload["principal_ref"]["principal_id"] == principal.principal_id
        and revision.payload["source_event_ref"]["content_hash"] == source_hash
    )
    if len(matching_replays) > 1:
        raise RuntimeError("feedback input maps to multiple current reaction heads")
    now = (
        str(matching_replays[0].payload["observed_at"])
        if matching_replays
        else datetime.now(timezone.utc).isoformat()
    )
    reaction = UserReactionInput(
        source_event_id=f"feedback-input-{source_identity}",
        source_revision_id=f"mcp-feedback:{source_identity}",
        source_content_hash=source_hash,
        observed_at=now,
        scope_type=scope_type,
        scope_id=scope_id,
        source_channel="predictive_push",
        subject_ref={"type": "delivery", "id": delivery_event_id},
        kind=normalized_action,
        evidence_refs=(
            f"delivery-event:{delivery_event_id}",
            f"delivery-presentation:{presentation['receipt_hash']}",
        ),
        evidence_content_hashes=(
            delivery_hash,
            presentation["rendered_content_hash"],
        ),
        access_control=access,
        delivery_ref={
            "state": "available",
            "event_id": delivery_event_id,
            "event_payload_hash": delivery_hash,
            "unavailable_reason": "",
        },
        display_ref={
            "state": "available",
            "display_id": presentation["receipt_hash"],
            "content_hash": presentation["rendered_content_hash"],
            "unavailable_reason": "",
        },
        prediction_ref=prediction.get("prediction_ref") or _unavailable_entity_ref(),
        decision_ref=prediction.get("decision_ref") or _unavailable_entity_ref(),
        action_ref=prediction.get("action_ref") or _unavailable_entity_ref(),
        exposure_id=delivery_event_id,
        interface_id="predictive-push-card",
        supersedes_event_id=supersedes_event_id,
        correction_of_event_id=(
            supersedes_event_id
            if normalized_action in {"inaccurate", "outdated"}
            else ""
        ),
        correction_target_ref=correction_target_ref,
        correction_reason=correction_reason,
    )
    owner = FeedbackAttributionStore(
        state,
        target_adapters=build_gated_feedback_target_adapters(database_dir),
    )
    receipt = (
        owner.correct_reaction(reaction, principal)
        if normalized_action in {"inaccurate", "outdated"}
        else owner.record_reaction(reaction, principal)
    )
    terminal_receipts = []
    for command_id in receipt.command_ids:
        terminal_receipts.append(owner.process_command(command_id))
    pending = tuple(
        command_id
        for command_id in receipt.command_ids
        if state.effect_receipt(command_id) is None
    )
    return {
        "success": True,
        "status": "complete" if not pending else "pending",
        "terminal_status": "complete" if not pending else "pending",
        "topic": normalized_topic,
        "action": normalized_action,
        "delivery_event_id": delivery_event_id,
        "feedback_event_id": receipt.event_id,
        "reaction_id": receipt.reaction_id,
        "reaction_revision_id": receipt.reaction_revision_id,
        "attribution_id": receipt.attribution_id,
        "attribution_revision_id": receipt.attribution_revision_id,
        "disposition": receipt.disposition,
        "command_ids": list(receipt.command_ids),
        "pending_command_ids": list(pending),
        "required_receipts_complete": not pending,
        "terminal_receipts": _terminal_receipts_payload(terminal_receipts),
        "principal": {
            "principal_id": principal.principal_id,
            "principal_agent": principal.agent,
            "project": effective_narrowing.project,
            "session_id": effective_narrowing.session_id,
        },
        "effect_delta": {
            "direct_domain_updates": 0,
            "proposal_commands": sum(
                1
                for command_id in pending
                if (state.command(command_id) or {}).get("payload", {}).get("eligible")
            ),
        },
    }


def record_context_search_feedback(
    *,
    database_dir: Path,
    search_object_id: int,
    search_session_id: str,
    query: str,
    result_paths_json: str,
    interaction: str,
    result_path: str,
    access_control: Mapping[str, Any],
    principal: PrincipalEnvelope,
) -> dict[str, Any]:
    """Record one exact search interaction without deriving ground truth."""

    assert_feedback_writes_enabled(database_dir)
    normalized_interaction = str(interaction or "").strip().lower()
    kind_by_interaction = {
        "open": "opened",
        "click": "clicked",
        "ignore": "ignore",
        "no_click": "no_click",
        "silence": "silence_window_closed",
    }
    kind = kind_by_interaction.get(normalized_interaction)
    if kind is None:
        raise ValueError("unsupported_context_search_interaction")
    if normalized_interaction in {"open", "click"} and not str(
        result_path or ""
    ).strip():
        raise ValueError("search result interaction requires result_path")
    normalized_session = str(search_session_id or "").strip()
    if not normalized_session:
        raise ValueError("search_session_id is required")
    scope = dict(access_control.get("scope") or {})
    scope_type = str(scope.get("scope_type") or "")
    scope_id = str(scope.get("scope_id") or "")
    if not scope_type or not scope_id:
        raise PermissionError("search feedback source ACL has no resolved scope")
    query_hash = sha256_json({"query": str(query)})
    results_hash = sha256_json({"result_paths_json": str(result_paths_json)})
    result_identity = (
        "path:"
        + sha256_json({"path": str(result_path)}).split(":", 1)[1][:32]
        if normalized_interaction in {"open", "click"}
        else "query:" + query_hash.split(":", 1)[1][:32]
    )
    exposure_id = f"search-exposure:{normalized_session}:{result_identity}"
    source_snapshot = {
        "schema_version": "mnemos.context_search_feedback_input.v1",
        "search_object_id": int(search_object_id),
        "search_session_id": normalized_session,
        "query_hash": query_hash,
        "result_paths_hash": results_hash,
        "interaction": normalized_interaction,
        "result_identity": result_identity,
        "principal_id": principal.principal_id,
    }
    source_hash = sha256_json(source_snapshot)
    state = CognitiveStateStore(Path(database_dir) / "producer_consumer_ledger.db")
    matching_replays = tuple(
        revision
        for revision in state.current_revisions(object_type="user_reaction_event")
        if revision.payload["source_event_ref"]["content_hash"] == source_hash
        and revision.payload["principal_ref"]["principal_id"]
        == principal.principal_id
    )
    if len(matching_replays) > 1:
        raise RuntimeError("search feedback maps to multiple current reaction heads")
    observed_at = (
        str(matching_replays[0].payload["observed_at"])
        if matching_replays
        else datetime.now(timezone.utc).isoformat()
    )
    source_identity = source_hash.split(":", 1)[1][:32]
    reaction = UserReactionInput(
        source_event_id=f"search-feedback-{source_identity}",
        source_revision_id=f"search-session:{int(search_object_id)}",
        source_content_hash=source_hash,
        observed_at=observed_at,
        scope_type=scope_type,
        scope_id=scope_id,
        source_channel="context_search",
        subject_ref={"type": "search_result", "id": result_identity},
        kind=kind,
        evidence_refs=(f"search-session:{int(search_object_id)}",),
        evidence_content_hashes=(source_hash,),
        access_control=access_control,
        search_ref={
            "state": "available",
            "session_id": normalized_session,
            "result_id": result_identity,
            "exposure_id": exposure_id,
            "unavailable_reason": "",
        },
        display_ref={
            "state": "available",
            "display_id": exposure_id,
            "content_hash": results_hash,
            "unavailable_reason": "",
        },
        exposure_id=exposure_id,
        interface_id="context-search-result-set",
    )
    owner = FeedbackAttributionStore(
        state,
        target_adapters=build_gated_feedback_target_adapters(database_dir),
    )
    receipt = owner.record_reaction(reaction, principal)
    terminal = []
    for command_id in receipt.command_ids:
        terminal.append(owner.process_command(command_id))
    pending = tuple(
        command_id
        for command_id in receipt.command_ids
        if state.effect_receipt(command_id) is None
    )
    return {
        "success": True,
        "status": "complete" if not pending else "pending",
        "feedback_event_id": receipt.event_id,
        "reaction_id": receipt.reaction_id,
        "reaction_revision_id": receipt.reaction_revision_id,
        "attribution_id": receipt.attribution_id,
        "attribution_revision_id": receipt.attribution_revision_id,
        "disposition": receipt.disposition,
        "command_ids": list(receipt.command_ids),
        "pending_command_ids": list(pending),
        "terminal_receipt_count": len(terminal),
        "terminal_receipts": _terminal_receipts_payload(terminal),
        "objective_ground_truth_created": False,
        "direct_domain_updates": 0,
    }


def record_reflection_feedback(
    *,
    database_dir: Path,
    reflection_id: str,
    feedback_type: str,
    comment: str,
    record_snapshot: Mapping[str, Any],
    access_control: Mapping[str, Any],
    principal: PrincipalEnvelope,
    supersedes_event_id: str = "",
    correction_target_ref: str = "",
    correction_reason: str = "",
) -> dict[str, Any]:
    """Record explicit Reflection feedback through the canonical owner."""

    assert_feedback_writes_enabled(database_dir)
    normalized_type = str(feedback_type or "").strip().lower()
    if normalized_type not in {"accurate", "inaccurate", "insightful", "irrelevant"}:
        raise ValueError("unsupported_reflection_feedback_type")
    normalized_id = str(reflection_id or "").strip()
    if not normalized_id:
        raise ValueError("reflection_id is required")
    scope = dict(access_control.get("scope") or {})
    scope_type = str(scope.get("scope_type") or "")
    scope_id = str(scope.get("scope_id") or "")
    if not scope_type or not scope_id:
        raise PermissionError("reflection feedback source ACL has no resolved scope")
    record_hash = sha256_json(record_snapshot)
    correction = normalized_type == "inaccurate"
    effective_target = (
        str(correction_target_ref or "").strip()
        or (f"reflection:{normalized_id}" if correction else "")
    )
    effective_reason = (
        str(correction_reason or "").strip()
        or str(comment or "").strip()
        or ("user_marked_reflection_inaccurate" if correction else "")
    )
    source_snapshot = {
        "schema_version": "mnemos.reflection_feedback_input.v1",
        "reflection_id": normalized_id,
        "reflection_content_hash": record_hash,
        "feedback_type": normalized_type,
        "comment_hash": sha256_json({"comment": str(comment or "")}),
        "principal_id": principal.principal_id,
        "supersedes_event_id": str(supersedes_event_id or ""),
        "correction_target_ref": effective_target,
        "correction_reason_hash": sha256_json({"reason": effective_reason}),
    }
    source_hash = sha256_json(source_snapshot)
    state = CognitiveStateStore(Path(database_dir) / "producer_consumer_ledger.db")
    current = tuple(
        revision
        for revision in state.current_revisions(object_type="user_reaction_event")
        if revision.payload["source_channel"] == "reflection"
        and revision.payload["subject_ref"]
        == {"type": "reflection", "id": normalized_id}
        and revision.payload["principal_ref"]["principal_id"]
        == principal.principal_id
    )
    if len(current) > 1:
        raise RuntimeError("reflection maps to multiple current reaction heads")
    replay = tuple(
        revision
        for revision in current
        if revision.payload["source_event_ref"]["content_hash"] == source_hash
    )
    observed_at = (
        str(replay[0].payload["observed_at"])
        if replay
        else datetime.now(timezone.utc).isoformat()
    )
    source_identity = source_hash.split(":", 1)[1][:32]
    reaction = UserReactionInput(
        source_event_id=f"reflection-feedback-{source_identity}",
        source_revision_id=f"reflection:{normalized_id}:{record_hash}",
        source_content_hash=source_hash,
        observed_at=observed_at,
        scope_type=scope_type,
        scope_id=scope_id,
        source_channel="reflection",
        subject_ref={"type": "reflection", "id": normalized_id},
        kind=normalized_type,
        evidence_refs=(f"reflection:{normalized_id}",),
        evidence_content_hashes=(record_hash,),
        access_control=access_control,
        display_ref={
            "state": "available",
            "display_id": f"reflection:{normalized_id}",
            "content_hash": record_hash,
            "unavailable_reason": "",
        },
        exposure_id=f"reflection:{normalized_id}",
        interface_id="reflection-feedback-card",
        supersedes_event_id=str(supersedes_event_id or ""),
        correction_of_event_id=(
            str(supersedes_event_id or "") if correction else ""
        ),
        correction_target_ref=effective_target,
        correction_reason=effective_reason,
    )
    owner = FeedbackAttributionStore(
        state,
        target_adapters=build_gated_feedback_target_adapters(database_dir),
    )
    receipt = (
        owner.correct_reaction(reaction, principal)
        if correction
        else owner.record_reaction(reaction, principal)
    )
    terminal = [owner.process_command(command_id) for command_id in receipt.command_ids]
    return {
        "success": True,
        "feedback_event_id": receipt.event_id,
        "reaction_id": receipt.reaction_id,
        "reaction_revision_id": receipt.reaction_revision_id,
        "attribution_id": receipt.attribution_id,
        "attribution_revision_id": receipt.attribution_revision_id,
        "disposition": receipt.disposition,
        "command_ids": list(receipt.command_ids),
        "terminal_receipt_count": len(terminal),
        "terminal_receipts": _terminal_receipts_payload(terminal),
        "legacy_reflection_feedback_updated": False,
        "direct_domain_updates": 0,
    }


def record_recap_feedback(
    *,
    database_dir: Path,
    recap_snapshot: Mapping[str, Any],
    feedback_type: str,
    comment: str,
    principal: PrincipalEnvelope,
    narrowing: AccessNarrowing,
    supersedes_event_id: str = "",
) -> dict[str, Any]:
    """Record authenticated recap feedback as one canonical reaction chain."""

    assert_feedback_writes_enabled(database_dir)
    normalized_type = str(feedback_type or "").strip().lower()
    if normalized_type not in {
        "accurate",
        "inaccurate",
        "useful",
        "irrelevant",
        "outdated",
    }:
        raise ValueError("unsupported_recap_feedback_type")
    recap_id = str(recap_snapshot.get("recap_id") or "").strip()
    if not recap_id:
        raise ValueError("recap_id is required")
    source_session_id = str(recap_snapshot.get("session_id") or "").strip()
    source_project = str(recap_snapshot.get("project") or "").strip().lower()
    caller_session_id = str(narrowing.session_id or "").strip()
    caller_project = str(narrowing.project or "").strip().lower()
    _feedback_scope(narrowing)
    if source_session_id:
        if caller_session_id != source_session_id:
            raise PermissionError("recap feedback session scope does not match its source")
    elif not source_project or caller_project != source_project:
        raise PermissionError("recap feedback project scope does not match its source")
    if caller_project and caller_project != source_project:
        raise PermissionError("recap feedback project scope does not match its source")
    effective_narrowing = AccessNarrowing(
        session_id=source_session_id,
        project=source_project,
    )
    scope_type, scope_id = _feedback_scope(effective_narrowing)
    recap_hash = sha256_json(recap_snapshot)
    correction = normalized_type in {"inaccurate", "outdated"}
    correction_reason = str(comment or "").strip()
    if correction and not correction_reason:
        raise ValueError("recap correction requires a reason")
    source_snapshot = {
        "schema_version": "mnemos.recap_feedback_input.v1",
        "recap_id": recap_id,
        "recap_revision_hash": recap_hash,
        "feedback_type": normalized_type,
        "comment_hash": sha256_json({"comment": str(comment or "")}),
        "principal_id": principal.principal_id,
        "supersedes_event_id": str(supersedes_event_id or ""),
    }
    source_hash = sha256_json(source_snapshot)
    state = CognitiveStateStore(Path(database_dir) / "producer_consumer_ledger.db")
    current = tuple(
        revision
        for revision in state.current_revisions(object_type="user_reaction_event")
        if revision.payload["source_channel"] == "retrospective"
        and revision.payload["subject_ref"]
        == {"type": "retrospective", "id": recap_id}
        and revision.payload["principal_ref"]["principal_id"]
        == principal.principal_id
    )
    if len(current) > 1:
        raise RuntimeError("recap maps to multiple current reaction heads")
    replay = tuple(
        revision
        for revision in current
        if revision.payload["source_event_ref"]["content_hash"] == source_hash
    )
    observed_at = (
        str(replay[0].payload["observed_at"])
        if replay
        else datetime.now(timezone.utc).isoformat()
    )
    source_identity = source_hash.split(":", 1)[1][:32]
    access = make_cognitive_access_envelope(
        owner_principal_id=principal.principal_id,
        owner_agent=principal.agent,
        scope_type=scope_type,
        scope_id=scope_id,
        session_id=effective_narrowing.session_id,
        project=effective_narrowing.project,
        purposes=("cognitive_state_read", "cognitive_state_write"),
        consent_provenance_refs=(
            f"recap-session:{recap_id}",
            f"feedback-input:{source_hash}",
        ),
        sensitivity="sensitive",
        retention_policy="feedback_attribution",
        source_acl_lineage=(source_hash,),
        visibility="private",
    )
    reaction = UserReactionInput(
        source_event_id=f"recap-feedback-{source_identity}",
        source_revision_id=f"recap-session:{recap_id}:{recap_hash}",
        source_content_hash=source_hash,
        observed_at=observed_at,
        scope_type=scope_type,
        scope_id=scope_id,
        source_channel="retrospective",
        subject_ref={"type": "retrospective", "id": recap_id},
        kind=normalized_type,
        evidence_refs=(f"recap-session:{recap_id}",),
        evidence_content_hashes=(recap_hash,),
        access_control=access,
        display_ref={
            "state": "available",
            "display_id": f"recap:{recap_id}",
            "content_hash": recap_hash,
            "unavailable_reason": "",
        },
        exposure_id=f"recap:{recap_id}",
        interface_id="retrospective-feedback-card",
        supersedes_event_id=str(supersedes_event_id or ""),
        correction_of_event_id=(
            str(supersedes_event_id or "") if correction else ""
        ),
        correction_target_ref=(f"retrospective:{recap_id}" if correction else ""),
        correction_reason=(correction_reason if correction else ""),
    )
    owner = FeedbackAttributionStore(
        state,
        target_adapters=build_gated_feedback_target_adapters(database_dir),
    )
    receipt = (
        owner.correct_reaction(reaction, principal)
        if correction
        else owner.record_reaction(reaction, principal)
    )
    terminal = [owner.process_command(command_id) for command_id in receipt.command_ids]
    return {
        "success": True,
        "feedback_event_id": receipt.event_id,
        "reaction_id": receipt.reaction_id,
        "reaction_revision_id": receipt.reaction_revision_id,
        "attribution_id": receipt.attribution_id,
        "attribution_revision_id": receipt.attribution_revision_id,
        "disposition": receipt.disposition,
        "command_ids": list(receipt.command_ids),
        "terminal_receipts": _terminal_receipts_payload(terminal),
        "direct_domain_updates": 0,
    }


def record_dialog_decision_feedback(
    *,
    database_dir: Path,
    proposal_snapshot: Mapping[str, Any],
    action: str,
    reason: str,
    principal: PrincipalEnvelope,
    narrowing: AccessNarrowing,
    supersedes_event_id: str = "",
) -> dict[str, Any]:
    """Record one authenticated trusted-proposal card response."""

    proposal_id = str(proposal_snapshot.get("proposal_id") or "").strip()
    kind = {
        "approve": "accept",
        "reject": "dismiss",
        "snooze": "ignore",
        "edit": "inaccurate",
    }.get(str(action or "").strip().lower())
    if not proposal_id or kind is None:
        raise ValueError("unsupported dialog proposal feedback")
    return _record_displayed_object_feedback(
        database_dir=database_dir,
        source_channel="dialog_decision_push",
        subject_type="trusted_proposal",
        subject_id=proposal_id,
        object_snapshot=proposal_snapshot,
        kind=kind,
        reason=reason,
        principal=principal,
        narrowing=narrowing,
        supersedes_event_id=supersedes_event_id,
        interface_id="trusted-proposal-decision-card",
    )


def record_dialog_reminder_feedback(
    *,
    database_dir: Path,
    reminder_snapshot: Mapping[str, Any],
    action: str,
    reason: str,
    principal: PrincipalEnvelope,
    narrowing: AccessNarrowing,
    supersedes_event_id: str = "",
) -> dict[str, Any]:
    """Record one authenticated reminder-card response."""

    reminder_id = str(reminder_snapshot.get("reminder_id") or "").strip()
    kind = {
        "resolve": "accept",
        "ignore": "ignore",
        "dismiss": "dismiss",
        "defer": "ignore",
    }.get(str(action or "").strip().lower())
    if not reminder_id or kind is None:
        raise ValueError("unsupported dialog reminder feedback")
    return _record_displayed_object_feedback(
        database_dir=database_dir,
        source_channel="dialog_reminder",
        subject_type="dialog_reminder",
        subject_id=reminder_id,
        object_snapshot=reminder_snapshot,
        kind=kind,
        reason=reason,
        principal=principal,
        narrowing=narrowing,
        supersedes_event_id=supersedes_event_id,
        interface_id="dialog-reminder-card",
    )


def _record_displayed_object_feedback(
    *,
    database_dir: Path,
    source_channel: str,
    subject_type: str,
    subject_id: str,
    object_snapshot: Mapping[str, Any],
    kind: str,
    reason: str,
    principal: PrincipalEnvelope,
    narrowing: AccessNarrowing,
    supersedes_event_id: str,
    interface_id: str,
) -> dict[str, Any]:
    assert_feedback_writes_enabled(database_dir)
    _feedback_scope(narrowing)
    object_session_id = f"{source_channel}:{subject_id}"
    scope_type, scope_id = "session", object_session_id
    object_hash = sha256_json(object_snapshot)
    correction = kind in {"inaccurate", "outdated"}
    correction_reason = str(reason or "").strip()
    if correction and not correction_reason:
        correction_reason = "user_edited_displayed_object"
    source_snapshot = {
        "schema_version": "mnemos.dialog_object_feedback_input.v1",
        "source_channel": source_channel,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "object_hash": object_hash,
        "kind": kind,
        "reason_hash": sha256_json({"reason": str(reason or "")}),
        "principal_id": principal.principal_id,
        "supersedes_event_id": supersedes_event_id,
    }
    source_hash = sha256_json(source_snapshot)
    state = CognitiveStateStore(Path(database_dir) / "producer_consumer_ledger.db")
    current = tuple(
        revision
        for revision in state.current_revisions(object_type="user_reaction_event")
        if revision.payload["source_channel"] == source_channel
        and revision.payload["subject_ref"]
        == {"type": subject_type, "id": subject_id}
        and revision.payload["principal_ref"]["principal_id"]
        == principal.principal_id
    )
    if len(current) > 1:
        raise RuntimeError("dialog object maps to multiple current reaction heads")
    replay = tuple(
        revision
        for revision in current
        if revision.payload["source_event_ref"]["content_hash"] == source_hash
    )
    observed_at = (
        str(replay[0].payload["observed_at"])
        if replay
        else datetime.now(timezone.utc).isoformat()
    )
    source_identity = source_hash.split(":", 1)[1][:32]
    access = make_cognitive_access_envelope(
        owner_principal_id=principal.principal_id,
        owner_agent=principal.agent,
        scope_type=scope_type,
        scope_id=scope_id,
        session_id=object_session_id,
        project="",
        purposes=("cognitive_state_read", "cognitive_state_write"),
        consent_provenance_refs=(
            f"{subject_type}:{subject_id}",
            f"feedback-input:{source_hash}",
        ),
        sensitivity="sensitive",
        retention_policy="feedback_attribution",
        source_acl_lineage=(source_hash,),
        visibility="private",
    )
    reaction = UserReactionInput(
        source_event_id=f"dialog-feedback-{source_identity}",
        source_revision_id=f"{subject_type}:{subject_id}:{object_hash}",
        source_content_hash=source_hash,
        observed_at=observed_at,
        scope_type=scope_type,
        scope_id=scope_id,
        source_channel=source_channel,
        subject_ref={"type": subject_type, "id": subject_id},
        kind=kind,
        evidence_refs=(f"{subject_type}:{subject_id}",),
        evidence_content_hashes=(object_hash,),
        access_control=access,
        display_ref={
            "state": "available",
            "display_id": f"{subject_type}:{subject_id}",
            "content_hash": object_hash,
            "unavailable_reason": "",
        },
        exposure_id=f"{subject_type}:{subject_id}",
        interface_id=interface_id,
        supersedes_event_id=supersedes_event_id,
        correction_of_event_id=(supersedes_event_id if correction else ""),
        correction_target_ref=(f"{subject_type}:{subject_id}" if correction else ""),
        correction_reason=(correction_reason if correction else ""),
    )
    owner = FeedbackAttributionStore(
        state,
        target_adapters=build_gated_feedback_target_adapters(database_dir),
    )
    receipt = (
        owner.correct_reaction(reaction, principal)
        if correction
        else owner.record_reaction(reaction, principal)
    )
    terminal = [owner.process_command(command_id) for command_id in receipt.command_ids]
    return {
        "success": True,
        "feedback_event_id": receipt.event_id,
        "reaction_revision_id": receipt.reaction_revision_id,
        "attribution_revision_id": receipt.attribution_revision_id,
        "disposition": receipt.disposition,
        "terminal_receipts": _terminal_receipts_payload(terminal),
        "direct_domain_updates": 0,
    }


def _feedback_scope(narrowing: AccessNarrowing) -> tuple[str, str]:
    session_id = str(narrowing.session_id or "").strip()
    project = str(narrowing.project or "").strip().lower()
    if session_id:
        return "session", session_id
    if project:
        return "project", project
    raise PermissionError("feedback attribution requires an exact project or session scope")


def _delivery_binding(
    db_path: Path,
    *,
    delivery_event_id: str,
    topic: str,
    principal: PrincipalEnvelope,
) -> dict[str, Any]:
    if not db_path.is_file() or not delivery_event_id:
        return {"reason": "delivery_event_not_found"}
    with sqlite3.connect(f"file:{db_path.resolve(strict=True)}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM delivery_events WHERE event_id=?",
            (delivery_event_id,),
        ).fetchone()
    if row is None:
        return {"reason": "delivery_event_not_found"}
    item = dict(row)
    try:
        metadata = json.loads(str(item.pop("metadata_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"reason": "delivery_event_metadata_invalid"}
    if str(item.get("subject") or "").strip().lower() != topic:
        return {"reason": "delivery_event_subject_mismatch"}
    if item.get("decision") != "deliver":
        return {"reason": "delivery_event_not_delivered"}
    principal_ref = metadata.get("delivery_principal")
    if not isinstance(principal_ref, Mapping):
        return {"reason": "delivery_event_principal_missing"}
    if (
        str(principal_ref.get("principal_id") or "") != principal.principal_id
        or str(principal_ref.get("agent") or "") != principal.agent
        or str(principal_ref.get("capability_id") or "") != principal.capability_id
    ):
        return {"reason": "delivery_event_principal_mismatch"}
    presentation_proof = verify_delivery_presentation(
        db_path,
        delivery_event_id=delivery_event_id,
        principal=principal,
    )
    if not presentation_proof["ok"]:
        return {"reason": presentation_proof["reason"]}
    presentation = dict(presentation_proof["presentation_receipt"])
    snapshot = {
        key: value
        for key, value in item.items()
        if key not in {"feedback", "feedback_at", "outcome_id"}
    }
    snapshot["metadata"] = metadata
    return {
        "delivery_snapshot": snapshot,
        "presentation_receipt": presentation,
    }


def _prediction_binding(
    state: CognitiveStateStore,
    delivery_event_id: str,
) -> dict[str, Any]:
    matches = tuple(
        revision
        for revision in state.current_revisions(object_type="prediction_record")
        if revision.payload["delivery_ref"]["event_id"] == delivery_event_id
    )
    if len(matches) > 1:
        raise RuntimeError("delivery event maps to multiple current predictions")
    if not matches:
        return {}
    prediction = matches[0]
    decision = prediction.payload["decision_ref"]
    decision_ref = (
        {
            "state": "available",
            "id": str(decision["decision_id"]),
            "revision_id": str(decision["revision_id"]),
            "content_hash": str(decision["revision_hash"]),
            "unavailable_reason": "",
        }
        if decision.get("revision_id") and decision.get("revision_hash")
        else {
            "state": "unavailable",
            "id": "",
            "revision_id": "",
            "content_hash": "",
            "unavailable_reason": "trust_decision_has_no_cognitive_revision",
        }
    )
    return {
        "delivery_event_payload_hash": prediction.payload["delivery_ref"][
            "event_payload_hash"
        ],
        "prediction_ref": {
            "state": "available",
            "id": prediction.object_id,
            "revision_id": prediction.revision_id,
            "content_hash": prediction.payload_hash,
            "unavailable_reason": "",
        },
        "decision_ref": decision_ref,
        "action_ref": {
            "state": "unavailable",
            "id": "",
            "revision_id": "",
            "content_hash": "",
            "unavailable_reason": "action_effect_is_not_a_cognitive_revision",
        },
        "access_scope": dict(prediction.payload["access_control"]["scope"]),
        "principal_ref": dict(prediction.payload["access_control"]["owner"]),
    }


def _unavailable_entity_ref() -> dict[str, str]:
    return {
        "state": "unavailable",
        "id": "",
        "revision_id": "",
        "content_hash": "",
        "unavailable_reason": "not_applicable",
    }
