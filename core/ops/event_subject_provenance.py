"""Object provenance and physical deletion owner for EventBus payloads."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from core.db_utils import render_sql
from core.privacy.object_provenance import (
    ObjectProvenance,
    ObjectProvenanceError,
    TRACKED_PROVENANCE_STATE,
    UNATTRIBUTED_PROVENANCE_STATE,
    normalize_scope_selector,
    scope_selector_hash,
)


EVENT_SUBJECT_PROVENANCE_SCHEMA_VERSION = "mnemos.event_subject_provenance.v1"
PROVENANCE_TABLE = "event_object_provenance"
LINK_TABLE = "event_subject_links"
TOMBSTONE_TABLE = "event_subject_tombstones"
RECEIPT_TABLE = "event_subject_deletion_receipts"
_REQUIRED_TABLES = frozenset(
    {"events", "dead_letters", "handler_receipts", "event_trace_claims", "event_deferred_keys"}
)
_RECEIPT_FIELDS = (
    "receipt_id",
    "status",
    "target_count",
    "events_deleted",
    "dead_letters_deleted",
    "handler_receipts_deleted",
    "trace_claims_deleted",
    "deferred_keys_deleted",
    "unresolved_legacy_count",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _receipt_id(request_id: str, scope_kind: str, selector_hash: str) -> str:
    material = "|".join((request_id, scope_kind, selector_hash))
    return "event-delete-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def ensure_event_subject_provenance_schema(conn: sqlite3.Connection) -> None:
    """Create sidecars without committing a caller's event transaction."""

    script = f"""
        CREATE TABLE IF NOT EXISTS {PROVENANCE_TABLE} (
            trace_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('tracked', 'unattributed')),
            access_json TEXT NOT NULL DEFAULT '',
            access_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {LINK_TABLE} (
            trace_id TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_value_hash TEXT NOT NULL,
            PRIMARY KEY(trace_id, scope_kind, scope_value_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_event_subject_links_scope
        ON {LINK_TABLE}(scope_kind, scope_value_hash, trace_id);
        CREATE TABLE IF NOT EXISTS {TOMBSTONE_TABLE} (
            trace_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            deletion_receipt_id TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_value_hash TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            tombstoned_at TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS event_subject_tombstone_no_update
        BEFORE UPDATE ON {TOMBSTONE_TABLE} BEGIN
            SELECT RAISE(ABORT, 'event subject tombstone is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS event_subject_tombstone_no_delete
        BEFORE DELETE ON {TOMBSTONE_TABLE} BEGIN
            SELECT RAISE(ABORT, 'event subject tombstone is append-only');
        END;
        CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE} (
            receipt_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            request_id TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_value_hash TEXT NOT NULL,
            target_count INTEGER NOT NULL,
            events_deleted INTEGER NOT NULL,
            dead_letters_deleted INTEGER NOT NULL,
            handler_receipts_deleted INTEGER NOT NULL,
            trace_claims_deleted INTEGER NOT NULL,
            deferred_keys_deleted INTEGER NOT NULL,
            after_count INTEGER NOT NULL,
            unresolved_legacy_count INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('flushed', 'applied')),
            created_at TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT '',
            UNIQUE(request_id, scope_kind, scope_value_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_event_subject_receipts_scope
        ON {RECEIPT_TABLE}(scope_kind, scope_value_hash, status);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_event_subject_receipts_pending
        ON {RECEIPT_TABLE}(scope_kind, scope_value_hash)
        WHERE status='flushed';
        """
    statement = ""
    for line in script.splitlines():
        statement += line + "\n"
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            statement = ""
            if sql:
                conn.execute(sql)
    if statement.strip():
        raise sqlite3.DatabaseError("event provenance schema DDL is incomplete")


