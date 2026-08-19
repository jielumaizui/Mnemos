# -*- coding: utf-8 -*-
"""OS-bound identity contract shared by daemon pidfile, heartbeat, status and stop."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

SCHEMA_VERSION = "mnemos.daemon_instance.v2"
HEARTBEAT_SCHEMA_VERSION = "mnemos.daemon_heartbeat.v3"
DAEMON_COMMAND_MARKER = "mnemos_daemon.py"


@dataclass(frozen=True)
class ProcessFingerprint:
    """Process facts sourced from the OS rather than a caller-provided PID file."""

    pid: int
    pid_start_time: str
    boot_id: str
    executable: str
    command_line: str


@dataclass(frozen=True)
class VerificationResult:
    """Machine-readable daemon identity verification outcome."""

    ok: bool
    reason: str
    identity_match: bool
    pid: int | None = None
    instance_id: str | None = None
    signal_sent: bool = False
    details: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "identity_match": self.identity_match,
            "pid": self.pid,
            "instance_id": self.instance_id,
            "signal_sent": self.signal_sent,
            "details": dict(self.details or {}),
        }


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _resolved_path(value: str | Path) -> str:
    try:
        return str(Path(value).expanduser().resolve())
    except (OSError, RuntimeError):
        return str(value)


def _config_hash(database_dir: Path) -> str:
    config_file = database_dir / "configs" / "main.json"
    try:
        return _sha256_bytes(config_file.read_bytes())
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"


def _effective_config_fingerprint() -> str:
    """Resolve the current process' value-safe effective config fingerprint."""
    try:
        from core.config import Config

        return Config(provision=False).config_fingerprint
    except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError) as exc:
        raise RuntimeError(
            f"cannot resolve effective config fingerprint: {type(exc).__name__}"
        ) from exc


def _database_identity(database_dir: Path) -> str:
    resolved = database_dir.expanduser().resolve()
    try:
        stat = resolved.stat()
        source = f"{resolved}|dev={stat.st_dev}|ino={stat.st_ino}"
    except OSError:
        source = f"{resolved}|missing"
    return _sha256_text(source)


def _service_manifest(service_names: Iterable[str]) -> list[str]:
    return sorted({str(name) for name in service_names if str(name)})


def _project_commit(project_root: Path) -> str:
    explicit = os.environ.get("MNEMOS_BUILD_COMMIT", "").strip()
    if explicit:
        return explicit
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        commit = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{40}", commit):
            return commit.lower()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        marker = project_root / "mnemos_daemon.py"
        return _sha256_bytes(marker.read_bytes())
    except OSError:
        return "unknown"


def _build_fingerprint(project_root: Path) -> str:
    """Hash runtime source content so dirty code and real build drift are observable."""
    candidates: list[Path] = []
    for directory in ("core", "daemon", "integrations"):
        root = project_root / directory
        if root.exists():
            candidates.extend(root.rglob("*.py"))
    for relative in ("mnemos_daemon.py", "mnemos_cli.py", "pyproject.toml"):
        path = project_root / relative
        if path.exists():
            candidates.append(path)
    digest = hashlib.sha256()
    for path in sorted(set(candidates), key=lambda item: str(item.relative_to(project_root))):
        relative = str(path.relative_to(project_root)).replace(os.sep, "/")
        try:
            content = path.read_bytes()
        except OSError:
            content = b"<unreadable>"
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(content)
        digest.update(b"\x00")
    return f"sha256:{digest.hexdigest()}"


