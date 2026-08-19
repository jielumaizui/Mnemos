#!/usr/bin/env python3
"""Preview or explicitly reconcile legacy Raw IDs to native canonical IDs.

The reconciler never deletes or rewrites a Raw turn or immutable revision.  It
only records a one-to-one alias when a legacy turn-number identity and a native
identity occupy the exact same source/session/ordinal tuple.  Ambiguity stays
blocking and ``--apply`` always takes a verified SQLite backup first.
"""

from __future__ import annotations

import argparse
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
from core.sync_framework.raw_event_identity_aliases import (
    RawEventIdentityAliasError,
    apply_reconciliation,
    inspect_reconciliation,
)


def _backup(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / (
        f"{db_path.stem}.{datetime.now().strftime('%Y%m%dT%H%M%S')}."
        "pre_raw_identity_alias.sqlite"
    )
    source = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    destination = sqlite3.connect(str(target))
    try:
        source.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise RuntimeError("SQLite backup integrity_check failed")
    finally:
        destination.close()
        source.close()
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="raw_events.db path (defaults to config)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    db_path = args.db or (Path(get_config().database_dir) / "raw_events.db")
    result: dict[str, Any] = {
        "schema": "mnemos.raw_event_identity_aliases.reconcile.v1",
        "db_path": str(db_path),
        "apply": bool(args.apply),
    }
    try:
        result["before"] = inspect_reconciliation(db_path)
        if args.apply:
            if args.backup_dir is None:
                raise ValueError("--apply requires --backup-dir")
            result["backup_path"] = (
                str(_backup(db_path, args.backup_dir)) if db_path.exists() else ""
            )
            result["after"] = apply_reconciliation(db_path)
        else:
            result["after"] = result["before"]
        result["ok"] = bool(result["after"].get("ok"))
    except (OSError, sqlite3.Error, RawEventIdentityAliasError, RuntimeError, ValueError) as exc:
        result["ok"] = False
        result["error"] = str(exc)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
