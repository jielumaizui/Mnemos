"""Fail-closed evidence planning for legacy Amphora tasks without Raw spans.

The planner is deliberately read-only.  It binds an old role/content-only task
to immutable Raw revisions by either its exact cognitive sync-event set or, for
pre-ledger tasks, the unique Raw preimage immediately preceding the task.  It
never edits legacy messages and never derives authority from message position
unless the complete ordered visible payload matches the selected Raw turns.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.kia.amphora_provenance_support import (
    read_exact_regular_file_bytes,
    read_owned_message_asset_bytes,
)
from core.kia.amphora_types import SYSTEM_OWNED_META_KEYS
from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.sync_framework.raw_event_reader import decode_raw_revision_snapshot

SCHEMA_VERSION = "mnemos.amphora_source_span_reconciliation.v1"
MIGRATION_SCHEMA_VERSION = "mnemos.amphora_source_span_migration.v1"
CAPTURE_RAW_BACKFILL_SCHEMA_VERSION = "mnemos.capture_raw_backfill.v1"
MIGRATION_REASON_PREFIX = "superseded_by_verified_source_span_migration:"
_MAX_TEMPORAL_DISTANCE_SECONDS = 5 * 60
_MIN_DISAMBIGUATION_MARGIN_SECONDS = 60 * 60


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = connect_readonly_sqlite(path, timeout_seconds=30)
    connection.row_factory = sqlite3.Row
    return connection


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        local_timezone = datetime.now().astimezone().tzinfo or timezone.utc
        parsed = parsed.replace(tzinfo=local_timezone)
    return parsed.astimezone(timezone.utc)


def _json_mapping(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _json_strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(str(item) for item in value if str(item)))


def _visible_projection(messages: Any) -> list[dict[str, str]]:
    if not isinstance(messages, list) or not messages:
        raise ValueError("legacy_messages_invalid")
    projected: list[dict[str, str]] = []
    for message in messages:
        if (
            not isinstance(message, Mapping)
            or not isinstance(message.get("role"), str)
            or not isinstance(message.get("content"), str)
            or not str(message.get("content") or "")
        ):
            raise ValueError("legacy_messages_invalid")
        projected.append(
            {
                "role": str(message["role"]),
                "content": str(message["content"]),
            }
        )
    return projected


def _span_state(messages: Any) -> str:
    if not isinstance(messages, list) or not messages:
        return "invalid"
    present = [
        isinstance(message, Mapping) and isinstance(message.get("source_span"), Mapping)
        for message in messages
    ]
    if all(present):
        for message in messages:
            span = message["source_span"]
            try:
                start = int(span["span_start"])
                end = int(span["span_end"])
            except (KeyError, TypeError, ValueError):
                return "invalid"
            if (
                not str(span.get("revision_id") or "")
                or not str(span.get("content_hash") or "")
                or str(span.get("role") or message.get("role") or "")
                != str(message.get("role") or "")
                or start < 0
                or end <= start
                or end - start != len(str(message.get("content") or ""))
            ):
                return "invalid"
        return "exact"
    if any(present):
        return "partial"
    return "missing"


def messages_revision(messages: Sequence[Mapping[str, Any]]) -> str:
    """Return the exact canonical Amphora messages revision."""

    return hashlib.sha256(_json(list(messages)).encode("utf-8")).hexdigest()


def task_id(session_id: str, source_agent: str, input_revision: str) -> str:
    material = f"{source_agent}\0{session_id}\0{input_revision}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def historical_object_hash(
    row: Mapping[str, Any],
    messages_path: Path,
    *,
    database_path: Path,
) -> str:
    """Bind the complete mutable task preimage and immutable message bytes."""

    raw = read_owned_message_asset_bytes(
        database_path=database_path,
        messages_path=messages_path,
        purpose="source span messages asset",
    )
    identity = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "row": {str(key): row[key] for key in sorted(row)},
        "messages_asset": {
            "path": str(messages_path),
            "size": len(raw),
            "sha256": _sha256_bytes(raw),
        },
    }
    return _sha256_json(identity)


@dataclass(frozen=True)
class _RawTurn:
    logical_event_id: str
    revision_id: str
    source_agent: str
    session_id: str
    turn_number: int
    content_hash: str
    revision_created_at: str
    user_content: str
    assistant_content: str
    snapshot_hash: str

    @property
    def visible_messages(self) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if self.user_content:
            messages.append({"role": "user", "content": self.user_content})
        if self.assistant_content:
            messages.append({"role": "assistant", "content": self.assistant_content})
        return messages

    @property
    def evidence_identity(self) -> dict[str, Any]:
        return {
            "logical_event_id": self.logical_event_id,
            "revision_id": self.revision_id,
            "source_agent": self.source_agent,
            "session_id": self.session_id,
            "turn_number": self.turn_number,
            "content_hash": self.content_hash,
            "revision_created_at": self.revision_created_at,
            "snapshot_hash": self.snapshot_hash,
        }


class _RawResolver:
    def __init__(self, raw_path: Path, ledger_path: Path) -> None:
        self.raw_path = raw_path
        self.ledger_path = ledger_path
        self.raw = _connect_read_only(raw_path)
        self.events = self._load_events(ledger_path)

    def close(self) -> None:
        self.raw.close()

    @staticmethod
    def _load_events(path: Path) -> dict[str, dict[str, Any]]:
        if not path.is_file():
            return {}
        with _connect_read_only(path) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' " "AND name='cognitive_data_events'"
            ).fetchone()
            if table is None:
                return {}
            rows = conn.execute(
                "SELECT event_id, source_uri, content_hash, created_at "
                "FROM cognitive_data_events WHERE producer='sync_engine'"
            ).fetchall()
        return {
            str(row["event_id"]): {
                "event_id": str(row["event_id"]),
                "source_uri": str(row["source_uri"] or ""),
                "content_hash": str(row["content_hash"] or ""),
                "created_at": str(row["created_at"] or ""),
            }
            for row in rows
            if str(row["event_id"] or "")
        }

    @staticmethod
    def _parse_sync_uri(value: str) -> tuple[str, str, int] | None:
        prefix = "sync://"
        if not value.startswith(prefix):
            return None
        source_agent, separator, session_and_turn = value[len(prefix) :].partition("/")
        session_id, marker, turn_text = session_and_turn.rpartition("/turn/")
        if (
            not separator
            or not source_agent
            or not marker
            or not session_id
            or not turn_text.isdigit()
        ):
            return None
        return source_agent, session_id, int(turn_text)

    def _load_revision(self, revision_id: str) -> _RawTurn | None:
        row = self.raw.execute(
            """
            SELECT t.event_id, r.revision_id, t.source_agent, t.session_id,
                   t.turn_number, r.content_hash, r.created_at, r.snapshot_blob,
                   COALESCE(m.retention_state, 'active') AS retention_state,
                   EXISTS (
                       SELECT 1 FROM raw_subject_deletion_receipts AS d
                       WHERE d.event_id=t.event_id AND d.status='applied'
                   ) AS is_deleted
            FROM raw_turn_revisions AS r
            JOIN raw_turns AS t ON t.event_id=r.logical_event_id
            LEFT JOIN raw_metrics AS m ON m.event_id=t.event_id
            WHERE r.revision_id=?
            """,
            (revision_id,),
        ).fetchone()
        if row is None or bool(row["is_deleted"]) or row["retention_state"] == "eligible_delete":
            return None
        blob = bytes(row["snapshot_blob"] or b"")
        try:
            payload = decode_raw_revision_snapshot(blob)
        except ValueError:
            return None
        logical_event_id = str(row["event_id"] or "")
        source_agent = str(row["source_agent"] or "")
        session_id = str(row["session_id"] or "")
        content_hash = str(row["content_hash"] or "")
        try:
            payload_turn = int(str(payload.get("turn_number")))
        except (TypeError, ValueError):
            return None
        if (
            not logical_event_id
            or not str(row["revision_id"] or "")
            or not content_hash
            or str(payload.get("event_id") or "") != logical_event_id
            or str(payload.get("source_agent") or "") != source_agent
            or str(payload.get("session_id") or "") != session_id
            or payload_turn != int(row["turn_number"])
            or str(payload.get("content_hash") or "") != content_hash
        ):
            return None
        user = str(payload.get("user_content") or "")
        assistant = str(payload.get("assistant_content") or "")
        if not user and not assistant:
            return None
        return _RawTurn(
            logical_event_id=logical_event_id,
            revision_id=str(row["revision_id"]),
            source_agent=source_agent,
            session_id=session_id,
            turn_number=int(row["turn_number"]),
            content_hash=content_hash,
            revision_created_at=str(row["created_at"] or ""),
            user_content=user,
            assistant_content=assistant,
            snapshot_hash=_sha256_bytes(blob),
        )

    def _canonical_revision_id(self, event_id: str, revision_id: str) -> str | None:
        table = self.raw.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='raw_event_identity_aliases'"
        ).fetchone()
        if table is None:
            return revision_id
        alias = self.raw.execute(
            """
            SELECT canonical_revision_id, visible_payload_equal
            FROM raw_event_identity_aliases WHERE alias_event_id=?
            """,
            (event_id,),
        ).fetchone()
        if alias is None:
            return revision_id
        canonical_revision_id = str(alias["canonical_revision_id"] or "")
        if not canonical_revision_id:
            return None
        if bool(alias["visible_payload_equal"]):
            return canonical_revision_id
        # An identity alias changes the current logical-event projection; it
        # does not erase the immutable historical revision.  When visible
        # bytes differ, a historical task must keep the original revision or
        # it would falsely cite text it never consumed.  When only structured
        # fields differ, prefer the canonical revision because the role-local
        # bytes remain identical.
        alias_turn = self._load_revision(revision_id)
        canonical_turn = self._load_revision(canonical_revision_id)
        if alias_turn is None or canonical_turn is None:
            return None
        if alias_turn.visible_messages == canonical_turn.visible_messages:
            return canonical_revision_id
        return revision_id

    def from_raw_refs(
        self,
        *,
        source_agent: str,
        session_id: str,
        refs: Sequence[Mapping[str, Any]],
    ) -> tuple[list[_RawTurn] | None, str]:
        """Resolve caller-stored full-turn refs without trusting their claims."""

        ordered: list[_RawTurn] = []
        seen_revisions: set[str] = set()
        for ref in refs:
            revision_id = str(ref.get("revision_id") or "")
            original = self._load_revision(revision_id)
            if original is None:
                return None, "raw_ref_revision_missing"
            try:
                ref_turn = int(ref.get("turn_number", original.turn_number))
                ref_start = int(ref["span_start"])
                ref_end = int(ref["span_end"])
            except (KeyError, TypeError, ValueError):
                return None, "raw_ref_invalid"
            if (
                original.source_agent != source_agent
                or original.session_id != session_id
                or original.turn_number != ref_turn
                or str(ref.get("content_hash") or "") != original.content_hash
                or str(ref.get("logical_event_id") or original.logical_event_id)
                != original.logical_event_id
                or ref_start != 0
                or ref_end != len(original.user_content) + len(original.assistant_content)
            ):
                return None, "raw_ref_binding_mismatch"
            canonical_revision_id = self._canonical_revision_id(
                original.logical_event_id,
                original.revision_id,
            )
            if not canonical_revision_id:
                return None, "raw_ref_alias_not_visibly_equivalent"
            selected = self._load_revision(canonical_revision_id)
            if selected is None or selected.visible_messages != original.visible_messages:
                return None, "raw_ref_canonical_revision_invalid"
            if selected.revision_id in seen_revisions:
                return None, "raw_ref_revision_duplicate"
            seen_revisions.add(selected.revision_id)
            ordered.append(selected)
        if not ordered:
            return None, "raw_ref_set_empty"
        return ordered, "task_raw_event_refs"

    def from_capture_refs(
        self,
        *,
        source_agent: str,
        session_id: str,
        refs: Sequence[Mapping[str, Any]],
    ) -> tuple[list[_RawTurn] | None, str]:
        """Resolve revision IDs already bound by a Capture handoff receipt."""

        ordered: list[_RawTurn] = []
        seen_revisions: set[str] = set()
        for ref in refs:
            try:
                turn_number = int(ref["turn_number"])
            except (KeyError, TypeError, ValueError):
                return None, "capture_raw_revision_invalid"
            capture_visible = ref.get("visible_messages")
            if not isinstance(capture_visible, list) or not capture_visible:
                return None, "capture_raw_revision_invalid"
            revision_id = str(ref.get("revision_id") or "")
            if revision_id:
                original = self._load_revision(revision_id)
                if original is None:
                    return None, "capture_raw_revision_missing"
                if (
                    original.source_agent != source_agent
                    or original.session_id != session_id
                    or original.turn_number != turn_number
                    or original.visible_messages != capture_visible
                ):
                    return None, "capture_raw_revision_binding_mismatch"
                selected_revision_id = self._canonical_revision_id(
                    original.logical_event_id,
                    original.revision_id,
                )
                if not selected_revision_id:
                    return None, "capture_raw_alias_invalid"
                selected = self._load_revision(selected_revision_id)
            else:
                selected = self._capture_payload_revision(
                    source_agent=source_agent,
                    session_id=session_id,
                    turn_number=turn_number,
                    content_hash=str(ref.get("capture_content_hash") or ""),
                    visible_messages=capture_visible,
                    capture_created_at=str(ref.get("capture_created_at") or ""),
                )
            if selected is None:
                return None, "capture_raw_revision_missing"
            if selected.visible_messages != capture_visible:
                return None, "capture_raw_canonical_revision_invalid"
            if selected.revision_id in seen_revisions:
                return None, "capture_raw_revision_duplicate"
            seen_revisions.add(selected.revision_id)
            ordered.append(selected)
        if not ordered:
            return None, "capture_raw_revision_set_empty"
        return ordered, "capture_handoff_receipt"

    def _capture_payload_revision(
        self,
        *,
        source_agent: str,
        session_id: str,
        turn_number: int,
        content_hash: str,
        visible_messages: Sequence[Mapping[str, str]],
        capture_created_at: str,
    ) -> _RawTurn | None:
        """Resolve an old Capture payload to one exact immutable Raw revision."""

        rows = self.raw.execute(
            """
            SELECT t.event_id, r.revision_id, r.content_hash, r.full_content_hash
            FROM raw_turns AS t
            JOIN raw_turn_revisions AS r ON r.logical_event_id=t.event_id
            WHERE t.source_agent=? AND t.session_id=? AND t.turn_number=?
            ORDER BY r.created_at, r.revision_id
            """,
            (source_agent, session_id, turn_number),
        ).fetchall()
        exact: dict[str, _RawTurn] = {}
        hash_bound: dict[str, _RawTurn] = {}
        for row in rows:
            selected_revision_id = self._canonical_revision_id(
                str(row["event_id"] or ""),
                str(row["revision_id"] or ""),
            )
            if not selected_revision_id:
                continue
            candidate = self._load_revision(selected_revision_id)
            if candidate is None or candidate.visible_messages != list(visible_messages):
                continue
            exact[candidate.revision_id] = candidate
            if content_hash and content_hash in {
                str(row["content_hash"] or ""),
                str(row["full_content_hash"] or ""),
                candidate.content_hash,
            }:
                hash_bound[candidate.revision_id] = candidate
        candidates = list(hash_bound.values() or exact.values())
        return self._nearest_unique(candidates, capture_created_at)

    def capture_raw_backfill_specs(
        self,
        *,
        source_agent: str,
        session_id: str,
        refs: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """Return exact Capture payloads that have no immutable Raw revision."""

        specs: list[dict[str, Any]] = []
        for ref in refs:
            if str(ref.get("revision_id") or ""):
                continue
            try:
                turn_number = int(ref["turn_number"])
                capture_event_id = int(ref["capture_event_id"])
            except (KeyError, TypeError, ValueError):
                return None, "capture_raw_backfill_evidence_invalid"
            visible_messages = ref.get("visible_messages")
            payload = ref.get("capture_payload")
            content_hash = str(ref.get("capture_content_hash") or "")
            capture_created_at = str(ref.get("capture_created_at") or "")
            if (
                not isinstance(visible_messages, list)
                or not visible_messages
                or not isinstance(payload, Mapping)
                or not content_hash
                or _parse_timestamp(capture_created_at) is None
                or _sha256_json(payload) != str(ref.get("capture_payload_hash") or "")
            ):
                return None, "capture_raw_backfill_evidence_invalid"
            existing = self._capture_payload_revision(
                source_agent=source_agent,
                session_id=session_id,
                turn_number=turn_number,
                content_hash=content_hash,
                visible_messages=visible_messages,
                capture_created_at=capture_created_at,
            )
            if existing is not None:
                continue
            specs.append(
                {
                    "schema_version": CAPTURE_RAW_BACKFILL_SCHEMA_VERSION,
                    "capture_event_id": capture_event_id,
                    "handoff_receipt_id": str(ref.get("handoff_receipt_id") or ""),
                    "source_agent": source_agent,
                    "session_id": session_id,
                    "turn_number": turn_number,
                    "content_hash": content_hash,
                    "capture_created_at": capture_created_at,
                    "capture_payload_hash": str(ref["capture_payload_hash"]),
                    "capture_payload": dict(payload),
                }
            )
        if not specs:
            return None, "capture_raw_backfill_candidate_missing"
        return specs, "capture_raw_backfill_required"

    def _candidate_rows_for_event(self, event: Mapping[str, Any]) -> list[_RawTurn]:
        parsed = self._parse_sync_uri(str(event.get("source_uri") or ""))
        if parsed is None or not str(event.get("content_hash") or ""):
            return []
        source_agent, session_id, turn_number = parsed
        rows = self.raw.execute(
            """
            SELECT t.event_id, r.revision_id
            FROM raw_turns AS t
            JOIN raw_turn_revisions AS r ON r.logical_event_id=t.event_id
            WHERE t.source_agent=? AND t.session_id=? AND t.turn_number=?
              AND (r.content_hash=? OR r.full_content_hash=?)
            ORDER BY r.created_at, r.revision_id
            """,
            (
                source_agent,
                session_id,
                turn_number,
                str(event["content_hash"]),
                str(event["content_hash"]),
            ),
        ).fetchall()
        candidates: dict[str, _RawTurn] = {}
        for row in rows:
            canonical_revision_id = self._canonical_revision_id(
                str(row["event_id"] or ""),
                str(row["revision_id"] or ""),
            )
            if not canonical_revision_id:
                continue
            candidate = self._load_revision(canonical_revision_id)
            if candidate is None:
                continue
            if (
                candidate.source_agent != source_agent
                or candidate.session_id != session_id
                or candidate.turn_number != turn_number
            ):
                continue
            candidates[candidate.revision_id] = candidate
        return list(candidates.values())

    @staticmethod
    def _nearest_unique(
        candidates: Sequence[_RawTurn],
        evidence_time: str,
    ) -> _RawTurn | None:
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        event_time = _parse_timestamp(evidence_time)
        if event_time is None:
            return None
        ranked: list[tuple[float, str, _RawTurn]] = []
        for candidate in candidates:
            revision_time = _parse_timestamp(candidate.revision_created_at)
            if revision_time is None:
                return None
            ranked.append(
                (
                    abs((revision_time - event_time).total_seconds()),
                    candidate.revision_id,
                    candidate,
                )
            )
        ranked.sort(key=lambda item: (item[0], item[1]))
        if (
            ranked[0][0] > _MAX_TEMPORAL_DISTANCE_SECONDS
            or ranked[1][0] - ranked[0][0] < _MIN_DISAMBIGUATION_MARGIN_SECONDS
        ):
            return None
        return ranked[0][2]

    def from_cognitive_events(
        self,
        *,
        source_agent: str,
        session_id: str,
        event_ids: Sequence[str],
    ) -> tuple[list[_RawTurn] | None, str]:
        ordered: list[_RawTurn] = []
        seen_revisions: set[str] = set()
        for event_id in event_ids:
            event = self.events.get(str(event_id))
            if event is None:
                return None, "cognitive_event_missing"
            parsed = self._parse_sync_uri(str(event.get("source_uri") or ""))
            if parsed is None:
                return None, "cognitive_event_uri_invalid"
            if parsed[0] != source_agent or parsed[1] != session_id:
                return None, "cognitive_event_task_identity_mismatch"
            candidates = self._candidate_rows_for_event(event)
            if not candidates:
                return None, "linked_raw_revision_missing"
            selected = self._nearest_unique(candidates, str(event.get("created_at") or ""))
            if selected is None:
                return None, "linked_raw_revision_ambiguous"
            if selected.revision_id in seen_revisions:
                continue
            seen_revisions.add(selected.revision_id)
            ordered.append(selected)
        if not ordered:
            return None, "cognitive_event_set_empty"
        return ordered, "cognitive_sync_events"

    def from_temporal_preimage(
        self,
        *,
        source_agent: str,
        session_id: str,
        task_created_at: str,
        require_recent: bool = True,
        allow_postimage: bool = False,
    ) -> tuple[list[_RawTurn] | None, str]:
        task_time = _parse_timestamp(task_created_at)
        if task_time is None:
            return None, "task_timestamp_invalid"
        try:
            lexical_task_time = datetime.fromisoformat(str(task_created_at).replace("Z", "+00:00"))
        except ValueError:
            return None, "task_timestamp_invalid"
        lexical_upper_bound = (
            lexical_task_time
            + timedelta(seconds=_MAX_TEMPORAL_DISTANCE_SECONDS if allow_postimage else 0)
        ).isoformat()
        rows = self.raw.execute(
            """
            SELECT t.event_id, t.turn_number, r.revision_id, r.created_at
            FROM raw_turns AS t
            JOIN raw_turn_revisions AS r ON r.logical_event_id=t.event_id
            WHERE t.source_agent=? AND t.session_id=? AND r.created_at<=?
            ORDER BY t.turn_number, t.event_id, r.created_at DESC, r.revision_id DESC
            """,
            (source_agent, session_id, lexical_upper_bound),
        ).fetchall()
        grouped: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            revision_time = _parse_timestamp(str(row["created_at"] or ""))
            if revision_time is None:
                return None, "temporal_raw_timestamp_invalid"
            distance = (revision_time - task_time).total_seconds()
            if distance > (_MAX_TEMPORAL_DISTANCE_SECONDS if allow_postimage else 0):
                continue
            if require_recent and abs(distance) > _MAX_TEMPORAL_DISTANCE_SECONDS:
                continue
            grouped.setdefault(int(row["turn_number"]), []).append(row)
        if not grouped:
            return None, "temporal_raw_preimage_missing"
        selected: list[_RawTurn] = []
        for turn_number, group in sorted(grouped.items()):
            group.sort(
                key=lambda row: (
                    _parse_timestamp(str(row["created_at"] or ""))
                    or datetime.min.replace(tzinfo=timezone.utc),
                    str(row["revision_id"] or ""),
                ),
                reverse=True,
            )
            latest_created_at = str(group[0]["created_at"] or "")
            latest = [row for row in group if str(row["created_at"] or "") == latest_created_at]
            if len(latest) != 1:
                return None, "temporal_raw_preimage_ambiguous"
            revision_time = _parse_timestamp(latest_created_at)
            if revision_time is None:
                return None, "temporal_raw_preimage_too_distant"
            canonical_revision_id = self._canonical_revision_id(
                str(latest[0]["event_id"] or ""),
                str(latest[0]["revision_id"] or ""),
            )
            if not canonical_revision_id:
                return None, "temporal_raw_alias_not_equivalent"
            candidate = self._load_revision(canonical_revision_id)
            if (
                candidate is None
                or candidate.source_agent != source_agent
                or candidate.session_id != session_id
                or candidate.turn_number != turn_number
            ):
                return None, "temporal_raw_revision_invalid"
            selected.append(candidate)
        selected.sort(
            key=lambda turn: (
                turn.turn_number,
                turn.revision_created_at,
                turn.logical_event_id,
                turn.revision_id,
            )
        )
        return selected, "task_temporal_neighborhood"


class _CaptureHandoffResolver:
    """Read exact CaptureQueue receipts without provisioning or rewriting them."""

    def __init__(self, capture_path: Path) -> None:
        self.path = capture_path
        self.connection = _connect_read_only(capture_path) if capture_path.is_file() else None

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()

    def raw_refs(
        self,
        *,
        row: Mapping[str, Any],
        meta: Mapping[str, Any],
        legacy_visible: Sequence[Mapping[str, str]],
    ) -> tuple[list[dict[str, Any]] | None, str]:
        if self.connection is None:
            return None, "capture_handoff_unavailable"
        receipt_id = str(row.get("handoff_receipt_id") or meta.get("handoff_receipt_id") or "")
        if not receipt_id:
            return None, "capture_handoff_unavailable"
        handoff = self.connection.execute(
            "SELECT * FROM capture_distillation_handoffs WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        if handoff is None:
            return None, "capture_handoff_missing"
        if (
            str(handoff["source_agent"] or "") != str(row.get("source_agent") or "")
            or str(handoff["session_id"] or "") != str(row.get("session_id") or "")
            or str(handoff["input_revision"] or "") != str(row.get("input_revision") or "")
            or str(handoff["downstream_task_id"] or "") != str(row.get("task_id") or "")
        ):
            return None, "capture_handoff_task_binding_mismatch"
        try:
            handoff_messages = json.loads(str(handoff["messages_json"] or "[]"))
            event_ids = json.loads(str(handoff["event_ids_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, "capture_handoff_payload_invalid"
        try:
            if _visible_projection(handoff_messages) != list(legacy_visible):
                return None, "capture_handoff_visible_messages_differ"
        except ValueError:
            return None, "capture_handoff_payload_invalid"
        if not isinstance(event_ids, list) or not event_ids:
            return None, "capture_handoff_event_set_empty"
        try:
            ordered_event_ids = [int(value) for value in event_ids if not isinstance(value, bool)]
        except (TypeError, ValueError):
            return None, "capture_handoff_event_set_invalid"
        if len(ordered_event_ids) != len(event_ids) or len(set(ordered_event_ids)) != len(
            ordered_event_ids
        ):
            return None, "capture_handoff_event_set_invalid"
        placeholders = ",".join("?" for _ in ordered_event_ids)
        events = self.connection.execute(
            f"SELECT id, source_agent, session_id, turn_number, content_hash, "
            f"raw_revision_id, created_at, payload_json "
            f"FROM capture_events WHERE id IN ({placeholders})",  # nosec B608
            tuple(ordered_event_ids),
        ).fetchall()
        if len(events) != len(ordered_event_ids):
            return None, "capture_handoff_event_missing"
        events_by_id = {int(event["id"]): event for event in events}
        refs: list[dict[str, Any]] = []
        capture_visible: list[dict[str, str]] = []
        for event_id in ordered_event_ids:
            event = events_by_id[event_id]
            if str(event["source_agent"] or "") != str(row.get("source_agent") or "") or str(
                event["session_id"] or ""
            ) != str(row.get("session_id") or ""):
                return None, "capture_handoff_event_binding_mismatch"
            try:
                payload = json.loads(str(event["payload_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                return None, "capture_handoff_event_payload_invalid"
            if not isinstance(payload, Mapping):
                return None, "capture_handoff_event_payload_invalid"
            user = payload.get("user_content")
            assistant = payload.get("assistant_content")
            if user is not None and not isinstance(user, str):
                return None, "capture_handoff_event_payload_invalid"
            if assistant is not None and not isinstance(assistant, str):
                return None, "capture_handoff_event_payload_invalid"
            event_visible: list[dict[str, str]] = []
            if user:
                event_visible.append({"role": "user", "content": user})
            if assistant:
                event_visible.append({"role": "assistant", "content": assistant})
            capture_visible.extend(event_visible)
            if not event_visible:
                continue
            refs.append(
                {
                    "revision_id": str(event["raw_revision_id"]),
                    "turn_number": int(event["turn_number"] or 0),
                    # The Raw owner revalidates the authoritative hash and
                    # full span.  Capture's older content_hash may be a source
                    # digest, so these two fields are filled after lookup.
                    "capture_content_hash": str(event["content_hash"] or ""),
                    "capture_created_at": str(event["created_at"] or ""),
                    "capture_event_id": int(event["id"]),
                    "visible_messages": event_visible,
                    "handoff_receipt_id": receipt_id,
                    "capture_payload_hash": _sha256_json(payload),
                    "capture_payload": dict(payload),
                }
            )
        if capture_visible != list(legacy_visible):
            return None, "capture_handoff_capture_payload_differ"
        if not refs:
            return None, "capture_handoff_visible_event_set_empty"
        return refs, "capture_handoff_receipt"


def _canonical_messages(turns: Sequence[_RawTurn]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for turn in turns:
        for role, content, start in (
            ("user", turn.user_content, 0),
            ("assistant", turn.assistant_content, len(turn.user_content)),
        ):
            if not content:
                continue
            messages.append(
                {
                    "role": role,
                    "content": content,
                    "turn": turn.turn_number,
                    "turn_number": turn.turn_number,
                    "source_span": {
                        "revision_id": turn.revision_id,
                        "logical_event_id": turn.logical_event_id,
                        "turn_number": turn.turn_number,
                        "content_hash": turn.content_hash,
                        "role": role,
                        "span_start": start,
                        "span_end": start + len(content),
                    },
                }
            )
    return messages


def _raw_event_refs(turns: Sequence[_RawTurn]) -> list[dict[str, Any]]:
    return [
        {
            "revision_id": turn.revision_id,
            "logical_event_id": turn.logical_event_id,
            "turn_number": turn.turn_number,
            "content_hash": turn.content_hash,
            "span_start": 0,
            "span_end": len(turn.user_content) + len(turn.assistant_content),
        }
        for turn in turns
    ]


def _unique_visible_alignment(
    *,
    turns: Sequence[_RawTurn],
    legacy_visible: Sequence[Mapping[str, str]],
    required_turns: Sequence[_RawTurn],
) -> tuple[list[_RawTurn] | None, str]:
    """Find one exact ordered Raw-turn projection, or refuse ambiguity.

    Some historical source adapters reused a native session identity while handing
    Amphora only the current window.  Extra Raw turns therefore cannot simply
    be included.  This matcher permits skips only when the complete task
    role/content sequence has exactly one ordered Raw solution and every
    cognitive-event anchor is present in that solution.
    """

    required = {(turn.turn_number, turn.revision_id) for turn in required_turns}
    # State values are capped at two distinct paths: once two survive, the
    # result is ambiguous and retaining more paths cannot make it unique.
    # Required revisions already have one fixed position in ``turns``.  A
    # path that skips one can never become valid later, so rejecting that skip
    # avoids an exponential bit-mask state without changing the proof.
    states: dict[int, list[tuple[int, ...]]] = {0: [()]}
    for raw_index, turn in enumerate(turns):
        segment = turn.visible_messages
        identity = (turn.turn_number, turn.revision_id)
        next_states: dict[int, list[tuple[int, ...]]] = (
            {} if identity in required else {key: list(paths) for key, paths in states.items()}
        )
        for legacy_index, paths in states.items():
            segment_end = legacy_index + len(segment)
            if (
                not segment
                or segment_end > len(legacy_visible)
                or list(legacy_visible[legacy_index:segment_end]) != segment
            ):
                continue
            bucket = next_states.setdefault(segment_end, [])
            for path in paths:
                candidate = (*path, raw_index)
                if candidate not in bucket and len(bucket) < 2:
                    bucket.append(candidate)
        states = next_states
        if not states:
            return None, "visible_messages_differ"
    solutions = states.get(len(legacy_visible), [])
    if not solutions:
        return None, "visible_messages_differ"
    if len(solutions) != 1:
        return None, "visible_message_alignment_ambiguous"
    return [turns[index] for index in solutions[0]], "unique_visible_raw_alignment"


def _queue_rows(path: Path) -> list[dict[str, Any]]:
    with _connect_read_only(path) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' " "AND name='distillation_tasks'"
        ).fetchone()
        if table is None:
            return []
        rows = conn.execute(
            "SELECT * FROM distillation_tasks "
            "WHERE status IN ('pending', 'retryable_failed', 'partial', 'failed', 'processing') "
            "ORDER BY created_at, task_id"
        ).fetchall()
    return [dict(row) for row in rows]


def _verified_migration_count(queue_path: Path) -> tuple[int, Counter[str]]:
    verified = 0
    errors: Counter[str] = Counter()
    with _connect_read_only(queue_path) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='amphora_source_span_migrations'"
        ).fetchone()
        if table is None:
            return 0, errors
        rows = conn.execute(
            "SELECT * FROM amphora_source_span_migrations ORDER BY legacy_task_id"
        ).fetchall()
        for migration in rows:
            old = conn.execute(
                "SELECT * FROM distillation_tasks WHERE task_id=?",
                (str(migration["legacy_task_id"]),),
            ).fetchone()
            new = conn.execute(
                "SELECT * FROM distillation_tasks WHERE task_id=?",
                (str(migration["canonical_task_id"]),),
            ).fetchone()
            if old is None or new is None:
                errors["migration_task_missing"] += 1
                continue
            if (
                str(old["status"] or "") != "intentional_skip"
                or str(old["terminal_reason"] or "")
                != MIGRATION_REASON_PREFIX + str(new["task_id"])
                or str(new["input_revision"] or "")
                != str(migration["canonical_input_revision"] or "")
            ):
                errors["migration_task_binding_mismatch"] += 1
                continue
            try:
                messages_bytes = read_owned_message_asset_bytes(
                    database_path=queue_path,
                    messages_path=str(new["messages_path"] or ""),
                    purpose="canonical source span messages asset",
                )
                messages = json.loads(messages_bytes.decode("utf-8"))
                canonical_meta = _json_mapping(new["meta"])
            except (
                UnicodeError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                errors["migration_messages_invalid"] += 1
                continue
            canonical_revision = str(
                migration["canonical_input_revision"] or ""
            )
            if (
                _span_state(messages) != "exact"
                or messages_revision(messages) != canonical_revision
                or str(canonical_meta.get("messages_revision") or "")
                != canonical_revision
            ):
                errors["migration_task_binding_mismatch"] += 1
                continue
            manifest_path = Path(str(migration["backup_manifest_path"] or ""))
            try:
                manifest_bytes = read_exact_regular_file_bytes(
                    manifest_path,
                    purpose="source span backup manifest",
                )
            except ValueError:
                errors["migration_backup_manifest_missing"] += 1
                continue
            if _sha256_bytes(manifest_bytes) != str(migration["backup_manifest_file_hash"] or ""):
                errors["migration_backup_manifest_hash_mismatch"] += 1
                continue
            verified += 1
    return verified, errors


def build_plan(database_dir: Path) -> dict[str, Any]:
    """Build a metadata-only reviewed inventory plus internal apply objects."""

    queue_path = database_dir / "distill_queue.db"
    raw_path = database_dir / "raw_events.db"
    ledger_path = database_dir / "producer_consumer_ledger.db"
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry_run",
        "ok": False,
        "missing_span_tasks": 0,
        "candidate_tasks": 0,
        "exact_span_tasks": 0,
        "verified_migrations": 0,
        "blocked_by_reason": {},
        "blocked_by_source": {},
        "blocked_objects": [],
        "capture_raw_backfill_events": 0,
        "capture_raw_backfill_tasks": 0,
        "capture_raw_backfill_manifest_hash": _sha256_json([]),
        "raw_backfills": [],
        "objects": [],
    }
    if not queue_path.is_file() or not raw_path.is_file():
        result["error"] = "required_database_missing"
        return result
    verified, migration_errors = _verified_migration_count(queue_path)
    result["verified_migrations"] = verified
    blocked: Counter[str] = Counter(migration_errors)
    blocked_by_source: Counter[str] = Counter()
    blocked_objects: list[dict[str, Any]] = []
    raw_backfills: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    resolver = _RawResolver(raw_path, ledger_path)
    capture_resolver = _CaptureHandoffResolver(database_dir / "capture_queue.db")
    try:
        for row in _queue_rows(queue_path):

            def block(reason: str, **details: Any) -> None:
                blocked[reason] += 1
                source = str(row.get("source_agent") or "unknown")
                blocked_by_source[f"{source}:{reason}"] += 1
                blocked_objects.append(
                    {
                        "legacy_task_id": str(row.get("task_id") or ""),
                        "source_agent": source,
                        "session_id": str(row.get("session_id") or ""),
                        "reason": reason,
                        **details,
                    }
                )

            messages_path = Path(str(row.get("messages_path") or ""))
            try:
                messages_bytes = read_owned_message_asset_bytes(
                    database_path=queue_path,
                    messages_path=messages_path,
                    purpose="source span messages asset",
                )
                messages = json.loads(messages_bytes.decode("utf-8"))
            except (
                UnicodeError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                block("legacy_messages_invalid")
                continue
            span_state = _span_state(messages)
            if span_state == "exact":
                result["exact_span_tasks"] += 1
                continue
            result["missing_span_tasks"] += 1
            if str(row.get("status") or "") == "processing":
                block("processing_task_requires_timeout_reset")
                continue
            if span_state != "missing":
                block(f"legacy_source_span_{span_state}")
                continue
            try:
                legacy_visible = _visible_projection(messages)
                object_hash = historical_object_hash(
                    row,
                    messages_path,
                    database_path=queue_path,
                )
            except ValueError:
                block("legacy_messages_invalid")
                continue
            meta = _json_mapping(row.get("meta"))
            event_ids = _json_strings(meta.get("cognitive_sync_event_ids"))
            required_turns: list[_RawTurn] = []
            supplied_raw_refs = meta.get("raw_event_refs")
            if isinstance(supplied_raw_refs, list) and supplied_raw_refs:
                turns, match_method = resolver.from_raw_refs(
                    source_agent=str(row.get("source_agent") or ""),
                    session_id=str(row.get("session_id") or ""),
                    refs=[ref for ref in supplied_raw_refs if isinstance(ref, Mapping)],
                )
                if turns is not None:
                    required_turns = turns
            else:
                capture_refs, capture_reason = capture_resolver.raw_refs(
                    row=row,
                    meta=meta,
                    legacy_visible=legacy_visible,
                )
                if capture_refs is not None:
                    turns, match_method = resolver.from_capture_refs(
                        source_agent=str(row.get("source_agent") or ""),
                        session_id=str(row.get("session_id") or ""),
                        refs=capture_refs,
                    )
                    if turns is not None:
                        required_turns = turns
                    elif match_method == "capture_raw_revision_missing":
                        specs, backfill_reason = resolver.capture_raw_backfill_specs(
                            source_agent=str(row.get("source_agent") or ""),
                            session_id=str(row.get("session_id") or ""),
                            refs=capture_refs,
                        )
                        if specs is None:
                            match_method = backfill_reason
                        else:
                            for spec in specs:
                                raw_backfills.append(
                                    {
                                        **spec,
                                        "legacy_task_id": str(row.get("task_id") or ""),
                                    }
                                )
                elif capture_reason != "capture_handoff_unavailable":
                    turns, match_method = None, capture_reason
                elif event_ids:
                    linked_turns, match_method = resolver.from_cognitive_events(
                        source_agent=str(row.get("source_agent") or ""),
                        session_id=str(row.get("session_id") or ""),
                        event_ids=event_ids,
                    )
                    if linked_turns is None:
                        turns = None
                    else:
                        required_turns = linked_turns
                        temporal_turns, temporal_reason = resolver.from_temporal_preimage(
                            source_agent=str(row.get("source_agent") or ""),
                            session_id=str(row.get("session_id") or ""),
                            task_created_at=str(row.get("created_at") or ""),
                            require_recent=False,
                            allow_postimage=True,
                        )
                        if temporal_turns is None:
                            turns = None
                            match_method = temporal_reason
                        else:
                            by_revision = {turn.revision_id: turn for turn in temporal_turns}
                            by_revision.update({turn.revision_id: turn for turn in linked_turns})
                            turns = sorted(
                                by_revision.values(),
                                key=lambda turn: (
                                    turn.turn_number,
                                    turn.revision_created_at,
                                    turn.logical_event_id,
                                    turn.revision_id,
                                ),
                            )
                            match_method = "cognitive_sync_events_plus_task_neighborhood"
                else:
                    turns, match_method = resolver.from_temporal_preimage(
                        source_agent=str(row.get("source_agent") or ""),
                        session_id=str(row.get("session_id") or ""),
                        task_created_at=str(row.get("created_at") or ""),
                        require_recent=False,
                    )
            if turns is None:
                block(match_method, legacy_message_count=len(legacy_visible))
                continue
            canonical_messages = _canonical_messages(turns)
            canonical_visible = _visible_projection(canonical_messages)
            if canonical_visible != legacy_visible:
                aligned_turns, alignment_reason = _unique_visible_alignment(
                    turns=turns,
                    legacy_visible=legacy_visible,
                    required_turns=required_turns,
                )
                if aligned_turns is not None:
                    turns = aligned_turns
                    canonical_messages = _canonical_messages(turns)
                    canonical_visible = _visible_projection(canonical_messages)
                    match_method = match_method + "+unique_visible_raw_alignment"
            if canonical_visible != legacy_visible:
                mismatch = next(
                    (
                        index
                        for index, (legacy_message, canonical_message) in enumerate(
                            zip(legacy_visible, canonical_visible),
                            start=1,
                        )
                        if legacy_message != canonical_message
                    ),
                    min(len(legacy_visible), len(canonical_visible)) + 1,
                )
                legacy_at_mismatch = (
                    legacy_visible[mismatch - 1] if mismatch <= len(legacy_visible) else {}
                )
                canonical_at_mismatch = (
                    canonical_visible[mismatch - 1] if mismatch <= len(canonical_visible) else {}
                )
                block(
                    alignment_reason,
                    match_method=match_method,
                    cognitive_event_count=len(event_ids),
                    raw_turn_count=len(turns),
                    legacy_message_count=len(legacy_visible),
                    canonical_message_count=len(canonical_visible),
                    first_mismatch_ordinal=mismatch,
                    legacy_mismatch_role=str(legacy_at_mismatch.get("role") or ""),
                    canonical_mismatch_role=str(canonical_at_mismatch.get("role") or ""),
                    legacy_mismatch_length=len(str(legacy_at_mismatch.get("content") or "")),
                    canonical_mismatch_length=len(str(canonical_at_mismatch.get("content") or "")),
                    legacy_mismatch_hash=_sha256_json(legacy_at_mismatch),
                    canonical_mismatch_hash=_sha256_json(canonical_at_mismatch),
                )
                continue
            raw_preimage = [turn.evidence_identity for turn in turns]
            raw_preimage_hash = _sha256_json(raw_preimage)
            canonical_revision = messages_revision(canonical_messages)
            canonical_task_id = task_id(
                str(row.get("session_id") or ""),
                str(row.get("source_agent") or ""),
                canonical_revision,
            )
            migration_id = (
                "amphora-span-migration-"
                + hashlib.sha256(
                    (
                        str(row.get("task_id") or "")
                        + "\0"
                        + object_hash
                        + "\0"
                        + raw_preimage_hash
                        + "\0"
                        + canonical_task_id
                    ).encode("utf-8")
                ).hexdigest()[:32]
            )
            canonical_meta = {
                key: value
                for key, value in meta.items()
                if key not in SYSTEM_OWNED_META_KEYS
            }
            canonical_meta["input_revision"] = canonical_revision
            canonical_meta["raw_event_refs"] = _raw_event_refs(turns)
            canonical_meta["source_span_migration"] = {
                "schema_version": MIGRATION_SCHEMA_VERSION,
                "migration_id": migration_id,
                "legacy_task_id": str(row.get("task_id") or ""),
                "legacy_object_hash": object_hash,
                "raw_preimage_hash": raw_preimage_hash,
                "match_method": match_method,
            }
            objects.append(
                {
                    "legacy_task_id": str(row.get("task_id") or ""),
                    "legacy_input_revision": str(row.get("input_revision") or ""),
                    "legacy_object_hash": object_hash,
                    "messages_path": str(messages_path),
                    "source_agent": str(row.get("source_agent") or ""),
                    "session_id": str(row.get("session_id") or ""),
                    "canonical_task_id": canonical_task_id,
                    "canonical_input_revision": canonical_revision,
                    "canonical_messages": canonical_messages,
                    "canonical_meta": canonical_meta,
                    "raw_event_refs": _raw_event_refs(turns),
                    "raw_preimage_hash": raw_preimage_hash,
                    "raw_preimage": raw_preimage,
                    "match_method": match_method,
                    "migration_id": migration_id,
                }
            )
    finally:
        capture_resolver.close()
        resolver.close()
    merged_backfills: dict[tuple[str, str, int], dict[str, Any]] = {}
    backfill_tasks: dict[tuple[str, str, int], set[str]] = {}
    for item in raw_backfills:
        key = (
            str(item["source_agent"]),
            str(item["session_id"]),
            int(item["capture_event_id"]),
        )
        identity = {name: item[name] for name in item if name != "legacy_task_id"}
        previous = merged_backfills.get(key)
        if previous is not None and _sha256_json(previous) != _sha256_json(identity):
            blocked["capture_raw_backfill_manifest_conflict"] += 1
            source = str(item["source_agent"] or "unknown")
            blocked_by_source[f"{source}:capture_raw_backfill_manifest_conflict"] += 1
            continue
        merged_backfills[key] = identity
        backfill_tasks.setdefault(key, set()).add(str(item["legacy_task_id"]))
    normalized_backfills = []
    for key in sorted(merged_backfills):
        normalized_backfills.append(
            {
                **merged_backfills[key],
                "legacy_task_ids": sorted(backfill_tasks[key]),
            }
        )
    backfill_manifest = [
        {
            name: item[name]
            for name in (
                "schema_version",
                "capture_event_id",
                "handoff_receipt_id",
                "source_agent",
                "session_id",
                "turn_number",
                "content_hash",
                "capture_created_at",
                "capture_payload_hash",
                "legacy_task_ids",
            )
        }
        for item in normalized_backfills
    ]
    manifest = [
        {
            key: item[key]
            for key in (
                "legacy_task_id",
                "legacy_input_revision",
                "legacy_object_hash",
                "source_agent",
                "session_id",
                "canonical_task_id",
                "canonical_input_revision",
                "raw_preimage_hash",
                "match_method",
                "migration_id",
            )
        }
        for item in objects
    ]
    result["candidate_tasks"] = len(objects)
    result["capture_raw_backfill_events"] = len(normalized_backfills)
    result["capture_raw_backfill_tasks"] = len(
        {task_id for item in normalized_backfills for task_id in item["legacy_task_ids"]}
    )
    result["capture_raw_backfill_manifest_hash"] = _sha256_json(backfill_manifest)
    result["blocked_by_reason"] = dict(sorted(blocked.items()))
    result["blocked_by_source"] = dict(sorted(blocked_by_source.items()))
    result["blocked_objects"] = blocked_objects
    result["object_manifest_hash"] = _sha256_json(manifest)
    result["inventory_hash"] = _sha256_json(
        {
            "schema_version": SCHEMA_VERSION,
            "object_manifest_hash": result["object_manifest_hash"],
            "capture_raw_backfill_manifest_hash": result["capture_raw_backfill_manifest_hash"],
            "missing_span_tasks": result["missing_span_tasks"],
            "blocked_by_reason": result["blocked_by_reason"],
        }
    )
    result["raw_backfills"] = normalized_backfills
    result["objects"] = objects
    result["ok"] = not blocked
    return result
