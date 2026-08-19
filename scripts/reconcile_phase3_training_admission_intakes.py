#!/usr/bin/env python3
"""Preview or apply exact pre-contract Phase 3 training intake commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.phase3_training_intake_reconciliation import (  # noqa: E402
    build_phase3_training_intake_inventory,
    public_phase3_training_intake_inventory,
    reconcile_phase3_training_admission_intakes,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-inventory-hash")
    parser.add_argument("--expected-object-manifest-hash")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.database_dir is None:
        from core.config import get_config

        database_dir = Path(get_config().database_dir)
    else:
        database_dir = args.database_dir
    error = _validate_args(args)
    if error:
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False))
        return 2
    try:
        if args.apply:
            result = reconcile_phase3_training_admission_intakes(
                database_dir=database_dir,
                expected_inventory_hash=str(args.expected_inventory_hash),
                expected_object_manifest_hash=str(
                    args.expected_object_manifest_hash
                ),
                backup_dir=args.backup_dir,
            )
        else:
            result = public_phase3_training_intake_inventory(
                build_phase3_training_intake_inventory(database_dir)
            )
        payload = {"ok": True, **result}
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        payload = {"ok": False, "error": str(exc)}
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


def _validate_args(args: argparse.Namespace) -> str:
    if not args.apply:
        if any(
            (
                args.expected_inventory_hash,
                args.expected_object_manifest_hash,
                args.backup_dir,
            )
        ):
            return "inventory hashes and --backup-dir require --apply"
        return ""
    if not args.expected_inventory_hash:
        return "--apply requires --expected-inventory-hash"
    if not args.expected_object_manifest_hash:
        return "--apply requires --expected-object-manifest-hash"
    if args.backup_dir is None:
        return "--apply requires --backup-dir"
    return ""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
