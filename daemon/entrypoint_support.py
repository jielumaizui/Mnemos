"""Broad-free orchestration used by the patchable ``mnemos_daemon`` facade.

The public daemon entrypoint intentionally keeps module-level wrapper names for
operator tooling and tests.  Implementations in this module receive that host
module explicitly, so dependency replacement remains visible and deterministic
without keeping hundreds of lines of orchestration in the executable facade.
"""

from __future__ import annotations

import random
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from daemon.command_control import DaemonCommandContext


def _dict_result(value: Any) -> Dict[str, Any]:
    """Type the result returned by the dynamic daemon facade boundary."""
    return cast(Dict[str, Any], value)


def service_names_for_profile(
    host: Any,
    *,
    controlled_raw_sync_only: bool,
) -> tuple[str, ...]:
    """Return the complete service manifest for one daemon profile."""
    if not controlled_raw_sync_only:
        return tuple(host.INTERVALS)
    missing = [
        name
        for name in host._CONTROLLED_RAW_SYNC_ONLY_SERVICE_NAMES
        if name not in host.INTERVALS
    ]
    if missing:
        raise RuntimeError(
            "controlled raw-sync profile is incomplete: "
            + ", ".join(sorted(missing))
        )
    return cast(tuple[str, ...], host._CONTROLLED_RAW_SYNC_ONLY_SERVICE_NAMES)


def active_intervals(host: Any) -> Dict[str, int]:
    """Return intervals for services bound to the active daemon profile."""
    return {
        name: host.INTERVALS[name]
        for name in host._active_service_names()
        if name in host.INTERVALS
    }


def service_enabled(host: Any, cfg: Any, service_name: str) -> bool:
    """Read a canonical service switch with constrained-profile semantics."""
    if host._daemon_run_profile == host._CONTROLLED_RAW_SYNC_ONLY_RUN_PROFILE:
        return service_name in host._active_service_names()
    if cfg is None:
        return True
    return bool(cfg.get(f"daemon.services.{service_name}"))


def apply_runtime_paths(host: Any, paths: Any) -> None:
    """Apply resolved runtime paths to the executable facade globals."""
    host._RUNTIME_PATHS = paths
    host._DATA_DIR = paths.data_dir
    host._DATABASE_DIR = paths.database_dir
    host.PID_FILE = paths.pid_file
    host.STATUS_FILE = paths.status_file
    host.DAEMON_LOG = paths.daemon_log
    host.DAEMON_HEARTBEAT_FILE = paths.heartbeat_file


