#!/usr/bin/env python3
"""Inspect or explicitly migrate ``action_ledger.db`` to append-only v1."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ops.action_ledger_schema import reconcile_action_ledger_schema  # noqa: E402
from core.migrations.model_call_ledger_reconcile.runtime import (  # noqa: E402
    runtime_writers_are_inactive,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _backup(source: Path, backup_dir: Path) -> dict[str, str]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"action-ledger-before-append-only-v1-{stamp}.db"
    if target.exists():
        raise FileExistsError(f"backup already exists: {target}")
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
            integrity = str(dst.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"backup integrity check failed: {integrity}")
    return {"path": str(target), "sha256": _sha256(target), "integrity_check": "ok"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.db_path is None:
        from core.config import get_config

        db_path = Path(get_config().database_dir) / "action_ledger.db"
    else:
        db_path = args.db_path
    if not db_path.is_file():
        print(json.dumps({"ok": False, "error": "action ledger is not initialized"}))
        return 2
    if args.apply and args.backup_dir is None:
        print(json.dumps({"ok": False, "error": "--apply requires --backup-dir"}))
        return 2
    if args.apply and not runtime_writers_are_inactive(db_path.parent):
        print(json.dumps({"ok": False, "error": "daemon_not_inactive"}))
        return 2
    backup: dict[str, str] | None = None
    try:
        if args.apply:
            backup = _backup(db_path, args.backup_dir)
            conn = sqlite3.connect(db_path)
        else:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            report = reconcile_action_ledger_schema(conn, apply=args.apply)
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            conn.close()
        after = report["after"] if args.apply else report["before"]
        payload = {
            "ok": bool(
                integrity == "ok"
                and (
                    after.get("ok")
                    or after.get("classification") in {"legacy_mutable_v0", "absent"}
                )
            ),
            "db_path": str(db_path),
            "backup": backup,
            "integrity_check": integrity,
            **report,
        }
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        payload = {
            "ok": False,
            "db_path": str(db_path),
            "backup": backup,
            "error": str(exc),
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
