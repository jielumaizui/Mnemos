# -*- coding: utf-8 -*-
"""Strict read-only projection of canonical Raw revisions."""

from __future__ import annotations

import json
import sqlite3
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence

from core.ops.durable_io import DurableIOError, inspect_path_kind
from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.sync_framework.raw_event_identity import _NATIVE_RAW_CONTRACT_LEDGER
from core.sync_framework.raw_event_identity_aliases import alias_table_exists
from core.sync_framework.raw_subject_deletion import (
    RAW_SUBJECT_DELETION_TABLE,
    subject_deletion_table_exists,
    subject_deletion_visibility_predicate,
)


class CanonicalRawReadError(RuntimeError):
    """Raised when a read-only consumer cannot prove canonical Raw input."""


@dataclass(frozen=True)
class CanonicalRawTurn:
    """One eligible current immutable Raw revision for read-only consumers.

    This is intentionally a narrow typed projection rather than a second Raw
    store.  Consumers receive the canonical logical event, exact current
    revision, visible content, and a stable cursor tuple without instantiating
    ``RawEventStore`` (whose constructor owns mutable schema/lifecycle setup).
    """

    logical_event_id: str
    revision_id: str
    source_agent: str
    session_id: str
    conversation_at: str
    captured_at: str
    updated_at: str
    content_hash: str
    user_content: str
    assistant_content: str
    reasoning: str
    tool_calls: list[Dict[str, Any]]
    tool_results: list[Dict[str, Any]]
    attachments: list[Dict[str, Any]]
    raw_event_refs: list[Dict[str, Any]]
    source_files: list[str]
    authority_context: Dict[str, Any] = field(default_factory=dict)

    @property
    def cursor_token(self) -> Dict[str, str]:
        return {
            "updated_at": self.updated_at,
            "event_id": self.logical_event_id,
            "revision_id": self.revision_id,
        }


def _decompress_text(blob: bytes | None) -> str:
    if not blob:
        return ""
    return zlib.decompress(blob).decode("utf-8")


def decode_raw_revision_snapshot(snapshot_blob: bytes | None) -> Dict[str, Any]:
    """Decode the canonical immutable revision payload without opening a store.

    Read-only consumers such as Observation must use the same snapshot format
    as ``RawEventStore.get_turn`` while avoiding the store constructor, which
    provisions mutable schema and lifecycle state.  Invalid snapshots are a
    schema/data failure, never a reason to silently fall back to Markdown.
    """
    try:
        decoded = json.loads(_decompress_text(snapshot_blob) or "{}")
    except (UnicodeDecodeError, ValueError, zlib.error) as exc:
        raise ValueError("invalid canonical Raw revision snapshot") from exc
    if not isinstance(decoded, dict):
        raise ValueError("canonical Raw revision snapshot must be an object")
    return decoded


def canonical_observation_text(turn: Dict[str, Any]) -> str:
    """Return the stable visible text contract consumed by Observation.

    Spans recorded with ``consumer_type=observation`` are offsets in this
    exact string.  It is deliberately derived only from the current immutable
    Raw revision's visible user and assistant content, not a Markdown
    projection or a filesystem path.
    """
    return "\n\n".join(
        (
            str(turn.get("user_content") or ""),
            str(turn.get("assistant_content") or ""),
        )
    )


def _read_only_raw_connection(db_path: Path) -> sqlite3.Connection:
    """Open canonical Raw without creating a database, WAL, or schema."""
    try:
        db_kind = inspect_path_kind(db_path)
    except DurableIOError:
        raise CanonicalRawReadError(
            f"canonical raw database is unavailable: {db_path}"
        ) from None
    if db_kind == "missing":
        raise CanonicalRawReadError(f"canonical raw database is missing: {db_path}")
    if db_kind != "file":
        raise CanonicalRawReadError(
            f"canonical raw database is not a regular file: {db_path}"
        )
    try:
        return connect_readonly_sqlite(db_path, timeout_seconds=5)
    except (OSError, sqlite3.Error) as exc:
        raise CanonicalRawReadError(
            f"canonical raw database is unreadable: {exc.__class__.__name__}"
        ) from exc


