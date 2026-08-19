"""Transactional Capture-to-Amphora outbox and session-end receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any, Iterable


def _now() -> str:
    return datetime.now().isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def build_input_revision(events: Iterable[dict[str, Any]]) -> str:
    """Hash the ordered Capture inputs without relying on mutable session identity."""
    material = [
        {
            "turn_number": int(event.get("turn_number") or 0),
            "turn_id": str(event.get("turn_id") or ""),
            "content_hash": str(event.get("content_hash") or ""),
        }
        for event in events
    ]
    material.sort(key=lambda item: (item["turn_number"], item["turn_id"], item["content_hash"]))
    return hashlib.sha256(_json(material).encode("utf-8")).hexdigest()


def _message_source_span(
    event: dict[str, Any],
    metadata: dict[str, Any],
    *,
    role: str,
    span_start: int,
    span_end: int,
) -> dict[str, Any] | None:
    """Return the immutable raw span for one visible role message.

    ``meta.raw_event_refs`` deliberately remains one entry per complete raw
    turn.  Chunking needs finer-grained input provenance, though: a user
    message and its assistant reply can land in different chunks.  Keep that
    information on the message itself rather than changing the established
    outbox metadata contract.
    """
    revision_id = str(
        metadata.get("raw_event_id")
        or metadata.get("provenance_id")
        or event.get("raw_revision_id")
        or ""
    )
    if not revision_id:
        return None
    return {
        "revision_id": revision_id,
        "logical_event_id": str(metadata.get("logical_event_id") or ""),
        "turn_number": int(event.get("turn_number") or 0),
        "content_hash": str(
            metadata.get("raw_content_hash") or event.get("content_hash") or ""
        ),
        "role": role,
        "span_start": span_start,
        "span_end": span_end,
    }


def build_messages(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build lossless messages with original turn and role-local raw spans.

    The raw-turn span convention is unchanged: visible user content starts at
    zero and visible assistant content immediately follows it.  Every emitted
    message carries the exact subspan so a later chunk can derive its
    ``source_span_map`` without guessing from message position.
    """
    messages: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda item: int(item.get("turn_number") or 0)):
        payload = event.get("payload") or _decode(event.get("payload_json"), {})
        metadata = payload.get("metadata") or {}
        user = str(payload.get("user_content") or "")
        assistant = str(payload.get("assistant_content") or "")
        turn_number = int(event.get("turn_number") or 0)
        if user:
            message: dict[str, Any] = {
                "role": "user",
                "content": user,
                "turn": turn_number,
                "turn_number": turn_number,
            }
            for key in (
                "asset_kind",
                "content_source",
                "source_authority",
                "source_authority_purpose",
            ):
                if metadata.get(key) not in (None, ""):
                    message[key] = metadata[key]
            source_span = _message_source_span(
                event,
                metadata,
                role="user",
                span_start=0,
                span_end=len(user),
            )
            if source_span is not None:
                message["source_span"] = source_span
            messages.append(message)
        if assistant:
            message = {
                "role": "assistant",
                "content": assistant,
                "turn": turn_number,
                "turn_number": turn_number,
            }
            source_span = _message_source_span(
                event,
                metadata,
                role="assistant",
                span_start=len(user),
                span_end=len(user) + len(assistant),
            )
            if source_span is not None:
                message["source_span"] = source_span
            messages.append(message)
    return messages


def build_messages_revision(events: Iterable[dict[str, Any]]) -> str:
    """Return the same canonical content revision used by direct Amphora callers."""
    return hashlib.sha256(_json(build_messages(events)).encode("utf-8")).hexdigest()


