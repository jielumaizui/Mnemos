#!/usr/bin/env python3
"""Audit that quick/integration/heavy own every pytest file exactly once."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_tests import audit_layer_coverage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--strict", action="store_true", help="fail on coverage gaps")
    args = parser.parse_args(argv)
    report = audit_layer_coverage()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            "Test suite denominator: "
            f"ok={report['ok']} discovered={report['discovered_count']} "
            f"assigned={report['assigned_count']}"
        )
        for field in ("missing", "extra", "overlaps"):
            if report[field]:
                print(f"{field}: {report[field]}")
    return 0 if report["ok"] or not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main())
