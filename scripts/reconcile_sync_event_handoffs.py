#!/usr/bin/env python3
"""Rebuild only exact Canonical-Raw-backed sync-to-Amphora handoffs.

The reconciler is intentionally narrower than a source backfill.  It considers
only existing ``sync_engine`` cognitive events that have no task-generation
link, then requires an exact source/session/turn *and* content-hash match to a
current Canonical Raw revision.  ``--apply`` creates explicit replay-generation
CaptureQueue receipts and durable Amphora handoffs; it never invokes a model,
rewrites Canonical Raw, or writes Wiki pages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.migrations.model_call_ledger_reconcile.runtime import (  # noqa: E402
    runtime_writers_are_inactive as _shared_runtime_is_inactive,
)
from core.ops.durable_io import (  # noqa: E402
    DurableIOError,
    fsync_directory,
    fsync_regular_file,
    normalize_private_sqlite_copy,
    owned_sqlite_connection_pair,
    private_sqlite_sidecars,
    regular_file_sha256,
    validate_private_sqlite_copy,
)
from core.ops.readiness_query_budget import connect_readonly_sqlite  # noqa: E402
from core.sync_framework.raw_event_reader import (  # noqa: E402
    CanonicalRawReadError,
    require_admissible_raw_revision,
)

SCHEMA_VERSION = "mnemos.sync_event_handoff_reconciliation.v2"
RECONCILIATION_CONTRACT = "sync-event-raw-handoff-replay.v2"
_SYNC_URI_PREFIX = "sync://"
_MAX_LEGACY_TIMESTAMP_DISTANCE_SECONDS = 5 * 60
_MIN_TIMESTAMP_DISAMBIGUATION_MARGIN_SECONDS = 60 * 60


def _json_mapping(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _json_list(value: Any) -> list[Any]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return list(decoded) if isinstance(decoded, list) else []


def _connect_read_only(path: Path) -> sqlite3.Connection:
    return connect_readonly_sqlite(path, timeout_seconds=10)


def _connect_immutable(path: Path) -> sqlite3.Connection:
    return connect_readonly_sqlite(path, timeout_seconds=10, immutable=True)


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    return "sha256:" + regular_file_sha256(path)


def _parse_sync_uri(value: str) -> tuple[str, str, int] | None:
    if not value.startswith(_SYNC_URI_PREFIX):
        return None
    remainder = value[len(_SYNC_URI_PREFIX) :]
    source_agent, separator, session_and_turn = remainder.partition("/")
    if not separator or not source_agent:
        return None
    session_id, marker, turn_text = session_and_turn.rpartition("/turn/")
    if not marker or not session_id or not turn_text.isdigit():
        return None
    return source_agent, session_id, int(turn_text)


def _parse_event_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_historical_raw_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        local_timezone = datetime.now().astimezone().tzinfo or timezone.utc
        parsed = parsed.replace(tzinfo=local_timezone)
    return parsed.astimezone(timezone.utc)


def _task_linked_cognitive_event_ids(queue_path: Path) -> set[str]:
    if not queue_path.is_file():
        return set()
    linked: set[str] = set()
    with _connect_read_only(queue_path) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='distillation_tasks'"
        ).fetchone()
        if table is None:
            return set()
        for (raw_meta,) in conn.execute("SELECT meta FROM distillation_tasks"):
            meta = _json_mapping(raw_meta)
            values = meta.get("cognitive_sync_event_ids")
            if isinstance(values, list):
                linked.update(str(value) for value in values if str(value))
    return linked


def _unlinked_sync_events(
    ledger_path: Path,
    *,
    task_linked_ids: set[str],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    blocked: Counter[str] = Counter()
    events: list[dict[str, Any]] = []
    with _connect_read_only(ledger_path) as conn:
        rows = conn.execute(
            """
            SELECT event_id, source_uri, content_hash, intended_consumers, created_at
            FROM cognitive_data_events
            WHERE producer='sync_engine'
            """
        ).fetchall()
        consumed = {
            (str(event_id), str(consumer_id))
            for event_id, consumer_id in conn.execute(
                """
                SELECT event_id, consumer_id
                FROM cognitive_data_consumptions
                WHERE status='consumed'
                """
            )
        }
    for event_id, source_uri, content_hash, intended_json, created_at in rows:
        cognitive_event_id = str(event_id or "")
        if not cognitive_event_id or cognitive_event_id in task_linked_ids:
            continue
        intended_consumers = {str(value) for value in _json_list(intended_json)}
        missing_consumers = {
            consumer
            for consumer in ("amphora", "distill")
            if consumer in intended_consumers and (cognitive_event_id, consumer) not in consumed
        }
        if not missing_consumers:
            continue
        parsed = _parse_sync_uri(str(source_uri or ""))
        if parsed is None:
            blocked["sync_uri_unparseable"] += 1
            continue
        source_agent, session_id, turn_number = parsed
        if not str(content_hash or ""):
            blocked["cognitive_content_hash_missing"] += 1
            continue
        events.append(
            {
                "cognitive_event_id": cognitive_event_id,
                "source_agent": source_agent,
                "session_id": session_id,
                "turn_number": turn_number,
                "content_hash": str(content_hash),
                "created_at": str(created_at or ""),
                "missing_consumers": sorted(missing_consumers),
            }
        )
    return events, blocked


def _raw_matches(
    raw_path: Path,
    event: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    admissible_revision_ids: set[str] | None = None
    with _connect_read_only(raw_path) as conn:
        revision_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='raw_turn_revisions'"
        ).fetchone()
        if revision_table is not None:
            rows = conn.execute(
                """
                SELECT t.event_id, r.revision_id, t.source_agent, t.session_id,
                       t.turn_number, r.content_hash, r.full_content_hash,
                       r.created_at, r.snapshot_blob,
                       EXISTS (
                           SELECT 1 FROM raw_event_identity_aliases AS a
                           WHERE a.alias_event_id=t.event_id
                       ) AS is_alias,
                       COALESCE(m.retention_state, 'active') AS retention_state,
                       EXISTS (
                           SELECT 1 FROM raw_subject_deletion_receipts AS d
                           WHERE d.event_id=t.event_id AND d.status='applied'
                       ) AS is_deleted
                FROM raw_turns AS t
                JOIN raw_turn_revisions AS r ON r.logical_event_id=t.event_id
                LEFT JOIN raw_metrics AS m ON m.event_id=t.event_id
                WHERE t.source_agent=? AND t.session_id=? AND t.turn_number=?
                  AND (r.content_hash=? OR r.full_content_hash=?)
                ORDER BY r.created_at, r.revision_id
                """,
                (
                    str(event["source_agent"]),
                    str(event["session_id"]),
                    int(event["turn_number"]),
                    str(event["content_hash"]),
                    str(event["content_hash"]),
                ),
            ).fetchall()
            admissible_revision_ids = set()
            for row in rows:
                revision_id = str(row[1] or "")
                try:
                    require_admissible_raw_revision(
                        conn,
                        logical_event_id=str(row[0] or ""),
                        revision_id=revision_id,
                    )
                except CanonicalRawReadError:
                    continue
                admissible_revision_ids.add(revision_id)
        else:
            rows = conn.execute(
                """
                SELECT event_id, current_revision_id, source_agent, session_id,
                       turn_number, content_hash, full_content_hash,
                       '' AS created_at, NULL AS snapshot_blob,
                       0 AS is_alias, 'active' AS retention_state, 0 AS is_deleted
                FROM raw_turns
                WHERE source_agent=? AND session_id=? AND turn_number=?
                  AND (content_hash=? OR full_content_hash=?)
                """,
                (
                    str(event["source_agent"]),
                    str(event["session_id"]),
                    int(event["turn_number"]),
                    str(event["content_hash"]),
                    str(event["content_hash"]),
                ),
            ).fetchall()
    if not rows:
        return None, "raw_turn_missing"
    from core.sync_framework.raw_event_reader import decode_raw_revision_snapshot

    candidates: list[dict[str, Any]] = []
    invalid_snapshot = False
    native_contract_not_admissible = False
    for row in rows:
        if not str(row[1] or ""):
            continue
        if (
            admissible_revision_ids is not None
            and str(row[1] or "") not in admissible_revision_ids
        ):
            native_contract_not_admissible = True
            continue
        if bool(row[9]) or str(row[10]) == "eligible_delete" or bool(row[11]):
            continue
        snapshot = (
            decode_raw_revision_snapshot(row[8])
            if row[8] is not None
            else {
                "completeness_status": "complete",
                "content_hash": str(row[5] or ""),
            }
        )
        if row[8] is not None and (
            str(snapshot.get("event_id") or "") != str(row[0] or "")
            or str(snapshot.get("source_agent") or "") != str(row[2] or "")
            or str(snapshot.get("session_id") or "") != str(row[3] or "")
            or int(snapshot.get("turn_number") or 0) != int(row[4] or 0)
            or (
                not str(snapshot.get("user_content") or "")
                and not str(snapshot.get("assistant_content") or "")
            )
        ):
            invalid_snapshot = True
            continue
        if str(snapshot.get("content_hash") or "") not in {
            str(row[5] or ""),
            str(row[6] or ""),
        }:
            invalid_snapshot = True
            continue
        candidates.append(
            {
                "logical_event_id": str(row[0] or ""),
                "raw_revision_id": str(row[1] or ""),
                "source_agent": str(row[2] or ""),
                "session_id": str(row[3] or ""),
                "turn_number": int(row[4] or 0),
                "content_hash": str(row[5] or ""),
                "full_content_hash": str(row[6] or ""),
                "revision_created_at": str(row[7] or ""),
                "completeness_status": str(
                    snapshot.get("completeness_status") or ""
                ),
            }
        )
    if not candidates:
        return None, (
            "raw_native_contract_not_admissible"
            if native_contract_not_admissible
            else (
                "raw_revision_snapshot_invalid"
                if invalid_snapshot
                else "raw_content_hash_mismatch"
            )
        )
    by_revision = {candidate["raw_revision_id"]: candidate for candidate in candidates}
    ordered = list(by_revision.values())
    match_method = "exact_revision_hash"
    if len(ordered) > 1:
        event_time = _parse_event_timestamp(str(event.get("created_at") or ""))
        if event_time is None:
            return None, "raw_content_hash_ambiguous"
        ranked: list[tuple[float, dict[str, Any]]] = []
        for candidate in ordered:
            revision_time = _parse_historical_raw_timestamp(
                candidate["revision_created_at"]
            )
            if revision_time is None:
                return None, "raw_content_hash_ambiguous"
            ranked.append((abs((revision_time - event_time).total_seconds()), candidate))
        ranked.sort(key=lambda value: (value[0], value[1]["raw_revision_id"]))
        distance, candidate = ranked[0]
        margin = ranked[1][0] - distance
        if distance > _MAX_LEGACY_TIMESTAMP_DISTANCE_SECONDS:
            return None, "raw_revision_timestamp_too_distant"
        if margin < _MIN_TIMESTAMP_DISAMBIGUATION_MARGIN_SECONDS:
            return None, "raw_revision_timestamp_ambiguous"
        candidate["match_distance_seconds"] = round(distance, 6)
        candidate["match_margin_seconds"] = round(margin, 6)
        match_method = "exact_hash_nearest_legacy_timestamp"
    else:
        candidate = ordered[0]
        candidate["match_distance_seconds"] = 0.0
        candidate["match_margin_seconds"] = 0.0
    candidate["match_method"] = match_method
    if candidate["completeness_status"] not in {"complete", "partial"}:
        return None, "raw_completeness_not_replayable"
    return candidate, None


def build_sync_event_handoff_replay_plan(config: Any) -> dict[str, Any]:
    """Build an exact, metadata-only repair plan without touching any store."""
    database_dir = Path(config.database_dir)
    ledger_path = database_dir / "producer_consumer_ledger.db"
    queue_path = database_dir / "distill_queue.db"
    raw_path = database_dir / "raw_events.db"
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry_run",
        "ok": False,
        "eligible_sync_events": 0,
        "replayable_events": 0,
        "replayable_sessions": 0,
        "timestamp_disambiguated_events": 0,
        "blocked_by_reason": {},
        "groups": [],
    }
    if not ledger_path.is_file() or not queue_path.is_file() or not raw_path.is_file():
        result["error"] = "required_database_missing"
        return result

    task_linked_ids = _task_linked_cognitive_event_ids(queue_path)
    events, blocked = _unlinked_sync_events(ledger_path, task_linked_ids=task_linked_ids)
    grouped: dict[tuple[str, str], dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
    for event in events:
        raw, reason = _raw_matches(raw_path, event)
        if raw is None:
            blocked[str(reason or "raw_match_unproven")] += 1
            continue
        group_key = (str(raw["source_agent"]), str(raw["session_id"]))
        raw_key = (str(raw["logical_event_id"]), str(raw["raw_revision_id"]))
        item = grouped[group_key].setdefault(
            raw_key,
            {
                **raw,
                "cognitive_sync_event_ids": [],
            },
        )
        item["cognitive_sync_event_ids"].append(str(event["cognitive_event_id"]))
        if raw.get("match_method") == "exact_hash_nearest_legacy_timestamp":
            result["timestamp_disambiguated_events"] += 1

    groups: list[dict[str, Any]] = []
    for (source_agent, session_id), items_by_raw in sorted(grouped.items()):
        items = []
        for item in sorted(
            items_by_raw.values(),
            key=lambda value: (
                int(value["turn_number"]),
                str(value["logical_event_id"]),
            ),
        ):
            item["cognitive_sync_event_ids"] = sorted(
                set(str(value) for value in item["cognitive_sync_event_ids"] if str(value))
            )
            items.append(item)
        groups.append(
            {
                "source_agent": source_agent,
                "session_id": session_id,
                "items": items,
            }
        )
    result["eligible_sync_events"] = len(events)
    result["replayable_events"] = sum(
        len(item["cognitive_sync_event_ids"])
        for group in groups
        for item in group["items"]
    )
    result["replayable_sessions"] = len(groups)
    result["blocked_by_reason"] = dict(sorted(blocked.items()))
    result["groups"] = groups
    object_manifest = [
        {
            "source_agent": group["source_agent"],
            "session_id": group["session_id"],
            "items": [
                {
                    "logical_event_id": item["logical_event_id"],
                    "raw_revision_id": item["raw_revision_id"],
                    "turn_number": item["turn_number"],
                    "content_hash": item["content_hash"],
                    "full_content_hash": item["full_content_hash"],
                    "match_method": item.get("match_method", ""),
                    "match_distance_seconds": item.get(
                        "match_distance_seconds", 0.0
                    ),
                    "match_margin_seconds": item.get("match_margin_seconds", 0.0),
                    "cognitive_sync_event_ids": item["cognitive_sync_event_ids"],
                }
                for item in group["items"]
            ],
        }
        for group in groups
    ]
    result["object_manifest_hash"] = "sha256:" + _sha256(object_manifest)
    result["inventory_hash"] = "sha256:" + _sha256(
        {
            "schema_version": SCHEMA_VERSION,
            "contract": RECONCILIATION_CONTRACT,
            "object_manifest_hash": result["object_manifest_hash"],
            "blocked_by_reason": result["blocked_by_reason"],
        }
    )
    result["ok"] = not blocked
    return result


def _reconciliation_id(group: Mapping[str, Any]) -> str:
    return "sync-event-replay:" + _sha256(
        {
            "contract": RECONCILIATION_CONTRACT,
            "source_agent": group["source_agent"],
            "session_id": group["session_id"],
            "items": [
                {
                    "logical_event_id": item["logical_event_id"],
                    "raw_revision_id": item["raw_revision_id"],
                    "content_hash": item["content_hash"],
                    "match_method": item.get("match_method", ""),
                    "match_distance_seconds": item.get("match_distance_seconds", 0.0),
                    "match_margin_seconds": item.get("match_margin_seconds", 0.0),
                    "cognitive_sync_event_ids": item["cognitive_sync_event_ids"],
                }
                for item in group["items"]
            ],
        }
    )


def _input_revision(group: Mapping[str, Any], replay_generation: int) -> str:
    return "sync-event-replay-revision:" + _sha256(
        {
            "reconciliation_id": _reconciliation_id(group),
            "replay_generation": replay_generation,
        }
    )


def _capture_rows_for_raw_revision(
    capture_path: Path,
    *,
    source_agent: str,
    raw_revision_id: str,
) -> list[dict[str, Any]]:
    with _connect_read_only(capture_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, raw_revision_id, replay_generation, status, payload_json,
                   turn_number, content_hash
            FROM capture_events
            WHERE source_agent=? AND raw_revision_id=?
            ORDER BY id
            """,
            (source_agent, raw_revision_id),
        ).fetchall()
    return [dict(row) for row in rows]


