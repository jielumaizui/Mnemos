"""Metadata-only inventory and immutable plan facts for ledger reconciliation."""

from __future__ import annotations

import os
import sqlite3
import stat as stat_module
from pathlib import Path
from typing import Any, Iterable

from core.db_utils import render_sql, validate_sql_identifier
from core.telemetry.model_call_ledger import ModelCallLedgerInvariantError

from .contracts import (
    HEX_SHA256 as _HEX_SHA256,
    RECORD_TABLES as _RECORD_TABLES,
    RETIRED_TABLES as _RETIRED_TABLES,
    SAFE_COLUMNS as _SAFE_COLUMNS,
    SQLITE_SIDECAR_SUFFIXES as _SQLITE_SIDECAR_SUFFIXES,
    HistoricalCall,
    ModelCallLedgerReconcileError,
    json_hash as _json_hash,
    safe_reconcile_error,
    safe_record_metadata_identity as _safe_record_metadata_identity,
)


def _safe_reconcile_error(exc: BaseException) -> str:
    return safe_reconcile_error(exc, invariant_error=ModelCallLedgerInvariantError)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    _require_regular_sqlite_file(path)
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _require_regular_sqlite_file(path: Path, *, allow_missing: bool = False) -> bool:
    """Reject symlink/device targets before a reconciliation SQLite connect.

    Reconciliation has fixed owner filenames beneath the configured runtime
    directory.  Following a file symlink here could turn a local repair into
    a DROP on an unrelated database.  Missing files are valid only for a
    dry-run/new canonical owner; callers that will open a database must use
    the default strict mode.
    """
    candidate = Path(path).expanduser()
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        if allow_missing:
            return False
        raise ModelCallLedgerReconcileError("sqlite_source_missing") from None
    except OSError as exc:
        raise ModelCallLedgerReconcileError("sqlite_source_uninspectable") from exc
    if stat_module.S_ISLNK(metadata.st_mode):
        raise ModelCallLedgerReconcileError("sqlite_source_symlink_forbidden")
    if not stat_module.S_ISREG(metadata.st_mode):
        raise ModelCallLedgerReconcileError("sqlite_source_not_regular_file")
    return True


def _reject_orphan_sqlite_sidecars(path: Path) -> None:
    """Reject an absent SQLite main file that still has durable sidecars.

    SQLite WAL and rollback-journal pages can contain retired prompt data even
    when their main database was removed.  Treating that state as an empty
    source would let a migration plan report clean while unaccounted raw bytes
    remain under a fixed runtime owner.  The plan is read-only, so it records
    only a typed failure and leaves every artifact untouched for manual
    recovery.
    """
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        candidate = Path(str(path) + suffix)
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ModelCallLedgerReconcileError("sqlite_sidecar_uninspectable") from exc
        raise ModelCallLedgerReconcileError("reconciliation_orphan_sidecar_present")


def _require_runtime_owner_path(path: Path, database_dir: Path) -> None:
    """Ensure a fixed reconciliation filename remains below its runtime root."""
    candidate = Path(path).expanduser()
    root = Path(database_dir).expanduser()
    try:
        root_metadata = root.lstat()
    except FileNotFoundError:
        root_metadata = None
    except OSError as exc:
        raise ModelCallLedgerReconcileError("runtime_database_directory_uninspectable") from exc
    if root_metadata is not None and (
        stat_module.S_ISLNK(root_metadata.st_mode)
        or not stat_module.S_ISDIR(root_metadata.st_mode)
    ):
        raise ModelCallLedgerReconcileError("runtime_database_directory_must_be_a_non_symlink_directory")
    if Path(os.path.abspath(str(candidate.parent))) != Path(os.path.abspath(str(root))):
        raise ModelCallLedgerReconcileError("sqlite_source_outside_runtime_directory")


def _source_generation(path: Path) -> str:
    """Return a non-content identity for one physical SQLite generation.

    Rowid plus a filename is not an immutable source identity: a source can
    be recreated at the same path and start rowids at one again.  Bind plans
    and historical fingerprints to filesystem generation facts instead.  We
    intentionally do not hash/read prompt or response pages here.
    """
    if not _require_regular_sqlite_file(path, allow_missing=True):
        return "missing"
    try:
        stat = path.lstat()
    except OSError as exc:
        raise ModelCallLedgerReconcileError("source_generation_unavailable") from exc
    return "sha256:" + _json_hash(
        {
            "path": str(path.resolve()),
            "device": int(stat.st_dev),
            "inode": int(stat.st_ino),
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
            "ctime_ns": int(stat.st_ctime_ns),
        }
    )


