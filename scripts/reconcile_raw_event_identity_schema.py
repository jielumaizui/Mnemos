#!/usr/bin/env python3
"""Read-only legacy Raw identity inspector.

Raw identity is coupled to canonical Raw reconstruction.  Applying it through
this former standalone runner is intentionally rejected; the exact-plan Agent
Native-to-Raw recovery owns the only supported mutation path.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config
from core.sync_framework.raw_event_identity_schema import inspect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="raw_events.db path (defaults to config)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    db_path = args.db or (Path(get_config().database_dir) / "raw_events.db")
    result: dict[str, Any] = {
        "schema": "mnemos.raw_event_identity.reconcile.v1",
        "db_path": str(db_path),
        "replacement_owner": "scripts/reconcile_agent_source_raw_capture.py",
        "replacement_apply_contract": (
            "python3 scripts/reconcile_agent_source_raw_capture.py --apply "
            "--expected-plan-hash <sha256> --backup-dir <dir> --json"
        ),
    }
    try:
        result["before"] = inspect(db_path)
        if args.apply:
            result["after"] = result["before"]
            raise ValueError("raw_identity_apply_owned_by_agent_source_raw_recovery")
        else:
            result["after"] = result["before"]
        result["ok"] = result["after"].get("status") in {"current", "uninitialized"}
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        result["ok"] = False
        result["error"] = str(exc)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
