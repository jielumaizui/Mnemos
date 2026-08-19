#!/usr/bin/env python3
"""Audit the unified cognitive data interface registry and ledger contract."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ops.cognitive_data_contract import (
    COGNITIVE_DATA_EVENT_SCHEMA_VERSION,
    DATA_INTERFACE_REGISTRY_SCHEMA_VERSION,
    RUNTIME_INSTRUMENTED_INTERFACE_IDS,
    data_interface_registry_payload,
    validate_data_interface_registry,
)


def audit_data_interface_registry() -> dict[str, Any]:
    errors = validate_data_interface_registry(repo_root=ROOT)
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (ROOT / "core", ROOT / "daemon", ROOT / "integrations")
        for path in root.rglob("*.py")
    )
    if not re.search(r"record_cognitive_data_event\s*\(", sources):
        errors.append("runtime cognitive data producers are not instrumented")
    if not re.search(r"record_cognitive_data_consumed\s*\(", sources):
        errors.append("runtime cognitive data consumers are not instrumented")
    if len(RUNTIME_INSTRUMENTED_INTERFACE_IDS) < 3:
        errors.append("capture queue and sync runtime interfaces must be instrumented")
    return {
        "schema_version": DATA_INTERFACE_REGISTRY_SCHEMA_VERSION,
        "event_schema_version": COGNITIVE_DATA_EVENT_SCHEMA_VERSION,
        "ok": not errors,
        "errors": errors,
        "registry": data_interface_registry_payload(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="return non-zero on audit errors")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    payload = audit_data_interface_registry()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif payload["ok"]:
        print("Data interface registry audit passed")
    else:
        print("Data interface registry audit failed:")
        for error in payload["errors"]:
            print(f"- {error}")
    return 1 if args.strict and not payload["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
