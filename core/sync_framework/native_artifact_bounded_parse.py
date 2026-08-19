"""RSS-bounded child parsing and private transcript spool ownership."""

from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import shutil
import signal
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import psutil

from core.runtime_environment import environment_set
from core.sync_framework.agent_source import (
    NativeSourceContractError,
    SessionInfo,
    SessionParseResult,
    Turn,
    parse_discovered_session_result,
)
from core.sync_framework.native_artifact_models import (
    NativeArtifactInventoryError,
    SNAPSHOT_PARSE_CHILD_REPORTED_ERROR_CODES,
)
from core.ops.durable_io import open_native_binary, read_native_bytes

_WORKER_POLL_SECONDS = 0.005
NATIVE_PARSE_FAILURE_SCHEMA_VERSION = "mnemos.native_parse_failure.v3"
_EPHEMERAL_PARENT: ContextVar[Path | None] = ContextVar(
    "native_bounded_parse_ephemeral_parent",
    default=None,
)


def _physical_path_kind(path: Path, *, unavailable_code: str) -> str:
    try:
        metadata = Path(path).lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        raise NativeArtifactInventoryError(unavailable_code) from None
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    return "other"


@contextmanager
def isolated_bounded_parse_spool(parent: Path) -> Iterator[None]:
    """Confine nested parser spools to one caller-owned private worker root."""
    try:
        resolved = Path(parent).expanduser().resolve(strict=False)
        resolved_kind = _physical_path_kind(
            resolved,
            unavailable_code="native_parse_spool_scope_invalid",
        )
    except OSError:
        raise NativeArtifactInventoryError("native_parse_spool_scope_invalid") from None
    if resolved_kind != "directory":
        raise NativeArtifactInventoryError("native_parse_spool_scope_invalid")
    if _EPHEMERAL_PARENT.get() is not None:
        raise NativeArtifactInventoryError("native_parse_spool_scope_already_active")
    token = _EPHEMERAL_PARENT.set(resolved)
    try:
        yield
    finally:
        _EPHEMERAL_PARENT.reset(token)


