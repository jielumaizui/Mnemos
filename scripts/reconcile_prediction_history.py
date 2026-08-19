#!/usr/bin/env python3
"""Dry-run, apply, or restore legacy predictive-delivery provenance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.delivery_router import resolve_delivery_db_path  # noqa: E402
from core.cognitive.prediction_history_migration import (  # noqa: E402
    MIGRATION_SCHEMA_VERSION,
    apply_prediction_history_migration,
    build_prediction_history_inventory,
    inspect_prediction_target,
    restore_prediction_backup,
)
from core.config import get_config  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inventory or reconcile legacy predictive deliveries",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore-manifest", type=Path)
    parser.add_argument("--inventory-hash", default="")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--delivery-db", type=Path)
    parser.add_argument("--target-db", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    """Run dry-run, reviewed apply, or manifest-bound restore."""

    args = _parser().parse_args()
    config = get_config()
    database_dir = Path(args.database_dir or config.database_dir).expanduser()
    delivery_db = resolve_delivery_db_path(
        config=config,
        database_dir=database_dir,
        explicit=args.delivery_db,
    )
    target_db = Path(
        args.target_db or database_dir / "producer_consumer_ledger.db"
    ).expanduser()
    if args.restore_manifest:
        if args.apply:
            raise ValueError("--apply and --restore-manifest are mutually exclusive")
        report = restore_prediction_backup(
            target_db=target_db,
            restore_manifest=args.restore_manifest,
            database_dir=database_dir,
        )
    elif args.apply:
        if args.backup_dir is None:
            raise ValueError("--backup-dir is required with --apply")
        if not args.inventory_hash:
            raise ValueError("--inventory-hash is required with --apply")
        report = apply_prediction_history_migration(
            delivery_db=delivery_db,
            target_db=target_db,
            expected_inventory_hash=args.inventory_hash,
            backup_dir=args.backup_dir,
            database_dir=database_dir,
        )
    else:
        inventory = build_prediction_history_inventory(delivery_db)
        report = inventory.report(
            target=inspect_prediction_target(target_db),
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": MIGRATION_SCHEMA_VERSION,
                    "ok": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        raise SystemExit(1) from exc
