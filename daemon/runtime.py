# -*- coding: utf-8 -*-
"""Startup/runtime bootstrap helpers for the Mnemos daemon."""

from __future__ import annotations

import logging
import signal
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.runtime_paths import RuntimePaths

logger = logging.getLogger("mnemos.daemon")

RUNTIME_OPERATION_ERRORS = (
    ImportError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    sqlite3.Error,
)


def ensure_project_on_path(project_root: Path) -> None:
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))


def ensure_vault_directories(*, log: logging.Logger | None = None) -> None:
    """Ensure configured raw/wiki vault skeleton directories exist."""
    log = log or logger
    try:
        from core.config import get_config
        from core.setup.vault_layout import init_vaults

        cfg = get_config()
        init_vaults(cfg.wiki_dir, cfg.obsidian_vault_path)
        log.info("[DAEMON] Vault 目录结构已就绪")
    except RUNTIME_OPERATION_ERRORS as exc:
        log.warning("[DAEMON] Vault 目录初始化失败: %s", exc, exc_info=True)


def bootstrap_runtime_schema(*, log: logging.Logger | None = None) -> None:
    """Idempotently initialize database tables required by daemon runtime."""
    log = log or logger
    try:
        from core.db_init import bootstrap_schema

        result = bootstrap_schema()
        if result.get("ok"):
            log.info("[DAEMON] 数据库 schema bootstrap 已就绪")
        else:
            failed = [s for s in result.get("steps", []) if not s.get("ok")]
            log.warning("[DAEMON] 数据库 schema bootstrap 部分失败: %s", failed)
    except RUNTIME_OPERATION_ERRORS as exc:
        log.warning("[DAEMON] 数据库 schema bootstrap 失败: %s", exc, exc_info=True)


def start_file_guardian(project_root: Path, *, log: logging.Logger | None = None) -> None:
    """Start the background critical-file guardian thread."""
    log = log or logger
    try:
        from scripts import file_guardian

        results = file_guardian.guard_critical_files(project_root)
        bad = [r for r in results.values() if not r["ok"]]
        if bad:
            log.error("[DAEMON] 启动时发现 %d 个关键文件异常，已自动处理", len(bad))

        thread = threading.Thread(
            target=file_guardian.file_guard_loop,
            args=(project_root, 30),
            daemon=True,
            name="FileGuardian",
        )
        thread.start()
        log.info("[DAEMON] 文件守护线程已启动")
    except RUNTIME_OPERATION_ERRORS as exc:
        log.warning("[DAEMON] 文件守护线程启动失败: %s", exc, exc_info=True)


def resolve_data_dirs() -> tuple[Path, Path]:
    """Resolve daemon data/database directories, falling back to ~/.mnemos."""
    try:
        paths = RuntimePaths.from_config()
        return paths.data_dir, paths.database_dir
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        paths = RuntimePaths.fallback()
        return paths.data_dir, paths.database_dir


