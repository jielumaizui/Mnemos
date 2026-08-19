#!/usr/bin/env python3
"""Inspect or explicitly migrate distillation chunk checkpoints.

Dry-run is the default and opens the database read-only. ``--apply`` creates a
verified SQLite backup before the transactional schema migration. Legacy rows
remain present but deliberately lack an execution spec, so they can never be
reused as current output and will be recomputed on the next retry.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.hephaestus.chunk_checkpoint import ChunkCheckpointStore  # noqa: E402


def _connect_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _schema_state(columns: list[sqlite3.Row]) -> str:
    names = {str(row[1]) for row in columns}
    primary_key = [
        str(row[1])
        for row in sorted(columns, key=lambda item: int(item[5] or 0))
        if int(row[5] or 0) > 0
    ]
    if not names:
        return "missing_table"
    if {
        "execution_spec_hash",
        "execution_spec_json",
    }.issubset(names) and primary_key == ["session_id", "chunk_index", "chunk_hash"]:
        return "execution_spec_v2"
    if primary_key == ["session_id", "chunk_index"]:
        return "legacy_v1"
    return "unknown"


def inspect_checkpoint_db(db_path: Path) -> dict[str, Any]:
    """Return a privacy-safe, read-only checkpoint migration report."""
    path = Path(db_path).expanduser()
    if not path.is_file():
        return {
            "exists": False,
            "schema_state": "missing_database",
            "rows": 0,
            "affected_sessions": 0,
            "legacy_rows": 0,
            "statuses": {},
            "integrity": "missing",
        }

    with _connect_read_only(path) as conn:
        columns = conn.execute("PRAGMA table_info(distill_chunk_results)").fetchall()
        state = _schema_state(columns)
        report: dict[str, Any] = {
            "exists": True,
            "schema_state": state,
            "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
            "rows": 0,
            "affected_sessions": 0,
            "legacy_rows": 0,
            "statuses": {},
            "integrity": str(conn.execute("PRAGMA integrity_check").fetchone()[0]),
        }
        if state == "missing_table":
            return report
        report["rows"] = int(
            conn.execute("SELECT COUNT(*) FROM distill_chunk_results").fetchone()[0]
        )
        report["statuses"] = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT status, COUNT(*) FROM distill_chunk_results GROUP BY status"
            )
        }
        if state == "legacy_v1":
            report["legacy_rows"] = report["rows"]
            report["affected_sessions"] = int(
                conn.execute(
                    "SELECT COUNT(DISTINCT session_id) FROM distill_chunk_results"
                ).fetchone()[0]
            )
        elif state == "execution_spec_v2":
            report["legacy_rows"] = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM distill_chunk_results
                    WHERE execution_spec_hash = '' OR execution_spec_json IN ('', '{}')
                    """
                ).fetchone()[0]
            )
            report["affected_sessions"] = int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT session_id) FROM distill_chunk_results
                    WHERE execution_spec_hash = '' OR execution_spec_json IN ('', '{}')
                    """
                ).fetchone()[0]
            )
        return report


def _backup_database(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_path = backup_dir / f"{db_path.stem}.pre-execution-spec.{stamp}.db"
    with _connect_read_only(db_path) as source, sqlite3.connect(str(backup_path)) as target:
        source.backup(target)
    with _connect_read_only(backup_path) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        backup_path.unlink(missing_ok=True)
        raise RuntimeError(f"checkpoint backup integrity failed: {integrity}")
    return backup_path


def migrate_checkpoint_db(db_path: Path, backup_dir: Path) -> dict[str, Any]:
    """Back up and migrate one checkpoint database without deleting old rows."""
    path = Path(db_path).expanduser()
    before = inspect_checkpoint_db(path)
    if not before["exists"]:
        raise FileNotFoundError(path)
    if before["schema_state"] == "execution_spec_v2":
        return {"migrated": False, "backup_path": None, "before": before, "after": before}
    if before["schema_state"] != "legacy_v1":
        raise RuntimeError(f"unsupported checkpoint schema: {before['schema_state']}")

    backup_path = _backup_database(path, Path(backup_dir).expanduser())
    migrated = ChunkCheckpointStore(path).migrate_schema()
    after = inspect_checkpoint_db(path)
    if not migrated or after["schema_state"] != "execution_spec_v2":
        raise RuntimeError("checkpoint schema migration did not reach execution_spec_v2")
    if before["rows"] != after["rows"] or after["integrity"] != "ok":
        raise RuntimeError("checkpoint migration row-count or integrity verification failed")
    return {
        "migrated": True,
        "backup_path": backup_path,
        "before": before,
        "after": after,
        "recompute_policy": "legacy rows are invalid and recompute on next retry",
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(child) for key, child in value.items()}
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="checkpoint DB; defaults to configured DB dir")
    parser.add_argument("--backup-dir", type=Path, help="backup directory used by --apply")
    parser.add_argument("--apply", action="store_true", help="back up and migrate legacy schema")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    cfg = get_config()
    db_path = (args.db or (Path(cfg.database_dir) / "distillation_chunks.db")).expanduser()
    if args.apply:
        backup_dir = args.backup_dir or (
            Path(cfg.data_dir) / "backups" / "distill-checkpoint-execution-spec"
        )
        result = migrate_checkpoint_db(db_path, backup_dir)
    else:
        result = {"dry_run": True, "report": inspect_checkpoint_db(db_path)}
    payload = _jsonable(result)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
