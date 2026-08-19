#!/usr/bin/env python3
"""Verify and optionally attest one host Agent's frozen native→Raw coverage.

This command never parses native transcript bodies.  It independently reads
the daemon's coverage sidecar, denominator ledger, and canonical Raw headers;
``--apply`` writes only a content-free Agent Kit receipt after the host has
already completed its authenticated synthetic-safe runtime probe.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agent_kit.runtime_receipts import AgentRuntimeReceiptStore
from core.agent_kit.source_capture_verification import verify_source_capture
from core.config import Config
from core.ops.durable_io import read_native_bytes


def _load_coverage(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(read_native_bytes(path).decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"coverage state is unreadable: {exc.__class__.__name__}") from exc
    if not isinstance(payload, dict):
        raise ValueError("coverage state must be a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True, help="manifest-declared host agent")
    parser.add_argument("--config", type=Path, help="read-only Mnemos config path")
    parser.add_argument("--coverage", type=Path, help="agent_source_coverage.json override")
    parser.add_argument("--cursor-db", type=Path, help="agent_sync_cursors.db override")
    parser.add_argument("--raw-db", type=Path, help="raw_events.db override")
    parser.add_argument("--receipt-db", type=Path, help="agent_authorization.db override")
    parser.add_argument("--apply", action="store_true", help="write the content-free attestation")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        config = Config(config_path=args.config, provision=False)
        database_dir = Path(config.database_dir)
        coverage_path = args.coverage or database_dir / "agent_source_coverage.json"
        cursor_db_path = args.cursor_db or database_dir / "agent_sync_cursors.db"
        raw_db_path = args.raw_db or database_dir / "raw_events.db"
        receipt_db_path = args.receipt_db or database_dir / "agent_authorization.db"
        receipt_store = AgentRuntimeReceiptStore(receipt_db_path, initialize=False)
        runtime_receipt = receipt_store.evaluate(args.agent)
        coverage = _load_coverage(coverage_path)
        evidence = verify_source_capture(
            source_name=args.agent,
            coverage=coverage,
            cursor_db_path=cursor_db_path,
            raw_db_path=raw_db_path,
            runtime_receipt=runtime_receipt,
        )
        result: dict[str, Any] = {
            "ok": bool(evidence["ok"]),
            "apply": bool(args.apply),
            "coverage_path": str(coverage_path),
            "cursor_db_path": str(cursor_db_path),
            "raw_db_path": str(raw_db_path),
            "runtime_receipt_state": str(runtime_receipt.get("runtime_state") or "missing"),
            "evidence": evidence,
        }
        if args.apply:
            if not evidence["ok"]:
                result["receipt"] = {
                    "success": False,
                    "source_capture_state": "source_capture_invalid",
                    "error": "refusing to attest unresolved source-to-Raw evidence",
                }
                result["ok"] = False
            else:
                receipt = AgentRuntimeReceiptStore(receipt_db_path).record_source_capture(
                    args.agent,
                    coverage=coverage,
                    cursor_db_path=cursor_db_path,
                    raw_db_path=raw_db_path,
                )
                result["receipt"] = receipt
                result["ok"] = bool(receipt.get("success"))
    except (OSError, RuntimeError, ValueError) as exc:
        result = {"ok": False, "apply": bool(args.apply), "error": str(exc)}

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
