"""Backup and restore CLI commands."""

from __future__ import annotations

import json
from typing import Any


def _emit(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_backup(args) -> int:
    from core.backup.snapshot_manager import MnemosSnapshotManager, SNAPSHOT_SCHEMA_VERSION
    from core.config import get_config

    manager = MnemosSnapshotManager(get_config())
    cmd = getattr(args, "backup_cmd", "") or "list"
    json_output = bool(getattr(args, "json", False))
    if cmd == "create":
        manifest = manager.create(
            reason=getattr(args, "reason", "") or "manual",
            trigger_action=getattr(args, "trigger_action", "") or "manual",
            dry_run=bool(getattr(args, "dry_run", False)),
        )
        payload = manifest.as_dict()
        payload["status"] = "planned" if manifest.dry_run else "verified"
    else:
        payload = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "status": "ok",
            "snapshots": manager.list_snapshots(),
        }
    _emit(payload, json_output=json_output)
    return 0 if payload.get("status") not in {"failed"} else 1


def cmd_restore(args) -> int:
    from core.backup.snapshot_manager import MnemosSnapshotManager
    from core.config import get_config

    manager = MnemosSnapshotManager(get_config())
    cmd = getattr(args, "restore_cmd", "") or "plan"
    snapshot_id = getattr(args, "snapshot_id", "") or "latest"
    try:
        if cmd == "plan":
            payload = manager.restore_plan(snapshot_id).as_dict()
        elif cmd == "apply":
            payload = manager.restore_apply(
                snapshot_id,
                allow_conflicts=bool(getattr(args, "allow_conflicts", False)),
            ).as_dict()
        else:
            payload = manager.restore_verify(snapshot_id).as_dict()
    except FileNotFoundError as exc:
        payload = {"status": "blocked", "error": str(exc), "snapshot_id": snapshot_id}
    except (OSError, json.JSONDecodeError) as exc:
        payload = {"status": "failed", "error": str(exc), "snapshot_id": snapshot_id}
    _emit(payload, json_output=bool(getattr(args, "json", False)))
    return 0 if payload.get("status") not in {"failed"} else 1
