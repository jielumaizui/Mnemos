#!/usr/bin/env python3
"""Audit or advance durable Wiki mutation/projection reconciliation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.wiki_projection_lifecycle import (  # noqa: E402
    DEFAULT_REQUIRED_CONSUMERS,
    WikiProjectionLedger,
)
from core.wiki_projection_publisher import publish_unpublished_mutations  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """Build the read-only-by-default projection reconciliation CLI parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="", help="override wiki_projection.db")
    parser.add_argument("--vault-dir", default="", help="override configured Wiki vault")
    parser.add_argument("--scan", action="store_true", help="record filesystem lifecycle changes")
    parser.add_argument(
        "--prune-out-of-scope",
        action="store_true",
        help="remove legacy ledger rows for hidden/outside-Vault projection artifacts",
    )
    parser.add_argument("--publish", action="store_true", help="publish mutations without event refs")
    parser.add_argument("--publish-limit", type=int, default=100)
    parser.add_argument("--apply", action="store_true", help="authorize --scan/--publish writes")
    parser.add_argument("--required-consumer", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run an optional scan/publish pass and emit the durable receipt report."""

    args = build_parser().parse_args(argv)
    if (args.scan or args.publish or args.prune_out_of_scope) and not args.apply:
        raise SystemExit("--scan/--publish/--prune-out-of-scope require --apply")
    cfg = get_config()
    db_path = Path(args.db_path).expanduser() if args.db_path else Path(cfg.database_dir) / "wiki_projection.db"
    vault_dir = Path(args.vault_dir).expanduser() if args.vault_dir else Path(cfg.wiki_dir)
    ledger = WikiProjectionLedger(db_path)
    payload: dict[str, object] = {
        "schema_version": "mnemos.wiki_projection_reconcile_run.v1",
        "db_path": str(db_path),
        "vault_dir": str(vault_dir),
        "applied": bool(args.apply),
    }
    if args.prune_out_of_scope:
        payload["pruned_out_of_scope"] = ledger.prune_out_of_scope_pages(vault_dir)
    if args.scan:
        payload["scan"] = ledger.reconcile_vault(vault_dir)
    if args.publish:
        payload["publish"] = publish_unpublished_mutations(
            ledger, limit=max(1, args.publish_limit)
        )
    required = tuple(args.required_consumer) or DEFAULT_REQUIRED_CONSUMERS
    report = ledger.reconciliation_report(required)
    payload["reconciliation"] = report
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            "Wiki projection reconciliation: "
            f"mutations={report['mutation_count']} receipts={report['receipt_count']} "
            f"gap={report['projection_gap']}"
        )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
