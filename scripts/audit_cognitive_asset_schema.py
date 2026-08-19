#!/usr/bin/env python3
"""Audit the system-wide CognitiveAsset contract registry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.system_contracts import audit_cognitive_asset_schema, contract_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="fail on non-runtime references too")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    errors = audit_cognitive_asset_schema(strict=args.strict)
    payload = {
        "schema_version": "mnemos.cognitive_asset.v1",
        "ok": not errors,
        "errors": errors,
        "asset_types": sorted(contract_snapshot()["cognitive_assets"]),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif errors:
        print("Cognitive asset schema audit failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("Cognitive asset schema audit passed")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