def _replay_generation_for_group(capture_path: Path, group: Mapping[str, Any]) -> int:
    reconciliation_id = _reconciliation_id(group)
    matching: set[int] = set()
    highest_existing = 0
    for item in group["items"]:
        rows = _capture_rows_for_raw_revision(
            capture_path,
            source_agent=str(group["source_agent"]),
            raw_revision_id=str(item["raw_revision_id"]),
        )
        for row in rows:
            generation = int(row["replay_generation"] or 0)
            highest_existing = max(highest_existing, generation)
            metadata = _json_mapping(_json_mapping(row["payload_json"]).get("metadata"))
            if metadata.get("reconciliation_id") == reconciliation_id:
                matching.add(generation)
    if len(matching) > 1:
        raise ValueError("reconciliation_replay_generation_inconsistent")
    if matching:
        generation = next(iter(matching))
        if generation <= 0:
            raise ValueError("reconciliation_replay_generation_not_explicit")
        return generation
    return max(1, highest_existing + 1)


def _decompress(blob: Any, compression: str) -> str:
    if not blob:
        return ""
    if compression != "zlib":
        raise ValueError("raw_compression_unsupported")
    return zlib.decompress(blob).decode("utf-8")


def _raw_payload(
    raw_path: Path,
    *,
    item: Mapping[str, Any],
    reconciliation_id: str,
    replay_generation: int,
) -> tuple[dict[str, Any], str]:
    with _connect_read_only(raw_path) as conn:
        revision_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='raw_turn_revisions'"
        ).fetchone()
        if revision_table is not None:
            revision_row = conn.execute(
                """
                SELECT logical_event_id, content_hash, full_content_hash,
                       snapshot_blob
                FROM raw_turn_revisions WHERE revision_id=?
                """,
                (str(item["raw_revision_id"]),),
            ).fetchone()
            if revision_row is None:
                raise ValueError("raw_revision_changed_or_missing")
            if (
                str(revision_row[0] or "") != str(item["logical_event_id"])
                or str(revision_row[1] or "") != str(item["content_hash"])
                or str(revision_row[2] or "") != str(item["full_content_hash"])
            ):
                raise ValueError("raw_content_hash_changed")
            try:
                require_admissible_raw_revision(
                    conn,
                    logical_event_id=str(item["logical_event_id"]),
                    revision_id=str(item["raw_revision_id"]),
                )
            except CanonicalRawReadError:
                raise ValueError("raw_native_contract_not_admissible") from None
            from core.sync_framework.raw_event_reader import (
                decode_raw_revision_snapshot,
            )

            snapshot = decode_raw_revision_snapshot(revision_row[3])
            if (
                str(snapshot.get("event_id") or "") != str(item["logical_event_id"])
                or str(snapshot.get("source_agent") or "") != str(item["source_agent"])
                or str(snapshot.get("session_id") or "") != str(item["session_id"])
                or int(snapshot.get("turn_number") or 0) != int(item["turn_number"])
                or str(snapshot.get("content_hash") or "") != str(item["content_hash"])
            ):
                raise ValueError("raw_revision_snapshot_identity_changed")
            raw_metadata = snapshot.get("metadata")
            raw_metadata = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
            metadata = {
                **raw_metadata,
                "raw_event_id": str(item["raw_revision_id"]),
                "logical_event_id": str(item["logical_event_id"]),
                "raw_content_hash": str(item["content_hash"]),
                "cognitive_sync_event_ids": list(item["cognitive_sync_event_ids"]),
                "reconciliation_id": reconciliation_id,
                "reconciliation_contract": RECONCILIATION_CONTRACT,
                "replay_generation": replay_generation,
                "raw_revision_match_method": str(item.get("match_method") or ""),
            }
            payload = {
                "user_content": str(snapshot.get("user_content") or ""),
                "assistant_content": str(snapshot.get("assistant_content") or ""),
                "timestamp": str(snapshot.get("conversation_at") or ""),
                "model": str(snapshot.get("model_tag") or item["source_agent"]),
                "cwd": str(
                    raw_metadata.get("cwd") or snapshot.get("source_path") or "."
                ),
                "metadata": metadata,
                "tool_calls": list(snapshot.get("tool_calls") or ()),
                "tool_results": list(snapshot.get("tool_results") or ()),
                "reasoning": str(snapshot.get("reasoning") or ""),
                "attachments": list(snapshot.get("attachments") or ()),
                "raw_event_refs": list(snapshot.get("raw_event_refs") or ()),
                "source_files": [
                    str(value) for value in snapshot.get("source_files") or () if str(value)
                ],
                "completeness": dict(snapshot.get("completeness") or {}),
            }
            if not payload["user_content"] and not payload["assistant_content"]:
                raise ValueError("raw_turn_has_no_distillable_messages")
            return payload, str(item["content_hash"])
        row = conn.execute(
            """
            SELECT event_id, current_revision_id, source_agent, session_id,
                   turn_number, model_tag, conversation_at, source_path,
                   source_files_json, content_hash, completeness_json,
                   metadata_json, tool_calls_json, tool_results_json,
                   attachments_json, raw_event_refs_json, reasoning_blob,
                   user_content_blob, assistant_content_blob, compression
            FROM raw_turns
            WHERE event_id=? AND current_revision_id=?
            """,
            (str(item["logical_event_id"]), str(item["raw_revision_id"])),
        ).fetchone()
    if row is None:
        raise ValueError("raw_revision_changed_or_missing")
    if str(row[9] or "") not in {str(item["content_hash"]), str(item["full_content_hash"])}:
        raise ValueError("raw_content_hash_changed")
    compression = str(row[19] or "")
    source_files = _json_list(row[8])
    completeness = _json_mapping(row[10])
    raw_metadata = _json_mapping(row[11])
    raw_event_refs = _json_list(row[15])
    metadata = {
        **raw_metadata,
        "raw_event_id": str(item["raw_revision_id"]),
        "logical_event_id": str(item["logical_event_id"]),
        "raw_content_hash": str(row[9] or ""),
        "cognitive_sync_event_ids": list(item["cognitive_sync_event_ids"]),
        "reconciliation_id": reconciliation_id,
        "reconciliation_contract": RECONCILIATION_CONTRACT,
        "replay_generation": replay_generation,
    }
    payload = {
        "user_content": _decompress(row[17], compression),
        "assistant_content": _decompress(row[18], compression),
        "timestamp": str(row[6] or ""),
        "model": str(row[5] or item["source_agent"]),
        "cwd": str(raw_metadata.get("cwd") or row[7] or "."),
        "metadata": metadata,
        "tool_calls": _json_list(row[12]),
        "tool_results": _json_list(row[13]),
        "reasoning": _decompress(row[16], compression),
        "attachments": _json_list(row[14]),
        "raw_event_refs": raw_event_refs,
        "source_files": [str(value) for value in source_files if str(value)],
        "completeness": completeness,
    }
    if not payload["user_content"] and not payload["assistant_content"]:
        raise ValueError("raw_turn_has_no_distillable_messages")
    return payload, str(row[9] or "")


