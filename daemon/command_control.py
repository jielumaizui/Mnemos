"""OS-facing daemon command orchestration, isolated from runtime services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


_WINDOWS_TASK_ENVIRONMENT = (
    "MNEMOS_DIR",
    "MNEMOS_LLM_API_KEY",
    "MNEMOS_LLM_BASE_URL",
    "MNEMOS_LLM_MODEL",
    "MNEMOS_EMBEDDING_API_KEY",
    "MNEMOS_EMBEDDING_BASE_URL",
    "MNEMOS_EMBEDDING_MODEL",
    "MNEMOS_RERANKER_API_KEY",
    "MNEMOS_RERANKER_BASE_URL",
    "MNEMOS_RERANKER_MODEL",
    "SILICONFLOW_API_KEY",
    "SILICONFLOW_BASE_URL",
    "SILICONFLOW_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "DMXAPI_API_KEY",
    "DMX_API_KEY",
    "DMXAPI_BASE_URL",
    "DMXAPI_MODEL",
)


@dataclass(frozen=True)
class DaemonCommandContext:
    """Explicit dependencies for command operations that control an OS process."""

    pid_file: Path
    project_root: Path
    startup_timeout: float
    instance_control: Any
    service_names_for_profile: Callable[[bool], tuple[str, ...]]
    clear_startup_status: Callable[[], None]
    read_startup_status: Callable[[float], tuple[bool, int | None, str]]
    write_startup_status: Callable[[bool, str], None]
    run_daemon: Callable[..., None]
    windows_executable: Callable[[str], str]
    print_model_status: Callable[[int], str]
    platform_system: Callable[[], str]
    script_path: Path
    os_module: Any
    sys_module: Any
    subprocess_module: Any
    emit: Callable[[str], None]
    log: Any


def daemonize_unix(os_module: Any, sys_module: Any) -> None:
    """Detach the current process with the conventional Unix double fork."""
    pid = os_module.fork()
    if pid > 0:
        sys_module.exit(0)
    os_module.setsid()
    pid = os_module.fork()
    if pid > 0:
        sys_module.exit(0)
    sys_module.stdout.flush()
    sys_module.stderr.flush()
    devnull = os_module.open(os_module.devnull, os_module.O_RDWR)
    os_module.dup2(devnull, sys_module.stdin.fileno())
    os_module.dup2(devnull, sys_module.stdout.fileno())
    os_module.dup2(devnull, sys_module.stderr.fileno())
    if devnull > 2:
        os_module.close(devnull)


def daemonize_windows() -> None:
    """Windows command startup is already detached by ``start``."""


def start(context: DaemonCommandContext, *, controlled_raw_sync_only: bool = False) -> int:
    """Prepare and launch a daemon instance using the selected service profile."""
    service_names = context.service_names_for_profile(controlled_raw_sync_only)
    decision = context.instance_control.prepare_start(
        context.pid_file,
        database_dir=context.pid_file.parent,
        service_names=service_names,
        project_root=context.project_root,
        log=context.log,
    )
    for message in decision.messages:
        context.emit(message)
    if not decision.proceed:
        return int(decision.exit_code)

    context.clear_startup_status()
    if context.platform_system() == "Windows":
        command = [context.sys_module.executable, str(context.script_path)]
        if controlled_raw_sync_only:
            command.append("--controlled-raw-sync-only")
        command.append("start")
        context.subprocess_module.Popen(
            command,
            creationflags=(
                context.subprocess_module.CREATE_NEW_PROCESS_GROUP
                | context.subprocess_module.DETACHED_PROCESS
            ),
            stdout=context.subprocess_module.DEVNULL,
            stderr=context.subprocess_module.DEVNULL,
        )
        success, child_pid, error = context.read_startup_status(context.startup_timeout)
        if success and child_pid:
            context.emit(f"Daemon 已启动 (PID {child_pid})")
            return 0
        context.emit(f"Daemon 启动失败: {error or '未知错误'}")
        return 1

    pid = context.os_module.fork()
    if pid == 0:
        try:
            context.run_daemon(
                foreground=False,
                controlled_raw_sync_only=controlled_raw_sync_only,
            )
        except (
            AttributeError,
            ImportError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            context.write_startup_status(False, str(exc))
            context.log.error("Daemon 启动失败: %s", exc, exc_info=True)
            context.sys_module.exit(1)
        return 1

    success, child_pid, error = context.read_startup_status(context.startup_timeout)
    if success and child_pid:
        context.emit(f"Daemon 已启动 (PID {child_pid})")
        return 0
    context.emit(f"Daemon 启动失败: {error or 'fork 后子进程未响应'}")
    return 1


def stop(context: DaemonCommandContext, *, controlled_raw_sync_only: bool = False) -> int:
    """Stop exactly the instance that owns the selected service manifest."""
    result = context.instance_control.stop(
        context.pid_file,
        database_dir=context.pid_file.parent,
        service_names=context.service_names_for_profile(controlled_raw_sync_only),
        project_root=context.project_root,
        windows_executable=context.windows_executable,
        log=context.log,
    )
    for message in result.messages:
        context.emit(message)
    return int(result.exit_code)


def status(context: DaemonCommandContext, *, controlled_raw_sync_only: bool = False) -> int:
    """Report process identity and model state for the selected profile."""
    result = context.instance_control.status(
        context.pid_file,
        database_dir=context.pid_file.parent,
        service_names=context.service_names_for_profile(controlled_raw_sync_only),
        project_root=context.project_root,
        log=context.log,
    )
    for message in result.messages:
        context.emit(message)
    if result.show_model_status and result.daemon_pid is not None:
        context.emit(context.print_model_status(result.daemon_pid))
    return int(result.exit_code)


def run(context: DaemonCommandContext, *, controlled_raw_sync_only: bool = False) -> int:
    """Run the daemon in the foreground for a scheduler or diagnostic shell."""
    context.run_daemon(
        foreground=True,
        controlled_raw_sync_only=controlled_raw_sync_only,
    )
    return 0


def windows_task_command(context: DaemonCommandContext, script: Path) -> str:
    """Build a PowerShell command that carries only approved runtime settings."""
    def ps_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    env_parts = [
        f"$env:{name}={ps_quote(value)};"
        for name in _WINDOWS_TASK_ENVIRONMENT
        if (value := context.os_module.environ.get(name, ""))
    ]
    command = " ".join(
        [
            *env_parts,
            f"& {ps_quote(context.sys_module.executable)} {ps_quote(str(script))} start",
        ]
    )
    return "powershell.exe -NoProfile -ExecutionPolicy Bypass -Command " + str(
        context.subprocess_module.list2cmdline([command])
    )


def install_windows(context: DaemonCommandContext) -> int:
    """Install the five-minute Windows scheduled task for this daemon entrypoint."""
    if context.platform_system() != "Windows":
        context.emit("此命令仅适用于 Windows")
        return 1

    task_name = "MnemosDaemon"
    command_line = windows_task_command(context, context.script_path)
    try:
        schtasks = context.windows_executable("schtasks.exe")
        context.subprocess_module.run(
            [schtasks, "/Delete", "/TN", task_name, "/F"],
            capture_output=True,
            timeout=10,
        )
        context.subprocess_module.run(
            [
                schtasks,
                "/Create",
                "/SC",
                "MINUTE",
                "/MO",
                "5",
                "/TN",
                task_name,
                "/TR",
                command_line,
                "/RL",
                "HIGHEST",
                "/F",
            ],
            capture_output=True,
            timeout=10,
            check=True,
        )
        context.emit(f"Windows 计划任务已创建: {task_name}")
        return 0
    except context.subprocess_module.CalledProcessError as exc:
        context.emit(f"创建计划任务失败: {exc.stderr.decode() if exc.stderr else exc}")
        return 1


def uninstall_windows(context: DaemonCommandContext) -> int:
    """Remove the Mnemos Windows scheduled task if it is installed."""
    if context.platform_system() != "Windows":
        context.emit("此命令仅适用于 Windows")
        return 1

    try:
        context.subprocess_module.run(
            [
                context.windows_executable("schtasks.exe"),
                "/Delete",
                "/TN",
                "MnemosDaemon",
                "/F",
            ],
            capture_output=True,
            timeout=10,
            check=True,
        )
        context.emit("Windows 计划任务已删除")
        return 0
    except context.subprocess_module.CalledProcessError as exc:
        context.emit(f"删除计划任务失败: {exc.stderr.decode() if exc.stderr else exc}")
        return 1
