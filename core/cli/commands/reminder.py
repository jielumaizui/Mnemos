# -*- coding: utf-8 -*-
"""
mnemos reminder — 对话提醒队列管理

- list [--status pending|pushed|resolved|all]: 列出提醒
- push [--max N]: 手动触发兜底推送
- resolve <reminder_id>|--issue <issue_id> [--choice ...]: 手动关闭提醒
- dismiss <reminder_id>|--issue <issue_id> [--reason ...]: 忽略提醒
- expire-stale [--days N]: 过期旧 pending/deferred 提醒
"""

from __future__ import annotations

import json
import hashlib


from core import config as _config_mod
from core.cli.local_principal import local_cli_identity
from core.kia.dialog_reminder import (
    DialogReminderQueue,
    get_dialog_reminder_queue,
    get_reminder_renderer,
)


def _queue() -> DialogReminderQueue:
    cfg = _config_mod.get_config()
    return get_dialog_reminder_queue(db_path=str(cfg.database_dir / "dialog_reminder.db"))


def cmd_reminder(args) -> int:
    """提醒 CLI 入口。"""
    cmd = getattr(args, "reminder_cmd", None)
    queue = _queue()

    if cmd == "list":
        status = getattr(args, "status", "all") or "all"
        reminders = queue.list_reminders(status=status, limit=100)
        if not reminders:
            print(f"暂无 {status} 提醒")
            return 0
        print(f"共 {len(reminders)} 条 {status} 提醒")
        for r in reminders:
            title = (r.content or "").split("\n")[0][:60]
            print(f"  - {r.reminder_id} [{r.severity}/{r.status}] {title}")
        return 0

    if cmd == "status":
        stats = queue.count_by_status()
        if getattr(args, "json", False):
            print(json.dumps({"ok": True, "status_counts": stats}, ensure_ascii=False, indent=2))
        else:
            print("提醒队列状态:")
            for status, count in sorted(stats.items()):
                print(f"  {status}: {count}")
        return 0

    if cmd == "push":
        max_results = getattr(args, "max", None)
        principal, _narrowing = local_cli_identity(project="mnemos")
        pushed = queue.on_user_active(max_results=max_results, principal=principal)
        if not pushed:
            print("无待推送提醒")
            return 0
        renderer = get_reminder_renderer()
        print(f"已推送 {len(pushed)} 条提醒:")
        for r in pushed:
            rendered = renderer.render_dialog(r)
            print(rendered)
            queue.record_presentation(
                r.reminder_id,
                principal=principal,
                rendered_content_hash="sha256:"
                + hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            )
        return 0

    if cmd == "resolve":
        reminder_id = getattr(args, "reminder_id", None)
        issue_id = getattr(args, "issue", None)
        if reminder_id and issue_id:
            print("错误: reminder_id 与 --issue 只能二选一")
            return 1
        if issue_id:
            entry = queue.get_by_issue(issue_id)
            if not entry:
                print(f"未找到未关闭 issue 提醒: {issue_id}")
                return 1
            reminder_id = entry.reminder_id
        if not reminder_id:
            print("错误: 缺少 reminder_id 或 --issue")
            return 1
        choice = getattr(args, "choice", "已处理") or "已处理"
        principal, narrowing = local_cli_identity(project="mnemos")
        result = queue.record_user_response(
            reminder_id,
            "resolve",
            principal=principal,
            narrowing=narrowing,
            choice=choice,
        )
        ok = bool(result.get("success"))
        if ok:
            if issue_id:
                print(f"已关闭 issue 提醒: {issue_id} ({reminder_id}) -> {choice}")
            else:
                print(f"已关闭提醒: {reminder_id} -> {choice}")
        else:
            print(f"未找到提醒: {reminder_id}")
            return 1
        return 0

    if cmd == "dismiss":
        reminder_id = getattr(args, "reminder_id", None)
        issue_id = getattr(args, "issue", None)
        if reminder_id and issue_id:
            print("错误: reminder_id 与 --issue 只能二选一")
            return 1
        if issue_id:
            entry = queue.get_by_issue(issue_id)
            if not entry:
                print(f"未找到未关闭 issue 提醒: {issue_id}")
                return 1
            reminder_id = entry.reminder_id
        if not reminder_id:
            print("错误: 缺少 reminder_id 或 --issue")
            return 1
        reason = getattr(args, "reason", "dismissed") or "dismissed"
        principal, narrowing = local_cli_identity(project="mnemos")
        result = queue.record_user_response(
            reminder_id,
            "dismiss",
            principal=principal,
            narrowing=narrowing,
            reason=reason,
        )
        ok = bool(result.get("success"))
        if ok:
            print(f"已忽略提醒: {reminder_id} -> {reason}")
            return 0
        print(f"未找到提醒: {reminder_id}")
        return 1

    if cmd == "expire-stale":
        days = int(getattr(args, "days", 30) or 30)
        limit = getattr(args, "limit", None)
        severity = getattr(args, "severity", "") or ""
        expired = queue.expire_stale_pending(days=days, limit=limit, severity=severity)
        result = {
            "ok": True,
            "expired": expired,
            "days": days,
            "severity": severity,
        }
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"已过期提醒: {expired}")
        return 0

    print("未知子命令")
    return 1
