from __future__ import annotations

#!/usr/bin/env python3
"""
Memos Master Scheduler - Memos 主调度器
整合所有Memos相关定时任务：
- Heat Scoring 衰减检查 & 周报
- Hermes Monitor 轮询
- Knowledge Inbox 处理
"""

import os
import subprocess
from pathlib import Path
from datetime import datetime
from core.config import get_config

MEMOS_CLIENT = get_config().wiki_dir.parent
SCRIPTS = {
    "heat_monitor": MEMOS_CLIENT / "heat_monitor.py",
    "hermes_monitor": MEMOS_CLIENT / "hermes_monitor.py",
    "inbox_processor": MEMOS_CLIENT / "knowledge_inbox.py",
}

CRON_CONFIG = """# Memos Master Scheduler - Generated {timestamp}
# Heat Scoring System
# Daily decay check at 2:00 AM
0 2 * * * {python} {heat_monitor} --daily
# Weekly report - Saturday and Sunday at 3:00 PM
0 15 * * 6 {python} {heat_monitor} --weekly
0 15 * * 0 {python} {heat_monitor} --weekly
# L5 Archive check - Sunday at 3:00 AM
0 3 * * 0 {python} {heat_monitor} --l5-archive

# Note: Knowledge Inbox is now manual trigger only
# Run: python3 ~/memos-client/knowledge_inbox.py --run

# Note: Hermes Monitor disabled - handled by Hermes directly
"""


class MemMaster:
    """Memos 主调度器类"""

    def __init__(self):
        self.scripts = {
            "heat_monitor": get_config().wiki_dir.parent / "heat_monitor.py",
            "hermes_monitor": get_config().wiki_dir.parent / "hermes_monitor.py",
            "inbox_processor": get_config().wiki_dir.parent / "knowledge_inbox.py",
        }

    def install(self):
        """安装 cron 任务"""
        install()

    def manual_run(self, component: str):
        """手动运行组件"""
        manual_run(component)

    def status(self):
        """查看状态"""
        status()

    def run(self, component: str = None):
        """运行命令"""
        import argparse
        parser = argparse.ArgumentParser(description="Memos Master Scheduler")
        parser.add_argument("--install", action="store_true", help="Generate cron config")
        parser.add_argument("--run", choices=["heat", "inbox", "hermes"], help="Manual run component")
        parser.add_argument("--status", action="store_true", help="Check all status")

        args = parser.parse_args()

        if args.install:
            install()
        elif args.run:
            manual_run(args.run)
        elif args.status:
            status()
        else:
            status()


def install():
    """【已废弃】安装所有cron任务。新体系使用 launchd 管理调度。"""
    print("[DEPRECATED] memmaster 已废弃。新体系使用 launchd 管理所有定时任务。")
    print("请查看 ~/Library/LaunchAgents/ 下的 plist 配置。")


def manual_run(component: str):
    """手动运行某个组件"""
    script = SCRIPTS.get(component)
    if not script or not script.exists():
        print(f"Script not found: {component}")
        return

    print(f"Running {component}...")
    result = subprocess.run(
        [sys.executable, script, "--run"],
        capture_output=True,
        text=True
    )
    print(result.stdout)
    if result.stderr:
        print("Errors:", result.stderr)


def status():
    """查看所有组件状态"""
    print("=== Memos System Status ===\n")

    # Check Heat Scoring
    print("1. Heat Scoring:")
    if SCRIPTS["heat_monitor"].exists():
        result = subprocess.run(
            [sys.executable, SCRIPTS["heat_monitor"], "--run-all"],
            capture_output=True,
            text=True
        )
        # Parse summary only
        lines = result.stdout.split("\n")
        for line in lines[:20]:
            if line.strip():
                print(f"   {line}")
    else:
        print("   Not installed")

    print("\n2. Knowledge Inbox:")
    if SCRIPTS["inbox_processor"].exists():
        result = subprocess.run(
            [sys.executable, SCRIPTS["inbox_processor"], "--status"],
            capture_output=True,
            text=True
        )
        lines = result.stdout.split("\n")
        for line in lines[:10]:
            if line.strip():
                print(f"   {line}")
    else:
        print("   Not installed")

    print("\n3. Hermes Monitor:")
    if SCRIPTS["hermes_monitor"].exists():
        result = subprocess.run(
            [sys.executable, SCRIPTS["hermes_monitor"], "--stats"],
            capture_output=True,
            text=True
        )
        lines = result.stdout.split("\n")
        for line in lines[:10]:
            if line.strip():
                print(f"   {line}")
    else:
        print("   Not installed")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Memos Master Scheduler")
    parser.add_argument("--install", action="store_true", help="Generate cron config")
    parser.add_argument("--run", choices=["heat", "inbox", "hermes"], help="Manual run component")
    parser.add_argument("--status", action="store_true", help="Check all status")

    args = parser.parse_args()

    if args.install:
        install()
    elif args.run:
        manual_run(args.run)
    elif args.status:
        status()
    else:
        status()


if __name__ == "__main__":
    main()
