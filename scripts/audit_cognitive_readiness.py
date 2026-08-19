#!/usr/bin/env python3
"""Run a read-only-by-default Mnemos cognitive readiness audit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config
from core.ops.cognitive_readiness import (
    build_cognitive_readiness_report,
    dumps_report,
    format_cognitive_readiness_text,
    record_cognitive_readiness_gaps,
)


def _build_config(args: argparse.Namespace):
    if not args.database_dir and not args.wiki_dir and not args.raw_vault_dir:
        return get_config()
    runtime = get_config()
    return SimpleNamespace(
        database_dir=Path(args.database_dir).expanduser()
        if args.database_dir
        else runtime.database_dir,
        wiki_dir=Path(args.wiki_dir).expanduser() if args.wiki_dir else runtime.wiki_dir,
        obsidian_vault_path=Path(args.raw_vault_dir).expanduser()
        if args.raw_vault_dir
        else runtime.obsidian_vault_path,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dir", help="Override Mnemos runtime database directory")
    parser.add_argument("--wiki-dir", help="Override Mnemos cognitive vault directory")
    parser.add_argument("--raw-vault-dir", help="Override raw vault directory")
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    parser.add_argument("--strict", action="store_true", help="Fail when readiness budgets fail")
    parser.add_argument("--budget", action="store_true", help="Enforce readiness budgets")
    parser.add_argument(
        "--record-gaps",
        action="store_true",
        help="Record current gaps to ActionLedger",
    )
    args = parser.parse_args(argv)

    config = _build_config(args)
    report = build_cognitive_readiness_report(
        config,
        strict=args.strict,
        enforce_budget=args.budget,
    )
    if args.record_gaps:
        record_cognitive_readiness_gaps(report, config)
    if args.json:
        print(dumps_report(report))
    else:
        print(format_cognitive_readiness_text(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