def _readonly_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()  # nosec B608
        }
    except sqlite3.Error as exc:
        raise CanonicalRawReadError(
            f"canonical raw schema cannot inspect {table}: {exc.__class__.__name__}"
        ) from exc


def _validate_canonical_raw_read_contract(conn: sqlite3.Connection) -> None:
    """Validate the immutable Raw read contract without mutating SQLite."""
    required = {
        "raw_turns": {
            "event_id",
            "current_revision_id",
            "source_agent",
            "session_id",
            "conversation_at",
            "captured_at",
            "updated_at",
        },
        "raw_turn_revisions": {
            "revision_id",
            "logical_event_id",
            "content_hash",
            "snapshot_blob",
        },
        "raw_metrics": {"event_id", "retention_state"},
    }
    for table, columns in required.items():
        available = _readonly_table_columns(conn, table)
        if not columns <= available:
            missing = ", ".join(sorted(columns - available))
            raise CanonicalRawReadError(
                f"canonical raw schema is incomplete for {table}: {missing}"
            )

    # Canonical identity and source-support visibility are not optional
    # decorations.  A pre-contract database must be reconciled explicitly;
    # treating it as readable would make a Markdown-like reconstruction
    # appear authoritative merely because its base tables happen to exist.
    if not alias_table_exists(conn):
        raise CanonicalRawReadError("canonical raw identity alias schema is missing")
    if not subject_deletion_table_exists(conn):
        raise CanonicalRawReadError("canonical raw subject deletion schema is missing")
    required_subject_deletion_columns = {"event_id", "status"}
    subject_deletion_columns = _readonly_table_columns(conn, RAW_SUBJECT_DELETION_TABLE)
    if not required_subject_deletion_columns <= subject_deletion_columns:
        missing = ", ".join(
            sorted(required_subject_deletion_columns - subject_deletion_columns)
        )
        raise CanonicalRawReadError(
            "canonical raw subject deletion schema is incomplete: " + missing
        )
    required_contract_columns = {
        "logical_event_id",
        "observed_revision_id",
        "contract_state",
        "support_manifest_hash",
        "observed_at",
        "contract_errors_json",
    }
    native_contract_columns = _readonly_table_columns(
        conn, "raw_native_contract_observations"
    )
    if not required_contract_columns <= native_contract_columns:
        missing = ", ".join(sorted(required_contract_columns - native_contract_columns))
        raise CanonicalRawReadError(
            "canonical raw native contract schema is incomplete: " + missing
        )


def _canonical_raw_current_scope_predicate() -> str:
    """Return lifecycle/privacy scope before current-contract certification."""
    return """
        COALESCE(m.retention_state, 'active') != 'eligible_delete'
        AND NOT EXISTS (
            SELECT 1
            FROM raw_event_identity_aliases AS alias
            WHERE alias.alias_event_id=t.event_id
        )
    """ + subject_deletion_visibility_predicate("t.event_id")


def _canonical_raw_visibility_predicate() -> str:
    """Return the shared eligible-current Raw predicate for read-only clients."""
    return (
        """
        t.current_revision_id IS NOT NULL
        AND
        """
        + _canonical_raw_current_scope_predicate()
        + _NATIVE_RAW_CONTRACT_LEDGER.current_event_visibility_predicate(
            "t.event_id"
        )
    )


