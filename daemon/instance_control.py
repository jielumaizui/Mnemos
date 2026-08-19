# -*- coding: utf-8 -*-
"""High-level daemon start/status/stop control over the instance identity contract."""

from __future__ import annotations

import logging
import os
import platform
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from daemon import instance_identity, process_control

logger = logging.getLogger("mnemos.daemon")


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    messages: tuple[str, ...]
    proceed: bool = False
    show_model_status: bool = False
    daemon_pid: int | None = None


def acquire_instance_lock(
    pid_file: Path,
    *,
    database_dir: Path,
    service_names: Iterable[str],
    project_root: Path,
    config_fingerprint: str | None = None,
    log: logging.Logger | None = None,
) -> dict[str, Any] | None:
    """Create a complete identity and persist it under the process lock."""
    log = log or logger
    try:
        record = instance_identity.create_instance_record(
            database_dir=database_dir,
            service_names=service_names,
            project_root=project_root,
            config_fingerprint=config_fingerprint,
        )
    except RuntimeError as exc:
        log.error("无法证明 daemon 进程身份: %s", exc)
        return None
    if not process_control.acquire_pid_lock(
        pid_file,
        instance_record=record,
        log=log,
    ):
        return None
    return record


def _verify(
    record: Mapping[str, Any],
    *,
    database_dir: Path,
    service_names: Iterable[str],
    project_root: Path,
    require_current_context: bool,
) -> instance_identity.VerificationResult:
    return instance_identity.verify_instance_record(
        record,
        database_dir=database_dir,
        service_names=service_names,
        project_root=project_root,
        require_current_context=require_current_context,
    )


def prepare_start(
    pid_file: Path,
    *,
    database_dir: Path,
    service_names: Iterable[str],
    project_root: Path,
    log: logging.Logger | None = None,
) -> CommandResult:
    """Decide whether start may proceed without trusting a reusable integer PID."""
    log = log or logger
    existing = process_control.read_pid_record(pid_file, log=log)
    if not existing:
        return CommandResult(0, (), proceed=True)
    pid = existing.get("pid")
    if existing.get("schema_version") == instance_identity.SCHEMA_VERSION:
        process_check = _verify(
            existing,
            database_dir=database_dir,
            service_names=service_names,
            project_root=project_root,
            require_current_context=False,
        )
        if process_check.ok:
            current_check = _verify(
                existing,
                database_dir=database_dir,
                service_names=service_names,
                project_root=project_root,
                require_current_context=True,
            )
            if current_check.ok:
                return CommandResult(
                    0,
                    (
                        f"Daemon 已在运行 (PID {pid}, instance "
                        f"{existing.get('instance_id')})",
                    ),
                )
            return CommandResult(
                1,
                (
                    "Daemon 正在运行，但 build/config/database 上下文已漂移 "
                    f"({current_check.reason})；请先执行受控 stop 再 start",
                ),
            )
    if isinstance(pid, int) and process_control.pid_exists(pid):
        return CommandResult(
            1,
            (
                "Daemon PID 文件存在但实例身份不可验证；为避免 PID 复用误判，"
                "不会启动第二实例或自动发信号",
            ),
        )
    process_control.clear_pid_record(pid_file, expected_record=existing, log=log)
    return CommandResult(0, (), proceed=True)


