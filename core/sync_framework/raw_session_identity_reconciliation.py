"""Append-only approvals for native session-identity contract upgrades.

Some native parsers must split one historical session identity into multiple
artifact-bound canonical sessions.  Existing Raw rows are immutable evidence:
they must not be rewritten merely to stamp the new parser contract.  This
module records an exact, content-free approval for the historical event set
that was reviewed before the new identity contract is allowed to write.

The table is deliberately *not* created by :class:`RawEventStore`.  It may be
created and populated only by an explicit offline reconciliation plan that has
already backed up the Raw database.
"""

from __future__ import annotations

import hashlib
from itertools import chain, groupby
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


TABLE_NAME = "raw_session_identity_reconciliations"
SCHEMA_VERSION = "mnemos.raw_session_identity_reconciliation.v1"
CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS raw_session_identity_reconciliations (
        receipt_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        source_agent TEXT NOT NULL,
        identity_contract_version TEXT NOT NULL,
        canonical_session_id TEXT NOT NULL,
        legacy_identity_set_json TEXT NOT NULL,
        legacy_identity_set_hash TEXT NOT NULL,
        source_artifact_id TEXT NOT NULL,
        historical_event_count INTEGER NOT NULL
            CHECK(historical_event_count > 0),
        historical_event_set_hash TEXT NOT NULL,
        plan_hash TEXT NOT NULL,
        reconciled_at TEXT NOT NULL
    )
"""
NO_UPDATE_TRIGGER_SQL = f"""
    CREATE TRIGGER IF NOT EXISTS
        raw_session_identity_reconciliations_no_update
    BEFORE UPDATE ON {TABLE_NAME}
    BEGIN
        SELECT RAISE(ABORT, 'raw_session_identity_reconciliation_append_only');
    END
"""
NO_DELETE_TRIGGER_SQL = f"""
    CREATE TRIGGER IF NOT EXISTS
        raw_session_identity_reconciliations_no_delete
    BEFORE DELETE ON {TABLE_NAME}
    BEGIN
        SELECT RAISE(ABORT, 'raw_session_identity_reconciliation_append_only');
    END
"""


class RawSessionIdentityReconciliationError(RuntimeError):
    """Fail-closed session-identity reconciliation error."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_hash(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


def _normalized_identities(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in values if str(value)}))


class _CanonicalListHasher:
    """Incrementally hash a canonical JSON list with bounded memory."""

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._digest.update(b"[")
        self._first = True
        self._finished = False

    def add(self, value: Any) -> None:
        """Append one canonical item to the in-progress list hash."""
        if self._finished:
            raise RawSessionIdentityReconciliationError(
                "raw_session_identity_reconciliation_hash_finalized"
            )
        if not self._first:
            self._digest.update(b",")
        self._digest.update(_canonical_json(value).encode("utf-8"))
        self._first = False

    def finish(self) -> str:
        """Finalize the list and return its typed SHA-256 identity."""
        if self._finished:
            raise RawSessionIdentityReconciliationError(
                "raw_session_identity_reconciliation_hash_finalized"
            )
        self._digest.update(b"]")
        self._finished = True
        return f"sha256:{self._digest.hexdigest()}"


def _blob_identity(
    conn: sqlite3.Connection,
    *,
    table: str,
    column: str,
    rowid: int,
    storage_type: str,
    size: int | None,
) -> dict[str, Any]:
    if storage_type == "null":
        return {"kind": "null"}
    if storage_type != "blob" or size is None:
        raise RawSessionIdentityReconciliationError(
            "raw_session_identity_reconciliation_blob_type_mismatch"
        )
    digest = hashlib.sha256()
    blobopen = getattr(conn, "blobopen", None)
    if not callable(blobopen):
        raise RawSessionIdentityReconciliationError(
            "raw_session_identity_reconciliation_blob_reader_unavailable"
        )
    with blobopen(table, column, rowid, readonly=True) as blob:
        while True:
            chunk = blob.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return {
        "kind": "blob",
        "size": int(size),
        "sha256": digest.hexdigest(),
    }


