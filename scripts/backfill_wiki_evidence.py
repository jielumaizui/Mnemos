#!/usr/bin/env python3
"""Backfill Wiki page source refs from Mnemos provenance tables."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config
from core.ops.evidence_backfill import (
    dumps_report,
    format_evidence_backfill_text,
    run_evidence_backfill,
)


def _build_config(args: argparse.Namespace):
    if not args.database_dir and not args.wiki_dir:
        return get_config()
    runtime = get_config()
    return SimpleNamespace(
        database_dir=Path(args.database_dir).expanduser()
        if args.database_dir
        else runtime.database_dir,
        wiki_dir=Path(args.wiki_dir).expanduser() if args.wiki_dir else runtime.wiki_dir,
        obsidian_vault_path=runtime.obsidian_vault_path,
        get=runtime.get,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dir", help="Override Mnemos runtime database directory")
    parser.add_argument("--wiki-dir", help="Override Mnemos cognitive vault directory")
    parser.add_argument("--apply", action="store_true", help="Write page_metrics and frontmatter")
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    parser.add_argument("--limit", type=int, default=None, help="Maximum changed pages")
    parser.add_argument("--max-refs-per-page", type=int, default=None)
    parser.add_argument("--frontmatter-ref-limit", type=int, default=None)
    parser.add_argument("--unresolved-sample-limit", type=int, default=None)
    parser.add_argument("--change-sample-limit", type=int, default=None)
    parser.add_argument("--report-dir", default=None, help="Report directory under Wiki root")
    parser.add_argument(
        "--relation-evidence-type",
        action="append",
        dest="relation_evidence_types",
        default=None,
        help="Allow a relation_evidence.evidence_type; repeatable",
    )
    parser.add_argument("--skip-relation-evidence", action="store_true")
    parser.add_argument("--no-frontmatter", action="store_true")
    parser.add_argument("--no-report", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    report = run_evidence_backfill(
        _build_config(args),
        apply=args.apply,
        limit=args.limit,
        max_refs_per_page=args.max_refs_per_page,
        frontmatter_ref_limit=args.frontmatter_ref_limit,
        unresolved_sample_limit=args.unresolved_sample_limit,
        change_sample_limit=args.change_sample_limit,
        include_relation_evidence=not args.skip_relation_evidence,
        relation_evidence_types=args.relation_evidence_types,
        write_frontmatter=not args.no_frontmatter,
        write_report=not args.no_report,
        report_dir=args.report_dir,
    )
    if args.json:
        print(dumps_report(report))
    else:
        print(format_evidence_backfill_text(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
