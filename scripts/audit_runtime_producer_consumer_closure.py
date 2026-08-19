#!/usr/bin/env python3
"""Audit runtime producer/consumer closure for module outputs and adaptive flows."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.module_toggles import audit_runtime_producer_consumer_closure, build_module_toggle_health
from core.ops.producer_consumer_ledger import DEFAULT_MATRIX
from core.ops.runtime_flow_health import (
    audit_runtime_producer_consumer_closure as audit_adaptive_runtime_closure,
    build_runtime_producer_consumer_health,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="fail when any wired producer lacks a consumer")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--db-dir",
        type=Path,
        default=None,
        help="Override runtime ledger database directory for tests or dry-run probes",
    )
    parser.add_argument(
        "--matrix",
        type=Path,
        default=DEFAULT_MATRIX,
        help="Path to adaptive_data_flows.json",
    )
    args = parser.parse_args(argv)

    from core.config import get_config

    config = args.db_dir if args.db_dir is not None else get_config()
    module_errors = audit_runtime_producer_consumer_closure(strict=args.strict)
    module_health = build_module_toggle_health()
    adaptive_errors = audit_adaptive_runtime_closure(
        config,
        strict=args.strict,
        matrix_path=args.matrix,
    )
    adaptive_health = build_runtime_producer_consumer_health(
        config,
        matrix_path=args.matrix,
    )
    errors = [*module_errors, *adaptive_errors]
    payload = {
        "schema_version": "mnemos.runtime_producer_consumer_closure.v1",
        "ok": not errors,
        "errors": errors,
        "module_toggles": {
            "schema_version": module_health["schema_versions"]["toggle_output"],
            "counts": module_health["counts"],
        },
        "adaptive_flows": adaptive_health,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif errors:
        print("Runtime producer/consumer closure audit failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("Runtime producer/consumer closure audit passed")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