def build_raw_event_refs(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build immutable, span-addressed raw provenance for the handoff payload."""
    refs: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda item: int(item.get("turn_number") or 0)):
        payload = event.get("payload") or _decode(event.get("payload_json"), {})
        metadata = payload.get("metadata") or {}
        revision_id = str(metadata.get("raw_event_id") or metadata.get("provenance_id") or "")
        if not revision_id:
            continue
        user = str(payload.get("user_content") or "")
        assistant = str(payload.get("assistant_content") or "")
        refs.append(
            {
                "revision_id": revision_id,
                "logical_event_id": str(metadata.get("logical_event_id") or ""),
                "turn_number": int(event.get("turn_number") or 0),
                "content_hash": str(
                    metadata.get("raw_content_hash") or event.get("content_hash") or ""
                ),
                "span_start": 0,
                "span_end": len(user) + len(assistant),
            }
        )
    return refs


def build_artifact_refs(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bind captured artifacts to the authoritative immutable Raw revision.

    Capture-time artifact helpers predate the Raw write receipt and therefore
    cannot own ``source_event_id``.  The handoff is the first boundary that has
    both values, so it replaces any provisional ID instead of trusting it.
    """
    refs: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda item: int(item.get("turn_number") or 0)):
        payload = event.get("payload") or _decode(event.get("payload_json"), {})
        metadata = payload.get("metadata") or {}
        revision_id = str(
            metadata.get("raw_event_id")
            or metadata.get("provenance_id")
            or event.get("raw_revision_id")
            or ""
        )
        if not revision_id:
            continue
        raw_refs = metadata.get("artifact_refs")
        if raw_refs is not None and not isinstance(raw_refs, list):
            refs.append({"_invalid_artifact_ref": True})
            continue
        if raw_refs is None:
            continue
        for raw_ref in raw_refs:
            if not isinstance(raw_ref, dict):
                refs.append({"_invalid_artifact_ref": True})
                continue
            normalized = dict(raw_ref)
            normalized["source_event_id"] = revision_id
            normalized["source_event_ids"] = [revision_id]
            refs.append(normalized)
    return refs


def build_cognitive_sync_event_ids(events: Iterable[dict[str, Any]]) -> list[str]:
    """Collect every explicit sync event that the handoff is allowed to consume."""
    event_ids: list[str] = []
    for event in events:
        payload = event.get("payload") or _decode(event.get("payload_json"), {})
        metadata = payload.get("metadata") or {}
        values = metadata.get("cognitive_sync_event_ids")
        if isinstance(values, (list, tuple)):
            event_ids.extend(str(value) for value in values if str(value))
            continue
        event_id = str(metadata.get("cognitive_sync_event_id") or "")
        if event_id:
            event_ids.append(event_id)
    return list(dict.fromkeys(event_ids))