def _validate_current_projection_bindings(conn: sqlite3.Connection) -> None:
    """Fail closed before a join can silently erase a corrupt current pointer."""
    query = """
        SELECT
            t.event_id,
            t.current_revision_id,
            t.content_hash,
            t.full_content_hash,
            r.logical_event_id,
            r.content_hash,
            r.full_content_hash
        FROM raw_turns AS t
        LEFT JOIN raw_turn_revisions AS r
          ON r.revision_id=t.current_revision_id
        LEFT JOIN raw_metrics AS m ON m.event_id=t.event_id
        WHERE
    """ + _canonical_raw_current_scope_predicate()
    try:
        rows = conn.execute(query)
        for row in rows:
            event_id = str(row[0] or "")
            revision_id = str(row[1] or "")
            if not event_id or not revision_id or row[4] is None:
                raise CanonicalRawReadError(
                    "canonical raw current revision is missing"
                )
            if str(row[4] or "") != event_id:
                raise CanonicalRawReadError(
                    "canonical raw current revision has a different logical owner"
                )
            if (
                str(row[5] or "") != str(row[2] or "")
                or str(row[6] or "") != str(row[3] or "")
            ):
                raise CanonicalRawReadError(
                    "canonical raw current revision header differs from its projection"
                )
            try:
                _NATIVE_RAW_CONTRACT_LEDGER.latest(conn, event_id)
            except ValueError as exc:
                raise CanonicalRawReadError(
                    "canonical raw current revision contract observation is invalid"
                ) from exc
    except CanonicalRawReadError:
        raise
    except sqlite3.Error as exc:
        raise CanonicalRawReadError(
            f"canonical raw current-revision validation failed: {exc.__class__.__name__}"
        ) from exc