def _estimated_object_bytes(
    value: Any,
    seen: set[int] | None = None,
) -> int:
    visited = seen if seen is not None else set()
    identity = id(value)
    if identity in visited:
        return 0
    visited.add(identity)
    size = sys.getsizeof(value)
    if is_dataclass(value) and not isinstance(value, type):
        return size + sum(
            _estimated_object_bytes(getattr(value, field.name), visited) for field in fields(value)
        )
    if isinstance(value, Mapping):
        return size + sum(
            _estimated_object_bytes(key, visited) + _estimated_object_bytes(item, visited)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return size + sum(_estimated_object_bytes(item, visited) for item in value)
    return size


def _write_bounded_parse_failure(
    *,
    failure_path: Path,
    code: str,
    source: Any,
    session: SessionInfo,
    exception_type: str,
    reason_code: str = "",
    storage_evidence: Mapping[str, Any] | None = None,
) -> None:
    """Persist only content-free parser failure identity."""
    session_id = str(
        getattr(session, "canonical_session_id", "") or getattr(session, "session_id", "")
    ).lower()
    payload = {
        "schema_version": NATIVE_PARSE_FAILURE_SCHEMA_VERSION,
        "code": str(code),
        "source_name": str(getattr(source, "name", "") or ""),
        "session_id_hash": ("sha256:" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()),
        "exception_type": str(exception_type),
    }
    if reason_code:
        payload["reason_code"] = str(reason_code)
    for key, value in dict(storage_evidence or {}).items():
        if key in {
            "failure_class",
            "os_errno",
            "sqlite_errorcode",
            "sqlite_errorname",
        }:
            payload[key] = value
    with failure_path.open("w", encoding="utf-8") as handle:
        json.dump(
            payload,
            handle,
            sort_keys=True,
            separators=(",", ":"),
        )
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_bounded_parse_spool(
    *,
    output_path: Path,
    failure_path: Path,
    sources: list[Any],
    sessions_by_source: list[list[SessionInfo]],
    max_bytes: int,
    max_turns: int,
) -> None:
    """Child-only parser execution; output is a private bounded NDJSON spool."""
    total_bytes = 0
    total_turns = 0
    estimated_bytes = 0
    try:
        with output_path.open("w", encoding="utf-8") as handle:
            pairs = zip(sources, sessions_by_source, strict=True)
            for source_index, (source, sessions) in enumerate(pairs):
                for session in sessions:
                    try:
                        parse_result = parse_discovered_session_result(
                            source,
                            session,
                        )
                        parsed = tuple(parse_result.turns)
                    except MemoryError:
                        _write_bounded_parse_failure(
                            failure_path=failure_path,
                            code="native_freeze_budget_exceeded",
                            source=source,
                            session=session,
                            exception_type="MemoryError",
                        )
                        os._exit(91)
                    except BaseException as exc:
                        if not isinstance(exc, Exception):
                            raise
                        changed = (
                            isinstance(exc, NativeSourceContractError)
                            and exc.code == "native_session_artifact_changed_during_parse"
                        )
                        retryable = isinstance(exc, NativeSourceContractError) and exc.retryable
                        failure_code = (
                            "native_artifact_drift_during_freeze"
                            if changed
                            else (
                                "native_session_parser_retryable_exception"
                                if retryable
                                else "native_session_parser_exception"
                            )
                        )
                        _write_bounded_parse_failure(
                            failure_path=failure_path,
                            code=failure_code,
                            source=source,
                            session=session,
                            exception_type=type(exc).__name__,
                            reason_code=(
                                exc.code
                                if isinstance(
                                    exc,
                                    NativeSourceContractError,
                                )
                                else ""
                            ),
                            storage_evidence=(
                                exc.details
                                if isinstance(
                                    exc,
                                    NativeSourceContractError,
                                )
                                else None
                            ),
                        )
                        os._exit(90)
                    total_turns += len(parsed)
                    estimated_bytes += _estimated_object_bytes(parsed)
                    if total_turns > max_turns or estimated_bytes > max_bytes:
                        os._exit(91)
                    header = {
                        "disposition": parse_result.disposition,
                        "reason_code": parse_result.reason_code,
                        "artifact_evidence_hash": (parse_result.artifact_evidence_hash),
                        "infrastructure_attempt_count": (parse_result.infrastructure_attempt_count),
                        "recovered_infrastructure_failure": dict(
                            parse_result.recovered_infrastructure_failure
                        ),
                    }
                    session_header = (
                        "S\t"
                        f"{source_index}\t"
                        + json.dumps(
                            session.session_id,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\t"
                        + json.dumps(
                            header,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode("utf-8")
                    total_bytes += len(session_header)
                    if total_bytes > max_bytes:
                        os._exit(91)
                    handle.buffer.write(session_header)
                    for turn in parsed:
                        encoded_turn = (
                            "T\t"
                            f"{source_index}\t"
                            + json.dumps(
                                session.session_id,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "\t"
                            + json.dumps(
                                asdict(turn),
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            + "\n"
                        ).encode("utf-8")
                        total_bytes += len(encoded_turn)
                        if total_bytes > max_bytes:
                            os._exit(91)
                        handle.buffer.write(encoded_turn)
                    handle.flush()
            os.fsync(handle.fileno())
    except MemoryError:
        os._exit(91)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RuntimeError,
    ):
        os._exit(90)


def read_bounded_parse_failure(
    failure_path: Path,
) -> tuple[str, dict[str, Any]] | None:
    try:
        payload = json.loads(read_native_bytes(failure_path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    code = payload.get("code")
    source_name = payload.get("source_name")
    session_id_hash = payload.get("session_id_hash")
    exception_type = payload.get("exception_type")
    reason_code = payload.get("reason_code")
    failure_class = payload.get("failure_class")
    os_errno = payload.get("os_errno")
    sqlite_errorcode = payload.get("sqlite_errorcode")
    sqlite_errorname = payload.get("sqlite_errorname")
    allowed_keys = {
        "code",
        "exception_type",
        "failure_class",
        "os_errno",
        "reason_code",
        "schema_version",
        "session_id_hash",
        "source_name",
        "sqlite_errorcode",
        "sqlite_errorname",
    }
    if (
        payload.get("schema_version") != NATIVE_PARSE_FAILURE_SCHEMA_VERSION
        or not set(payload).issubset(allowed_keys)
        or code not in SNAPSHOT_PARSE_CHILD_REPORTED_ERROR_CODES
        or not isinstance(source_name, str)
        or not source_name
        or not isinstance(session_id_hash, str)
        or not session_id_hash.startswith("sha256:")
        or len(session_id_hash) != 71
        or any(character not in "0123456789abcdef" for character in session_id_hash[7:])
        or not isinstance(exception_type, str)
        or not exception_type
        or (
            reason_code is not None
            and (
                not isinstance(reason_code, str)
                or re.fullmatch(
                    r"[a-z][a-z0-9_]{2,127}",
                    reason_code,
                )
                is None
            )
        )
        or (
            failure_class is not None
            and failure_class
            not in {
                "os_nontransient",
                "os_transient",
                "sqlite_nontransient",
                "sqlite_transient",
                "storage_untyped",
            }
        )
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (os_errno, sqlite_errorcode)
            if value is not None
        )
        or (
            sqlite_errorname is not None
            and (
                not isinstance(sqlite_errorname, str)
                or re.fullmatch(
                    r"SQLITE_[A-Z0-9_]{1,96}",
                    sqlite_errorname,
                )
                is None
            )
        )
    ):
        return None
    details: dict[str, Any] = {
        "source_name": source_name,
        "session_id_hash": session_id_hash,
        "exception_type": exception_type,
    }
    if isinstance(reason_code, str):
        details["reason_code"] = reason_code
    if isinstance(failure_class, str):
        details["failure_class"] = failure_class
    if isinstance(os_errno, int):
        details["os_errno"] = os_errno
    if isinstance(sqlite_errorcode, int):
        details["sqlite_errorcode"] = sqlite_errorcode
    if isinstance(sqlite_errorname, str):
        details["sqlite_errorname"] = sqlite_errorname
    return str(code), details


def iter_spool_lines(handle: Any) -> Iterator[bytes]:
    """Return a lazy iterator; never pre-materialize a transcript spool."""
    return iter(handle)


def _allocate_spool_root(
    create_registered_snapshot_root: Callable[[], tuple[Path, int]],
    write_snapshot_owner_marker: Callable[[Path, tuple[int, ...]], None],
) -> Path:
    ephemeral_parent = _EPHEMERAL_PARENT.get()
    if ephemeral_parent is None:
        spool_root, _ = create_registered_snapshot_root()
        return spool_root
    try:
        spool_root = Path(
            tempfile.mkdtemp(
                prefix="parse-spool-",
                dir=ephemeral_parent,
            )
        ).resolve()
        os.chmod(spool_root, 0o700)
        write_snapshot_owner_marker(spool_root, (os.getpid(),))
        return spool_root
    except (OSError, psutil.Error):
        raise NativeArtifactInventoryError("native_snapshot_registry_unavailable") from None


def bounded_parse_sources(
    *,
    sources: list[Any],
    sessions_by_source: list[list[SessionInfo]],
    max_bytes: int,
    max_turns: int,
    create_registered_snapshot_root: Callable[[], tuple[Path, int]],
    write_snapshot_owner_marker: Callable[[Path, tuple[int, ...]], None],
) -> tuple[list[dict[str, SessionParseResult]], int, int]:
    """Parse in an RSS-monitored child so one parser cannot exhaust the parent."""
    if not hasattr(os, "fork"):
        raise NativeArtifactInventoryError("native_freeze_worker_unavailable")
    spool_root = _allocate_spool_root(
        create_registered_snapshot_root,
        write_snapshot_owner_marker,
    )
    output_path = spool_root / "turn-spool.ndjson"
    failure_path = spool_root / "parse-failure.json"
    descriptor = os.open(
        output_path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    os.close(descriptor)
    failure_descriptor = os.open(
        failure_path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    os.close(failure_descriptor)
    ready_read, ready_write = os.pipe()
    start_read, start_write = os.pipe()
    null_descriptor = os.open(os.devnull, os.O_RDWR)
    pid = os.fork()
    if pid == 0:  # pragma: no branch - isolated worker
        try:
            os.dup2(null_descriptor, 1)
            os.dup2(null_descriptor, 2)
            if null_descriptor > 2:
                os.close(null_descriptor)
            private_temp_root = str(spool_root)
            for variable in ("SQLITE_TMPDIR", "TMPDIR", "TMP", "TEMP"):
                environment_set(variable, private_temp_root)
            tempfile.tempdir = private_temp_root
            os.close(ready_read)
            os.close(start_write)
            os.write(ready_write, b"1")
            os.close(ready_write)
            address_space_limit = int(os.read(start_read, 64).decode("ascii"))
            os.close(start_read)
            resource.setrlimit(
                resource.RLIMIT_AS,
                (address_space_limit, address_space_limit),
            )
            _write_bounded_parse_spool(
                output_path=output_path,
                failure_path=failure_path,
                sources=sources,
                sessions_by_source=sessions_by_source,
                max_bytes=max_bytes,
                max_turns=max_turns,
            )
            os._exit(0)
        except BaseException:
            os._exit(93)
    os.close(ready_write)
    os.close(start_read)
    os.close(null_descriptor)
    killed_for_budget = False
    try:
        if os.read(ready_read, 1) != b"1":
            raise NativeArtifactInventoryError("native_freeze_worker_failed")
        os.close(ready_read)
        process = psutil.Process(pid)
        write_snapshot_owner_marker(spool_root, (os.getpid(), pid))
        baseline_memory = process.memory_info()
        baseline_rss = baseline_memory.rss
        os.write(
            start_write,
            str(baseline_memory.vms + max_bytes).encode("ascii"),
        )
        os.close(start_write)
        while True:
            waited_pid, status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == pid:
                break
            try:
                if process.memory_info().rss - baseline_rss > max_bytes:
                    killed_for_budget = True
                    os.kill(pid, signal.SIGKILL)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
            time.sleep(_WORKER_POLL_SECONDS)
        if killed_for_budget:
            raise NativeArtifactInventoryError("native_freeze_worker_budget_exceeded")
        exit_code = os.waitstatus_to_exitcode(status)
        if exit_code == 91:
            failure = read_bounded_parse_failure(failure_path)
            raise NativeArtifactInventoryError(
                "native_freeze_budget_exceeded",
                details=failure[1] if failure is not None else None,
            )
        if exit_code == 90:
            failure = read_bounded_parse_failure(failure_path)
            if failure is not None:
                raise NativeArtifactInventoryError(
                    failure[0],
                    details=failure[1],
                )
            raise NativeArtifactInventoryError("native_freeze_worker_failed")
        if exit_code == 93:
            raise NativeArtifactInventoryError("native_freeze_worker_setup_failed")
        if exit_code < 0:
            raise NativeArtifactInventoryError(
                "native_freeze_worker_signaled",
                details={"signal": -exit_code},
            )
        if exit_code != 0:
            raise NativeArtifactInventoryError("native_freeze_worker_failed")
        frozen_turn_count = 0
        estimated_bytes = 0
        materialized: list[dict[str, list[Turn]]] = [{} for _source in sources]
        headers: list[dict[str, dict[str, Any]]] = [{} for _source in sources]
        consumed_spool_bytes = 0
        with open_native_binary(output_path) as handle:
            for encoded_line in iter_spool_lines(handle):
                consumed_spool_bytes += len(encoded_line)
                if consumed_spool_bytes > max_bytes:
                    raise NativeArtifactInventoryError("native_freeze_budget_exceeded")
                parts = encoded_line.decode("utf-8").rstrip("\n").split("\t", 3)
                if len(parts) not in {3, 4}:
                    raise NativeArtifactInventoryError("native_freeze_worker_failed")
                kind = parts[0]
                source_index = int(parts[1])
                if source_index < 0 or source_index >= len(sources):
                    raise NativeArtifactInventoryError("native_freeze_worker_failed")
                session_id = str(json.loads(parts[2]))
                if kind == "S" and len(parts) == 4:
                    if session_id in materialized[source_index]:
                        raise NativeArtifactInventoryError("native_freeze_worker_failed")
                    header = json.loads(parts[3])
                    expected_header_keys = {
                        "disposition",
                        "reason_code",
                        "artifact_evidence_hash",
                        "infrastructure_attempt_count",
                        "recovered_infrastructure_failure",
                    }
                    if not isinstance(header, dict) or set(header) != expected_header_keys:
                        raise NativeArtifactInventoryError("native_freeze_worker_failed")
                    materialized[source_index][session_id] = []
                    headers[source_index][session_id] = dict(header)
                    continue
                if kind != "T" or len(parts) != 4:
                    raise NativeArtifactInventoryError("native_freeze_worker_failed")
                if session_id not in materialized[source_index]:
                    raise NativeArtifactInventoryError("native_freeze_worker_failed")
                if frozen_turn_count + 1 > max_turns:
                    raise NativeArtifactInventoryError("native_freeze_budget_exceeded")
                turn = Turn(**json.loads(parts[3]))
                turn_bytes = _estimated_object_bytes(turn)
                if estimated_bytes + turn_bytes > max_bytes:
                    raise NativeArtifactInventoryError("native_freeze_budget_exceeded")
                materialized[source_index][session_id].append(turn)
                frozen_turn_count += 1
                estimated_bytes += turn_bytes
        results_by_source = [
            {
                session_id: SessionParseResult(
                    turns=tuple(turns),
                    disposition=headers[source_index][session_id]["disposition"],
                    reason_code=headers[source_index][session_id]["reason_code"],
                    artifact_evidence_hash=headers[source_index][session_id][
                        "artifact_evidence_hash"
                    ],
                    infrastructure_attempt_count=headers[source_index][session_id][
                        "infrastructure_attempt_count"
                    ],
                    recovered_infrastructure_failure=headers[source_index][session_id][
                        "recovered_infrastructure_failure"
                    ],
                )
                for session_id, turns in source_turns.items()
            }
            for source_index, source_turns in enumerate(materialized)
        ]
        return results_by_source, frozen_turn_count, estimated_bytes
    except (
        OSError,
        UnicodeError,
        MemoryError,
        psutil.Error,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        raise NativeArtifactInventoryError("native_freeze_worker_failed") from None
    finally:
        for descriptor_value in (ready_read, start_write):
            try:
                os.close(descriptor_value)
            except OSError:
                pass
        try:
            waited_pid, _status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == 0:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
        except (ChildProcessError, ProcessLookupError):
            pass
        shutil.rmtree(spool_root, ignore_errors=True)
