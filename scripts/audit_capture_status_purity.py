#!/usr/bin/env python3
"""Verify that Capture status diagnostics are schema- and retention-pure."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.sync_framework.capture_queue import CaptureQueue
from core.sync_framework.capture_schema import CaptureQueueSchema
from core.sync_framework.capture_status_reader import CaptureStatusReader


def _snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        path.name: (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.iterdir())
        if path.is_file()
    }


def _snapshot_database(db_path: Path) -> dict[str, tuple[int, int, str]]:
    paths = (
        db_path,
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
    )
    return {
        path.name: (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in paths
        if path.is_file()
    }


def _ast_contract() -> list[str]:
    errors: list[str] = []
    facade_path = ROOT / "core/application/facade.py"
    tree = ast.parse(facade_path.read_text(encoding="utf-8"), filename=str(facade_path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "capture_status":
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                if call.func.id == "CaptureService":
                    errors.append("facade_capture_status_constructs_capture_service")
    reader_path = ROOT / "core/sync_framework/capture_status_reader.py"
    reader_text = reader_path.read_text(encoding="utf-8")
    if "mode=ro&immutable=1" not in reader_text:
        errors.append("capture_status_reader_missing_read_only_immutable_uri")
    return errors


def audit() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mnemos-capture-status-audit-") as temp:
        root = Path(temp)
        missing = CaptureStatusReader(root / "missing.db").read("codex", "missing")
        missing_after = _snapshot(root)

        db_path = root / "capture_queue.db"
        CaptureQueueSchema.initialize(db_path)
        queue = CaptureQueue(db_path=str(db_path))
        try:
            assert queue.enqueue(
                source_agent="codex",
                session_id="status-session",
                turn_id="native-status",
                turn_number=1,
                payload={},
                content_hash="status-hash",
                raw_revision_id="rawrev-status-audit",
            ) == "queued"
        finally:
            queue.close()
        before = _snapshot_database(db_path)
        reader = CaptureStatusReader(db_path)
        results = [reader.read("codex", "status-session", 1) for _ in range(100)]
        after = _snapshot_database(db_path)

        old_path = root / "old.db"
        with sqlite3.connect(old_path) as conn:
            conn.execute(
                """
                CREATE TABLE capture_events (
                    source_agent TEXT, session_id TEXT, turn_number INTEGER,
                    status TEXT, retry_count INTEGER, created_at TEXT,
                    processed_at TEXT, error TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO capture_events VALUES
                ('codex', 'legacy', 1, 'pending', 0, '2026-07-12T00:00:00', NULL, '')
                """
            )
        old_before = _snapshot_database(old_path)
        old_result = CaptureStatusReader(old_path).read("codex", "legacy", 1)
        old_after = _snapshot_database(old_path)

    errors = _ast_contract()
    if missing["status"] != "uninitialized" or missing_after:
        errors.append("missing_database_status_read_wrote_state")
    if not all(result.get("status") == "pending" for result in results):
        errors.append("current_schema_status_read_failed")
    if before != after:
        errors.append("read_only_capture_status_writes")
    if old_result.get("status") != "pending" or old_before != old_after:
        errors.append("old_schema_status_read_writes")
    return {
        "schema": "mnemos.capture_status_purity.v1",
        "ok": not errors,
        "read_only_capture_status_writes": 0 if before == after else 1,
        "constructor_schema_mutations": 0 if not _ast_contract() else 1,
        "diagnostic_cleanup_effects": 0 if before == after and old_before == old_after else 1,
        "status_samples": len(results),
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = audit()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
