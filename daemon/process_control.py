# -*- coding: utf-8 -*-
"""Process-control helpers for the Mnemos daemon."""

from __future__ import annotations

import csv
import json
import logging
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from daemon import instance_identity

logger = logging.getLogger("mnemos.daemon")


def _pid_payload(instance_record: Mapping[str, Any]) -> bytes:
    record = dict(instance_record)
    if (
        record.get("schema_version") != instance_identity.SCHEMA_VERSION
        or not record.get("instance_id")
    ):
        raise ValueError("PID record requires a complete daemon instance identity")
    if record.get("pid") != os.getpid():
        raise ValueError("PID record must describe the current process")
    return (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def acquire_pid_lock(
    pid_file: Path,
    *,
    instance_record: Mapping[str, Any],
    log: logging.Logger | None = None,
) -> bool:
    """Try to atomically acquire the daemon PID file lock."""
    log = log or logger
    try:
        payload = _pid_payload(instance_record)
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        if platform.system() == "Windows":
            fd = os.open(str(pid_file), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                import msvcrt  # type: ignore[import]

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            except (IOError, OSError):
                os.close(fd)
                return False
            os.ftruncate(fd, 0)
            os.write(fd, payload)
            os.fsync(fd)
            os.chmod(pid_file, 0o600)
            acquire_pid_lock._fd = fd  # type: ignore[attr-defined]
            return True

        import fcntl

        fd = os.open(str(pid_file), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, payload)
        os.fsync(fd)
        os.chmod(pid_file, 0o600)
        acquire_pid_lock._fd = fd  # type: ignore[attr-defined]
        return True
    except (ImportError, OSError, RuntimeError, ValueError, TypeError) as exc:
        log.warning("PID 锁获取失败: %s", exc, exc_info=True)
        return False


def release_pid_lock(pid_file: Path, *, log: logging.Logger | None = None) -> None:
    """Release the daemon PID file lock and remove the PID file."""
    log = log or logger
    fd = getattr(acquire_pid_lock, "_fd", None)
    if fd is not None and fd > 0:
        try:
            if platform.system() == "Windows":
                import msvcrt  # type: ignore[import]

                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except (ImportError, OSError, RuntimeError) as exc:
            log.warning("PID 锁释放失败: %s", exc, exc_info=True)
        try:
            os.close(fd)
        except OSError as exc:
            log.warning("PID 文件描述符关闭失败: %s", exc, exc_info=True)
        acquire_pid_lock._fd = None  # type: ignore[attr-defined]

    try:
        pid_file.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("PID 文件删除失败: %s", exc, exc_info=True)


def read_pid_record(
    pid_file: Path,
    *,
    log: logging.Logger | None = None,
) -> dict[str, Any] | None:
    """Read a v1 identity record or classify a historical integer PID file."""
    log = log or logger
    try:
        if pid_file.exists():
            raw = pid_file.read_text(encoding="utf-8").strip()
            # Offline migrations lock the daemon PID inode to close the
            # stop-check/start race.  When no daemon identity exists yet the
            # locked file is intentionally empty; the OS lock, not empty
            # content, is the exclusion authority.
            if not raw:
                return None
            if raw.isdigit():
                return {
                    "schema_version": "mnemos.daemon_pid.legacy",
                    "pid": int(raw),
                    "identity_complete": False,
                }
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("PID record must be a JSON object")
            return value
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
        json.JSONDecodeError,
        subprocess.SubprocessError
    ):
        log.warning("读取 PID 文件失败", exc_info=True)
    return None


def read_pid(pid_file: Path, *, log: logging.Logger | None = None) -> Optional[int]:
    record = read_pid_record(pid_file, log=log)
    if not record:
        return None
    value = record.get("pid")
    return value if isinstance(value, int) else None


def clear_pid_record(
    pid_file: Path,
    *,
    expected_record: Mapping[str, Any] | None = None,
    log: logging.Logger | None = None,
) -> bool:
    """Remove a stale record only if it still names the expected instance."""
    log = log or logger
    current = read_pid_record(pid_file, log=log)
    if current is None:
        return True
    if expected_record is not None:
        identity = ("schema_version", "instance_id", "pid")
        if any(current.get(key) != expected_record.get(key) for key in identity):
            return False
    try:
        pid_file.unlink(missing_ok=True)
        return True
    except OSError:
        log.warning("清理 PID 记录失败", exc_info=True)
        return False


def pid_exists(pid: int) -> bool:
    """Return PID liveness only; this result is never daemon identity authority."""
    if pid <= 0:
        return False
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return False
            for row in csv.reader(result.stdout.splitlines()):
                if len(row) >= 2 and row[1].strip().isdigit() and int(row[1]) == pid:
                    return True
            return False

        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def count_daemon_processes(*, log: logging.Logger | None = None) -> int:
    """Count mnemos_daemon.py processes, excluding the current process."""
    log = log or logger
    my_pid = os.getpid()
    count = 0
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.splitlines()[1:]:
                if "mnemos_daemon" in line.lower() and "pytest" not in line.lower():
                    parts = line.strip('"').split('","')
                    if len(parts) >= 2:
                        try:
                            if int(parts[1]) != my_pid:
                                count += 1
                        except ValueError:
                            pass
        else:
            result = subprocess.run(
                ["pgrep", "-f", "mnemos_daemon.py"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        pid = int(line)
                        if pid != my_pid:
                            count += 1
                    except ValueError:
                        pass
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
        subprocess.SubprocessError
    ):
        log.warning("检测 daemon 进程失败", exc_info=True)
    return count


def write_startup_status(
    status_file: Path,
    success: bool,
    error: str = "",
    *,
    log: logging.Logger | None = None,
) -> None:
    log = log or logger
    try:
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text(
            f"{os.getpid()}\n{'ok' if success else 'fail'}\n{error}",
            encoding="utf-8",
        )
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
        subprocess.SubprocessError
    ):
        log.warning("写入启动状态文件失败", exc_info=True)


def read_startup_status(
    status_file: Path,
    timeout: float = 3.0,
    *,
    log: logging.Logger | None = None,
) -> tuple[bool, Optional[int], str]:
    """Read child-process startup status as (success, pid, error)."""
    log = log or logger
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if status_file.exists():
                lines = status_file.read_text(encoding="utf-8").splitlines()
                if len(lines) >= 2:
                    pid = int(lines[0]) if lines[0].isdigit() else None
                    success = lines[1].strip() == "ok"
                    error = lines[2] if len(lines) >= 3 else ""
                    return success, pid, error
        except (
            OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
            subprocess.SubprocessError
        ):
            log.warning("读取启动状态文件失败", exc_info=True)
        time.sleep(0.2)
    return False, None, "timeout"


def clear_startup_status(status_file: Path, *, log: logging.Logger | None = None) -> None:
    log = log or logger
    try:
        status_file.unlink(missing_ok=True)
    except (
        OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
        subprocess.SubprocessError
    ):
        log.debug("清理启动状态文件失败", exc_info=True)
