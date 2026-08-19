"""Data ownership CLI commands."""

from __future__ import annotations

import json
import time
from typing import Any


def _emit(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_data(args) -> int:
    from core.config import get_config
    from core.privacy.data_ownership import DataOwnershipManager

    config = get_config()
    manager = DataOwnershipManager(config)
    cmd = getattr(args, "data_cmd", "") or "inventory"
    json_output = bool(getattr(args, "json", False))
    try:
        if cmd == "inventory":
            payload = manager.inventory()
        elif cmd == "export":
            payload = manager.export(
                getattr(args, "scope", "all"),
                dry_run=bool(getattr(args, "dry_run", False)),
            ).as_dict()
            payload["status"] = "planned" if payload.get("dry_run") else "verified"
        elif cmd == "freeze":
            payload = manager.freeze(
                getattr(args, "scope", "all"),
                reason=getattr(args, "reason", "") or "user_request",
            ).as_dict()
        elif cmd == "snapshot":
            manifest = manager.create_delete_snapshot(
                getattr(args, "scope", "all"),
                retention_days=int(getattr(args, "retention_days", 30)),
            )
            payload = {
                "status": "verified",
                "snapshot_id": manifest.snapshot_id,
                "schema_version": manifest.schema_version,
                "trigger_action": manifest.trigger_action,
                "retention_expires_at": manifest.retention_expires_at,
                "retention_policy": manifest.retention_policy,
                "payload_count": len(manifest.file_entries)
                + len(manifest.database_entries),
            }
        else:
            scope = getattr(args, "scope", "all")
            delete_kwargs = {
                "dry_run": bool(getattr(args, "dry_run", False)),
                "apply": bool(getattr(args, "apply", False)),
                "confirm": bool(getattr(args, "confirm", False)),
                "snapshot_ref": getattr(args, "snapshot_ref", "") or "",
            }
            if not delete_kwargs["apply"] or delete_kwargs["dry_run"]:
                payload = manager.delete(
                    scope,
                    dry_run=bool(delete_kwargs["dry_run"]),
                    apply=bool(delete_kwargs["apply"]),
                    confirm=bool(delete_kwargs["confirm"]),
                    snapshot_ref=str(delete_kwargs["snapshot_ref"]),
                ).as_dict()
            else:
                payload = _apply_delete_with_config_bound_bus(
                    config,
                    scope=scope,
                    delete_kwargs=delete_kwargs,
                )
        if "status" not in payload:
            payload["status"] = "ok"
    except (ValueError, PermissionError, OSError) as exc:
        payload = {"status": "failed", "error": str(exc)}
    _emit(payload, json_output=json_output)
    return 0 if payload.get("status") not in {"failed"} else 1


def _wait_for_event_bus(event_bus: Any, *, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    stable = 0
    while time.monotonic() < deadline:
        stats = event_bus.stats()
        idle = (
            int(stats.get("pending", 0)) == 0
            and int(stats.get("processing", 0)) == 0
            and int(stats.get("queue_depth", 0)) == 0
        )
        stable = stable + 1 if idle else 0
        if stable >= 3:
            return True
        time.sleep(0.03)
    return False


def _apply_delete_with_config_bound_bus(
    config: Any,
    *,
    scope: str,
    delete_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Run a confirmed delete with projection consumers bound to one config."""

    from core.mnemos_bus import EventBus
    from core.privacy.data_ownership import DataOwnershipManager
    from core.wiki_projection_lifecycle import resolve_wiki_projection_db_path

    projection_db = resolve_wiki_projection_db_path(config)
    if not projection_db.is_file():
        return DataOwnershipManager(config).delete(scope, **delete_kwargs).as_dict()

    from core.cognitive_graph import CognitiveGraphStore, CognitiveGraphUpdater
    from daemon.wiki_projection_handlers import register_wiki_projection_handlers

    event_bus = EventBus(config=config)
    graph_store = CognitiveGraphStore(
        str(config.database_dir / "cognitive_graph.db"),
        ownership_config=config,
    )
    try:
        register_wiki_projection_handlers(event_bus, config)
        CognitiveGraphUpdater(store=graph_store, bus=event_bus).subscribe()
        manager = DataOwnershipManager(config, event_bus=event_bus)
        event_bus.start_dispatch()
        first = manager.delete(scope, **delete_kwargs)
        drained = _wait_for_event_bus(event_bus)
        final = manager.delete(scope, **delete_kwargs) if drained else first
        payload = final.as_dict()
        payload["projection_dispatch_drained"] = drained
        return payload
    finally:
        event_bus.stop_dispatch()
        event_bus.close()
