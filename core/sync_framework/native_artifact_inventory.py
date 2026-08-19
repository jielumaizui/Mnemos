"""Immutable, privacy-safe inventory for AgentSource parser inputs."""

from __future__ import annotations

import gc
import hashlib
import fcntl
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import psutil

from core.ops.durable_io import (
    DurableIOError,
    fsync_directory,
    fsync_regular_file,
    normalize_private_sqlite_copy,
    owned_sqlite_connection_pair,
    private_sqlite_sidecars,
)
from core.sync_framework.agent_source import (
    NativeSourceContractError,
    SessionInfo,
    SessionParseResult,
    Turn,
    canonicalize_session_info,
)
from core.ops.durable_io import (
    canonical_native_path,
    copy_native_file_to_descriptor,
    open_native_binary,
    read_native_bytes,
)
from core.sync_framework.native_artifact_bounded_parse import (  # noqa: F401
    bounded_parse_sources as _run_bounded_parse_sources,
    isolated_bounded_parse_spool,
    iter_spool_lines as _iter_spool_lines,
    read_bounded_parse_failure as _read_bounded_parse_failure,
)
from core.sync_framework.native_artifact_models import (
    DEFAULT_SNAPSHOT_PARSE_INFRASTRUCTURE_ATTEMPTS,
    FrozenAgentSource as _FrozenAgentSource,
    FrozenNativeSourceSet,
    INVENTORY_SCHEMA_VERSION,
    NativeArtifactEvidence,
    NativeArtifactInventory,
    NativeArtifactInventoryError,
    NativeSourceEvidence,
    SNAPSHOT_PARSE_RETRYABLE_ERROR_CODES,
    SNAPSHOT_PARSE_TERMINAL_ERROR_CODES,
    SnapshotNativeSourceSet,
    snapshot_parse_recovery_evidence_is_valid,
)
from core.sync_framework.native_sqlite import (
    NativeSQLiteReadError,
    connect_native_sqlite_readonly,
    native_storage_failure_evidence,
)

DEFAULT_FREEZE_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_FREEZE_MAX_TURNS = 1_000_000
DEFAULT_SNAPSHOT_MAX_SESSION_LOGICAL_BYTES = 512 * 1024 * 1024
DEFAULT_SNAPSHOT_MAX_SESSION_PARSE_BYTES = 1024 * 1024 * 1024
DEFAULT_SNAPSHOT_MAX_SESSION_TURNS = 1_000_000
DEFAULT_SNAPSHOT_STABILIZATION_ATTEMPTS = 3
_SNAPSHOT_REGISTRY_SCHEMA = "mnemos.native_artifact_snapshot_owner.v1"


def _content_free_storage_details(
    evidence: Mapping[str, Any],
    *,
    reason_code: str = "",
) -> dict[str, Any]:
    details = {
        key: value
        for key, value in dict(evidence).items()
        if key
        in {
            "failure_class",
            "os_errno",
            "sqlite_errorcode",
            "sqlite_errorname",
        }
    }
    if reason_code:
        details["reason_code"] = reason_code
    return details


def _storage_inventory_error(
    deterministic_code: str,
    failure: BaseException,
) -> NativeArtifactInventoryError:
    evidence = native_storage_failure_evidence(failure)
    return NativeArtifactInventoryError(
        (
            "native_storage_transient_failure"
            if evidence.get("retryable") is True
            else deterministic_code
        ),
        details=_content_free_storage_details(evidence),
    )


def _source_inventory_error(
    deterministic_code: str,
    failure: NativeSourceContractError,
) -> NativeArtifactInventoryError:
    return NativeArtifactInventoryError(
        ("native_source_transient_failure" if failure.retryable else deterministic_code),
        details=_content_free_storage_details(
            failure.details,
            reason_code=failure.code,
        ),
    )


def _physical_path_kind(path: Path, *, unavailable_code: str) -> str:
    """Inspect one snapshot-owned path without following links or hiding IO failure."""

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


def _snapshot_registry_root() -> Path:
    return Path(tempfile.gettempdir()).resolve() / "mnemos-native-artifact-snapshots-v1"


def _snapshot_owner_records(owner: Mapping[str, Any]) -> list[dict[str, Any]]:
    if owner.get("schema_version") != _SNAPSHOT_REGISTRY_SCHEMA:
        return []
    try:
        raw_owners = owner.get("owners")
        owners = (
            list(raw_owners)
            if isinstance(raw_owners, list)
            else [
                {
                    "pid": owner["pid"],
                    "process_create_time": owner["process_create_time"],
                }
            ]
        )
        return [
            {
                "pid": int(item["pid"]),
                "process_create_time": float(item["process_create_time"]),
                "role": str(item.get("role") or "controller"),
            }
            for item in owners
            if isinstance(item, Mapping)
        ]
    except (KeyError, TypeError, ValueError):
        return []


def _matching_owner_process(record: Mapping[str, Any]) -> psutil.Process | None:
    try:
        process = psutil.Process(int(record["pid"]))
        expected_start = float(record["process_create_time"])
        if abs(float(process.create_time()) - expected_start) < 0.001:
            return process
        return None
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    except psutil.AccessDenied:
        raise NativeArtifactInventoryError("native_snapshot_owner_unverifiable") from None


