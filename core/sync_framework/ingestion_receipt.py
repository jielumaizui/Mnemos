# -*- coding: utf-8 -*-
"""Canonical ingestion receipt helpers for formal content ingestion."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


def create_ingestion_receipt(
    *,
    content: str,
    source_agent: str,
    source_path: str = "",
    session_id: str = "",
    title: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    capture_service: Any = None,
) -> Dict[str, Any]:
    """Capture formal ingest content before L1/Wiki writes and return provenance IDs."""
    from core.sync_framework.capture_service import CaptureService

    normalized_content = content or ""
    normalized_source = source_agent or "ingestion"
    receipt_session_id = session_id or _derive_session_id(
        normalized_source,
        source_path=source_path,
        title=title,
        content=normalized_content,
    )
    service = capture_service or CaptureService(start_worker=False)
    capture_metadata = {
        "capture_source": "ingestion_receipt",
        "ingestion_title": title,
        "source_path": source_path,
        **(metadata or {}),
    }
    result = service.capture_turn(
        source_agent=normalized_source,
        session_id=receipt_session_id,
        turn_number=1,
        user_content=normalized_content,
        assistant_content="",
        timestamp=datetime.now().isoformat(),
        model=normalized_source,
        metadata=capture_metadata,
        source_files=[source_path] if source_path else [],
        completeness={"visible_text": "full", "truncated": False},
    )
    status = str(result.get("status") or "error")
    raw_event_id = str(result.get("raw_event_id") or "")
    source_event_id = str(
        result.get("source_event_id")
        or raw_event_id
        or f"capture:{normalized_source}:{receipt_session_id}:1"
    )
    success = status in {"queued", "duplicate"} and result.get("raw_event_status") != "failed"
    return {
        "success": success,
        "schema_version": "mnemos.ingestion_receipt.v1",
        "status": status,
        "source_agent": normalized_source,
        "session_id": receipt_session_id,
        "turn_number": 1,
        "source_event_id": source_event_id,
        "raw_event_id": raw_event_id,
        "provenance_id": raw_event_id or source_event_id,
        "capture_result": result,
    }


def _derive_session_id(source_agent: str, *, source_path: str, title: str, content: str) -> str:
    basis = source_path or title or content[:1024]
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    source = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in source_agent)
    if source_path:
        stem = Path(source_path).stem[:48] or "document"
        return f"{source}:{stem}:{digest}"
    return f"{source}:{digest}"
