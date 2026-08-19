# -*- coding: utf-8 -*-
"""
mnemos capsule — 时间胶囊管理

- list: 列出所有胶囊
- due: 列出即将到期
- overdue: 列出已逾期
- set <page> [--days N]: 设置人工提醒
- complete <id>: 标记完成
- dismiss <id>: 忽略提醒
- snooze <id> [--days N]: 推迟 N 天
- report: 生成提醒报告
"""

from __future__ import annotations

from core.config import get_config
from core.kia.aion import TimeCapsule, get_due, set_reminder


def _fmt_reminder(r):
    return (
        f"[{r.capsule_id:>4}] {r.scheduled_date} {r.status:<10} "
        f"{r.reminder_type:<12} {r.page_title or r.page_path}\n"
        f"       {r.reason}"
    )


def cmd_capsule(args) -> int:
    """时间胶囊 CLI 入口。"""
    cfg = get_config()
    cmd = getattr(args, "capsule_cmd", None)

    if cmd == "due":
        days = getattr(args, "days", 7)
        reminders = get_due(days_ahead=days, wiki_base=str(cfg.wiki_dir))
        if not reminders:
            print(f"未来 {days} 天内无到期提醒")
            return 0
        print(f"未来 {days} 天内到期 {len(reminders)} 条")
        for r in reminders:
            print(_fmt_reminder(r))
        return 0

    if cmd == "set":
        page_path = getattr(args, "page_path", None)
        days = getattr(args, "days", 90)
        if not page_path:
            print("错误: 缺少 page_path")
            return 1
        if set_reminder(page_path, days=int(days)):
            print(f"已设置胶囊提醒: {page_path}（{days} 天后）")
            return 0
        print(f"设置胶囊提醒失败: {page_path}")
        return 1

    capsule = TimeCapsule(wiki_base=str(cfg.wiki_dir))

    if cmd == "list":
        page = getattr(args, "page", None)
        status = getattr(args, "status", None)
        reminders = capsule.get_all_reminders(page_path=page, status=status)
        if not reminders:
            print("暂无时间胶囊记录")
            return 0
        print(f"共 {len(reminders)} 条记录")
        for r in reminders:
            print(_fmt_reminder(r))
        return 0

    if cmd == "overdue":
        reminders = capsule.get_overdue_reminders()
        if not reminders:
            print("无已逾期提醒")
            return 0
        print(f"已逾期 {len(reminders)} 条")
        for r in reminders:
            print(_fmt_reminder(r))
        return 0

    if cmd == "complete":
        cid = getattr(args, "capsule_id", None)
        if not cid:
            print("错误: 缺少 capsule_id")
            return 1
        if capsule.complete_reminder(int(cid)):
            print(f"已标记胶囊 {cid} 为完成")
            return 0
        print(f"标记胶囊 {cid} 失败")
        return 1

    if cmd == "dismiss":
        cid = getattr(args, "capsule_id", None)
        if not cid:
            print("错误: 缺少 capsule_id")
            return 1
        if capsule.dismiss_reminder(int(cid)):
            print(f"已忽略胶囊 {cid}")
            return 0
        print(f"忽略胶囊 {cid} 失败")
        return 1

    if cmd == "snooze":
        cid = getattr(args, "capsule_id", None)
        days = getattr(args, "days", 7)
        if not cid:
            print("错误: 缺少 capsule_id")
            return 1
        if capsule.snooze_reminder(int(cid), days=int(days)):
            print(f"已推迟胶囊 {cid} {days} 天")
            return 0
        print(f"推迟胶囊 {cid} 失败")
        return 1

    if cmd == "report":
        print(capsule.generate_reminder_report())
        return 0

    print("未知子命令")
    return 1