def _sqlite_row_hash(
    conn: sqlite3.Connection,
    *,
    table: str,
    rowid: int,
    columns: tuple[str, ...],
    scalar_values: Mapping[str, Any],
    blob_metadata: Mapping[str, tuple[str, int | None]],
) -> str:
    """Hash one exact SQLite row while streaming all BLOB bytes."""
    values: list[dict[str, Any]] = []
    for column in columns:
        if column in blob_metadata:
            storage_type, size = blob_metadata[column]
            encoded: Any = _blob_identity(
                conn,
                table=table,
                column=column,
                rowid=rowid,
                storage_type=storage_type,
                size=size,
            )
        else:
            value = scalar_values[column]
            if value is None:
                encoded = {"kind": "null"}
            else:
                encoded = {
                    "kind": type(value).__name__,
                    "value": value,
                }
        if column not in blob_metadata and scalar_values[column] is None:
            encoded = {"kind": "null"}
        values.append({"column": column, "value": encoded})
    return _canonical_hash(values)


def table_exists(conn: sqlite3.Connection) -> bool:
    """Return whether the explicit reconciliation ledger already exists."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (TABLE_NAME,),
    ).fetchone()
    return row is not None


def _expected_columns() -> tuple[str, ...]:
    return (
        "receipt_id",
        "schema_version",
        "source_agent",
        "identity_contract_version",
        "canonical_session_id",
        "legacy_identity_set_json",
        "legacy_identity_set_hash",
        "source_artifact_id",
        "historical_event_count",
        "historical_event_set_hash",
        "plan_hash",
        "reconciled_at",
    )


def _normalized_schema_sql(value: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return normalized.replace(" if not exists ", " ")


def validate_schema(conn: sqlite3.Connection) -> None:
    """Validate the exact append-only table and mutation guards."""
    if not table_exists(conn):
        raise RawSessionIdentityReconciliationError(
            "raw_session_identity_reconciliation_schema_missing"
        )
    columns = tuple(
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()
    )
    if columns != _expected_columns():
        raise RawSessionIdentityReconciliationError(
            "raw_session_identity_reconciliation_schema_mismatch"
        )
    table_info = conn.execute(f"PRAGMA table_info({TABLE_NAME})").fetchall()
    if (
        tuple(str(row[2]).upper() for row in table_info)
        != (
            "TEXT",
            "TEXT",
            "TEXT",
            "TEXT",
            "TEXT",
            "TEXT",
            "TEXT",
            "TEXT",
            "INTEGER",
            "TEXT",
            "TEXT",
            "TEXT",
        )
        or tuple(int(row[5] or 0) for row in table_info)
        != (1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        or tuple(int(row[3] or 0) for row in table_info)
        != (0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1)
    ):
        raise RawSessionIdentityReconciliationError(
            "raw_session_identity_reconciliation_schema_mismatch"
        )
    table_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (TABLE_NAME,),
    ).fetchone()
    if (
        table_sql_row is None
        or _normalized_schema_sql(str(table_sql_row[0] or ""))
        != _normalized_schema_sql(CREATE_TABLE_SQL)
    ):
        raise RawSessionIdentityReconciliationError(
            "raw_session_identity_reconciliation_schema_mismatch"
        )
    triggers = {
        str(row[0]): str(row[1] or "")
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name=?",
            (TABLE_NAME,),
        ).fetchall()
    }
    expected = {
        "raw_session_identity_reconciliations_no_update": (
            NO_UPDATE_TRIGGER_SQL
        ),
        "raw_session_identity_reconciliations_no_delete": (
            NO_DELETE_TRIGGER_SQL
        ),
    }
    if set(triggers) != set(expected) or any(
        _normalized_schema_sql(triggers[name])
        != _normalized_schema_sql(expected[name])
        for name in expected
    ):
        raise RawSessionIdentityReconciliationError(
            "raw_session_identity_reconciliation_trigger_mismatch"
        )


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Create the explicit append-only reconciliation ledger."""
    conn.execute(CREATE_TABLE_SQL)
    conn.execute(NO_UPDATE_TRIGGER_SQL)
    conn.execute(NO_DELETE_TRIGGER_SQL)
    validate_schema(conn)


