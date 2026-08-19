"""Worker registry and process-wide write sandbox for Raw recovery."""

from __future__ import annotations

import ctypes
import gc
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

import psutil

from core.ops.durable_io import fsync_directory
from core.ops.durable_io import read_native_bytes
from scripts.agent_source_raw_recovery_contract import (
    AgentSourceRawReconciliationError,
)

_PROCESS_WRITE_SCOPE_LOCK = threading.RLock()
_ACTIVE_PROCESS_WRITE_SCOPE: "_ProcessDatabaseWriteScope | None" = None
_PROCESS_WRITE_AUDIT_HOOK_INSTALLED = False
_RECOVERY_WORKER_REGISTRY_SCHEMA = "mnemos.raw_recovery_worker_owner.v1"


def _recovery_worker_owner_is_live(owner: Mapping[str, Any]) -> bool:
    if (
        owner.get("schema_version") != _RECOVERY_WORKER_REGISTRY_SCHEMA
        or not isinstance(owner.get("controller_pid"), int)
        or isinstance(owner.get("controller_pid"), bool)
        or int(owner["controller_pid"]) < 1
        or not isinstance(owner.get("controller_create_time"), (int, float))
        or isinstance(owner.get("controller_create_time"), bool)
    ):
        raise AgentSourceRawReconciliationError(
            "recovery_worker_owner_invalid"
        )
    try:
        process = psutil.Process(int(owner["controller_pid"]))
        return (
            abs(
                float(process.create_time())
                - float(owner["controller_create_time"])
            )
            < 0.001
        )
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        raise AgentSourceRawReconciliationError(
            "recovery_worker_owner_unverifiable"
        ) from None


