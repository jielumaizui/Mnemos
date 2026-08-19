#!/usr/bin/env python3
"""Dry-run/apply/restore object-level DecisionTrace history migration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.decision_trace_migration import (  # noqa: E402
    apply_decision_trace_history_migration,
    build_decision_trace_inventory,
    configured_source_domains,
    inspect_decision_trace_target,
    restore_decision_trace_backup,
)
from core.config import get_config  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inventory or migrate legacy material-action provenance",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore-manifest", type=Path)
    parser.add_argument("--inventory-hash", default="")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--action-db", type=Path)
    parser.add_argument("--delivery-db", type=Path)
    parser.add_argument("--trusted-push-db", type=Path)
    parser.add_argument("--target-db", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    """Run dry inventory, reviewed apply, or verified restore."""

    args = _parser().parse_args()
    config = get_config()
    database_dir = Path(args.database_dir or config.database_dir).expanduser()
    delivery_path = Path(args.delivery_db).expanduser() if args.delivery_db else None
    trusted_path = (
        Path(args.trusted_push_db).expanduser()
        if args.trusted_push_db
        else None
    )
    target = Path(
        args.target_db or database_dir / "producer_consumer_ledger.db"
    ).expanduser()
    domains = list(
        configured_source_domains(
            config=config,
            database_dir=database_dir,
            delivery_db_path=delivery_path,
            trusted_push_db_path=trusted_path,
        )
    )
    if args.action_db:
        domains[0] = type(domains[0])(
            **{**domains[0].__dict__, "path": args.action_db.expanduser()}
        )

    if args.restore_manifest:
        if args.apply:
            raise ValueError("--apply and --restore-manifest are mutually exclusive")
        report = restore_decision_trace_backup(
            target_db=target,
            restore_manifest=args.restore_manifest,
            database_dir=database_dir,
        )
    elif args.apply:
        if args.backup_dir is None:
            raise ValueError("--backup-dir is required with --apply")
        if not args.inventory_hash:
            raise ValueError("--inventory-hash is required with --apply")
        report = apply_decision_trace_history_migration(
            domains=tuple(domains),
            target_db=target,
            expected_inventory_hash=args.inventory_hash,
            backup_dir=args.backup_dir,
            database_dir=database_dir,
        )
    else:
        inventory = build_decision_trace_inventory(tuple(domains))
        report = inventory.report(target=inspect_decision_trace_target(target))

    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    print(rendered)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": "mnemos.decision_trace_history_migration.v1",
                    "ok": False,
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        raise SystemExit(1) from exc