def incompatible_event_fingerprint(
    conn: sqlite3.Connection,
    *,
    source_agent: str,
    session_ids: Iterable[Any],
    identity_contract_version: str,
) -> dict[str, Any]:
    """Stream-hash every historical event not bound to the new contract."""
    identities = _normalized_identities(session_ids)
    if not source_agent or not identity_contract_version or not identities:
        return {
            "historical_event_count": 0,
            "historical_event_set_hash": _canonical_hash([]),
        }
    placeholders = ",".join("?" for _ in identities)
    raw_schema = tuple(
        (str(row[1]), str(row[2]).upper())
        for row in conn.execute("PRAGMA table_info(raw_turns)").fetchall()
    )
    revision_schema = tuple(
        (str(row[1]), str(row[2]).upper())
        for row in conn.execute("PRAGMA table_info(raw_turn_revisions)").fetchall()
    )
    raw_columns = tuple(name for name, _type in raw_schema)
    revision_columns = tuple(name for name, _type in revision_schema)
    raw_blob_columns = tuple(
        name for name, declared_type in raw_schema if declared_type == "BLOB"
    )
    revision_blob_columns = tuple(
        name
        for name, declared_type in revision_schema
        if declared_type == "BLOB"
    )
    raw_scalar_columns = tuple(
        name for name in raw_columns if name not in raw_blob_columns
    )
    revision_scalar_columns = tuple(
        name
        for name in revision_columns
        if name not in revision_blob_columns
    )

    def _quoted(alias: str, column: str) -> str:
        return f'{alias}."{column.replace(chr(34), chr(34) * 2)}"'

    projections = [
        "t.rowid",
        *(_quoted("t", column) for column in raw_scalar_columns),
        *(
            expression
            for column in raw_blob_columns
            for expression in (
                f"typeof({_quoted('t', column)})",
                f"length({_quoted('t', column)})",
            )
        ),
        "r.rowid",
        *(_quoted("r", column) for column in revision_scalar_columns),
        *(
            expression
            for column in revision_blob_columns
            for expression in (
                f"typeof({_quoted('r', column)})",
                f"length({_quoted('r', column)})",
            )
        ),
    ]
    cursor = conn.execute(
        f"SELECT {', '.join(projections)} "  # nosec B608
        "FROM raw_turns AS t "
        "LEFT JOIN raw_turn_revisions AS r ON r.logical_event_id=t.event_id "
        f"WHERE t.source_agent=? AND t.session_id IN ({placeholders}) "  # nosec B608
        "ORDER BY t.event_id, r.revision_number, r.revision_id",
        (str(source_agent), *identities),
    )
    raw_scalar_start = 1
    raw_blob_start = raw_scalar_start + len(raw_scalar_columns)
    revision_rowid_index = raw_blob_start + (2 * len(raw_blob_columns))
    revision_scalar_start = revision_rowid_index + 1
    revision_blob_start = revision_scalar_start + len(
        revision_scalar_columns
    )
    event_id_index = raw_scalar_start + raw_scalar_columns.index("event_id")
    incompatible_hash = _CanonicalListHasher()
    incompatible_count = 0

    for _event_id, joined_rows in groupby(
        cursor,
        key=lambda row: str(row[event_id_index]),
    ):
        group = iter(joined_rows)
        first = next(group)
        raw_scalars = {
            column: first[raw_scalar_start + index]
            for index, column in enumerate(raw_scalar_columns)
        }
        raw_blobs = {
            column: (
                str(first[raw_blob_start + (2 * index)] or ""),
                (
                    int(first[raw_blob_start + (2 * index) + 1])
                    if first[raw_blob_start + (2 * index) + 1] is not None
                    else None
                ),
            )
            for index, column in enumerate(raw_blob_columns)
        }
        event_id = str(raw_scalars["event_id"])
        session_id = str(raw_scalars["session_id"])
        turn_number = int(raw_scalars["turn_number"])
        try:
            metadata = json.loads(str(raw_scalars["metadata_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if isinstance(metadata, Mapping) and metadata.get(
            "identity_contract_version"
        ) == identity_contract_version:
            continue

        raw_turn_row_hash = _sqlite_row_hash(
            conn,
            table="raw_turns",
            rowid=int(first[0]),
            columns=raw_columns,
            scalar_values=raw_scalars,
            blob_metadata=raw_blobs,
        )
        current_revision_id = str(
            raw_scalars["current_revision_id"] or ""
        )
        current_revision_row_hash = ""
        revision_set_hash = _CanonicalListHasher()
        revision_content_hash = _CanonicalListHasher()
        revision_count = 0
        for joined in chain((first,), group):
            revision_rowid = joined[revision_rowid_index]
            if revision_rowid is None:
                continue
            revision_scalars = {
                column: joined[revision_scalar_start + index]
                for index, column in enumerate(revision_scalar_columns)
            }
            revision_blobs = {
                column: (
                    str(joined[revision_blob_start + (2 * index)] or ""),
                    (
                        int(joined[revision_blob_start + (2 * index) + 1])
                        if joined[
                            revision_blob_start + (2 * index) + 1
                        ]
                        is not None
                        else None
                    ),
                )
                for index, column in enumerate(revision_blob_columns)
            }
            revision_id = str(revision_scalars["revision_id"])
            revision_row_hash = _sqlite_row_hash(
                conn,
                table="raw_turn_revisions",
                rowid=int(revision_rowid),
                columns=revision_columns,
                scalar_values=revision_scalars,
                blob_metadata=revision_blobs,
            )
            revision_set_hash.add(
                {
                    "revision_id": revision_id,
                    "row_hash": revision_row_hash,
                }
            )
            revision_content_hash.add(
                {
                    "revision_id": revision_id,
                    "content_hash": str(
                        revision_scalars["content_hash"] or ""
                    ),
                    "full_content_hash": str(
                        revision_scalars["full_content_hash"] or ""
                    ),
                }
            )
            if revision_id == current_revision_id:
                current_revision_row_hash = revision_row_hash
            revision_count += 1
        if (
            revision_count == 0
            or not current_revision_id
            or not current_revision_row_hash
        ):
            raise RawSessionIdentityReconciliationError(
                "raw_session_identity_reconciliation_current_revision_invalid"
            )
        incompatible_hash.add(
            {
                "event_id": event_id,
                "session_id": session_id,
                "turn_number": turn_number,
                "raw_turn_row_hash": raw_turn_row_hash,
                "current_revision_id": current_revision_id,
                "current_revision_row_hash": current_revision_row_hash,
                "logical_content_hash": _canonical_hash(
                    {
                        "current_revision_id": current_revision_id,
                        "content_hash": str(
                            raw_scalars["content_hash"] or ""
                        ),
                        "full_content_hash": str(
                            raw_scalars["full_content_hash"] or ""
                        ),
                        "revision_count": revision_count,
                        "revision_content_set_hash": (
                            revision_content_hash.finish()
                        ),
                    }
                ),
                "revision_set_hash": revision_set_hash.finish(),
            }
        )
        incompatible_count += 1
    return {
        "historical_event_count": incompatible_count,
        "historical_event_set_hash": incompatible_hash.finish(),
    }


def build_receipt_material(
    conn: sqlite3.Connection,
    *,
    source_agent: str,
    identity_contract_version: str,
    canonical_session_id: str,
    legacy_session_ids: Iterable[Any],
    source_artifact_id: str,
) -> dict[str, Any] | None:
    """Build exact receipt material, or ``None`` when no old row needs approval."""
    identities = _normalized_identities(
        (canonical_session_id, *tuple(legacy_session_ids))
    )
    fingerprint = incompatible_event_fingerprint(
        conn,
        source_agent=source_agent,
        session_ids=identities,
        identity_contract_version=identity_contract_version,
    )
    if int(fingerprint["historical_event_count"]) == 0:
        return None
    if (
        not source_agent
        or not identity_contract_version
        or not canonical_session_id
        or not source_artifact_id
    ):
        raise RawSessionIdentityReconciliationError(
            "raw_session_identity_reconciliation_context_incomplete"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "source_agent": str(source_agent),
        "identity_contract_version": str(identity_contract_version),
        "canonical_session_id": str(canonical_session_id),
        "legacy_identity_set_json": _canonical_json(list(identities)),
        "legacy_identity_set_hash": _canonical_hash(list(identities)),
        "source_artifact_id": str(source_artifact_id),
        "historical_event_count": int(fingerprint["historical_event_count"]),
        "historical_event_set_hash": str(
            fingerprint["historical_event_set_hash"]
        ),
    }


def _receipt_identity(material: Mapping[str, Any], plan_hash: str) -> str:
    return _canonical_hash(
        {
            **dict(material),
            "plan_hash": str(plan_hash),
        }
    )


def record_receipt(
    conn: sqlite3.Connection,
    *,
    material: Mapping[str, Any],
    plan_hash: str,
    reconciled_at: str | None = None,
) -> str:
    """Append one receipt and verify its exact durable binding."""
    validate_schema(conn)
    required = set(_expected_columns()) - {
        "receipt_id",
        "plan_hash",
        "reconciled_at",
    }
    if set(material) != required:
        raise RawSessionIdentityReconciliationError(
            "raw_session_identity_reconciliation_material_mismatch"
        )
    receipt_id = _receipt_identity(material, plan_hash)
    timestamp = reconciled_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO raw_session_identity_reconciliations (
            receipt_id, schema_version, source_agent,
            identity_contract_version, canonical_session_id,
            legacy_identity_set_json, legacy_identity_set_hash,
            source_artifact_id, historical_event_count,
            historical_event_set_hash, plan_hash, reconciled_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            receipt_id,
            material["schema_version"],
            material["source_agent"],
            material["identity_contract_version"],
            material["canonical_session_id"],
            material["legacy_identity_set_json"],
            material["legacy_identity_set_hash"],
            material["source_artifact_id"],
            material["historical_event_count"],
            material["historical_event_set_hash"],
            str(plan_hash),
            timestamp,
        ),
    )
    row = conn.execute(
        """
        SELECT schema_version, source_agent, identity_contract_version,
               canonical_session_id, legacy_identity_set_json,
               legacy_identity_set_hash, source_artifact_id,
               historical_event_count, historical_event_set_hash, plan_hash
        FROM raw_session_identity_reconciliations
        WHERE receipt_id=?
        """,
        (receipt_id,),
    ).fetchone()
    expected = (
        material["schema_version"],
        material["source_agent"],
        material["identity_contract_version"],
        material["canonical_session_id"],
        material["legacy_identity_set_json"],
        material["legacy_identity_set_hash"],
        material["source_artifact_id"],
        material["historical_event_count"],
        material["historical_event_set_hash"],
        str(plan_hash),
    )
    if row != expected:
        raise RawSessionIdentityReconciliationError(
            "raw_session_identity_reconciliation_receipt_mismatch"
        )
    return receipt_id


def receipt_allows_current_fingerprint(
    conn: sqlite3.Connection,
    *,
    source_agent: str,
    identity_contract_version: str,
    canonical_session_id: str,
    legacy_session_ids: Iterable[Any],
    source_artifact_id: str,
) -> bool:
    """Return true only for an exact, still-current reviewed event set."""
    material = build_receipt_material(
        conn,
        source_agent=source_agent,
        identity_contract_version=identity_contract_version,
        canonical_session_id=canonical_session_id,
        legacy_session_ids=legacy_session_ids,
        source_artifact_id=source_artifact_id,
    )
    if material is None:
        return True
    if not table_exists(conn):
        return False
    try:
        validate_schema(conn)
    except RawSessionIdentityReconciliationError:
        return False
    row = conn.execute(
        """
        SELECT receipt_id, plan_hash
        FROM raw_session_identity_reconciliations
        WHERE source_agent=?
          AND identity_contract_version=?
          AND canonical_session_id=?
          AND legacy_identity_set_hash=?
          AND source_artifact_id=?
          AND historical_event_count=?
          AND historical_event_set_hash=?
        ORDER BY reconciled_at DESC, receipt_id DESC
        LIMIT 1
        """,
        (
            material["source_agent"],
            material["identity_contract_version"],
            material["canonical_session_id"],
            material["legacy_identity_set_hash"],
            material["source_artifact_id"],
            material["historical_event_count"],
            material["historical_event_set_hash"],
        ),
    ).fetchone()
    if row is None:
        return False
    return str(row[0]) == _receipt_identity(material, str(row[1]))
