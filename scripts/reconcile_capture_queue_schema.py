#!/usr/bin/env python3
"""Preview or explicitly migrate the Capture queue schema.

This is the only operational entry point permitted to create or alter
``capture_queue.db``.  Runtime Capture producers fail closed until this has
been run, and status readers never call it.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config
from core.migrations.model_call_ledger_reconcile.runtime import (
    runtime_writers_are_inactive as _runtime_writers_are_inactive,
)
from core.ops.offline_migration_lock import offline_migration_lock
from core.ops.durable_io import (
    DurableIOError,
    secure_publish_immutable_bytes,
)
from core.ops.offline_schema_plan import (
    OfflineSchemaPlanError,
    backup_sqlite_database,
    build_offline_schema_plan,
    restore_sqlite_database,
)
from core.sync_framework.capture_schema import CaptureQueueSchema

_MIGRATION_ID = "capture_queue_schema_v2"
_PLAN_SOURCE_PATHS = (
    Path(__file__),
    ROOT / "core/ops/durable_io.py",
    ROOT / "core/ops/offline_migration_lock.py",
    ROOT / "core/ops/offline_schema_plan.py",
    ROOT / "core/ops/readiness_query_budget.py",
    ROOT / "core/sync_framework/capture_schema.py",
)


def _plan_target_identity(
    plan: dict[str, Any],
    db_path: Path,
) -> dict[str, object] | None:
    physical = plan.get("physical_preimage")
    entries = physical.get("entries") if isinstance(physical, dict) else None
    if not isinstance(entries, list):
        raise OfflineSchemaPlanError("capture_schema_plan_preimage_invalid")
    match = next(
        (
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("path") == str(db_path)
        ),
        None,
    )
    if not isinstance(match, dict):
        raise OfflineSchemaPlanError("capture_schema_plan_preimage_invalid")
    if match.get("present") is False:
        return None
    if (
        match.get("kind") != "file"
        or isinstance(match.get("device"), bool)
        or not isinstance(match.get("device"), int)
        or isinstance(match.get("inode"), bool)
        or not isinstance(match.get("inode"), int)
    ):
        raise OfflineSchemaPlanError("capture_schema_plan_preimage_invalid")
    return {
        "device": match["device"],
        "inode": match["inode"],
    }


def _build_plan(
    *,
    db_path: Path,
    backup_dir: Path | None,
    writers_inactive: bool,
) -> dict[str, Any]:
    return build_offline_schema_plan(
        migration_id=_MIGRATION_ID,
        db_path=db_path,
        backup_dir=backup_dir,
        before=CaptureQueueSchema.inspect(db_path),
        source_paths=_PLAN_SOURCE_PATHS,
        writers_inactive=writers_inactive,
    )


def reconcile(
    *,
    db_path: Path,
    apply: bool,
    backup_dir: Path | None,
    expected_plan_hash: str = "",
    runtime_writers_are_inactive: Callable[[Path], bool] | None = None,
) -> dict[str, Any]:
    db_path = Path(db_path).expanduser().absolute()
    backup_dir = (
        Path(backup_dir).expanduser().absolute()
        if backup_dir is not None
        else None
    )
    writer_check = runtime_writers_are_inactive or _runtime_writers_are_inactive
    writers_inactive = bool(writer_check(db_path.parent))
    plan = _build_plan(
        db_path=db_path,
        backup_dir=backup_dir,
        writers_inactive=writers_inactive,
    )
    result: dict[str, Any] = {
        "schema": "mnemos.capture_queue_schema.reconcile.v1",
        "db_path": str(db_path),
        "before": plan["before"],
        "apply": apply,
        "plan_hash": plan["plan_hash"],
        "apply_required": plan["apply_required"],
        "apply_eligible": plan["apply_eligible"],
        "writer_lock_state": plan["writer_lock_state"],
        "backup": {"present": False, "path": ""},
    }
    if not apply:
        result["after"] = plan["before"]
        result["ok"] = plan["apply_eligible"] is True
        return result
    if backup_dir is None:
        raise ValueError("--apply requires --backup-dir")
    if not expected_plan_hash:
        raise ValueError("expected_plan_hash_required")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", expected_plan_hash) is None:
        raise ValueError("expected_plan_hash_invalid")
    if not writers_inactive:
        raise RuntimeError("capture_schema_writers_not_inactive")
    try:
        with offline_migration_lock(
            db_path.parent,
            daemon_check=writer_check,
        ):
            locked_plan = _build_plan(
                db_path=db_path,
                backup_dir=backup_dir,
                writers_inactive=bool(writer_check(db_path.parent)),
            )
            if expected_plan_hash != locked_plan["plan_hash"]:
                raise ValueError("expected_plan_hash_mismatch")
            if locked_plan["apply_eligible"] is not True:
                raise RuntimeError("capture_schema_plan_not_apply_eligible")
            result["reviewed_plan_hash"] = expected_plan_hash
            result["before"] = locked_plan["before"]
            if locked_plan["apply_required"] is not True:
                result["after"] = locked_plan["before"]
                result["ok"] = True
                return result
            backup_record = backup_sqlite_database(
                db_path,
                backup_dir,
                label="capture-queue.pre-schema-v2",
            )
            result["backup"] = dict(backup_record)
            backup_path = (
                Path(str(backup_record["path"]))
                if backup_record.get("present") is True
                else None
            )
            target_identity = _plan_target_identity(locked_plan, db_path)
            if target_identity is None:
                try:
                    bootstrap_receipt = secure_publish_immutable_bytes(
                        db_path.parent,
                        db_path.name,
                        b"",
                        return_receipt=True,
                    )
                except (DurableIOError, OSError):
                    raise OfflineSchemaPlanError(
                        "capture_schema_bootstrap_target_unavailable"
                    ) from None
                if bootstrap_receipt.created is not True:
                    raise OfflineSchemaPlanError(
                        "capture_schema_bootstrap_target_collision"
                    )
                target_identity = {
                    "device": bootstrap_receipt.preimage.get("device"),
                    "inode": bootstrap_receipt.preimage.get("inode"),
                }
            try:
                result["after"] = CaptureQueueSchema.initialize(db_path)
            except BaseException:
                restore_sqlite_database(
                    backup_path,
                    db_path,
                    expected_target_identity=target_identity,
                )
                raise
    except OfflineSchemaPlanError:
        raise
    except RuntimeError as exc:
        if str(exc) in {
            "capture_schema_writers_not_inactive",
            "capture_schema_plan_not_apply_eligible",
        }:
            raise
        if str(exc) not in {
            "all Mnemos daemon and MCP writers must be stopped",
            "another Mnemos offline migration is active",
            "Mnemos daemon started before migration lock",
            "Mnemos MCP writer started before migration lock",
        }:
            raise
        raise RuntimeError("capture_schema_writer_lock_unavailable") from None
    result["ok"] = result["after"].get("status") == "current"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="capture_queue.db path (defaults to config)")
    parser.add_argument("--apply", action="store_true", help="apply the explicit schema migration")
    parser.add_argument("--backup-dir", type=Path, help="required backup directory for --apply")
    parser.add_argument(
        "--expected-plan-hash",
        default="",
        help="exact dry-run plan hash required for --apply",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    db_path = args.db or (Path(get_config().database_dir) / "capture_queue.db")
    try:
        result = reconcile(
            db_path=db_path,
            apply=args.apply,
            backup_dir=args.backup_dir,
            expected_plan_hash=str(args.expected_plan_hash),
        )
    except (
        OfflineSchemaPlanError,
        OSError,
        sqlite3.Error,
        RuntimeError,
        ValueError,
    ) as exc:
        result = {
            "schema": "mnemos.capture_queue_schema.reconcile.v1",
            "db_path": str(db_path),
            "apply": bool(args.apply),
            "ok": False,
            "error": str(exc),
        }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