def _capture_event_for_generation(
    capture_path: Path,
    *,
    source_agent: str,
    raw_revision_id: str,
    replay_generation: int,
) -> dict[str, Any]:
    rows = [
        row
        for row in _capture_rows_for_raw_revision(
            capture_path,
            source_agent=source_agent,
            raw_revision_id=raw_revision_id,
        )
        if int(row["replay_generation"] or 0) == replay_generation
    ]
    if len(rows) != 1:
        raise ValueError("capture_replay_receipt_not_unique")
    row = rows[0]
    row["payload"] = _json_mapping(row["payload_json"])
    return row


def _backup_databases(paths: list[Path], backup_dir: Path) -> list[dict[str, str]]:
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    results: list[dict[str, str]] = []
    created: list[Path] = []
    error: BaseException
    try:
        for source in paths:
            target = backup_dir / f"{source.stem}-before-sync-event-replay-{stamp}.db"
            descriptor = os.open(
                target,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            created.append(target)
            os.close(descriptor)
            with owned_sqlite_connection_pair(
                lambda: _connect_read_only(source),
                lambda: sqlite3.connect(target),
            ) as (src, dst):
                src.backup(dst)
                integrity = str(
                    dst.execute("PRAGMA integrity_check").fetchone()[0]
                )
            if integrity != "ok":
                raise RuntimeError("backup_integrity_check_failed")
            normalize_private_sqlite_copy(target)
            target.chmod(0o600)
            fsync_regular_file(target)
            fsync_directory(target.parent)
            results.append(
                {
                    "source": str(source),
                    "path": str(target),
                    "integrity_check": integrity,
                    "sha256": _file_sha256(target),
                }
            )
        return results
    except DurableIOError:
        error = RuntimeError("backup_normalization_failed")
    except BaseException as exc:
        error = exc
    for target in created:
        for candidate in (*private_sqlite_sidecars(target), target):
            candidate.unlink(missing_ok=True)
    fsync_directory(backup_dir)
    raise error


def _restore_databases(backups: list[dict[str, str]]) -> None:
    for receipt in reversed(backups):
        source = Path(receipt["source"])
        backup = Path(receipt["path"])
        if _file_sha256(backup) != receipt["sha256"]:
            raise RuntimeError("backup_changed_before_rollback")
        try:
            validate_private_sqlite_copy(backup)
        except DurableIOError:
            raise RuntimeError("rollback_backup_not_standalone") from None
        stage = source.with_name(f".{source.name}.{uuid.uuid4().hex}.restore")
        stage_created = False
        try:
            descriptor = os.open(
                stage,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            stage_created = True
            os.close(descriptor)
            with owned_sqlite_connection_pair(
                lambda: _connect_immutable(backup),
                lambda: sqlite3.connect(stage),
            ) as (src, dst):
                src.backup(dst)
            normalize_private_sqlite_copy(stage)
            with _connect_immutable(stage) as restored:
                integrity = str(
                    restored.execute("PRAGMA integrity_check").fetchone()[0]
                )
            if integrity != "ok":
                raise RuntimeError("rollback_integrity_check_failed")
            os.replace(stage, source)
            for sidecar in private_sqlite_sidecars(source):
                sidecar.unlink(missing_ok=True)
            source.chmod(0o600)
            fsync_regular_file(source)
            fsync_directory(source.parent)
        except DurableIOError:
            raise RuntimeError("rollback_sqlite_copy_failed") from None
        finally:
            if stage_created:
                for candidate in (*private_sqlite_sidecars(stage), stage):
                    candidate.unlink(missing_ok=True)


def _runtime_writers_are_inactive(database_dir: Path) -> bool:
    return _shared_runtime_is_inactive(database_dir)


def _integrity(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def reconcile_sync_event_handoffs(
    config: Any,
    *,
    apply: bool,
    backup_dir: Path | None = None,
    expected_inventory_hash: str = "",
) -> dict[str, Any]:
    """Replay only exact Raw-backed task handoffs under an explicit generation."""
    reviewed_plan = build_sync_event_handoff_replay_plan(config)
    plan = {key: value for key, value in reviewed_plan.items() if key != "groups"}
    plan["mode"] = "apply" if apply else "dry_run"
    plan["backup"] = []
    plan["applied"] = {
        "queue_events_created": 0,
        "queue_events_reused": 0,
        "handoffs_committed": 0,
        "task_receipts_created": 0,
        "task_receipts_reused": 0,
        "apply_failures": {},
    }
    if plan.get("error"):
        return plan
    if not apply:
        return plan
    if backup_dir is None or not expected_inventory_hash:
        plan["error"] = "backup_directory_and_expected_inventory_hash_required"
        plan["ok"] = False
        return plan
    if expected_inventory_hash != str(plan.get("inventory_hash") or ""):
        plan["error"] = "inventory_hash_mismatch"
        plan["ok"] = False
        return plan
    if not plan.get("ok"):
        return plan
    if int(plan.get("replayable_events", 0)) == 0:
        plan["status"] = "noop"
        plan["applied"] = False
        return plan

    database_dir = Path(config.database_dir)
    if not _runtime_writers_are_inactive(database_dir):
        plan["error"] = "daemon_not_inactive"
        plan["ok"] = False
        return plan
    capture_path = database_dir / "capture_queue.db"
    distill_path = database_dir / "distill_queue.db"
    ledger_path = database_dir / "producer_consumer_ledger.db"
    raw_path = database_dir / "raw_events.db"
    if not all(path.is_file() for path in (capture_path, distill_path, ledger_path, raw_path)):
        plan["error"] = "required_database_missing"
        plan["ok"] = False
        return plan

    try:
        plan["backup"] = _backup_databases(
            [capture_path, distill_path, ledger_path],
            Path(backup_dir),
        )
        full_plan = build_sync_event_handoff_replay_plan(config)
        if (
            full_plan.get("error")
            or not full_plan.get("ok")
            or full_plan.get("inventory_hash") != expected_inventory_hash
        ):
            raise RuntimeError("reviewed_sync_event_inventory_drifted")
        from core.kia.amphora import enqueue_with_receipt
        from core.ops.cognitive_pipeline_receipts import record_sync_handoff
        from core.sync_framework.capture_queue import CaptureQueue

        queue = CaptureQueue(db_path=str(capture_path))
        try:
            failures: Counter[str] = Counter()
            for group in full_plan["groups"]:
                handoff: dict[str, Any] | None = None
                try:
                    reconciliation_id = _reconciliation_id(group)
                    replay_generation = _replay_generation_for_group(capture_path, group)
                    input_revision = _input_revision(group, replay_generation)
                    prepared = [
                        (
                            item,
                            *_raw_payload(
                                raw_path,
                                item=item,
                                reconciliation_id=reconciliation_id,
                                replay_generation=replay_generation,
                            ),
                        )
                        for item in group["items"]
                    ]
                    queue_events: list[dict[str, Any]] = []
                    for item, payload, content_hash in prepared:
                        existing = None
                        try:
                            existing = _capture_event_for_generation(
                                capture_path,
                                source_agent=str(group["source_agent"]),
                                raw_revision_id=str(item["raw_revision_id"]),
                                replay_generation=replay_generation,
                            )
                        except ValueError:
                            pass
                        if existing is None:
                            status = queue.enqueue(
                                source_agent=str(group["source_agent"]),
                                session_id=str(group["session_id"]),
                                turn_id=str(item["logical_event_id"]),
                                turn_number=int(item["turn_number"]),
                                payload=payload,
                                content_hash=content_hash,
                                raw_revision_id=str(item["raw_revision_id"]),
                                replay_generation=replay_generation,
                            )
                            if status not in {"queued", "duplicate"}:
                                raise RuntimeError("capture_queue_replay_not_accepted")
                            if status == "queued":
                                plan["applied"]["queue_events_created"] += 1
                            else:
                                plan["applied"]["queue_events_reused"] += 1
                            existing = _capture_event_for_generation(
                                capture_path,
                                source_agent=str(group["source_agent"]),
                                raw_revision_id=str(item["raw_revision_id"]),
                                replay_generation=replay_generation,
                            )
                        else:
                            plan["applied"]["queue_events_reused"] += 1
                        queue_events.append(existing)
                    handoff = queue.create_distillation_handoff(
                        str(group["source_agent"]),
                        str(group["session_id"]),
                        queue_events,
                        enabled=True,
                        input_revision=input_revision,
                    )
                    if str(handoff.get("status") or "") == "intentional_skip":
                        raise RuntimeError("replay_handoff_has_no_distillable_messages")
                    receipt = enqueue_with_receipt(
                        session_id=str(group["session_id"]),
                        messages=list(handoff["messages"]),
                        meta=dict(handoff["meta"]),
                    )
                    if str(receipt.input_revision) != input_revision:
                        raise RuntimeError("amphora_input_revision_mismatch")
                    queue.commit_distillation_handoff(
                        str(handoff["receipt_id"]),
                        downstream_receipt_id=str(receipt.receipt_id),
                        downstream_task_id=str(receipt.task_id),
                    )
                    record_sync_handoff(
                        config,
                        str(group["session_id"]),
                        dict(handoff["meta"]),
                        receipt,
                    )
                    plan["applied"]["handoffs_committed"] += 1
                    if receipt.created:
                        plan["applied"]["task_receipts_created"] += 1
                    else:
                        plan["applied"]["task_receipts_reused"] += 1
                except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error) as exc:
                    failures[type(exc).__name__] += 1
                    if handoff is not None and handoff.get("receipt_id"):
                        try:
                            queue.fail_distillation_handoff(
                                str(handoff["receipt_id"]),
                                f"sync-event handoff reconciliation failed: {type(exc).__name__}",
                            )
                        except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error):
                            failures["handoff_failure_recording_failed"] += 1
            plan["applied"]["apply_failures"] = dict(sorted(failures.items()))
        finally:
            queue.close()
        if plan["applied"]["apply_failures"]:
            raise RuntimeError("sync_event_handoff_apply_failed")
        post_plan = build_sync_event_handoff_replay_plan(config)
        if (
            not post_plan.get("ok")
            or int(post_plan.get("replayable_events", 0)) != 0
            or post_plan.get("blocked_by_reason")
        ):
            raise RuntimeError("post_apply_sync_event_inventory_not_clean")
        plan["integrity_check"] = {
            "capture_queue": _integrity(capture_path),
            "distill_queue": _integrity(distill_path),
            "producer_consumer_ledger": _integrity(ledger_path),
        }
        plan["ok"] = bool(
            not plan["blocked_by_reason"]
            and not plan["applied"]["apply_failures"]
            and all(value == "ok" for value in plan["integrity_check"].values())
        )
        plan["status"] = "verified" if plan["ok"] else "failed"
        plan["post_inventory_hash"] = post_plan["inventory_hash"]
        plan["reviewed_inventory_hash"] = expected_inventory_hash
        return plan
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error) as exc:
        backups = list(plan.get("backup") or [])
        if backups:
            _restore_databases(backups)
        rolled_back = build_sync_event_handoff_replay_plan(config)
        rollback_verified = (
            rolled_back.get("inventory_hash") == expected_inventory_hash
        )
        plan["error"] = type(exc).__name__
        plan["ok"] = False
        plan["status"] = "rolled_back" if rollback_verified else "failed"
        plan["rollback_verified"] = rollback_verified
        return plan


def _render(result: Mapping[str, Any]) -> dict[str, Any]:
    """Keep machine output free of task ids, paths, and raw content."""
    return {
        key: value
        for key, value in result.items()
        if key not in {"groups"}
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--expected-inventory-hash", default="")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from core.config import get_config

    result = reconcile_sync_event_handoffs(
        get_config(),
        apply=bool(args.apply),
        backup_dir=args.backup_dir,
        expected_inventory_hash=str(args.expected_inventory_hash),
    )
    rendered = json.dumps(_render(result), ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
