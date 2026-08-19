"""Daemon command for Mnemos CLI."""

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def cmd_daemon(args):
    """后台守护进程管理"""
    import subprocess
    import mnemos_cli

    daemon_script = Path(mnemos_cli.__file__).parent / "mnemos_daemon.py"
    if not daemon_script.exists():
        print(f"守护进程脚本不存在: {daemon_script}")
        return

    if args.daemon_cmd == "start":
        subprocess.run([sys.executable, str(daemon_script), "start"])
    elif args.daemon_cmd == "stop":
        subprocess.run([sys.executable, str(daemon_script), "stop"])
    elif args.daemon_cmd == "status":
        subprocess.run([sys.executable, str(daemon_script), "status"])
    elif args.daemon_cmd == "run":
        subprocess.run([sys.executable, str(daemon_script), "run"])
    else:
        print("可用子命令: start, stop, status, run")
