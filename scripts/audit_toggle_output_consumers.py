#!/usr/bin/env python3
"""Audit toggle output contracts and downstream consumers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.module_toggles import audit_toggle_output_consumers, build_module_toggle_health


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="enforce consumer effect metrics")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    errors = audit_toggle_output_consumers(strict=args.strict)
    health = build_module_toggle_health()
    payload = {
        "schema_version": health["schema_versions"]["toggle_output"],
        "ok": not errors,
        "errors": errors,
        "counts": health["counts"],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif errors:
        print("Toggle output consumer audit failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("Toggle output consumer audit passed")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