def build_runtime_context(
    *,
    database_dir: Path,
    service_names: Iterable[str],
    project_root: Path | None = None,
    config_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Build the current code/config/database/service context without raw secrets."""
    root = project_root or Path(__file__).resolve().parents[1]
    manifest = _service_manifest(service_names)
    return {
        "commit": _project_commit(root),
        "build_fingerprint": _build_fingerprint(root),
        "config_hash": _config_hash(database_dir),
        "config_fingerprint": config_fingerprint or _effective_config_fingerprint(),
        "database_identity": _database_identity(database_dir),
        "service_manifest": manifest,
        "service_manifest_hash": _sha256_text(json.dumps(manifest, separators=(",", ":"))),
        "python": _resolved_path(sys.executable),
    }


def _run_text(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _darwin_proc_pidpath(pid: int) -> str | None:
    """Read the full Darwin executable path via libproc when available."""
    try:
        import ctypes

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        buffer = ctypes.create_string_buffer(4096)
        size = libproc.proc_pidpath(pid, buffer, len(buffer))
        if size > 0:
            return buffer.value.decode("utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    return None


def _darwin_proc_start_time(pid: int) -> str | None:
    """Read Darwin process start time with microsecond precision via libproc."""
    try:
        import ctypes

        class ProcBsdInfo(ctypes.Structure):
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("pbi_rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * 16),
                ("pbi_name", ctypes.c_char * 32),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        info = ProcBsdInfo()
        size = libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size == ctypes.sizeof(info) and info.pbi_start_tvsec:
            return f"{info.pbi_start_tvsec}.{info.pbi_start_tvusec:06d}"
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return None


def _darwin_boot_id() -> str | None:
    session_uuid = _run_text(["sysctl", "-n", "kern.bootsessionuuid"])
    value = _run_text(["sysctl", "-n", "kern.boottime"])
    match = re.search(r"sec\s*=\s*(\d+)", value or "")
    boot_seconds = match.group(1) if match else None
    if session_uuid:
        result = f"darwin-session:{session_uuid.lower()}"
        return f"{result}|bootsec:{boot_seconds}" if boot_seconds else result
    if boot_seconds:
        return f"darwin-boot:{boot_seconds}"
    return _sha256_text(value) if value else None


def _inspect_darwin_or_posix(pid: int) -> ProcessFingerprint | None:
    start_time = _darwin_proc_start_time(pid) if platform.system() == "Darwin" else None
    start_time = start_time or _run_text(["ps", "-o", "lstart=", "-p", str(pid)])
    command_line = _run_text(["ps", "-ww", "-o", "command=", "-p", str(pid)])
    executable = _darwin_proc_pidpath(pid) if platform.system() == "Darwin" else None
    executable = executable or _run_text(["ps", "-o", "comm=", "-p", str(pid)])
    if platform.system() == "Darwin":
        boot_id = _darwin_boot_id()
    else:
        boot_id = _run_text(["sysctl", "-n", "kern.boottime"])
        if boot_id:
            boot_id = _sha256_text(boot_id)
    if not all((start_time, command_line, executable, boot_id)):
        return None
    assert start_time is not None
    assert command_line is not None
    assert executable is not None
    assert boot_id is not None
    return ProcessFingerprint(
        pid=pid,
        pid_start_time=start_time,
        boot_id=boot_id,
        executable=executable,
        command_line=command_line,
    )


def _inspect_linux(pid: int) -> ProcessFingerprint | None:
    proc = Path("/proc") / str(pid)
    try:
        stat_value = (proc / "stat").read_text(encoding="utf-8")
        after_comm = stat_value.rsplit(")", 1)[1].strip().split()
        pid_start_time = after_comm[19]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        executable = os.readlink(proc / "exe")
        command_line = (
            (proc / "cmdline")
            .read_bytes()
            .replace(b"\x00", b" ")
            .decode("utf-8", errors="replace")
            .strip()
        )
    except (OSError, IndexError, ValueError):
        return None
    if not all((pid_start_time, boot_id, executable, command_line)):
        return None
    return ProcessFingerprint(pid, pid_start_time, boot_id, executable, command_line)


def _inspect_windows(pid: int) -> ProcessFingerprint | None:
    command = (
        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId = "
        f"{pid}\"; if ($p) {{ @($p.CreationDate,$p.ExecutablePath,$p.CommandLine) "
        "-join \"`n\" }}"
    )
    value = _run_text(["powershell", "-NoProfile", "-Command", command])
    boot_id = _run_text(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToString('o')",
        ]
    )
    if not value or not boot_id:
        return None
    parts = value.splitlines()
    if len(parts) < 3 or not all(parts[:3]):
        return None
    return ProcessFingerprint(pid, parts[0], f"windows-boot:{boot_id}", parts[1], parts[2])


def inspect_process(pid: int) -> ProcessFingerprint | None:
    """Return an OS-derived process fingerprint, or None when it cannot be proven."""
    if pid <= 0:
        return None
    system = platform.system()
    if system == "Windows":
        return _inspect_windows(pid)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError, PermissionError):
        return None
    if system == "Linux" and Path(f"/proc/{pid}/stat").exists():
        return _inspect_linux(pid)
    return _inspect_darwin_or_posix(pid)


def create_instance_record(
    *,
    database_dir: Path,
    service_names: Iterable[str],
    project_root: Path | None = None,
    process_fingerprint: ProcessFingerprint | None = None,
    instance_id: str | None = None,
    created_at: datetime | None = None,
    build_commit: str | None = None,
    config_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Create the complete immutable identity record written into the PID file."""
    observed = process_fingerprint or inspect_process(os.getpid())
    if observed is None:
        raise RuntimeError("cannot prove daemon process identity")
    context = build_runtime_context(
        database_dir=database_dir,
        service_names=service_names,
        project_root=project_root,
        config_fingerprint=config_fingerprint,
    )
    if build_commit is not None:
        context["commit"] = build_commit
    when = created_at or datetime.now(timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "instance_id": instance_id or str(uuid.uuid4()),
        "created_at": when.isoformat(),
        "pid": observed.pid,
        "pid_start_time": observed.pid_start_time,
        "boot_id": observed.boot_id,
        "executable": _resolved_path(observed.executable),
        "command_line_hash": _sha256_text(observed.command_line),
        **context,
    }


def migrate_historical_pid_for_control(
    legacy_record: Mapping[str, Any],
    *,
    database_dir: Path,
    service_names: Iterable[str],
    project_root: Path | None = None,
    process_inspector: Callable[[int], ProcessFingerprint | None] | None = None,
) -> dict[str, Any] | None:
    """Upgrade an older identity only when the live process is OS-proven."""
    source_schema = legacy_record.get("schema_version")
    if source_schema not in {"mnemos.daemon_pid.legacy", "mnemos.daemon_instance.v1"}:
        return None
    pid = legacy_record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    inspector = process_inspector or inspect_process
    observed = inspector(pid)
    if observed is None or DAEMON_COMMAND_MARKER not in observed.command_line:
        return None
    if source_schema == "mnemos.daemon_instance.v1":
        if observed.pid_start_time != legacy_record.get("pid_start_time"):
            return None
        if not _boot_ids_match(legacy_record.get("boot_id"), observed.boot_id):
            return None
        if _resolved_path(observed.executable) != _resolved_path(
            str(legacy_record.get("executable"))
        ):
            return None
        if _sha256_text(observed.command_line) != legacy_record.get("command_line_hash"):
            return None
    record = create_instance_record(
        database_dir=database_dir,
        service_names=service_names,
        project_root=project_root,
        process_fingerprint=observed,
        instance_id=f"legacy-migration-{uuid.uuid4()}",
        config_fingerprint=(
            "control-only:" + str(legacy_record.get("config_hash") or "legacy")
        ),
    )
    record["migration_source"] = str(source_schema)
    record["migration_persisted"] = False
    return record


def _failed(
    reason: str,
    *,
    record: Mapping[str, Any],
    identity_match: bool = False,
    details: Mapping[str, Any] | None = None,
) -> VerificationResult:
    pid_value = record.get("pid")
    return VerificationResult(
        ok=False,
        reason=reason,
        identity_match=identity_match,
        pid=pid_value if isinstance(pid_value, int) else None,
        instance_id=str(record.get("instance_id")) if record.get("instance_id") else None,
        details=details,
    )


def _boot_ids_match(recorded: Any, observed: Any) -> bool:
    """Allow Darwin kern.boottime's observed one-second clock-adjustment jitter."""
    if isinstance(recorded, str) and isinstance(observed, str):
        session_pattern = re.compile(r"^darwin-session:([^|]+)")
        recorded_session = session_pattern.match(recorded)
        observed_session = session_pattern.match(observed)
        if recorded_session and observed_session:
            return recorded_session.group(1) == observed_session.group(1)

        def boot_seconds(value: str) -> int | None:
            match = re.search(r"(?:^darwin-boot:|\|bootsec:)(\d+)", value)
            try:
                return int(match.group(1)) if match else None
            except (AttributeError, ValueError):
                return None

        recorded_seconds = boot_seconds(recorded)
        observed_seconds = boot_seconds(observed)
        if recorded_seconds is not None and observed_seconds is not None:
            return abs(recorded_seconds - observed_seconds) <= 2
    return bool(recorded == observed)


def verify_instance_record(
    record: Mapping[str, Any],
    *,
    database_dir: Path,
    service_names: Iterable[str],
    project_root: Path | None = None,
    require_current_context: bool = True,
    process_inspector: Callable[[int], ProcessFingerprint | None] | None = None,
    current_context: Mapping[str, Any] | None = None,
) -> VerificationResult:
    """Verify record integrity, live OS identity and optionally current build/config context."""
    if not record.get("instance_id"):
        return _failed("missing_instance_id", record=record)
    if record.get("schema_version") != SCHEMA_VERSION:
        return _failed("unsupported_identity_schema", record=record)

    required = (
        "pid",
        "pid_start_time",
        "boot_id",
        "executable",
        "command_line_hash",
        "commit",
        "build_fingerprint",
        "config_hash",
        "config_fingerprint",
        "database_identity",
        "service_manifest",
        "service_manifest_hash",
        "python",
    )
    missing = [key for key in required if record.get(key) in (None, "", [])]
    if missing:
        return _failed("incomplete_identity", record=record, details={"missing_fields": missing})

    expected_context = dict(
        current_context
        or build_runtime_context(
            database_dir=database_dir,
            service_names=service_names,
            project_root=project_root,
        )
    )
    if list(record.get("service_manifest") or []) != expected_context["service_manifest"]:
        return _failed("service_manifest_mismatch", record=record)
    if record.get("service_manifest_hash") != expected_context["service_manifest_hash"]:
        return _failed("service_manifest_hash_mismatch", record=record)

    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return _failed("invalid_pid", record=record)
    inspector = process_inspector or inspect_process
    observed = inspector(pid)
    if observed is None:
        return _failed("process_not_found_or_unverifiable", record=record)
    if observed.pid != pid:
        return _failed("pid_mismatch", record=record)
    if observed.pid_start_time != record.get("pid_start_time"):
        return _failed("pid_start_time_mismatch", record=record)
    if not _boot_ids_match(record.get("boot_id"), observed.boot_id):
        return _failed("boot_id_mismatch", record=record)
    if _resolved_path(observed.executable) != _resolved_path(str(record.get("executable"))):
        return _failed("executable_mismatch", record=record)
    if DAEMON_COMMAND_MARKER not in observed.command_line:
        return _failed("daemon_command_mismatch", record=record)
    if _sha256_text(observed.command_line) != record.get("command_line_hash"):
        return _failed("command_line_mismatch", record=record)

    if require_current_context:
        for key, reason in (
            ("build_fingerprint", "build_fingerprint_mismatch"),
            ("config_hash", "config_hash_mismatch"),
            ("config_fingerprint", "config_fingerprint_mismatch"),
            ("database_identity", "database_identity_mismatch"),
            ("python", "python_executable_mismatch"),
        ):
            if record.get(key) != expected_context.get(key):
                return _failed(reason, record=record, identity_match=True)

    return VerificationResult(
        ok=True,
        reason="verified",
        identity_match=True,
        pid=pid,
        instance_id=str(record["instance_id"]),
        details={
            "context_match": require_current_context,
            "commit_match": record.get("commit") == expected_context.get("commit"),
            "recorded_commit": record.get("commit"),
            "current_commit": expected_context.get("commit"),
            "build_compatible": (
                record.get("build_fingerprint") == expected_context.get("build_fingerprint")
            ),
        },
    )


def signal_verified_instance(
    record: Mapping[str, Any],
    sig: int,
    *,
    database_dir: Path,
    service_names: Iterable[str],
    project_root: Path | None = None,
    process_inspector: Callable[[int], ProcessFingerprint | None] | None = None,
    signal_sender: Callable[[int, int], Any] | None = None,
) -> VerificationResult:
    """Revalidate the OS identity immediately before sending a signal."""
    result = verify_instance_record(
        record,
        database_dir=database_dir,
        service_names=service_names,
        project_root=project_root,
        require_current_context=False,
        process_inspector=process_inspector,
    )
    if not result.ok or result.pid is None:
        return result
    sender = signal_sender or os.kill
    try:
        sender(result.pid, sig)
    except (OSError, ProcessLookupError, PermissionError) as exc:
        return VerificationResult(
            ok=False,
            reason="signal_failed",
            identity_match=True,
            pid=result.pid,
            instance_id=result.instance_id,
            details={"error_type": type(exc).__name__},
        )
    return replace(result, reason="signal_sent", signal_sent=True)
