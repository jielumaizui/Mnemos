"""Persona command for Mnemos CLI."""

import json
import logging
import sqlite3
from datetime import datetime

logger = logging.getLogger(__name__)


def cmd_persona(args):
    """Persona 画像系统 CLI 入口。"""
    cmd = getattr(args, "persona_cmd", None)
    if cmd == "behavior-metrics":
        return cmd_persona_behavior_metrics(args)
    if cmd == "daily-summary":
        return cmd_persona_daily_summary(args)
    if cmd == "projects":
        return cmd_persona_projects(args)
    if cmd == "project-signals":
        return cmd_persona_project_signals(args)
    if cmd == "recent-signals":
        return cmd_persona_recent_signals(args)
    print(
        "用法: mnemos persona "
        "{behavior-metrics|daily-summary|projects|project-signals|recent-signals}"
    )
    return 0


def cmd_persona_behavior_metrics(args):
    """输出画像行为提示最近 N 天的使用指标。"""
    days = getattr(args, "days", 30)
    try:
        from core.persona.behavior_tracker import BehaviorPromptTracker

        metrics = BehaviorPromptTracker().get_metrics(days=days)
        if not metrics.get("success", True) and "error" in metrics:
            print(f"获取指标失败: {metrics['error']}")
            return 1

        print(f"画像行为提示指标（最近 {metrics['days']} 天）")
        print("=" * 50)
        print(f"总调用次数: {metrics['total_calls']}")

        if metrics["by_agent"]:
            print("\n按 Agent 分布:")
            for agent, count in metrics["by_agent"].items():
                print(f"  {agent}: {count}")

        if metrics["by_source"]:
            print("\n按来源分布:")
            for source, count in metrics["by_source"].items():
                print(f"  {source}: {count}")

        if metrics["ab_test"]:
            print("\nA/B 分组:")
            for group, count in metrics["ab_test"].items():
                print(f"  {group}: {count}")

        if metrics["by_strategy"]:
            print("\nTop 策略:")
            for strategy, count in list(metrics["by_strategy"].items())[:10]:
                print(f"  {strategy}: {count}")

        if metrics["daily_calls"]:
            print("\n每日调用:")
            for day in metrics["daily_calls"]:
                print(f"  {day['date']}: {day['count']}")

        return 0
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as e:
        print(f"获取指标失败: {e}")
        return 1


def cmd_persona_daily_summary(args):
    """输出指定日期的画像信号聚合摘要。"""
    date = getattr(args, "date", "") or datetime.now().date().isoformat()
    try:
        from core.persona.psyche import SignalStore

        store = SignalStore()
        try:
            summary = store.get_daily_summary(date)
        finally:
            store.close()
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as e:
        print(f"获取日摘要失败: {e}")
        return 1

    if getattr(args, "json", False):
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if not summary:
        print(f"{date} 无画像信号摘要")
        return 0

    print(f"画像信号日摘要（{date}）")
    print("=" * 40)
    for source_type, data in sorted(summary.items()):
        print(f"  - {source_type}: {data.get('signal_count', 0)}")
        details = data.get("summary") or {}
        for key, value in sorted(details.items()):
            if value:
                print(f"    {key}: {value}")
    return 0


def cmd_persona_project_signals(args):
    """输出指定项目隔离后的画像信号。"""
    project_dir = getattr(args, "project_dir", "")
    days = getattr(args, "days", 30)
    try:
        from core.persona.psyche import SignalStore

        store = SignalStore()
        try:
            signals = store.get_project_isolated_signals(project_dir, days)
        finally:
            store.close()
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as e:
        print(f"获取项目隔离信号失败: {e}")
        return 1

    if getattr(args, "json", False):
        print(json.dumps(signals, ensure_ascii=False, indent=2))
        return 0

    print(f"项目隔离画像信号（最近 {days} 天）")
    print("=" * 40)
    print(f"项目: {project_dir}")
    for source_type in ("session", "git", "file_system"):
        print(f"  - {source_type}: {len(signals.get(source_type, []))}")
    return 0


def cmd_persona_projects(args):
    """输出最近有画像信号的项目列表。"""
    days = getattr(args, "days", 30)
    try:
        from core.persona.psyche import SignalStore

        store = SignalStore()
        try:
            projects = store.get_signal_projects(days)
        finally:
            store.close()
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as e:
        print(f"获取画像信号项目失败: {e}")
        return 1

    if getattr(args, "json", False):
        print(json.dumps(projects, ensure_ascii=False, indent=2))
        return 0

    print(f"画像信号项目（最近 {days} 天）")
    print("=" * 40)
    if not projects:
        print("  无项目信号")
        return 0
    for project in projects:
        print(
            f"  - {project.get('identifier', '')} "
            f"[{project.get('type', '')}]: {project.get('signal_count', 0)}"
        )
    return 0


def cmd_persona_recent_signals(args):
    """输出最近的 notes/wechat 原始画像信号。"""
    source = getattr(args, "source", "all")
    days = getattr(args, "days", 30)
    if source not in {"all", "notes", "wechat"}:
        print("source 只能是 all、notes 或 wechat")
        return 2

    try:
        from core.persona.psyche import SignalStore

        store = SignalStore()
        try:
            signals = {}
            if source in {"all", "notes"}:
                signals["notes"] = store.get_recent_note_signals(days)
            if source in {"all", "wechat"}:
                signals["wechat"] = store.get_recent_wechat_signals(days)
        finally:
            store.close()
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as e:
        print(f"获取最近画像信号失败: {e}")
        return 1

    if getattr(args, "json", False):
        print(json.dumps(signals, ensure_ascii=False, indent=2))
        return 0

    print(f"最近画像原始信号（最近 {days} 天）")
    print("=" * 40)
    for source_type in ("notes", "wechat"):
        if source_type in signals:
            print(f"  - {source_type}: {len(signals[source_type])}")
    return 0
