"""Durable complete-session handoff from SyncEngine to Amphora."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.evidence.artifact_capture import build_capture_artifact_refs

from .agent_source import AgentSource, SessionInfo, Turn


logger = logging.getLogger(__name__)


def _revision_identity(turn: Turn) -> tuple[str, str]:
    metadata = turn.metadata or {}
    return (
        str(metadata.get("raw_event_id") or metadata.get("provenance_id") or ""),
        str(metadata.get("raw_content_hash") or ""),
    )


def _require_revision_identity(turn: Turn) -> tuple[str, str]:
    revision_id, content_hash = _revision_identity(turn)
    if not revision_id or not content_hash:
        raise ValueError(
            "complete-session handoff requires authoritative Raw revision and content hash"
        )
    return revision_id, content_hash


def build_session_messages(turns: list[Turn]) -> list[dict[str, Any]]:
    """Build lossless role messages with exact Raw revision subspans."""
    messages: list[dict[str, Any]] = []
    for turn in sorted(turns, key=lambda item: item.turn_number):
        user = str(turn.user_content or "")
        assistant = str(turn.assistant_content or "")
        if not user and not assistant:
            continue
        revision_id, content_hash = _require_revision_identity(turn)
        for role, content, span_start in (
            ("user", user, 0),
            ("assistant", assistant, len(user)),
        ):
            if not content:
                continue
            message: dict[str, Any] = {
                "role": role,
                "content": content,
                "turn": turn.turn_number,
                "turn_number": turn.turn_number,
            }
            if role == "user":
                for key in (
                    "asset_kind",
                    "content_source",
                    "source_authority",
                    "source_authority_purpose",
                ):
                    if (turn.metadata or {}).get(key) not in (None, ""):
                        message[key] = (turn.metadata or {})[key]
            message["source_span"] = {
                "revision_id": revision_id,
                "turn_number": turn.turn_number,
                "content_hash": content_hash,
                "role": role,
                "span_start": span_start,
                "span_end": span_start + len(content),
            }
            messages.append(message)
    return messages


def build_session_raw_event_refs(turns: list[Turn]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for turn in sorted(turns, key=lambda item: item.turn_number):
        span_end = len(str(turn.user_content or "")) + len(str(turn.assistant_content or ""))
        if span_end <= 0:
            continue
        revision_id, content_hash = _require_revision_identity(turn)
        refs.append(
            {
                "revision_id": revision_id,
                "turn_number": turn.turn_number,
                "content_hash": content_hash,
                "span_start": 0,
                "span_end": span_end,
            }
        )
    return refs


def build_session_artifact_refs(
    *,
    source_agent: str,
    session_id: str,
    turns: list[Turn],
    managed_database_dir: Path | None = None,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for turn in sorted(turns, key=lambda item: item.turn_number):
        metadata = turn.metadata or {}
        supplied_refs = metadata.get("artifact_refs")
        reasoning_path = str(metadata.get("reasoning_artifact_path") or "")
        capture_path = str(metadata.get("artifact_path") or "")
        has_artifacts = bool(
            supplied_refs is not None
            or reasoning_path
            or capture_path
            or turn.tool_results
            or turn.attachments
        )
        if not has_artifacts:
            continue
        revision_id, _ = _require_revision_identity(turn)
        if supplied_refs is not None and not isinstance(supplied_refs, list):
            refs.append({"_invalid_artifact_ref": True})
        elif isinstance(supplied_refs, list):
            for raw_ref in supplied_refs:
                if not isinstance(raw_ref, dict):
                    refs.append({"_invalid_artifact_ref": True})
                    continue
                normalized = dict(raw_ref)
                normalized["source_event_id"] = revision_id
                normalized["source_event_ids"] = [revision_id]
                refs.append(normalized)
        if capture_path == reasoning_path:
            capture_path = ""
        refs.extend(
            build_capture_artifact_refs(
                source_agent=source_agent,
                session_id=session_id,
                turn_number=turn.turn_number,
                source_event_id=revision_id,
                capture_artifact_path=capture_path,
                reasoning_artifact_path=reasoning_path,
                reasoning_sha256=str(metadata.get("reasoning_sha256") or ""),
                tool_results=turn.tool_results,
                attachments=turn.attachments,
                managed_database_dir=managed_database_dir or "",
            )
        )
    return refs


def enqueue_complete_session(
    *,
    database_dir: Path,
    source: AgentSource,
    session_info: SessionInfo,
    turns: list[Turn],
) -> dict[str, Any]:
    """Write the typed Amphora receipt for one complete canonical session."""
    try:
        from core.kia import amphora
    except ImportError as exc:
        raise RuntimeError("Amphora is required for durable distillation handoff") from exc

    messages = build_session_messages(turns)
    if not messages:
        return {
            "status": "intentional_skip",
            "terminal_reason": "session_contains_no_distillable_messages",
        }

    cognitive_sync_event_ids = [
        str((turn.metadata or {}).get("cognitive_sync_event_id"))
        for turn in turns
        if (turn.metadata or {}).get("cognitive_sync_event_id")
    ]
    meta = {
        "source": source.name,
        "working_dir": getattr(session_info, "working_dir", ".") or ".",
        "capture_source": "sync_engine",
        "canonical_session_id": session_info.session_id,
        "session_aliases": list(session_info.session_aliases),
        "source_kind": session_info.source_kind,
        "cognitive_sync_event_ids": cognitive_sync_event_ids,
        "raw_event_refs": build_session_raw_event_refs(turns),
        "artifact_refs": build_session_artifact_refs(
            source_agent=source.name,
            session_id=session_info.session_id,
            turns=turns,
            managed_database_dir=database_dir,
        ),
    }
    receipt = amphora.enqueue_with_receipt(
        session_id=session_info.session_id,
        messages=messages,
        meta=meta,
    )
    from core.ops.cognitive_pipeline_receipts import record_sync_handoff

    record_sync_handoff(database_dir, session_info.session_id, meta, receipt)
    logger.info(
        "[SyncEngine] 蒸馏 handoff %s/%s revision=%s task=%s",
        source.name,
        session_info.session_id[:8],
        receipt.input_revision[:12],
        receipt.task_id,
    )
    return receipt.to_dict()
