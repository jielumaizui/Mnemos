"""Fail-closed reconciliation for legacy Raw turn identities.

Older Raw rows used ``source_agent/session_id/turn_number`` as the only
logical identity.  Current native ingestion uses a producer event ID or an
auditable parser/artifact offset.  The transition must not delete old evidence
or silently merge two real same-ordinal native events: a proven one-to-one
legacy/native match is recorded as an append-only alias, while ambiguity stays
blocking for manual review.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.ops.durable_io import DurableIOError, inspect_path_kind
from core.ops.readiness_query_budget import connect_readonly_sqlite


SCHEMA_VERSION = "mnemos.raw_event_identity_aliases.v1"
TABLE_NAME = "raw_event_identity_aliases"
LEGACY_IDENTITY_KINDS = frozenset({"missing", "legacy_turn_number"})
NATIVE_IDENTITY_KINDS = frozenset({"native_event_id", "parser_artifact_offset"})
_VISIBLE_FIELDS = (
    "user_content",
    "assistant_content",
    "reasoning",
    "tool_calls",
    "tool_results",
    "attachments",
    "raw_event_refs",
)


class RawEventIdentityAliasError(RuntimeError):
    """Raised when a historical/native alias cannot be proven safe."""


@dataclass(frozen=True)
class IdentityAliasCandidate:
    alias_event_id: str
    canonical_event_id: str
    source_agent: str
    session_id: str
    turn_number: int
    alias_identity_kind: str
    canonical_identity_kind: str
    alias_revision_id: str
    canonical_revision_id: str
    alias_content_hash: str
    canonical_content_hash: str
    alias_visible_hash: str
    canonical_visible_hash: str
    source_path_relation: str
    receipt_hash: str

    @property
    def visible_payload_equal(self) -> bool:
        return self.alias_visible_hash == self.canonical_visible_hash

    def insert_values(self, reconciled_at: str) -> tuple[Any, ...]:
        return (
            self.alias_event_id,
            self.canonical_event_id,
            self.source_agent,
            self.session_id,
            self.turn_number,
            self.alias_identity_kind,
            self.canonical_identity_kind,
            self.alias_revision_id,
            self.canonical_revision_id,
            self.alias_content_hash,
            self.canonical_content_hash,
            self.alias_visible_hash,
            self.canonical_visible_hash,
            int(self.visible_payload_equal),
            self.source_path_relation,
            self.receipt_hash,
            reconciled_at,
        )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def alias_table_exists(conn: sqlite3.Connection) -> bool:
    """Return whether this Raw database already has the alias projection table."""
    return _table_exists(conn, TABLE_NAME)


def _canonical_alias_lookup_query() -> str:
    """Return a query whose table identifier is this module's schema constant."""
    return f"SELECT canonical_event_id FROM {TABLE_NAME} WHERE alias_event_id=?"


def _alias_exists_query() -> str:
    """Return a fixed alias-membership query for a bound event identifier."""
    return f"SELECT 1 FROM {TABLE_NAME} WHERE alias_event_id=?"


def _unaliased_duplicate_rows_query(*, aliases_exist: bool) -> str:
    """Return one of two fixed Raw identity queries without caller SQL input."""
    alias_filter = (
        f"AND NOT EXISTS (SELECT 1 FROM {TABLE_NAME} a WHERE a.alias_event_id=t.event_id)"
        if aliases_exist
        else ""
    )
    same_turn_alias_filter = (
        f"AND NOT EXISTS (SELECT 1 FROM {TABLE_NAME} a "
        "WHERE a.alias_event_id=same_turn.event_id)"
        if aliases_exist
        else ""
    )
    return f"""
        SELECT t.event_id, t.source_agent, t.session_id, t.turn_number,
               t.current_revision_id, t.content_hash, t.metadata_json,
               t.source_path, r.snapshot_blob
        FROM raw_turns AS t
        JOIN raw_turn_revisions AS r ON r.revision_id=t.current_revision_id
        WHERE EXISTS (
            SELECT 1
            FROM raw_turns AS same_turn
            WHERE same_turn.source_agent=t.source_agent
              AND same_turn.session_id=t.session_id
              AND same_turn.turn_number=t.turn_number
              {same_turn_alias_filter}
            GROUP BY same_turn.source_agent, same_turn.session_id, same_turn.turn_number
            HAVING COUNT(*) > 1
        )
        {alias_filter}
        ORDER BY t.source_agent, t.session_id, t.turn_number, t.event_id
        """


