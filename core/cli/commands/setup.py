"""Setup, upgrade, and uninstall CLI commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _emit(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _auto_setup_argv(args: Any) -> list[str]:
    flag_map = (
        ("yes", "--yes"),
        ("skip_backend", "--skip-backend"),
        ("skip_daemon", "--skip-daemon"),
        ("skip_scheduler", "--skip-scheduler"),
        ("skip_hooks", "--skip-hooks"),
        ("skip_verify", "--skip-verify"),
        ("skip_backfill", "--skip-backfill"),
        ("skip_e2e", "--skip-e2e"),
        ("preserve_config", "--preserve-config"),
        ("json", "--json"),
    )
    argv = [flag for attr, flag in flag_map if bool(getattr(args, attr, False))]
    max_smoke_attempts = getattr(args, "max_smoke_attempts", None)
    if max_smoke_attempts is not None:
        argv.extend(["--max-smoke-attempts", str(max_smoke_attempts)])
    return argv


def _auto_setup_namespace(args: Any) -> argparse.Namespace:
    return argparse.Namespace(
        yes=bool(getattr(args, "yes", False)),
        dry_run=False,
        skip_backend=bool(getattr(args, "skip_backend", False)),
        skip_daemon=bool(getattr(args, "skip_daemon", False)),
        skip_scheduler=bool(getattr(args, "skip_scheduler", False)),
        skip_hooks=bool(getattr(args, "skip_hooks", False)),
        skip_verify=bool(getattr(args, "skip_verify", False)),
        skip_backfill=bool(getattr(args, "skip_backfill", False)),
        skip_e2e=bool(getattr(args, "skip_e2e", False)),
        preserve_config=bool(getattr(args, "preserve_config", False)),
        max_smoke_attempts=int(getattr(args, "max_smoke_attempts", 3)),
        venv_reexec=bool(getattr(args, "venv_reexec", False)),
        reexec_args=["setup", *_auto_setup_argv(args)],
        reexec_entrypoint=str(Path(__file__).resolve().parents[3] / "mnemos_cli.py"),
    )


def cmd_setup(args) -> int:
    from core.config import get_config
    from core.setup.install_lifecycle import InstallLifecycleManager

    manager = InstallLifecycleManager(get_config())
    state = manager.run_setup(
        dry_run=bool(getattr(args, "dry_run", False)),
        auto_setup_args=_auto_setup_namespace(args),
    )
    payload = state.as_dict()
    payload["ok"] = state.status in {"configuring", "installed_partial", "installed_ready"}
    _emit(payload, json_output=bool(getattr(args, "json", False)))
    return 0 if payload["ok"] else 1


def cmd_upgrade(args) -> int:
    from core.config import get_config
    from core.setup.install_lifecycle import InstallLifecycleManager

    manager = InstallLifecycleManager(get_config())
    cmd = getattr(args, "upgrade_cmd", "") or "plan"
    if cmd == "plan":
        state = manager.upgrade_plan()
    elif cmd == "apply":
        state = manager.upgrade_apply(
            execute_wrapped=bool(getattr(args, "execute_wrapped", False))
        )
    else:
        state = manager.upgrade_plan()
    payload = state.as_dict()
    payload["ok"] = state.status in {
        "installed_ready",
        "upgrade_available",
        "rollback_available",
    }
    _emit(payload, json_output=bool(getattr(args, "json", False)))
    return 0 if payload["ok"] else 1


def cmd_uninstall(args) -> int:
    from core.config import get_config
    from core.setup.install_lifecycle import InstallLifecycleManager

    purge_data = bool(getattr(args, "purge_data", False))
    preserve_data = bool(getattr(args, "preserve_data", False)) or not purge_data
    manager = InstallLifecycleManager(get_config())
    state = manager.uninstall(
        preserve_data=preserve_data,
        purge_data=purge_data,
        confirm=bool(getattr(args, "confirm", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    payload = state.as_dict()
    payload["ok"] = state.status in {"uninstalled_preserve_data", "uninstalled_purged"}
    _emit(payload, json_output=bool(getattr(args, "json", False)))
    return 0 if payload["ok"] else 1


def cmd_repair_all(args) -> int:
    from core.config import get_config
    from core.setup.install_lifecycle import InstallLifecycleManager

    manager = InstallLifecycleManager(get_config())
    state = manager.repair_all(dry_run=bool(getattr(args, "dry_run", False)))
    payload = state.as_dict()
    payload["ok"] = state.status in {"installed_ready", "installed_partial"}
    _emit(payload, json_output=bool(getattr(args, "json", False)))
    return 0 if payload["ok"] else 1