def iter_current_raw_turns_readonly(
    db_path: str | Path,
    *,
    cursor: Optional[Mapping[str, str]] = None,
    limit: Optional[int] = None,
    max_snapshot_bytes: Optional[int] = None,
    include_structured_payload: bool = True,
) -> Iterator[CanonicalRawTurn]:
    """Yield eligible current Raw revisions through a bounded typed API.

    Rows are decoded one at a time while the SQLite cursor remains open.  This
    is intentionally an iterator rather than a convenience list: production
    Raw databases can hold multi-gigabyte lossless snapshots.  ``limit`` and
    ``max_snapshot_bytes`` bound a page without changing the denominator; a
    single oversized revision is yielded alone so it remains retryable.
    ``include_structured_payload=False`` keeps only the fields Observation
    needs, preventing a source page from retaining every historical tool
    payload after the snapshot has been verified.
    """
    if limit is not None and limit < 1:
        return
    if max_snapshot_bytes is not None and max_snapshot_bytes < 1:
        raise ValueError("max_snapshot_bytes must be positive when provided")

    path = Path(db_path).expanduser()
    with _read_only_raw_connection(path) as conn:
        _validate_canonical_raw_read_contract(conn)
        _validate_current_projection_bindings(conn)
        query = """
            SELECT
                t.event_id, t.current_revision_id, t.source_agent, t.session_id,
                t.conversation_at, t.captured_at, t.updated_at,
                r.logical_event_id, r.content_hash, r.snapshot_blob
            FROM raw_turns AS t
            JOIN raw_turn_revisions AS r ON r.revision_id=t.current_revision_id
            LEFT JOIN raw_metrics AS m ON m.event_id=t.event_id
            WHERE
        """ + _canonical_raw_visibility_predicate()
        params: list[Any] = []
        token = {str(key): str(value) for key, value in (cursor or {}).items()}
        if token:
            required_token = {"updated_at", "event_id", "revision_id"}
            if not required_token <= set(token):
                raise CanonicalRawReadError("canonical raw cursor is incomplete")
            query += """
                AND (
                    t.updated_at > ?
                    OR (t.updated_at = ? AND t.event_id > ?)
                    OR (
                        t.updated_at = ? AND t.event_id = ?
                        AND t.current_revision_id > ?
                    )
                )
            """
            params.extend(
                (
                    token["updated_at"],
                    token["updated_at"],
                    token["event_id"],
                    token["updated_at"],
                    token["event_id"],
                    token["revision_id"],
                )
            )
        query += " ORDER BY t.updated_at, t.event_id, t.current_revision_id"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))

        try:
            rows = conn.execute(query, params)
        except sqlite3.Error as exc:
            raise CanonicalRawReadError(
                f"canonical raw current-revision query failed: {exc.__class__.__name__}"
            ) from exc

        decoded_snapshot_bytes = 0
        yielded = 0
        for row in rows:
            snapshot_blob = row[9]
            snapshot_bytes = len(snapshot_blob or b"")
            if (
                max_snapshot_bytes is not None
                and yielded > 0
                and decoded_snapshot_bytes + snapshot_bytes > max_snapshot_bytes
            ):
                break

            logical_event_id = str(row[0] or "")
            revision_id = str(row[1] or "")
            if not logical_event_id or not revision_id:
                raise CanonicalRawReadError(
                    "canonical raw row is missing event or revision identity"
                )
            if str(row[7] or "") != logical_event_id:
                raise CanonicalRawReadError(
                    f"canonical raw revision {revision_id} does not bind its current logical event"
                )
            try:
                payload = decode_raw_revision_snapshot(snapshot_blob)
            except ValueError as exc:
                raise CanonicalRawReadError(
                    f"canonical raw revision {revision_id} snapshot is invalid"
                ) from exc
            if str(payload.get("event_id") or "") != logical_event_id:
                raise CanonicalRawReadError(
                    f"canonical raw revision {revision_id} does not match its logical event"
                )
            source_agent = str(payload.get("source_agent") or row[2] or "")
            session_id = str(payload.get("session_id") or row[3] or "")
            if source_agent != str(row[2] or "") or session_id != str(row[3] or ""):
                raise CanonicalRawReadError(
                    f"canonical raw revision {revision_id} identity differs from current header"
                )
            revision_content_hash = str(row[8] or "")
            if (
                not revision_content_hash
                or str(payload.get("content_hash") or "") != revision_content_hash
            ):
                raise CanonicalRawReadError(
                    f"canonical raw revision {revision_id} content hash differs from its snapshot"
                )
            decoded_snapshot_bytes += snapshot_bytes
            yielded += 1
            yield CanonicalRawTurn(
                logical_event_id=logical_event_id,
                revision_id=revision_id,
                source_agent=source_agent,
                session_id=session_id,
                conversation_at=str(payload.get("conversation_at") or row[4] or ""),
                captured_at=str(payload.get("captured_at") or row[5] or ""),
                updated_at=str(row[6] or ""),
                content_hash=revision_content_hash,
                user_content=str(payload.get("user_content") or ""),
                assistant_content=str(payload.get("assistant_content") or ""),
                reasoning=(
                    str(payload.get("reasoning") or "")
                    if include_structured_payload
                    else ""
                ),
                tool_calls=(list(payload.get("tool_calls") or []) if include_structured_payload else []),
                tool_results=(
                    list(payload.get("tool_results") or [])
                    if include_structured_payload
                    else []
                ),
                attachments=(
                    list(payload.get("attachments") or [])
                    if include_structured_payload
                    else []
                ),
                raw_event_refs=(
                    list(payload.get("raw_event_refs") or [])
                    if include_structured_payload
                    else []
                ),
                source_files=(
                    list(payload.get("source_files") or [])
                    if include_structured_payload
                    else []
                ),
                authority_context={
                    key: value
                    for key in (
                        "asset_kind",
                        "content_source",
                        "source_authority",
                        "source_authority_purpose",
                        "capture_source",
                    )
                    if (value := dict(payload.get("metadata") or {}).get(key))
                    not in (None, "")
                },
            )


def list_current_raw_turns_readonly(
    db_path: str | Path,
    *,
    cursor: Optional[Mapping[str, str]] = None,
    limit: Optional[int] = None,
    max_snapshot_bytes: Optional[int] = None,
    include_structured_payload: bool = True,
) -> list[CanonicalRawTurn]:
    """Return eligible current Raw revisions through a non-mutating typed API.

    ``cursor`` is a lexicographic tuple over the canonical Raw update order.
    It is deliberately revision-aware: when one logical event receives a new
    current revision, its updated timestamp/revision pair appears again rather
    than being hidden by a logical-event-only watermark.
    """
    return list(
        iter_current_raw_turns_readonly(
            db_path,
            cursor=cursor,
            limit=limit,
            max_snapshot_bytes=max_snapshot_bytes,
            include_structured_payload=include_structured_payload,
        )
    )


