#!/usr/bin/env python3
"""Run a registered incident reproducer against exact before/after fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from core.ops.operational_incident import OperationalIncidentStore


def _fixture(path_value: str) -> tuple[dict, str]:
    path = Path(path_value).expanduser().resolve(strict=True)
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"diagnostic fixture must be a JSON object: {path}")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    return payload, f"fixture:{path}:{digest}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--occurrence-id", required=True)
    parser.add_argument("--before-json", required=True)
    parser.add_argument("--after-json", required=True)
    parser.add_argument(
        "--reproducer",
        default="distillation_fragment_contract.v1",
        choices=("distillation_fragment_contract.v1",),
    )
    parser.add_argument("--evidence-kind", default="distillation_contract_repair")
    parser.add_argument("--confirm-record-evidence", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not args.confirm_record_evidence:
        raise SystemExit("--confirm-record-evidence is required")
    before, before_ref = _fixture(args.before_json)
    after, after_ref = _fixture(args.after_json)
    result = OperationalIncidentStore(Path(args.db)).execute_diagnostic_reproducer(
        args.incident_id,
        occurrence_id=args.occurrence_id,
        evidence_kind=args.evidence_kind,
        source_refs=(before_ref, after_ref),
        reproducer_id=args.reproducer,
        before_input=before,
        after_input=after,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(result["evidence_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
