#!/usr/bin/env python3
"""Validate the Phase 0 COG-040 performance baseline contract."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ops.cognitive_performance_baseline import (  # noqa: E402
    DEFAULT_MANIFEST_PATH,
    build_performance_baseline_health,
    load_performance_baseline,
)


def main(argv: list[str] | None = None) -> int:
    """Validate the frozen non-certifying Phase 0 performance baseline."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    manifest = load_performance_baseline(args.manifest)
    payload = build_performance_baseline_health(manifest, verify_files=True)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif payload["ok"]:
        print("COG-040 Phase 0 performance baseline contract passed")
        print("PerformanceCertificate eligible: false")
    else:
        print("COG-040 Phase 0 performance baseline contract failed")
        for error in payload["errors"]:
            print(f"- {error}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
