#!/usr/bin/env python3
"""Audit the Phase 6 operational incident pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.config import get_config
from core.ops.operational_incident_audit import (
    audit_operational_incident_pipeline,
    audit_operational_incident_reference,
    audit_operational_incident_static,
)


def main() -> int:
    """Run the selected operational-incident audit and return its exit code."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    db_path = args.db
    if args.static_only and args.self_test:
        parser.error("--static-only and --self-test are mutually exclusive")
    if not args.static_only and not args.self_test and db_path is None:
        db_path = get_config().database_dir / "operational_incidents.db"
    try:
        if args.self_test:
            report = audit_operational_incident_reference(
                repo_root=Path(__file__).resolve().parents[1],
            )
        elif args.static_only:
            report = audit_operational_incident_static(
                repo_root=Path(__file__).resolve().parents[1],
            )
        else:
            report = audit_operational_incident_pipeline(
                db_path,
                repo_root=Path(__file__).resolve().parents[1],
            )
    except (OSError, RuntimeError, ValueError) as exc:
        report = {
            "schema_version": "mnemos.operational_incident_pipeline_audit.v1",
            "ok": False,
            "status": "blocked",
            "error_type": type(exc).__name__,
            "repair_action": (
                "Run scripts/reconcile_operational_incidents.py --json, "
                "review the plan, then use the explicit apply workflow."
            ),
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and not report.get("ok") else 0


if __name__ == "__main__":
    raise SystemExit(main())