def record_event_subject_provenance(
    conn: sqlite3.Connection,
    *,
    trace_id: str,
    subject_provenance: Mapping[str, Any] | None,
    ownership_config: Any | None = None,
) -> None:
    """Write an immutable sidecar alongside an EventBus payload transaction."""

    ensure_event_subject_provenance_schema(conn)
    trace_id = str(trace_id)
    if event_is_tombstoned(conn, trace_id):
        raise PermissionError(f"event trace_id {trace_id!r} is tombstoned")
    if subject_provenance is None:
        state = UNATTRIBUTED_PROVENANCE_STATE
        access_json = ""
        access_hash = ""
        links: tuple[tuple[str, str], ...] = ()
    else:
        provenance = ObjectProvenance.from_access_control(subject_provenance)
        if ownership_config is None:
            from core.config import get_config

            ownership_config = get_config()
        from core.privacy.ownership_freeze import assert_cognitive_write_not_frozen

        assert_cognitive_write_not_frozen(
            ownership_config,
            provenance.access_control,
            domain="event bus",
        )
        state = provenance.state
        access_json = provenance.access_json
        access_hash = provenance.access_hash
        links = provenance.selector_hashes
    existing = conn.execute(
        render_sql(
            "SELECT state, access_json, access_hash FROM {table} WHERE trace_id=?",
            identifiers={"table": PROVENANCE_TABLE},
        ),
        (trace_id,),
    ).fetchone()
    if existing is not None:
        # Recovered Event objects intentionally carry only durable event
        # fields; a transition from pending to dead-letter must preserve the
        # already-bound sidecar instead of downgrading it to ``unattributed``.
        # This is safe because ``trace_id`` is immutable and the caller is not
        # asserting a different ACL.
        if subject_provenance is None:
            return
        if tuple(str(item) for item in existing) != (state, access_json, access_hash):
            raise ValueError(f"immutable event provenance conflict for trace_id={trace_id}")
        return
    conn.execute(
        render_sql(
            """
        INSERT INTO {table}
            (trace_id, schema_version, state, access_json, access_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
            identifiers={"table": PROVENANCE_TABLE},
        ),
        (
            trace_id,
            EVENT_SUBJECT_PROVENANCE_SCHEMA_VERSION,
            state,
            access_json,
            access_hash,
            _now(),
        ),
    )
    if links:
        conn.executemany(
            render_sql(
                "INSERT INTO {table}(trace_id, scope_kind, scope_value_hash) "
                "VALUES (?, ?, ?)",
                identifiers={"table": LINK_TABLE},
            ),
            ((trace_id, kind, value_hash) for kind, value_hash in links),
        )


def event_is_tombstoned(conn: sqlite3.Connection, trace_id: str) -> bool:
    try:
        return conn.execute(
            render_sql(
                "SELECT 1 FROM {table} WHERE trace_id=?",
                identifiers={"table": TOMBSTONE_TABLE},
            ),
            (str(trace_id),),
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _payload_hash(conn: sqlite3.Connection, trace_id: str) -> str:
    rows: list[dict[str, str]] = []
    for table in ("events", "dead_letters"):
        for row in conn.execute(
            render_sql(
                "SELECT event_type, source, payload_json FROM {table} WHERE trace_id=?",
                identifiers={"table": table},
            ),
            (trace_id,),
        ).fetchall():
            rows.append(
                {
                    "event_type": str(row[0]),
                    "source": str(row[1]),
                    "payload_json": str(row[2]),
                }
            )
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _trace_ids_for_scope(
    conn: sqlite3.Connection,
    *,
    scope_kind: str,
    selector_hash: str,
) -> list[str]:
    if scope_kind == "all":
        rows = conn.execute(
            render_sql(
                """
            SELECT trace_id FROM events
            UNION SELECT trace_id FROM dead_letters
            UNION SELECT trace_id FROM handler_receipts
            UNION SELECT trace_id FROM {provenance_table}
            EXCEPT SELECT trace_id FROM {tombstone_table}
            ORDER BY trace_id
            """,
                identifiers={
                    "provenance_table": PROVENANCE_TABLE,
                    "tombstone_table": TOMBSTONE_TABLE,
                },
            )
        ).fetchall()
    else:
        rows = conn.execute(
            render_sql(
                """
            SELECT link.trace_id FROM {link_table} AS link
            JOIN {provenance_table} AS provenance ON provenance.trace_id=link.trace_id
            WHERE provenance.state=?
              AND link.scope_kind=?
              AND link.scope_value_hash=?
              AND NOT EXISTS (
                  SELECT 1 FROM {tombstone_table} AS tombstone
                  WHERE tombstone.trace_id=link.trace_id
              )
            ORDER BY link.trace_id
            """,
                identifiers={
                    "link_table": LINK_TABLE,
                    "provenance_table": PROVENANCE_TABLE,
                    "tombstone_table": TOMBSTONE_TABLE,
                },
            ),
            (TRACKED_PROVENANCE_STATE, scope_kind, selector_hash),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _unresolved_historical_count(conn: sqlite3.Connection, *, scope_kind: str) -> int:
    if scope_kind == "all":
        return 0
    row = conn.execute(
        render_sql(
            """
        SELECT COUNT(*) FROM (
            SELECT trace_id FROM events
            UNION SELECT trace_id FROM dead_letters
            UNION SELECT trace_id FROM handler_receipts
        ) AS live
        WHERE NOT EXISTS (
            SELECT 1 FROM {tombstone_table} AS tombstone
            WHERE tombstone.trace_id=live.trace_id
        )
          AND NOT EXISTS (
            SELECT 1 FROM {provenance_table} AS provenance
            WHERE provenance.trace_id=live.trace_id
              AND provenance.state=?
        )
        """,
            identifiers={
                "tombstone_table": TOMBSTONE_TABLE,
                "provenance_table": PROVENANCE_TABLE,
            },
        ),
        (TRACKED_PROVENANCE_STATE,),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def _after_count(conn: sqlite3.Connection, receipt_id: str) -> int:
    row = conn.execute(
        render_sql(
            """
        SELECT COUNT(*) FROM (
            SELECT trace_id FROM events
            UNION SELECT trace_id FROM dead_letters
            UNION SELECT trace_id FROM handler_receipts
        ) AS live
        JOIN {tombstone_table} AS tombstone ON tombstone.trace_id=live.trace_id
        WHERE tombstone.deletion_receipt_id=?
        """,
            identifiers={"tombstone_table": TOMBSTONE_TABLE},
        ),
        (receipt_id,),
    ).fetchone()
    return int(row[0] or 0) if row else 0


def _result(
    *,
    status: str,
    target_count: int,
    events_deleted: int,
    dead_letters_deleted: int,
    handler_receipts_deleted: int,
    trace_claims_deleted: int,
    deferred_keys_deleted: int,
    after_count: int,
    unresolved_legacy_count: int,
) -> dict[str, Any]:
    return {
        "status": status,
        "target_count": target_count,
        "receipt_count": 1,
        "events_deleted": events_deleted,
        "dead_letters_deleted": dead_letters_deleted,
        "handler_receipts_deleted": handler_receipts_deleted,
        "trace_claims_deleted": trace_claims_deleted,
        "deferred_keys_deleted": deferred_keys_deleted,
        "after_count": after_count,
        "unresolved_legacy_count": unresolved_legacy_count,
        "verified": (
            status in {"applied", "existing"}
            and after_count == 0
            and unresolved_legacy_count == 0
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


def _receipt_values(row: sqlite3.Row) -> dict[str, int | str]:
    return {
        "receipt_id": str(row["receipt_id"]),
        "status": str(row["status"]),
        "target_count": int(row["target_count"] or 0),
        "events_deleted": int(row["events_deleted"] or 0),
        "dead_letters_deleted": int(row["dead_letters_deleted"] or 0),
        "handler_receipts_deleted": int(row["handler_receipts_deleted"] or 0),
        "trace_claims_deleted": int(row["trace_claims_deleted"] or 0),
        "deferred_keys_deleted": int(row["deferred_keys_deleted"] or 0),
        "unresolved_legacy_count": int(row["unresolved_legacy_count"] or 0),
    }


def _result_from_values(
    *, status: str, after_count: int, values: Mapping[str, int | str]
) -> dict[str, Any]:
    return _result(
        status=status,
        target_count=int(values["target_count"]),
        events_deleted=int(values["events_deleted"]),
        dead_letters_deleted=int(values["dead_letters_deleted"]),
        handler_receipts_deleted=int(values["handler_receipts_deleted"]),
        trace_claims_deleted=int(values["trace_claims_deleted"]),
        deferred_keys_deleted=int(values["deferred_keys_deleted"]),
        after_count=after_count,
        unresolved_legacy_count=int(values["unresolved_legacy_count"]),
    )


def delete_event_subject_scope(
    *,
    db_path: Path | str,
    request_id: str,
    scope_kind: str,
    scope_value: str,
) -> dict[str, Any]:
    """Physically delete exactly linked event bodies and tombstone their trace IDs."""

    try:
        kind, value = normalize_scope_selector(scope_kind, scope_value)
    except ObjectProvenanceError:
        return {
            "status": "unsupported_scope",
            "target_count": 0,
            "receipt_count": 0,
            "verified": False,
        }
    database = Path(db_path).expanduser()
    if not database.is_file():
        return {
            "status": "not_initialized",
            "target_count": 0,
            "receipt_count": 0,
            "unresolved_legacy_count": 0,
            "verified": True,
        }
    if not str(request_id or "").strip():
        raise ValueError("event deletion requires request_id")
    selector_hash = scope_selector_hash(kind, value)
    receipt_id = _receipt_id(str(request_id), kind, selector_hash)

    try:
        with sqlite3.connect(str(database), timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if not _REQUIRED_TABLES <= tables:
                return _blocked("event_bus_schema_incomplete")
            ensure_event_subject_provenance_schema(conn)
            conn.commit()
            existing = conn.execute(
                render_sql(
                    "SELECT {fields} FROM {receipt_table} "
                    "WHERE request_id=? AND scope_kind=? AND scope_value_hash=?",
                    identifiers={"receipt_table": RECEIPT_TABLE},
                    identifier_lists={"fields": _RECEIPT_FIELDS},
                ),
                (request_id, kind, selector_hash),
            ).fetchone()
            pending = conn.execute(
                render_sql(
                    "SELECT {fields} FROM {receipt_table} "
                    "WHERE scope_kind=? AND scope_value_hash=? AND status='flushed' "
                    "ORDER BY created_at ASC LIMIT 1",
                    identifiers={"receipt_table": RECEIPT_TABLE},
                    identifier_lists={"fields": _RECEIPT_FIELDS},
                ),
                (kind, selector_hash),
            ).fetchone()
            selected = pending or existing
            if selected is not None:
                values = _receipt_values(selected)
                receipt_id = str(values["receipt_id"])
                if values["status"] == "applied":
                    return _result_from_values(status="existing", after_count=0, values=values)
            else:
                secure_delete = conn.execute("PRAGMA secure_delete=ON").fetchone()
                if not secure_delete or int(secure_delete[0] or 0) < 1:
                    return _blocked("event_provenance_secure_delete_unavailable")
                conn.execute("BEGIN IMMEDIATE")
                trace_ids = _trace_ids_for_scope(
                    conn, scope_kind=kind, selector_hash=selector_hash
                )
                target_count = len(trace_ids)
                for trace_id in trace_ids:
                    conn.execute(
                        render_sql(
                            """
                        INSERT INTO {tombstone_table}(
                            trace_id, schema_version, deletion_receipt_id,
                            scope_kind, scope_value_hash, payload_hash, tombstoned_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                            identifiers={"tombstone_table": TOMBSTONE_TABLE},
                        ),
                        (
                            trace_id,
                            EVENT_SUBJECT_PROVENANCE_SCHEMA_VERSION,
                            receipt_id,
                            kind,
                            selector_hash,
                            _payload_hash(conn, trace_id),
                            _now(),
                        ),
                    )
                values = {
                    "target_count": target_count,
                    "events_deleted": 0,
                    "dead_letters_deleted": 0,
                    "handler_receipts_deleted": 0,
                    "trace_claims_deleted": 0,
                    "deferred_keys_deleted": 0,
                }
                if trace_ids:
                    for key, table in (
                        ("events_deleted", "events"),
                        ("dead_letters_deleted", "dead_letters"),
                        ("handler_receipts_deleted", "handler_receipts"),
                        ("trace_claims_deleted", "event_trace_claims"),
                        ("deferred_keys_deleted", "event_deferred_keys"),
                    ):
                        values[key] = int(
                            conn.execute(
                                render_sql(
                                    "DELETE FROM {table} WHERE trace_id IN ({trace_ids})",
                                    identifiers={"table": table},
                                    placeholder_counts={"trace_ids": len(trace_ids)},
                                ),
                                tuple(trace_ids),
                            ).rowcount
                            or 0
                        )
                    conn.execute(
                        render_sql(
                            "DELETE FROM {table} WHERE trace_id IN ({trace_ids})",
                            identifiers={"table": LINK_TABLE},
                            placeholder_counts={"trace_ids": len(trace_ids)},
                        ),
                        tuple(trace_ids),
                    )
                    conn.execute(
                        render_sql(
                            "DELETE FROM {table} WHERE trace_id IN ({trace_ids})",
                            identifiers={"table": PROVENANCE_TABLE},
                            placeholder_counts={"trace_ids": len(trace_ids)},
                        ),
                        tuple(trace_ids),
                    )
                unresolved = _unresolved_historical_count(conn, scope_kind=kind)
                conn.execute(
                    render_sql(
                        """
                    INSERT INTO {receipt_table}(
                        receipt_id, schema_version, request_id, scope_kind, scope_value_hash,
                        target_count, events_deleted, dead_letters_deleted,
                        handler_receipts_deleted, trace_claims_deleted, deferred_keys_deleted,
                        after_count, unresolved_legacy_count, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'flushed', ?)
                    """,
                        identifiers={"receipt_table": RECEIPT_TABLE},
                    ),
                    (
                        receipt_id,
                        EVENT_SUBJECT_PROVENANCE_SCHEMA_VERSION,
                        request_id,
                        kind,
                        selector_hash,
                        values["target_count"],
                        values["events_deleted"],
                        values["dead_letters_deleted"],
                        values["handler_receipts_deleted"],
                        values["trace_claims_deleted"],
                        values["deferred_keys_deleted"],
                        unresolved,
                        _now(),
                    ),
                )
                conn.commit()
                values.update(
                    {
                        "receipt_id": receipt_id,
                        "status": "flushed",
                        "unresolved_legacy_count": unresolved,
                    }
                )
    except (sqlite3.Error, OSError, ValueError):
        return _blocked("event_subject_deletion_failed")

    try:
        with sqlite3.connect(str(database), timeout=10) as conn:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0] or 0) != 0:
                return _result_from_values(
                    status="pending_checkpoint", after_count=0, values=values
                )
            after_count = _after_count(conn, str(values["receipt_id"]))
            if after_count:
                return _blocked("event_subject_after_oracle_nonzero")
            conn.execute(
                render_sql(
                    "UPDATE {receipt_table} "
                    "SET status='applied', after_count=0, applied_at=? "
                    "WHERE receipt_id=? AND status='flushed'",
                    identifiers={"receipt_table": RECEIPT_TABLE},
                ),
                (_now(), values["receipt_id"]),
            )
            conn.commit()
    except (sqlite3.Error, OSError, ValueError):
        return _result_from_values(status="pending_checkpoint", after_count=0, values=values)

    return _result_from_values(status="applied", after_count=0, values=values)