def _safe_recovery_worker_registry_root() -> Path:
    root = (
        Path(tempfile.gettempdir()).resolve()
        / "mnemos-raw-recovery-workers-v1"
    )
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = root.lstat()
        os.chmod(root, 0o700)
    except OSError:
        raise AgentSourceRawReconciliationError(
            "recovery_worker_registry_unavailable"
        ) from None
    if (
        root.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise AgentSourceRawReconciliationError(
            "recovery_worker_registry_unsafe"
        )
    return root


def _create_recovery_worker_root(kind: str) -> tuple[Path, int]:
    if kind not in {"challenger", "raw-generation"}:
        raise AgentSourceRawReconciliationError(
            "recovery_worker_kind_invalid"
        )
    root = _safe_recovery_worker_registry_root()
    lock_path = root / ".registry.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        import fcntl

        descriptor = os.open(lock_path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except (ImportError, OSError):
        raise AgentSourceRawReconciliationError(
            "recovery_worker_registry_unavailable"
        ) from None
    cleaned = 0
    worker_root: Path | None = None
    temporary: Path | None = None
    try:
        for child in root.iterdir():
            if not child.name.startswith(
                ("challenger-", "raw-generation-")
            ):
                continue
            try:
                child_metadata = child.lstat()
            except OSError:
                raise AgentSourceRawReconciliationError(
                    "recovery_worker_registry_unavailable"
                ) from None
            if (
                stat.S_ISLNK(child_metadata.st_mode)
                or not stat.S_ISDIR(child_metadata.st_mode)
            ):
                raise AgentSourceRawReconciliationError(
                    "recovery_worker_registry_unsafe"
                )
            marker = child / ".owner.json"
            try:
                owner = json.loads(read_native_bytes(marker).decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise AgentSourceRawReconciliationError(
                    "recovery_worker_owner_invalid"
                ) from None
            if not isinstance(owner, Mapping):
                raise AgentSourceRawReconciliationError(
                    "recovery_worker_owner_invalid"
                )
            if _recovery_worker_owner_is_live(owner):
                continue
            shutil.rmtree(child)
            cleaned += 1
        worker_root = Path(
            tempfile.mkdtemp(prefix=f"{kind}-", dir=root)
        ).resolve()
        os.chmod(worker_root, 0o700)
        marker = worker_root / ".owner.json"
        temporary = worker_root / (
            f".owner.{os.getpid()}.{time.time_ns()}.tmp"
        )
        owner = {
            "schema_version": _RECOVERY_WORKER_REGISTRY_SCHEMA,
            "controller_pid": os.getpid(),
            "controller_create_time": psutil.Process().create_time(),
        }
        with open(temporary, "x", encoding="utf-8") as handle:
            json.dump(owner, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, marker)
        fsync_directory(worker_root)
        fsync_directory(root)
    except AgentSourceRawReconciliationError:
        if worker_root is not None:
            shutil.rmtree(worker_root, ignore_errors=True)
        raise
    except (OSError, psutil.Error):
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if worker_root is not None:
            shutil.rmtree(worker_root, ignore_errors=True)
        raise AgentSourceRawReconciliationError(
            "recovery_worker_registry_unavailable"
        ) from None
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    return worker_root, cleaned


def _descriptor_target_path(descriptor: int) -> Path | None:
    raw_target = ""
    for descriptor_path in (
        f"/dev/fd/{descriptor}",
        f"/proc/self/fd/{descriptor}",
    ):
        try:
            raw_target = os.readlink(descriptor_path)
            break
        except OSError:
            continue
    if not raw_target:
        try:
            import fcntl

            get_path = getattr(fcntl, "F_GETPATH")
            raw_target = os.fsdecode(
                fcntl.fcntl(
                    descriptor,
                    get_path,
                    b"\0" * 1024,
                ).split(b"\0", 1)[0]
            )
        except (AttributeError, ImportError, OSError, TypeError, ValueError):
            return None
    try:
        return Path(
            os.path.realpath(os.path.abspath(raw_target))
        )
    except (OSError, ValueError, TypeError):
        return None


def _audit_event_path(
    value: object,
    *,
    dir_fd: object | None = None,
) -> Path | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return _descriptor_target_path(value)
    if not isinstance(value, (str, bytes, os.PathLike)):
        return None
    try:
        raw = os.fspath(value)
    except TypeError:
        return None
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    if not raw:
        return None
    if raw.startswith("file:"):
        raw = raw[5:].split("?", 1)[0]
    try:
        candidate = Path(raw)
        if not candidate.is_absolute() and isinstance(dir_fd, int):
            descriptor_target = _descriptor_target_path(dir_fd)
            if descriptor_target is None:
                return None
            candidate = descriptor_target / candidate
        return Path(
            os.path.realpath(os.path.abspath(os.fspath(candidate)))
        )
    except (OSError, ValueError, TypeError):
        return None


def _audit_open_write_requested(args: tuple[object, ...]) -> bool | None:
    """Return the open audit event's write intent, or ``None`` if ambiguous."""

    mode_value = args[1] if len(args) > 1 else ""
    mode = mode_value if isinstance(mode_value, str) else str(mode_value or "")
    flags_value = args[2] if len(args) > 2 else 0
    if isinstance(flags_value, bool) or not isinstance(flags_value, int):
        return None
    write_flags = (
        os.O_WRONLY
        | os.O_RDWR
        | os.O_CREAT
        | os.O_TRUNC
        | os.O_APPEND
    )
    return any(character in mode for character in "wax+") or bool(
        flags_value & write_flags
    )


def _audit_event_write_paths(
    event: str,
    args: tuple[object, ...],
) -> tuple[Path, ...]:
    if event == "sqlite3.connect" and args:
        path = _audit_event_path(args[0])
        return (path,) if path is not None else ()
    if event == "open" and args:
        write_requested = _audit_open_write_requested(args)
        if write_requested is False:
            return ()
        path = _audit_event_path(args[0])
        return (path,) if path is not None else ()
    path_specs = {
        "os.remove": ((0, 1),),
        "os.unlink": ((0, 1),),
        "os.rmdir": ((0, 1),),
        "os.mkdir": ((0, 2),),
        "os.chmod": ((0, 2),),
        "os.truncate": ((0, None),),
        "os.rename": ((0, 2), (1, 3)),
        "os.replace": ((0, 2), (1, 3)),
        "os.link": ((0, 2), (1, 3)),
        "os.symlink": ((1, 2),),
    }.get(event, ())
    paths = tuple(
        path
        for index, dir_fd_index in path_specs
        if index < len(args)
        for path in (
            _audit_event_path(
                args[index],
                dir_fd=(
                    args[dir_fd_index]
                    if dir_fd_index is not None
                    and dir_fd_index < len(args)
                    else None
                ),
            ),
        )
        if path is not None
    )
    return paths


def _audit_event_has_ambiguous_relative_open(
    event: str,
    args: tuple[object, ...],
) -> bool:
    """Reject relative write opens because CPython omits their ``dir_fd``."""

    if (
        event != "open"
        or not args
        or (
            isinstance(args[0], int)
            and not isinstance(args[0], bool)
        )
    ):
        return False
    write_requested = _audit_open_write_requested(args)
    if write_requested is False:
        return False
    if not isinstance(args[0], (str, bytes, os.PathLike)):
        return True
    try:
        raw = os.fspath(args[0])
    except TypeError:
        return True
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    if raw.startswith("file:"):
        raw = raw[5:].split("?", 1)[0]
    return not Path(raw).is_absolute()


def _audit_event_requires_write_attribution(
    event: str,
    args: tuple[object, ...],
) -> bool:
    if event == "sqlite3.connect":
        return bool(args)
    if event == "open":
        if not args:
            return False
        write_requested = _audit_open_write_requested(args)
        return write_requested is not False
    return event in {
        "os.remove",
        "os.unlink",
        "os.rmdir",
        "os.mkdir",
        "os.chmod",
        "os.truncate",
        "os.rename",
        "os.replace",
        "os.link",
        "os.symlink",
    }


def _close_inherited_regular_file_descriptors() -> int:
    """Close every inherited regular-file descriptor before worker code runs."""

    try:
        opened = list(psutil.Process().open_files())
    except psutil.Error:
        raise AgentSourceRawReconciliationError(
            "worker_inherited_descriptor_audit_failed"
        ) from None
    closed = 0
    try:
        devnull = os.open(
            os.devnull,
            os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
        )
        for item in opened:
            descriptor = int(item.fd)
            if descriptor < 0:
                raise AgentSourceRawReconciliationError(
                    "worker_inherited_descriptor_audit_failed"
                )
            try:
                if descriptor != devnull:
                    os.dup2(
                        devnull,
                        descriptor,
                        inheritable=False,
                    )
                closed += 1
            except OSError:
                raise AgentSourceRawReconciliationError(
                    "worker_inherited_descriptor_close_failed"
                ) from None
    except OSError:
        raise AgentSourceRawReconciliationError(
            "worker_inherited_descriptor_close_failed"
        ) from None
    finally:
        if "devnull" in locals():
            os.close(devnull)
    # Python IO wrappers inherited across fork may later finalize and close
    # their original numeric descriptor again.  Replacing those descriptors
    # with /dev/null prevents immediate reuse; collect unreachable wrappers
    # before creating guardian pipes or SQLite handles.
    gc.collect()
    try:
        remaining = list(psutil.Process().open_files())
    except psutil.Error:
        raise AgentSourceRawReconciliationError(
            "worker_inherited_descriptor_audit_failed"
        ) from None
    if remaining:
        raise AgentSourceRawReconciliationError(
            "worker_inherited_descriptor_close_failed"
        )
    return closed


def _install_worker_filesystem_sandbox(
    allowed_write_roots: Iterable[Path],
    *,
    allowed_write_paths: Iterable[Path] = (),
) -> str:
    """Apply the Darwin kernel sandbox before untrusted parser/runtime code."""

    roots = tuple(
        sorted(
            {
                Path(path).expanduser().resolve(strict=False)
                for path in allowed_write_roots
            },
            key=str,
        )
    )
    if not roots:
        raise AgentSourceRawReconciliationError(
            "worker_filesystem_sandbox_unavailable"
        )
    exact_paths = tuple(
        sorted(
            {
                Path(path).expanduser().resolve(strict=False)
                for path in allowed_write_paths
            },
            key=str,
        )
    )
    if sys.platform != "darwin":
        raise AgentSourceRawReconciliationError(
            "worker_filesystem_sandbox_unavailable"
        )
    filters = " ".join(
        [
            *(
                f"(subpath {json.dumps(str(root))})"
                for root in roots
            ),
            *(
                f"(literal {json.dumps(str(path))})"
                for path in exact_paths
            ),
        ]
    )
    profile = (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write* "
        f"(require-not (require-any {filters})))\n"
    )
    error_buffer = ctypes.c_char_p()
    try:
        library = ctypes.CDLL("/usr/lib/libsandbox.dylib")
        library.sandbox_init.argtypes = [
            ctypes.c_char_p,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_char_p),
        ]
        library.sandbox_init.restype = ctypes.c_int
        result = int(
            library.sandbox_init(
                profile.encode("utf-8"),
                0,
                ctypes.byref(error_buffer),
            )
        )
    except (AttributeError, OSError, TypeError, ValueError):
        raise AgentSourceRawReconciliationError(
            "worker_filesystem_sandbox_unavailable"
        ) from None
    if result != 0:
        raise AgentSourceRawReconciliationError(
            "worker_filesystem_sandbox_unavailable"
        )
    return "darwin_kernel_deny_writes_outside_allowed_roots_v1"


def _process_write_audit_hook(event: str, args: tuple[object, ...]) -> None:
    with _PROCESS_WRITE_SCOPE_LOCK:
        scope = _ACTIVE_PROCESS_WRITE_SCOPE
    if scope is None:
        return
    if event in {
        "subprocess.Popen",
        "os.posix_spawn",
        "os.posix_spawnp",
        "os.exec",
        "os.execve",
    }:
        scope.reject_exec_child()
    if _audit_event_has_ambiguous_relative_open(event, args):
        scope.reject_ambiguous_relative_open()
    write_paths = _audit_event_write_paths(event, args)
    if _audit_event_requires_write_attribution(event, args) and not write_paths:
        scope.reject_unattributed_write()
    for path in write_paths:
        if event == "os.mkdir" and path == scope.database_dir and path.is_dir():
            continue
        scope.authorize(path)


def _ensure_process_write_audit_hook() -> None:
    global _PROCESS_WRITE_AUDIT_HOOK_INSTALLED
    with _PROCESS_WRITE_SCOPE_LOCK:
        if _PROCESS_WRITE_AUDIT_HOOK_INSTALLED:
            return
        sys.addaudithook(_process_write_audit_hook)
        _PROCESS_WRITE_AUDIT_HOOK_INSTALLED = True


class _ProcessDatabaseWriteScope:
    """Attribute this process's database-root writes to an exact Raw-only set."""

    def __init__(
        self,
        *,
        database_dir: Path,
        allowed_names: Iterable[str],
        allowed_subtrees: Iterable[Path] = (),
    ) -> None:
        self.database_dir = Path(database_dir).expanduser().resolve(strict=False)
        self.allowed_names = frozenset(str(name) for name in allowed_names)
        self.allowed_subtrees = tuple(
            Path(path).expanduser().resolve(strict=False) for path in allowed_subtrees
        )
        self.blocked_names: set[str] = set()
        self.active = False
        self.descriptor_audit_ok = False

    def _is_allowed(self, path: Path) -> bool:
        if any(
            path == root or root in path.parents
            for root in self.allowed_subtrees
        ):
            return True
        if path.parent != self.database_dir:
            return False
        name = path.name
        if name in self.allowed_names:
            return True
        return any(
            name in {f"{base}-journal", f"{base}-wal", f"{base}-shm"}
            or (
                name.startswith(f".{base}.")
                and name.endswith(
                    (
                        ".tmp",
                        ".restore",
                        ".restore-journal",
                        ".restore-shm",
                        ".restore-wal",
                    )
                )
            )
            or name.startswith(f"{base}.tmp.")
            for base in self.allowed_names
        )

    def authorize(self, path: Path) -> None:
        try:
            path.relative_to(self.database_dir)
        except ValueError:
            return
        if self._is_allowed(path):
            return
        self.blocked_names.add(path.name)
        raise PermissionError("raw_reconciliation_process_write_scope_violation")

    def reject_exec_child(self) -> None:
        self.blocked_names.add("<exec-child>")
        raise PermissionError("raw_reconciliation_process_exec_scope_violation")

    def reject_ambiguous_relative_open(self) -> None:
        self.blocked_names.add("<ambiguous-relative-open>")
        raise PermissionError(
            "raw_reconciliation_process_relative_open_scope_violation"
        )

    def reject_unattributed_write(self) -> None:
        self.blocked_names.add("<unattributed-write>")
        raise PermissionError(
            "raw_reconciliation_process_unattributed_write_scope_violation"
        )

    def start(self) -> None:
        global _ACTIVE_PROCESS_WRITE_SCOPE
        _ensure_process_write_audit_hook()
        with _PROCESS_WRITE_SCOPE_LOCK:
            if _ACTIVE_PROCESS_WRITE_SCOPE is not None:
                raise AgentSourceRawReconciliationError("process_write_scope_already_active")
            _ACTIVE_PROCESS_WRITE_SCOPE = self
            self.active = True
        try:
            if os.name == "posix":
                try:
                    import fcntl

                    for opened in psutil.Process().open_files():
                        descriptor = int(opened.fd)
                        try:
                            flags = int(fcntl.fcntl(descriptor, fcntl.F_GETFL))
                            if flags & os.O_ACCMODE == os.O_RDONLY:
                                continue
                            target = _audit_event_path(opened.path)
                        except (OSError, ValueError):
                            raise AgentSourceRawReconciliationError(
                                "process_write_scope_descriptor_audit_failed"
                            ) from None
                        if target is None:
                            continue
                        try:
                            target.relative_to(self.database_dir)
                        except ValueError:
                            continue
                        if not self._is_allowed(target):
                            self.blocked_names.add(target.name)
                except (ImportError, OSError, psutil.Error):
                    raise AgentSourceRawReconciliationError(
                        "process_write_scope_descriptor_audit_failed"
                    ) from None
            self.descriptor_audit_ok = True
            if self.blocked_names:
                raise AgentSourceRawReconciliationError(
                    "process_write_scope_preexisting_handle"
                )
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        global _ACTIVE_PROCESS_WRITE_SCOPE
        with _PROCESS_WRITE_SCOPE_LOCK:
            if _ACTIVE_PROCESS_WRITE_SCOPE is self:
                _ACTIVE_PROCESS_WRITE_SCOPE = None
            self.active = False

    def evidence(self) -> dict[str, object]:
        names = sorted(self.blocked_names)
        return {
            "process_write_scope_verified": self.descriptor_audit_ok,
            "process_write_guard": "python-audit-exact-database-path-v1",
            "blocked_process_mutation_count": len(names),
            "blocked_process_mutation_name_hashes": [
                hashlib.sha256(name.encode("utf-8")).hexdigest()[:16] for name in names
            ],
        }