def require_admissible_raw_revision(
    conn: sqlite3.Connection,
    *,
    logical_event_id: str,
    revision_id: str,
) -> Mapping[str, Any] | None:
    """Prove one exact revision is safe for automatic execution.

    This connection-level owner lets reviewed migration/replay plans enforce
    the same policy inside their own SQLite snapshot and again immediately
    before apply.  Forensic readers deliberately do not call it.
    """
    normalized_event_id = str(logical_event_id or "")
    normalized_revision_id = str(revision_id or "")
    if not normalized_event_id or not normalized_revision_id:
        raise CanonicalRawReadError("canonical Raw revision is not admissible")
    try:
        owner = conn.execute(
            """
            SELECT logical_event_id
            FROM raw_turn_revisions
            WHERE revision_id=?
            """,
            (normalized_revision_id,),
        ).fetchone()
        if owner is None or str(owner[0] or "") != normalized_event_id:
            raise CanonicalRawReadError(
                f"canonical Raw revision {normalized_revision_id} is not admissible"
            )
        latest = _NATIVE_RAW_CONTRACT_LEDGER.latest_for_revision(
            conn,
            logical_event_id=normalized_event_id,
            revision_id=normalized_revision_id,
        )
    except CanonicalRawReadError:
        raise
    except (sqlite3.Error, ValueError) as exc:
        raise CanonicalRawReadError(
            f"canonical Raw revision {normalized_revision_id} is not admissible"
        ) from exc
    if latest is not None and latest["contract_state"] != "conformant":
        raise CanonicalRawReadError(
            f"canonical Raw revision {normalized_revision_id} is not admissible"
        )
    return latest


def _read_exact_raw_revisions_readonly(
    db_path: str | Path,
    revision_ids: Sequence[str],
    *,
    require_admissible: bool,
) -> list[CanonicalRawTurn]:
    """Read exact immutable revisions under one explicit consumer policy."""

    ordered_ids = [str(value).strip() for value in revision_ids if str(value).strip()]
    if not ordered_ids or len(set(ordered_ids)) != len(ordered_ids):
        raise ValueError("exact Raw revision ids must be non-empty and unique")
    path = Path(db_path).expanduser()
    with _read_only_raw_connection(path) as conn:
        _validate_canonical_raw_read_contract(conn)
        placeholders = ",".join("?" for _ in ordered_ids)
        rows = conn.execute(
            f"""
            SELECT t.event_id, r.revision_id, t.source_agent, t.session_id,
                   t.conversation_at, t.captured_at, t.updated_at,
                   r.logical_event_id, r.content_hash, r.snapshot_blob
            FROM raw_turn_revisions AS r
            JOIN raw_turns AS t ON t.event_id=r.logical_event_id
            WHERE r.revision_id IN ({placeholders})
              {subject_deletion_visibility_predicate("t.event_id")}
            """,  # nosec B608 - placeholders are generated solely from a count
            tuple(ordered_ids),
        ).fetchall()
        by_revision = {str(row[1]): row for row in rows}
        if require_admissible:
            for revision_id, row in by_revision.items():
                require_admissible_raw_revision(
                    conn,
                    logical_event_id=str(row[0] or ""),
                    revision_id=revision_id,
                )
    if set(by_revision) != set(ordered_ids):
        raise CanonicalRawReadError(
            "canonical Raw store does not contain every requested exact revision"
        )
    result: list[CanonicalRawTurn] = []
    for revision_id in ordered_ids:
        row = by_revision[revision_id]
        logical_event_id = str(row[0] or "")
        if str(row[7] or "") != logical_event_id:
            raise CanonicalRawReadError(
                f"canonical Raw revision {revision_id} has a mismatched logical event"
            )
        try:
            payload = decode_raw_revision_snapshot(row[9])
        except ValueError as exc:
            raise CanonicalRawReadError(
                f"canonical Raw revision {revision_id} snapshot is invalid"
            ) from exc
        if str(payload.get("event_id") or "") != logical_event_id:
            raise CanonicalRawReadError(
                f"canonical Raw revision {revision_id} snapshot identity is invalid"
            )
        source_agent = str(payload.get("source_agent") or row[2] or "")
        session_id = str(payload.get("session_id") or row[3] or "")
        if source_agent != str(row[2] or "") or session_id != str(row[3] or ""):
            raise CanonicalRawReadError(
                f"canonical Raw revision {revision_id} header identity is invalid"
            )
        content_hash = str(row[8] or "")
        if not content_hash or str(payload.get("content_hash") or "") != content_hash:
            raise CanonicalRawReadError(
                f"canonical Raw revision {revision_id} content hash is invalid"
            )
        metadata = dict(payload.get("metadata") or {})
        result.append(
            CanonicalRawTurn(
                logical_event_id=logical_event_id,
                revision_id=revision_id,
                source_agent=source_agent,
                session_id=session_id,
                conversation_at=str(payload.get("conversation_at") or row[4] or ""),
                captured_at=str(payload.get("captured_at") or row[5] or ""),
                updated_at=str(payload.get("updated_at") or row[6] or ""),
                content_hash=content_hash,
                user_content=str(payload.get("user_content") or ""),
                assistant_content=str(payload.get("assistant_content") or ""),
                reasoning=str(payload.get("reasoning") or ""),
                tool_calls=list(payload.get("tool_calls") or []),
                tool_results=list(payload.get("tool_results") or []),
                attachments=list(payload.get("attachments") or []),
                raw_event_refs=list(payload.get("raw_event_refs") or []),
                source_files=list(payload.get("source_files") or []),
                authority_context={
                    key: value
                    for key in (
                        "asset_kind",
                        "content_source",
                        "source_authority",
                        "source_authority_purpose",
                        "capture_source",
                    )
                    if (value := metadata.get(key)) not in (None, "")
                },
            )
        )
    return result