def _windows_sender(
    executable_resolver: Callable[[str], str],
) -> Callable[[int, int], None]:
    def send(pid: int, _sig: int) -> None:
        taskkill = executable_resolver("taskkill.exe")
        result = subprocess.run(
            [taskkill, "/PID", str(pid), "/F"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise OSError("taskkill failed")

    return send


def stop(
    pid_file: Path,
    *,
    database_dir: Path,
    service_names: Iterable[str],
    project_root: Path,
    windows_executable: Callable[[str], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    log: logging.Logger | None = None,
) -> CommandResult:
    """Stop only the OS process that still matches the persisted instance identity."""
    log = log or logger
    original = process_control.read_pid_record(pid_file, log=log)
    if not original:
        return CommandResult(0, ("Daemon 未运行",))
    record: Mapping[str, Any] = original
    pid = record.get("pid")
    if not isinstance(pid, int):
        return CommandResult(1, ("停止 daemon 失败: PID 文件缺少有效 pid；未发送任何信号",))

    messages: list[str] = []
    if record.get("schema_version") != instance_identity.SCHEMA_VERSION:
        migrated = instance_identity.migrate_historical_pid_for_control(
            record,
            database_dir=database_dir,
            service_names=service_names,
            project_root=project_root,
        )
        if migrated is not None:
            record = migrated
            messages.append("旧 PID 文件已通过 OS 进程事实完成一次性安全迁移")
        elif not process_control.pid_exists(pid):
            process_control.clear_pid_record(pid_file, expected_record=original, log=log)
            return CommandResult(0, ("Daemon 未运行（旧 PID 文件已安全清理）",))
        else:
            return CommandResult(
                1,
                ("停止 daemon 失败: 旧 PID 文件没有可证明的 daemon identity；未发送任何信号",),
            )

    initial = _verify(
        record,
        database_dir=database_dir,
        service_names=service_names,
        project_root=project_root,
        require_current_context=False,
    )
    if not initial.ok:
        if not process_control.pid_exists(pid):
            process_control.clear_pid_record(pid_file, expected_record=original, log=log)
            return CommandResult(0, ("Daemon 未运行（残留 instance record 已清理）",))
        return CommandResult(
            1,
            (f"停止 daemon 失败: 实例身份不可验证 ({initial.reason})；未发送任何信号",),
        )

    if platform.system() == "Windows":
        if windows_executable is None:
            return CommandResult(1, ("停止 daemon 失败: Windows taskkill resolver unavailable",))
        sender = _windows_sender(windows_executable)
    else:
        sender = os.kill

    def send(sig: int) -> instance_identity.VerificationResult:
        return instance_identity.signal_verified_instance(
            record,
            sig,
            database_dir=database_dir,
            service_names=service_names,
            project_root=project_root,
            signal_sender=sender,
        )

    sent = send(signal.SIGTERM)
    if not sent.ok:
        return CommandResult(
            1,
            (f"停止 daemon 失败: {sent.reason}；未向未验证进程发送信号",),
        )

    stopped = False
    for _ in range(30):
        check = _verify(
            record,
            database_dir=database_dir,
            service_names=service_names,
            project_root=project_root,
            require_current_context=False,
        )
        if not check.ok:
            if (
                check.reason == "process_not_found_or_unverifiable"
                and process_control.pid_exists(pid)
            ):
                return CommandResult(
                    1,
                    (
                        "停止 daemon 未确认完成: 原 PID 仍存在但身份暂不可验证；"
                        "保留 instance record 且不发送 SIGKILL",
                    ),
                )
            stopped = True
            break
        sleep(0.5)
    if not stopped:
        log.warning("PID %s 未响应 SIGTERM，验证身份后发送 SIGKILL", pid)
        killed = send(signal.SIGKILL)
        if not killed.ok:
            return CommandResult(
                1,
                (f"停止 daemon 失败: SIGKILL 前身份校验失败 ({killed.reason})",),
            )

    process_control.clear_pid_record(pid_file, expected_record=original, log=log)
    messages.append(f"Daemon 已停止 (PID {pid})")
    return CommandResult(0, tuple(messages))


def status(
    pid_file: Path,
    *,
    database_dir: Path,
    service_names: Iterable[str],
    project_root: Path,
    log: logging.Logger | None = None,
) -> CommandResult:
    """Report verified identity and context drift without mutating runtime state."""
    log = log or logger
    record = process_control.read_pid_record(pid_file, log=log)
    if not record:
        return CommandResult(0, ("Daemon 未运行",))
    if record.get("schema_version") != instance_identity.SCHEMA_VERSION:
        return CommandResult(1, ("Daemon 状态不可验证（legacy/incomplete PID record）",))
    check = _verify(
        record,
        database_dir=database_dir,
        service_names=service_names,
        project_root=project_root,
        require_current_context=True,
    )
    if check.ok:
        current_commit = (check.details or {}).get("current_commit")
        commit_match = bool((check.details or {}).get("commit_match"))
        compatibility = ""
        if not commit_match:
            compatibility = (
                f", current_commit {str(current_commit)[:12]}, build_compatible=true"
            )
        messages = (
            f"Daemon 运行中 (PID {check.pid}, instance {check.instance_id}, "
            f"identity_match=true, commit {str(record.get('commit'))[:12]}"
            f"{compatibility})",
            "config_hash=" + str(record.get("config_hash"))
            + " config_fingerprint=" + str(record.get("config_fingerprint"))
            + " database_identity=" + str(record.get("database_identity"))
            + " service_manifest_hash=" + str(record.get("service_manifest_hash")),
        )
        return CommandResult(0, messages, show_model_status=True, daemon_pid=check.pid)
    process_check = _verify(
        record,
        database_dir=database_dir,
        service_names=service_names,
        project_root=project_root,
        require_current_context=False,
    )
    if process_check.ok:
        return CommandResult(
            1,
            (
                f"Daemon 运行但上下文不匹配 (PID {process_check.pid}, "
                f"instance {process_check.instance_id}, reason={check.reason})",
            ),
        )
    return CommandResult(1, (f"Daemon 状态不可验证 ({process_check.reason})",))
