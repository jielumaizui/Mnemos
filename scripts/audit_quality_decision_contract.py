#!/usr/bin/env python3
"""Audit the unified QualityDecision contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.system_contracts import QUALITY_DECISIONS, audit_quality_decision_contract


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    errors = audit_quality_decision_contract(strict=args.strict)
    payload = {
        "schema_version": "mnemos.quality_decision.v1",
        "ok": not errors,
        "errors": errors,
        "decisions": sorted(QUALITY_DECISIONS),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif errors:
        print("Quality decision contract audit failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("Quality decision contract audit passed")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
