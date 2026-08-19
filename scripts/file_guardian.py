#!/usr/bin/env python3
"""文件守护模块：监控关键文件，0字节时自动采集现场、恢复、并记录日志。

集成方式：
    # 在 daemon 启动时调用
    from scripts.file_guardian import auto_guard_files
    auto_guard_files(repo_root=Path(__file__).resolve().parent)

    # 或启动后台线程
    import threading
    t = threading.Thread(target=file_guard_loop, args=(repo_root, 30), daemon=True)
    t.start()
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("mnemos.file_guardian")

# 关键文件清单：(相对路径, 最小允许字节数)
CRITICAL_FILES: List[tuple[str, int]] = [
    ("mnemos_daemon.py", 1000),
    ("mnemos_cli.py", 500),
    ("core/config.py", 500),
]


def _get_forensic_log() -> Path:
    """返回现场日志路径。惰性计算以避免模块导入时的循环依赖。"""
    from core.config import get_config

    return get_config().database_dir / "logs" / "file_corruption_incidents.log"


def _ensure_log_dir() -> None:
    _get_forensic_log().parent.mkdir(parents=True, exist_ok=True)


def _repo_root() -> Path:
    """自动定位仓库根目录。"""
    script_dir = Path(__file__).resolve().parent
    if (script_dir.parent / ".git").is_dir():
        return script_dir.parent
    cwd = Path.cwd()
    for p in [cwd] + list(cwd.parents):
        if (p / ".git").is_dir():
            return p
    raise RuntimeError("无法定位 git 仓库根目录")


def _run_cmd(cmd: List[str], cwd: Path | None = None, timeout: int = 5) -> str:
    """安全执行命令，返回输出或空字符串。"""
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    # DEBT(S8): 容错降级，返回默认值避免局部失败扩散
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
        subprocess.SubprocessError
    ):
        return ""


def _collect_forensic(path: Path, repo_root: Path) -> Dict[str, Any]:
    """采集文件异常时的全部现场信息，无需用户手动执行任何命令。"""
    _ensure_log_dir()
    now = datetime.now().isoformat()
    forensic: Dict[str, Any] = {
        "timestamp": now,
        "file": str(path),
        "file_stat": {},
        "git_status": "",
        "git_log": "",
        "claude_update": {},
        "processes": "",
        "lsof": "",
        "system_logs": "",
    }

    # 1. 文件 stat
    try:
        st = path.stat()
        forensic["file_stat"] = {
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(),
            "ctime": datetime.fromtimestamp(st.st_ctime).isoformat(),
            "mode": oct(st.st_mode),
            "inode": st.st_ino,
            "device": st.st_dev,
        }
    except OSError as e:
        forensic["file_stat"] = {"error": str(e)}

    # 2. git 状态
    forensic["git_status"] = _run_cmd(["git", "status", "--short"], cwd=repo_root)
    forensic["git_log"] = _run_cmd(["git", "log", "--oneline", "-5"], cwd=repo_root)

    # 3. Claude Code 更新状态
    claude_update_file = Path.home() / ".claude" / ".last-update-result.json"
    if claude_update_file.exists():
        try:
            with open(claude_update_file, "r", encoding="utf-8") as f:
                forensic["claude_update"] = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    # 4. 可能相关的进程
    forensic["processes"] = _run_cmd(["pgrep", "-afil", "claude|opencode|codex"])

    # 5. lsof（谁打开了这个文件）
    forensic["lsof"] = _run_cmd(["lsof", str(path)])

    # 6. 系统日志（最近 5 分钟的文件系统相关日志）
    forensic["system_logs"] = _run_cmd(
        [
            "log",
            "show",
            "--predicate",
            'subsystem == "com.apple.kernel"',
            "--last",
            "5m",
        ]
    )

    # 7. xattrs
    forensic["xattrs"] = _run_cmd(["xattr", "-l", str(path)])

    # 8. 当前工作目录
    forensic["cwd"] = str(Path.cwd())
    forensic["python_exe"] = sys.executable

    return forensic


def _write_forensic(forensic: Dict[str, Any]) -> None:
    """将现场信息写入日志文件。"""
    _ensure_log_dir()
    try:
        with open(_get_forensic_log(), "a", encoding="utf-8") as f:
            f.write("=" * 60 + "\n")
            f.write(f"FILE CORRUPTION INCIDENT: {forensic['timestamp']}\n")
            f.write("=" * 60 + "\n")
            json.dump(forensic, f, indent=2, ensure_ascii=False, default=str)
            f.write("\n\n")
    except OSError as e:
        logger.error("[GUARDIAN] 无法写入现场日志: %s", e, exc_info=True)


def _git_restore(path: Path, repo_root: Path) -> bool:
    """从 git HEAD 恢复文件。"""
    try:
        rel = path.relative_to(repo_root)
        _ = subprocess.run(
            ["git", "checkout", "HEAD", "--", str(rel)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        logger.error("[GUARDIAN] 已自动恢复 %s 从 git HEAD", rel)
        return True
    except subprocess.CalledProcessError as exc:
        logger.error("[GUARDIAN] git restore 失败: %s", exc.stderr, exc_info=True)
        return False
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.error("[GUARDIAN] git restore 异常: %s", exc, exc_info=True)
        return False


def guard_critical_files(
    repo_root: Path | None = None,
    auto_restore: bool = True,
) -> Dict[str, dict]:
    """检查所有关键文件，自动采集现场并恢复。

    Returns:
        {file_path: {"ok": bool, "size": int, "restored": bool, "reason": str}}
    """
    if repo_root is None:
        repo_root = _repo_root()

    results: Dict[str, dict] = {}
    for rel_path, min_bytes in CRITICAL_FILES:
        full = repo_root / rel_path
        status = {"ok": True, "size": 0, "restored": False, "reason": ""}

        if not full.exists():
            status["ok"] = False
            status["reason"] = "文件不存在"
            if auto_restore:
                status["restored"] = _git_restore(full, repo_root)
                if status["restored"]:
                    status["ok"] = True
                    status["reason"] = "已从 git 恢复"
            results[rel_path] = status
            continue

        size = full.stat().st_size
        status["size"] = size

        if size < min_bytes:
            status["ok"] = False
            status["reason"] = f"文件过小 ({size} < {min_bytes} bytes)"

            # 采集现场（自动，无需用户干预）
            forensic = _collect_forensic(full, repo_root)
            forensic["detection_reason"] = status["reason"]
            _write_forensic(forensic)
            logger.error(
                "[GUARDIAN] %s 异常！现场已采集到 %s",
                rel_path,
                _get_forensic_log(),
            )

            if auto_restore:
                status["restored"] = _git_restore(full, repo_root)
                if status["restored"]:
                    status["ok"] = True
                    status["reason"] = "已从 git 恢复"
                    # 记录恢复后的状态
                    _write_forensic(
                        {
                            "timestamp": datetime.now().isoformat(),
                            "event": "auto_restore_completed",
                            "file": str(full),
                            "success": True,
                        }
                    )

        results[rel_path] = status

        if not status["ok"]:
            logger.error(
                "[GUARDIAN] %s: %s (size=%d)",
                rel_path,
                status["reason"],
                status["size"],
            )
        elif status["restored"]:
            logger.error(
                "[GUARDIAN] %s: 已自动恢复 (原 %s)",
                rel_path,
                status["reason"],
            )

    return results


def file_guard_loop(repo_root: Path, interval: int = 30) -> None:
    """后台守护循环，轮询检查关键文件。"""
    logger.info("[GUARDIAN] 守护线程启动，轮询间隔 %ds", interval)
    while not _guardian_stop.is_set():
        try:
            results = guard_critical_files(repo_root)
            bad = [r for r in results.values() if not r["ok"]]
            if bad:
                logger.error("[GUARDIAN] 检测到 %d 个文件异常", len(bad))
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            logger.error("[GUARDIAN] 轮询异常: %s", exc, exc_info=True)
        time.sleep(interval)


# 内部停止事件（不依赖外部 daemon 的 stop_event）
_guardian_stop = threading.Event()


def auto_guard_files(repo_root: Path | None = None, interval: int = 30) -> None:  # noqa
    """一键启动自动化文件守护。

    1. 立即检查一次关键文件
    2. 启动后台线程持续轮询
    """
    import threading

    if repo_root is None:
        repo_root = _repo_root()

    # 立即检查一次
    results = guard_critical_files(repo_root)
    bad = [r for r in results.values() if not r["ok"]]
    if bad:
        logger.error("[GUARDIAN] 启动时发现 %d 个文件异常，已自动处理", len(bad))

    # 启动后台线程
    t = threading.Thread(
        target=file_guard_loop,
        args=(repo_root, interval),
        daemon=True,
        name="FileGuardian",
    )
    t.start()
    logger.info("[GUARDIAN] 后台守护线程已启动")


if __name__ == "__main__":
    # 命令行用法保留，但默认行为改为 --check-once
    import argparse

    parser = argparse.ArgumentParser(description="Mnemos 文件守护者")
    parser.add_argument(
        "--check-once", action="store_true", default=True, help="检查一次后退出（默认）"
    )
    parser.add_argument("--loop", action="store_true", help="前台循环守护模式")
    parser.add_argument("--interval", type=int, default=30, help="轮询间隔（秒，默认 30）")
    parser.add_argument("--repo-root", type=Path, default=None, help="仓库根目录（默认自动检测）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    repo_root = args.repo_root or _repo_root()

    if args.loop:
        file_guard_loop(repo_root, args.interval)
    else:
        results = guard_critical_files(repo_root)
        print(json.dumps(results, indent=2, ensure_ascii=False))
        bad = [r for r in results.values() if not r["ok"]]
        sys.exit(1 if bad else 0)
