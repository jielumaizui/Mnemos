#!/usr/bin/env python3
"""
Mnemos Daemon — 后台守护进程

CLI:
    python mnemos_daemon.py start      # 后台启动
    python mnemos_daemon.py stop       # 停止
    python mnemos_daemon.py status     # 查看状态
    python mnemos_daemon.py run        # 前台运行（用于 cron / 调试）
    python mnemos_daemon.py install-windows
    python mnemos_daemon.py uninstall-windows
"""

from __future__ import annotations

import argparse  # noqa: F401
import concurrent.futures
import logging
import os
import platform  # noqa: F401
import shutil
import sqlite3
import subprocess  # noqa: F401
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

# ── 项目路径注入 ──
_PROJECT_ROOT = Path(__file__).resolve().parent


def _resolve_executable(name: str) -> Optional[str]:
    """将命令名解析为绝对路径；未找到返回 None。"""
    return shutil.which(name)


def _windows_executable(name: str) -> str:
    """返回 Windows 系统命令的绝对路径（优先 shutil.which，回退 System32）。"""
    resolved = _resolve_executable(name)
    if resolved:
        return resolved
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.join(system_root, "System32", name)


from daemon import process_control as _process_control
from daemon import adaptive_service as _adaptive_service  # noqa: F401
from daemon import agent_source_runtime as _agent_source_runtime
from daemon import command_control as _command_control
from daemon import event_handlers as _event_handlers  # noqa: F401
from daemon import entrypoint_support as _entrypoint_support
from daemon import file_ingest as _file_ingest
from daemon import heartbeat as _heartbeat
from daemon import instance_control as _instance_control
from daemon import intervals as _intervals
from daemon import kia_services as _kia_services  # noqa: F401
from daemon import link_probe as _link_probe  # noqa: F401
from daemon import maintenance as _maintenance
from daemon import observation_service as _observation_service  # noqa: F401
from daemon import prediction_service as _prediction_service  # noqa: F401
from daemon import training_governance_service as _training_governance_service  # noqa: F401
from daemon import consolidation_service as _consolidation_service  # noqa: F401
from daemon import raw_sync as _raw_sync
from daemon import raw_projection_service as _raw_projection_service
from daemon import reflection_services as _reflection_services  # noqa: F401
from daemon import resource_budget as _resource_budget
from daemon import runtime as _runtime
from daemon import scoring_signals as _scoring_signals  # noqa: F401
from daemon import service_registry as _service_registry
from daemon import service_state as _service_state
from daemon import triggers as _triggers
from core.sync_framework.agent_path_watcher import AgentPathWatcher
from core.sync_framework.storage_backend import StorageError

_runtime.ensure_project_on_path(_PROJECT_ROOT)

logger = logging.getLogger("mnemos.daemon")
_ENTRYPOINT_HOST = sys.modules[__name__]

DAEMON_OPERATION_ERRORS = (
    ImportError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    sqlite3.Error,
    StorageError,
)


# ── Vault 目录自动初始化 ──
def _ensure_vault_directories():
    """首次启动时确保两个 Vault 的骨架目录存在。"""
    _runtime.ensure_vault_directories(log=logger)


def _bootstrap_runtime_schema():
    """Daemon 启动时幂等初始化当前运行所需的关键数据库表。"""
    _runtime.bootstrap_runtime_schema(log=logger)


def _bootstrap_runtime_flow_ledger(cfg: Any) -> None:
    """Provision flow declarations outside the read-only health path."""
    from daemon.runtime_flow_receipts import bootstrap_runtime_flow_ledger

    bootstrap_runtime_flow_ledger(cfg, log=logger)


# ── 文件守护（自动防 0 字节腐败）──
def _start_file_guardian():
    """启动文件守护线程，无需用户干预。

    1. 立即检查一次关键文件完整性
    2. 启动后台线程持续轮询
    """
    _runtime.start_file_guardian(_PROJECT_ROOT, log=logger)


# ── 配置 ──
# 优先从 Config 读取数据目录，支持 MNEMOS_DIR / MNEMOS_DATABASE_DIR 环境变量。
# 导入阶段只保留占位路径；run/main 阶段再读取真实配置。
_RUNTIME_PATHS: Optional[Any] = None
_DATA_DIR = Path(".mnemos")
_DATABASE_DIR = Path(".mnemos")
PID_FILE = _DATABASE_DIR / "daemon.pid"
STATUS_FILE = _DATABASE_DIR / "daemon.status"  # 子进程写入启动状态
DAEMON_LOG = _DATABASE_DIR / "logs" / "daemon.log"
DAEMON_HEARTBEAT_FILE = _DATABASE_DIR / "daemon_heartbeat.json"
STARTUP_STATUS_TIMEOUT_SECONDS = 45.0

INTERVALS: Dict[str, int] = _intervals.build_default_intervals(capture_tick=300)
_daemon_instance_identity: Dict[str, Any] | None = None

# A constrained profile is intentionally explicit instead of relying on a long
# list of environment-disabled services.  That keeps the OS-bound instance
# identity, heartbeat, and scheduler aligned with what is actually running.
_PRODUCTION_RUN_PROFILE = "production"
_CONTROLLED_RAW_SYNC_ONLY_RUN_PROFILE = "controlled_raw_sync_only_v1"
_CONTROLLED_RAW_SYNC_ONLY_SERVICE_NAMES = ("heartbeat", "raw_sync")
_daemon_run_profile = _PRODUCTION_RUN_PROFILE
_daemon_service_names: tuple[str, ...] | None = None


def _service_names_for_profile(*, controlled_raw_sync_only: bool) -> tuple[str, ...]:
    """Return the complete, auditable service manifest for one daemon profile."""
    return _entrypoint_support.service_names_for_profile(
        _ENTRYPOINT_HOST,
        controlled_raw_sync_only=controlled_raw_sync_only,
    )


def _activate_daemon_profile(*, controlled_raw_sync_only: bool) -> None:
    """Bind process-global runtime reporting to the selected daemon profile."""
    global _daemon_run_profile, _daemon_service_names
    _daemon_run_profile = (
        _CONTROLLED_RAW_SYNC_ONLY_RUN_PROFILE
        if controlled_raw_sync_only
        else _PRODUCTION_RUN_PROFILE
    )
    _daemon_service_names = _service_names_for_profile(
        controlled_raw_sync_only=controlled_raw_sync_only
    )


