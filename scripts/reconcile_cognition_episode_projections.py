#!/usr/bin/env python3
"""Plan or apply the backed-up COG-030 historical projection reconciliation."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.cognition_episode_reconciliation import (  # noqa: E402
    apply_reconciliation,
    build_reconciliation_plan,
)
from core.config import get_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-inventory-hash", default="")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    config = get_config()
    try:
        if args.apply:
            if not args.expected_inventory_hash or args.backup_dir is None:
                raise ValueError("--apply requires --expected-inventory-hash and --backup-dir")
            report = apply_reconciliation(
                config,
                expected_inventory_hash=args.expected_inventory_hash,
                backup_dir=args.backup_dir,
            )
        else:
            if args.expected_inventory_hash or args.backup_dir is not None:
                raise ValueError("inventory hash and backup directory require --apply")
            report = build_reconciliation_plan(config)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        report = {"ok": False, "error": str(exc)}
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=None if args.json else 2,
        )
    )
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
