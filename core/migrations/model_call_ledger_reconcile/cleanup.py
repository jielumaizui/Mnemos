"""Verified local backup comparison and retired-storage cleanup."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from core.telemetry.model_call_ledger.migration import LedgerReconciliation, LedgerReconciliationSession
from core.db_utils import render_sql, validate_sql_identifier

from .contracts import (
    RECORD_TABLES as _RECORD_TABLES,
    RETIRED_TABLES as _RETIRED_TABLES,
    SQLITE_SIDECAR_SUFFIXES as _SQLITE_SIDECAR_SUFFIXES,
    HistoricalCall,
    ModelCallLedgerReconcileError,
    json_hash as _json_hash,
    safe_record_metadata_identity as _safe_record_metadata_identity,
)
from .inventory import (
    _connect_read_only,
    _historical_rows_from_table,
    _require_regular_sqlite_file,
    _retired_identifier,
    _retired_storage_fingerprint,
    _source_generation,
    _source_inventory_fingerprint,
    _source_payload_fingerprint,
    _user_schema_objects,
    _user_tables,
)


def source_inventory_from_connection(path: Path, conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _user_tables(conn)
    retired = sorted(tables & _RETIRED_TABLES)
    source_generation = _source_generation(path)
    records: list[HistoricalCall] = []
    rows_by_table = {
        table: int(
            conn.execute(f"SELECT COUNT(*) FROM {_retired_identifier(table)}").fetchone()[0] or 0  # nosec B608
        )
        for table in retired
    }
    for table in retired:
        if table in _RECORD_TABLES:
            records.extend(
                _historical_rows_from_table(
                    path,
                    conn,
                    table,
                    source_generation=source_generation,
                )
            )
    return {
        "path": str(path),
        "exists": True,
        "source_generation": source_generation,
        "integrity_check": str(conn.execute("PRAGMA integrity_check").fetchone()[0]),
        "retired_tables": retired,
        "other_user_tables": sorted(tables - _RETIRED_TABLES),
        "other_user_schema_objects": _user_schema_objects(conn),
        "rows_by_table": rows_by_table,
        "safe_metadata_fingerprint": "sha256:" + _json_hash(
            {
                "records": sorted(_safe_record_metadata_identity(record) for record in records),
                "rows_by_table": rows_by_table,
                "retired_tables": retired,
            }
        ),
        "safe_to_delete_database": bool(tables)
        and not bool(tables - _RETIRED_TABLES)
        and not bool(_user_schema_objects(conn)),
        "error": "",
    }


def _retired_tables_from_inventory(report: dict[str, Any]) -> list[str]:
    tables = report.get("retired_tables")
    if not isinstance(tables, list) or not all(isinstance(table, str) for table in tables):
        raise ModelCallLedgerReconcileError("source_inventory_invalid")
    return tables


_BACKUP_SNAPSHOT_ALIAS = "mcl_verified_backup"


def _retired_table_columns_for_snapshot(
    conn: sqlite3.Connection,
    *,
    schema: str,
    table: str,
) -> tuple[str, ...]:
    """Return auditable visible columns or fail before a destructive cleanup.

    The comparator deliberately supports only ordinary rowid tables with
    identifier-safe, visible columns.  A more exotic retired table is still
    recoverable from its private backup, but is not safe to auto-drop until a
    purpose-built migration can prove an equivalent snapshot.
    """
    table_identifier = _retired_identifier(table)
    rows = conn.execute(
        f"PRAGMA {schema}.table_xinfo({table_identifier})"  # nosec B608 - fixed schema alias.
    ).fetchall()
    if not rows or any(len(row) > 6 and int(row[6] or 0) != 0 for row in rows):
        raise ModelCallLedgerReconcileError("retired_snapshot_schema_unsupported")
    names = tuple(str(row[1]) for row in rows)
    if not names:
        raise ModelCallLedgerReconcileError("retired_snapshot_schema_unsupported")
    try:
        return tuple(validate_sql_identifier(name) for name in names)
    except ValueError as exc:
        raise ModelCallLedgerReconcileError("retired_snapshot_schema_unsupported") from exc


def _retired_table_uses_rowid(
    conn: sqlite3.Connection,
    *,
    schema: str,
    table: str,
    columns: tuple[str, ...],
) -> str:
    """Choose an unshadowed SQLite rowid alias, otherwise fail closed."""
    table_identifier = _retired_identifier(table)
    names = {name.lower() for name in columns}
    for candidate in ("rowid", "_rowid_", "oid"):
        if candidate in names:
            continue
        try:
            conn.execute(
                f"SELECT {candidate} FROM {schema}.{table_identifier} LIMIT 0"  # nosec B608
            )
        except sqlite3.Error:
            continue
        return candidate
    raise ModelCallLedgerReconcileError("retired_snapshot_schema_unsupported")


def _retired_table_schema_matches_snapshot(
    conn: sqlite3.Connection,
    *,
    table: str,
) -> bool:
    """Compare table/index/trigger DDL inside SQLite without exporting SQL text."""
    difference_count = conn.execute(
        """
        WITH current_objects AS (
            SELECT type, name, tbl_name, sql
            FROM main.sqlite_master
            WHERE (type='table' AND name=?)
               OR (type IN ('index', 'trigger') AND tbl_name=?)
        ), backup_objects AS (
            SELECT type, name, tbl_name, sql
            FROM mcl_verified_backup.sqlite_master
            WHERE (type='table' AND name=?)
               OR (type IN ('index', 'trigger') AND tbl_name=?)
        ), forward_difference AS (
            SELECT * FROM current_objects EXCEPT SELECT * FROM backup_objects
        ), reverse_difference AS (
            SELECT * FROM backup_objects EXCEPT SELECT * FROM current_objects
        )
        SELECT (SELECT COUNT(*) FROM forward_difference)
             + (SELECT COUNT(*) FROM reverse_difference)
        """,
        (table, table, table, table),
    ).fetchone()[0]
    return int(difference_count or 0) == 0


def _retired_table_rows_match_snapshot(
    conn: sqlite3.Connection,
    *,
    table: str,
    columns: tuple[str, ...],
    rowid_alias: str,
) -> bool:
    """Compare a retired rowid table as a multiset without reading raw cells.

    ``EXCEPT`` normally has set semantics.  Including the physical rowid
    makes each selectable row unique, so the bidirectional difference catches
    changed values, additions, removals, and duplicate-count changes without
    returning any prompt/response value to Python.
    """
    table_identifier = _retired_identifier(table)
    selected = ", ".join(columns)
    current_count = int(
        conn.execute(f"SELECT COUNT(*) FROM main.{table_identifier}").fetchone()[0] or 0  # nosec B608
    )
    backup_count = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {_BACKUP_SNAPSHOT_ALIAS}.{table_identifier}"  # nosec B608
        ).fetchone()[0]
        or 0
    )
    if current_count != backup_count:
        return False
    difference = conn.execute(
        f"""
        WITH current_rows AS (
            SELECT {rowid_alias} AS mcl_snapshot_rowid, {selected}
            FROM main.{table_identifier}
        ), backup_rows AS (
            SELECT {rowid_alias} AS mcl_snapshot_rowid, {selected}
            FROM {_BACKUP_SNAPSHOT_ALIAS}.{table_identifier}
        ), forward_difference AS (
            SELECT * FROM current_rows EXCEPT SELECT * FROM backup_rows
        ), reverse_difference AS (
            SELECT * FROM backup_rows EXCEPT SELECT * FROM current_rows
        )
        SELECT EXISTS(
            SELECT 1 FROM forward_difference
            UNION ALL
            SELECT 1 FROM reverse_difference
        )
        """,  # nosec B608 - identifiers are fixed/validated above.
    ).fetchone()[0]
    return not bool(difference)


def _assert_retired_storage_matches_verified_backup(
    conn: sqlite3.Connection,
    *,
    backup_path: Path,
    backup_identity: str,
    expected_tables: Iterable[str],
    mismatch_error: str,
) -> None:
    """Prove live retired owners equal their immutable private backup.

    Filesystem generation facts are only a drift signal; a WAL writer can
    alter an unselected raw field without changing the main database's stat
    fingerprint before a journal switch checkpoints it.  The destructive
    path therefore compares the full retired table schema and every row,
    inside SQLite while its source write transaction is held.  The backup's
    byte identity is checked before attach and again immediately before the
    caller releases any source cell.  Neither raw values nor raw hashes leave
    this helper.
    """
    private_identity = LedgerReconciliation.backup_identity(backup_path)
    if private_identity != backup_identity:
        raise ModelCallLedgerReconcileError("verified_backup_identity_changed")
    expected = sorted(str(table) for table in expected_tables)
    if set(expected) - _RETIRED_TABLES:
        raise ModelCallLedgerReconcileError("unexpected_retired_table")
    try:
        conn.execute(f"ATTACH DATABASE ? AS {_BACKUP_SNAPSHOT_ALIAS}", (str(backup_path),))
        backup_tables = {
            str(row[0])
            for row in conn.execute(
                render_sql(
                    "SELECT name FROM {catalog} "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'",
                    identifiers={
                        "catalog": _BACKUP_SNAPSHOT_ALIAS + ".sqlite_master"
                    },
                )
            ).fetchall()
        }
        current_tables = _user_tables(conn)
        current_retired = sorted(current_tables & _RETIRED_TABLES)
        backup_retired = sorted(backup_tables & _RETIRED_TABLES)
        if current_retired != expected or backup_retired != expected:
            raise ModelCallLedgerReconcileError(mismatch_error)
        for table in expected:
            current_columns = _retired_table_columns_for_snapshot(
                conn, schema="main", table=table
            )
            backup_columns = _retired_table_columns_for_snapshot(
                conn, schema=_BACKUP_SNAPSHOT_ALIAS, table=table
            )
            if current_columns != backup_columns or not _retired_table_schema_matches_snapshot(
                conn, table=table
            ):
                raise ModelCallLedgerReconcileError(mismatch_error)
            current_rowid = _retired_table_uses_rowid(
                conn, schema="main", table=table, columns=current_columns
            )
            backup_rowid = _retired_table_uses_rowid(
                conn,
                schema=_BACKUP_SNAPSHOT_ALIAS,
                table=table,
                columns=backup_columns,
            )
            if current_rowid != backup_rowid or not _retired_table_rows_match_snapshot(
                conn,
                table=table,
                columns=current_columns,
                rowid_alias=current_rowid,
            ):
                raise ModelCallLedgerReconcileError(mismatch_error)
        if LedgerReconciliation.backup_identity(backup_path) != backup_identity:
            raise ModelCallLedgerReconcileError("verified_backup_identity_changed")
    finally:
        try:
            conn.execute(f"DETACH DATABASE {_BACKUP_SNAPSHOT_ALIAS}")
        except sqlite3.Error:
            # ``DETACH`` is not valid while the caller's write transaction is
            # active.  It will be retried by the post-commit cleanup below;
            # do not hide the authoritative comparison result here.
            pass


def cleanup_source_database(
    path: Path,
    *,
    expected_report: dict[str, Any] | None = None,
    verified_backup_path: Path | None = None,
    verified_backup_identity: str | None = None,
) -> dict[str, Any]:
    """Drop only the exact pre-backed-up retired owner inventory.

    Every source is rechecked under its own write transaction immediately
    before DDL.  A cleanup failure can therefore be resumed safely from the
    verified backup manifest without deleting source drift that arrived after
    the original plan.
    """
    if not _require_regular_sqlite_file(path, allow_missing=True):
        observed = {
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
            "error": "",
        }
        if (
            expected_report
            and _source_inventory_fingerprint(expected_report)
            != _source_inventory_fingerprint(observed)
        ):
            raise ModelCallLedgerReconcileError("source_drift_before_cleanup")
        return {"path": str(path), "dropped_tables": [], "database_removed": False}
    with sqlite3.connect(str(path), timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        pre_journal_observed = source_inventory_from_connection(path, conn)
        if (
            expected_report
            and _source_inventory_fingerprint(expected_report)
            != _source_inventory_fingerprint(pre_journal_observed)
        ):
            raise ModelCallLedgerReconcileError("source_drift_before_cleanup")
        # Clear any stale WAL before freeing retired prompt cells.  A busy
        # reader makes the switch fail, in which case the table remains and
        # health continues to report the unreconciled owner instead of a
        # false-green cleanup result.
        journal_mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        if journal_mode is None or str(journal_mode[0] or "").lower() != "delete":
            raise ModelCallLedgerReconcileError("sqlite_private_cleanup_requires_journal_mode_delete")
        # Changing journal mode legitimately changes mtime/ctime, so compare
        # the physical generation captured *after* that transition as well as
        # the metadata payload below.  Otherwise a writer could replace an
        # unselected raw prompt/stat value in the narrow gap before we acquire
        # BEGIN IMMEDIATE; row counts and selected metadata would still match
        # while the cleanup deleted unbacked bytes.
        post_journal_generation = _source_generation(path)
        conn.execute("BEGIN IMMEDIATE")
        observed = source_inventory_from_connection(path, conn)
        if observed["source_generation"] != post_journal_generation:
            conn.rollback()
            raise ModelCallLedgerReconcileError("source_drift_before_cleanup")
        if (
            expected_report
            and _source_payload_fingerprint(expected_report)
            != _source_payload_fingerprint(observed)
        ):
            conn.rollback()
            raise ModelCallLedgerReconcileError("source_drift_before_cleanup")
        if observed["other_user_schema_objects"]:
            conn.rollback()
            raise ModelCallLedgerReconcileError("source_drift_before_cleanup")
        expected_tables = _retired_tables_from_inventory(observed)
        verified_backup: Path | None = None
        if expected_tables:
            if verified_backup_path is None or not verified_backup_identity:
                conn.rollback()
                raise ModelCallLedgerReconcileError("verified_backup_required_for_private_cleanup")
            verified_backup = verified_backup_path
            _assert_retired_storage_matches_verified_backup(
                conn,
                backup_path=verified_backup,
                backup_identity=verified_backup_identity,
                expected_tables=expected_tables,
                mismatch_error="source_drift_before_cleanup",
            )
        # Retired tables may contain raw prompt/response fields that this
        # reconciler deliberately never selects.  SQLite must confirm secure
        # deletion *before* their cells are released; otherwise DROP TABLE can
        # leave that private content in live database slack when an unrelated
        # owner keeps the file around.
        conn.execute("PRAGMA secure_delete=ON")
        secure_delete = conn.execute("PRAGMA secure_delete").fetchone()
        if secure_delete is None or int(secure_delete[0] or 0) != 1:
            conn.rollback()
            raise ModelCallLedgerReconcileError("sqlite_secure_delete_unavailable")
        drop_tables = _retired_tables_from_inventory(observed)
        for table in drop_tables:
            conn.execute(f"DROP TABLE {_retired_identifier(table)}")  # nosec B608
        if verified_backup is not None and (
            LedgerReconciliation.backup_identity(verified_backup)
            != verified_backup_identity
        ):
            conn.rollback()
            raise ModelCallLedgerReconcileError("verified_backup_identity_changed")
        conn.execute("COMMIT")
        try:
            conn.execute(f"DETACH DATABASE {_BACKUP_SNAPSHOT_ALIAS}")
        except sqlite3.Error:
            pass
        remaining = _user_tables(conn)
        remaining_schema_objects = _user_schema_objects(conn)
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise ModelCallLedgerReconcileError("legacy_cleanup_integrity_failed")
    removed = False
    if not remaining and not remaining_schema_objects:
        for candidate in (path, *(Path(str(path) + suffix) for suffix in _SQLITE_SIDECAR_SUFFIXES)):
            # trusted-scan: manual_repair owner=model_call_ledger target=retired_sqlite_source expires=never
            candidate.unlink(missing_ok=True)
        removed = True
    return {
        "path": str(path),
        "dropped_tables": drop_tables,
        "remaining_user_tables": sorted(remaining),
        "remaining_user_schema_objects": remaining_schema_objects,
        "database_removed": removed,
        "integrity_check": integrity,
    }


def cleanup_canonical_retired_storage(
    session: LedgerReconciliationSession,
    *,
    expected_report: dict[str, Any],
    verified_backup_path: Path | None = None,
    verified_backup_identity: str | None = None,
) -> dict[str, Any]:
    """Drop counted canonical retired owners under the live backup capability.

    The canonical file cannot be treated like an external source database: it
    retains the upgraded ledger.  This function therefore validates the live
    lexical authorization, rechecks only the retired tables' safe metadata
    under lock, securely drops exactly those allowlisted owners, and proves the
    remaining canonical schema is runtime-valid before committing.
    """
    source = session.canonical_path
    expected_tables = sorted(str(table) for table in expected_report.get("retired_tables", []))
    if not expected_tables:
        return {"path": str(source), "dropped_tables": [], "database_removed": False}
    if set(expected_tables) - _RETIRED_TABLES:
        raise ModelCallLedgerReconcileError("unexpected_canonical_retired_table")
    if verified_backup_path is None or not verified_backup_identity:
        raise ModelCallLedgerReconcileError("verified_backup_required_for_private_cleanup")
    session.require_cleanup_ready()
    _require_regular_sqlite_file(source)
    with sqlite3.connect(str(source), timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        before = source_inventory_from_connection(source, conn)
        if before["other_user_schema_objects"]:
            raise ModelCallLedgerReconcileError("canonical_retired_source_drift_before_cleanup")
        if _retired_storage_fingerprint(before) != _retired_storage_fingerprint(expected_report):
            raise ModelCallLedgerReconcileError("canonical_retired_source_drift_before_cleanup")
        journal_mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        if journal_mode is None or str(journal_mode[0] or "").lower() != "delete":
            raise ModelCallLedgerReconcileError(
                "sqlite_private_cleanup_requires_journal_mode_delete"
            )
        # See the equivalent external-source guard above.  The canonical
        # ledger retains its file after cleanup, so a raw retired-table cell
        # changed after the verified backup must be detected before DROP.
        post_journal_generation = _source_generation(source)
        conn.execute("BEGIN IMMEDIATE")
        observed = source_inventory_from_connection(source, conn)
        if observed["source_generation"] != post_journal_generation:
            conn.rollback()
            raise ModelCallLedgerReconcileError(
                "canonical_retired_source_drift_before_cleanup"
            )
        if _retired_storage_fingerprint(observed) != _retired_storage_fingerprint(expected_report):
            conn.rollback()
            raise ModelCallLedgerReconcileError("canonical_retired_source_drift_before_cleanup")
        if observed["other_user_schema_objects"]:
            conn.rollback()
            raise ModelCallLedgerReconcileError("canonical_retired_source_drift_before_cleanup")
        if sorted(observed["retired_tables"]) != expected_tables:
            conn.rollback()
            raise ModelCallLedgerReconcileError("canonical_retired_table_inventory_drift")
        _assert_retired_storage_matches_verified_backup(
            conn,
            backup_path=verified_backup_path,
            backup_identity=verified_backup_identity,
            expected_tables=expected_tables,
            mismatch_error="canonical_retired_source_drift_before_cleanup",
        )
        conn.execute("PRAGMA secure_delete=ON")
        secure_delete = conn.execute("PRAGMA secure_delete").fetchone()
        if secure_delete is None or int(secure_delete[0] or 0) != 1:
            conn.rollback()
            raise ModelCallLedgerReconcileError("sqlite_secure_delete_unavailable")
        for table in expected_tables:
            conn.execute(f"DROP TABLE {_retired_identifier(table)}")  # nosec B608
        if LedgerReconciliation.backup_identity(verified_backup_path) != verified_backup_identity:
            conn.rollback()
            raise ModelCallLedgerReconcileError("verified_backup_identity_changed")
        session.assert_runtime_valid(conn)
        conn.execute("COMMIT")
        try:
            conn.execute(f"DETACH DATABASE {_BACKUP_SNAPSHOT_ALIAS}")
        except sqlite3.Error:
            pass
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        raise ModelCallLedgerReconcileError("canonical_retired_cleanup_integrity_failed")
    session.complete_canonical_cleanup()
    verify = _connect_read_only(source)
    try:
        remaining_user_tables = sorted(_user_tables(verify))
    finally:
        verify.close()
    return {
        "path": str(source),
        "dropped_tables": expected_tables,
        "remaining_user_tables": remaining_user_tables,
        "database_removed": False,
        "integrity_check": integrity,
    }
