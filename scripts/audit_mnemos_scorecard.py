#!/usr/bin/env python3
"""Audit the unified Mnemos full-score scorecard."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.system_contracts import SCORECARD_DIMENSIONS, audit_mnemos_scorecard


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    errors = audit_mnemos_scorecard(strict=args.strict)
    payload = {
        "schema_version": "mnemos.scorecard.v1",
        "ok": not errors,
        "errors": errors,
        "dimensions": sorted(SCORECARD_DIMENSIONS),
        "max_total": sum(dim.max_score for dim in SCORECARD_DIMENSIONS.values()),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif errors:
        print("Mnemos scorecard audit failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("Mnemos scorecard audit passed")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
