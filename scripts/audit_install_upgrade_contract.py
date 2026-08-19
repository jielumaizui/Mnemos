#!/usr/bin/env python3
"""Audit the Mnemos install/upgrade/uninstall lifecycle contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.setup.install_lifecycle import (
    audit_install_upgrade_contract,
    build_install_lifecycle_health,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="fail on missing CLI/script wiring")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    errors = audit_install_upgrade_contract(strict=args.strict)
    health = build_install_lifecycle_health()
    payload = {
        "schema_version": health["schema_version"],
        "ok": not errors,
        "errors": errors,
        "recommended_entrypoints": health["recommended_entrypoints"],
        "state_status": health["state"]["status"],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif errors:
        print("Install/upgrade contract audit failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("Install/upgrade contract audit passed")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
