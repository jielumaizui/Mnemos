#!/usr/bin/env python3
"""Remove one explicitly identified foreign path root from a Raw index.

Projection indexes are vault-owned.  This reconciler is intentionally narrow:
it only removes rows whose absolute path is the exact requested root or a
descendant, and it requires a verified SQLite backup before changing data.
It never reads or reports indexed content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config


SCHEMA_VERSION = "mnemos.raw_index_path_reconcile.v1"
_REQUIRED_TABLES = frozenset({"raw_index", "raw_fts", "raw_tags"})
_PATH_PREFIX_PREDICATE = (
    "(abs_path=? OR (substr(abs_path, 1, length(?))=? "
    "AND substr(abs_path, length(?)+1, 1)='/'))"
)
_CANDIDATE_ROWS_QUERY = (
    "SELECT id, file_path FROM raw_index WHERE "
    + _PATH_PREFIX_PREDICATE
    + " ORDER BY id"
)
_FTS_MATCH_COUNT_QUERY = (
    "SELECT COUNT(*) FROM raw_fts "
    "WHERE rowid IN (SELECT id FROM raw_index WHERE "
    + _PATH_PREFIX_PREDICATE
    + ")"
)
_INSERT_RECONCILE_TARGETS_QUERY = (
    "INSERT INTO raw_index_path_reconcile_targets (id, file_path) "
    "SELECT id, file_path FROM raw_index WHERE "
    + _PATH_PREFIX_PREDICATE
)
_COUNT_QUERIES = {
    "raw_index": "SELECT COUNT(*) FROM raw_index",
    "raw_fts": "SELECT COUNT(*) FROM raw_fts",
    "raw_index_path_reconcile_targets": (
        "SELECT COUNT(*) FROM raw_index_path_reconcile_targets"
    ),
}


class RawIndexPathReconcileError(RuntimeError):
    """Raised when a narrow Raw-index path cleanup cannot be proven safe."""


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _normalise_prefix(value: str | Path) -> str:
    prefix = str(value).rstrip("/")
    if not prefix or prefix == ".":
        raise RawIndexPathReconcileError("remove path prefix must be non-empty")
    return prefix


def _prefix_params(prefix: str) -> tuple[str, str, str, str]:
    # Use substring boundaries rather than LIKE so '%' and '_' in a user path
    # can never broaden the deletion set.
    return (prefix, prefix, prefix, prefix)


def _candidate_rows(conn: sqlite3.Connection, prefix: str) -> list[tuple[int, str]]:
    params = _prefix_params(prefix)
    return [
        (int(row[0]), str(row[1]))
        for row in conn.execute(_CANDIDATE_ROWS_QUERY, params).fetchall()
    ]


def _safe_count(conn: sqlite3.Connection, table: str) -> int:
    query = _COUNT_QUERIES.get(table)
    if query is None:
        raise RawIndexPathReconcileError(f"unsupported count table: {table}")
    return int(conn.execute(query).fetchone()[0])


def inspect_index(db_path: Path | str, *, remove_abs_prefix: str | Path) -> dict[str, Any]:
    """Inspect one exact removable root without mutating the index."""
    path = Path(db_path).expanduser()
    prefix = _normalise_prefix(remove_abs_prefix)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "db_path": str(path),
        "remove_abs_prefix": prefix,
    }
    if not path.is_file():
        return {
            **report,
            "status": "uninitialized",
            "candidate_rows": 0,
            "ok": True,
        }
    try:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
            tables = _tables(conn)
            missing = sorted(_REQUIRED_TABLES - tables)
            if missing:
                return {
                    **report,
                    "status": "schema_incompatible",
                    "missing_tables": missing,
                    "candidate_rows": 0,
                    "ok": False,
                }
            candidates = _candidate_rows(conn, prefix)
            candidate_ids = {row[0] for row in candidates}
            fts_matches = 0
            if candidate_ids:
                fts_matches = int(
                    conn.execute(
                        _FTS_MATCH_COUNT_QUERY,
                        _prefix_params(prefix),
                    ).fetchone()[0]
                )
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            raw_index_rows = _safe_count(conn, "raw_index")
            raw_fts_rows = _safe_count(conn, "raw_fts")
            integrity_ok = bool(integrity and str(integrity[0]) == "ok")
            fts_consistent = raw_fts_rows == raw_index_rows
            status = "reconciliation_required" if candidates else "clean"
            if not integrity_ok or not fts_consistent:
                status = "index_inconsistent"
            return {
                **report,
                "status": status,
                "candidate_rows": len(candidates),
                "candidate_fts_rows": fts_matches,
                "candidate_file_path_hash": hashlib.sha256(
                    "\n".join(sorted(path for _id, path in candidates)).encode("utf-8")
                ).hexdigest(),
                "raw_index_rows": raw_index_rows,
                "raw_fts_rows": raw_fts_rows,
                "fts_row_count_matches_index": fts_consistent,
                "integrity_check": str(integrity[0]) if integrity else "missing",
                "ok": integrity_ok and fts_consistent,
            }
    except (OSError, sqlite3.Error) as exc:
        return {
            **report,
            "status": "unreadable",
            "candidate_rows": 0,
            "ok": False,
            "error": exc.__class__.__name__,
        }


def _backup(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / (
        f"{db_path.stem}.{datetime.now().strftime('%Y%m%dT%H%M%S')}."
        "pre_raw_index_path_reconcile.sqlite"
    )
    source = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise RawIndexPathReconcileError("SQLite backup integrity_check failed")
    finally:
        destination.close()
        source.close()
    return target


def apply_index_cleanup(
    db_path: Path | str,
    *,
    remove_abs_prefix: str | Path,
    vacuum: bool = False,
) -> dict[str, Any]:
    """Delete only proven foreign root rows and their owned FTS/tag entries."""
    path = Path(db_path).expanduser()
    prefix = _normalise_prefix(remove_abs_prefix)
    before = inspect_index(path, remove_abs_prefix=prefix)
    if before["status"] == "uninitialized":
        return {**before, "applied_rows": 0, "vacuumed": False}
    if before["status"] != "reconciliation_required":
        if before.get("ok"):
            return {**before, "applied_rows": 0, "vacuumed": False}
        raise RawIndexPathReconcileError(f"cannot reconcile Raw index: {before}")

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        params = _prefix_params(prefix)
        conn.execute(
            "CREATE TEMP TABLE raw_index_path_reconcile_targets ("
            "id INTEGER PRIMARY KEY, file_path TEXT NOT NULL)"
        )
        conn.execute(
            _INSERT_RECONCILE_TARGETS_QUERY,
            params,
        )
        selected = _safe_count(conn, "raw_index_path_reconcile_targets")
        if selected != int(before["candidate_rows"]):
            raise RawIndexPathReconcileError("Raw-index candidate set changed during cleanup")
        fts_deleted = conn.execute(
            "DELETE FROM raw_fts WHERE rowid IN "
            "(SELECT id FROM raw_index_path_reconcile_targets)"
        ).rowcount
        tags_deleted = conn.execute(
            "DELETE FROM raw_tags WHERE file_path IN "
            "(SELECT file_path FROM raw_index_path_reconcile_targets)"
        ).rowcount
        raw_deleted = conn.execute(
            "DELETE FROM raw_index WHERE id IN "
            "(SELECT id FROM raw_index_path_reconcile_targets)"
        ).rowcount
        if int(raw_deleted or 0) != selected:
            raise RawIndexPathReconcileError("Raw-index cleanup did not delete its exact target set")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise RawIndexPathReconcileError("Raw-index cleanup failed integrity_check")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    if vacuum:
        with sqlite3.connect(str(path)) as vacuum_conn:
            vacuum_conn.execute("VACUUM")
    after = inspect_index(path, remove_abs_prefix=prefix)
    if not after.get("ok") or int(after["candidate_rows"]) != 0:
        raise RawIndexPathReconcileError(f"Raw-index cleanup did not converge: {after}")
    return {
        "before": before,
        "after": after,
        "applied_rows": int(raw_deleted or 0),
        "raw_fts_rows_deleted": int(fts_deleted or 0),
        "raw_tags_rows_deleted": int(tags_deleted or 0),
        "vacuumed": bool(vacuum),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="raw_index.db path (defaults to config)")
    parser.add_argument("--remove-abs-prefix", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--vacuum", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    db_path = args.db or (Path(get_config().database_dir) / "raw_index.db")
    result: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "db_path": str(db_path),
        "apply": bool(args.apply),
    }
    try:
        result["before"] = inspect_index(
            db_path, remove_abs_prefix=args.remove_abs_prefix
        )
        if args.apply:
            if args.backup_dir is None:
                raise ValueError("--apply requires --backup-dir")
            if int(result["before"].get("candidate_rows") or 0) > 0:
                result["backup_path"] = str(_backup(Path(db_path), args.backup_dir))
            else:
                result["backup_path"] = ""
            result["result"] = apply_index_cleanup(
                db_path,
                remove_abs_prefix=args.remove_abs_prefix,
                vacuum=args.vacuum,
            )
            result["after"] = result["result"]["after"]
        else:
            result["after"] = result["before"]
        result["ok"] = bool(result["after"].get("ok")) and int(
            result["after"].get("candidate_rows") or 0
        ) == 0
    except (OSError, sqlite3.Error, RawIndexPathReconcileError, RuntimeError, ValueError) as exc:
        result["ok"] = False
        result["error"] = str(exc)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
