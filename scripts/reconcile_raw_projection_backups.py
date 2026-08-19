#!/usr/bin/env python3
"""Reconcile over-budget legacy Raw-projection backups without reading Raw bodies.

Only direct ``raw-vault-projection-*`` directories classified by the metadata
auditor as a symlink-free ``legacy_full_copy`` are eligible.  Before each
deletion the directory inventory is rechecked against the approved plan; an
append-only metadata receipt records the exact selection and completion state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config
from scripts.audit_raw_projection_backups import (
    DEFAULT_MAX_TOTAL_MB,
    _backup_record,
    audit_raw_projection_backups,
)


SCHEMA_VERSION = "mnemos.raw_projection_backup_reconcile.v1"
RECEIPT_NAME = "raw-projection-backup-cleanup-receipt.json"


class RawProjectionBackupReconcileError(RuntimeError):
    """Raised when a historical backup deletion plan is not safe to apply."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _eligible_record(record: dict[str, Any]) -> bool:
    return (
        record.get("recovery_class") == "legacy_full_copy"
        and int(record.get("symlink_count") or 0) == 0
        and isinstance(record.get("backup_id"), str)
        and record["backup_id"].startswith("raw-vault-projection-")
    )


def _selection_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "backup_id": str(record["backup_id"]),
        "recovery_class": str(record["recovery_class"]),
        "file_count": int(record["file_count"]),
        "total_bytes": int(record["total_bytes"]),
        "inventory_hash": str(record["inventory_hash"]),
        "symlink_count": int(record["symlink_count"]),
    }


def plan_cleanup(backup_root: Path, *, max_total_bytes: int) -> dict[str, Any]:
    """Return a content-free plan; no filesystem state is changed."""
    audit = audit_raw_projection_backups(
        backup_root, max_total_bytes=max_total_bytes
    )
    selected = [
        _selection_record(record)
        for record in audit["backups"]
        if _eligible_record(record)
    ]
    selected.sort(key=lambda record: record["backup_id"])
    selected_bytes = sum(int(record["total_bytes"]) for record in selected)
    return {
        "schema_version": SCHEMA_VERSION,
        "backup_root": str(backup_root),
        "audit": audit,
        "selected": selected,
        "selected_count": len(selected),
        "selected_bytes": selected_bytes,
        "selection_hash": _sha256(selected),
        "status": (
            "cleanup_required"
            if audit["over_budget"] and selected
            else "clean"
            if not audit["over_budget"]
            else "blocked_no_eligible_legacy_backup"
        ),
    }


def verify_canonical_preconditions(raw_db: Path, raw_dir: Path) -> dict[str, Any]:
    """Check current Raw and its published V2 journal without opening bodies."""
    if not raw_db.is_file():
        raise RawProjectionBackupReconcileError("canonical raw_events.db is missing")
    journal_path = raw_dir / ".mnemos_raw_projection_journal.json"
    try:
        with sqlite3.connect(f"file:{raw_db.resolve()}?mode=ro", uri=True) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            foreign_key_count = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            raw_turn_count = int(conn.execute("SELECT COUNT(*) FROM raw_turns").fetchone()[0])
    except (OSError, sqlite3.Error) as exc:
        raise RawProjectionBackupReconcileError(
            f"canonical raw database is unreadable: {exc.__class__.__name__}"
        ) from exc
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RawProjectionBackupReconcileError("current Raw projection journal is unreadable") from exc
    files = journal.get("files") if isinstance(journal, dict) else None
    journal_ok = (
        isinstance(journal, dict)
        and journal.get("schema_version") == "mnemos.raw_projection.v2"
        and isinstance(files, dict)
        and bool(files)
    )
    result = {
        "raw_db": str(raw_db),
        "raw_dir": str(raw_dir),
        "raw_integrity_check": str(integrity[0]) if integrity else "missing",
        "raw_foreign_key_violations": foreign_key_count,
        "raw_turn_count": raw_turn_count,
        "projection_journal_file_count": len(files) if isinstance(files, dict) else 0,
        "projection_generation_hash": str(journal.get("generation_hash") or ""),
        "ok": (
            bool(integrity and str(integrity[0]) == "ok")
            and foreign_key_count == 0
            and raw_turn_count > 0
            and journal_ok
        ),
    }
    if not result["ok"]:
        raise RawProjectionBackupReconcileError(
            f"canonical Raw/projection precondition failed: {result}"
        )
    return result


