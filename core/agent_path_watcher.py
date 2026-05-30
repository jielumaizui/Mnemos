# -*- coding: utf-8 -*-
"""
AgentPathWatcher — Agent 配置路径动态监听器

职责：
- 使用 watchdog 监控常见父目录（~/.config/, ~/Library/Application Support/, ~）
- 当检测到新的 Agent 配置目录创建/重命名时，自动刷新 PathDiscover 缓存
- 异步线程运行，不影响主流程性能

用法：
    from core.agent_path_watcher import AgentPathWatcher
    watcher = AgentPathWatcher()
    watcher.start()   # 后台线程启动
    ...
    watcher.stop()    # 优雅停止
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger(__name__)

# 延迟导入 watchdog，避免硬依赖
_watchdog_available = False
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileCreatedEvent, DirCreatedEvent, FileMovedEvent, DirMovedEvent
    _watchdog_available = True
except ImportError:
    Observer = None
    FileSystemEventHandler = object


class _AgentPathHandler(FileSystemEventHandler):
    """watchdog 事件处理器：过滤 Agent 相关路径变化"""

    # 已知 Agent 目录名特征
    AGENT_DIR_NAMES = {
        ".claude", "claude",
        ".kimi", "kimi",
        ".hermes", "hermes",
        ".codex", "codex",
        ".openclaw", "openclaw",
        ".cursor", "cursor",
        ".windsurf", "windsurf",
        ".gemini", "gemini",
    }

    def __init__(self, watcher: AgentPathWatcher):
        self.watcher = watcher

    def on_created(self, event):
        self._check(event.src_path)

    def on_moved(self, event):
        self._check(event.dest_path)

    def _check(self, path: str):
        name = Path(path).name.lower()
        if any(a in name for a in self.AGENT_DIR_NAMES):
            logger.info(f"[AgentPathWatcher] 检测到 Agent 路径变化: {path}")
            self.watcher._on_path_changed()


class AgentPathWatcher:
    """Agent 配置路径动态监听器"""

    def __init__(self):
        self._observer: Optional[Observer] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._debounce_timer: Optional[threading.Timer] = None
        self._debounce_lock = threading.Lock()

    def start(self):
        """启动监听（后台线程）"""
        if not _watchdog_available:
            logger.warning("[AgentPathWatcher] watchdog 未安装，跳过动态监听。"
                           "如需启用请执行: pip install watchdog")
            return
        if self._observer is not None:
            return

        self._thread = threading.Thread(target=self._run, name="AgentPathWatcher", daemon=True)
        self._thread.start()
        logger.info("[AgentPathWatcher] 已启动")

    def stop(self):
        """停止监听"""
        self._stop_event.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("[AgentPathWatcher] 已停止")

    def _run(self):
        """监听线程主循环"""
        from watchdog.observers import Observer

        self._observer = Observer()
        handler = _AgentPathHandler(self)

        # 监控的父目录
        watch_paths: Set[Path] = set()
        for p in [
            Path.home(),
            Path.home() / ".config",
            Path.home() / "Library" / "Application Support",
        ]:
            if p.exists():
                watch_paths.add(p)

        for p in watch_paths:
            try:
                self._observer.schedule(handler, str(p), recursive=False)
                logger.debug(f"[AgentPathWatcher] 监控: {p}")
            except Exception as e:
                logger.warning(f"[AgentPathWatcher] 无法监控 {p}: {e}")

        self._observer.start()
        self._stop_event.wait()
        self._observer.stop()
        self._observer.join()

    def _on_path_changed(self):
        """路径变化回调（带防抖）"""
        with self._debounce_lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
            # 3 秒后刷新缓存，避免批量文件操作触发多次
            self._debounce_timer = threading.Timer(3.0, self._do_refresh)
            self._debounce_timer.start()

    def _do_refresh(self):
        """实际刷新 PathDiscover 缓存"""
        try:
            from core.sync_framework.registry import PathDiscover
            PathDiscover.invalidate_cache()
            logger.info("[AgentPathWatcher] PathDiscover 缓存已刷新")
        except Exception as e:
            logger.warning(f"[AgentPathWatcher] 刷新缓存失败: {e}")