def read_raw_revisions_forensic_readonly(
    db_path: str | Path,
    revision_ids: Sequence[str],
) -> list[CanonicalRawTurn]:
    """Read exact immutable bytes, including quarantined and superseded history.

    Applied subject-deletion receipts remain an absolute privacy boundary.
    Native contract state, lifecycle retention, identity aliases, and logical
    current pointers do not rewrite or hide otherwise preserved forensic bytes.
    """
    return _read_exact_raw_revisions_readonly(
        db_path,
        revision_ids,
        require_admissible=False,
    )


def read_admissible_raw_revisions_readonly(
    db_path: str | Path,
    revision_ids: Sequence[str],
) -> list[CanonicalRawTurn]:
    """Read exact revisions that are safe for automatic execution/certification.

    Rows with no native observation retain the base canonical Raw contract.
    Once an append-only native observation exists, its latest logical verdict
    must be valid, conformant, and bound to the exact requested revision.
    """
    return _read_exact_raw_revisions_readonly(
        db_path,
        revision_ids,
        require_admissible=True,
    )


def count_current_raw_turns_readonly(db_path: str | Path) -> int:
    """Count eligible current Raw revisions without decoding any snapshot."""
    path = Path(db_path).expanduser()
    with _read_only_raw_connection(path) as conn:
        _validate_canonical_raw_read_contract(conn)
        _validate_current_projection_bindings(conn)
        query = """
            SELECT COUNT(*)
            FROM raw_turns AS t
            LEFT JOIN raw_metrics AS m ON m.event_id=t.event_id
            WHERE
        """ + _canonical_raw_visibility_predicate()
        try:
            row = conn.execute(query).fetchone()
        except sqlite3.Error as exc:
            raise CanonicalRawReadError(
                f"canonical raw current-revision count failed: {exc.__class__.__name__}"
            ) from exc
    return int(row[0] or 0) if row else 0