def _snapshot_owner_is_live(owner: Mapping[str, Any]) -> bool:
    records = _snapshot_owner_records(owner)
    if not records:
        return False
    controllers = [record for record in records if record.get("role") == "controller"]
    if not controllers:
        return False
    return any(_matching_owner_process(record) is not None for record in controllers)


def _terminate_abandoned_snapshot_workers(owner: Mapping[str, Any]) -> None:
    for record in _snapshot_owner_records(owner):
        if record.get("role") != "worker":
            continue
        process = _matching_owner_process(record)
        if process is None:
            continue
        try:
            process.terminate()
            process.wait(timeout=2)
        except psutil.TimeoutExpired:
            try:
                process.kill()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.AccessDenied:
                raise NativeArtifactInventoryError("native_snapshot_owner_unverifiable") from None
            try:
                process.wait(timeout=2)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.TimeoutExpired:
                raise NativeArtifactInventoryError(
                    "native_snapshot_worker_termination_failed"
                ) from None
            except psutil.AccessDenied:
                raise NativeArtifactInventoryError("native_snapshot_owner_unverifiable") from None
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            raise NativeArtifactInventoryError("native_snapshot_owner_unverifiable") from None


def _safe_snapshot_registry_root() -> Path:
    root = _snapshot_registry_root()
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = root.lstat()
    except OSError:
        raise NativeArtifactInventoryError("native_snapshot_registry_unavailable") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise NativeArtifactInventoryError("native_snapshot_registry_unsafe")
    os.chmod(root, 0o700)
    return root


