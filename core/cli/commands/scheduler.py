"""Scheduler command for Mnemos CLI."""

import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _build_scheduler():
    from core.kia.chronos import KnowledgeScheduler

    scheduler = KnowledgeScheduler()
    scheduler.register_all_default_steps()
    return scheduler


def _load_runtime_state(scheduler) -> dict:
    db_path = Path(getattr(scheduler, "DB_PATH", ""))
    if not db_path.exists():
        return {}

    state: dict = {}
    try:
        with sqlite3.connect(str(db_path), timeout=5) as conn:
            for step_name, last_run in conn.execute(
                "SELECT step_name, last_run FROM scheduler_step_state"
            ):
                state.setdefault(step_name, {})["last_run"] = last_run or "-"

            for row in conn.execute("""
                SELECT l.step_name, l.started_at, l.status, l.error, l.duration_sec
                FROM scheduler_step_log l
                JOIN (
                    SELECT step_name, MAX(started_at) AS started_at
                    FROM scheduler_step_log
                    GROUP BY step_name
                ) latest
                ON latest.step_name = l.step_name AND latest.started_at = l.started_at
                """):  # noqa: E125
                step_name, started_at, status, error, duration_sec = row
                state.setdefault(step_name, {}).update(
                    {
                        "last_started_at": started_at or "-",
                        "last_status": status or "-",
                        "last_error": error or "",
                        "last_duration_sec": duration_sec,
                    }
                )
    except sqlite3.Error:
        logger.debug("scheduler runtime state unavailable", exc_info=True)
    return state


def _load_live_results(scheduler) -> dict:
    getter = getattr(scheduler, "get_last_results", None)
    if not callable(getter):
        return {}
    try:
        results = getter()
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        logger.debug("scheduler live results unavailable", exc_info=True)
        return {}
    return results if isinstance(results, dict) else {}


def _format_enabled(value: bool) -> str:
    return "enabled" if value else "disabled"


def _format_deps(deps) -> str:
    return ",".join(deps) if deps else "-"


def _print_step_list(scheduler) -> None:
    status = scheduler.get_step_status()
    print("KIA 调度步骤列表")
    if not status:
        print("无已注册步骤")
        return
    for name in sorted(status):
        info = status[name]
        print(
            f"- {name}: {_format_enabled(info.get('enabled', False))}; "
            f"trigger={info.get('trigger', '-')}; "
            f"deps={_format_deps(info.get('deps', []))}; "
            f"timeout={info.get('timeout', '-')}"
        )


def _print_step_status(scheduler) -> None:
    status = scheduler.get_step_status()
    runtime = _load_runtime_state(scheduler)
    live_results = _load_live_results(scheduler)
    print("KIA 调度步骤状态")
    if not status:
        print("无已注册步骤")
        return
    for name in sorted(status):
        info = status[name]
        rt = runtime.get(name, {})
        live = live_results.get(name, {})
        if not isinstance(live, dict):
            live = {}
        print(
            f"- {name}: {_format_enabled(info.get('enabled', False))}; "
            f"trigger={info.get('trigger', '-')}; "
            f"deps={_format_deps(info.get('deps', []))}; "
            f"failures={info.get('consecutive_failures', 0)}; "
            f"last_run={rt.get('last_run', '-')}; "
            f"last_status={rt.get('last_status', '-')}; "
            f"last_started_at={rt.get('last_started_at', '-')}; "
            f"live_status={live.get('status', '-')}; "
            f"live_error={live.get('error', '')}"
        )


def _dry_run_tick(scheduler) -> list[dict]:
    rows = []
    ready = []
    skipped = []

    for step in scheduler.steps.values():
        if not getattr(step, "enabled", False):
            skipped.append((step, "disabled"))
            continue
        try:
            due = bool(step.trigger.is_due())
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            skipped.append((step, f"trigger_error:{exc}"))
            continue
        if due:
            ready.append(step)
        else:
            skipped.append((step, "not_due"))

    if hasattr(scheduler, "_topological_sort"):
        ready = scheduler._topological_sort(ready)

    planned = set()
    for step in ready:
        missing_deps = [dep for dep in getattr(step, "deps", []) if dep not in planned]
        if missing_deps:
            rows.append(
                {
                    "name": step.name,
                    "status": "skipped",
                    "reason": "dependencies_not_met:" + ",".join(missing_deps),
                    "trigger": step.trigger.describe(),
                    "deps": getattr(step, "deps", []),
                }
            )
            continue
        rows.append(
            {
                "name": step.name,
                "status": "would_run",
                "reason": "trigger_due",
                "trigger": step.trigger.describe(),
                "deps": getattr(step, "deps", []),
            }
        )
        planned.add(step.name)

    for step, reason in skipped:
        rows.append(
            {
                "name": step.name,
                "status": "skipped",
                "reason": reason,
                "trigger": step.trigger.describe(),
                "deps": getattr(step, "deps", []),
            }
        )
    return rows