def get_adaptive_config(host: Any) -> Any:
    """Build and cache the daemon-scoped adaptive configuration."""
    if host._adaptive_config_instance is None:
        try:
            from core.kia.adaptive_config import AdaptiveConfig
            from core.kia.policy import get_effective_policy

            host._adaptive_config_instance = AdaptiveConfig(
                policy=get_effective_policy(),
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            host.logger.debug("[DAEMON] AdaptiveConfig 初始化失败", exc_info=True)
    return host._adaptive_config_instance


def get_kia_module_registry(host: Any, cfg: Any = None) -> Any:
    """Build and cache the daemon-level KIA module registry."""
    if host._kia_module_registry is not None:
        return host._kia_module_registry
    try:
        if cfg is None:
            from core.config import get_config

            cfg = get_config()
        from core.kia.module_registry import build_kia_module_registry

        config_data = getattr(cfg, "_data", {})
        if not isinstance(config_data, dict):
            config_data = {}
        wiki_dir = getattr(cfg, "wiki_dir", None)
        dry_run = host._kia_stress_test_dry_run(cfg) if hasattr(cfg, "get") else True
        host._kia_module_registry = build_kia_module_registry(
            wiki_base=str(wiki_dir) if wiki_dir is not None else None,
            dry_run=dry_run,
            config=config_data,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        AttributeError,
        RuntimeError,
    ):
        host.logger.debug("[DAEMON] KIA module registry 初始化失败", exc_info=True)
    return host._kia_module_registry


def get_reflection_engine(host: Any) -> Any:
    """Build the daemon-global ReflectionEngine with its Layer 5 consumers."""
    if host._reflection_engine_instance is None:
        from core.config import get_config
        from core.persona.psyche import get_signal_store
        from core.reflection.reflection_engine import ReflectionEngine

        cfg = get_config()
        persona_store = get_signal_store()
        reflection_engine = ReflectionEngine(
            register_default_consumers=True,
            persona_store=persona_store,
            kia_store=None,
            wiki_dir=str(cfg.wiki_dir) if cfg.wiki_dir else None,
            export_to_wiki=True,
        )
        try:
            for consumer in getattr(reflection_engine, "_consumers", []):
                for child in getattr(consumer, "consumers", []):
                    if getattr(child, "kia_store", None) is None:
                        child.kia_store = reflection_engine.ref_store
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            host.logger.debug("设置 KIA 经验存储失败", exc_info=True)

        host._reflection_engine_instance = reflection_engine
        host.logger.info("[DAEMON] ReflectionEngine 已初始化并注册 Layer 5 消费者")
    return host._reflection_engine_instance


def service_heartbeat(host: Any) -> Dict[str, Any]:
    """Build and persist the daemon heartbeat."""
    from core.config import get_config

    cfg = get_config()
    module_registry = None
    if host._daemon_run_profile == host._PRODUCTION_RUN_PROFILE:
        module_registry = host._get_kia_module_registry(cfg)
    if host._daemon_instance_identity is None:
        raise RuntimeError("daemon instance identity is unavailable")
    snapshot = _dict_result(host._heartbeat.build_heartbeat_snapshot(
        instance_identity=host._daemon_instance_identity,
        intervals=host._active_intervals(),
        service_results=host._service_results,
        service_error_state=host._service_error_state,
        cfg=cfg,
        service_enabled=host._service_enabled,
        module_health=module_registry.health_check() if module_registry is not None else None,
        persisted_source_coverage=host._agent_source_runtime.persisted_source_coverage(
            cfg.database_dir
        ),
    ))
    snapshot["run_profile"] = host._daemon_run_profile
    host._write_daemon_heartbeat(snapshot)
    host.logger.debug("daemon heartbeat: %d services tracked", len(snapshot["services"]))
    return snapshot


def service_file_ingestor(host: Any, cfg: Any = None) -> Dict[str, Any]:
    """Scan the configured ingest directory and persist new L1 inputs."""
    return _dict_result(host._file_ingest.run_service(
        cfg,
        data_dir=host._DATA_DIR,
        log_service_error=host._log_service_error,
    ))


def service_raw_sync(host: Any) -> Dict[str, Any]:
    """Synchronize supported agent sources into canonical Raw."""
    return _dict_result(host._agent_source_runtime.run_raw_sync(
        raw_sync=host._raw_sync,
        log_service_error=host._log_service_error,
    ))


def service_distill_and_merge(host: Any) -> Dict[str, Any]:
    """Run one queued distillation-and-merge service tick."""

    from daemon.distill_service import service_distill_and_merge as run_service

    return run_service(host._log_service_error)


def service_distill_cognitive_actions(host: Any) -> Dict[str, Any]:
    """Run one durable distillation cognitive-action service tick."""

    from daemon.distill_service import service_distill_cognitive_actions as run_service

    return run_service(host._log_service_error)


def service_operational_incidents(host: Any) -> Dict[str, Any]:
    """Advance durable operational diagnosis and notification commands."""

    from core.config import get_config
    from daemon.operational_incident_service import run_service

    return run_service(get_config())


def service_wiki_route(host: Any) -> Dict[str, Any]:
    """Route accepted Wiki pages through the canonical daemon service."""

    from daemon.wiki_route import run_service

    return run_service(log_error=host._log_service_error)


def service_adaptive_config(host: Any) -> Dict[str, Any]:
    """Run one adaptive-configuration service tick."""

    return _dict_result(host._adaptive_service.run_service(
        host._get_adaptive_config,
        host._log_service_error,
        log_info=host.logger.info,
        log_debug=host.logger.debug,
    ))


def service_search_ignore_detection(host: Any) -> Dict[str, Any]:
    """Detect and persist eligible search-ignore scoring signals."""

    return _dict_result(host._scoring_signals.run_search_ignore_detection(
        host._log_service_error,
        log_info=host.logger.info,
    ))


def service_user_correction_detection(host: Any) -> Dict[str, Any]:
    """Detect and persist eligible user-correction scoring signals."""

    return _dict_result(host._scoring_signals.run_user_correction_detection(
        host._log_service_error,
        log_info=host.logger.info,
        log_warning=host.logger.warning,
    ))


def service_observation_engine(host: Any) -> Dict[str, Any]:
    """Run one incremental Observation extraction service tick."""

    return _dict_result(host._observation_service.run_service(
        host._log_service_error,
        log_info=host.logger.info,
    ))


def service_reflection_engine(host: Any) -> Dict[str, Any]:
    """Run one daemon-owned Reflection service tick."""

    return _dict_result(host._reflection_services.run_reflection_engine(
        host._get_reflection_engine,
        host._log_service_error,
        log_info=host.logger.info,
    ))


def service_feedback_prompt(host: Any) -> Dict[str, Any]:
    """Publish feedback prompts that are currently due."""

    return _dict_result(host._reflection_services.run_feedback_prompt(
        host._event_bus_instance,
        host._get_reflection_engine,
        host._log_service_error,
        log_info=host.logger.info,
        log_debug=host.logger.debug,
    ))


def service_recap_consumption(host: Any) -> Dict[str, Any]:
    """Replay pending recap consumption and correction receipts."""

    from core.config import get_config

    result = _dict_result(host._kia_services.run_recap_consumption(host._log_service_error))
    if not result.get("errors"):
        return _dict_result(
            host._mark_service_recovered("recap_consumption", result, get_config())
        )
    return result


def service_cognitive_graph_reconcile(host: Any) -> Dict[str, Any]:
    """Reconcile pending cognitive-graph outbox work."""

    return _dict_result(host._kia_services.run_cognitive_graph_reconcile(
        host._log_service_error,
        log_info=host.logger.info,
    ))


def service_reminder_scan(host: Any) -> Dict[str, Any]:
    """Scan canonical reminders and enqueue due work."""

    return _dict_result(host._kia_services.run_reminder_scan(
        host._log_service_error,
        log_info=host.logger.info,
        log_debug=host.logger.debug,
    ))


def service_freshness_refresh(host: Any) -> Dict[str, Any]:
    """Refresh or archive eligible stale knowledge pages."""

    return _dict_result(host._kia_services.run_freshness_refresh(
        host._log_service_error,
        log_info=host.logger.info,
        log_debug=host.logger.debug,
    ))


def service_entropy_scan(host: Any) -> Dict[str, Any]:
    """Scan for high-similarity entropy-reduction candidates."""

    return _dict_result(host._kia_services.run_entropy_scan(
        host._log_service_error,
        log_info=host.logger.info,
        log_debug=host.logger.debug,
        module_registry=host._get_kia_module_registry(),
    ))


def service_dispute_scan(host: Any) -> Dict[str, Any]:
    """Scan and route eligible knowledge disputes."""

    return _dict_result(host._kia_services.run_dispute_scan(
        host._log_service_error,
        log_info=host.logger.info,
    ))


def service_link_probe(host: Any, cfg: Any = None) -> Dict[str, Any]:
    """Probe pending external links through the daemon service boundary."""

    return _dict_result(host._link_probe.run_service(
        cfg,
        log_service_error=host._log_service_error,
        log_info=host.logger.info,
    ))


def service_db_maintenance(host: Any, cfg: Any = None) -> Dict[str, Any]:
    """Run one configured database-maintenance cycle."""

    if host._db_maintenance_task is None:
        host._db_maintenance_task = host._maintenance.DatabaseMaintenanceTask(config=cfg)
    return _dict_result(host._db_maintenance_task.run())


def run_startup_compensation(host: Any) -> Dict[str, Any]:
    """Replay daemon startup compensation work."""

    return _dict_result(host._maintenance.run_startup_compensation(host._log_service_error))


def run_startup_cleanup(host: Any) -> Dict[str, Any]:
    """Run bounded daemon startup cleanup."""

    return _dict_result(host._maintenance.run_startup_cleanup(host._log_service_error))


def print_model_status(host: Any, daemon_pid: int) -> str:
    """Format the operator-facing model and daemon status."""

    return cast(str, host._maintenance.format_model_status(
        count_daemon_processes=host._count_daemon_processes,
        daemon_pid=daemon_pid,
    ))


def generate_drift_report(host: Any) -> Dict[str, Any]:
    """Generate the current configuration and runtime drift report."""

    return _dict_result(host._maintenance.generate_drift_report(
        host._log_service_error,
        log_info=host.logger.info,
    ))


def run_preflight_checks(host: Any) -> Dict[str, Any]:
    """Run daemon startup preflight checks."""

    return _dict_result(host._maintenance.run_preflight_checks())


def on_session_end(host: Any, event: Any) -> None:
    """Handle one canonical session-end event."""

    host._event_handlers.on_session_end(
        event,
        get_reflection_engine=host._get_reflection_engine,
        event_bus_provider=lambda: host._event_bus_instance,
        log_service_error=host._log_service_error,
        log_info=host.logger.info,
        log_debug=host.logger.debug,
    )


def on_observation_updated(host: Any, event: Any) -> None:
    """Handle one committed Observation update event."""

    host._event_handlers.on_observation_updated(
        event,
        get_reflection_engine=host._get_reflection_engine,
        event_bus_provider=lambda: host._event_bus_instance,
        log_service_error=host._log_service_error,
        log_info=host.logger.info,
    )


def on_knowledge_stale(host: Any, event: Any) -> None:
    """Handle one canonical stale-knowledge event."""

    host._event_handlers.on_knowledge_stale(
        event,
        log_service_error=host._log_service_error,
        log_info=host.logger.info,
        log_debug=host.logger.debug,
    )


def setup_logging(host: Any) -> None:
    """Configure daemon file rotation and foreground stderr logging."""
    host.DAEMON_LOG.parent.mkdir(parents=True, exist_ok=True)
    root_logger = host.logging.getLogger()
    root_logger.setLevel(host.logging.INFO)
    formatter = host.logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    file_handler = RotatingFileHandler(
        str(host.DAEMON_LOG),
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    console_handler = host.logging.StreamHandler(host.sys.stderr)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)


def register_telemetry_handlers(host: Any, event_bus: Any) -> None:
    """Register the telemetry audit sink."""

    def event_audit_sink(event: Any) -> Dict[str, Any]:
        host.logger.debug(
            "[DAEMON] EventBus telemetry consumed: %s trace_id=%s",
            event.event_type,
            event.trace_id,
        )
        return {"status": "observed", "event_type": event.event_type}

    for event_type in (
        "distillation_progress",
        "feedback_loop",
        "guard_alert",
        "polled",
    ):
        event_bus.subscribe(event_type, event_audit_sink)


def register_session_event_handlers(host: Any, event_bus: Any) -> None:
    """Register session and cognition event handlers on the shared bus."""

    def on_session_start(event: Any) -> Dict[str, Any]:
        host.logger.debug(
            "[DAEMON] session.start consumed: trace_id=%s",
            event.trace_id,
        )
        return {"status": "observed", "event_type": event.event_type}

    event_bus.subscribe("session.end", host._on_session_end)
    event_bus.subscribe("session.start", on_session_start)
    event_bus.subscribe("observation.updated", host._on_observation_updated)
    event_bus.subscribe("knowledge_stale", host._on_knowledge_stale)
    host.logger.info("[DAEMON] observation.updated → Reflection 已订阅 EventBus")
    host.logger.info("[DAEMON] knowledge_stale → FreshnessRefresh 已订阅 EventBus")


def run_daemon_main_loop(
    host: Any,
    cfg: Optional[Any],
    executor: Any,
    *,
    service_names: tuple[str, ...] | None = None,
) -> None:
    """Schedule periodic services until the runtime context stops."""
    active_intervals = {
        name: host.INTERVALS[name]
        for name in (service_names or host._active_service_names())
        if name in host.INTERVALS
    }
    last_run: Dict[str, float] = {}
    for name, interval in active_intervals.items():
        jitter = random.uniform(0, interval)  # nosec B311
        last_run[name] = time.time() - interval + jitter

    while not host.stop_event.is_set():
        now = time.time()
        for service_name, interval in active_intervals.items():
            host._schedule_service_if_due(
                cfg,
                service_name,
                interval,
                now,
                last_run,
                executor,
            )
        host.stop_event.wait(timeout=10)


def run_daemon(
    host: Any,
    foreground: bool = False,
    *,
    controlled_raw_sync_only: bool = False,
) -> None:
    """Run the production daemon or the constrained Raw-sync profile."""
    from core.utils import set_sensitive_umask

    set_sensitive_umask()
    cfg = host._load_daemon_config()
    if cfg is None:
        raise RuntimeError("daemon configuration is invalid")
    host._configure_runtime_paths(cfg)
    host._setup_logging()
    host.logger.info("Mnemos Daemon 启动")
    host._activate_daemon_profile(
        controlled_raw_sync_only=controlled_raw_sync_only
    )

    if not foreground:
        if host.platform.system() == "Windows":
            host._daemonize_windows()
        else:
            host._daemonize_unix()

    if not host._acquire_pid_lock(cfg):
        host._reset_daemon_profile()
        host._write_startup_status(
            success=False,
            error="daemon identity lock unavailable",
        )
        raise RuntimeError("daemon already running or process identity is unverifiable")

    ctx = host._runtime.RuntimeContext()
    host.stop_event = ctx.stop_event
    host._runtime_context = ctx
    ctx.install_signal_handlers()

    if controlled_raw_sync_only:
        host._apply_interval_overrides(cfg)
        host.service_heartbeat()
        host._write_startup_status(success=True)
    else:
        host._start_file_guardian()
        host._ensure_vault_directories()
        host._bootstrap_runtime_schema()
        host._bootstrap_runtime_flow_ledger(cfg)
        host._run_startup_compensation()
        host._run_startup_cleanup()
        host._event_bus_instance = host._initialize_event_bus(
            cfg,
            start_dispatch=False,
        )
        if host._event_bus_instance is not None:
            ctx.register(
                "event_bus",
                host._event_bus_instance,
                closer=lambda bus: bus.close(),
            )
        host._register_wiki_auto_commit(ctx, cfg)
        host._apply_interval_overrides(cfg)
        host._register_kia_modules(ctx, cfg)
        if host._event_bus_instance is not None:
            host._start_event_bus_dispatch(host._event_bus_instance)
        host._register_trigger_dispatcher(ctx, cfg)
        if host._event_bus_instance is not None:
            host.service_heartbeat()
            host._write_startup_status(success=True)

    service_executor = host._build_service_executor(cfg)
    if controlled_raw_sync_only:
        host._run_daemon_main_loop(
            cfg,
            service_executor,
            service_names=host._active_service_names(),
        )
    else:
        host._run_daemon_main_loop(cfg, service_executor)
    service_executor.shutdown(wait=False)
    host._shutdown_daemon(ctx)


def daemon_command_context(host: Any) -> DaemonCommandContext:
    """Bind OS command orchestration to the current daemon facade state."""
    context = host._command_control.DaemonCommandContext(
        pid_file=host.PID_FILE,
        project_root=host._PROJECT_ROOT,
        startup_timeout=host.STARTUP_STATUS_TIMEOUT_SECONDS,
        instance_control=host._instance_control,
        service_names_for_profile=lambda controlled: host._service_names_for_profile(
            controlled_raw_sync_only=controlled
        ),
        clear_startup_status=host._clear_startup_status,
        read_startup_status=host._read_startup_status,
        write_startup_status=lambda success, error: host._write_startup_status(
            success=success,
            error=error,
        ),
        run_daemon=host.run_daemon,
        windows_executable=host._windows_executable,
        print_model_status=host._print_model_status,
        platform_system=host.platform.system,
        script_path=Path(host.__file__).resolve(),
        os_module=host.os,
        sys_module=host.sys,
        subprocess_module=host.subprocess,
        emit=print,
        log=host.logger,
    )
    if not isinstance(context, DaemonCommandContext):
        raise TypeError("daemon command context factory returned an invalid contract")
    return context


def main(host: Any, argv: Optional[List[str]] = None) -> int:
    """Parse daemon CLI arguments and dispatch the selected command."""
    parser = host.argparse.ArgumentParser(description="Mnemos 后台守护进程")
    parser.add_argument(
        "--controlled-raw-sync-only",
        action="store_true",
        help=(
            "仅启动 heartbeat 与 raw_sync 的受控恢复档；"
            "start/status/stop 必须使用同一选项以校验实例身份"
        ),
    )
    parser.add_argument(
        "command",
        choices=[
            "start",
            "stop",
            "status",
            "run",
            "install-windows",
            "uninstall-windows",
        ],
        help="守护进程命令",
    )
    args = parser.parse_args(argv)

    if args.controlled_raw_sync_only and args.command not in {
        "start",
        "stop",
        "status",
        "run",
    }:
        parser.error("--controlled-raw-sync-only 仅适用于 start/stop/status/run")

    host._configure_runtime_paths()
    if args.command == "start":
        return cast(int, host.cmd_start(
            controlled_raw_sync_only=args.controlled_raw_sync_only
        ))
    if args.command == "stop":
        return cast(int, host.cmd_stop(
            controlled_raw_sync_only=args.controlled_raw_sync_only
        ))
    if args.command == "status":
        return cast(int, host.cmd_status(
            controlled_raw_sync_only=args.controlled_raw_sync_only
        ))
    if args.command == "run":
        return cast(int, host.cmd_run(
            controlled_raw_sync_only=args.controlled_raw_sync_only
        ))
    if args.command == "install-windows":
        return cast(int, host.cmd_install_windows())
    return cast(int, host.cmd_uninstall_windows())