@contextmanager
def _snapshot_registry_lock() -> Iterator[Path]:
    root = _safe_snapshot_registry_root()
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(root / ".registry.lock", flags, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError:
        raise NativeArtifactInventoryError("native_snapshot_registry_unavailable") from None
    try:
        yield root
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _cleanup_stale_snapshot_dirs_locked(root: Path) -> int:
    cleaned = 0
    try:
        children = list(root.iterdir())
    except OSError:
        raise NativeArtifactInventoryError("native_snapshot_registry_unavailable") from None
    for child in children:
        if not child.name.startswith("snapshot-"):
            continue
        try:
            child_kind = _physical_path_kind(
                child,
                unavailable_code="native_snapshot_registry_unsafe",
            )
            if child_kind != "directory":
                raise NativeArtifactInventoryError("native_snapshot_registry_unsafe")
            marker = child / ".owner.json"
            marker_kind = _physical_path_kind(
                marker,
                unavailable_code="native_snapshot_owner_invalid",
            )
            if marker_kind in {"symlink", "directory", "other"}:
                raise NativeArtifactInventoryError("native_snapshot_owner_invalid")
            if marker_kind == "file":
                owner = json.loads(read_native_bytes(marker).decode("utf-8"))
                if not isinstance(owner, Mapping):
                    raise NativeArtifactInventoryError("native_snapshot_owner_invalid")
                if not _snapshot_owner_records(owner):
                    raise NativeArtifactInventoryError("native_snapshot_owner_invalid")
                if _snapshot_owner_is_live(owner):
                    continue
                if owner.get("schema_version") != _SNAPSHOT_REGISTRY_SCHEMA:
                    raise NativeArtifactInventoryError("native_snapshot_owner_invalid")
                _terminate_abandoned_snapshot_workers(owner)
            shutil.rmtree(child)
            cleaned += 1
        except NativeArtifactInventoryError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise NativeArtifactInventoryError("native_snapshot_owner_invalid") from None
    return cleaned


def _create_registered_snapshot_root() -> tuple[Path, int]:
    with _snapshot_registry_lock() as registry_root:
        cleaned = _cleanup_stale_snapshot_dirs_locked(registry_root)
        try:
            snapshot_root = Path(tempfile.mkdtemp(prefix="snapshot-", dir=registry_root)).resolve()
            os.chmod(snapshot_root, 0o700)
            _write_snapshot_owner_marker(snapshot_root, (os.getpid(),))
        except NativeArtifactInventoryError:
            if "snapshot_root" in locals():
                shutil.rmtree(snapshot_root, ignore_errors=True)
            raise
        except (OSError, psutil.Error):
            if "snapshot_root" in locals():
                shutil.rmtree(snapshot_root, ignore_errors=True)
            raise NativeArtifactInventoryError("native_snapshot_registry_unavailable") from None
    return snapshot_root, cleaned


def _write_snapshot_owner_marker(
    snapshot_root: Path,
    owner_pids: tuple[int, ...],
) -> None:
    owner_path = snapshot_root / ".owner.json"
    temporary = snapshot_root / (f".owner.{os.getpid()}.{time.time_ns()}.tmp")
    temporary_created = False
    try:
        owners = [
            {
                "pid": int(pid),
                "process_create_time": psutil.Process(pid).create_time(),
                "role": "controller" if index == 0 else "worker",
            }
            for index, pid in enumerate(owner_pids)
        ]
        owner = {
            "schema_version": _SNAPSHOT_REGISTRY_SCHEMA,
            "owners": owners,
        }
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        temporary_created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(owner, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, owner_path)
        fsync_directory(snapshot_root)
    except (OSError, psutil.Error):
        raise NativeArtifactInventoryError("native_snapshot_registry_unavailable") from None
    finally:
        if temporary_created:
            temporary.unlink(missing_ok=True)


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _file_snapshot(path: Path) -> tuple[str, int]:
    try:
        digest = hashlib.sha256()
        size = 0
        with open_native_binary(path) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        return f"sha256:{digest.hexdigest()}", size
    except OSError as exc:
        raise _storage_inventory_error(
            "native_file_snapshot_failed",
            exc,
        ) from None


def _is_sqlite(path: Path) -> bool:
    try:
        with open_native_binary(path) as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError as exc:
        raise _storage_inventory_error(
            "native_sqlite_header_read_failed",
            exc,
        ) from None


def _sqlite_snapshot(path: Path) -> tuple[str, int]:
    try:
        source = connect_native_sqlite_readonly(path)
        try:
            source.execute("BEGIN")
            digest = hashlib.sha256()
            size = 0
            for statement in source.iterdump():
                encoded = statement.encode("utf-8")
                digest.update(encoded)
                digest.update(b"\n")
                size += len(encoded) + 1
            return f"sha256:{digest.hexdigest()}", size
        finally:
            source.close()
    except (NativeSQLiteReadError, OSError, sqlite3.Error) as exc:
        raise _storage_inventory_error(
            "sqlite_snapshot_failed",
            exc,
        ) from None


def _path_in_root(path: Path, roots: tuple[Path, ...]) -> tuple[Path, Path]:
    root = _artifact_root(path, roots)
    try:
        return root, path.relative_to(root)
    except ValueError:
        raise NativeArtifactInventoryError("native_artifact_outside_declared_roots") from None


def _remap_path(
    path: Path,
    root_pairs: tuple[tuple[Path, Path], ...],
) -> Path:
    candidates: list[tuple[int, Path]] = []
    resolved = path.expanduser().resolve(strict=False)
    for source_root, target_root in root_pairs:
        try:
            relative = resolved.relative_to(source_root)
        except ValueError:
            continue
        candidates.append((len(source_root.parts), target_root / relative))
    if not candidates:
        raise NativeArtifactInventoryError("native_artifact_outside_declared_roots")
    return max(candidates, key=lambda item: item[0])[1]


def _remap_structured_paths(
    value: Any,
    root_pairs: tuple[tuple[Path, Path], ...],
) -> Any:
    if isinstance(value, str) and os.path.isabs(value):
        try:
            return str(_remap_path(Path(value), root_pairs))
        except NativeArtifactInventoryError:
            return value
    if isinstance(value, list):
        return [_remap_structured_paths(item, root_pairs) for item in value]
    if isinstance(value, tuple):
        return tuple(_remap_structured_paths(item, root_pairs) for item in value)
    if isinstance(value, dict):
        return {key: _remap_structured_paths(item, root_pairs) for key, item in value.items()}
    return value


def _remap_path_if_owned(
    path: Path,
    root_pairs: tuple[tuple[Path, Path], ...],
) -> Path:
    """Remap parser-owned paths while preserving unrelated project paths."""

    try:
        return _remap_path(path, root_pairs)
    except NativeArtifactInventoryError:
        return path


def _normalize_private_sqlite_snapshot(target: Path) -> None:
    """Make one private SQLite backup physically immutable to read-only parsers."""

    try:
        normalize_private_sqlite_copy(target)
    except DurableIOError:
        raise NativeArtifactInventoryError("native_sqlite_snapshot_normalization_failed") from None


def _copy_snapshot_artifact(source: Path, target: Path) -> tuple[str, int]:
    created = False
    completed = False
    descriptor = -1
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target_kind = _physical_path_kind(
            target,
            unavailable_code="native_artifact_snapshot_failed",
        )
        if target_kind == "file":
            return _sqlite_snapshot(target) if _is_sqlite(target) else _file_snapshot(target)
        if target_kind != "missing":
            raise NativeArtifactInventoryError("native_artifact_snapshot_failed")
        descriptor = os.open(
            target,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        if _is_sqlite(source):
            os.close(descriptor)
            descriptor = -1
            with owned_sqlite_connection_pair(
                lambda: connect_native_sqlite_readonly(source),
                lambda: sqlite3.connect(str(target)),
            ) as (source_connection, target_connection):
                source_connection.backup(target_connection)
            _normalize_private_sqlite_snapshot(target)
        else:
            copy_native_file_to_descriptor(source, descriptor)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
        os.chmod(target, 0o600)
        fsync_regular_file(target)
        fsync_directory(target.parent)
        completed = True
    except NativeArtifactInventoryError:
        raise
    except DurableIOError:
        raise NativeArtifactInventoryError("native_artifact_snapshot_failed") from None
    except (NativeSQLiteReadError, OSError, sqlite3.Error) as exc:
        raise _storage_inventory_error(
            "native_artifact_snapshot_failed",
            exc,
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if created and not completed:
            for candidate in (*private_sqlite_sidecars(target), target):
                candidate.unlink(missing_ok=True)
    return _sqlite_snapshot(target) if _is_sqlite(target) else _file_snapshot(target)


class _SnapshotAgentSource:
    """Delegate parsing to one source while substituting immutable artifacts."""

    def __init__(
        self,
        source: Any,
        original_sessions: list[SessionInfo],
        snapshot_sessions: Mapping[str, SessionInfo],
        forward_roots: tuple[tuple[Path, Path], ...],
        max_session_parse_bytes: int,
        max_session_turns: int,
    ) -> None:
        self._source = source
        self._original_sessions = tuple(original_sessions)
        self._snapshot_sessions = dict(snapshot_sessions)
        self._forward_roots = forward_roots
        self._original_roots = tuple(
            original_root for original_root, _snapshot_root in forward_roots
        )
        self._max_session_parse_bytes = max_session_parse_bytes
        self._max_session_turns = max_session_turns
        self._reverse_roots = tuple(
            (snapshot_root, original_root) for original_root, snapshot_root in forward_roots
        )
        self._native_challenger_identity_isolation = True
        self.name = str(source.name)
        self.model_tag = str(source.model_tag)

    def discover_sessions(self) -> list[SessionInfo]:
        return list(self._original_sessions)

    def parse_session(self, session_info: SessionInfo) -> list[Turn]:
        return list(self.parse_session_result(session_info).turns)

    def parse_session_result(
        self,
        session_info: SessionInfo,
    ) -> SessionParseResult:
        canonical = canonicalize_session_info(session_info)
        snapshot = self._snapshot_sessions.get(canonical.session_id)
        if snapshot is None:
            raise NativeArtifactInventoryError(
                "snapshot_session_identity_missing",
                details={"attempt_count": 1},
            )
        recovered_infrastructure_failure: dict[str, Any] = {}
        for attempt in range(
            1,
            DEFAULT_SNAPSHOT_PARSE_INFRASTRUCTURE_ATTEMPTS + 1,
        ):
            try:
                results_by_source, _turn_count, _estimated_bytes = _bounded_parse_sources(
                    sources=[self._source],
                    sessions_by_source=[[snapshot]],
                    max_bytes=self._max_session_parse_bytes,
                    max_turns=self._max_session_turns,
                )
                break
            except NativeArtifactInventoryError as exc:
                details = dict(exc.details)
                details["attempt_count"] = attempt
                if (
                    exc.code not in SNAPSHOT_PARSE_RETRYABLE_ERROR_CODES
                    or attempt >= DEFAULT_SNAPSHOT_PARSE_INFRASTRUCTURE_ATTEMPTS
                ):
                    if exc.code not in SNAPSHOT_PARSE_TERMINAL_ERROR_CODES:
                        reason_code = (
                            exc.code
                            if re.fullmatch(
                                r"[a-z][a-z0-9_]{2,127}",
                                exc.code,
                            )
                            else "native_unregistered_parse_failure"
                        )
                        raise NativeArtifactInventoryError(
                            "native_parse_terminal_error_unregistered",
                            details={
                                "attempt_count": attempt,
                                "reason_code": reason_code,
                            },
                        ) from None
                    raise NativeArtifactInventoryError(
                        exc.code,
                        details=details,
                    ) from None
                recovered_infrastructure_failure = {
                    key: value
                    for key, value in details.items()
                    if key
                    in {
                        "exception_type",
                        "failure_class",
                        "os_errno",
                        "reason_code",
                        "signal",
                        "sqlite_errorcode",
                        "sqlite_errorname",
                    }
                }
                recovered_infrastructure_failure["error_code"] = exc.code
                if not snapshot_parse_recovery_evidence_is_valid(recovered_infrastructure_failure):
                    raise NativeArtifactInventoryError(
                        "native_parse_recovery_evidence_invalid",
                        details={
                            "attempt_count": attempt,
                            "reason_code": exc.code,
                        },
                    ) from None
                gc.collect()
        else:  # pragma: no cover - range is statically non-empty
            raise NativeArtifactInventoryError("native_freeze_worker_failed")
        result = results_by_source[0].get(snapshot.session_id)
        if result is None:
            raise NativeArtifactInventoryError(
                "snapshot_session_identity_missing",
                details={"attempt_count": attempt},
            )
        turns = [
            replace(
                turn,
                metadata=_remap_structured_paths(
                    dict(turn.metadata or {}),
                    self._reverse_roots,
                ),
                tool_calls=_remap_structured_paths(
                    list(turn.tool_calls or []),
                    self._reverse_roots,
                ),
                tool_results=_remap_structured_paths(
                    list(turn.tool_results or []),
                    self._reverse_roots,
                ),
                attachments=_remap_structured_paths(
                    list(turn.attachments or []),
                    self._reverse_roots,
                ),
                raw_event_refs=_remap_structured_paths(
                    list(turn.raw_event_refs or []),
                    self._reverse_roots,
                ),
                source_files=[
                    (
                        str(_remap_path_if_owned(Path(path), self._reverse_roots))
                        if os.path.isabs(path)
                        else path
                    )
                    for path in (turn.source_files or [])
                ],
                completeness=_remap_structured_paths(
                    dict(turn.completeness or {}),
                    self._reverse_roots,
                ),
            )
            for turn in result.turns
        ]
        evidence_hash = str(
            (snapshot.metadata or {}).get("native_session_artifact_evidence_hash")
            or result.artifact_evidence_hash
            or ""
        )
        return SessionParseResult(
            turns=tuple(turns),
            disposition=result.disposition,
            reason_code=result.reason_code,
            artifact_evidence_hash=evidence_hash,
            infrastructure_attempt_count=attempt,
            recovered_infrastructure_failure=(recovered_infrastructure_failure),
        )

    def _framework_bound_session_artifact_evidence_hash(
        self,
        session_info: SessionInfo,
    ) -> str:
        """Return the inventory-owned evidence bound to the private snapshot."""

        canonical = canonicalize_session_info(session_info)
        snapshot = self._snapshot_sessions.get(canonical.session_id)
        if snapshot is None:
            raise NativeArtifactInventoryError("snapshot_session_identity_missing")
        return str((snapshot.metadata or {}).get("native_session_artifact_evidence_hash") or "")

    def parse_turns(self, session_path: Path) -> list[Turn]:
        matches = [
            session for session in self._original_sessions if session.source_path == session_path
        ]
        if len(matches) != 1:
            raise NativeArtifactInventoryError("snapshot_session_identity_missing")
        return self.parse_session(matches[0])

    @property
    def data_dir(self) -> Path:
        if not self._original_roots:
            raise NativeArtifactInventoryError("native_root_unresolvable")
        return self._original_roots[0]

    def observed_roots(self) -> list[Path]:
        return list(self._original_roots)

    def snapshot_read_roots(self) -> list[Path]:
        """Return exact private immutable roots authorized for parser reads."""

        return [snapshot_root for _original_root, snapshot_root in self._forward_roots]

    def native_artifact_paths(self, session_info: SessionInfo) -> list[Path]:
        return list(self._source.native_artifact_paths(session_info) or [])

    def completeness_capabilities(self) -> dict[str, Any]:
        return dict(self._source.completeness_capabilities())

    def build_extra_tags(self, turn: Turn) -> list[str]:
        return list(self._source.build_extra_tags(turn))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)


@contextmanager
def _snapshot_native_sources_once(
    sources: Iterable[Any],
    *,
    max_session_logical_bytes: int = DEFAULT_SNAPSHOT_MAX_SESSION_LOGICAL_BYTES,
    max_session_parse_bytes: int = DEFAULT_SNAPSHOT_MAX_SESSION_PARSE_BYTES,
    max_session_turns: int = DEFAULT_SNAPSHOT_MAX_SESSION_TURNS,
) -> Iterator[SnapshotNativeSourceSet]:
    """Create exact private parser inputs without materializing every turn."""

    if max_session_logical_bytes <= 0 or max_session_parse_bytes <= 0 or max_session_turns <= 0:
        raise NativeArtifactInventoryError("native_snapshot_session_budget_invalid")
    source_list = tuple(sources)
    before = build_native_artifact_inventory(source_list)
    expected_entries = {
        (
            entry.source_name,
            entry.canonical_session_id,
            entry.artifact_identity_hash,
        ): entry
        for entry in before.entries
    }
    snapshot_root, stale_snapshot_dirs_cleaned = _create_registered_snapshot_root()
    wrappers: list[Any] = []
    copied_targets: set[Path] = set()
    try:
        for source_index, source in enumerate(source_list):
            source_name = str(getattr(source, "name", "") or "")
            try:
                sessions = list(source.discover_sessions() or [])
            except NativeSourceContractError as exc:
                raise _source_inventory_error(
                    "native_discovery_failed",
                    exc,
                ) from None
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
                raise NativeArtifactInventoryError("native_discovery_failed") from None
            try:
                declared_paths = [
                    canonical_native_path(Path(path))
                    for session in sessions
                    for path in (source.native_artifact_paths(session) or [])
                ]
            except NativeSourceContractError as exc:
                raise _source_inventory_error(
                    "native_artifact_declaration_failed",
                    exc,
                ) from None
            roots = _source_roots(source, declared_paths)
            root_pairs = tuple(
                (
                    root,
                    snapshot_root / f"{source_index:02d}-{source_name}" / f"root-{root_index:02d}",
                )
                for root_index, root in enumerate(roots)
            )
            for _original_root, target_root in root_pairs:
                target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            snapshot_sessions: dict[str, SessionInfo] = {}
            seen_session_ids: set[str] = set()
            for session in sessions:
                canonical = canonicalize_session_info(session)
                if canonical.session_id in seen_session_ids:
                    raise NativeArtifactInventoryError("canonical_session_duplicate")
                seen_session_ids.add(canonical.session_id)
                try:
                    artifact_paths = [
                        canonical_native_path(Path(path))
                        for path in (source.native_artifact_paths(session) or [])
                    ]
                except NativeSourceContractError as exc:
                    raise _source_inventory_error(
                        "native_artifact_declaration_failed",
                        exc,
                    ) from None
                if not artifact_paths:
                    raise NativeArtifactInventoryError("native_artifact_roster_empty")
                session_logical_bytes = 0
                session_artifact_evidence: list[dict[str, Any]] = []
                for artifact_path in artifact_paths:
                    original_root, _relative = _path_in_root(artifact_path, roots)
                    target_path = _remap_path(artifact_path, root_pairs)
                    root_hash = _canonical_hash(
                        {"source_name": source_name, "root": str(original_root)}
                    )
                    identity_hash = _canonical_hash(
                        {
                            "source_name": source_name,
                            "canonical_session_id": canonical.session_id,
                            "root_hash": root_hash,
                            "resolved_path": str(artifact_path),
                        }
                    )
                    expected = expected_entries.get(
                        (source_name, canonical.session_id, identity_hash)
                    )
                    if expected is None:
                        raise NativeArtifactInventoryError(
                            "native_artifact_snapshot_binding_missing"
                        )
                    content_hash, logical_size = _copy_snapshot_artifact(
                        artifact_path,
                        target_path,
                    )
                    copied_targets.add(target_path)
                    if (
                        content_hash != expected.content_hash
                        or logical_size != expected.logical_size_bytes
                    ):
                        raise NativeArtifactInventoryError("native_artifact_drift_during_snapshot")
                    session_artifact_evidence.append(
                        {
                            "artifact_identity_hash": expected.artifact_identity_hash,
                            "content_hash": expected.content_hash,
                            "logical_size_bytes": expected.logical_size_bytes,
                        }
                    )
                    session_logical_bytes += logical_size
                if session_logical_bytes > max_session_logical_bytes:
                    raise NativeArtifactInventoryError("native_snapshot_session_budget_exceeded")
                snapshot_source_path = _remap_path(
                    canonical.source_path,
                    root_pairs,
                )
                try:
                    source_path_kind = _physical_path_kind(
                        canonical.source_path,
                        unavailable_code="native_artifact_drift_during_snapshot",
                    )
                except NativeArtifactInventoryError:
                    raise NativeArtifactInventoryError(
                        "native_artifact_drift_during_snapshot"
                    ) from None
                if source_path_kind == "directory":
                    snapshot_source_path.mkdir(
                        parents=True,
                        exist_ok=True,
                        mode=0o700,
                    )
                elif source_path_kind != "file":
                    raise NativeArtifactInventoryError("native_artifact_drift_during_snapshot")
                snapshot_sessions[canonical.session_id] = replace(
                    canonical,
                    source_path=snapshot_source_path,
                    source_paths=[
                        _remap_path_if_owned(path, root_pairs)
                        for path in (canonical.source_paths or [])
                    ],
                    working_dir=(
                        str(
                            _remap_path_if_owned(
                                Path(canonical.working_dir),
                                root_pairs,
                            )
                        )
                        if canonical.working_dir and os.path.isabs(canonical.working_dir)
                        else canonical.working_dir
                    ),
                    metadata={
                        **_remap_structured_paths(
                            dict(canonical.metadata or {}),
                            root_pairs,
                        ),
                        "native_session_artifact_evidence_hash": _canonical_hash(
                            sorted(
                                session_artifact_evidence,
                                key=lambda item: item["artifact_identity_hash"],
                            )
                        ),
                    },
                )
            wrappers.append(
                _SnapshotAgentSource(
                    source,
                    sessions,
                    snapshot_sessions,
                    root_pairs,
                    max_session_parse_bytes,
                    max_session_turns,
                )
            )
        after = build_native_artifact_inventory(source_list)
        if after.inventory_hash != before.inventory_hash:
            raise NativeArtifactInventoryError("native_artifact_drift_during_snapshot")
        yield SnapshotNativeSourceSet(
            sources=tuple(wrappers),
            inventory=before,
            snapshot_logical_bytes=sum(entry.logical_size_bytes for entry in before.entries),
            snapshot_artifact_count=len(copied_targets),
            max_session_logical_bytes=max_session_logical_bytes,
            max_session_parse_bytes=max_session_parse_bytes,
            max_session_turns=max_session_turns,
            stale_snapshot_dirs_cleaned=stale_snapshot_dirs_cleaned,
        )
    finally:
        shutil.rmtree(snapshot_root, ignore_errors=True)


@contextmanager
def snapshot_native_sources(
    sources: Iterable[Any],
    *,
    max_session_logical_bytes: int = DEFAULT_SNAPSHOT_MAX_SESSION_LOGICAL_BYTES,
    max_session_parse_bytes: int = DEFAULT_SNAPSHOT_MAX_SESSION_PARSE_BYTES,
    max_session_turns: int = DEFAULT_SNAPSHOT_MAX_SESSION_TURNS,
    max_stabilization_attempts: int = DEFAULT_SNAPSHOT_STABILIZATION_ATTEMPTS,
) -> Iterator[SnapshotNativeSourceSet]:
    """Yield one coherent generation after bounded whole-snapshot retries.

    Agent histories are append-only but the currently active host may append
    once after a reconciliation process starts. A drifted candidate is never
    exposed: its entire private snapshot is discarded and both the inventory
    and copies are rebuilt. Continuous drift remains a typed hard failure.
    """

    if max_stabilization_attempts <= 0:
        raise NativeArtifactInventoryError("native_snapshot_stabilization_budget_invalid")
    source_list = tuple(sources)
    for attempt in range(1, max_stabilization_attempts + 1):
        stack = ExitStack()
        try:
            snapshot = stack.enter_context(
                _snapshot_native_sources_once(
                    source_list,
                    max_session_logical_bytes=max_session_logical_bytes,
                    max_session_parse_bytes=max_session_parse_bytes,
                    max_session_turns=max_session_turns,
                )
            )
        except NativeArtifactInventoryError as exc:
            stack.close()
            if (
                exc.code
                in {
                    "native_artifact_drift_during_snapshot",
                    "native_source_transient_failure",
                    "native_storage_transient_failure",
                }
                and attempt < max_stabilization_attempts
            ):
                gc.collect()
                continue
            if exc.code in {
                "native_source_transient_failure",
                "native_storage_transient_failure",
            }:
                raise NativeArtifactInventoryError(
                    exc.code,
                    details={
                        **dict(exc.details),
                        "attempt_count": attempt,
                    },
                ) from None
            raise
        with stack:
            yield replace(snapshot, stabilization_attempts=attempt)
        return
    raise NativeArtifactInventoryError("native_artifact_drift_during_snapshot")


def _source_roots(source: Any, paths: list[Path]) -> tuple[Path, ...]:
    observed = getattr(source, "observed_roots", None)
    roots: list[Path] = []
    if callable(observed):
        try:
            roots.extend(Path(root).expanduser().resolve(strict=False) for root in observed())
        except NativeSourceContractError as exc:
            raise _source_inventory_error(
                "native_root_unresolvable",
                exc,
            ) from None
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
            raise NativeArtifactInventoryError("native_root_unresolvable") from None
    try:
        configured = getattr(source, "data_dir", None)
    except NativeSourceContractError as exc:
        raise _source_inventory_error(
            "native_root_unresolvable",
            exc,
        ) from None
    if isinstance(configured, Path):
        roots.append(configured.expanduser().resolve(strict=False))
    roots = list(dict.fromkeys(roots))
    if roots:
        for root in roots:
            try:
                root_kind = _physical_path_kind(
                    root,
                    unavailable_code="native_root_unresolvable",
                )
            except NativeArtifactInventoryError:
                raise NativeArtifactInventoryError("native_root_unresolvable") from None
            if root_kind != "directory":
                raise NativeArtifactInventoryError("native_root_not_detected")
        return tuple(roots)
    if not paths:
        raise NativeArtifactInventoryError("native_root_not_detected")
    try:
        return (Path(os.path.commonpath([str(path.parent) for path in paths])).resolve(),)
    except (OSError, ValueError):
        raise NativeArtifactInventoryError("native_root_unresolvable") from None


def _artifact_root(path: Path, roots: tuple[Path, ...]) -> Path:
    candidates: list[Path] = []
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        candidates.append(root)
    if not candidates:
        raise NativeArtifactInventoryError("native_artifact_outside_declared_roots")
    return max(candidates, key=lambda candidate: len(candidate.parts))


def _bounded_parse_sources(
    *,
    sources: list[Any],
    sessions_by_source: list[list[SessionInfo]],
    max_bytes: int,
    max_turns: int,
) -> tuple[list[dict[str, SessionParseResult]], int, int]:
    """Run bounded parsing with the current snapshot ownership callbacks."""
    return _run_bounded_parse_sources(
        sources=sources,
        sessions_by_source=sessions_by_source,
        max_bytes=max_bytes,
        max_turns=max_turns,
        create_registered_snapshot_root=_create_registered_snapshot_root,
        write_snapshot_owner_marker=_write_snapshot_owner_marker,
    )


def build_native_artifact_inventory(
    sources: Iterable[Any],
) -> NativeArtifactInventory:
    """Build a deterministic inventory from each parser's declared inputs."""
    entries: list[NativeArtifactEvidence] = []
    source_evidence: list[NativeSourceEvidence] = []
    for source in sources:
        source_name = str(getattr(source, "name", "") or "")
        try:
            sessions = list(source.discover_sessions() or [])
        except NativeSourceContractError as exc:
            raise _source_inventory_error(
                "native_discovery_failed",
                exc,
            ) from None
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            raise NativeArtifactInventoryError("native_discovery_failed") from None
        declared: list[tuple[SessionInfo, Path]] = []
        for session in sessions:
            canonical_session_id = str(session.canonical_session_id or session.session_id or "")
            if not canonical_session_id:
                raise NativeArtifactInventoryError("canonical_session_id_missing")
            try:
                paths = list(source.native_artifact_paths(session) or [])
            except NativeSourceContractError as exc:
                raise _source_inventory_error(
                    "native_artifact_declaration_failed",
                    exc,
                ) from None
            except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
                raise NativeArtifactInventoryError("native_artifact_declaration_failed") from None
            if not paths:
                raise NativeArtifactInventoryError("native_artifact_roster_empty")
            for raw_path in paths:
                try:
                    path = canonical_native_path(Path(raw_path))
                    path_kind = _physical_path_kind(
                        path,
                        unavailable_code="native_artifact_unreadable",
                    )
                except (NativeArtifactInventoryError, OSError, RuntimeError):
                    raise NativeArtifactInventoryError("native_artifact_unreadable") from None
                if path_kind != "file":
                    raise NativeArtifactInventoryError("native_artifact_unreadable")
                declared.append((session, path))
        roots = _source_roots(source, [path for _session, path in declared])
        source_evidence.append(
            NativeSourceEvidence(
                source_name=source_name,
                root_identity_hashes=tuple(
                    sorted(
                        _canonical_hash(
                            {
                                "source_name": source_name,
                                "root": str(root),
                            }
                        )
                        for root in roots
                    )
                ),
                session_count=len(sessions),
                artifact_count=len(declared),
            )
        )
        seen: set[tuple[str, str]] = set()
        for session, path in declared:
            canonical_session_id = str(session.canonical_session_id or session.session_id)
            root = _artifact_root(path, roots)
            root_hash = _canonical_hash({"source_name": source_name, "root": str(root)})
            identity_hash = _canonical_hash(
                {
                    "source_name": source_name,
                    "canonical_session_id": canonical_session_id,
                    "root_hash": root_hash,
                    "resolved_path": str(path),
                }
            )
            dedupe_key = (canonical_session_id, identity_hash)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            sqlite_file = _is_sqlite(path)
            content_hash, logical_size_bytes = (
                _sqlite_snapshot(path) if sqlite_file else _file_snapshot(path)
            )
            entries.append(
                NativeArtifactEvidence(
                    source_name=source_name,
                    canonical_session_id=canonical_session_id,
                    artifact_identity_hash=identity_hash,
                    content_hash=content_hash,
                    logical_size_bytes=logical_size_bytes,
                    hash_contract=("sqlite-logical-dump-v1" if sqlite_file else "file-bytes-v1"),
                )
            )
    ordered = tuple(
        sorted(
            entries,
            key=lambda item: (
                item.source_name,
                item.canonical_session_id,
                item.artifact_identity_hash,
            ),
        )
    )
    evidence = [entry.to_dict() for entry in ordered]
    ordered_sources = tuple(sorted(source_evidence, key=lambda item: item.source_name))
    source_roster = [source.to_dict() for source in ordered_sources]
    return NativeArtifactInventory(
        entries=ordered,
        sources=ordered_sources,
        inventory_hash=_canonical_hash(
            {
                "schema_version": INVENTORY_SCHEMA_VERSION,
                "sources": source_roster,
                "entries": evidence,
            }
        ),
    )


def freeze_native_sources(
    sources: Iterable[Any],
    *,
    max_bytes: int = DEFAULT_FREEZE_MAX_BYTES,
    max_turns: int = DEFAULT_FREEZE_MAX_TURNS,
) -> FrozenNativeSourceSet:
    """Parse a stable native snapshot before any migration mutation occurs."""
    if max_bytes <= 0 or max_turns <= 0:
        raise NativeArtifactInventoryError("native_freeze_budget_invalid")
    source_list = list(sources)
    before = build_native_artifact_inventory(source_list)
    preparse_logical_bytes = sum(entry.logical_size_bytes for entry in before.entries)
    sessions_by_source: list[list[SessionInfo]] = []
    roots_by_source: list[tuple[Path, ...]] = []
    for source in source_list:
        try:
            sessions = list(source.discover_sessions() or [])
        except NativeSourceContractError as exc:
            raise _source_inventory_error(
                "native_discovery_failed",
                exc,
            ) from None
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            raise NativeArtifactInventoryError("native_discovery_failed") from None
        canonical_sessions: list[SessionInfo] = []
        declared_paths: list[Path] = []
        for session in sessions:
            canonical = canonicalize_session_info(session)
            if any(item.session_id == canonical.session_id for item in canonical_sessions):
                raise NativeArtifactInventoryError("canonical_session_duplicate")
            try:
                declared_paths.extend(
                    Path(path).expanduser().resolve()
                    for path in source.native_artifact_paths(session)
                )
            except NativeSourceContractError as exc:
                raise _source_inventory_error(
                    "native_artifact_declaration_failed",
                    exc,
                ) from None
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
                RuntimeError,
            ):
                raise NativeArtifactInventoryError("native_artifact_declaration_failed") from None
            canonical_sessions.append(canonical)
        sessions_by_source.append(canonical_sessions)
        roots_by_source.append(_source_roots(source, declared_paths))
    results_by_source, frozen_turn_count, estimated_bytes = _bounded_parse_sources(
        sources=source_list,
        sessions_by_source=sessions_by_source,
        max_bytes=max_bytes,
        max_turns=max_turns,
    )
    frozen: list[Any] = []
    for source_index, source in enumerate(source_list):
        frozen.append(
            _FrozenAgentSource(
                source,
                sessions_by_source[source_index],
                results_by_source[source_index],
                roots_by_source[source_index],
            )
        )
    after = build_native_artifact_inventory(source_list)
    if after.inventory_hash != before.inventory_hash:
        raise NativeArtifactInventoryError("native_artifact_drift_during_freeze")
    return FrozenNativeSourceSet(
        sources=tuple(frozen),
        inventory=before,
        frozen_turn_count=frozen_turn_count,
        estimated_bytes=estimated_bytes,
        max_bytes=max_bytes,
        max_turns=max_turns,
        preparse_logical_bytes=preparse_logical_bytes,
    )


__all__ = [
    "INVENTORY_SCHEMA_VERSION",
    "DEFAULT_FREEZE_MAX_BYTES",
    "DEFAULT_FREEZE_MAX_TURNS",
    "DEFAULT_SNAPSHOT_MAX_SESSION_LOGICAL_BYTES",
    "DEFAULT_SNAPSHOT_MAX_SESSION_PARSE_BYTES",
    "DEFAULT_SNAPSHOT_MAX_SESSION_TURNS",
    "NativeArtifactEvidence",
    "NativeSourceEvidence",
    "NativeArtifactInventory",
    "NativeArtifactInventoryError",
    "FrozenNativeSourceSet",
    "SnapshotNativeSourceSet",
    "build_native_artifact_inventory",
    "freeze_native_sources",
    "snapshot_native_sources",
]