def _print_tick_results(results: dict) -> None:
    print("KIA 调度 tick 执行结果")
    if not results:
        print("无到期步骤")
        return
    for name, result in results.items():
        print(f"- {name}: {result.get('status', 'unknown')}; error={result.get('error', '')}")


def _print_dry_run(rows: list[dict]) -> None:
    print("KIA 调度 tick dry-run")
    if not rows:
        print("无已注册步骤")
        return
    for row in rows:
        print(
            f"- {row['name']}: {row['status']}; "
            f"reason={row['reason']}; "
            f"trigger={row['trigger']}; "
            f"deps={_format_deps(row.get('deps', []))}"
        )


def _task_to_dict(task) -> dict:
    return {
        "task_id": task.task_id,
        "task_type": task.task_type,
        "subtype": task.subtype,
        "due_date": task.due_date,
        "reminder_date": task.reminder_date,
        "status": task.status,
        "context": task.context,
        "reminded_at": task.reminded_at,
        "priority": task.priority,
    }


def _print_reminders(reminders) -> None:
    if not reminders:
        print("无到期知识调度提醒")
        return
    print(f"到期知识调度提醒 ({len(reminders)} 条)")
    for task in reminders:
        print(
            f"- {task.task_id}: {task.task_type}/{task.subtype}; "
            f"due={task.due_date}; reminder={task.reminder_date}; priority={task.priority}"
        )
        if task.context:
            print(f"  context={task.context}")


def _cmd_reminders(args) -> int:
    from core.kia.chronos import check_reminders

    reminders = check_reminders()
    if getattr(args, "json", False):
        payload = {"count": len(reminders), "reminders": [_task_to_dict(task) for task in reminders]}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_reminders(reminders)
    return 0


def _cmd_schedule(args) -> int:
    from core.kia.chronos import schedule_task

    try:
        due_date = datetime.fromisoformat(args.due_date)
    except ValueError:
        print("due_date 必须是 ISO 时间，例如 2026-07-10T09:30:00")
        return 2

    task_id = schedule_task(
        args.task_type,
        args.subtype,
        due_date,
        context=getattr(args, "context", "") or "",
        is_periodic=bool(getattr(args, "periodic", False) or getattr(args, "period", None)),
        period=getattr(args, "period", None),
        priority=getattr(args, "priority", 0),
    )
    print(f"已调度任务: {task_id}")
    return 0


def cmd_scheduler(args):
    """定时任务管理"""
    if args.scheduler_cmd == "install-windows":
        import subprocess
        import mnemos_cli

        daemon_script = Path(mnemos_cli.__file__).parent / "mnemos_daemon.py"
        if not daemon_script.exists():
            print(f"守护进程脚本不存在: {daemon_script}")
            return 1
        result = subprocess.run([sys.executable, str(daemon_script), "install-windows"])
        return result.returncode
    elif args.scheduler_cmd == "uninstall-windows":
        import subprocess
        import mnemos_cli

        daemon_script = Path(mnemos_cli.__file__).parent / "mnemos_daemon.py"
        if not daemon_script.exists():
            print(f"守护进程脚本不存在: {daemon_script}")
            return 1
        result = subprocess.run([sys.executable, str(daemon_script), "uninstall-windows"])
        return result.returncode
    elif args.scheduler_cmd == "reminders":
        return _cmd_reminders(args)
    elif args.scheduler_cmd == "schedule":
        return _cmd_schedule(args)
    elif args.scheduler_cmd in {"status", "list", "tick"}:
        scheduler = _build_scheduler()
        try:
            if args.scheduler_cmd == "status":
                _print_step_status(scheduler)
            elif args.scheduler_cmd == "list":
                _print_step_list(scheduler)
            elif getattr(args, "dry_run", False):
                _print_dry_run(_dry_run_tick(scheduler))
            else:
                _print_tick_results(scheduler.tick())
        finally:
            shutdown = getattr(scheduler, "shutdown", None)
            if callable(shutdown):
                shutdown()
        return 0
    else:
        print("可用子命令: install-windows, uninstall-windows, status, list, reminders, schedule, tick")
        return 2
