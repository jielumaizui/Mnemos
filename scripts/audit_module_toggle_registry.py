#!/usr/bin/env python3
"""Audit the Mnemos module toggle registry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.module_toggles import audit_module_toggle_registry, build_module_toggle_health


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="fail on stale or missing evidence refs")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    errors = audit_module_toggle_registry(strict=args.strict)
    health = build_module_toggle_health()
    payload = {
        "schema_version": health["schema_versions"]["module_toggle"],
        "ok": not errors,
        "errors": errors,
        "counts": health["counts"],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif errors:
        print("Module toggle registry audit failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("Module toggle registry audit passed")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
