#!/usr/bin/env python3
"""Run the independent COG-038 feedback attribution audit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.feedback_attribution_audit import (  # noqa: E402
    audit_feedback_attribution,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the strict feedback attribution audit CLI parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the audit and return nonzero when strict findings remain."""

    args = build_parser().parse_args(argv)
    if args.database_dir is None:
        from core.config import get_config

        database_dir = Path(get_config().database_dir)
    else:
        database_dir = args.database_dir
    report = audit_feedback_attribution(
        database_dir=database_dir,
        repo_root=ROOT,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if args.strict and not report["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