def _reset_daemon_profile() -> None:
    """Restore import-time defaults after a daemon instance exits or fails to start."""
    global _daemon_run_profile, _daemon_service_names
    _daemon_run_profile = _PRODUCTION_RUN_PROFILE
    _daemon_service_names = None


def _active_service_names() -> tuple[str, ...]:
    """Return the services bound to this process, preserving test-time intervals."""
    return _daemon_service_names or tuple(INTERVALS)


def _active_intervals() -> Dict[str, int]:
    """Return intervals for the services the current daemon instance may schedule."""
    return _entrypoint_support.active_intervals(_ENTRYPOINT_HOST)


def _apply_runtime_paths(paths: Any) -> None:
    """Apply resolved runtime paths to the daemon entrypoint host."""
    _entrypoint_support.apply_runtime_paths(_ENTRYPOINT_HOST, paths)


def _configure_runtime_paths(cfg=None) -> Any:
    """Resolve daemon runtime paths at command/runtime entrypoints."""
    try:
        paths = _runtime.RuntimePaths.from_config(cfg)
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        paths = _runtime.RuntimePaths.fallback()
    _apply_runtime_paths(paths)
    return paths


# 全局停止事件
stop_event = threading.Event()

# 正在线程池中执行的服务 Future（P105：避免重服务重叠执行）
_service_futures: Dict[str, Any] = {}

# 各服务最近一次执行结果摘要（供 heartbeat/status 使用）
_service_results: Dict[str, Dict[str, Any]] = {}

# 各服务最近捕获的已知运维错误摘要，供 health/doctor 展示。
_service_error_state: Dict[str, Dict[str, Any]] = {}


def _service_enabled(cfg, service_name: str) -> bool:
    """Read one canonical daemon service switch."""
    return _entrypoint_support.service_enabled(
        _ENTRYPOINT_HOST,
        cfg,
        service_name,
    )


def _start_wiki_auto_commit(cfg) -> None:
    """启动 Wiki 数据仓库自动提交监控（如果服务启用）。"""
    global _wiki_auto_commit_handler
    if _wiki_auto_commit_handler is not None:
        return
    if not _service_enabled(cfg, "wiki_auto_commit"):
        logger.info("[DAEMON] wiki_auto_commit 服务已关闭，跳过")
        return

    try:
        from scripts.auto_commit_wiki import start_auto_commit

        _wiki_auto_commit_handler = start_auto_commit()
        if _wiki_auto_commit_handler is not None:
            logger.info("[DAEMON] Wiki 自动提交服务已启动")
    except DAEMON_OPERATION_ERRORS as exc:
        logger.warning("[DAEMON] Wiki 自动提交服务启动失败: %s", exc, exc_info=True)


# 全局 EventBus 实例（daemon 生命周期内复用）
_event_bus_instance: Optional[Any] = None

# Wiki 自动提交监控句柄（daemon 生命周期内复用）
_wiki_auto_commit_handler: Optional[Any] = None

# 全局 CognitiveGraphUpdater 实例（daemon 生命周期内复用）
_cognitive_graph_updater: Optional[Any] = None

_cognition_episode_dispatch_owner: Optional[Any] = None

# 全局 ReflectionEngine 实例（daemon 生命周期内复用，已注册 Layer 5 消费者）
_reflection_engine_instance: Optional[Any] = None

# 全局 AdaptiveConfig 实例（daemon 生命周期内复用）
_adaptive_config_instance: Optional[Any] = None

# 全局 KIA PluggableModule registry（daemon 生命周期内复用）
_kia_module_registry: Optional[Any] = None

# 全局 CaptureWorkerPool 实例（daemon 生命周期内复用）
_capture_worker_pool: Optional[Any] = None

# 数据库维护任务单例（daemon 生命周期内复用）
_db_maintenance_task: Optional[_maintenance.DatabaseMaintenanceTask] = None

# KIA scheduler singleton（daemon 生命周期内复用，避免每次 tick 重复订阅 EventBus）
_knowledge_scheduler_instance: Optional[Any] = None

# TriggerDispatcher 相关（事件驱动 Raw 同步）
_trigger_dispatcher: Optional[Any] = None
_trigger_dirty_sources: set = set()
_trigger_lock: threading.Lock = threading.Lock()

# FileIngestor 相关（守护进程定时摄入目录）
_file_ingestor_instance: Optional[Any] = None

# Stat-based watchers（轻量轮询后备）
_agent_path_watcher: Optional[AgentPathWatcher] = None
_agent_path_watcher_primed: bool = False

# 统一运行时上下文（由 run_daemon 设置，供延迟创建的资源注册）
_runtime_context: Optional[Any] = None


def _get_adaptive_config():
    """懒加载 AdaptiveConfig，复用 daemon 生命周期内的实例，并注入全局 EffectivePolicy。"""
    return _entrypoint_support.get_adaptive_config(_ENTRYPOINT_HOST)


def _get_kia_module_registry(cfg=None):
    """Build and reuse the daemon-level KIA pluggable module registry."""
    return _entrypoint_support.get_kia_module_registry(_ENTRYPOINT_HOST, cfg)


def _kia_stress_test_dry_run(cfg: Any) -> bool:
    return bool(cfg.get("stress_test.dry_run", True))


def _start_kia_modules(cfg=None) -> Dict[str, Any]:
    """Start enabled KIA modules in registry dependency order."""
    registry = _get_kia_module_registry(cfg)
    if registry is None:
        return {}
    try:
        status = cast(Dict[str, Any], registry.start_enabled())
        running = sum(1 for item in status.values() if item.get("state") == "running")
        logger.info("[DAEMON] KIA modules started: %d running / %d total", running, len(status))
        return status
    except DAEMON_OPERATION_ERRORS as exc:
        _log_service_error("kia_modules", exc)
        return {"errors": 1, "error": str(exc)}


def _stop_kia_modules() -> Dict[str, Any]:
    """Stop daemon-level KIA modules in reverse start order."""
    global _kia_module_registry
    registry = _kia_module_registry
    if registry is None:
        return {}
    try:
        status = cast(Dict[str, Any], registry.stop_all())
        logger.info("[DAEMON] KIA modules stopped: %d", len(status))
        return status
    except DAEMON_OPERATION_ERRORS as exc:
        logger.warning("[DAEMON] KIA modules 停止失败: %s", exc, exc_info=True)
        return {"errors": 1, "error": str(exc)}
    finally:
        _kia_module_registry = None


