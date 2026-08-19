#!/usr/bin/env python3
"""Repair the safe subset of Mnemos SQLite disk-budget findings."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.ops.sqlite_disk_budget import repair_sqlite_disk_budget  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Checkpoint SQLite WAL files and delete stale Mnemos temp files."
    )
    parser.add_argument("--apply", action="store_true", help="apply safe repairs")
    parser.add_argument("--dry-run", action="store_true", help="preview repairs")
    parser.add_argument("--wal", action="store_true", help="include WAL checkpoint")
    parser.add_argument("--temp", action="store_true", help="include stale temp cleanup")
    args = parser.parse_args(argv)

    include_wal = bool(args.wal or not args.temp)
    include_temp = bool(args.temp or not args.wal)
    payload = repair_sqlite_disk_budget(
        get_config(),
        apply=bool(args.apply and not args.dry_run),
        repair_wal=include_wal,
        repair_temp=include_temp,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
