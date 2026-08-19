#!/usr/bin/env python3
"""Run the independent read-only cognitive consolidation reference audit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config
from core.ops.cognitive_readiness_reference import build_consolidation_reference_audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dir")
    parser.add_argument("--wiki-dir")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    config = get_config()
    report = build_consolidation_reference_audit(
        database_dir=Path(args.database_dir).expanduser() if args.database_dir else config.database_dir,
        wiki_dir=Path(args.wiki_dir).expanduser() if args.wiki_dir else config.wiki_dir,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "Mnemos cognitive readiness reference: "
            f"{'ok' if report['ok'] else 'fail'}; "
            f"candidates={report['covered']}/{report['candidate_denominator']}; "
            f"snapshot={report['snapshot_hash']}"
        )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
