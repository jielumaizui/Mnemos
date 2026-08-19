#!/usr/bin/env python3
"""Audit the Mnemos data ownership contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.privacy.data_ownership import audit_data_ownership_contract, build_data_ownership_health


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="fail on missing scopes/states")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    errors = audit_data_ownership_contract(strict=args.strict)
    health = build_data_ownership_health()
    payload = {
        "schema_version": health["schema_version"],
        "ok": not errors,
        "errors": errors,
        "counts": health["counts"],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif errors:
        print("Data ownership contract audit failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("Data ownership contract audit passed")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
