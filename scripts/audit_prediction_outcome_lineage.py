#!/usr/bin/env python3
"""Audit canonical PredictionRecord and OutcomeMeasurement lineage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.delivery_router import resolve_delivery_db_path  # noqa: E402
from core.cognitive.prediction_lineage_audit import (  # noqa: E402
    AUDIT_SCHEMA_VERSION,
    audit_prediction_outcome_lineage,
)
from core.config import get_config  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit PredictionRecord/OutcomeMeasurement provenance",
    )
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--delivery-db", type=Path)
    parser.add_argument("--target-db", type=Path)
    parser.add_argument("--raw-db", type=Path)
    return parser


def main() -> int:
    """Run the independent lineage audit and enforce strict exit status."""

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
    report = audit_prediction_outcome_lineage(
        delivery_db=delivery_db,
        target_db=target_db,
        repo_root=ROOT,
        raw_db=args.raw_db,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 1 if args.strict and not report["ok"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": AUDIT_SCHEMA_VERSION,
                    "ok": False,
                    "status": "error",
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
        )
        raise SystemExit(1) from exc