class CaptureHandoffStore:
    """Keep the upstream terminal state and downstream handoff in one SQLite transaction."""

    @staticmethod
    def ensure_schema(conn: sqlite3.Connection) -> None:
        """Compatibility shim; DDL remains owned by CaptureQueueSchema."""
        from core.sync_framework.capture_schema import CaptureQueueSchema

        CaptureQueueSchema.ensure_handoff_schema(conn)

    @staticmethod
    def create(
        conn: sqlite3.Connection,
        *,
        source_agent: str,
        session_id: str,
        events: list[dict[str, Any]],
        enabled: bool = True,
        input_revision: str = "",
    ) -> dict[str, Any]:
        if not events:
            raise ValueError("capture handoff requires at least one event")
        missing_raw = [
            int(event.get("id") or 0)
            for event in events
            if not str(event.get("raw_revision_id") or "")
        ]
        if missing_raw:
            raise ValueError(
                "capture handoff requires a canonical Raw revision for every event: "
                f"{missing_raw}"
            )
        revision = input_revision or build_input_revision(events)
        receipt_id = (
            "handoff-"
            + hashlib.sha256(
                f"{source_agent}\0{session_id}\0{revision}".encode("utf-8")
            ).hexdigest()[:24]
        )
        event_ids = [int(event["id"]) for event in events]
        messages = build_messages(events)
        first_payload = events[0].get("payload") or _decode(events[0].get("payload_json"), {})
        meta = {
            "source": source_agent,
            "working_dir": first_payload.get("cwd", "."),
            "capture_source": "capture_worker",
            "completeness": first_payload.get("completeness", {}),
            "input_revision": revision,
            "handoff_receipt_id": receipt_id,
            "turn_range": [
                min(int(event.get("turn_number") or 0) for event in events),
                max(int(event.get("turn_number") or 0) for event in events),
            ],
            "raw_event_refs": build_raw_event_refs(events),
            "artifact_refs": build_artifact_refs(events),
            "cognitive_sync_event_ids": build_cognitive_sync_event_ids(events),
        }
        if not enabled:
            status, reason = "intentional_skip", "automatic_distillation_disabled"
        elif not messages:
            status, reason = "intentional_skip", "capture_contains_no_distillable_messages"
        else:
            status, reason = "handoff_pending", ""
        now = _now()
        conn.execute(
            """
            INSERT INTO capture_distillation_handoffs (
                receipt_id, source_agent, session_id, input_revision, status,
                event_ids_json, messages_json, meta_json, terminal_reason,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_agent, session_id, input_revision) DO UPDATE SET
                event_ids_json=excluded.event_ids_json,
                messages_json=excluded.messages_json,
                meta_json=excluded.meta_json,
                updated_at=excluded.updated_at
            """,
            (
                receipt_id,
                source_agent,
                session_id,
                revision,
                status,
                _json(event_ids),
                _json(messages),
                _json(meta),
                reason,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM capture_distillation_handoffs WHERE receipt_id=?", (receipt_id,)
        ).fetchone()
        handoff = CaptureHandoffStore.row(row)
        placeholders = ",".join("?" for _ in event_ids)
        event_status = (
            "done"
            if handoff.get("status") in {"committed", "intentional_skip"}
            else "handoff_pending"
        )
        conn.execute(
            f"UPDATE capture_events SET status=?, processed_at=? WHERE id IN ({placeholders})",  # nosec B608
            (event_status, now, *event_ids),
        )
        return handoff

    @staticmethod
    def row(row: sqlite3.Row | tuple | None) -> dict[str, Any]:
        if row is None:
            return {}
        data = dict(row)
        data["event_ids"] = _decode(data.pop("event_ids_json", "[]"), [])
        data["messages"] = _decode(data.pop("messages_json", "[]"), [])
        data["meta"] = _decode(data.pop("meta_json", "{}"), {})
        return data

    @staticmethod
    def list_dispatchable(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT * FROM capture_distillation_handoffs
            WHERE status IN ('handoff_pending', 'retryable_failed')
            ORDER BY created_at, receipt_id LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [CaptureHandoffStore.row(row) for row in rows]

    @staticmethod
    def mark_failed(conn: sqlite3.Connection, receipt_id: str, error: str) -> None:
        conn.execute(
            """
            UPDATE capture_distillation_handoffs
            SET status='retryable_failed', error=?, attempt_count=attempt_count+1, updated_at=?
            WHERE receipt_id=?
            """,
            (str(error)[:2000], _now(), receipt_id),
        )

    @staticmethod
    def commit(
        conn: sqlite3.Connection,
        receipt_id: str,
        *,
        downstream_receipt_id: str,
        downstream_task_id: str,
    ) -> None:
        if not downstream_receipt_id or not downstream_task_id:
            raise ValueError("downstream receipt and task identity are required")
        row = conn.execute(
            "SELECT event_ids_json FROM capture_distillation_handoffs WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"capture handoff not found: {receipt_id}")
        event_ids = [int(value) for value in _decode(row[0], [])]
        now = _now()
        conn.execute(
            """
            UPDATE capture_distillation_handoffs
            SET status='committed', downstream_receipt_id=?, downstream_task_id=?,
                error='', updated_at=? WHERE receipt_id=?
            """,
            (downstream_receipt_id, downstream_task_id, now, receipt_id),
        )
        if event_ids:
            placeholders = ",".join("?" for _ in event_ids)
            conn.execute(
                f"UPDATE capture_events SET status='done', processed_at=?, error=NULL "  # nosec B608
                f"WHERE id IN ({placeholders})",
                (now, *event_ids),
            )
