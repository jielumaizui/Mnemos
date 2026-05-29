from __future__ import annotations

"""
定时任务调度系统
- 热力衰减
- 冷降级
- 草稿整理
- 健康检查
- 周报生成

NOTE: 本模块是配置层的调度器，负责 launchd plist 生成和任务安装。
      core/kia/chronos.py 负责运行时任务队列管理，两者互补不冲突。
"""

import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

from core.config import get_config


class Scheduler:
    """
    定时任务调度器
    基于 macOS launchd，支持 Linux (systemd/cron) 和 Windows (Task Scheduler) 回退
    """

    PLATFORM = sys.platform
    TASKS = {
        "draft_clean": {
            "cron": "0 */4 * * *",
            "description": "每4小时整理Memos草稿（错过跳过）",
            "script": "clean_drafts.py",
            "log": "draft_clean.log",
            "skip_if_missed": True,
        },
        "heat_decay": {
            "cron": "0 11 * * *",
            "description": "每天上午11点执行热力衰减",
            "script": "heat_decay.py",
            "log": "heat_decay.log",
            "skip_if_missed": True,
        },
        "cold_demotion": {
            "cron": "0 13 * * *",
            "description": "每天下午1点执行冷降级",
            "script": "cold_demotion.py",
            "log": "cold_demotion.log",
            "skip_if_missed": True,
        },
        "expand_scan": {
            "cron": "0 12 * * *",
            "description": "每天中午12点扫描Expand候选",
            "script": "expand_scan.py",
            "log": "expand_scan.log",
            "skip_if_missed": True,
        },
        "wiki_tags_sync": {
            "cron": "0 14 * * *",
            "description": "每天下午2点同步Wiki标签（错过跳过）",
            "script": "sync_wiki_tags.py",
            "log": "wiki_tags_sync.log",
            "skip_if_missed": True,
        },
        "synthesis_pipeline": {
            "cron": "0 16 * * 0",
            "description": "每周日16点执行知识合成",
            "script": "synthesis_pipeline.py",
            "log": "synthesis_pipeline.log",
            "skip_if_missed": True,
        },
        "weekly_report": {
            "cron": "0 10 * * 1",
            "description": "每周一上午10点生成周报",
            "script": "wrapper_weekly_report.py",
            "log": "weekly_report.log",
            "skip_if_missed": True,
        },
        "health_check": {
            "cron": "0 15 * * *",
            "description": "每天下午3点执行健康检查",
            "script": "health_check.py",
            "log": "health_check.log",
            "skip_if_missed": True,
        },
    }

    def __init__(self, config_path: str = None):
        if config_path:
            self.config_path = Path(config_path).expanduser()
        else:
            self.config_path = get_config().data_dir / "config"
        self.logs_path = get_config().data_dir / "logs"
        self.logs_path.mkdir(parents=True, exist_ok=True)

    def _get_schedule_minutes(self, task: dict) -> int:
        """获取调度间隔（分钟）"""
        cron = task["cron"]
        parts = cron.split()

        if parts[1] == "*/4":
            return 240
        elif parts[1] == "11":
            return 24 * 60
        elif parts[1] == "13":
            return 24 * 60
        elif parts[1] == "10" and parts[4] == "1":
            return 7 * 24 * 60
        elif parts[1] == "15":
            return 24 * 60
        return 60

    def generate_wrapper_script(self, task_name: str) -> str:
        """生成包装脚本，处理跳过逻辑"""
        task = self.TASKS.get(task_name)

        schedule_minutes = self._get_schedule_minutes(task)

        # 修复：任务完成后再写入 lock 文件，避免首次运行误判
        skip_logic = f'''
import os
import sys
from datetime import datetime, timedelta

LOCK_FILE = str(get_config().data_dir / "locks" / f"{task_name}.lock")
SCHEDULE_INTERVAL = {schedule_minutes}
SHOULD_SKIP = False

os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)

if os.path.exists(LOCK_FILE):
    with open(LOCK_FILE, 'r') as f:
        last_run = f.read().strip()
    try:
        last_time = datetime.fromisoformat(last_run)
        elapsed = datetime.now() - last_time
        # 如果超过1.5倍调度间隔，说明错过了（关机/休眠）
        if elapsed > timedelta(minutes=SCHEDULE_INTERVAL * 1.5):
            print(f"[{{datetime.now().isoformat()}}] Skipped: missed schedule (was off for {{elapsed}})")
            SHOULD_SKIP = True
    except ValueError:
        pass

if SHOULD_SKIP:
    sys.exit(0)

# 先记录启动时间，任务完成后更新为完成时间
START_TIME = datetime.now().isoformat()
with open(LOCK_FILE, 'w') as f:
    f.write(START_TIME)
'''

        wrapper = f"""#!/usr/bin/env python3
{skip_logic}

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 使用相对路径加载脚本
import runpy
SCRIPT_PATH = Path(__file__).parent / "{task['script']}"
runpy.run_path(str(SCRIPT_PATH), run_name="__main__")

# 任务成功完成后，更新时间戳
from datetime import datetime
with open(LOCK_FILE, 'w') as f:
    f.write(datetime.now().isoformat())
"""
        return wrapper

    def generate_plist(self, task_name: str) -> str:
        """生成launchd plist配置"""
        task = self.TASKS.get(task_name)
        if not task:
            raise ValueError(f"Unknown task: {task_name}")

        minute, hour, day, month, weekday = task["cron"].split()

        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.memos.wiki.{task_name}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{self.config_path.parent}/scripts/.wrapper_{task_name}.py</string>
    </array>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Minute</key>
        <integer>{minute}</integer>
        <key>Hour</key>
        <integer>{hour}</integer>
