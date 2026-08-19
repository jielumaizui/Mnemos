"""Explicit migration for legacy Raw turn-number uniqueness."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.ops.durable_io import DurableIOError, inspect_path_kind
from core.ops.readiness_query_budget import connect_readonly_sqlite


class RawEventIdentitySchemaMigrationError(RuntimeError):
    """Raised when an identity-schema migration cannot prove a safe result."""


_RAW_TURNS_COLUMNS = (
    "event_id",
    "current_revision_id",
    "source_agent",
    "session_id",
    "turn_number",
    "model_tag",
    "conversation_at",
    "captured_at",
    "origin",
    "source_path",
    "source_files_json",
    "content_hash",
    "full_content_hash",
    "completeness_status",
    "completeness_json",
    "metadata_json",
    "tool_calls_json",
    "tool_results_json",
    "attachments_json",
    "raw_event_refs_json",
    "reasoning_blob",
    "user_content_blob",
    "assistant_content_blob",
    "compression",
    "raw_bytes",
    "quality_rank",
    "updated_at",
)

_ORPHAN_PROVENANCE_GAP_REASON = "orphan_raw_revision_edge_unprovable"
_ORPHAN_RECEIPTS_TABLE = "raw_provenance_orphan_receipts"


def _orphan_receipt_lookup_query() -> str:
    """Return the fixed receipt lookup for the module-owned schema table."""
    return f"""
        SELECT edge_id, source_revision_id, span_start, span_end,
               consumer_type, consumer_id, original_created_at, reason
        FROM {_ORPHAN_RECEIPTS_TABLE}
        WHERE edge_id=?
        """


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _require_columns(
    conn: sqlite3.Connection,
    table: str,
    required: tuple[str, ...],
) -> None:
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table!r})").fetchall()
    }
    missing = [column for column in required if column not in columns]
    if missing:
        raise RawEventIdentitySchemaMigrationError(
            f"{table} is too old for identity migration; missing columns: {missing}"
        )


def _orphan_provenance_rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    tables = _table_names(conn)
    if "raw_provenance_edges" not in tables:
        return []
    if "raw_turn_revisions" not in tables:
        raise RawEventIdentitySchemaMigrationError(
            "raw_provenance_edges exists but raw_turn_revisions is missing"
        )
    _require_columns(
        conn,
        "raw_provenance_edges",
        (
            "edge_id",
            "source_revision_id",
            "span_start",
            "span_end",
            "consumer_type",
            "consumer_id",
            "created_at",
        ),
    )
    return conn.execute(
        """
        SELECT e.edge_id, e.source_revision_id, e.span_start, e.span_end,
               e.consumer_type, e.consumer_id, e.created_at
        FROM raw_provenance_edges AS e
        LEFT JOIN raw_turn_revisions AS r ON r.revision_id=e.source_revision_id
        WHERE r.revision_id IS NULL
        ORDER BY e.edge_id
        """
    ).fetchall()


def _foreign_key_violations(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]


def _orphan_receipt_id(edge_id: str) -> str:
    return "raworphan-" + hashlib.sha256(edge_id.encode("utf-8")).hexdigest()[:40]


def _provenance_gap_id(consumer_type: str, consumer_id: str) -> str:
    raw = f"{consumer_type}\0{consumer_id}\0{_ORPHAN_PROVENANCE_GAP_REASON}"
    return "rawgap-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _validate_orphan_provenance_row(row: tuple[Any, ...]) -> tuple[str, str, int, int, str, str, str]:
    edge_id, source_revision_id, span_start, span_end, consumer_type, consumer_id, created_at = row
    text_fields = (edge_id, source_revision_id, consumer_type, consumer_id, created_at)
    if any(not isinstance(value, str) or not value for value in text_fields):
        raise RawEventIdentitySchemaMigrationError(
            "orphan provenance edge lacks a stable metadata identity"
        )
    if (
        not isinstance(span_start, int)
        or isinstance(span_start, bool)
        or not isinstance(span_end, int)
        or isinstance(span_end, bool)
        or span_start < 0
        or span_end <= span_start
    ):
        raise RawEventIdentitySchemaMigrationError(
            "orphan provenance edge has an invalid source span"
        )
    return (
        edge_id,
        source_revision_id,
        span_start,
        span_end,
        consumer_type,
        consumer_id,
        created_at,
    )


def _ensure_orphan_receipt_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_ORPHAN_RECEIPTS_TABLE} (
            receipt_id TEXT PRIMARY KEY,
            edge_id TEXT NOT NULL UNIQUE,
            source_revision_id TEXT NOT NULL,
            span_start INTEGER NOT NULL,
            span_end INTEGER NOT NULL,
            consumer_type TEXT NOT NULL,
            consumer_id TEXT NOT NULL,
            original_created_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            reconciled_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_raw_provenance_orphan_consumer
        ON {_ORPHAN_RECEIPTS_TABLE}(consumer_type, consumer_id)
        """
    )
    _require_columns(
        conn,
        _ORPHAN_RECEIPTS_TABLE,
        (
            "receipt_id",
            "edge_id",
            "source_revision_id",
            "span_start",
            "span_end",
            "consumer_type",
            "consumer_id",
            "original_created_at",
            "reason",
            "reconciled_at",
        ),
    )


