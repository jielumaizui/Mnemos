#!/usr/bin/env python3
"""Backfill small ACL headers for typed cognitive search."""

from __future__ import annotations

import argparse
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.search_state_headers import reconcile_state_search_headers  # noqa: E402
from core.migrations.model_call_ledger_reconcile.runtime import (  # noqa: E402
    runtime_writers_are_inactive,
)
from core.ops.offline_migration_lock import offline_migration_lock  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _backup_database(source: Path, backup_dir: Path) -> dict[str, str]:
    if backup_dir.exists() and any(backup_dir.iterdir()):
        raise ValueError("backup directory must not exist or must be empty")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"producer-consumer-before-search-headers-{stamp}.db"
    with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
            integrity = str(dst.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"backup integrity check failed: {integrity}")
    return {
        "path": str(target),
        "sha256": _sha256(target),
        "integrity_check": "ok",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--failpoint", choices=("after_schema", "after_copy"), default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.db_path is None:
        from core.config import get_config

        db_path = Path(get_config().database_dir) / "producer_consumer_ledger.db"
    else:
        db_path = args.db_path.expanduser()
    if not db_path.is_file():
        payload: dict[str, Any] = {"ok": False, "error": "database_not_initialized"}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    if args.apply and args.backup_dir is None:
        payload = {"ok": False, "error": "apply_requires_backup_dir"}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    backup: dict[str, str] | None = None
    try:
        lock: AbstractContextManager[None]
        if args.apply:
            lock = offline_migration_lock(
                db_path.parent,
                daemon_check=runtime_writers_are_inactive,
            )
        else:
            lock = nullcontext()
        with lock:
            if args.apply:
                assert args.backup_dir is not None
                backup = _backup_database(db_path, args.backup_dir.expanduser())
                conn = sqlite3.connect(db_path)
            else:
                conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
            try:
                report = reconcile_state_search_headers(
                    conn,
                    apply=args.apply,
                    failpoint=args.failpoint,
                )
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            finally:
                conn.close()
        after = report["after"]
        payload = {
            "ok": bool(integrity == "ok" and (after["ok"] if args.apply else True)),
            "db_path": str(db_path),
            "backup": backup,
            "integrity_check": integrity,
            **report,
        }
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        payload = {
            "ok": False,
            "db_path": str(db_path),
            "backup": backup,
            "error": str(exc),
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
