#!/usr/bin/env python3
"""Replay one failed distillation occurrence through the formal extractor."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.hephaestus.distillation_engine import DistillationEngine  # noqa: E402
from core.ops.operational_incident_replay import (  # noqa: E402
    execute_distillation_failure_replay,
    plan_distillation_failure_replay,
)


def _parser() -> argparse.ArgumentParser:
    """Build the replay CLI parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--occurrence-id", required=True)
    parser.add_argument("--raw-db", type=Path)
    parser.add_argument("--expected-plan-hash", default="")
    parser.add_argument("--expected-artifact-hash", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-send-content", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    """Run read-only planning or an explicitly confirmed formal replay."""

    args = _parser().parse_args()
    db_path = args.db or (Path(get_config().database_dir) / "operational_incidents.db")
    try:
        if args.apply:
            if not args.confirm_send_content:
                raise ValueError("--apply requires --confirm-send-content")
            if args.raw_db is None:
                raise ValueError("--apply requires --raw-db")
            if not args.expected_plan_hash or not args.expected_artifact_hash:
                raise ValueError(
                    "--apply requires --expected-plan-hash and --expected-artifact-hash"
                )
            engine = DistillationEngine()
            payload = execute_distillation_failure_replay(
                db_path,
                occurrence_id=args.occurrence_id,
                expected_plan_hash=args.expected_plan_hash,
                expected_artifact_hash=args.expected_artifact_hash,
                raw_db=args.raw_db,
                runner=lambda current_session, current_messages, current_meta: engine.process(
                    current_session,
                    current_messages,
                    meta=current_meta,
                ),
            )
        else:
            payload = plan_distillation_failure_replay(
                db_path,
                occurrence_id=args.occurrence_id,
            )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        payload = {
            "schema_version": "mnemos.operational_incident_replay.v1",
            "ok": False,
            "applied": False,
            "error": f"{type(exc).__name__}:{exc}",
        }
        exit_code = 2
    else:
        payload["ok"] = payload.get("status", "committed") != "failed"
        exit_code = 0 if payload["ok"] else 1
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=None if args.json else 2,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
