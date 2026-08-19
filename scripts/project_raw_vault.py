#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rebuild the Obsidian raw vault as a retention-driven projection.

Canonical raw data lives in ``raw_events.db``.  This script mirrors all raw
turns that are still retained by lifecycle metrics.  Turns marked
``eligible_delete`` are omitted by default.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os  # noqa: F401 - compatibility seam for crash-injection tests/operators
import re
import shutil  # noqa: F401 - compatibility seam for crash-injection tests/operators
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if __name__ == "__main__":
    # Split runtime modules import the public compatibility surface by its
    # package name. Direct script execution must expose this already-running
    # module under that same identity instead of loading a second copy.
    sys.modules.setdefault("scripts.project_raw_vault", sys.modules[__name__])

from core.app.raw_search import RawIndex  # noqa: F401 - public compatibility seam
from core.config import get_config
from core.ops.durable_io import DurableIOError, physical_scope_signature
from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.sync_framework.native_raw_contract_ledger import NativeRawContractLedger
from core.ops.durable_io import read_native_bytes
from core.sync_framework.raw_event_reader import decode_raw_revision_snapshot
from core.sync_framework.raw_subject_deletion import subject_deletion_visibility_predicate
from scripts.raw_projection_contract import (  # noqa: F401
    PROJECTION_CONTRACT,
    PROJECTION_JOURNAL_NAME,
    PROJECTION_TRANSACTION_DIR,
    PROJECTION_TRANSACTION_SCHEMA,
    PROJECTION_TRANSACTION_STATE_SCHEMA,
    PROJECTION_TRANSACTION_TOMBSTONE_PREFIX,
    _projection_path_kind,
    projection_timestamp_path_segment,
    render_projection_event_header,
    safe_projection_target as _safe_projection_target,
    validate_projection_metadata,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_FILES = 0
DEFAULT_CHUNK_TURNS = 5
# Canonical Raw is never a preview. A compact view must be implemented as a
# separately named projection rather than silently truncating this one.
DEFAULT_MAX_TURN_CHARS = 0
# Per-file byte cap for the rendered Markdown projection.  Oversized chunks are
# published as an index page plus ordered ``<name>.part-NNN.md`` parts; 0 keeps
# the legacy single-file behavior.  Small chunks must render byte-identical
# output either way so unchanged vault files never churn.
DEFAULT_MAX_FILE_BYTES = 2097152
PROJECTION_VERSION = 2
EVENT_MARKER_PREFIX = "<!-- mnemos-raw-event-v2 "
FIELD_MARKER_PREFIX = "<!-- mnemos-raw-field-v2 "
FIELD_MARKER_END = "<!-- /mnemos-raw-field-v2 -->"
FIELD_CONT_MARKER_PREFIX = "<!-- mnemos-raw-field-cont-v2 "
PROJECTION_INDEX_MNEMOS_TYPE = "raw_retention_projection_index"
VISIBLE_FIELDS = ("user_content", "assistant_content", "reasoning", "structured")
_PROJECTION_PREAMBLE_NOTICE = (
    "> Lossless Raw projection. Canonical raw content is stored in "
    "`raw_events.db`; every visible field below is byte-hashed.\n\n"
)
_PART_SUFFIX_PATTERN = re.compile(r"\.part-[0-9]{3,}\Z")
PROJECTION_PART_PATH_PATTERN = re.compile(r"\.part-[0-9]{3,}\.md\Z")


def _sqlite_epoch_signature(db_path: Path) -> Dict[str, Any]:
    paths = {
        suffix: Path(f"{db_path}{suffix}").absolute()
        for suffix in ("", "-wal", "-shm")
    }
    try:
        scope = physical_scope_signature(paths.values())
        raw_entries = scope.get("entries")
        if not isinstance(raw_entries, list):
            raise DurableIOError(
                "canonical_raw_epoch_inspection_failed"
            )
        entries = {
            str(entry.get("path")): entry
            for entry in raw_entries
            if isinstance(entry, dict)
        }
    except (DurableIOError, OSError, TypeError):
        raise DurableIOError(
            "canonical_raw_epoch_inspection_failed"
        ) from None
    signature: Dict[str, Any] = {}
    for suffix, path in paths.items():
        entry = entries.get(str(path))
        if not isinstance(entry, dict):
            raise DurableIOError(
                "canonical_raw_epoch_inspection_failed"
            )
        if entry.get("present") is False:
            signature[suffix] = None
            continue
        if entry.get("kind") != "file":
            raise DurableIOError(
                "canonical_raw_epoch_path_not_regular"
            )
        signature[suffix] = {
            key: value
            for key, value in entry.items()
            if key not in {"path", "present", "kind"}
        }
    return signature


@dataclass(frozen=True)
class TurnRef:
    event_id: str
    source_agent: str
    session_id: str
    turn_number: int
    conversation_at: str
    captured_at: str
    completeness_status: str
    search_count: int
    result_count: int
    hit_count: int
    view_count: int
    reference_count: int
    freshness_score: float
    confidence: float
    survival_score: float
    pinned: int
    retention_state: str
    # ``event_id`` is the immutable current revision identifier.  Keep the
    # logical id separately so a revision update replaces its existing chunk
    # path instead of creating a new file and deleting the old one.
    logical_event_id: str = ""

    @property
    def timestamp(self) -> str:
        return self.conversation_at or self.captured_at or ""


@dataclass
class ProjectionChunk:
    source_agent: str
    session_id: str
    chunk_index: int
    refs: List[TurnRef]

    @property
    def start_turn(self) -> int:
        return min(ref.turn_number for ref in self.refs)

    @property
    def end_turn(self) -> int:
        return max(ref.turn_number for ref in self.refs)

    @property
    def event_ids(self) -> List[str]:
        return [
            ref.event_id
            for ref in sorted(self.refs, key=lambda item: (item.turn_number, item.event_id))
        ]

    @property
    def logical_event_ids(self) -> List[str]:
        """Stable identities for paths and journal reconciliation."""
        return [
            ref.logical_event_id or ref.event_id
            for ref in sorted(self.refs, key=lambda item: (item.turn_number, item.event_id))
        ]

    @property
    def latest_timestamp(self) -> str:
        return max((ref.timestamp for ref in self.refs if ref.timestamp), default="")

    @property
    def score_tuple(self) -> Tuple[float, ...]:
        pinned = max(ref.pinned for ref in self.refs)
        reference_count = sum(ref.reference_count for ref in self.refs)
        hit_count = sum(ref.hit_count for ref in self.refs)
        result_count = sum(ref.result_count for ref in self.refs)
        survival = max(ref.survival_score for ref in self.refs)
        freshness = max(ref.freshness_score for ref in self.refs)
        timestamp_score = _timestamp_sort_value(self.latest_timestamp)
        return (
            float(pinned),
            float(reference_count),
            float(hit_count),
            float(result_count),
            float(survival),
            float(freshness),
            float(timestamp_score),
        )


class ReadOnlyProjectionSource:
    """Freeze the exact current-Raw inputs without provisioning live state."""

    def __init__(self, db_path: Path, *, include_eligible_delete: bool = False) -> None:
        self.db_path = Path(db_path).expanduser()
        epoch_before = _sqlite_epoch_signature(self.db_path)
        wal_state = epoch_before.get("-wal")
        if wal_state is not None and int(wal_state["size"]):
            raise ValueError(
                "canonical raw database has a non-empty WAL; checkpoint before "
                "read-only projection planning"
            )
        try:
            connection = connect_readonly_sqlite(
                self.db_path,
                immutable=True,
            )
        except (OSError, sqlite3.Error) as exc:
            raise ValueError(f"canonical raw database is unreadable: {exc}") from exc
        try:
            query = """
                SELECT
                    t.current_revision_id, t.event_id,
                    t.source_agent, t.session_id, t.turn_number,
                    t.conversation_at, t.captured_at, t.completeness_status,
                    m.search_count, m.result_count, m.hit_count, m.view_count,
                    m.reference_count, m.freshness_score, m.confidence,
                    m.survival_score, m.pinned, m.retention_state,
                    r.logical_event_id, r.content_hash
                FROM raw_turns AS t
                LEFT JOIN raw_turn_revisions AS r
                  ON r.revision_id=t.current_revision_id
                LEFT JOIN raw_metrics AS m ON m.event_id=t.event_id
                WHERE
                    NOT EXISTS (
                        SELECT 1
                        FROM raw_event_identity_aliases AS alias
                        WHERE alias.alias_event_id=t.event_id
                    )
            """
            query += NativeRawContractLedger.current_event_visibility_predicate("t.event_id")
            query += subject_deletion_visibility_predicate("t.event_id")
            if not include_eligible_delete:
                query += " AND COALESCE(m.retention_state, 'active') != 'eligible_delete'"
            query += " ORDER BY t.event_id, t.current_revision_id"
            rows = connection.execute(query).fetchall()
        except sqlite3.Error as exc:
            connection.close()
            raise ValueError(f"canonical raw read contract is invalid: {exc}") from exc

        refs: List[TurnRef] = []
        headers: Dict[str, Tuple[str, str, str, int, str, str, str, str]] = {}
        logical_ids: set[str] = set()
        for row in rows:
            revision_id = str(row[0] or "")
            logical_event_id = str(row[1] or "")
            if (
                not revision_id
                or not logical_event_id
                or str(row[18] or "") != logical_event_id
                or revision_id in headers
                or logical_event_id in logical_ids
                or row[17] is None
                or not str(row[19] or "")
            ):
                connection.close()
                raise ValueError("canonical raw current revision identity is invalid")
            headers[revision_id] = (
                logical_event_id,
                str(row[2] or ""),
                str(row[3] or ""),
                int(row[4]),
                str(row[5] or ""),
                str(row[6] or ""),
                str(row[7] or ""),
                str(row[19] or ""),
            )
            logical_ids.add(logical_event_id)
            refs.append(
                TurnRef(
                    event_id=revision_id,
                    source_agent=str(row[2] or ""),
                    session_id=str(row[3] or ""),
                    turn_number=int(row[4]),
                    conversation_at=str(row[5] or ""),
                    captured_at=str(row[6] or ""),
                    completeness_status=str(row[7] or ""),
                    search_count=int(row[8] or 0),
                    result_count=int(row[9] or 0),
                    hit_count=int(row[10] or 0),
                    view_count=int(row[11] or 0),
                    reference_count=int(row[12] or 0),
                    freshness_score=float(row[13] or 0.0),
                    confidence=float(row[14] or 0.0),
                    survival_score=float(row[15] or 0.0),
                    pinned=int(row[16] or 0),
                    retention_state=str(row[17] or "active"),
                    logical_event_id=logical_event_id,
                )
            )
        self._connection = connection
        self._refs = tuple(refs)
        self._headers = headers
        epoch_after = _sqlite_epoch_signature(self.db_path)
        if epoch_after != epoch_before:
            connection.close()
            raise ValueError("canonical raw evidence epoch changed during projection planning")
        self._epoch_signature = epoch_after
        self.epoch_hash = _sha256_text(
            json.dumps(
                epoch_after,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def projection_refs(self) -> List[TurnRef]:
        return list(self._refs)

    def get_turn(self, revision_id: str) -> Optional[Dict[str, Any]]:
        normalized_revision_id = str(revision_id)
        header = self._headers.get(normalized_revision_id)
        if header is None:
            return None
        row = self._connection.execute(
            "SELECT snapshot_blob FROM raw_turn_revisions WHERE revision_id=?",
            (normalized_revision_id,),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"canonical raw revision {normalized_revision_id} disappeared during plan"
            )
        try:
            payload = decode_raw_revision_snapshot(row[0])
        except ValueError as exc:
            raise ValueError(
                f"canonical raw revision {normalized_revision_id} snapshot is invalid"
            ) from exc
        (
            logical_event_id,
            source_agent,
            session_id,
            turn_number,
            conversation_at,
            captured_at,
            completeness_status,
            content_hash,
        ) = header
        if (
            str(payload.get("event_id") or "") != logical_event_id
            or str(payload.get("source_agent") or "") != source_agent
            or str(payload.get("session_id") or "") != session_id
            or str(payload.get("content_hash") or "") != content_hash
        ):
            raise ValueError(
                f"canonical raw revision {normalized_revision_id} snapshot identity is invalid"
            )
        turn = dict(payload)
        turn["event_id"] = normalized_revision_id
        turn["logical_event_id"] = logical_event_id
        turn["source_agent"] = source_agent
        turn["session_id"] = session_id
        turn["turn_number"] = turn_number
        turn["conversation_at"] = str(payload.get("conversation_at") or conversation_at)
        turn["captured_at"] = str(payload.get("captured_at") or captured_at)
        turn["completeness_status"] = completeness_status
        return turn

    def close(self) -> None:
        self._connection.close()

    def assert_epoch_current(self) -> None:
        if _sqlite_epoch_signature(self.db_path) != self._epoch_signature:
            raise RuntimeError("canonical raw evidence epoch changed after projection planning")


def _timestamp_sort_value(value: str) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _safe_slug(value: str, max_len: int = 64) -> str:
    allowed = []
    for char in value:
        if char.isalnum() or char in ("-", "_"):
            allowed.append(char)
        else:
            allowed.append("-")
    slug = "".join(allowed).strip("-") or "unknown"
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:max_len]


def _frontmatter_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(
        str(value),
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _frontmatter_line(key: str, value: Any) -> List[str]:
    if isinstance(value, list):
        if not value:
            return [f"{key}: []"]
        lines = [f"{key}:"]
        for item in value:
            if isinstance(item, dict):
                if not item:
                    raise ValueError("frontmatter list mapping entries must be non-empty")
                first = True
                for sub_key, sub_value in item.items():
                    prefix = "  - " if first else "    "
                    lines.append(f"{prefix}{sub_key}: {_frontmatter_scalar(sub_value)}")
                    first = False
            else:
                lines.append(f"  - {item}")
        return lines
    return [f"{key}: {_frontmatter_scalar(value)}"]


def _frontmatter(fields: Dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        lines.extend(_frontmatter_line(key, value))
    lines.extend(["---", ""])
    return "\n".join(lines)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def structured_field_text(turn: Dict[str, Any]) -> str:
    """Return the deterministic visible encoding for non-text Raw fields."""
    structured = {
        "tool_calls": turn.get("tool_calls") or [],
        "tool_results": turn.get("tool_results") or [],
        "attachments": turn.get("attachments") or [],
        "raw_event_refs": turn.get("raw_event_refs") or [],
        "source_files": turn.get("source_files") or [],
    }
    return json.dumps(structured, ensure_ascii=False, indent=2, sort_keys=True)


def _field_marker(event_id: str, field: str, value: str) -> str:
    return (
        FIELD_MARKER_PREFIX
        + json.dumps(
            {
                "event_id": event_id,
                "field": field,
                "sha256": _sha256_text(value),
                "bytes": len(value.encode("utf-8")),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + " -->"
    )


def _field_cont_marker(event_id: str, field: str) -> str:
    """Continuation marker opening a part that resumes a split field slice."""
    return (
        FIELD_CONT_MARKER_PREFIX
        + json.dumps(
            {"event_id": event_id, "field": field},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + " -->"
    )


def _render_text_field(event_id: str, field: str, heading: str, value: str) -> str:
    marker = _field_marker(event_id, field, value)
    return f"### {heading}\n\n{marker}\n{value}\n{FIELD_MARKER_END}\n\n"


def _render_structured_field(event_id: str, value: str) -> str:
    marker = _field_marker(event_id, "structured", value)
    return f"### Structured\n\n{marker}\n```json\n{value}\n```\n" f"{FIELD_MARKER_END}\n\n"


def _part_relative_path(base_relative_path: str, part_suffix: str) -> str:
    """Map a chunk's base path plus one render suffix to its artifact path."""
    if not part_suffix:
        return base_relative_path
    if _PART_SUFFIX_PATTERN.fullmatch(part_suffix) is None or not base_relative_path.endswith(
        ".md"
    ):
        raise ValueError("Raw projection part path suffix is invalid")
    return base_relative_path[: -len(".md")] + part_suffix + ".md"


def _fetch_refs(store: Any, include_eligible_delete: bool = False) -> List[TurnRef]:
    if isinstance(store, ReadOnlyProjectionSource):
        return store.projection_refs()
    conn = store._pool.get_conn()  # noqa: SLF001
    query = """
        SELECT
            COALESCE(t.current_revision_id, t.event_id), t.event_id,
            t.source_agent, t.session_id, t.turn_number,
            t.conversation_at, t.captured_at, t.completeness_status,
            m.search_count, m.result_count, m.hit_count, m.view_count,
            m.reference_count, m.freshness_score, m.confidence,
            m.survival_score, m.pinned, m.retention_state
        FROM raw_turns t
        JOIN raw_metrics m ON m.event_id = t.event_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM raw_event_identity_aliases a
            WHERE a.alias_event_id=t.event_id
        )
    """
    query += NativeRawContractLedger.current_event_visibility_predicate("t.event_id")
    query += subject_deletion_visibility_predicate("t.event_id")
    if not include_eligible_delete:
        query += " AND m.retention_state != 'eligible_delete'"
    rows = conn.execute(query).fetchall()
    return [
        TurnRef(
            event_id=row[0],
            source_agent=row[2],
            session_id=row[3],
            turn_number=int(row[4]),
            conversation_at=row[5] or "",
            captured_at=row[6] or "",
            completeness_status=row[7] or "",
            search_count=int(row[8] or 0),
            result_count=int(row[9] or 0),
            hit_count=int(row[10] or 0),
            view_count=int(row[11] or 0),
            reference_count=int(row[12] or 0),
            freshness_score=float(row[13] or 0.0),
            confidence=float(row[14] or 0.0),
            survival_score=float(row[15] or 0.0),
            pinned=int(row[16] or 0),
            retention_state=row[17] or "active",
            logical_event_id=str(row[1] or row[0]),
        )
        for row in rows
    ]


def build_projection_chunks(
    refs: Iterable[TurnRef],
    *,
    chunk_turns: int,
    max_chunks: Optional[int],
) -> List[ProjectionChunk]:
    grouped: Dict[Tuple[str, str, int], List[TurnRef]] = {}
    for ref in refs:
        chunk_index = ref.turn_number // max(1, chunk_turns)
        key = (ref.source_agent, ref.session_id, chunk_index)
        grouped.setdefault(key, []).append(ref)

    chunks = [
        ProjectionChunk(
            source,
            session,
            chunk_index,
            sorted(items, key=lambda ref: (ref.turn_number, ref.event_id)),
        )
        for (source, session, chunk_index), items in grouped.items()
    ]
    return _select_source_balanced_chunks(chunks, max_chunks=max_chunks)


def _select_source_balanced_chunks(
    chunks: List[ProjectionChunk],
    *,
    max_chunks: Optional[int],
) -> List[ProjectionChunk]:
    if max_chunks is None or max_chunks >= len(chunks):
        return sorted(chunks, key=lambda chunk: chunk.score_tuple, reverse=True)
    if max_chunks <= 0:
        return []

    by_source: Dict[str, List[ProjectionChunk]] = {}
    for chunk in chunks:
        by_source.setdefault(chunk.source_agent, []).append(chunk)
    for source_chunks in by_source.values():
        source_chunks.sort(key=lambda chunk: chunk.score_tuple, reverse=True)

    sources = sorted(by_source)
    if len(sources) >= max_chunks:
        top_chunks = [by_source[source][0] for source in sources]
        return sorted(top_chunks, key=lambda chunk: chunk.score_tuple, reverse=True)[:max_chunks]

    total_source_chunks = sum(len(source_chunks) for source_chunks in by_source.values())
    if total_source_chunks == 0:
        return []

    raw_quotas = {
        source: max_chunks * len(source_chunks) / total_source_chunks
        for source, source_chunks in by_source.items()
    }
    allocations = {
        source: min(len(by_source[source]), max(1, int(raw_quotas[source]))) for source in sources
    }

    while sum(allocations.values()) > max_chunks:
        removable = [source for source in sources if allocations[source] > 1]
        if not removable:
            break
        source = min(
            removable,
            key=lambda item: (
                raw_quotas[item] - int(raw_quotas[item]),
                len(by_source[item]),
                by_source[item][allocations[item] - 1].score_tuple,
            ),
        )
        allocations[source] -= 1

    while sum(allocations.values()) < max_chunks:
        expandable = [source for source in sources if allocations[source] < len(by_source[source])]
        if not expandable:
            break
        source = max(
            expandable,
            key=lambda item: (
                raw_quotas[item] - allocations[item],
                by_source[item][allocations[item]].score_tuple,
                len(by_source[item]),
            ),
        )
        allocations[source] += 1

    selected = [chunk for source in sources for chunk in by_source[source][: allocations[source]]]
    return sorted(selected, key=lambda chunk: chunk.score_tuple, reverse=True)[:max_chunks]


def _chunk_relative_path(chunk: ProjectionChunk) -> str:
    """Return the chunk's vault-relative base path without touching ``raw_dir``."""
    date = projection_timestamp_path_segment(chunk.latest_timestamp)
    source = _safe_slug(chunk.source_agent, 24)
    session = _safe_slug(chunk.session_id, 36)
    chunk_id = _safe_slug(
        chunk.logical_event_ids[0][:10] if chunk.logical_event_ids else "unknown", 12
    )
    name = (
        f"{source}_{session}_{chunk_id}" f"_t{chunk.start_turn + 1:04d}-{chunk.end_turn + 1:04d}.md"
    )
    return (Path(source) / date / name).as_posix()


def _chunk_path(raw_dir: Path, chunk: ProjectionChunk) -> Path:
    return raw_dir / _chunk_relative_path(chunk)


def _load_turns(store: Any, chunk: ProjectionChunk) -> List[Dict[str, Any]]:
    # ``chunk.event_ids`` is the canonical publisher sequence derived from
    # current Raw refs. Load in that exact order: historical snapshot metadata
    # may retain an older turn number, so re-sorting on snapshot bytes would
    # make frontmatter, journal and body disagree about one generation.
    turns = []
    for event_id in chunk.event_ids:
        turn = store.get_turn(event_id)
        if turn is None or str(turn.get("event_id") or "") != event_id:
            raise RuntimeError(
                "Raw projection chunk lacks its complete canonical event sequence"
            )
        turns.append(turn)
    return turns


def _chunk_frontmatter_fields(chunk: ProjectionChunk, db_path: Path) -> Dict[str, Any]:
    return {
        "mnemos_type": "raw_retention_projection",
        "projection_version": PROJECTION_VERSION,
        "projection_contract": PROJECTION_CONTRACT,
        "canonical_db": str(db_path),
        "source": chunk.source_agent,
        "session_id": chunk.session_id,
        "turn_start": chunk.start_turn + 1,
        "turn_end": chunk.end_turn + 1,
        "event_ids": chunk.event_ids,
        "logical_event_ids": chunk.logical_event_ids,
        "conversation_start_at": min(
            (ref.timestamp for ref in chunk.refs if ref.timestamp),
            default="",
        ),
        "conversation_end_at": chunk.latest_timestamp,
        "completeness_statuses": sorted({ref.completeness_status for ref in chunk.refs}),
        "search_count": sum(ref.search_count for ref in chunk.refs),
        "result_count": sum(ref.result_count for ref in chunk.refs),
        "hit_count": sum(ref.hit_count for ref in chunk.refs),
        "view_count": sum(ref.view_count for ref in chunk.refs),
        "reference_count": sum(ref.reference_count for ref in chunk.refs),
        "freshness_score": round(max(ref.freshness_score for ref in chunk.refs), 4),
        "confidence": round(max(ref.confidence for ref in chunk.refs), 4),
        "survival_score": round(max(ref.survival_score for ref in chunk.refs), 2),
        "retention_state": "active",
        "tags": [
            "raw-retention-projection",
            f"source={chunk.source_agent}",
            "canonical=raw_events",
        ],
    }


@dataclass(frozen=True)
class _ChunkFieldSegment:
    """One visible field with its structural wrapper, splittable at line bounds."""

    event_id: str
    field: str
    prefix: str
    value: str
    suffix: str
    byte_length: int

    @property
    def text(self) -> str:
        return self.prefix + self.value + self.suffix


@dataclass(frozen=True)
class _ChunkTurnBlock:
    """One rendered turn: the event header plus its four visible field segments."""

    header: str
    fields: Tuple[_ChunkFieldSegment, ...]
    byte_length: int

    @property
    def text(self) -> str:
        return self.header + "".join(segment.text for segment in self.fields)


def _field_segment(event_id: str, field: str, heading: str, value: str) -> _ChunkFieldSegment:
    marker = _field_marker(event_id, field, value)
    prefix = f"### {heading}\n\n{marker}\n"
    suffix = f"\n{FIELD_MARKER_END}\n\n"
    byte_length = (
        len(prefix.encode("utf-8")) + len(value.encode("utf-8")) + len(suffix.encode("utf-8"))
    )
    return _ChunkFieldSegment(
        event_id=event_id,
        field=field,
        prefix=prefix,
        value=value,
        suffix=suffix,
        byte_length=byte_length,
    )


def _structured_segment(event_id: str, value: str) -> _ChunkFieldSegment:
    marker = _field_marker(event_id, "structured", value)
    prefix = f"### Structured\n\n{marker}\n```json\n"
    suffix = f"\n```\n{FIELD_MARKER_END}\n\n"
    byte_length = (
        len(prefix.encode("utf-8")) + len(value.encode("utf-8")) + len(suffix.encode("utf-8"))
    )
    return _ChunkFieldSegment(
        event_id=event_id,
        field="structured",
        prefix=prefix,
        value=value,
        suffix=suffix,
        byte_length=byte_length,
    )


def _render_chunk_atoms(
    store: Any,
    chunk: ProjectionChunk,
    *,
    db_path: Path,
    max_turn_chars: int,
) -> Tuple[str, List[_ChunkTurnBlock]]:
    """Render the chunk preamble and per-turn atoms shared by every layout."""
    if max_turn_chars != 0:
        raise ValueError(
            "canonical Raw projection requires --max-turn-chars=0; "
            "implement a separately named Raw Preview for compact output"
        )
    turns = _load_turns(store, chunk)
    preamble = (
        _frontmatter(_chunk_frontmatter_fields(chunk, db_path))
        + f"# {chunk.source_agent} / {chunk.session_id}\n\n"
        + _PROJECTION_PREAMBLE_NOTICE
    )
    blocks: List[_ChunkTurnBlock] = []
    for turn in turns:
        ref = next((item for item in chunk.refs if item.event_id == turn["event_id"]), None)
        if ref is None:
            raise RuntimeError("Raw projection chunk lacks its canonical event reference")
        event_id = str(turn["event_id"])
        projection_metadata = validate_projection_metadata(turn)
        visible_values = {
            "user_content": str(turn.get("user_content") or ""),
            "assistant_content": str(turn.get("assistant_content") or ""),
            "reasoning": str(turn.get("reasoning") or ""),
            "structured": structured_field_text(turn),
        }
        event_marker = (
            EVENT_MARKER_PREFIX
            + json.dumps(
                {
                    "event_id": event_id,
                    "logical_event_id": str(turn.get("logical_event_id") or ""),
                    "field_hashes": {
                        field: _sha256_text(visible_values[field]) for field in VISIBLE_FIELDS
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + " -->"
        )
        header = (
            render_projection_event_header(
                event_id=event_id,
                projection_metadata=projection_metadata,
                survival_score=ref.survival_score,
                search_count=ref.search_count,
                result_count=ref.result_count,
                hit_count=ref.hit_count,
                reference_count=ref.reference_count,
            )
            + f"{event_marker}\n\n"
        )
        segments = (
            _field_segment(event_id, "user_content", "User", visible_values["user_content"]),
            _field_segment(
                event_id,
                "assistant_content",
                "Assistant",
                visible_values["assistant_content"],
            ),
            _field_segment(event_id, "reasoning", "Reasoning", visible_values["reasoning"]),
            _structured_segment(event_id, visible_values["structured"]),
        )
        blocks.append(
            _ChunkTurnBlock(
                header=header,
                fields=segments,
                byte_length=len(header.encode("utf-8"))
                + sum(segment.byte_length for segment in segments),
            )
        )
    return preamble, blocks


def render_chunk(
    store: Any,
    chunk: ProjectionChunk,
    *,
    db_path: Path,
    max_turn_chars: int,
) -> Tuple[str, bool]:
    preamble, blocks = _render_chunk_atoms(
        store,
        chunk,
        db_path=db_path,
        max_turn_chars=max_turn_chars,
    )
    text = preamble + "".join(block.text for block in blocks)
    return text.rstrip() + "\n", False


def render_projection_index_body(
    *,
    source_agent: str,
    session_id: str,
    base_stem: str,
    part_count: int,
) -> str:
    """Render the deterministic index-page body shared by writer and auditor."""
    part_links = "\n".join(
        f"{index}. [[{base_stem}.part-{index:03d}]]" for index in range(1, part_count + 1)
    )
    return (
        f"# {source_agent} / {session_id}\n\n"
        "> Paged Raw projection index. This chunk exceeded the per-file byte "
        "budget; the complete lossless content is the ordered concatenation "
        f"of part-001 through part-{part_count:03d}.\n\n"
        "## Parts\n\n" + part_links + "\n"
    )


def _render_part_preamble(
    chunk: ProjectionChunk,
    base_relative_path: str,
    db_path: Path,
    *,
    part_index: int,
    part_count: int,
) -> str:
    fields = {
        "mnemos_type": "raw_retention_projection",
        "projection_version": PROJECTION_VERSION,
        "projection_contract": PROJECTION_CONTRACT,
        "canonical_db": str(db_path),
        "source": chunk.source_agent,
        "session_id": chunk.session_id,
        "chunk_file": base_relative_path,
        "part_index": part_index,
        "part_count": part_count,
        "turn_start": chunk.start_turn + 1,
        "turn_end": chunk.end_turn + 1,
        "retention_state": "active",
        "tags": [
            "raw-retention-projection",
            f"source={chunk.source_agent}",
            "canonical=raw_events",
        ],
    }
    return (
        _frontmatter(fields)
        + f"# {chunk.source_agent} / {chunk.session_id} "
        + f"(part {part_index}/{part_count})\n\n"
        + _PROJECTION_PREAMBLE_NOTICE
    )


def _render_chunk_index_page(
    chunk: ProjectionChunk,
    base_relative_path: str,
    db_path: Path,
    part_entries: List[Dict[str, Any]],
) -> str:
    fields = _chunk_frontmatter_fields(chunk, db_path)
    fields["mnemos_type"] = PROJECTION_INDEX_MNEMOS_TYPE
    fields["part_count"] = len(part_entries)
    fields["parts"] = part_entries
    base_name = Path(base_relative_path).name
    base_stem = base_name[: -len(".md")] if base_name.endswith(".md") else base_name
    return _frontmatter(fields) + render_projection_index_body(
        source_agent=chunk.source_agent,
        session_id=chunk.session_id,
        base_stem=base_stem,
        part_count=len(part_entries),
    )


def _utf8_prefix_cut(encoded: bytes, budget: int) -> int:
    """Largest cut <= ``budget`` that lands on a whole-character boundary."""
    cut = min(max(budget, 0), len(encoded))
    while cut > 0 and (encoded[cut] & 0xC0) == 0x80:
        cut -= 1
    if cut == 0 and encoded:
        # The first character alone exceeds the budget: take it whole so the
        # slice always makes progress.
        lead = encoded[0]
        cut = 1 if lead < 0x80 else 2 if lead < 0xE0 else 3 if lead < 0xF0 else 4
        cut = min(cut, len(encoded))
    return cut


def _take_line_slice(value: str, budget: int) -> Tuple[str, str]:
    """Split ``value`` at a line boundary whenever whole lines fit the budget.

    Whole lines are packed greedily and are never cut while another line fits.
    A single line that itself exceeds the budget is split inside the line at
    the byte budget, backing off to a whole-character boundary so every slice
    stays valid UTF-8 (one whole character is the atomic minimum).
    """
    if not value:
        return "", ""
    taken: List[str] = []
    taken_bytes = 0
    lines = value.splitlines(keepends=True)
    rest_index = len(lines)
    for position, line in enumerate(lines):
        encoded = line.encode("utf-8")
        if taken and taken_bytes + len(encoded) > budget:
            rest_index = position
            break
        if not taken and len(encoded) > budget:
            cut = _utf8_prefix_cut(encoded, budget)
            slice_text = encoded[:cut].decode("utf-8")
            rest = encoded[cut:].decode("utf-8") + "".join(lines[position + 1 :])
            return slice_text, rest
        taken.append(line)
        taken_bytes += len(encoded)
        if taken_bytes >= budget:
            rest_index = position + 1
            break
    return "".join(taken), "".join(lines[rest_index:])


def _pack_chunk_blocks(
    blocks: List[_ChunkTurnBlock],
    *,
    preamble_bytes: Any,
    max_file_bytes: int,
) -> List[str]:
    """Greedily pack turn blocks into part bodies bounded by ``max_file_bytes``.

    Turn blocks are atomic; an oversized turn decomposes into its header plus
    field segments, and an oversized field segment splits its value at line
    boundaries, cutting inside a single oversized line at whole-character byte
    boundaries.  A part that ends inside a field carries no closing marker and
    the next part resumes the field with a continuation marker, so the ordered
    concatenation of part bodies restores the exact chunk body.
    """
    parts: List[str] = []
    buffer: List[str] = []
    used = preamble_bytes(1)

    def flush() -> None:
        nonlocal buffer, used
        parts.append("".join(buffer))
        buffer = []
        used = preamble_bytes(len(parts) + 1)

    def emit_oversized_field(segment: _ChunkFieldSegment) -> None:
        nonlocal used
        cont_line = _field_cont_marker(segment.event_id, segment.field) + "\n"
        cont_bytes = len(cont_line.encode("utf-8"))
        prefix_bytes = len(segment.prefix.encode("utf-8"))
        suffix_bytes = len(segment.suffix.encode("utf-8"))
        remaining = segment.value
        first = True
        while True:
            wrapper = segment.prefix if first else cont_line
            wrapper_bytes = prefix_bytes if first else cont_bytes
            budget = max_file_bytes - used - wrapper_bytes - suffix_bytes
            slice_text, remaining = _take_line_slice(remaining, budget)
            last = not remaining
            piece = wrapper + slice_text + (segment.suffix if last else "")
            buffer.append(piece)
            used += len(piece.encode("utf-8"))
            if last:
                return
            flush()
            first = False

    for block in blocks:
        if used + block.byte_length <= max_file_bytes:
            buffer.append(block.text)
            used += block.byte_length
            continue
        if buffer:
            flush()
        if used + block.byte_length <= max_file_bytes:
            buffer.append(block.text)
            used += block.byte_length
            continue
        header_bytes = len(block.header.encode("utf-8"))
        if buffer and used + header_bytes > max_file_bytes:
            flush()
        buffer.append(block.header)
        used += header_bytes
        for segment in block.fields:
            if used + segment.byte_length <= max_file_bytes:
                buffer.append(segment.text)
                used += segment.byte_length
                continue
            if buffer:
                flush()
            if used + segment.byte_length <= max_file_bytes:
                buffer.append(segment.text)
                used += segment.byte_length
                continue
            emit_oversized_field(segment)
    if buffer:
        flush()
    return parts


def render_chunk_parts(
    store: Any,
    chunk: ProjectionChunk,
    *,
    db_path: Path,
    max_turn_chars: int,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> List[Tuple[str, str]]:
    """Render one chunk as ``[(path_suffix, text)]`` projection artifacts.

    The empty suffix is the chunk's base ``<source>/<date>/<name>.md`` path.
    When ``max_file_bytes`` is 0 or the single-file rendering fits the cap the
    result is exactly ``[("", text)]`` and byte-identical to ``render_chunk``.
    An oversized chunk returns ``[("", index_text), (".part-001", text), ...]``:
    the base path becomes an index page and each numbered suffix is a
    standalone part file whose ordered body concatenation restores the chunk
    body (continuation markers rejoin split fields).
    """
    if max_turn_chars != 0:
        raise ValueError(
            "canonical Raw projection requires --max-turn-chars=0; "
            "implement a separately named Raw Preview for compact output"
        )
    if max_file_bytes < 0:
        raise ValueError("canonical Raw projection requires --max-file-bytes>=0")
    preamble, blocks = _render_chunk_atoms(
        store,
        chunk,
        db_path=db_path,
        max_turn_chars=max_turn_chars,
    )
    if blocks:
        # The joined document ends with the final field suffix ``-->\n\n``; the
        # single-file render rstrips that trailing blank line, saving one byte.
        single_bytes = (
            len(preamble.encode("utf-8")) + sum(block.byte_length for block in blocks) - 1
        )
    else:
        single_bytes = len(preamble.rstrip().encode("utf-8")) + 1
    if max_file_bytes == 0 or single_bytes <= max_file_bytes:
        single_text = (preamble + "".join(block.text for block in blocks)).rstrip() + "\n"
        return [("", single_text)]
    base_relative_path = _chunk_relative_path(chunk)
    part_count_guess = 1
    part_contents: List[str] = []
    for _attempt in range(16):
        part_contents = _pack_chunk_blocks(
            blocks,
            preamble_bytes=lambda index: len(
                _render_part_preamble(
                    chunk,
                    base_relative_path,
                    db_path,
                    part_index=index,
                    part_count=part_count_guess,
                ).encode("utf-8")
            ),
            max_file_bytes=max_file_bytes,
        )
        if len(part_contents) == part_count_guess:
            break
        part_count_guess = len(part_contents)
    else:
        raise RuntimeError("Raw projection part packing did not stabilize")
    part_count = len(part_contents)
    rendered: List[Tuple[str, str]] = []
    part_entries: List[Dict[str, Any]] = []
    for index, content in enumerate(part_contents, start=1):
        suffix = f".part-{index:03d}"
        text = (
            _render_part_preamble(
                chunk,
                base_relative_path,
                db_path,
                part_index=index,
                part_count=part_count,
            )
            + content
        )
        rendered.append((suffix, text))
        part_entries.append(
            {
                "path": _part_relative_path(base_relative_path, suffix),
                "bytes": len(text.encode("utf-8")),
                "sha256": _sha256_text(text),
            }
        )
    index_text = _render_chunk_index_page(chunk, base_relative_path, db_path, part_entries)
    return [("", index_text), *rendered]


@dataclass(frozen=True)
class ProjectionArtifact:
    """One deterministic Markdown projection owned by the Raw publisher."""

    relative_path: str
    text: str
    sha256: str
    event_ids: Tuple[str, ...]
    logical_event_ids: Tuple[str, ...]
    revision_set_hash: str
    source_agent: str
    session_id: str
    tags: Tuple[str, ...]
    index_state: Dict[str, Any] | None = None


def _is_projection_internal_path(relative_path: Path) -> bool:
    return any(
        part == PROJECTION_TRANSACTION_DIR
        or re.fullmatch(
            re.escape(PROJECTION_TRANSACTION_TOMBSTONE_PREFIX) + r"[0-9a-f]{32}",
            part,
        )
        is not None
        for part in relative_path.parts
    )


def _existing_markdown_files(raw_dir: Path) -> List[Path]:
    root_kind = _projection_path_kind(raw_dir)
    if root_kind == "missing":
        return []
    if root_kind != "directory":
        raise ValueError("Raw projection vault root is unsafe")
    try:
        candidates = sorted(
            path
            for path in raw_dir.rglob("*.md")
            if ".obsidian" not in path.relative_to(raw_dir).parts
            and not _is_projection_internal_path(path.relative_to(raw_dir))
        )
    except OSError as exc:
        raise ValueError(
            "Raw projection vault inventory is unavailable"
        ) from exc
    for path in candidates:
        _safe_projection_target(raw_dir, path.relative_to(raw_dir))
    return candidates


def _existing_vault_file_count(raw_dir: Path) -> int:
    root_kind = _projection_path_kind(raw_dir)
    if root_kind == "missing":
        return 0
    if root_kind != "directory":
        raise ValueError("Raw projection vault root is unsafe")
    try:
        candidates = tuple(raw_dir.rglob("*"))
    except OSError as exc:
        raise ValueError(
            "Raw projection vault inventory is unavailable"
        ) from exc
    count = 0
    for path in candidates:
        relative = path.relative_to(raw_dir)
        if (
            ".obsidian" in relative.parts
            or _is_projection_internal_path(relative)
        ):
            continue
        if _projection_path_kind(path) == "file":
            count += 1
    return count


def _journal_path(raw_dir: Path) -> Path:
    return raw_dir / PROJECTION_JOURNAL_NAME


def _load_projection_journal(raw_dir: Path) -> Dict[str, Any]:
    root_kind = _projection_path_kind(raw_dir)
    if root_kind == "missing":
        return {}
    if root_kind != "directory":
        raise ValueError("Raw projection vault root is unsafe")
    journal_path = _journal_path(raw_dir)
    journal_kind = _projection_path_kind(journal_path)
    if journal_kind == "missing":
        return {}
    if journal_kind != "file":
        raise ValueError("projection journal path is unsafe")

    def reject_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"projection journal has duplicate key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            read_native_bytes(journal_path).decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("projection journal is unreadable") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "projection_contract", "generation_hash", "files"}
        or payload.get("schema_version") != "mnemos.raw_projection.v2"
        or payload.get("projection_contract") != PROJECTION_CONTRACT
    ):
        raise ValueError("projection journal contract is invalid")
    files = payload.get("files")
    if not isinstance(files, dict):
        raise ValueError("projection journal files contract is invalid")
    expected_generation_hash = _sha256_text(
        json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    if payload.get("generation_hash") != expected_generation_hash:
        raise ValueError("projection journal generation hash is invalid")
    for relative_path, metadata in files.items():
        relative = Path(str(relative_path))
        if (
            not isinstance(relative_path, str)
            or relative.is_absolute()
            or ".." in relative.parts
            or not isinstance(metadata, dict)
            or set(metadata)
            != {
                "content_hash",
                "logical_event_ids",
                "revision_ids",
                "revision_set_hash",
            }
        ):
            raise ValueError("projection journal file record is invalid")
        _safe_projection_target(raw_dir, relative_path)
        revision_ids = metadata.get("revision_ids")
        logical_event_ids = metadata.get("logical_event_ids")
        content_hash = metadata.get("content_hash")
        if (
            not isinstance(content_hash, str)
            or len(content_hash) != 64
            or any(character not in "0123456789abcdef" for character in content_hash)
            or not isinstance(revision_ids, list)
            or not revision_ids
            or not all(isinstance(item, str) and item for item in revision_ids)
            or len(set(revision_ids)) != len(revision_ids)
            or not isinstance(logical_event_ids, list)
            or len(logical_event_ids) != len(revision_ids)
            or not all(isinstance(item, str) and item for item in logical_event_ids)
            or metadata.get("revision_set_hash")
            != _sha256_text(
                json.dumps(
                    revision_ids,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        ):
            raise ValueError("projection journal file metadata is invalid")
    return payload


def _journal_file_paths(journal: Dict[str, Any]) -> set[str]:
    files = journal.get("files")
    if not isinstance(files, dict):
        return set()
    return {
        str(relative_path)
        for relative_path, metadata in files.items()
        if isinstance(relative_path, str) and isinstance(metadata, dict)
    }


def _is_managed_projection_file(path: Path) -> bool:
    try:
        text = _decode_utf8_prefix(read_native_bytes(path)[:4096])
    except UnicodeError as exc:
        raise ValueError(
            "managed Raw projection file is not valid UTF-8"
        ) from exc
    except OSError as exc:
        raise ValueError(
            "managed Raw projection file is unreadable"
        ) from exc
    return bool(
        re.search(
            r'^mnemos_type:\s+["\']?raw_retention_projection(?:_index)?["\']?\s*$',
            text,
            flags=re.MULTILINE,
        )
    )


def _managed_projection_paths(raw_dir: Path) -> set[str]:
    journal_paths = _journal_file_paths(_load_projection_journal(raw_dir))
    legacy_paths = {
        path.relative_to(raw_dir).as_posix()
        for path in _existing_markdown_files(raw_dir)
        if _is_managed_projection_file(path)
    }
    return journal_paths | legacy_paths


def managed_projection_paths(raw_dir: Path) -> List[str]:
    """Return only publisher-owned Markdown paths, never unrelated vault notes."""
    existing: list[str] = []
    for relative_path in _managed_projection_paths(raw_dir):
        kind = _projection_path_kind(raw_dir / relative_path)
        if kind == "file":
            existing.append(relative_path)
        elif kind != "missing":
            raise ValueError("managed Raw projection path is unsafe")
    return sorted(existing)


from scripts.raw_projection_secure_io import (  # noqa: F401
    _open_secure_directory_path,
    _ensure_safe_projection_root,
    _acquire_projection_transaction_lock,
    _release_projection_transaction_lock,
    _secure_projection_parent_fd,
    _fd_file_hash,
    _secure_publish_staged_file,
    _secure_atomic_write_bytes,
    _secure_atomic_write_text,
    _secure_read_file,
    _secure_delete_managed_file,
    _decode_utf8_prefix,
)


from scripts.raw_projection_transaction_runtime import (  # noqa: F401
    _transaction_path,
    _cleanup_projection_transaction_tombstones,
    _remove_projection_transaction,
    _create_projection_transaction,
    _write_projection_transaction_state,
    _projection_transaction_entries,
    _promote_projection_transaction_state_temp,
    _load_projection_transaction,
    _prepare_projection_transaction,
    _publish_projection_transaction,
    _rollback_projection_transaction,
    recover_interrupted_projection,
    _recover_interrupted_projection_locked,
    _artifact_descriptors,
    _read_file_hash,
    _write_change_manifest,
    _json_text,
    _projection_journal,
    write_projection,
)


from scripts.raw_projection_plan_runtime import (  # noqa: F401
    _expected_index_state,
    _raw_index_write_set,
    build_projection_plan,
    _validated_projection_plan,
    validate_projection_plan,
    update_raw_index_changes,
    rebuild_raw_index,
    plan_projection,
    apply_projection,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", default="", help="Obsidian raw vault path")
    parser.add_argument("--db-path", default="", help="raw_events.db path")
    parser.add_argument(
        "--canonical-db-identity",
        default="",
        help=(
            "Canonical production raw_events.db identity rendered into output when "
            "--db-path points to a checkpointed read-only snapshot"
        ),
    )
    parser.add_argument(
        "--backup-dir",
        default="",
        help="Write a metadata-only change manifest here; Raw files are never copied or moved",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=DEFAULT_MAX_FILES,
        help=(
            "Must remain 0 for canonical complete Raw; use a separately named "
            "Raw Preview for compact output"
        ),
    )
    parser.add_argument("--chunk-turns", type=int, default=DEFAULT_CHUNK_TURNS)
    parser.add_argument(
        "--max-turn-chars",
        type=int,
        default=DEFAULT_MAX_TURN_CHARS,
        help="Must remain 0 for canonical lossless Raw; use a separately named Raw Preview",
    )
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=DEFAULT_MAX_FILE_BYTES,
        help=(
            "Per-file byte cap; oversized chunks publish an index page plus "
            "ordered <name>.part-NNN.md parts. 0 disables paging"
        ),
    )
    parser.add_argument("--include-eligible-delete", action="store_true")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Atomically publish changed chunks only; never move unrelated vault files",
    )
    parser.add_argument(
        "--expected-plan-hash",
        default="",
        help="Required with CLI --apply; must equal the reviewed dry-run plan_hash",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    args = build_parser().parse_args()
    if args.apply and not args.expected_plan_hash:
        raise ValueError("--apply requires --expected-plan-hash from a reviewed dry-run")
    if args.apply and not args.backup_dir:
        raise ValueError("--apply requires --backup-dir for immutable metadata receipts")
    if args.apply:
        cfg = get_config()
        raw_dir = Path(args.raw_dir).expanduser() if args.raw_dir else cfg.obsidian_vault_path
        recovery = recover_interrupted_projection(
            raw_dir,
            expected_plan_hash=args.expected_plan_hash,
            expected_backup_dir=Path(args.backup_dir).expanduser(),
        )
        if recovery["recovered"]:
            payload = {
                "status": "recovered_for_replan",
                "production_effect": "interrupted_projection_recovered",
                **recovery,
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                print(f"raw projection recovery: {json.dumps(payload, ensure_ascii=False)}")
            return 0
    store, chunks, stats = plan_projection(args)
    try:
        if args.apply:
            stats = apply_projection(args, store, chunks, stats)
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            action = "applied" if args.apply else "dry-run"
            print(f"raw projection {action}: {json.dumps(stats, ensure_ascii=False)}")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
