#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mnemos Wiki 自动提交守护进程

监控配置中的 Mnemos Wiki Vault 文件变更，延迟窗口结束后自动执行 git commit。
设计目标：让 Wiki 数据仓库随编辑自动备份，同时避免连续编辑产生大量 commit。

集成方式：
- 独立运行：python3 scripts/auto_commit_wiki.py
- 由 mnemos daemon 启动：作为后台服务线程运行
"""

from __future__ import annotations

import logging
import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.cli.periodic import add_periodic_loop_args, resolve_max_cycles, run_periodic_loop

# watchdog 已由项目依赖提供
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

# 延迟提交窗口：检测到变更后，若 N 秒内无新变更则提交
DEFAULT_DEBOUNCE_SECONDS = 3600

# 忽略的路径模式（相对路径匹配）
IGNORED_PATTERNS = {
    ".git",
    ".obsidian",
    ".kg",
    "__pycache__",
    ".DS_Store",
    "07-Shadow",  # 影子页面自动生成，变化频繁，建议单独处理
}

# 只监控这些后缀的文件；空集合表示监控所有非忽略文件
WATCHED_SUFFIXES = {".md", ".db"}


def _default_watch_dir() -> Path:
    from core.config import get_config

    return Path(get_config().wiki_dir).expanduser()


def _should_ignore(path: Path, watch_dir: Path) -> bool:
    """判断某个路径是否应被忽略。"""
    try:
        rel_parts = path.relative_to(watch_dir).parts
    except ValueError:
        return True

    for part in rel_parts:
        if part.startswith(".") and part != ".":
            # 忽略所有隐藏目录/文件，但保留顶层 .kg/*.db
            if part == ".kg" and path.suffix == ".db":
                return False
            return True
        if part in IGNORED_PATTERNS:
            # .kg 目录内的 .db 文件仍然监控
            if part == ".kg" and path.suffix == ".db":
                return False
            return True

    if path.is_dir():
        return True

    if WATCHED_SUFFIXES and path.suffix.lower() not in WATCHED_SUFFIXES:
        return True

    return False


class WikiAutoCommitHandler(FileSystemEventHandler):
    """文件变更处理器：收集变更事件，触发延迟提交。"""

    def __init__(self, watch_dir: Path, debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS):
        self.watch_dir = watch_dir
        self.debounce_seconds = debounce_seconds
        self._last_event_time = 0.0
        self._pending = False
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        # 用于检测系统休眠/关机后唤醒：单调时钟 + 墙上时钟对照
        self._last_loop_mono = time.monotonic()
        self._last_loop_wall = time.time()

    def on_any_event(self, event: FileSystemEvent) -> None:  # noqa: Vulture - watchdog hook.
        if event.is_directory:
            return
        src_path = Path(event.src_path)  # type: ignore[arg-type]
        if _should_ignore(src_path, self.watch_dir):
            return

        with self._lock:
            self._last_event_time = time.time()
            self._pending = True
        logger.debug("[watch] %s: %s", event.event_type, src_path.relative_to(self.watch_dir))

    def _detect_and_handle_sleep(self) -> bool:
        """检测系统是否刚结束休眠/关机。若是，重置计时器以顺延提交。"""
        now_mono = time.monotonic()
        now_wall = time.time()
        mono_delta = now_mono - self._last_loop_mono
        wall_delta = now_wall - self._last_loop_wall
        # 如果墙上时间比单调时钟多走了 60 秒以上，认为中间经历了休眠
        slept = wall_delta - mono_delta > 60
        if slept:
            logger.info(
                "[auto-commit] 检测到系统休眠/唤醒（wall_delta=%.0fs, mono_delta=%.0fs），顺延提交",
                wall_delta,
                mono_delta,
            )
            with self._lock:
                if self._pending:
                    self._last_event_time = now_wall
        self._last_loop_mono = now_mono
        self._last_loop_wall = now_wall
        return slept

    def run_commit_loop(self) -> None:
        """后台循环：等待 debounce 窗口结束后执行提交。"""
        logger.info("[auto-commit] 启动提交循环，debounce=%ds", self.debounce_seconds)
        while not self._stop_event.is_set():
            self._detect_and_handle_sleep()

            should_commit = False
            with self._lock:
                if self._pending and time.time() - self._last_event_time >= self.debounce_seconds:
                    should_commit = True
                    self._pending = False

            if should_commit:
                self._try_commit()

            # 每 10 秒检查一次
            self._stop_event.wait(10)

    def stop(self) -> None:
        self._stop_event.set()

    # Git 操作超时（秒），防止大仓库或锁竞争导致线程永久挂起
    GIT_TIMEOUT = 120

    def _try_commit(self) -> None:
        """尝试执行 git add -A && git commit。"""
        try:
            status_result = subprocess.run(
                ["git", "status", "--short"],
                cwd=self.watch_dir,
                capture_output=True,
                text=True,
                check=True,
                timeout=self.GIT_TIMEOUT,
            )
            changed = status_result.stdout.strip()
            if not changed:
                logger.debug("[auto-commit] 无变更，跳过")
                return

            # 添加所有变更
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self.watch_dir,
                check=True,
                capture_output=True,
                timeout=self.GIT_TIMEOUT,
            )

            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            # 提交信息包含前 10 个变更文件摘要
            summary_lines = changed.splitlines()[:10]
            summary = "\n".join(summary_lines)
            message = f"auto: wiki sync at {timestamp}\n\nChanged files:\n{summary}"

            commit_result = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=self.watch_dir,
                check=True,
                capture_output=True,
                text=True,
                timeout=self.GIT_TIMEOUT,
            )
            logger.info(
                "[auto-commit] 已提交: %s",
                commit_result.stdout.strip().splitlines()[0] if commit_result.stdout else timestamp,
            )

        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else ""
            stdout = exc.stdout.strip() if exc.stdout else ""
            logger.warning("[auto-commit] git 操作失败: %s %s", stdout, stderr)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("[auto-commit] 提交异常: %s", exc, exc_info=True)


def start_auto_commit(
    watch_dir: Optional[Path] = None, debounce_seconds: int = DEFAULT_DEBOUNCE_SECONDS
) -> Optional[WikiAutoCommitHandler]:
    """启动 Wiki 自动提交监控，返回 handler（可用于停止）。"""
    watch_dir = Path(watch_dir).expanduser() if watch_dir is not None else _default_watch_dir()
    if not (watch_dir / ".git").exists():
        logger.warning("[auto-commit] %s 不是 git 仓库，跳过启动", watch_dir)
        return None

    handler = WikiAutoCommitHandler(watch_dir, debounce_seconds)
    observer = Observer()
    observer.schedule(handler, str(watch_dir), recursive=True)
    observer.start()

    commit_thread = threading.Thread(
        target=handler.run_commit_loop,
        name="wiki-auto-commit",
        daemon=True,
    )
    commit_thread.start()

    logger.info("[auto-commit] 开始监控 %s", watch_dir)
    return handler


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 入口：独立运行模式。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="Mnemos Wiki 自动提交守护进程")
    parser.add_argument("watch_dir", nargs="?", help="要监控的 Wiki git 仓库目录")
    add_periodic_loop_args(parser, default_interval=1)
    args = parser.parse_args(argv)

    watch_dir = Path(args.watch_dir) if args.watch_dir else None

    handler = start_auto_commit(watch_dir)
    if handler is None:
        return 1

    try:
        run_periodic_loop(
            lambda: None,
            interval=args.interval,
            max_cycles=resolve_max_cycles(once=args.once, max_cycles=args.max_cycles),
            run_seconds=args.run_seconds,
        )
    except KeyboardInterrupt:
        logger.info("[auto-commit] 收到中断信号，正在停止...")
    finally:
        handler.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