# ── 文件锁（Unix）──


def _acquire_pid_lock(cfg: Any | None = None) -> bool:
    """Acquire the PID lock and bind it to a complete OS-derived instance identity."""
    global _daemon_instance_identity
    record = _instance_control.acquire_instance_lock(
        PID_FILE,
        database_dir=PID_FILE.parent,
        service_names=_active_service_names(),
        project_root=_PROJECT_ROOT,
        config_fingerprint=getattr(cfg, "config_fingerprint", None),
        log=logger,
    )
    _daemon_instance_identity = record
    return record is not None


def _release_pid_lock() -> None:
    """释放 PID 文件锁并删除文件。"""
    global _daemon_instance_identity
    _process_control.release_pid_lock(PID_FILE, log=logger)
    _daemon_instance_identity = None


def _count_daemon_processes() -> int:
    """统计 mnemos_daemon.py 进程数，排除当前进程和测试进程。"""
    return _process_control.count_daemon_processes(log=logger)


# ── 状态文件（子进程 ↔ 父进程通信）──


def _write_startup_status(success: bool, error: str = "") -> None:
    _process_control.write_startup_status(STATUS_FILE, success, error, log=logger)


def _read_startup_status(timeout: float = 3.0) -> tuple[bool, Optional[int], str]:
    """读取子进程启动状态。返回 (成功, pid, 错误信息)。"""
    return _process_control.read_startup_status(STATUS_FILE, timeout, log=logger)


def _write_daemon_heartbeat(snapshot: Dict[str, Any]) -> None:
    """Persist the latest daemon heartbeat for out-of-process health checks."""
    _heartbeat.write_daemon_heartbeat(DAEMON_HEARTBEAT_FILE, snapshot, log=logger)


def _clear_startup_status() -> None:
    _process_control.clear_startup_status(STATUS_FILE, log=logger)


# ── Daemonize ──


def _daemonize_unix() -> None:
    """Unix 平台 double-fork 到后台。"""
    _command_control.daemonize_unix(os, sys)


def _daemonize_windows() -> None:
    """Windows is already detached by cmd_start; lock acquisition is shared below."""
    _command_control.daemonize_windows()


# ── 异常分类 ──


def _log_service_error(service_name: str, exc: Exception) -> None:
    """按异常类型分级记录服务错误。"""
    _service_state.log_service_error(_service_error_state, service_name, exc, log=logger)


def _record_service_recovery_action(
    service_name: str,
    previous_error: Dict[str, Any],
    result: Dict[str, Any],
    cfg: Any,
) -> None:
    from daemon.runtime_flow_receipts import record_service_recovery_action

    record_service_recovery_action(service_name, previous_error, result, cfg, log=logger)


def _mark_service_recovered(
    service_name: str,
    result: Dict[str, Any],
    cfg: Any,
) -> Dict[str, Any]:
    previous_error = _service_state.clear_service_error(_service_error_state, service_name)
    if not previous_error:
        return result
    result["_recovered_error_count"] = int(previous_error.get("count", 0) or 0)
    result["_recovered_error_type"] = previous_error.get("last_error_type", "")
    result["_recovered_error_context"] = previous_error.get("last_context", service_name)
    _record_service_recovery_action(service_name, previous_error, result, cfg)
    return result


def _make_service_done_callback(service_name: str):
    """线程池服务完成回调：保存结果摘要、清理 future 并记录未捕获异常。"""
    return _service_state.make_service_done_callback(
        service_name,
        service_futures=_service_futures,
        service_results=_service_results,
        error_state=_service_error_state,
        service_enabled=_service_enabled,
        log=logger,
    )


def _resolve_service_call(cfg, service_name: str):
    """将服务名解析为可调用的无参函数（P105 线程池调度用）。"""
    return _service_registry.resolve_service_call(service_name, globals(), cfg)


def _get_file_ingest_dir(cfg) -> Optional[Path]:
    """解析 FileIngestor 监控目录，默认使用 DATA_DIR/file_ingest"""
    return _file_ingest.resolve_ingest_dir(cfg, _DATA_DIR)


def _start_trigger_dispatcher(cfg) -> None:
    """启动 TriggerDispatcher，为已发现的 AgentSource 注册事件触发器，并监控文件摄入目录。"""
    global _trigger_dispatcher, _file_ingestor_instance
    if _trigger_dispatcher is not None:
        return
    if not _service_enabled(cfg, "trigger_dispatcher"):
        return

    try:
        from core.sync_framework.triggers import TriggerDispatcher
        from core.sync_framework.registry import SourceRegistry, PathDiscover
        from core.sync_framework.file_ingestor import FileIngestor
        from core.agent_kit.source_support_manifest import (
            AgentSourceSupportManifestError,
            get_agent_source_support_manifest,
        )

        class _SourceAwareDispatcher:
            """为每个来源维护独立 TriggerDispatcher，避免单一回调无法区分来源。"""

            def __init__(self):
                self._dispatchers: Dict[str, Any] = {}

            def register(
                self, source_name: str, strategy: Dict[str, Any], watch_path: Path, callback
            ):
                dispatcher = TriggerDispatcher(callback=callback)
                dispatcher.register(source_name, strategy, watch_path)
                self._dispatchers[source_name] = dispatcher

            def start_all(self):
                for dispatcher in self._dispatchers.values():
                    dispatcher.start_all()

            def stop_all(self):
                for dispatcher in self._dispatchers.values():
                    try:
                        dispatcher.stop_all()
                    except (
                        OSError,
                        ValueError,
                        TypeError,
                        KeyError,
                        ImportError,
                        AttributeError,
                        RuntimeError,
                    ):
                        logger.debug("TriggerDispatcher 子调度器停止失败", exc_info=True)

        dispatcher = _SourceAwareDispatcher()

        # 1. 为每个已发现的 AgentSource 注册触发器
        SourceRegistry.register_builtin_agents()
        support_manifest = get_agent_source_support_manifest()
        for source in SourceRegistry.auto_discover():
            try:
                data_dir = source.data_dir
                if data_dir is None:
                    data_dir = PathDiscover.find(source.name)
                if data_dir and data_dir.exists():
                    source_name = source.name
                    source_spec = support_manifest.require_active_source(source_name)
                    strategy = source.trigger_strategy
                    expected_trigger = str(source_spec.continuous["trigger"])
                    if not isinstance(strategy, dict) or strategy.get("type") != expected_trigger:
                        raise AgentSourceSupportManifestError(
                            f"{source_spec.name}: runtime trigger strategy does not match "
                            "the manifest continuous contract"
                        )

                    def _on_agent_change(path: str, name=source_name):
                        logger.debug("[DAEMON] Trigger dirty: %s from %s", name, path)
                        with _trigger_lock:
                            _trigger_dirty_sources.add(name)

                    dispatcher.register(source_name, strategy, data_dir, _on_agent_change)
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
            ):
                logger.warning(
                    "[DAEMON] TriggerDispatcher 注册 %s 失败",
                    getattr(source, "name", "?"),
                    exc_info=True,
                )

        # 2. 监控文件摄入目录，变化时直接调用 FileIngestor
        ingest_dir = _get_file_ingest_dir(cfg)
        if ingest_dir and ingest_dir.exists():
            _file_ingestor_instance = FileIngestor()

            def _on_file_change(path: str):
                try:
                    _file_ingestor_instance.ingest_file(Path(path), agent_name="file")
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    ImportError,
                    AttributeError,
                    RuntimeError,
                ):
                    logger.warning("[DAEMON] 文件摄入失败: %s", path, exc_info=True)

            dispatcher.register(
                "file_ingestor",
                {"type": "watchdog", "events": ["created", "modified"], "debounce": 5.0},
                ingest_dir,
                _on_file_change,
            )

        dispatcher.start_all()
        _trigger_dispatcher = dispatcher
        logger.info("[DAEMON] TriggerDispatcher 已启动")
    except DAEMON_OPERATION_ERRORS as exc:
        logger.warning("[DAEMON] TriggerDispatcher 启动失败: %s", exc, exc_info=True)
        _trigger_dispatcher = None


