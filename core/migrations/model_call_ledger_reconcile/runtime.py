"""Runtime gates shared by reconciliation and sealed recovery."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import re
from typing import Any, Sequence

psutil: Any | None
try:
    psutil = importlib.import_module("psutil")
except ModuleNotFoundError:
    psutil = None

_UNINSPECTABLE_PROCESS_ATTRIBUTE = object()


def is_mnemos_runtime_process(*, name: str, cmdline: Sequence[str]) -> bool:
    """Return whether one OS process is a live Mnemos writer/runtime."""

    def basename(value: str) -> str:
        return re.split(r"[\\/]", value.lower())[-1]

    process_name = basename(name)
    arguments = tuple(str(item).lower() for item in cmdline)
    basenames = tuple(basename(item) for item in arguments)
    if process_name == "mnemos_daemon.py" or "mnemos_daemon.py" in basenames:
        return True
    if any("mnemos" in item and "mcp" in item for item in (process_name, *basenames)):
        return True
    return bool(
        "mcp" in arguments
        and "serve" in arguments
        and any(item in {"mnemos", "mnemos_cli.py"} for item in basenames)
    )


def _could_host_mnemos_runtime_without_cmdline(name: str) -> bool:
    """Return whether an uninspectable executable could be a Mnemos runtime."""

    process_name = re.split(r"[\\/]", str(name).lower())[-1]
    return bool(
        process_name.startswith("python")
        or process_name in {"node", "nodejs", "uv", "uvx"}
        or "mnemos" in process_name
    )


def mnemos_runtime_is_active() -> bool:
    """Detect daemon or MCP processes, failing closed on enumeration errors."""

    if psutil is None:
        return True
    current_pid = os.getpid()
    try:
        current_user = psutil.Process(current_pid).username()
    except (psutil.AccessDenied, psutil.Error):
        return True
    if not isinstance(current_user, str) or not current_user.strip():
        return True
    try:
        processes = psutil.process_iter(
            ("pid", "username", "name", "cmdline"),
            ad_value=_UNINSPECTABLE_PROCESS_ATTRIBUTE,
        )
        for process in processes:
            try:
                info = process.info
                raw_pid = info.get("pid", _UNINSPECTABLE_PROCESS_ATTRIBUTE)
                raw_username = info.get(
                    "username",
                    _UNINSPECTABLE_PROCESS_ATTRIBUTE,
                )
                if any(
                    value is _UNINSPECTABLE_PROCESS_ATTRIBUTE
                    for value in (raw_pid, raw_username)
                ):
                    return True
                if not isinstance(raw_pid, int) or isinstance(raw_pid, bool):
                    return True
                if raw_pid == current_pid:
                    continue
                if not isinstance(raw_username, str) or not raw_username.strip():
                    return True
                if raw_username != current_user:
                    continue
                raw_name = info.get("name", _UNINSPECTABLE_PROCESS_ATTRIBUTE)
                raw_cmdline = info.get(
                    "cmdline",
                    _UNINSPECTABLE_PROCESS_ATTRIBUTE,
                )
                if any(
                    value is _UNINSPECTABLE_PROCESS_ATTRIBUTE
                    for value in (raw_name, raw_cmdline)
                ):
                    try:
                        if process.status() == psutil.STATUS_ZOMBIE:
                            continue
                    except (psutil.NoSuchProcess, psutil.ZombieProcess):
                        continue
                    except (psutil.AccessDenied, psutil.Error):
                        return True
                    return True
                if bool(info.get("inspection_incomplete")):
                    return True
                if not isinstance(raw_name, str) or not raw_name.strip():
                    return True
                process_name = raw_name
                if raw_cmdline is None:
                    if (
                        not process_name.strip()
                        or _could_host_mnemos_runtime_without_cmdline(process_name)
                    ):
                        return True
                    continue
                if not isinstance(raw_cmdline, (list, tuple)) or not all(
                    isinstance(item, str) for item in raw_cmdline
                ):
                    return True
                normalized_cmdline = tuple(item for item in raw_cmdline if item)
                if not normalized_cmdline:
                    if (
                        not process_name.strip()
                        or _could_host_mnemos_runtime_without_cmdline(process_name)
                    ):
                        return True
                    continue
                if is_mnemos_runtime_process(
                    name=process_name,
                    cmdline=normalized_cmdline,
                ):
                    return True
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.AccessDenied:
                return True
    except (psutil.AccessDenied, psutil.Error):
        return True
    return False


def runtime_writers_are_inactive(database_dir: Path) -> bool:
    """Return whether every Mnemos runtime writer is conclusively stopped.

    Planning never calls this gate.  Apply and restore call it immediately
    before they acquire the shared local migration lock. The gate covers both
    daemon and MCP serve processes because either can hold live database state.
    """
    from daemon import instance_control
    from daemon.intervals import build_default_intervals

    decision = instance_control.status(
        database_dir / "daemon.pid",
        database_dir=database_dir,
        service_names=build_default_intervals(),
        project_root=Path(__file__).resolve().parents[3],
    )
    daemon_stopped = bool(
        decision.exit_code == 0
        and decision.daemon_pid is None
        and any("未运行" in message for message in decision.messages)
    )
    return daemon_stopped and not mnemos_runtime_is_active()
