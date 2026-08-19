"""Object provenance and tombstone owner for the append-only ActionLedger.

The ledger's evidence rows cannot be rewritten or deleted.  Subject deletion
therefore appends a non-reversible tombstone and removes the active provenance
sidecar.  The public facade projects tombstoned rows without their body while
retaining a one-way record hash for operational audit.
"""

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


ACTION_LEDGER_SUBJECT_PROVENANCE_SCHEMA_VERSION = "mnemos.action_ledger_subject_provenance.v1"
PROVENANCE_TABLE = "action_ledger_object_provenance"
LINK_TABLE = "action_ledger_subject_links"
TOMBSTONE_TABLE = "action_ledger_subject_tombstones"
RECEIPT_TABLE = "action_ledger_subject_deletion_receipts"
_REDACTED = "[redacted:subject-deleted]"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _receipt_id(request_id: str, scope_kind: str, scope_value_hash: str) -> str:
    material = "|".join((request_id, scope_kind, scope_value_hash))
    return "action-ledger-delete-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def _record_hash(row: sqlite3.Row | Mapping[str, Any]) -> str:
    payload = {
        key: row[key]
        for key in (
            "action_id",
            "schema_version",
            "actor",
            "action_type",
            "target",
            "before_ref",
            "after_ref",
            "evidence_refs_json",
            "quality_decision_id",
            "verification_json",
            "rollback_ref",
            "status",
            "created_at",
        )
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def ensure_action_ledger_subject_provenance_schema(conn: sqlite3.Connection) -> None:
    """Create the sidecar schema without committing an action-row transaction."""

    script = f"""
        CREATE TABLE IF NOT EXISTS {PROVENANCE_TABLE} (
            action_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('tracked', 'unattributed')),
            access_json TEXT NOT NULL DEFAULT '',
            access_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {LINK_TABLE} (
            action_id TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_value_hash TEXT NOT NULL,
            PRIMARY KEY(action_id, scope_kind, scope_value_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_action_ledger_subject_links_scope
        ON {LINK_TABLE}(scope_kind, scope_value_hash, action_id);
        CREATE TABLE IF NOT EXISTS {TOMBSTONE_TABLE} (
            action_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            deletion_receipt_id TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_value_hash TEXT NOT NULL,
            record_hash TEXT NOT NULL,
            tombstoned_at TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS action_ledger_subject_tombstone_no_update
        BEFORE UPDATE ON {TOMBSTONE_TABLE} BEGIN
            SELECT RAISE(ABORT, 'action ledger subject tombstone is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS action_ledger_subject_tombstone_no_delete
        BEFORE DELETE ON {TOMBSTONE_TABLE} BEGIN
            SELECT RAISE(ABORT, 'action ledger subject tombstone is append-only');
        END;
        CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE} (
            receipt_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            request_id TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_value_hash TEXT NOT NULL,
            target_count INTEGER NOT NULL,
            tombstoned_count INTEGER NOT NULL,
            after_count INTEGER NOT NULL,
            unresolved_legacy_count INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('flushed', 'applied')),
            created_at TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT '',
            UNIQUE(request_id, scope_kind, scope_value_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_action_ledger_subject_receipt_scope
        ON {RECEIPT_TABLE}(scope_kind, scope_value_hash, status);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_action_ledger_subject_receipt_pending
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
        raise sqlite3.DatabaseError("action provenance schema DDL is incomplete")


def record_action_subject_provenance(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    subject_provenance: Mapping[str, Any] | None,
    ownership_config: Any | None = None,
) -> None:
    """Persist or verify a sidecar in the same transaction as an action row."""

    ensure_action_ledger_subject_provenance_schema(conn)
    action_id = str(action_id)
    if action_is_tombstoned(conn, action_id):
        raise PermissionError(f"action ledger record {action_id!r} is tombstoned")
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
            domain="action ledger",
        )
        state = provenance.state
        access_json = provenance.access_json
        access_hash = provenance.access_hash
        links = provenance.selector_hashes

    existing = conn.execute(
        render_sql(
            "SELECT state, access_json, access_hash FROM {table} WHERE action_id=?",
            identifiers={"table": PROVENANCE_TABLE},
        ),
        (action_id,),
    ).fetchone()
    if existing is not None:
        # Exact ActionRecord replays can omit the optional in-memory ACL field.
        # Preserve the immutable association already stored for this action ID
        # instead of allowing a retry to downgrade it to ``unattributed``.
        if subject_provenance is None:
            return
        if tuple(str(value) for value in existing) != (state, access_json, access_hash):
            raise ValueError(f"immutable action provenance conflict for action_id={action_id}")
        return
    conn.execute(
        render_sql(
            """
        INSERT INTO {table}
            (action_id, schema_version, state, access_json, access_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
            identifiers={"table": PROVENANCE_TABLE},
        ),
        (
            action_id,
            ACTION_LEDGER_SUBJECT_PROVENANCE_SCHEMA_VERSION,
            state,
            access_json,
            access_hash,
            _now(),
        ),
    )
    if links:
        conn.executemany(
            render_sql(
                """
            INSERT INTO {table}(action_id, scope_kind, scope_value_hash)
            VALUES (?, ?, ?)
            """,
                identifiers={"table": LINK_TABLE},
            ),
            ((action_id, kind, value_hash) for kind, value_hash in links),
        )