"""

        if weekday != "*":
            plist_content += f"""        <key>Weekday</key>
        <integer>{weekday}</integer>
"""

        plist_content += f"""    </dict>

    <key>StandardOutPath</key>
    <string>{self.logs_path}/{task["log"]}</string>

    <key>StandardErrorPath</key>
    <string>{self.logs_path}/{task["log"]}.error</string>

    <key>WorkingDirectory</key>
    <string>{self.config_path.parent}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>{str(Path.home())}</string>
        <key>PYTHONPATH</key>
        <string>{self.config_path.parent}</string>
    </dict>

    <key>RunAtLoad</key>
    <false/>

    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
"""

        return plist_content

    def install_task(self, task_name: str) -> bool:
        """安装定时任务（跨平台）"""
        task = self.TASKS.get(task_name)
        if not task:
            print(f"Unknown task: {task_name}")
            return False

        script_path = self.config_path.parent / "scripts" / task["script"]
        if not script_path.exists():
            print(f"[ERR] 脚本不存在，无法安装: {script_path}")
            return False

        # 生成包装脚本（处理跳过逻辑）
        wrapper_script = self.generate_wrapper_script(task_name)
        wrapper_path = self.config_path.parent / "scripts" / f".wrapper_{task_name}.py"
        wrapper_path.write_text(wrapper_script, encoding="utf-8")
        wrapper_path.chmod(0o755)

        if sys.platform == "darwin":
            return self._install_task_macos(task_name, task, wrapper_path)
        elif sys.platform == "linux":
            return self._install_task_linux(task_name, task, wrapper_path)
        elif sys.platform == "win32":
            return self._install_task_windows(task_name, task, wrapper_path)
        else:
            print(f"[WARN] 不支持的平台: {sys.platform}，请手动配置定时任务")
            print(f"  包装脚本已生成: {wrapper_path}")
            print(f"  建议 cron: {task['cron']} {sys.executable} {wrapper_path}")
            return False

    def _install_task_macos(self, task_name: str, task: dict, wrapper_path: Path) -> bool:
        """macOS: 使用 launchd"""
        plist_content = self.generate_plist(task_name)
        plist_path = Path(f"~/Library/LaunchAgents/com.memos.wiki.{task_name}.plist").expanduser()

        try:
            plist_path.write_text(plist_content, encoding="utf-8")
            result = subprocess.run(
                ["launchctl", "load", str(plist_path)],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                print(f"[OK] Installed (macOS launchd): {task_name}")
                return True
            else:
                print(f"[ERR] Failed to load {task_name}: {result.stderr}")
                return False
        except Exception as e:
            print(f"[ERR] Error installing {task_name}: {e}")
            return False

    def _install_task_linux(self, task_name: str, task: dict, wrapper_path: Path) -> bool:
        """Linux: 使用 systemd user timer（优先）或 cron 回退"""
        # 尝试 systemd user timer
        systemd_dir = Path.home() / ".config" / "systemd" / "user"
        if systemd_dir.parent.exists():
            try:
                systemd_dir.mkdir(parents=True, exist_ok=True)
                service_content = self._generate_systemd_service(task_name, wrapper_path)
                timer_content = self._generate_systemd_timer(task_name, task)

                service_path = systemd_dir / f"mnemos-{task_name}.service"
                timer_path = systemd_dir / f"mnemos-{task_name}.timer"
                service_path.write_text(service_content, encoding="utf-8")
                timer_path.write_text(timer_content, encoding="utf-8")

                subprocess.run(["systemctl", "--user", "daemon-reload"],
                               capture_output=True, timeout=10)
                subprocess.run(["systemctl", "--user", "enable", f"mnemos-{task_name}.timer"],
                               capture_output=True, timeout=10)
                subprocess.run(["systemctl", "--user", "start", f"mnemos-{task_name}.timer"],
                               capture_output=True, timeout=10)
                print(f"[OK] Installed (Linux systemd): {task_name}")
                return True
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass  # systemd 不可用，回退到 cron

        # 回退到 cron
        try:
            cron_line = self._generate_cron_line(task_name, task, wrapper_path)
            # 添加到用户 crontab
            result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            existing = result.stdout if result.returncode == 0 else ""
            # 去重：移除旧的相同任务行
            lines = [l for l in existing.split("\n") if f"mnemos-{task_name}" not in l]
            lines.append(cron_line)
            new_crontab = "\n".join(lines) + "\n"
            proc = subprocess.run(["crontab", "-"], input=new_crontab,
                                  capture_output=True, text=True, timeout=10)
            if proc.returncode == 0:
                print(f"[OK] Installed (Linux cron): {task_name}")
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        print(f"[WARN] Linux 定时任务安装失败，请手动配置:")
        print(f"  cron: {self._generate_cron_line(task_name, task, wrapper_path)}")
        return False

    def _install_task_windows(self, task_name: str, task: dict, wrapper_path: Path) -> bool:
        """Windows: 使用 schtasks"""
        try:
            minute, hour, day, month, weekday = task["cron"].split()
            # 构建 schtasks 时间参数
            schedule = "/SC DAILY" if weekday == "*" else "/SC WEEKLY"
            if weekday != "*":
                # weekday: 0=Sunday in cron, 1=Sunday in schtasks? No, schtasks uses MON,TUE...
                # Actually schtasks /D uses day names for weekly
                days_map = {"0": "SUN", "1": "MON", "2": "TUE", "3": "WED",
                           "4": "THU", "5": "FRI", "6": "SAT"}
                if weekday in days_map:
                    schedule += f" /D {days_map[weekday]}"

            task_name_win = f"Mnemos-{task_name}"
            cmd = [
                "schtasks", "/Create", "/F", "/TN", task_name_win,
                "/TR", f'"{sys.executable}" "{wrapper_path}"',
                schedule,
                "/ST", f"{hour.zfill(2)}:{minute.zfill(2)}",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)
            if result.returncode == 0 or "SUCCESS" in result.stdout:
                print(f"[OK] Installed (Windows Task Scheduler): {task_name}")
                return True
            else:
                print(f"[ERR] schtasks failed: {result.stderr}")
                return False
        except FileNotFoundError:
            print(f"[WARN] schtasks 不可用，请手动创建 Windows 计划任务")
            print(f"  命令: {sys.executable} {wrapper_path}")
            return False
        except Exception as e:
            print(f"[ERR] Windows 定时任务安装失败: {e}")
            return False

    def _generate_systemd_service(self, task_name: str, wrapper_path: Path) -> str:
        """生成 systemd service 文件"""
        return f"""[Unit]
