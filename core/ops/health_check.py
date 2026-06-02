#!/usr/bin/env python3
"""
Mnemos 健康检查脚本 — Phase 8 运维基础设施

检查项：
1. 进程状态（daemon、Memos）
2. 队列健康（amphora、EventBus）
3. 存储健康（数据库大小、Inbox 堆积）
4. API 健康（LLM 可用性、配额）
5. 磁盘空间

用法：
    python3 -m core.ops.health_check
    python3 -m core.ops.health_check --json
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

# Ensure project root in path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.config import get_config


def check_process(name: str, pattern: str) -> Dict:
    """检查进程是否存在（过滤编辑器误匹配）"""
    try:
        result = subprocess.run(
            ["pgrep", "-af", pattern],
            capture_output=True,
            text=True,
        )
        lines = [ln.strip() for ln in result.stdout.strip().split("\n") if ln.strip()]
        # 排除编辑器/搜索工具的误匹配
        excluded = {"vi ", "vim ", "nvim ", "code ", "cursor ", "grep ", "rg ", "cat ", "less ", "more "}
        valid = []
        for ln in lines:
            if any(ln.lower().startswith(ex) for ex in excluded):
                continue
            parts = ln.split(None, 1)
            if parts:
                valid.append(int(parts[0]))
        return {
            "name": name,
            "running": len(valid) > 0,
            "pids": valid,
        }
    except Exception as e:
        return {"name": name, "running": False, "error": str(e)}


def check_memos_api() -> Dict:
    """检查 Memos API 是否可达（使用统一诊断层）"""
    try:
        from core.diagnostics import ConnectionDiagnostics
        status = ConnectionDiagnostics.check_memos()
        return {
            "reachable": status.reachable is True,
            "configured": status.configured,
            "error": status.error,
        }
    except Exception as e:
        return {"reachable": False, "error": str(e)}


def check_amphora() -> Dict:
    """检查 amphora 蒸馏队列状态"""
    try:
        from core.kia import amphora
        pending = amphora.list_pending()
        processing = amphora.list_processing()
        done = amphora.get_task_count("done")
        failed = amphora.get_task_count("failed")
        return {
            "pending": len(pending),
            "processing": len(processing),
            "done": done,
            "failed": failed,
            "healthy": len(pending) < 50 and len(processing) < 5,
        }
    except Exception as e:
        return {"error": str(e), "healthy": False}


def check_event_bus() -> Dict:
    """检查 EventBus 积压情况"""
    db_path = Path.home() / ".mnemos" / "events.db"
    if not db_path.exists():
        return {"db_exists": False, "healthy": True}
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM events WHERE status='pending'")
        pending = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM dead_letters")
        dead = cursor.fetchone()[0]
        conn.close()
        return {
            "pending_events": pending,
            "dead_letters": dead,
            "healthy": pending < 1000 and dead < 10,
        }
    except Exception as e:
        return {"error": str(e), "healthy": False}


def check_storage() -> Dict:
    """检查存储使用情况"""
    cfg = get_config()
    results = {}
    # Disk usage
    stat = shutil.disk_usage(str(Path.home()))
    results["disk_free_gb"] = round(stat.free / (1024**3), 2)
    results["disk_total_gb"] = round(stat.total / (1024**3), 2)
    results["disk_used_pct"] = round((stat.total - stat.free) / stat.total * 100, 1)
    results["disk_healthy"] = results["disk_used_pct"] < 90

    # Database sizes
    db_files = [
        ("events", Path.home() / ".mnemos" / "events.db"),
        ("wiki_state", Path.home() / ".mnemos" / "wiki_state.db"),
        ("user_signals", Path.home() / ".mnemos" / "user_signals.db"),
        ("distill_queue", cfg.claude_data_dir / "distill_queue.db"),
    ]
    results["databases"] = {}
    for name, path in db_files:
        if path.exists():
            size_mb = round(path.stat().st_size / (1024**2), 2)
            results["databases"][name] = {"size_mb": size_mb, "healthy": size_mb < 100}

    # Inbox backlog
    inbox = cfg.wiki_dir / "00-Inbox"
    if inbox.exists():
        count = len(list(inbox.glob("*.md")))
        results["inbox_count"] = count
        results["inbox_healthy"] = count < 200

    return results


def check_api() -> Dict:
    """检查 LLM API 可用性"""
    try:
        from core.hephaestus.distillation_engine import HostAgentCaller
        caller = HostAgentCaller(force_provider="api", timeout=10)
        result = caller._invoke("Reply with OK only", timeout=10)
        return {
            "reachable": result is not None and len(result) > 0,
            "response_preview": result[:50] if result else None,
        }
    except Exception as e:
        return {"reachable": False, "error": str(e)}


def run_health_check() -> Dict:
    """运行完整健康检查"""
    return {
        "timestamp": datetime.now().isoformat(),
        "processes": [
            check_process("mnemos_daemon", "mnemos_daemon"),
            # Memos 是外部 API 服务（可能跑在 Docker/远程），进程检查不可靠。
            # 用 API 可达性代替进程检查（check_memos_api 已覆盖）。
        ],
        "memos_api": check_memos_api(),
        "amphora": check_amphora(),
        "event_bus": check_event_bus(),
        "storage": check_storage(),
        "api": check_api(),
    }


def print_report(report: Dict):
    """打印人类可读的健康报告"""
    print(f"\n{'='*60}")
    print(f"Mnemos 健康检查报告 — {report['timestamp']}")
    print(f"{'='*60}")

    # Processes
    print("\n[进程状态]")
    for p in report["processes"]:
        status = "运行中" if p["running"] else "未运行"
        icon = "🟢" if p["running"] else "🔴"
        print(f"  {icon} {p['name']}: {status} {p.get('pids', '')}")

    # Memos API
    m = report["memos_api"]
    status = "可达" if m.get("reachable") else "不可达"
    icon = "🟢" if m.get("reachable") else "🔴"
    print(f"\n[Memos API] {icon} {status}")
    if not m.get("reachable"):
        print(f"  错误: {m.get('error', 'Unknown')}")

    # Amphora
    a = report["amphora"]
    status = "健康" if a.get("healthy") else "异常"
    icon = "🟢" if a.get("healthy") else "🔴"
    print(f"\n[Amphora 队列] {icon} {status}")
    print(f"  Pending: {a.get('pending', 'N/A')} | Processing: {a.get('processing', 'N/A')}")
    print(f"  Done: {a.get('done', 'N/A')} | Failed: {a.get('failed', 'N/A')}")

    # EventBus
    e = report["event_bus"]
    status = "健康" if e.get("healthy") else "积压"
    icon = "🟢" if e.get("healthy") else "🟡"
    print(f"\n[EventBus] {icon} {status}")
    print(f"  Pending events: {e.get('pending_events', 'N/A')}")
    print(f"  Dead letters: {e.get('dead_letters', 'N/A')}")

    # Storage
    s = report["storage"]
    disk_status = "🟢" if s.get("disk_healthy") else "🔴"
    print(f"\n[存储] {disk_status}")
    print(f"  磁盘: {s['disk_used_pct']}% 已用 ({s['disk_free_gb']} GB 空闲)")
    print(f"  Inbox: {s.get('inbox_count', 'N/A')} 个文件")
    for db_name, db_info in s.get("databases", {}).items():
        db_icon = "🟢" if db_info["healthy"] else "🔴"
        print(f"  DB {db_name}: {db_info['size_mb']} MB {db_icon}")

    # API
    api = report["api"]
    status = "可用" if api.get("reachable") else "不可用"
    icon = "🟢" if api.get("reachable") else "🔴"
    print(f"\n[LLM API] {icon} {status}")
    if not api.get("reachable"):
        print(f"  错误: {api.get('error', 'Unknown')}")

    print(f"\n{'='*60}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Mnemos 健康检查")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = parser.parse_args()

    report = run_health_check()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
