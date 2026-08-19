#!/usr/bin/env python3
"""Run the independent COG-048 governed-training audit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.training_governance_audit import (  # noqa: E402
    audit_training_governance,
    audit_training_governance_static,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the strict governed-training audit CLI parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument(
        "--static-only",
        action="store_true",
        help="audit repository contracts without opening runtime databases",
    )
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the read-only audit and return a strict failure exit code."""

    args = build_parser().parse_args(argv)
    if args.static_only:
        report = audit_training_governance_static(repo_root=ROOT)
    elif args.database_dir is None:
        from core.config import get_config

        database_dir = Path(get_config().database_dir)
    else:
        database_dir = args.database_dir
    if not args.static_only:
        report = audit_training_governance(
            database_dir=database_dir,
            repo_root=ROOT,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if args.strict and not report["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
