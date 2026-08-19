#!/usr/bin/env python3
"""Audit unified lifecycle status and failure-class mappings."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.system_contracts import FAILURE_CLASSES, LIFECYCLE_STATUSES, audit_lifecycle_status_contract


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    errors = audit_lifecycle_status_contract(strict=args.strict)
    payload = {
        "schema_version": "mnemos.lifecycle_status.v1",
        "ok": not errors,
        "errors": errors,
        "statuses": sorted(LIFECYCLE_STATUSES),
        "failure_classes": sorted(FAILURE_CLASSES),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif errors:
        print("Lifecycle status contract audit failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("Lifecycle status contract audit passed")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
