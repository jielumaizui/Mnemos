"""Recap queue CLI commands."""

from __future__ import annotations

import json

from core.app.forced_retrospective import ForcedRetrospective


def _forced() -> ForcedRetrospective:
    return ForcedRetrospective()


def _as_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _print_or_json(data: dict, as_json: bool) -> None:
    if as_json:
        print(_as_json(data))


def cmd_recap(args) -> int:
    """Manage recap task backlog."""
    cmd = getattr(args, "recap_cmd", None)
    forced = _forced()
    as_json = bool(getattr(args, "json", False))

    if cmd == "list":
        tasks = forced.list_recap_tasks(
            status=getattr(args, "status", "pending") or "pending",
            severity=getattr(args, "severity", "") or "",
            source=getattr(args, "source", "") or "",
            limit=getattr(args, "limit", 50) or 50,
        )
        result = {"ok": True, "count": len(tasks), "tasks": [task.__dict__ for task in tasks]}
        if as_json:
            print(_as_json(result))
            return 0
        print(f"recap tasks: {len(tasks)}")
        for task in tasks:
            print(f"  - {task.task_id} [{task.severity}/{task.status}] {task.topic}")
        return 0

    if cmd in {"resolve", "dismiss"}:
        task_id = getattr(args, "task_id", "") or ""
        all_tasks = bool(getattr(args, "all", False))
        if task_id and all_tasks:
            result = {
                "ok": False,
                "error": "task_id_and_all_conflict",
                "message": "task_id 与 --all 只能二选一",
            }
            _print_or_json(result, as_json)
            if not as_json:
                print(result["message"])
            return 1
        if not task_id and not all_tasks:
            result = {
                "ok": False,
                "error": "task_id_or_all_required",
                "message": "需要指定 task_id 或显式 --all",
            }
            _print_or_json(result, as_json)
            if not as_json:
                print(result["message"])
            return 1

        status = "resolved" if cmd == "resolve" else "dismissed"
        reason = getattr(args, "reason", "") or f"cli {cmd}"
        actor = getattr(args, "actor", "") or "cli"
        if task_id:
            changed = 1 if forced.mark_recap_status(task_id, status, reason, actor) else 0
        else:
            changed = forced.close_pending_recaps(
                status,
                severity=getattr(args, "severity", "") or "",
                source=getattr(args, "source", "") or "system",
                reason=reason,
                actor=actor,
                limit=getattr(args, "limit", None),
            )
        result = {"ok": True, "action": cmd, "status": status, "changed": changed}
        if as_json:
            print(_as_json(result))
        else:
            print(f"recap {cmd}: {changed}")
        return 0

    print("用法: mnemos recap {list|resolve|dismiss}")
    return 1
