#!/usr/bin/env python3
"""Reconcile legacy distillation failures into operational incidents.

Dry-run is the default and performs no filesystem or SQLite writes. Apply
requires an exact dry-run plan hash plus an explicit backup directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.ops.operational_incident_reconcile import (  # noqa: E402
    apply_operational_incident_reconciliation,
    plan_operational_incident_reconciliation,
)


def _parser() -> argparse.ArgumentParser:
    """Build the reconciliation CLI parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--expected-plan-hash", default="")
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    """Run a read-only plan or an explicitly authorized offline apply."""

    args = _parser().parse_args()
    config = get_config()
    database_dir = args.database_dir or Path(config.database_dir)
    wiki_dir = args.wiki_dir or Path(config.wiki_dir)
    try:
        if args.apply:
            if args.backup_dir is None:
                raise ValueError("--apply requires --backup-dir")
            if not args.expected_plan_hash:
                raise ValueError("--apply requires --expected-plan-hash")
            payload = apply_operational_incident_reconciliation(
                database_dir,
                expected_plan_hash=args.expected_plan_hash,
                backup_dir=args.backup_dir,
                wiki_dir=wiki_dir,
            )
        else:
            payload = plan_operational_incident_reconciliation(
                database_dir,
                wiki_dir=wiki_dir,
            )
            payload["read_only"] = True
    except (OSError, RuntimeError, ValueError) as exc:
        payload = {
            "schema_version": "mnemos.operational_incident_reconciliation.v1",
            "ok": False,
            "applied": False,
            "error": f"{type(exc).__name__}:{exc}",
        }
        exit_code = 2
    else:
        exit_code = 0 if payload.get("ok", True) else 1
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