def _reconcile_orphan_provenance_edges(conn: sqlite3.Connection) -> int:
    rows = [_validate_orphan_provenance_row(row) for row in _orphan_provenance_rows(conn)]
    if not rows:
        return 0
    if "raw_provenance_gaps" not in _table_names(conn):
        raise RawEventIdentitySchemaMigrationError(
            "orphan provenance edges require raw_provenance_gaps before migration"
        )
    _require_columns(
        conn,
        "raw_provenance_gaps",
        (
            "gap_id",
            "consumer_type",
            "consumer_id",
            "source_agent",
            "session_id",
            "reason",
            "status",
            "created_at",
            "resolved_at",
        ),
    )
    _ensure_orphan_receipt_schema(conn)
    reconciled_at = datetime.now(timezone.utc).isoformat()
    for row in rows:
        (
            edge_id,
            source_revision_id,
            span_start,
            span_end,
            consumer_type,
            consumer_id,
            created_at,
        ) = row
        conn.execute(
            f"""
            INSERT OR IGNORE INTO {_ORPHAN_RECEIPTS_TABLE} (
                receipt_id, edge_id, source_revision_id, span_start, span_end,
                consumer_type, consumer_id, original_created_at, reason, reconciled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _orphan_receipt_id(edge_id),
                edge_id,
                source_revision_id,
                span_start,
                span_end,
                consumer_type,
                consumer_id,
                created_at,
                _ORPHAN_PROVENANCE_GAP_REASON,
                reconciled_at,
            ),
        )
        stored = conn.execute(
            _orphan_receipt_lookup_query(),
            (edge_id,),
        ).fetchone()
        expected = (
            edge_id,
            source_revision_id,
            span_start,
            span_end,
            consumer_type,
            consumer_id,
            created_at,
            _ORPHAN_PROVENANCE_GAP_REASON,
        )
        if tuple(stored or ()) != expected:
            raise RawEventIdentitySchemaMigrationError(
                "orphan provenance receipt identity collision"
            )
        conn.execute(
            """
            INSERT INTO raw_provenance_gaps (
                gap_id, consumer_type, consumer_id, source_agent, session_id,
                reason, status, created_at, resolved_at
            ) VALUES (?, ?, ?, '', '', ?, 'pending_rebuild', ?, NULL)
            ON CONFLICT(consumer_type, consumer_id, reason) DO UPDATE SET
                status='pending_rebuild', resolved_at=NULL
            """,
            (
                _provenance_gap_id(consumer_type, consumer_id),
                consumer_type,
                consumer_id,
                _ORPHAN_PROVENANCE_GAP_REASON,
                reconciled_at,
            ),
        )
        deleted = conn.execute(
            "DELETE FROM raw_provenance_edges WHERE edge_id=?", (edge_id,)
        ).rowcount
        if int(deleted or 0) != 1:
            raise RawEventIdentitySchemaMigrationError(
                "orphan provenance edge disappeared during reconciliation"
            )
    if _orphan_provenance_rows(conn):
        raise RawEventIdentitySchemaMigrationError(
            "orphan provenance reconciliation did not converge"
        )
    return len(rows)


def historical_turn_uniqueness(conn: sqlite3.Connection) -> bool:
    for index in conn.execute("PRAGMA index_list(raw_turns)").fetchall():
        if len(index) < 3 or not int(index[2]):
            continue
        name = str(index[1])
        columns = tuple(
            str(row[2]) for row in conn.execute(f"PRAGMA index_info({name!r})").fetchall()
        )
        if columns == ("source_agent", "session_id", "turn_number"):
            return True
    return False


def inspect(db_path: Path | str) -> dict[str, Any]:
    path = Path(db_path).expanduser()
    try:
        path_kind = inspect_path_kind(path)
    except DurableIOError:
        return {
            "status": "unreadable",
            "db_path": str(path),
            "error": "raw_identity_path_unavailable",
        }
    if path_kind == "missing":
        return {"status": "uninitialized", "db_path": str(path)}
    if path_kind != "file":
        return {
            "status": "unreadable",
            "db_path": str(path),
            "error": "raw_identity_path_not_regular",
        }
    wal_path = Path(f"{path}-wal")
    try:
        wal_kind = inspect_path_kind(wal_path)
        if wal_kind not in {"missing", "file"}:
            return {
                "status": "unreadable",
                "db_path": str(path),
                "error": "raw_identity_wal_not_regular",
            }
        live_wal = wal_kind == "file" and wal_path.stat().st_size > 0
    except (DurableIOError, OSError):
        return {
            "status": "unreadable",
            "db_path": str(path),
            "error": "raw_identity_wal_unavailable",
        }
    try:
        conn = connect_readonly_sqlite(
            path,
            immutable=not live_wal,
        )
    except (OSError, sqlite3.Error):
        return {
            "status": "unreadable",
            "db_path": str(path),
            "error": "raw_identity_database_unreadable",
        }
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_turns'"
        ).fetchone()
        if not table:
            return {"status": "uninitialized", "db_path": str(path)}
        orphan_rows = _orphan_provenance_rows(conn)
        foreign_key_violations = _foreign_key_violations(conn)
        legacy_identity = historical_turn_uniqueness(conn)
        return {
            "status": (
                "migration_required"
                if legacy_identity or foreign_key_violations
                else "current"
            ),
            "db_path": str(path),
            "legacy_turn_number_unique": legacy_identity,
            "raw_turn_count": int(conn.execute("SELECT COUNT(*) FROM raw_turns").fetchone()[0]),
            "orphan_provenance_edges": len(orphan_rows),
            "foreign_key_violation_count": len(foreign_key_violations),
        }
    except sqlite3.Error:
        return {
            "status": "unreadable",
            "db_path": str(path),
            "error": "raw_identity_schema_unreadable",
        }
    finally:
        conn.close()


def apply(db_path: Path | str) -> dict[str, Any]:
    """Rebuild Raw identity while preserving irrecoverable provenance as gaps."""
    path = Path(db_path).expanduser()
    before = inspect(path)
    if before["status"] == "uninitialized":
        return before
    if before["status"] == "current":
        return before
    if before["status"] != "migration_required":
        raise RawEventIdentitySchemaMigrationError(
            "raw event identity schema is unavailable"
        )
    conn = sqlite3.connect(str(path))
    orphan_reconciled = 0
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        orphan_reconciled = _reconcile_orphan_provenance_edges(conn)
        remaining_foreign_key_violations = _foreign_key_violations(conn)
        if remaining_foreign_key_violations:
            raise RawEventIdentitySchemaMigrationError(
                "raw event identity migration found foreign-key violations outside "
                "reconcilable orphan provenance edges"
            )
        if before["legacy_turn_number_unique"]:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(raw_turns)")}
            if "current_revision_id" not in columns:
                conn.execute("ALTER TABLE raw_turns ADD COLUMN current_revision_id TEXT")
                columns.add("current_revision_id")
            missing = [name for name in _RAW_TURNS_COLUMNS if name not in columns]
            if missing:
                raise RawEventIdentitySchemaMigrationError(
                    f"raw_turns is too old for identity migration; missing columns: {missing}"
                )
            before_count = int(conn.execute("SELECT COUNT(*) FROM raw_turns").fetchone()[0])
            conn.execute(
                """
                CREATE TABLE raw_turns_identity_v2 (
                    event_id TEXT PRIMARY KEY,
                    current_revision_id TEXT,
                    source_agent TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    turn_number INTEGER NOT NULL,
                    model_tag TEXT,
                    conversation_at TEXT,
                    captured_at TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    source_path TEXT,
                    source_files_json TEXT,
                    content_hash TEXT NOT NULL,
                    full_content_hash TEXT,
                    completeness_status TEXT NOT NULL,
                    completeness_json TEXT,
                    metadata_json TEXT,
                    tool_calls_json TEXT,
                    tool_results_json TEXT,
                    attachments_json TEXT,
                    raw_event_refs_json TEXT,
                    reasoning_blob BLOB,
                    user_content_blob BLOB NOT NULL,
                    assistant_content_blob BLOB NOT NULL,
                    compression TEXT NOT NULL DEFAULT 'zlib',
                    raw_bytes INTEGER NOT NULL DEFAULT 0,
                    quality_rank INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns_sql = ", ".join(_RAW_TURNS_COLUMNS)
            conn.execute(
                f"INSERT INTO raw_turns_identity_v2 ({columns_sql}) "
                f"SELECT {columns_sql} FROM raw_turns"  # nosec B608 - fixed internal column contract.
            )
            copied = int(conn.execute("SELECT COUNT(*) FROM raw_turns_identity_v2").fetchone()[0])
            if copied != before_count:
                raise RawEventIdentitySchemaMigrationError(
                    f"raw_turns migration row count mismatch: {before_count} != {copied}"
                )
            conn.execute("DROP TABLE raw_turns")
            conn.execute("ALTER TABLE raw_turns_identity_v2 RENAME TO raw_turns")
            conn.execute(
                "CREATE INDEX idx_raw_turns_source_session ON raw_turns(source_agent, session_id)"
            )
            conn.execute(
                "CREATE INDEX idx_raw_turns_source_session_turn "
                "ON raw_turns(source_agent, session_id, turn_number)"
            )
            conn.execute("CREATE INDEX idx_raw_turns_status ON raw_turns(completeness_status)")
        foreign_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_errors:
            raise RawEventIdentitySchemaMigrationError(
                f"foreign key validation failed after migration: {foreign_errors[:3]}"
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    result = inspect(path)
    if result["status"] != "current":
        raise RawEventIdentitySchemaMigrationError(f"identity migration did not converge: {result}")
    result["orphan_provenance_reconciled"] = orphan_reconciled
    return result
