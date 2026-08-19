#!/usr/bin/env python3
"""Audit auto-disable policies for module toggles."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.module_toggles import audit_toggle_auto_disable_policy, build_module_toggle_health


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="enforce high-cost budget guards")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    errors = audit_toggle_auto_disable_policy(strict=args.strict)
    payload = {
        "schema_version": build_module_toggle_health()["schema_versions"]["module_toggle"],
        "ok": not errors,
        "errors": errors,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif errors:
        print("Toggle auto-disable policy audit failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("Toggle auto-disable policy audit passed")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