def _alias_count_query() -> str:
    """Return the fixed count query for this module's alias schema."""
    return f"SELECT COUNT(*) FROM {TABLE_NAME}"


def _verify_alias_receipt_query() -> str:
    """Return the fixed post-insert receipt validation query."""
    return f"""
        SELECT alias_event_id, canonical_event_id, source_agent, session_id,
               turn_number, alias_identity_kind, canonical_identity_kind,
               alias_revision_id, canonical_revision_id, alias_content_hash,
               canonical_content_hash, alias_visible_hash, canonical_visible_hash,
               visible_payload_equal, source_path_relation, receipt_hash
        FROM {TABLE_NAME}
        WHERE alias_event_id=?
        """


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the append-only alias mapping schema owned by this module."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            alias_event_id TEXT PRIMARY KEY,
            canonical_event_id TEXT NOT NULL,
            source_agent TEXT NOT NULL,
            session_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL,
            alias_identity_kind TEXT NOT NULL,
            canonical_identity_kind TEXT NOT NULL,
            alias_revision_id TEXT NOT NULL,
            canonical_revision_id TEXT NOT NULL,
            alias_content_hash TEXT NOT NULL,
            canonical_content_hash TEXT NOT NULL,
            alias_visible_hash TEXT NOT NULL,
            canonical_visible_hash TEXT NOT NULL,
            visible_payload_equal INTEGER NOT NULL CHECK(visible_payload_equal IN (0, 1)),
            source_path_relation TEXT NOT NULL,
            receipt_hash TEXT NOT NULL UNIQUE,
            reconciled_at TEXT NOT NULL,
            CHECK(alias_event_id != canonical_event_id),
            FOREIGN KEY(alias_event_id) REFERENCES raw_turns(event_id) ON DELETE RESTRICT,
            FOREIGN KEY(canonical_event_id) REFERENCES raw_turns(event_id) ON DELETE RESTRICT
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_raw_event_identity_aliases_canonical
        ON {TABLE_NAME}(canonical_event_id)
        """
    )
    required = {
        "alias_event_id",
        "canonical_event_id",
        "source_agent",
        "session_id",
        "turn_number",
        "alias_identity_kind",
        "canonical_identity_kind",
        "alias_revision_id",
        "canonical_revision_id",
        "alias_content_hash",
        "canonical_content_hash",
        "alias_visible_hash",
        "canonical_visible_hash",
        "visible_payload_equal",
        "source_path_relation",
        "receipt_hash",
        "reconciled_at",
    }
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({TABLE_NAME!r})")}
    missing = sorted(required - columns)
    if missing:
        raise RawEventIdentityAliasError(
            f"{TABLE_NAME} is incompatible; missing columns: {missing}"
        )


def resolve_canonical_event_id(conn: sqlite3.Connection, event_id: str) -> str:
    """Resolve a logical alias without rewriting old immutable revision IDs."""
    if not event_id or not alias_table_exists(conn):
        return event_id
    current = event_id
    seen = {current}
    for _ in range(8):
        row = conn.execute(
            _canonical_alias_lookup_query(),
            (current,),
        ).fetchone()
        if row is None:
            return current
        current = str(row[0] or "")
        if not current or current in seen:
            raise RawEventIdentityAliasError("raw event identity alias cycle or empty target")
        seen.add(current)
    raise RawEventIdentityAliasError("raw event identity alias chain exceeds depth limit")


def is_alias_event(conn: sqlite3.Connection, event_id: str) -> bool:
    if not event_id or not alias_table_exists(conn):
        return False
    return (
        conn.execute(_alias_exists_query(), (event_id,)).fetchone()
        is not None
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _identity_kind(metadata: dict[str, Any]) -> str:
    value = metadata.get("logical_event_identity_kind")
    return str(value) if isinstance(value, str) and value else "missing"


def _visible_payload_hash(snapshot_blob: Any) -> str:
    try:
        payload = json.loads(zlib.decompress(snapshot_blob).decode("utf-8"))
    except (TypeError, ValueError, zlib.error, UnicodeDecodeError) as exc:
        raise RawEventIdentityAliasError(
            "current Raw revision snapshot is unreadable for identity reconciliation"
        ) from exc
    if not isinstance(payload, dict):
        raise RawEventIdentityAliasError(
            "current Raw revision snapshot is malformed for identity reconciliation"
        )
    return _sha256({field: payload.get(field) for field in _VISIBLE_FIELDS})


def _metadata(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RawEventIdentityAliasError(
            "Raw metadata is unreadable for identity reconciliation"
        ) from exc
    if not isinstance(value, dict):
        raise RawEventIdentityAliasError(
            "Raw metadata is malformed for identity reconciliation"
        )
    return value


def _source_path_relation(alias_path: str, canonical_path: str) -> str:
    if alias_path and canonical_path:
        return "same" if alias_path == canonical_path else "different"
    if alias_path:
        return "canonical_missing"
    if canonical_path:
        return "legacy_missing"
    return "both_missing"


def _identity_record(row: tuple[Any, ...]) -> dict[str, Any]:
    (
        event_id,
        source_agent,
        session_id,
        turn_number,
        current_revision_id,
        content_hash,
        metadata_json,
        source_path,
        snapshot_blob,
    ) = row
    values = {
        "event_id": str(event_id or ""),
        "source_agent": str(source_agent or ""),
        "session_id": str(session_id or ""),
        "turn_number": turn_number,
        "revision_id": str(current_revision_id or ""),
        "content_hash": str(content_hash or ""),
        "metadata": _metadata(metadata_json),
        "source_path": str(source_path or ""),
        "visible_hash": _visible_payload_hash(snapshot_blob),
    }
    if (
        not values["event_id"]
        or not values["source_agent"]
        or not values["session_id"]
        or not isinstance(values["turn_number"], int)
        or isinstance(values["turn_number"], bool)
        or not values["revision_id"]
        or not values["content_hash"]
    ):
        raise RawEventIdentityAliasError(
            "Raw row lacks the stable fields required for identity reconciliation"
        )
    values["identity_kind"] = _identity_kind(values["metadata"])
    return values


def _candidate_receipt_fields(
    legacy: dict[str, Any], canonical: dict[str, Any], source_path_relation: str
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "alias_event_id": legacy["event_id"],
        "canonical_event_id": canonical["event_id"],
        "source_agent": legacy["source_agent"],
        "session_id": legacy["session_id"],
        "turn_number": legacy["turn_number"],
        "alias_identity_kind": legacy["identity_kind"],
        "canonical_identity_kind": canonical["identity_kind"],
        "alias_revision_id": legacy["revision_id"],
        "canonical_revision_id": canonical["revision_id"],
        "alias_content_hash": legacy["content_hash"],
        "canonical_content_hash": canonical["content_hash"],
        "alias_visible_hash": legacy["visible_hash"],
        "canonical_visible_hash": canonical["visible_hash"],
        "source_path_relation": source_path_relation,
    }


def _candidate_from_group(records: list[dict[str, Any]]) -> tuple[IdentityAliasCandidate | None, str]:
    if len(records) != 2:
        return None, "group_not_one_legacy_one_native"
    legacy = [record for record in records if record["identity_kind"] in LEGACY_IDENTITY_KINDS]
    native = [record for record in records if record["identity_kind"] in NATIVE_IDENTITY_KINDS]
    if len(legacy) != 1 or len(native) != 1:
        return None, "group_not_one_legacy_one_native"
    alias = legacy[0]
    canonical = native[0]
    if not str(canonical["metadata"].get("logical_event_identity") or ""):
        return None, "canonical_identity_value_missing"
    relation = _source_path_relation(alias["source_path"], canonical["source_path"])
    receipt_fields = _candidate_receipt_fields(alias, canonical, relation)
    return (
        IdentityAliasCandidate(
            alias_event_id=alias["event_id"],
            canonical_event_id=canonical["event_id"],
            source_agent=alias["source_agent"],
            session_id=alias["session_id"],
            turn_number=alias["turn_number"],
            alias_identity_kind=alias["identity_kind"],
            canonical_identity_kind=canonical["identity_kind"],
            alias_revision_id=alias["revision_id"],
            canonical_revision_id=canonical["revision_id"],
            alias_content_hash=alias["content_hash"],
            canonical_content_hash=canonical["content_hash"],
            alias_visible_hash=alias["visible_hash"],
            canonical_visible_hash=canonical["visible_hash"],
            source_path_relation=relation,
            receipt_hash=_sha256(receipt_fields),
        ),
        "",
    )


def _unaliased_duplicate_rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    if not _table_exists(conn, "raw_turns") or not _table_exists(conn, "raw_turn_revisions"):
        return []
    return conn.execute(
        _unaliased_duplicate_rows_query(aliases_exist=alias_table_exists(conn))
    ).fetchall()


def _discover(conn: sqlite3.Connection) -> tuple[list[IdentityAliasCandidate], list[str]]:
    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for row in _unaliased_duplicate_rows(conn):
        record = _identity_record(row)
        key = (record["source_agent"], record["session_id"], record["turn_number"])
        grouped.setdefault(key, []).append(record)
    candidates: list[IdentityAliasCandidate] = []
    blocking: list[str] = []
    for key, records in sorted(grouped.items()):
        candidate, reason = _candidate_from_group(records)
        if candidate is None:
            blocking.append(f"{reason}:{key[0]}")
        else:
            candidates.append(candidate)
    return candidates, blocking


def _summary(
    *,
    db_path: Path,
    candidates: Iterable[IdentityAliasCandidate],
    blocking: Iterable[str],
    alias_count: int,
) -> dict[str, Any]:
    candidate_rows = list(candidates)
    blocking_rows = list(blocking)
    source_counts: dict[str, int] = {}
    source_visible_equal: dict[str, int] = {}
    source_path_relations: dict[str, dict[str, int]] = {}
    for candidate in candidate_rows:
        source_counts[candidate.source_agent] = source_counts.get(candidate.source_agent, 0) + 1
        source_visible_equal[candidate.source_agent] = source_visible_equal.get(
            candidate.source_agent, 0
        ) + int(candidate.visible_payload_equal)
        relation_counts = source_path_relations.setdefault(candidate.source_agent, {})
        relation_counts[candidate.source_path_relation] = (
            relation_counts.get(candidate.source_path_relation, 0) + 1
        )
    blocking_reason_counts: dict[str, int] = {}
    for item in blocking_rows:
        reason = item.split(":", 1)[0]
        blocking_reason_counts[reason] = blocking_reason_counts.get(reason, 0) + 1
    digest = hashlib.sha256(
        "\n".join(sorted(candidate.receipt_hash for candidate in candidate_rows)).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "db_path": str(db_path),
        "alias_count": alias_count,
        "candidate_count": len(candidate_rows),
        "candidate_visible_payload_equal_count": sum(
            int(candidate.visible_payload_equal) for candidate in candidate_rows
        ),
        "candidate_visible_payload_changed_count": sum(
            int(not candidate.visible_payload_equal) for candidate in candidate_rows
        ),
        "candidate_counts_by_source": dict(sorted(source_counts.items())),
        "candidate_visible_payload_equal_by_source": dict(sorted(source_visible_equal.items())),
        "candidate_source_path_relations": {
            source: dict(sorted(counts.items()))
            for source, counts in sorted(source_path_relations.items())
        },
        "candidate_receipt_hash": digest,
        "blocking_count": len(blocking_rows),
        "blocking_reason_counts": dict(sorted(blocking_reason_counts.items())),
        "ok": not candidate_rows and not blocking_rows,
        "status": "current" if not candidate_rows and not blocking_rows else "reconciliation_required",
    }


def inspect_reconciliation(db_path: Path | str) -> dict[str, Any]:
    """Read only the structural identity transition state; never write Raw."""
    path = Path(db_path).expanduser()
    try:
        path_kind = inspect_path_kind(path)
    except DurableIOError:
        raise RawEventIdentityAliasError(
            "identity alias database unavailable"
        ) from None
    if path_kind == "missing":
        return {
            "schema_version": SCHEMA_VERSION,
            "db_path": str(path),
            "alias_count": 0,
            "candidate_count": 0,
            "blocking_count": 0,
            "ok": True,
            "status": "uninitialized",
        }
    if path_kind != "file":
        raise RawEventIdentityAliasError(
            "identity alias database is not a regular file"
        )
    with connect_readonly_sqlite(path) as conn:
        alias_count = (
            int(conn.execute(_alias_count_query()).fetchone()[0])
            if alias_table_exists(conn)
            else 0
        )
        candidates, blocking = _discover(conn)
    return _summary(
        db_path=path,
        candidates=candidates,
        blocking=blocking,
        alias_count=alias_count,
    )


def _verify_inserted(conn: sqlite3.Connection, candidate: IdentityAliasCandidate) -> None:
    row = conn.execute(
        _verify_alias_receipt_query(),
        (candidate.alias_event_id,),
    ).fetchone()
    expected = candidate.insert_values("ignored")[:-1]
    if tuple(row or ()) != expected:
        raise RawEventIdentityAliasError("identity alias receipt collision")


def apply_reconciliation(db_path: Path | str) -> dict[str, Any]:
    """Atomically persist only proven historical-to-native aliases."""
    path = Path(db_path).expanduser()
    try:
        path_kind = inspect_path_kind(path)
    except DurableIOError:
        raise RawEventIdentityAliasError(
            "identity alias database unavailable"
        ) from None
    if path_kind == "missing":
        return inspect_reconciliation(path)
    if path_kind != "file":
        raise RawEventIdentityAliasError(
            "identity alias database is not a regular file"
        )
    conn = sqlite3.connect(str(path))
    applied_count = 0
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        ensure_schema(conn)
        candidates, blocking = _discover(conn)
        if blocking:
            raise RawEventIdentityAliasError(
                "identity alias reconciliation is ambiguous: "
                + ", ".join(sorted(set(blocking))[:5])
            )
        reconciled_at = datetime.now(timezone.utc).isoformat()
        for candidate in candidates:
            inserted = conn.execute(
                f"""
                INSERT OR IGNORE INTO {TABLE_NAME} (
                    alias_event_id, canonical_event_id, source_agent, session_id,
                    turn_number, alias_identity_kind, canonical_identity_kind,
                    alias_revision_id, canonical_revision_id, alias_content_hash,
                    canonical_content_hash, alias_visible_hash, canonical_visible_hash,
                    visible_payload_equal, source_path_relation, receipt_hash, reconciled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                candidate.insert_values(reconciled_at),
            )
            applied_count += int(inserted.rowcount or 0)
            _verify_inserted(conn, candidate)
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RawEventIdentityAliasError(
                "identity alias reconciliation left foreign-key violations"
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    result = inspect_reconciliation(path)
    if not result["ok"]:
        raise RawEventIdentityAliasError(
            f"identity alias reconciliation did not converge: {result}"
        )
    result["applied_count"] = applied_count
    return result