class RuntimeContext:
    """Daemon 统一运行时上下文：持有长生命周期对象并负责优雅关闭。

    设计目标：
    - 把散落在 mnemos_daemon.py 全局变量和各个模块单例中的生命周期集中管理。
    - 按注册顺序启动/获取，按反向顺序关闭，避免依赖倒置导致的事件丢失或锁异常。
    - 收到 SIGTERM/SIGINT 时触发 shutdown，保证 SQLite 连接池、EventBus、WorkerPool 等被正确释放。
    - shutdown 后清理模块级单例状态，支持 daemon 重启或测试隔离。

    使用方式：
        ctx = RuntimeContext()
        ctx.install_signal_handlers()
        ctx.register("event_bus", get_event_bus())
        ...
        ctx.shutdown()
    """

    def __init__(self) -> None:
        self._resources: List[Tuple[str, Any, Optional[Callable[[Any], None]], bool]] = []
        self._registry: Dict[str, Any] = {}
        self._stop_event = threading.Event()
        self._signal_handlers_installed = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False

    @property
    def stop_event(self) -> threading.Event:
        return self._stop_event

    def register(
        self,
        name: str,
        resource: Any,
        *,
        closer: Optional[Callable[[Any], None]] = None,
        close_on_shutdown: bool = True,
    ) -> Any:
        """注册一个长生命周期资源。

        Args:
            name: 资源名称，用于后续 get(name) 和日志。
            resource: 任意对象；若 closer 为 None，则依次尝试 stop/close/shutdown 方法。
            closer: 自定义关闭 callable，接收 resource 参数。
            close_on_shutdown: 是否在 shutdown 时调用 closer。
        """
        self._resources.append((name, resource, closer, close_on_shutdown))
        self._registry[name] = resource
        return resource

    def get(self, name: str) -> Any:
        """获取已注册资源。"""
        return self._registry.get(name)

    def _default_closer(self, resource: Any) -> None:
        """依次尝试调用资源的 stop/close/shutdown 方法。"""
        for method_name in ("stop", "close", "shutdown"):
            method = getattr(resource, method_name, None)
            if callable(method):
                try:
                    method()
                except RUNTIME_OPERATION_ERRORS as exc:
                    logger.warning(
                        "[RuntimeContext] 调用 %s.%s() 失败: %s",
                        type(resource).__name__,
                        method_name,
                        exc,
                        exc_info=True,
                    )
                return

    def install_signal_handlers(self) -> None:
        """注册 SIGTERM/SIGINT 处理器，触发 stop_event 并在新线程中异步 shutdown。"""
        if self._signal_handlers_installed:
            return
        self._signal_handlers_installed = True

        def _signal_handler(signum: int, _frame: Any) -> None:
            logger.info("[RuntimeContext] 收到信号 %s，准备退出...", signum)
            self._stop_event.set()
            threading.Thread(target=self.shutdown, name="RuntimeContext-Shutdown", daemon=True).start()

        try:
            signal.signal(signal.SIGTERM, _signal_handler)
            signal.signal(signal.SIGINT, _signal_handler)
            logger.info("[RuntimeContext] SIGTERM/SIGINT 信号处理器已注册")
        except RUNTIME_OPERATION_ERRORS as exc:
            logger.warning("[RuntimeContext] 信号处理器注册失败: %s", exc, exc_info=True)

    def shutdown(self, timeout: float = 30.0) -> None:
        """按反向顺序关闭所有已注册资源，并清理模块级单例。"""
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True

        logger.info("[RuntimeContext] 开始优雅关闭...")
        self._stop_event.set()

        # 按注册反向顺序关闭资源
        for name, resource, closer, close_on_shutdown in reversed(self._resources):
            if not close_on_shutdown:
                continue
            if resource is None:
                continue
            try:
                if closer is not None:
                    closer(resource)
                else:
                    self._default_closer(resource)
                logger.info("[RuntimeContext] 资源 %s 已关闭", name)
            except RUNTIME_OPERATION_ERRORS as exc:
                logger.warning("[RuntimeContext] 关闭资源 %s 失败: %s", name, exc, exc_info=True)

        # 清理模块级单例状态
        self._reset_singletons()
        logger.info("[RuntimeContext] 优雅关闭完成")

    def _reset_singletons(self) -> None:
        """重置各模块级/类级单例，支持 daemon 重启或测试隔离。"""
        try:
            from core.config import reset_config

            reset_config()
            logger.debug("[RuntimeContext] Config 单例已重置")
        except RUNTIME_OPERATION_ERRORS as exc:
            logger.warning("[RuntimeContext] Config 单例重置失败: %s", exc, exc_info=True)

        try:
            from core.mnemos_bus import reset_event_bus

            reset_event_bus()
            logger.debug("[RuntimeContext] EventBus 单例已重置")
        except RUNTIME_OPERATION_ERRORS as exc:
            logger.warning("[RuntimeContext] EventBus 单例重置失败: %s", exc, exc_info=True)

        try:
            from core.sync_framework.capture_service import CaptureService

            CaptureService.reset_instance()
            logger.debug("[RuntimeContext] CaptureService 单例已重置")
        except RUNTIME_OPERATION_ERRORS as exc:
            logger.warning("[RuntimeContext] CaptureService 单例重置失败: %s", exc, exc_info=True)

        try:
            from core.sync_framework.registry import SourceRegistry

            SourceRegistry.reset()
            logger.debug("[RuntimeContext] SourceRegistry 实例缓存已重置")
        except RUNTIME_OPERATION_ERRORS as exc:
            logger.warning("[RuntimeContext] SourceRegistry 重置失败: %s", exc, exc_info=True)

    def __enter__(self) -> "RuntimeContext":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.shutdown()