def _sync_trigger_dirty_sources(cfg) -> None:
    """同步被 TriggerDispatcher 标记为 dirty 的 AgentSource"""
    with _trigger_lock:
        dirty = list(_trigger_dirty_sources)
        _trigger_dirty_sources.clear()
    _agent_source_runtime.sync_dirty_sources(
        dirty,
        cfg,
        raw_sync=_raw_sync,
        trigger_sync=_triggers.sync_dirty_sources,
        log_service_error=_log_service_error,
        log=logger,
    )


def _get_agent_path_watcher(cfg) -> Optional[AgentPathWatcher]:
    """Lazy singleton for the agent path watcher."""
    global _agent_path_watcher
    if _agent_path_watcher is not None:
        return _agent_path_watcher
    try:
        from core.sync_framework.registry import SourceRegistry

        SourceRegistry.register_builtin_agents()
        agents = [source.name for source in SourceRegistry.auto_discover()]
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError):
        logger.debug("[DAEMON] AgentPathWatcher 未发现 Agent", exc_info=True)
        raise
    _agent_path_watcher = AgentPathWatcher(agents)
    return _agent_path_watcher


def service_agent_path_watch() -> Dict[str, Any]:
    """Poll discovered Agent data paths and mark changed sources dirty.

    This feeds the existing trigger_dispatcher/sync pipeline: the next
    trigger_dispatcher tick will sync any sources added to
    _trigger_dirty_sources.
    """
    global _agent_path_watcher_primed
    result: Dict[str, Any] = {"enabled": False, "changed": 0, "marked_dirty": 0, "errors": 0}
    try:
        from core.config import get_config

        cfg = get_config()
        if not _service_enabled(cfg, "agent_path_watch"):
            return result
        if not cfg.get("watchers.enabled", False):
            return result
        if not cfg.get("watchers.agent_paths.enabled", False):
            return result
        watcher = _get_agent_path_watcher(cfg)
        if watcher is None:
            return result
        states = watcher.refresh()
        unavailable = [
            state for state in states if state.availability_state == "unavailable"
        ]
        result["errors"] += len(unavailable)
        for state in unavailable:
            _log_service_error(
                "agent_path_watch",
                RuntimeError(state.error_code or "agent_path_inspection_unavailable"),
            )
        if not _agent_path_watcher_primed:
            if not unavailable:
                _agent_path_watcher_primed = True
            return result
        result["enabled"] = True
        changed = [state for state in states if state.changed and state.exists]
        result["changed"] = len(changed)
        for state in changed:
            logger.debug("[DAEMON] agent path changed: %s %s", state.agent, state.path)
            with _trigger_lock:
                _trigger_dirty_sources.add(state.agent)
            result["marked_dirty"] += 1
    except DAEMON_OPERATION_ERRORS as exc:
        _log_service_error("agent_path_watch", exc)
        result["errors"] += 1
    return result


def _get_reflection_engine():
    """获取 daemon 全局 ReflectionEngine，已注册 Layer 5 消费者。"""
    return _entrypoint_support.get_reflection_engine(_ENTRYPOINT_HOST)


# ── 服务函数 ──


def service_capture_worker() -> Dict[str, Any]:
    global _capture_worker_pool
    result = {"processed": 0, "errors": 0}
    try:
        if _capture_worker_pool is None:
            from core.sync_framework.capture_worker import CaptureWorkerPool

            _capture_worker_pool = CaptureWorkerPool()
            _capture_worker_pool.start()
            if _runtime_context is not None:
                _runtime_context.register(
                    "capture_worker_pool",
                    _capture_worker_pool,
                    closer=lambda pool: pool.close(),
                )
        try:
            from core.config import get_config

            limit = int(get_config().get("capture.max_batch_per_tick", 10))
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            limit = 10
        batch_result = _capture_worker_pool.process_batch(limit=limit)
        result["processed"] = batch_result.get("processed", 0)
        result["errors"] = batch_result.get("errors", 0)
    except DAEMON_OPERATION_ERRORS as exc:
        _log_service_error("capture_worker", exc)
        result["errors"] += 1
    return result


def service_heartbeat() -> Dict[str, Any]:
    """返回 daemon 心跳，包含各服务最近执行摘要。"""
    return _entrypoint_support.service_heartbeat(_ENTRYPOINT_HOST)


def service_inbox_scanner() -> Dict[str, Any]:
    result = {"processed": 0}
    try:
        from core.kia.knowledge_inbox import KnowledgeInbox

        inbox = KnowledgeInbox()
        items = inbox.scan_inbox()
        for item in items:
            inbox.process_file(item)
        result["processed"] = len(items)
    except DAEMON_OPERATION_ERRORS as exc:
        _log_service_error("inbox_scanner", exc)
    return result


