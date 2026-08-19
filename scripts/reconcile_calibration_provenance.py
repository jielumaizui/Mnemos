#!/usr/bin/env python3
"""Preview or apply object-level CalibrationRecord provenance migration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.calibration_reconcile_contracts import (  # noqa: E402
    CalibrationReconciliationPaths,
    RECONCILIATION_SCHEMA_VERSION,
)
from core.cognitive.calibration_reconcile_executor import (  # noqa: E402
    apply_calibration_reconciliation,
)
from core.cognitive.calibration_reconcile_planner import (  # noqa: E402
    build_calibration_reconciliation_plan,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--expected-inventory-hash", default="")
    parser.add_argument("--json", action="store_true")
    return parser


def _paths(args: argparse.Namespace) -> CalibrationReconciliationPaths:
    if args.database_dir is not None and args.wiki_dir is not None:
        return CalibrationReconciliationPaths(args.database_dir, args.wiki_dir)
    from core.config import get_config

    config = get_config()
    return CalibrationReconciliationPaths(
        args.database_dir or Path(config.database_dir),
        args.wiki_dir or Path(config.wiki_dir),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply and (args.backup_dir is None or not args.expected_inventory_hash):
        payload = {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "ok": False,
            "status": "blocked",
            "error": "--apply requires --backup-dir and --expected-inventory-hash",
        }
    elif not args.apply and (args.backup_dir is not None or args.expected_inventory_hash):
        payload = {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "ok": False,
            "status": "blocked",
            "error": "apply-only arguments require --apply",
        }
    else:
        try:
            paths = _paths(args)
            if args.apply:
                assert args.backup_dir is not None
                payload = apply_calibration_reconciliation(
                    paths,
                    expected_inventory_hash=str(args.expected_inventory_hash),
                    backup_dir=args.backup_dir,
                )
            else:
                payload = build_calibration_reconciliation_plan(paths).as_dict()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            payload = {
                "schema_version": RECONCILIATION_SCHEMA_VERSION,
                "ok": False,
                "status": "blocked",
                "error": str(exc),
            }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