def _safe_candidate(backup_root: Path, backup_id: str) -> Path:
    candidate = backup_root / backup_id
    try:
        candidate_stat = candidate.lstat()
    except OSError as exc:
        raise RawProjectionBackupReconcileError(
            f"backup candidate disappeared: {backup_id}"
        ) from exc
    if not stat.S_ISDIR(candidate_stat.st_mode) or stat.S_ISLNK(candidate_stat.st_mode):
        raise RawProjectionBackupReconcileError(
            f"backup candidate is not a real directory: {backup_id}"
        )
    if candidate.parent.resolve() != backup_root.resolve():
        raise RawProjectionBackupReconcileError(
            f"backup candidate escaped the configured root: {backup_id}"
        )
    for root, directories, files in os.walk(candidate, followlinks=False):
        for name in [*directories, *files]:
            nested = Path(root) / name
            try:
                if stat.S_ISLNK(nested.lstat().st_mode):
                    raise RawProjectionBackupReconcileError(
                        f"backup candidate contains a symlink: {backup_id}"
                    )
            except OSError as exc:
                raise RawProjectionBackupReconcileError(
                    f"backup candidate cannot be safely traversed: {backup_id}"
                ) from exc
    return candidate


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _same_inventory(expected: dict[str, Any], current: dict[str, Any]) -> bool:
    actual = _selection_record(current)
    return actual == expected and _eligible_record(current)


def apply_cleanup(
    backup_root: Path,
    *,
    max_total_bytes: int,
    receipt_dir: Path,
    canonical_preconditions: dict[str, Any],
) -> dict[str, Any]:
    """Apply a stable, metadata-attested deletion plan one directory at a time."""
    plan = plan_cleanup(backup_root, max_total_bytes=max_total_bytes)
    if plan["status"] != "cleanup_required":
        raise RawProjectionBackupReconcileError(
            f"legacy backup cleanup is not eligible: {plan['status']}"
        )
    receipt_path = receipt_dir / RECEIPT_NAME
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "started_at": _utcnow(),
        "backup_root": str(backup_root),
        "max_total_bytes": max_total_bytes,
        "canonical_preconditions": canonical_preconditions,
        "selection_hash": plan["selection_hash"],
        "selected": plan["selected"],
        "completed_backup_ids": [],
        "status": "in_progress",
    }
    _write_receipt(receipt_path, receipt)
    for expected in plan["selected"]:
        candidate = _safe_candidate(backup_root, expected["backup_id"])
        current = _backup_record(candidate)
        if not _same_inventory(expected, current):
            receipt["status"] = "blocked_inventory_drift"
            receipt["blocked_backup_id"] = expected["backup_id"]
            receipt["finished_at"] = _utcnow()
            _write_receipt(receipt_path, receipt)
            raise RawProjectionBackupReconcileError(
                f"legacy backup inventory changed: {expected['backup_id']}"
            )
        shutil.rmtree(candidate)
        receipt["completed_backup_ids"].append(expected["backup_id"])
        _write_receipt(receipt_path, receipt)
    after = plan_cleanup(backup_root, max_total_bytes=max_total_bytes)
    receipt["finished_at"] = _utcnow()
    receipt["after"] = {
        "backup_count": after["audit"]["backup_count"],
        "total_bytes": after["audit"]["total_bytes"],
        "over_budget": after["audit"]["over_budget"],
        "selected_count": after["selected_count"],
        "status": after["status"],
    }
    receipt["status"] = "committed" if after["status"] == "clean" else "incomplete"
    _write_receipt(receipt_path, receipt)
    if after["status"] != "clean":
        raise RawProjectionBackupReconcileError(
            f"legacy backup cleanup did not satisfy the budget: {receipt['after']}"
        )
    return {
        "before": plan,
        "after": after,
        "receipt_path": str(receipt_path),
        "deleted_backup_count": len(receipt["completed_backup_ids"]),
        "deleted_bytes": plan["selected_bytes"],
    }


def _default_backup_root() -> Path:
    config = get_config()
    mnemos_dir = Path(
        getattr(config, "mnemos_dir", None)
        or getattr(config, "data_dir", None)
        or config.database_dir
    )
    return mnemos_dir / "backups"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-root", type=Path, default=None)
    parser.add_argument("--max-total-mb", type=float, default=DEFAULT_MAX_TOTAL_MB)
    parser.add_argument("--raw-db", type=Path)
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--receipt-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    backup_root = args.backup_root.expanduser() if args.backup_root else _default_backup_root()
    budget_bytes = int(max(0.0, args.max_total_mb) * 1024 * 1024)
    result: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "backup_root": str(backup_root),
        "apply": bool(args.apply),
    }
    try:
        result["before"] = plan_cleanup(backup_root, max_total_bytes=budget_bytes)
        if args.apply:
            if args.raw_db is None or args.raw_dir is None or args.receipt_dir is None:
                raise ValueError("--apply requires --raw-db, --raw-dir, and --receipt-dir")
            preconditions = verify_canonical_preconditions(
                args.raw_db.expanduser(), args.raw_dir.expanduser()
            )
            result["result"] = apply_cleanup(
                backup_root,
                max_total_bytes=budget_bytes,
                receipt_dir=args.receipt_dir.expanduser(),
                canonical_preconditions=preconditions,
            )
            result["after"] = result["result"]["after"]
        else:
            result["after"] = result["before"]
        result["ok"] = result["after"]["status"] == "clean"
    except (OSError, sqlite3.Error, RawProjectionBackupReconcileError, ValueError) as exc:
        result["ok"] = False
        result["error"] = str(exc)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