def service_file_ingestor(cfg=None) -> Dict[str, Any]:
    """扫描文件摄入目录并将新文件写入 L1"""
    return _entrypoint_support.service_file_ingestor(_ENTRYPOINT_HOST, cfg)


def service_signal_collector() -> Dict[str, Any]:
    result: Dict[str, Any] = {"collected": 0, "errors": 0}
    try:
        from core.config import get_config

        cfg = get_config()
        if cfg.get("persona.enabled", True):
            from core.persona.daimon import SignalCollector

            collector = SignalCollector()
            stats = collector.collect_all()
            result["collected"] = sum(v for v in stats.values() if v > 0)
            result["sources"] = stats
    except DAEMON_OPERATION_ERRORS as exc:
        _log_service_error("signal_collector", exc)
        result["errors"] += 1
    try:
        from core.app.application_signal_service import ApplicationSignalService
        from core.config import get_config

        app_stats = ApplicationSignalService(config=get_config()).run()
        result["application_signals"] = app_stats
        result["collected"] += int(app_stats.get("persisted", 0) or 0)
    except DAEMON_OPERATION_ERRORS as exc:
        _log_service_error("application_signals", exc)
        result["errors"] += 1
    return result


def service_persona_analyzer() -> Dict[str, Any]:
    result: Dict[str, Any] = {"analyzed": False}
    try:
        from core.config import get_config

        cfg = get_config()
        if not cfg.get("persona.enabled", True):
            return result

        from core.application.persona import PersonaApplicationService
        from core.persona.psyche import get_signal_store

        store = get_signal_store()
        replayed = store.replay_profile_usage_outbox()
        result = PersonaApplicationService().run_canonical_revision_cycle(
            signal_store=store,
            days=30,
        )
        result["profile_usage_replayed"] = len(replayed)
        return result
    except DAEMON_OPERATION_ERRORS as exc:
        _log_service_error("persona_analyzer", exc)
    return result


def service_eventbus_health() -> Dict[str, Any]:
    """EventBus 健康检查：确保后台分发线程存活，并报告队列深度。

    事件消费统一由 run_daemon() 中启动的后台分发线程负责，本服务不再
    额外轮询/处理 SQLite 中的 pending 事件，避免与后台线程竞争状态或
    重复确认事件。
    """
    result = {"status": "ok", "queue_depth": 0}
    if _event_bus_instance is None:
        logger.debug("[DAEMON] EventBus 未初始化，跳过健康检查")
        return result
    try:
        dispatch_thread = getattr(_event_bus_instance, "_dispatch_thread", None)
        if dispatch_thread is None or not dispatch_thread.is_alive():
            logger.warning("[DAEMON] EventBus 分发线程停止，尝试重启")
            _event_bus_instance.start_dispatch()
            result["restarted"] = True
        result["queue_depth"] = (
            getattr(_event_bus_instance, "_queue", None) and _event_bus_instance._queue.qsize() or 0
        )
    except DAEMON_OPERATION_ERRORS as exc:
        _log_service_error("eventbus", exc)
        result["status"] = "error"
    return result


# 历史服务名兼容：外部监控/测试仍可通过 service_eventbus 引用。
service_eventbus = service_eventbus_health


def service_raw_sync() -> Dict[str, Any]:
    return _entrypoint_support.service_raw_sync(_ENTRYPOINT_HOST)


service_l1_sync = service_raw_sync  # noqa: Vulture - legacy daemon service alias.


def service_raw_projection() -> Dict[str, Any]:
    return _raw_projection_service.service_raw_projection(_ENTRYPOINT_HOST)


def service_retry_failed(cfg=None) -> Dict[str, Any]:
    """重试 sync_log 中失败的同步记录。"""
    result = {"retried": 0, "errors": 0}
    try:
        from core.config import get_config
        from core.sync_framework.sync_engine import SyncEngine

        engine = SyncEngine()
        try:
            limit = int(get_config().get("sync.retry_failed_limit", 50))
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            limit = 50
        try:
            results = engine.retry_failed(agent_name=None, limit=limit)
            result["retried"] = len(results)
        except DAEMON_OPERATION_ERRORS as exc:
            _log_service_error("retry_failed", exc)
            result["errors"] += 1
        finally:
            engine.close()
    except DAEMON_OPERATION_ERRORS as exc:
        _log_service_error("retry_failed", exc)
        result["errors"] += 1
    return result


service_distill_and_merge = lambda: _entrypoint_support.service_distill_and_merge(_ENTRYPOINT_HOST)  # noqa: E501,E731
service_distill_cognitive_actions = lambda: _entrypoint_support.service_distill_cognitive_actions(_ENTRYPOINT_HOST)  # noqa: E501,E731
service_operational_incidents = lambda: _entrypoint_support.service_operational_incidents(_ENTRYPOINT_HOST)  # noqa: E501,E731


def service_wiki_route() -> Dict[str, Any]:
    """Route classifiable Inbox Wiki pages into formal Obsidian folders."""
    return _entrypoint_support.service_wiki_route(_ENTRYPOINT_HOST)


def service_scheduler_tick() -> Dict[str, Any]:
    """Chronos 知识调度器 tick — 驱动影子页面、scorer 训练队列等定时步骤。"""
    global _knowledge_scheduler_instance
    result = {"steps_run": 0, "steps": []}
    try:
        from core.config import get_config
        from core.kia.chronos import KnowledgeScheduler

        if _knowledge_scheduler_instance is None:
            cfg = get_config()
            include_heavy = bool(cfg.get("daemon.scheduler_heavy_steps_enabled", False))
            _knowledge_scheduler_instance = KnowledgeScheduler()
            _knowledge_scheduler_instance.register_all_default_steps(
                include_heavy_steps=include_heavy
            )
            if _runtime_context is not None:
                _runtime_context.register(
                    "knowledge_scheduler",
                    _knowledge_scheduler_instance,
                    closer=lambda scheduler: scheduler.shutdown(),
                )
        scheduler = _knowledge_scheduler_instance
        tick_results = scheduler.tick()
        result["steps_run"] = len(tick_results)
        result["steps"] = [
            {"name": name, "status": r.get("status", "unknown")} for name, r in tick_results.items()
        ]
        if tick_results:
            logger.info("scheduler.tick: %d 个步骤执行完成", len(tick_results))
    except DAEMON_OPERATION_ERRORS as exc:
        _log_service_error("scheduler_tick", exc)
    return result


