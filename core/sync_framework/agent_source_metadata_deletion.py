"""Physical deletion owner for provenance-addressable SyncEngine metadata.

``sync_log`` and ``user_signals`` are not canonical Raw: they are local
deduplication and behavioral metadata keyed by agent and session.  This owner
deletes only the scopes those tables can prove from their headers.  It never
guesses that an aggregate ``sync_audit`` row belongs to a session: a session
request deletes the exact rows it can identify, records the after-oracle, and
remains unverified while that aggregate is unresolved.

The receipt keeps scope hashes and counts only.  It must not become another
copy of an agent, session, artifact path, or source body.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.db_utils import render_sql
from core.ops.durable_io import inspect_path_kind


AGENT_SOURCE_METADATA_DELETION_SCHEMA_VERSION = "mnemos.agent_source_metadata_deletion.v1"
AGENT_SOURCE_METADATA_DELETION_TABLE = "agent_source_metadata_deletion_receipts"
_CURRENT_TABLES = frozenset({"sync_log", "user_signals", "sync_audit"})
_SUPPORTED_SCOPES = frozenset({"all", "agent", "session"})
_PREDICATE_COLUMNS = frozenset({"agent_name", "session_id", "agent", "source"})
_PREDICATE_CONTRACT = frozenset(
    {"1=1", "0=1"}
    | {f"{column}=?" for column in _PREDICATE_COLUMNS}
    | {f"LOWER({column})=?" for column in _PREDICATE_COLUMNS}
)
_REQUIRED_COLUMNS = {
    "sync_log": frozenset({"agent_name", "session_id"}),
    "user_signals": frozenset({"agent", "session_id"}),
    "sync_audit": frozenset({"source"}),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope_hash(scope_kind: str, scope_value: str) -> str:
    material = f"{str(scope_kind).strip().lower()}:{str(scope_value).strip()}"
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _receipt_id(*, request_id: str, scope_kind: str, scope_value_hash: str) -> str:
    material = "|".join((str(request_id), str(scope_kind), str(scope_value_hash)))
    return "source-metadata-delete-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if str(row[0]) != "sqlite_sequence"
    }


def _has_required_columns(conn: sqlite3.Connection, table_name: str, columns: frozenset[str]) -> bool:
    actual = {
        str(row[1])
        for row in conn.execute(
            render_sql(
                "PRAGMA table_info({table})",
                identifiers={"table": table_name},
            )
        )
    }
    return columns <= actual


def _ensure_receipt_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        render_sql(
            """
        CREATE TABLE IF NOT EXISTS {table} (
            receipt_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            request_id TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_value_hash TEXT NOT NULL,
            sync_log_deleted INTEGER NOT NULL,
            user_signals_deleted INTEGER NOT NULL,
            sync_audit_deleted INTEGER NOT NULL,
            after_count INTEGER NOT NULL,
            unresolved_sync_audit_count INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('planned', 'flushed', 'applied')),
            created_at TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT '',
            UNIQUE(request_id, scope_kind, scope_value_hash)
        )
        """,
            identifiers={"table": AGENT_SOURCE_METADATA_DELETION_TABLE},
        )
    )
    conn.execute(
        render_sql(
            """
        CREATE INDEX IF NOT EXISTS idx_agent_source_metadata_deletion_scope
        ON {table}(scope_kind, scope_value_hash, status)
        """,
            identifiers={"table": AGENT_SOURCE_METADATA_DELETION_TABLE},
        )
    )
    conn.execute(
        render_sql(
            """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_source_metadata_delete_pending_scope
        ON {table}(scope_kind, scope_value_hash)
        WHERE status IN ('planned', 'flushed')
        """,
            identifiers={"table": AGENT_SOURCE_METADATA_DELETION_TABLE},
        )
    )


def _predicate(*, scope_kind: str, scope_value: str, column: str) -> tuple[str, tuple[str, ...]]:
    if column not in _PREDICATE_COLUMNS:
        raise ValueError(f"unsupported source metadata predicate column: {column}")
    if scope_kind == "all":
        return "1=1", ()
    if scope_kind == "agent":
        # Agent source names are identifiers, not presentation strings.  The
        # ownership-freeze boundary normalizes them case-insensitively too.
        return f"LOWER({column})=?", (scope_value.lower(),)
    return f"{column}=?", (scope_value,)


def _count(conn: sqlite3.Connection, table_name: str, predicate: str, params: tuple[str, ...]) -> int:
    row = conn.execute(
        render_sql(
            "SELECT COUNT(*) FROM {table} WHERE {predicate}",
            identifiers={"table": table_name},
            fixed_fragments={"predicate": (predicate, _PREDICATE_CONTRACT)},
        ),
        params,
    ).fetchone()
    return int(row[0] or 0) if row is not None else 0


def _receipt_schema_compatible(conn: sqlite3.Connection) -> bool:
    """Reject an old receipt table rather than treating it as checkpoint-safe."""

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (AGENT_SOURCE_METADATA_DELETION_TABLE,),
    ).fetchone()
    if row is None:
        return False
    normalized = "".join(str(row[0] or "").lower().split())
    pending_index = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        ("idx_agent_source_metadata_delete_pending_scope",),
    ).fetchone()
    return (
        "check(statusin('planned','flushed','applied'))" in normalized
        and pending_index is not None
    )


def _receipt_state(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    scope_kind: str,
    scope_value_hash: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        render_sql(
            """
        SELECT receipt_id, status, sync_log_deleted, user_signals_deleted,
               sync_audit_deleted, after_count, unresolved_sync_audit_count
        FROM {table}
        WHERE request_id=? AND scope_kind=? AND scope_value_hash=?
        """,
            identifiers={"table": AGENT_SOURCE_METADATA_DELETION_TABLE},
        ),
        (request_id, scope_kind, scope_value_hash),
    ).fetchone()
    if row is None:
        return None
    return {
        "receipt_id": str(row[0]),
        "receipt_status": str(row[1]),
        "sync_log_deleted": int(row[2] or 0),
        "user_signals_deleted": int(row[3] or 0),
        "sync_audit_deleted": int(row[4] or 0),
        "after_count": int(row[5] or 0),
        "unresolved_sync_audit_count": int(row[6] or 0),
    }


def _pending_scope_receipt(
    conn: sqlite3.Connection,
    *,
    scope_kind: str,
    scope_value_hash: str,
) -> dict[str, Any] | None:
    """Return the one non-terminal receipt that must be resumed first."""

    row = conn.execute(
        render_sql(
            """
        SELECT receipt_id, status, sync_log_deleted, user_signals_deleted,
               sync_audit_deleted, after_count, unresolved_sync_audit_count
        FROM {table}
        WHERE scope_kind=? AND scope_value_hash=? AND status IN ('planned', 'flushed')
        ORDER BY created_at ASC
        LIMIT 1
        """,
            identifiers={"table": AGENT_SOURCE_METADATA_DELETION_TABLE},
        ),
        (scope_kind, scope_value_hash),
    ).fetchone()
    if row is None:
        return None
    return {
        "receipt_id": str(row[0]),
        "receipt_status": str(row[1]),
        "sync_log_deleted": int(row[2] or 0),
        "user_signals_deleted": int(row[3] or 0),
        "sync_audit_deleted": int(row[4] or 0),
        "after_count": int(row[5] or 0),
        "unresolved_sync_audit_count": int(row[6] or 0),
    }


def _result(
    *,
    status: str,
    sync_log_deleted: int,
    user_signals_deleted: int,
    sync_audit_deleted: int,
    after_count: int,
    unresolved_sync_audit_count: int,
) -> dict[str, Any]:
    return {
        "status": status,
        "target_count": sync_log_deleted + user_signals_deleted + sync_audit_deleted,
        "receipt_count": 1,
        "sync_log_deleted": sync_log_deleted,
        "user_signals_deleted": user_signals_deleted,
        "sync_audit_deleted": sync_audit_deleted,
        "after_count": after_count,
        "unresolved_sync_audit_count": unresolved_sync_audit_count,
        "verified": (
            status in {"applied", "existing"}
            and after_count == 0
            and unresolved_sync_audit_count == 0
        ),
    }


def _blocked(error: str) -> dict[str, Any]:
    return {
        "status": "blocked",
        "target_count": 0,
        "receipt_count": 0,
        "verified": False,
        "error": error,
    }


def delete_agent_source_metadata_subject_scope(
    *,
    db_path: Path | str,
    request_id: str,
    scope_kind: str,
    scope_value: str,
) -> dict[str, Any]:
    """Delete provenance-addressable SyncEngine metadata with a typed receipt.

    ``agent`` and ``session`` are the only safe scoped selectors. A historical
    table or incomplete current schema blocks before mutation; this prevents a
    new owner from falsely certifying unknown historical source metadata.
    """

    database = Path(db_path).expanduser()
    kind = str(scope_kind or "").strip().lower()
    value = str(scope_value or "").strip()
    if kind not in _SUPPORTED_SCOPES or (kind == "all" and value != "all"):
        return {
            "status": "unsupported_scope",
            "target_count": 0,
            "receipt_count": 0,
            "verified": False,
            "supported_scopes": sorted(_SUPPORTED_SCOPES),
        }
    if not str(request_id or "").strip() or not value:
        raise ValueError("agent source metadata deletion requires request_id and scope_value")
    database_kind = inspect_path_kind(database)
    if database_kind == "missing":
        return {
            "status": "not_initialized",
            "target_count": 0,
            "receipt_count": 0,
            "verified": True,
        }
    if database_kind != "file":
        raise RuntimeError("agent_source_metadata_store_not_regular")

    normalized_value = value.lower() if kind == "agent" else value
    scope_value_hash = _scope_hash(kind, normalized_value)
    receipt_id = _receipt_id(
        request_id=request_id,
        scope_kind=kind,
        scope_value_hash=scope_value_hash,
    )
    sync_log_predicate, sync_log_params = _predicate(
        scope_kind=kind,
        scope_value=normalized_value,
        column="agent_name" if kind == "agent" else "session_id",
    )
    user_signals_predicate, user_signals_params = _predicate(
        scope_kind=kind,
        scope_value=normalized_value,
        column="agent" if kind == "agent" else "session_id",
    )
    sync_audit_params: tuple[str, ...]
    if kind == "session":
        sync_audit_predicate, sync_audit_params = "0=1", ()
    else:
        sync_audit_predicate, sync_audit_params = _predicate(
            scope_kind=kind,
            scope_value=normalized_value,
            column="source",
        )

    def after_oracle(conn: sqlite3.Connection) -> tuple[int, int]:
        after_count = (
            _count(conn, "sync_log", sync_log_predicate, sync_log_params)
            + _count(conn, "user_signals", user_signals_predicate, user_signals_params)
            + _count(conn, "sync_audit", sync_audit_predicate, sync_audit_params)
        )
        unresolved_audit_count = (
            _count(conn, "sync_audit", "1=1", ()) if kind == "session" else 0
        )
        return after_count, unresolved_audit_count

    try:
        with sqlite3.connect(str(database), timeout=30) as conn:
            tables = _table_names(conn)
            unknown_tables = tables - _CURRENT_TABLES - {AGENT_SOURCE_METADATA_DELETION_TABLE}
            if unknown_tables:
                return _blocked("unknown_source_metadata_tables")
            if not _CURRENT_TABLES <= tables:
                return _blocked("agent_source_metadata_schema_incomplete")
            if any(
                not _has_required_columns(conn, table_name, columns)
                for table_name, columns in _REQUIRED_COLUMNS.items()
            ):
                return _blocked("agent_source_metadata_schema_incomplete")

            conn.execute("BEGIN IMMEDIATE")
            _ensure_receipt_schema(conn)
            if not _receipt_schema_compatible(conn):
                conn.rollback()
                return _blocked("agent_source_metadata_receipt_schema_incompatible")
            conn.commit()
            requested_receipt = _receipt_state(
                conn,
                request_id=request_id,
                scope_kind=kind,
                scope_value_hash=scope_value_hash,
            )
            # DataOwnership creates a fresh request ID on each retry.  A
            # prior delete that committed its physical effect but has not yet
            # checkpointed must therefore be resumed by scope, never bypassed
            # with a new zero-target receipt.
            existing = _pending_scope_receipt(
                conn,
                scope_kind=kind,
                scope_value_hash=scope_value_hash,
            ) or requested_receipt
            if existing is not None:
                receipt_id = str(existing["receipt_id"])
            if existing is not None and existing["receipt_status"] == "applied":
                after_count, unresolved_sync_audit_count = after_oracle(conn)
                if after_count:
                    return _blocked("agent_source_metadata_after_oracle_nonzero")
                return _result(
                    status="existing",
                    sync_log_deleted=existing["sync_log_deleted"],
                    user_signals_deleted=existing["user_signals_deleted"],
                    sync_audit_deleted=existing["sync_audit_deleted"],
                    after_count=after_count,
                    unresolved_sync_audit_count=unresolved_sync_audit_count,
                )

            if existing is None:
                conn.execute("PRAGMA secure_delete=ON")
                secure_delete = conn.execute("PRAGMA secure_delete").fetchone()
                if not secure_delete or int(secure_delete[0] or 0) < 1:
                    return _blocked("agent_source_metadata_secure_delete_unavailable")
                conn.execute("BEGIN IMMEDIATE")
                sync_log_deleted = int(
                    conn.execute(
                        render_sql(
                            "DELETE FROM sync_log WHERE {predicate}",
                            fixed_fragments={
                                "predicate": (
                                    sync_log_predicate,
                                    _PREDICATE_CONTRACT,
                                )
                            },
                        ),
                        sync_log_params,
                    ).rowcount
                    or 0
                )
                user_signals_deleted = int(
                    conn.execute(
                        render_sql(
                            "DELETE FROM user_signals WHERE {predicate}",
                            fixed_fragments={
                                "predicate": (
                                    user_signals_predicate,
                                    _PREDICATE_CONTRACT,
                                )
                            },
                        ),
                        user_signals_params,
                    ).rowcount
                    or 0
                )
                sync_audit_deleted = int(
                    conn.execute(
                        render_sql(
                            "DELETE FROM sync_audit WHERE {predicate}",
                            fixed_fragments={
                                "predicate": (
                                    sync_audit_predicate,
                                    _PREDICATE_CONTRACT,
                                )
                            },
                        ),
                        sync_audit_params,
                    ).rowcount
                    or 0
                )
                after_count, unresolved_sync_audit_count = after_oracle(conn)
                if after_count:
                    conn.rollback()
                    return _blocked("agent_source_metadata_after_oracle_nonzero")
                now = _now()
                conn.execute(
                    render_sql(
                        """
                    INSERT INTO {table} (
                        receipt_id, schema_version, request_id, scope_kind, scope_value_hash,
                        sync_log_deleted, user_signals_deleted, sync_audit_deleted,
                        after_count, unresolved_sync_audit_count, status, created_at, applied_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'flushed', ?, '')
                    """,
                        identifiers={
                            "table": AGENT_SOURCE_METADATA_DELETION_TABLE
                        },
                    ),
                    (
                        receipt_id,
                        AGENT_SOURCE_METADATA_DELETION_SCHEMA_VERSION,
                        request_id,
                        kind,
                        scope_value_hash,
                        sync_log_deleted,
                        user_signals_deleted,
                        sync_audit_deleted,
                        after_count,
                        unresolved_sync_audit_count,
                        now,
                    ),
                )
                conn.commit()
            else:
                sync_log_deleted = int(existing["sync_log_deleted"])
                user_signals_deleted = int(existing["user_signals_deleted"])
                sync_audit_deleted = int(existing["sync_audit_deleted"])
                after_count, unresolved_sync_audit_count = after_oracle(conn)
                if after_count:
                    return _blocked("agent_source_metadata_after_oracle_nonzero")
    except (sqlite3.Error, OSError, ValueError):
        return _blocked("agent_source_metadata_delete_failed")

    # SQLite WAL can retain deleted bytes until checkpointed.  The typed
    # receipt becomes terminal only after a fresh connection observes both a
    # non-busy checkpoint and a zero after-oracle.
    try:
        with sqlite3.connect(str(database), timeout=10) as conn:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0] or 0) != 0:
                return _result(
                    status="pending_checkpoint",
                    sync_log_deleted=sync_log_deleted,
                    user_signals_deleted=user_signals_deleted,
                    sync_audit_deleted=sync_audit_deleted,
                    after_count=after_count,
                    unresolved_sync_audit_count=unresolved_sync_audit_count,
                )
            after_count, unresolved_sync_audit_count = after_oracle(conn)
            if after_count:
                return _blocked("agent_source_metadata_post_checkpoint_oracle_nonzero")
            updated = conn.execute(
                render_sql(
                    """
                UPDATE {table}
                SET status='applied', after_count=?, unresolved_sync_audit_count=?, applied_at=?
                WHERE receipt_id=? AND status IN ('planned', 'flushed')
                """,
                    identifiers={"table": AGENT_SOURCE_METADATA_DELETION_TABLE},
                ),
                (after_count, unresolved_sync_audit_count, _now(), receipt_id),
            ).rowcount
            if not updated:
                return _blocked("agent_source_metadata_receipt_transition_failed")
            conn.commit()
    except (sqlite3.Error, OSError, ValueError):
        return _result(
            status="pending_checkpoint",
            sync_log_deleted=sync_log_deleted,
            user_signals_deleted=user_signals_deleted,
            sync_audit_deleted=sync_audit_deleted,
            after_count=after_count,
            unresolved_sync_audit_count=unresolved_sync_audit_count,
        )

    return _result(
        status="applied",
        sync_log_deleted=sync_log_deleted,
        user_signals_deleted=user_signals_deleted,
        sync_audit_deleted=sync_audit_deleted,
        after_count=after_count,
        unresolved_sync_audit_count=unresolved_sync_audit_count,
    )