Description=Mnemos Task: {task_name}

[Service]
Type=oneshot
ExecStart={sys.executable} {wrapper_path}
WorkingDirectory={self.config_path.parent}
Environment=HOME={Path.home()}
Environment=PYTHONPATH={self.config_path.parent}
"""

    def _generate_systemd_timer(self, task_name: str, task: dict) -> str:
        """生成 systemd timer 文件"""
        minute, hour, day, month, weekday = task["cron"].split()
        # 转换为 systemd OnCalendar 格式
        if weekday != "*":
            # Weekly: Mon, Tue, etc.
            days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
            cal = f"{days[int(weekday)]} *-*-* {hour}:{minute}:00"
        elif hour.startswith("*/"):
            interval = hour.replace("*/", "")
            cal = f"*-*-* *:00/{interval}:00"
        else:
            cal = f"*-*-* {hour}:{minute}:00"

        return f"""[Unit]
Description=Mnemos Timer: {task_name}

[Timer]
OnCalendar={cal}
Persistent=true

[Install]
WantedBy=timers.target
"""

    def _generate_cron_line(self, task_name: str, task: dict, wrapper_path: Path) -> str:
        """生成 cron 表达式"""
        return f"{task['cron']} {sys.executable} {wrapper_path} # mnemos-{task_name}"

    def uninstall_task(self, task_name: str) -> bool:
        """卸载定时任务（跨平台）"""
        wrapper_path = self.config_path.parent / "scripts" / f".wrapper_{task_name}.py"
        success = True

        if sys.platform == "darwin":
            plist_path = Path(f"~/Library/LaunchAgents/com.memos.wiki.{task_name}.plist").expanduser()
            try:
                if plist_path.exists():
                    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
                    plist_path.unlink()
            except Exception as e:
                print(f"[ERR] macOS 卸载失败: {e}")
                success = False

        elif sys.platform == "linux":
            # 尝试 systemd
            try:
                subprocess.run(["systemctl", "--user", "stop", f"mnemos-{task_name}.timer"],
                               capture_output=True, timeout=10)
                subprocess.run(["systemctl", "--user", "disable", f"mnemos-{task_name}.timer"],
                               capture_output=True, timeout=10)
                service_path = Path.home() / ".config" / "systemd" / "user" / f"mnemos-{task_name}.service"
                timer_path = Path.home() / ".config" / "systemd" / "user" / f"mnemos-{task_name}.timer"
                if service_path.exists():
                    service_path.unlink()
                if timer_path.exists():
                    timer_path.unlink()
                subprocess.run(["systemctl", "--user", "daemon-reload"],
                               capture_output=True, timeout=10)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            # 尝试 cron
            try:
                result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
                if result.returncode == 0:
                    lines = [l for l in result.stdout.split("\n") if f"mnemos-{task_name}" not in l]
                    new_crontab = "\n".join(lines) + "\n"
                    subprocess.run(["crontab", "-"], input=new_crontab,
                                   capture_output=True, text=True, timeout=10)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        elif sys.platform == "win32":
            try:
                task_name_win = f"Mnemos-{task_name}"
                subprocess.run(["schtasks", "/Delete", "/TN", task_name_win, "/F"],
                               capture_output=True, text=True, timeout=30,
                               creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)
            except FileNotFoundError:
                pass

        # 清理包装脚本
        if wrapper_path.exists():
            wrapper_path.unlink()

        if success:
            print(f"[OK] Uninstalled: {task_name}")
        return success

    def list_tasks(self) -> List[Dict]:
        """列出已安装的任务（跨平台）"""
        tasks = []

        for task_name in self.TASKS:
            task_info = {
                "name": task_name,
                "description": self.TASKS[task_name]["description"],
                "installed": False,
                "running": False,
            }

            if sys.platform == "darwin":
                plist_path = Path(f"~/Library/LaunchAgents/com.memos.wiki.{task_name}.plist").expanduser()
                task_info["installed"] = plist_path.exists()
                if plist_path.exists():
                    result = subprocess.run(
                        ["launchctl", "list", f"com.memos.wiki.{task_name}"],
                        capture_output=True,
                        text=True
                    )
                    task_info["running"] = result.returncode == 0

            elif sys.platform == "linux":
                # 检查 systemd timer
                timer_path = Path.home() / ".config" / "systemd" / "user" / f"mnemos-{task_name}.timer"
                if timer_path.exists():
                    task_info["installed"] = True
                    result = subprocess.run(
                        ["systemctl", "--user", "is-active", f"mnemos-{task_name}.timer"],
                        capture_output=True, text=True, timeout=5
                    )
                    task_info["running"] = result.returncode == 0
                else:
                    # 检查 cron
                    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
                    if result.returncode == 0 and f"mnemos-{task_name}" in result.stdout:
                        task_info["installed"] = True

            elif sys.platform == "win32":
                task_name_win = f"Mnemos-{task_name}"
                result = subprocess.run(
                    ["schtasks", "/Query", "/TN", task_name_win],
                    capture_output=True, text=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                )
                task_info["installed"] = result.returncode == 0 or "ERROR" not in result.stderr

            tasks.append(task_info)

        return tasks

    def run_task_now(self, task_name: str) -> Dict:
        """立即运行任务（用于测试）"""
        task = self.TASKS.get(task_name)
        if not task:
            return {"error": f"Unknown task: {task_name}"}

        script_path = self.config_path.parent / "scripts" / task["script"]

        if not script_path.exists():
            return {"error": f"Script not found: {script_path}"}

        try:
            result = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True,
                text=True,
                cwd=str(self.config_path.parent)
            )

            return {
                "task": task_name,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "success": result.returncode == 0
            }

        except Exception as e:
            return {"error": str(e)}

    def install_all(self) -> Dict:
        """安装所有任务"""
        results = {}
        for task_name in self.TASKS:
            results[task_name] = self.install_task(task_name)
        return results

    def uninstall_all(self) -> Dict:
        """卸载所有任务"""
        results = {}
        for task_name in self.TASKS:
            results[task_name] = self.uninstall_task(task_name)
        return results


def main():
    """CLI入口"""
    if len(sys.argv) < 2:
        print("Usage: python scheduler.py <command> [args]")
        print("")
        print("Commands:")
        print("  install <task>    Install a specific task")
        print("  install-all       Install all tasks")
        print("  uninstall <task>  Uninstall a task")
        print("  uninstall-all     Uninstall all tasks")
        print("  list              List all tasks")
        print("  run <task>        Run a task immediately")
        print("")
        print("Tasks:")
        scheduler = Scheduler()
        for name, task in scheduler.TASKS.items():
            print(f"  {name:15} - {task['description']}")
        return

    command = sys.argv[1]
    scheduler = Scheduler()

    if command == "install" and len(sys.argv) >= 3:
        task_name = sys.argv[2]
        scheduler.install_task(task_name)

    elif command == "install-all":
        results = scheduler.install_all()
        success = sum(1 for v in results.values() if v)
        print(f"\nInstalled {success}/{len(results)} tasks")

    elif command == "uninstall" and len(sys.argv) >= 3:
        task_name = sys.argv[2]
        scheduler.uninstall_task(task_name)

    elif command == "uninstall-all":
        results = scheduler.uninstall_all()
        success = sum(1 for v in results.values() if v)
        print(f"\nUninstalled {success}/{len(results)} tasks")

    elif command == "list":
        tasks = scheduler.list_tasks()
        print(f"{'Task':<20} {'Status':<10} {'Running':<10} Description")
        print("-" * 70)
        for task in tasks:
            status = "[OK]" if task["installed"] else "[NO]"
            running = "[ON]" if task["running"] else "[OFF]"
            print(f"{task['name']:<20} {status:<10} {running:<10} {task['description']}")

    elif command == "run" and len(sys.argv) >= 3:
        task_name = sys.argv[2]
        result = scheduler.run_task_now(task_name)
        print(json.dumps(result, indent=2))

    else:
        print(f"Unknown command: {command}")


if __name__ == "__main__":
    main()