def service_adaptive_config() -> Dict[str, Any]:
    """AdaptiveConfig 自适应调整 tick — 采集指标、检查回滚、建议调整、应用调整。"""
    return _entrypoint_support.service_adaptive_config(_ENTRYPOINT_HOST)


def service_search_ignore_detection() -> Dict[str, Any]:
    """[P2-10] 检测搜索忽略信号：超过 5 分钟未被点击的搜索会话记为 ignore。"""
    return _entrypoint_support.service_search_ignore_detection(_ENTRYPOINT_HOST)


def service_user_correction_detection() -> Dict[str, Any]:
    """[P2-10] 检测用户修正信号：蒸馏生成的 wiki 页面被用户手动编辑。"""
    return _entrypoint_support.service_user_correction_detection(_ENTRYPOINT_HOST)


def service_observation_engine() -> Dict[str, Any]:
    """L3 Observation Engine — 从 L1 raw + L2 wiki 增量提取客观观察并持久化。"""
    return _entrypoint_support.service_observation_engine(_ENTRYPOINT_HOST)


def service_reflection_engine() -> Dict[str, Any]:
    """L4 Reflection Engine — 定时触发一次通用 Reflection。"""
    return _entrypoint_support.service_reflection_engine(_ENTRYPOINT_HOST)


def service_feedback_prompt() -> Dict[str, Any]:
    """L5 Feedback — 扫描 pending feedback 并通过 daemon 全局 EventBus 发布提示事件。"""
    return _entrypoint_support.service_feedback_prompt(_ENTRYPOINT_HOST)


def service_recap_consumption() -> Dict[str, Any]:
    """Retry durable recap target and correction receipts."""
    return _entrypoint_support.service_recap_consumption(_ENTRYPOINT_HOST)


def service_cognitive_graph_reconcile() -> Dict[str, Any]:
    """跨层认知图 reconciliation — 消费 outbox 并补全缺失关系。"""
    return _entrypoint_support.service_cognitive_graph_reconcile(_ENTRYPOINT_HOST)


def service_prediction_maturity() -> Dict[str, Any]:
    """Close mature canonical predictions in one bounded idempotent batch."""

    return _prediction_service.run_service(_log_service_error)


def service_training_governance() -> Dict[str, Any]:
    """Reconcile canonical training projections and deterministic runs."""

    return _training_governance_service.run_service(_log_service_error)


def service_cognitive_consolidation() -> Dict[str, Any]:
    """Generate a read-only consolidation plan; trusted commit stays external."""

    return _consolidation_service.run_service(_log_service_error)


def service_reminder_scan() -> Dict[str, Any]:
    """统一提醒引擎 — 全量扫描 wiki 新鲜度，高优先级过期页面入队提醒。"""
    return _entrypoint_support.service_reminder_scan(_ENTRYPOINT_HOST)


def service_freshness_refresh() -> Dict[str, Any]:
    """自动刷新高优先级过期页面，并归档超期冷知识。"""
    return _entrypoint_support.service_freshness_refresh(_ENTRYPOINT_HOST)


def service_entropy_scan() -> Dict[str, Any]:
    """熵减扫描 — 将高相似候选入队到对话提醒。"""
    return _entrypoint_support.service_entropy_scan(_ENTRYPOINT_HOST)


def service_dispute_scan() -> Dict[str, Any]:
    """争议自动扫描与仲裁 — 检测知识冲突并自动裁决或生成争议页。"""
    return _entrypoint_support.service_dispute_scan(_ENTRYPOINT_HOST)


def service_link_probe(cfg=None) -> Dict[str, Any]:
    """外部链接可达性探测 — 批量扫描 pending 链接并反写失效链接到 frontmatter。"""
    return _entrypoint_support.service_link_probe(_ENTRYPOINT_HOST, cfg)


def service_db_maintenance(cfg=None) -> Dict[str, Any]:
    """数据库定期维护：保留期清理、WAL checkpoint、optimize、VACUUM。"""
    return _entrypoint_support.service_db_maintenance(_ENTRYPOINT_HOST, cfg)


# ── 辅助函数 ──


def _run_startup_compensation() -> Dict[str, Any]:
    return _entrypoint_support.run_startup_compensation(_ENTRYPOINT_HOST)


def _run_startup_cleanup() -> Dict[str, Any]:
    return _entrypoint_support.run_startup_cleanup(_ENTRYPOINT_HOST)


def _print_model_status(daemon_pid: int) -> str:
    return _entrypoint_support.print_model_status(_ENTRYPOINT_HOST, daemon_pid)


def _generate_drift_report() -> Dict[str, Any]:
    return _entrypoint_support.generate_drift_report(_ENTRYPOINT_HOST)


def _run_preflight_checks() -> Dict[str, Any]:
    return _entrypoint_support.run_preflight_checks(_ENTRYPOINT_HOST)


def _on_session_end(event) -> None:
    """session.end 事件处理器：增量提取 Observation + 自动触发 Reflection。"""
    _entrypoint_support.on_session_end(_ENTRYPOINT_HOST, event)


def _on_observation_updated(event) -> None:
    """observation.updated 事件处理器：对高置信度/突变观察自动触发 Reflection。

    把 L3 Observation 层的变化作为 L4 Reflection 的触发源之一，补齐
    Observation → Reflection 的事件驱动链路。
    """
    _entrypoint_support.on_observation_updated(_ENTRYPOINT_HOST, event)


def _on_knowledge_stale(event) -> None:
    """knowledge_stale 事件处理器：自动刷新过期知识页面。

    把 EvolutionTracker 检测到的 stale 页面作为自动刷新触发源，
    补齐 knowledge_stale → FreshnessRefreshWorker 的事件驱动链路。
    """
    _entrypoint_support.on_knowledge_stale(_ENTRYPOINT_HOST, event)


def _run_persona_challenge() -> Dict[str, Any]:
    """Consume one durable challenge command emitted by a real DecisionTrace."""

    try:
        from core.config import get_config
        from core.persona.challenge_queue import PersonaChallengeQueueConsumer

        cfg = get_config()
        if not cfg.get("persona.enabled", True):
            return {
                "challenges": 0,
                "consumed": 0,
                "status": "noop",
                "reason": "persona_disabled",
            }
        return PersonaChallengeQueueConsumer(cfg).run_once()
    except DAEMON_OPERATION_ERRORS as exc:
        _log_service_error("persona_challenge", exc)
        return {
            "challenges": 0,
            "consumed": 0,
            "status": "retry",
            "reason": "persona_challenge_consumer_error",
        }