def _user_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _user_schema_objects(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Return non-table schema owners that prevent automatic source cleanup.

    A view can retain an independent user-facing object while referencing a
    retired prompt table.  Dropping the table and then removing a database
    merely because no tables remain would silently delete that view.  Triggers
    have the same ownership ambiguity when they belong to another table.
    Reconciliation therefore classifies all user views/triggers as manual
    blockers instead of guessing whether they are safe to preserve.
    """
    return [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2] or ""),
        }
        for row in conn.execute(
            "SELECT type, name, tbl_name FROM sqlite_master "
            "WHERE type IN ('view', 'trigger') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
    ]


def _retired_identifier(value: str) -> str:
    """Return an allowlisted retired-table identifier for dynamic SQLite DDL."""
    if value not in _RETIRED_TABLES:
        raise ModelCallLedgerReconcileError("unexpected_retired_table")
    try:
        return validate_sql_identifier(value)
    except ValueError as exc:
        raise ModelCallLedgerReconcileError("unsafe_schema_identifier") from exc


def _safe_column_identifier(value: str) -> str:
    """Return a fixed metadata-column identifier used by reconciliation only."""
    if value not in _SAFE_COLUMNS:
        raise ModelCallLedgerReconcileError("unexpected_metadata_column")
    try:
        return validate_sql_identifier(value)
    except ValueError as exc:
        raise ModelCallLedgerReconcileError("unsafe_schema_identifier") from exc


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _success(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "failed", "error", "no"}
    return bool(value) if value is not None else True


def _safe_digest(value: Any, fallback: dict[str, Any]) -> str:
    raw = str(value or "").strip()
    if _HEX_SHA256.fullmatch(raw):
        return raw.lower()
    # Do not read or derive from historic prompt preview/body columns.  This
    # digest uses only the non-reversible migration metadata already selected.
    return "sha256:" + _json_hash(fallback)


def _normalize_row(
    *,
    source_db: Path,
    source_generation: str,
    source_table: str,
    source_rowid: int,
    row: sqlite3.Row,
) -> HistoricalCall:
    operation = str(row["operation"] or row["task_type"] or "legacy_model_call")
    provider = str(row["provider"] or "legacy")
    model = str(row["model"] or "unknown")
    created_at = str(row["created_at"] or "1970-01-01T00:00:00+00:00")
    input_tokens = _int(row["input_tokens"] or row["prompt_tokens"])
    output_tokens = _int(row["output_tokens"] or row["completion_tokens"])
    latency_ms = _int(row["latency_ms"])
    session_id = str(row["session_id"] or "").strip()
    success = _success(row["success"])
    base = {
        "operation": operation,
        "provider": provider,
        "model": model,
        "session_id": session_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": latency_ms,
        "success": success,
        "created_at": created_at,
    }
    input_digest = _safe_digest(row["input_digest"] or row["prompt_hash"], base)
    # A matching metadata tuple is not proof that two historical rows were
    # one provider request.  The only durable deduplication identity available
    # without reading private content is the source database/table/rowid
    # triple.  It keeps repeated reconciliation idempotent while preserving
    # distinct same-looking billable observations for manual review/accounting.
    fingerprint = "sha256:" + _json_hash(
        {
            **base,
            "input_digest": input_digest,
            "source_db": source_db.name,
            "source_generation": source_generation,
            "source_table": source_table,
            "source_rowid": source_rowid,
        }
    )
    return HistoricalCall(
        source_db=source_db.name,
        source_generation=source_generation,
        source_table=source_table,
        source_rowid=source_rowid,
        operation=operation,
        provider=provider,
        model=model,
        input_digest=input_digest,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        success=success,
        created_at=created_at,
        fingerprint=fingerprint,
        # A free-form historic session_id proves neither that the caller owns
        # that subject nor that this row contains only its data.  Without a
        # separately verified provenance receipt, importing it would invent
        # deletion authority.  Keep it in the in-memory fingerprint only and
        # require the command's explicit discard path.
        subject_scope=None,
    )


def _historical_rows_from_table(
    path: Path, conn: sqlite3.Connection, table: str, *, source_generation: str
) -> list[HistoricalCall]:
    table_identifier = _retired_identifier(table)
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_identifier})")}
    selected = [column for column in _SAFE_COLUMNS if column in columns]
    # Alias absent fields to NULL so normalization never needs to inspect raw
    # records or prompt-preview columns.
    expressions = ["rowid AS source_rowid"] + [
        f"{_safe_column_identifier(column)} AS {_safe_column_identifier(column)}"
        for column in selected
    ]
    for column in _SAFE_COLUMNS:
        if column not in columns:
            expressions.append(f"NULL AS {_safe_column_identifier(column)}")
    query = f"SELECT {', '.join(expressions)} FROM {table_identifier} ORDER BY rowid"  # nosec B608
    calls: list[HistoricalCall] = []
    for row in conn.execute(query):
        calls.append(
            _normalize_row(
                source_db=path,
                source_generation=source_generation,
                source_table=table,
                source_rowid=_int(row["source_rowid"]),
                row=row,
            )
        )
    return calls


def _source_snapshot(path: Path) -> tuple[dict[str, Any], list[HistoricalCall]]:
    report: dict[str, Any] = {
        "path": str(path),
        "exists": False,
        "source_generation": "missing",
        "integrity_check": "missing",
        "retired_tables": [],
        "other_user_tables": [],
        "other_user_schema_objects": [],
        "rows_by_table": {},
        "safe_metadata_fingerprint": "sha256:" + _json_hash([]),
        "safe_to_delete_database": False,
    }
    try:
        exists = _require_regular_sqlite_file(path, allow_missing=True)
    except ModelCallLedgerReconcileError as exc:
        report["exists"] = True
        report["error"] = _safe_reconcile_error(exc)
        return report, []
    report["exists"] = exists
    if not exists:
        try:
            _reject_orphan_sqlite_sidecars(path)
        except ModelCallLedgerReconcileError as exc:
            report["error"] = _safe_reconcile_error(exc)
        return report, []
    try:
        report["source_generation"] = _source_generation(path)
        conn = _connect_read_only(path)
        try:
            tables = _user_tables(conn)
            retired = sorted(tables & _RETIRED_TABLES)
            report["integrity_check"] = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            report["retired_tables"] = retired
            report["other_user_tables"] = sorted(tables - _RETIRED_TABLES)
            report["other_user_schema_objects"] = _user_schema_objects(conn)
            report["safe_to_delete_database"] = bool(tables) and not report[
                "other_user_tables"
            ] and not report["other_user_schema_objects"]
            calls: list[HistoricalCall] = []
            for table in retired:
                if table in _RECORD_TABLES:
                    rows = _historical_rows_from_table(
                        path,
                        conn,
                        table,
                        source_generation=str(report["source_generation"]),
                    )
                    report["rows_by_table"][table] = len(rows)
                    calls.extend(rows)
                else:
                    table_identifier = _retired_identifier(table)
                    report["rows_by_table"][table] = int(
                        conn.execute(
                            render_sql(
                                "SELECT COUNT(*) FROM {table}",
                                identifiers={"table": table_identifier},
                            )
                        ).fetchone()[0]
                        or 0
                    )
            report["safe_metadata_fingerprint"] = "sha256:" + _json_hash(
                {
                    "records": sorted(_safe_record_metadata_identity(record) for record in calls),
                    "rows_by_table": report["rows_by_table"],
                    "retired_tables": retired,
                }
            )
            return report, calls
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ModelCallLedgerReconcileError) as exc:
        report["error"] = _safe_reconcile_error(exc)
        return report, []


def _plan_fingerprint(
    calls: Iterable[HistoricalCall],
    canonical_state: str,
    source_reports: Iterable[dict[str, Any]],
) -> str:
    """Bind apply-time cleanup to the exact inspected source structure.

    A record-only fingerprint catches new or changed billable calls, but it
    would miss an owner-table change that occurs after the verified backup.
    That is not safe: a newly created retired statistics table or unrelated
    user table would be removed or left unbacked.  Fingerprint the complete
    non-content source inventory so any such drift forces a fresh dry-run and
    backup before cleanup.
    """
    source_inventory = [_source_inventory(report) for report in source_reports]
    return "sha256:" + _json_hash(
        {
            "canonical_state": canonical_state,
            "fingerprints": sorted({call.fingerprint for call in calls}),
            "source_inventory": source_inventory,
        }
    )


def _source_inventory(report: dict[str, Any]) -> dict[str, Any]:
    """Return the non-content source structure that must match before cleanup."""
    return {
        "path": str(report.get("path", "")),
        "exists": bool(report.get("exists")),
        "source_generation": str(report.get("source_generation", "missing")),
        "integrity_check": str(report.get("integrity_check", "")),
        "retired_tables": list(report.get("retired_tables", [])),
        "other_user_tables": list(report.get("other_user_tables", [])),
        "other_user_schema_objects": list(report.get("other_user_schema_objects", [])),
        "rows_by_table": dict(report.get("rows_by_table", {})),
        "safe_metadata_fingerprint": str(report.get("safe_metadata_fingerprint", "")),
        "safe_to_delete_database": bool(report.get("safe_to_delete_database")),
        "error": str(report.get("error", "")),
    }


def _source_inventory_fingerprint(report: dict[str, Any]) -> str:
    return "sha256:" + _json_hash(_source_inventory(report))


def _source_payload_fingerprint(report: dict[str, Any]) -> str:
    """Fingerprint stable retired-owner metadata after journal-mode changes."""
    inventory = _source_inventory(report)
    inventory.pop("source_generation", None)
    return "sha256:" + _json_hash(inventory)


def _retired_storage_fingerprint(report: dict[str, Any]) -> str:
    """Fingerprint only retired-owner metadata within a canonical database."""
    return "sha256:" + _json_hash(
        {
            "exists": bool(report.get("exists")),
            "retired_tables": list(report.get("retired_tables", [])),
            "rows_by_table": dict(report.get("rows_by_table", {})),
            "safe_metadata_fingerprint": str(report.get("safe_metadata_fingerprint", "")),
            "error": str(report.get("error", "")),
        }
    )
