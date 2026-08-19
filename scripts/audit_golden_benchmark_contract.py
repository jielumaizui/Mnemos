#!/usr/bin/env python3
"""Audit the deterministic golden benchmark contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.benchmarks.golden import (  # noqa: E402
    GOLDEN_BENCHMARK_SCHEMA_VERSION,
    audit_golden_benchmark_contract,
    build_golden_benchmark_health,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    errors = audit_golden_benchmark_contract(strict=args.strict)
    health = build_golden_benchmark_health()
    payload = {
        "schema_version": GOLDEN_BENCHMARK_SCHEMA_VERSION,
        "ok": not errors,
        "errors": errors,
        "counts": health["counts"],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif errors:
        print("Golden benchmark contract audit failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("Golden benchmark contract audit passed")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