# ── 主循环 ──


def _setup_logging() -> None:
    _entrypoint_support.setup_logging(_ENTRYPOINT_HOST)


def _register_kg_event_handlers(event_bus: Any) -> None:
    """注册 Wiki 生命周期的 KG、索引、MOC 与 metrics 投影消费者。"""
    from core.config import get_config
    from daemon.wiki_projection_handlers import register_wiki_projection_handlers

    register_wiki_projection_handlers(event_bus, get_config())


def _register_kia_event_handlers(event_bus: Any) -> None:
    """注册免疫/DNA/熵减报告消费者。"""
    try:
        from core.kia.kia_event_consumer import KIAEventConsumer

        consumer = KIAEventConsumer()

        def _on_immune_report(event):
            return consumer.on_immune_report(event.payload)

        def _on_dna_computed(event):
            return consumer.on_dna_computed(event.payload)

        def _on_entropy_suggestions(event):
            return consumer.on_entropy_suggestions(event.payload)

        event_bus.subscribe("immune.report", _on_immune_report)
        event_bus.subscribe("dna.computed", _on_dna_computed)
        event_bus.subscribe("entropy.suggestions", _on_entropy_suggestions)
        logger.info(
            "[DAEMON] KIAEventConsumer 已订阅 immune.report / dna.computed / entropy.suggestions"
        )
    except DAEMON_OPERATION_ERRORS as kia_exc:
        logger.warning("[DAEMON] KIAEventConsumer 订阅失败: %s", kia_exc, exc_info=True)


def _register_telemetry_handlers(event_bus: Any) -> None:
    """注册 telemetry 事件的审计 sink。"""
    _entrypoint_support.register_telemetry_handlers(_ENTRYPOINT_HOST, event_bus)


def _register_cognitive_graph(event_bus: Any) -> None:
    """注册 CognitiveGraphUpdater 订阅跨层事件。"""
    try:
        from core.cognitive_graph import CognitiveGraphStore, CognitiveGraphUpdater

        global _cognitive_graph_updater
        cg_store = CognitiveGraphStore()
        _cognitive_graph_updater = CognitiveGraphUpdater(store=cg_store)
        _cognitive_graph_updater.subscribe(event_bus)
        logger.info("[DAEMON] CognitiveGraphUpdater 已订阅 EventBus")
    except DAEMON_OPERATION_ERRORS as cg_exc:
        logger.warning("[DAEMON] CognitiveGraphUpdater 订阅失败: %s", cg_exc, exc_info=True)


def _register_cognition_episode_dispatch(event_bus: Any, cfg: Optional[Any]) -> None:
    from daemon.cognition_episode_dispatch import register_cognition_episode_dispatch

    global _cognition_episode_dispatch_owner
    _cognition_episode_dispatch_owner = register_cognition_episode_dispatch(
        event_bus,
        cfg,
        cognitive_graph_store=getattr(_cognitive_graph_updater, "store", None),
    )


def _register_session_event_handlers(event_bus: Any) -> None:
    """注册 session.start / session.end / observation.updated / knowledge_stale 处理器。"""
    _entrypoint_support.register_session_event_handlers(
        _ENTRYPOINT_HOST,
        event_bus,
    )


def _replay_dead_letters(event_bus: Any, cfg: Optional[Any]) -> None:
    """启动时重放已有消费者的 no_consumer 死信事件。"""
    try:
        replayed = event_bus.replay_no_consumer_dead_letters(
            limit=int(cfg.get("event_bus.startup_replay_limit", 500)) if cfg else 500,
            max_age_hours=(
                int(cfg.get("event_bus.dead_letter_replay_max_age_hours", 168)) if cfg else 168
            ),  # noqa: E501
            per_type_limit=(
                int(cfg.get("event_bus.dead_letter_replay_per_type_limit", 100)) if cfg else 100
            ),  # noqa: E501
        )
        if replayed:
            logger.info("[DAEMON] 已重放 %d 个已有消费者的 no_consumer 死信事件", replayed)
    except DAEMON_OPERATION_ERRORS as replay_exc:
        logger.warning("[DAEMON] no_consumer 死信重放失败: %s", replay_exc, exc_info=True)


def _start_event_bus_dispatch(event_bus: Any) -> None:
    event_bus.start_dispatch()
    logger.info("[DAEMON] EventBus 分发线程已启动")


def _initialize_event_bus(cfg: Optional[Any], *, start_dispatch: bool = True) -> Any:
    """初始化 EventBus 并注册所有处理器；失败时返回 None 并写入启动状态。"""
    try:
        from core.mnemos_bus import get_event_bus

        event_bus = get_event_bus(config=cfg)
        _register_kg_event_handlers(event_bus)
        _register_kia_event_handlers(event_bus)
        _register_telemetry_handlers(event_bus)
        _register_cognitive_graph(event_bus)
        _register_cognition_episode_dispatch(event_bus, cfg)
        _register_session_event_handlers(event_bus)
        _replay_dead_letters(event_bus, cfg)

        if start_dispatch:
            _start_event_bus_dispatch(event_bus)
        return event_bus
    except DAEMON_OPERATION_ERRORS as exc:
        logger.warning("[DAEMON] EventBus 初始化失败: %s", exc, exc_info=True)
        _write_startup_status(success=False, error=f"EventBus 初始化失败: {exc}")
        return None


def _load_daemon_config() -> Optional[Any]:
    """加载 daemon 配置，失败时返回 None 并记录警告。"""
    try:
        from core.config import get_config

        return get_config()
    except DAEMON_OPERATION_ERRORS as exc:
        logger.warning("[DAEMON] 加载配置失败，使用默认开关: %s", exc)
        return None


def _register_wiki_auto_commit(ctx: Any, cfg: Optional[Any]) -> None:
    """启动并注册 Wiki 自动提交监控。"""
    _start_wiki_auto_commit(cfg)
    if _wiki_auto_commit_handler is not None:
        ctx.register(
            "wiki_auto_commit",
            _wiki_auto_commit_handler,
            closer=lambda handler: handler.stop(),
        )


