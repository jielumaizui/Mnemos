#!/usr/bin/env python3
"""Inventory, quarantine, or restore pre-COG-038 feedback objects."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.feedback_history_migration import (  # noqa: E402
    build_feedback_history_inventory,
    public_inventory_report,
    reconcile_feedback_history,
    restore_feedback_history,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the historical feedback reconciliation CLI parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-inventory-hash")
    parser.add_argument("--expected-object-manifest-hash")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--restore-manifest", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Preview, apply, or restore the object-level migration."""

    args = build_parser().parse_args(argv)
    if args.database_dir is None:
        from core.config import get_config

        database_dir = Path(get_config().database_dir)
    else:
        database_dir = args.database_dir
    validation_error = _validate_args(args)
    if validation_error:
        print(json.dumps({"ok": False, "error": validation_error}, ensure_ascii=False))
        return 2
    try:
        if args.restore_manifest is not None:
            result = restore_feedback_history(
                database_dir=database_dir,
                restore_manifest=args.restore_manifest,
            )
        elif args.apply:
            result = reconcile_feedback_history(
                database_dir=database_dir,
                expected_inventory_hash=str(args.expected_inventory_hash),
                expected_object_manifest_hash=str(
                    args.expected_object_manifest_hash
                ),
                backup_dir=args.backup_dir,
            )
        else:
            inventory = build_feedback_history_inventory(database_dir)
            result = public_inventory_report(
                inventory,
                target_db=database_dir / "producer_consumer_ledger.db",
            )
        payload = {"ok": True, **result}
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        payload = {"ok": False, "error": str(exc)}
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


def _validate_args(args: argparse.Namespace) -> str:
    if args.apply and args.restore_manifest is not None:
        return "--apply and --restore-manifest are mutually exclusive"
    if args.restore_manifest is not None and any(
        (
            args.expected_inventory_hash,
            args.expected_object_manifest_hash,
            args.backup_dir,
        )
    ):
        return "--restore-manifest cannot be combined with apply inventory options"
    if not args.apply:
        return "" if args.restore_manifest is not None else _reject_apply_only_args(args)
    if not args.expected_inventory_hash:
        return "--apply requires --expected-inventory-hash"
    if not args.expected_object_manifest_hash:
        return "--apply requires --expected-object-manifest-hash"
    if args.backup_dir is None:
        return "--apply requires --backup-dir"
    return ""


def _reject_apply_only_args(args: argparse.Namespace) -> str:
    if any(
        (
            args.expected_inventory_hash,
            args.expected_object_manifest_hash,
            args.backup_dir,
        )
    ):
        return "inventory hashes and --backup-dir require --apply"
    return ""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
