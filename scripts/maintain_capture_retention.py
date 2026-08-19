#!/usr/bin/env python3
"""Plan or explicitly apply Capture queue/artifact retention maintenance."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config
from core.sync_framework.capture_maintenance import CaptureRetentionMaintenance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete planned terminal payloads/artifacts")
    parser.add_argument("--payload-retention-days", type=int)
    parser.add_argument("--artifact-retention-days", type=int)
    parser.add_argument("--artifact-max-total-bytes", type=int)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    config = get_config()
    maintenance = CaptureRetentionMaintenance(config=config)
    try:
        plan = maintenance.plan(
            payload_retention_days=(
                args.payload_retention_days
                if args.payload_retention_days is not None
                else int(config.get("capture.payload_retention_days", 30))
            ),
            artifact_retention_days=(
                args.artifact_retention_days
                if args.artifact_retention_days is not None
                else int(config.get("capture.artifact_ttl_days", 30))
            ),
            artifact_max_total_bytes=(
                args.artifact_max_total_bytes
                if args.artifact_max_total_bytes is not None
                else int(config.get("capture.artifact_max_total_bytes", 5 * 1024 * 1024 * 1024))
            ),
        )
        result = maintenance.apply(plan) if args.apply else {"status": "planned", **plan}
        payload = {"ok": True, "apply": bool(args.apply), "result": result}
    except (OSError, RuntimeError, ValueError) as exc:
        payload = {"ok": False, "apply": bool(args.apply), "error": str(exc)}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(payload)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