def _apply_interval_overrides(cfg: Optional[Any]) -> None:
    """用已加载配置覆盖默认服务间隔。"""
    if cfg is None:
        return
    _intervals.apply_interval_overrides(INTERVALS, cfg)


def _register_kia_modules(ctx: Any, cfg: Optional[Any]) -> None:
    """启动并注册 KIA 模块。"""
    if cfg is None:
        return
    _start_kia_modules(cfg)
    if _kia_module_registry is not None:
        if _event_bus_instance is not None:
            _kia_module_registry.subscribe_to_event_bus(_event_bus_instance)
            logger.info("[DAEMON] KIA module registry 已订阅 EventBus")
        ctx.register("kia_modules", _kia_module_registry, closer=lambda reg: reg.stop_all())


def _register_trigger_dispatcher(ctx: Any, cfg: Optional[Any]) -> None:
    """启动并注册 TriggerDispatcher。"""
    _start_trigger_dispatcher(cfg)
    if _trigger_dispatcher is not None:
        ctx.register(
            "trigger_dispatcher",
            _trigger_dispatcher,
            closer=lambda dispatcher: dispatcher.stop_all(),
        )


def _build_service_executor(cfg: Optional[Any]) -> concurrent.futures.ThreadPoolExecutor:
    """创建服务调度线程池。"""
    max_workers = 2
    if cfg is not None:
        try:
            max_workers = max(1, min(16, int(cfg.get("daemon.max_workers", 2))))
        except (TypeError, ValueError) as exc:
            logger.debug("daemon.max_workers 配置无效，使用默认值: %s", exc)
    return concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers, thread_name_prefix="daemon_service"
    )


def _schedule_service_if_due(
    cfg: Optional[Any],
    service_name: str,
    interval: float,
    now: float,
    last_run: Dict[str, float],
    executor: concurrent.futures.ThreadPoolExecutor,
) -> None:
    """检查并提交到期的周期性服务任务。"""
    if now - last_run.get(service_name, 0) < interval:
        return
    if not _service_enabled(cfg, service_name):
        last_run[service_name] = now
        logger.debug("[DAEMON] 服务 %s 已关闭，跳过", service_name)
        return

    existing = _service_futures.get(service_name)
    if existing is not None and not existing.done():
        logger.debug("[DAEMON] 服务 %s 仍在运行，跳过本次调度", service_name)
        return

    if _resource_budget.defer_if_needed(
        service_name,
        now,
        interval,
        last_run,
        _service_results,
        config=cfg,
    ):
        return
    last_run[service_name] = now
    try:
        fn = _resolve_service_call(cfg, service_name)
        future = executor.submit(fn)
        _service_futures[service_name] = future
        future.add_done_callback(_make_service_done_callback(service_name))
    except DAEMON_OPERATION_ERRORS as exc:
        logger.error("调度服务 %s 失败: %s", service_name, exc, exc_info=True)
        logger.debug(traceback.format_exc())


def _run_daemon_main_loop(
    cfg: Optional[Any],
    executor: concurrent.futures.ThreadPoolExecutor,
    *,
    service_names: tuple[str, ...] | None = None,
) -> None:
    """daemon 主循环：调度周期性服务。"""
    _entrypoint_support.run_daemon_main_loop(
        _ENTRYPOINT_HOST,
        cfg,
        executor,
        service_names=service_names,
    )


def _shutdown_daemon(ctx: Any) -> None:
    """关闭 RuntimeContext 并清理 daemon 全局引用。"""
    global _event_bus_instance, _capture_worker_pool, _trigger_dispatcher
    global _wiki_auto_commit_handler, _kia_module_registry, _runtime_context
    global _knowledge_scheduler_instance

    try:
        ctx.shutdown()
    except DAEMON_OPERATION_ERRORS as exc:
        logger.warning("[DAEMON] RuntimeContext shutdown 失败: %s", exc, exc_info=True)

    # 清空 daemon 级全局引用（资源本身已由 RuntimeContext 关闭）
    _event_bus_instance = None
    _capture_worker_pool = None
    _trigger_dispatcher = None
    _wiki_auto_commit_handler = None
    _kia_module_registry = None
    _knowledge_scheduler_instance = None
    _runtime_context = None

    _release_pid_lock()
    _reset_daemon_profile()
    logger.info("Mnemos Daemon 已退出")


def run_daemon(
    foreground: bool = False,
    *,
    controlled_raw_sync_only: bool = False,
) -> None:
    """Run the production daemon or an explicit, constrained Raw-sync profile.

    The constrained profile is for auditable Source-to-Raw recovery only. It
    intentionally excludes unrelated writers, EventBus replay, KIA modules,
    and startup compensation; the identity and heartbeat make the distinction
    durable and visible to the operator.
    """
    _entrypoint_support.run_daemon(
        _ENTRYPOINT_HOST,
        foreground,
        controlled_raw_sync_only=controlled_raw_sync_only,
    )


# ── CLI ──


def _daemon_command_context() -> _command_control.DaemonCommandContext:
    return _entrypoint_support.daemon_command_context(_ENTRYPOINT_HOST)


def cmd_start(*, controlled_raw_sync_only: bool = False) -> int:
    return _command_control.start(
        _daemon_command_context(),
        controlled_raw_sync_only=controlled_raw_sync_only,
    )


def cmd_stop(*, controlled_raw_sync_only: bool = False) -> int:
    return _command_control.stop(
        _daemon_command_context(),
        controlled_raw_sync_only=controlled_raw_sync_only,
    )


def cmd_status(*, controlled_raw_sync_only: bool = False) -> int:
    return _command_control.status(
        _daemon_command_context(),
        controlled_raw_sync_only=controlled_raw_sync_only,
    )


def cmd_run(*, controlled_raw_sync_only: bool = False) -> int:
    return _command_control.run(
        _daemon_command_context(),
        controlled_raw_sync_only=controlled_raw_sync_only,
    )


def _windows_task_command(script: Path) -> str:
    return _command_control.windows_task_command(_daemon_command_context(), script)


def cmd_install_windows() -> int:
    return _command_control.install_windows(_daemon_command_context())


def cmd_uninstall_windows() -> int:
    return _command_control.uninstall_windows(_daemon_command_context())


def main(argv: Optional[List[str]] = None) -> int:
    return _entrypoint_support.main(_ENTRYPOINT_HOST, argv)


if __name__ == "__main__":
    sys.exit(main())