def _tombstone_for_action(conn: sqlite3.Connection, action_id: str) -> dict[str, str] | None:
    try:
        row = conn.execute(
            render_sql(
                """
            SELECT record_hash, tombstoned_at FROM {table}
            WHERE action_id=?
            """,
                identifiers={"table": TOMBSTONE_TABLE},
            ),
            (action_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {"record_hash": str(row[0]), "tombstoned_at": str(row[1])}


def action_is_tombstoned(conn: sqlite3.Connection, action_id: str) -> bool:
    return _tombstone_for_action(conn, action_id) is not None


def action_tombstone(conn: sqlite3.Connection, action_id: str) -> dict[str, str] | None:
    """Return body-free tombstone evidence for the facade projection."""

    return _tombstone_for_action(conn, action_id)


def redact_action_projection(item: dict[str, Any], tombstone: Mapping[str, str]) -> dict[str, Any]:
    """Return the only supported reader projection for a deleted subject."""

    return {
        "action_id": item["action_id"],
        "schema_version": item["schema_version"],
        "actor": _REDACTED,
        "action_type": item["action_type"],
        "target": _REDACTED,
        "before_ref": _REDACTED,
        "after_ref": _REDACTED,
        "evidence_refs": [],
        "quality_decision_id": _REDACTED,
        "verification": {
            "redacted": True,
            "record_hash": tombstone["record_hash"],
            "tombstoned_at": tombstone["tombstoned_at"],
        },
        "rollback_ref": _REDACTED,
        "status": "tombstoned",
        "created_at": item["created_at"],
    }


def _result(
    *,
    status: str,
    target_count: int,
    tombstoned_count: int,
    after_count: int,
    unresolved_legacy_count: int,
) -> dict[str, Any]:
    return {
        "status": status,
        "target_count": target_count,
        "receipt_count": 1,
        "tombstoned_count": tombstoned_count,
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


def _active_action_rows(
    conn: sqlite3.Connection,
    *,
    scope_kind: str,
    scope_value_hash: str,
) -> list[sqlite3.Row]:
    if scope_kind == "all":
        return conn.execute(
            render_sql(
                """
            SELECT ledger.* FROM action_ledger AS ledger
            WHERE NOT EXISTS (
                SELECT 1 FROM {tombstone_table} AS tombstone
                WHERE tombstone.action_id=ledger.action_id
            )
            ORDER BY ledger.action_id
            """,
                identifiers={"tombstone_table": TOMBSTONE_TABLE},
            )
        ).fetchall()
    return conn.execute(
        render_sql(
            """
        SELECT ledger.* FROM action_ledger AS ledger
        JOIN {link_table} AS link ON link.action_id=ledger.action_id
        JOIN {provenance_table} AS provenance ON provenance.action_id=ledger.action_id
        WHERE provenance.state=?
          AND link.scope_kind=?
          AND link.scope_value_hash=?
          AND NOT EXISTS (
              SELECT 1 FROM {tombstone_table} AS tombstone
              WHERE tombstone.action_id=ledger.action_id
          )
        ORDER BY ledger.action_id
        """,
            identifiers={
                "link_table": LINK_TABLE,
                "provenance_table": PROVENANCE_TABLE,
                "tombstone_table": TOMBSTONE_TABLE,
            },
        ),
        (TRACKED_PROVENANCE_STATE, scope_kind, scope_value_hash),
    ).fetchall()


def _unresolved_historical_count(conn: sqlite3.Connection, *, scope_kind: str) -> int:
    if scope_kind == "all":
        return 0
    row = conn.execute(
        render_sql(
            """
        SELECT COUNT(*) FROM action_ledger AS ledger
        WHERE NOT EXISTS (
            SELECT 1 FROM {tombstone_table} AS tombstone
            WHERE tombstone.action_id=ledger.action_id
        )
          AND NOT EXISTS (
            SELECT 1 FROM {provenance_table} AS provenance
            WHERE provenance.action_id=ledger.action_id
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


def delete_action_ledger_subject_scope(
    *,
    db_path: Path | str,
    request_id: str,
    scope_kind: str,
    scope_value: str,
) -> dict[str, Any]:
    """Tombstone exact provenance-linked ledger objects without guessing old rows."""

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
        raise ValueError("action ledger deletion requires request_id")

    selector_hash = scope_selector_hash(kind, value)
    receipt_id = _receipt_id(str(request_id), kind, selector_hash)
    try:
        with sqlite3.connect(str(database), timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='action_ledger'"
            ).fetchone()
            if exists is None:
                return _blocked("action_ledger_schema_missing")
            ensure_action_ledger_subject_provenance_schema(conn)
            conn.commit()

            existing = conn.execute(
                render_sql(
                    """
                SELECT receipt_id, status, target_count, tombstoned_count,
                       after_count, unresolved_legacy_count
                FROM {receipt_table}
                WHERE request_id=? AND scope_kind=? AND scope_value_hash=?
                """,
                    identifiers={"receipt_table": RECEIPT_TABLE},
                ),
                (request_id, kind, selector_hash),
            ).fetchone()
            pending = conn.execute(
                render_sql(
                    """
                SELECT receipt_id, status, target_count, tombstoned_count,
                       after_count, unresolved_legacy_count
                FROM {receipt_table}
                WHERE scope_kind=? AND scope_value_hash=? AND status='flushed'
                ORDER BY created_at ASC LIMIT 1
                """,
                    identifiers={"receipt_table": RECEIPT_TABLE},
                ),
                (kind, selector_hash),
            ).fetchone()
            selected_receipt = pending or existing
            if selected_receipt is not None:
                receipt_id = str(selected_receipt["receipt_id"])
                target_count = int(selected_receipt["target_count"] or 0)
                tombstoned_count = int(selected_receipt["tombstoned_count"] or 0)
                unresolved = int(selected_receipt["unresolved_legacy_count"] or 0)
                if selected_receipt["status"] == "applied":
                    return _result(
                        status="existing",
                        target_count=target_count,
                        tombstoned_count=tombstoned_count,
                        after_count=0,
                        unresolved_legacy_count=unresolved,
                    )
            else:
                secure_delete = conn.execute("PRAGMA secure_delete=ON").fetchone()
                if not secure_delete or int(secure_delete[0] or 0) < 1:
                    return _blocked("action_ledger_provenance_secure_delete_unavailable")
                conn.execute("BEGIN IMMEDIATE")
                rows = _active_action_rows(
                    conn, scope_kind=kind, scope_value_hash=selector_hash
                )
                target_count = len(rows)
                for row in rows:
                    conn.execute(
                        render_sql(
                            """
                        INSERT INTO {tombstone_table}(
                            action_id, schema_version, deletion_receipt_id,
                            scope_kind, scope_value_hash, record_hash, tombstoned_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                            identifiers={"tombstone_table": TOMBSTONE_TABLE},
                        ),
                        (
                            str(row["action_id"]),
                            ACTION_LEDGER_SUBJECT_PROVENANCE_SCHEMA_VERSION,
                            receipt_id,
                            kind,
                            selector_hash,
                            _record_hash(row),
                            _now(),
                        ),
                    )
                action_ids = tuple(str(row["action_id"]) for row in rows)
                if action_ids:
                    conn.execute(
                        render_sql(
                            "DELETE FROM {link_table} WHERE action_id IN ({action_ids})",
                            identifiers={"link_table": LINK_TABLE},
                            placeholder_counts={"action_ids": len(action_ids)},
                        ),
                        action_ids,
                    )
                    conn.execute(
                        render_sql(
                            "DELETE FROM {provenance_table} "
                            "WHERE action_id IN ({action_ids})",
                            identifiers={"provenance_table": PROVENANCE_TABLE},
                            placeholder_counts={"action_ids": len(action_ids)},
                        ),
                        action_ids,
                    )
                unresolved = _unresolved_historical_count(conn, scope_kind=kind)
                conn.execute(
                    render_sql(
                        """
                    INSERT INTO {receipt_table}(
                        receipt_id, schema_version, request_id, scope_kind, scope_value_hash,
                        target_count, tombstoned_count, after_count,
                        unresolved_legacy_count, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 'flushed', ?)
                    """,
                        identifiers={"receipt_table": RECEIPT_TABLE},
                    ),
                    (
                        receipt_id,
                        ACTION_LEDGER_SUBJECT_PROVENANCE_SCHEMA_VERSION,
                        request_id,
                        kind,
                        selector_hash,
                        target_count,
                        target_count,
                        unresolved,
                        _now(),
                    ),
                )
                tombstoned_count = target_count
                conn.commit()
    except (sqlite3.Error, OSError, ValueError):
        return _blocked("action_ledger_subject_deletion_failed")

    try:
        with sqlite3.connect(str(database), timeout=10) as conn:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0] or 0) != 0:
                return _result(
                    status="pending_checkpoint",
                    target_count=target_count,
                    tombstoned_count=tombstoned_count,
                    after_count=0,
                    unresolved_legacy_count=unresolved,
                )
            after = conn.execute(
                render_sql(
                    "SELECT COUNT(*) FROM {tombstone_table} WHERE deletion_receipt_id=?",
                    identifiers={"tombstone_table": TOMBSTONE_TABLE},
                ),
                (receipt_id,),
            ).fetchone()
            after_count = max(0, target_count - int(after[0] or 0)) if after else target_count
            if after_count:
                return _blocked("action_ledger_tombstone_after_oracle_nonzero")
            conn.execute(
                render_sql(
                    """
                UPDATE {receipt_table}
                SET status='applied', after_count=0, applied_at=?
                WHERE receipt_id=? AND status='flushed'
                """,
                    identifiers={"receipt_table": RECEIPT_TABLE},
                ),
                (_now(), receipt_id),
            )
            conn.commit()
    except (sqlite3.Error, OSError, ValueError):
        return _result(
            status="pending_checkpoint",
            target_count=target_count,
            tombstoned_count=tombstoned_count,
            after_count=0,
            unresolved_legacy_count=unresolved,
        )

    return _result(
        status="applied",
        target_count=target_count,
        tombstoned_count=tombstoned_count,
        after_count=0,
        unresolved_legacy_count=unresolved,
    )
