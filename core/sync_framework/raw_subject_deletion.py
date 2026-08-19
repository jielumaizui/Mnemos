# -*- coding: utf-8 -*-
"""Subject-scoped deletion receipts for canonical Raw.

The canonical Raw store retains immutable revision hashes and provenance edges,
but a confirmed privacy deletion must remove every recoverable body before any
downstream reconciliation can be called complete.  This module owns the small
receipt schema shared by writable Raw and read-only Raw consumers.  It keeps
only hashes and opaque event IDs; subject values and deleted content never
enter the receipt.
"""

from __future__ import annotations

import hashlib
import sqlite3

from core.db_utils import render_sql


RAW_SUBJECT_DELETION_SCHEMA_VERSION = "mnemos.raw_subject_deletion.v1"
RAW_SUBJECT_DELETION_TABLE = "raw_subject_deletion_receipts"


def subject_scope_hash(scope_kind: str, scope_value: str) -> str:
    """Return the non-reversible receipt key for an ownership scope."""

    value = f"{str(scope_kind).strip().lower()}:{str(scope_value).strip()}"
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def subject_deletion_receipt_id(
    *,
    request_id: str,
    event_id: str,
    scope_hash: str,
) -> str:
    """Create a deterministic receipt ID without retaining subject literals."""

    material = "|".join((str(request_id), str(event_id), str(scope_hash)))
    return "raw-delete-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def ensure_subject_deletion_schema(conn: sqlite3.Connection) -> None:
    """Create the canonical Raw deletion receipt table if it is absent."""

    conn.execute(
        render_sql(
            """
        CREATE TABLE IF NOT EXISTS {table} (
            receipt_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            request_id TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_value_hash TEXT NOT NULL,
            event_id TEXT NOT NULL UNIQUE,
            current_revision_id TEXT NOT NULL DEFAULT '',
            source_content_hash TEXT NOT NULL DEFAULT '',
            redaction_hash TEXT NOT NULL,
            revision_count INTEGER NOT NULL DEFAULT 0,
            dependent_consumer_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL CHECK(status = 'applied'),
            created_at TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """,
            identifiers={"table": RAW_SUBJECT_DELETION_TABLE},
        )
    )
    conn.execute(
        render_sql(
            """
        CREATE INDEX IF NOT EXISTS idx_raw_subject_deletion_request
        ON {table}(request_id, status)
        """,
            identifiers={"table": RAW_SUBJECT_DELETION_TABLE},
        )
    )


def subject_deletion_visibility_predicate(event_column: str) -> str:
    """Exclude objects whose recoverable Raw payload was subject-deleted."""

    return render_sql(
        """
        AND NOT EXISTS (
            SELECT 1
            FROM {table} AS subject_delete
            WHERE subject_delete.event_id={event_column}
              AND subject_delete.status='applied'
        )
    """,
        identifiers={
            "table": RAW_SUBJECT_DELETION_TABLE,
            "event_column": event_column,
        },
    )


def subject_deletion_table_exists(conn: sqlite3.Connection) -> bool:
    """Return whether this database has the required deletion contract."""

    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (RAW_SUBJECT_DELETION_TABLE,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def is_subject_deleted(conn: sqlite3.Connection, event_id: str) -> bool:
    """Fail closed when the deletion receipt owner is absent or unreadable."""

    if not subject_deletion_table_exists(conn):
        return True
    try:
        row = conn.execute(
            render_sql(
                """
            SELECT 1
            FROM {table}
            WHERE event_id=? AND status='applied'
            LIMIT 1
            """,
                identifiers={"table": RAW_SUBJECT_DELETION_TABLE},
            ),
            (str(event_id),),
        ).fetchone()
    except sqlite3.Error:
        return True
    return row is not None
