"""Support primitives for the bounded Agent Native-to-Raw recovery."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, NoReturn

from core.agent_kit.source_support_manifest import (
    AgentSourceSupportManifestError,
    get_agent_source_support_manifest,
)
from core.ops.durable_io import (
    DurableIOError,
    fsync_directory,
    fsync_regular_file,
    inspect_path_kind,
    normalize_private_sqlite_copy,
    owned_sqlite_connection_pair,
    private_sqlite_sidecars,
    regular_file_sha256,
    secure_read_bytes,
    secure_regular_file_preimage,
    validate_private_sqlite_copy,
)
from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.ops.runtime_execution_identity import runtime_execution_identity
from core.sync_framework.agent_source import (
    canonicalize_session_info,
    parse_discovered_session,
)
from core.sync_framework.native_artifact_inventory import (
    NativeArtifactInventoryError,
    SnapshotNativeSourceSet,
)
from core.sync_framework.native_artifact_models import (
    SNAPSHOT_PARSE_TERMINAL_ERROR_CODES,
    SNAPSHOT_PARSE_TERMINAL_EVIDENCE_KEYS,
    snapshot_parse_recovery_evidence_is_valid,
    snapshot_parse_terminal_evidence_is_valid,
)
from core.sync_framework.registry import SourceRegistry
from scripts.agent_source_raw_recovery_contract import (
    AgentSourceRawReconciliationError,
    SCHEMA_VERSION,
)
from scripts.agent_source_raw_worker_sandbox import (  # noqa: F401
    _ProcessDatabaseWriteScope,
    _audit_event_has_ambiguous_relative_open,
    _audit_event_path,
    _audit_event_requires_write_attribution,
    _audit_event_write_paths,
    _audit_open_write_requested,
    _close_inherited_regular_file_descriptors,
    _create_recovery_worker_root,
    _descriptor_target_path,
    _ensure_process_write_audit_hook,
    _install_worker_filesystem_sandbox,
    _process_write_audit_hook,
    _recovery_worker_owner_is_live,
    _safe_recovery_worker_registry_root,
)

ROOT = Path(__file__).resolve().parents[1]
RECOVERY_EXECUTION_DEPENDENCY_PATHS = (
    "scripts/reconcile_agent_source_raw_capture.py",
    "scripts/agent_source_raw_migration_certification.py",
    "scripts/agent_source_raw_migration_runtime.py",
    "scripts/agent_source_raw_recovery_contract.py",
    "scripts/agent_source_raw_reconciliation_support.py",
    "scripts/agent_source_raw_reconciliation_cli.py",
    "scripts/agent_source_raw_recovery_support.py",
    "scripts/agent_source_raw_worker_sandbox.py",
    "scripts/agent_source_raw_worker_runtime.py",
    "core/sync_framework/raw_current_projection_reconciliation.py",
    "core/agent_kit/native_raw_challenger.py",
    "core/agent_kit/source_capture_verification.py",
    "core/agent_kit/source_support_manifest.py",
    "core/ops/durable_io.py",
    "core/ops/runtime_execution_identity.py",
    "core/sync_framework/agent_source.py",
    "core/sync_framework/native_artifact_inventory.py",
    "core/sync_framework/raw_event_store.py",
    "core/sync_framework/raw_session_identity_reconciliation.py",
    "core/sync_framework/registry.py",
    "core/sync_framework/source_support.py",
    "daemon/agent_source_coverage.py",
    "daemon/agent_sync_cursor.py",
    "daemon/raw_only_sync_engine.py",
    "daemon/raw_sync.py",
)


class _DeferredCurrentCodexSource:
    """Explicit offline-cutoff view; ordinary Codex runtime stays complete."""

    def __init__(self, source: Any, session_id: str) -> None:
        self._source = source
        self._session_id = str(session_id).lower()
        self.name = str(source.name)
        self.model_tag = str(source.model_tag)

    def discover_sessions(self) -> list[Any]:
        return [
            session
            for session in (self._source.discover_sessions() or [])
            if str(
                getattr(session, "canonical_session_id", "") or getattr(session, "session_id", "")
            ).lower()
            != self._session_id
        ]

    def deferred_active_session_evidence(self) -> dict[str, Any]:
        return {
            "schema_version": "mnemos.codex_active_session_cutoff.v1",
            "reason": "current_append_only_rollout_deferred_to_next_generation",
            "deferred_count": 1,
            "session_id_hashes": [hashlib.sha256(self._session_id.encode("utf-8")).hexdigest()],
            "runtime_reentry": "watchdog-created-modified-moved",
            "scope": "explicit_offline_reconciliation_only",
        }

    def parser_identity_source(self) -> Any:
        return self._source

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)


def _with_explicit_current_codex_cutoff(
    sources: Iterable[Any],
) -> list[Any]:
    source_list = list(sources)
    codex_sources = [source for source in source_list if str(source.name) == "codex"]
    if len(codex_sources) != 1:
        raise AgentSourceRawReconciliationError("current_codex_cutoff_source_ambiguous")
    source = codex_sources[0]
    active_reader = getattr(source, "current_active_session_id", None)
    active_session_id = str(active_reader() or "") if callable(active_reader) else ""
    if not active_session_id:
        raise AgentSourceRawReconciliationError("current_codex_cutoff_identity_unavailable")
    matching_sessions = [
        session
        for session in (source.discover_sessions() or [])
        if str(
            getattr(session, "canonical_session_id", "") or getattr(session, "session_id", "")
        ).lower()
        == active_session_id.lower()
    ]
    if len(matching_sessions) != 1:
        raise AgentSourceRawReconciliationError("current_codex_cutoff_session_not_exact")
    return [
        (_DeferredCurrentCodexSource(item, active_session_id) if item is source else item)
        for item in source_list
    ]


class _StaticSourceRegistry:
    """Narrow recovery registry; it cannot discover sources outside its roster."""

    def __init__(self, sources: Iterable[Any]):
        self._sources = list(sources)

    def list_sources(self) -> list[Any]:
        return list(self._sources)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_sha256(path: Path) -> str:
    return regular_file_sha256(path)


def _recovery_execution_dependency_paths() -> tuple[Path, ...]:
    """Return a conservative production-code closure for exact plans."""

    paths = {ROOT / relative for relative in RECOVERY_EXECUTION_DEPENDENCY_PATHS}
    for production_root in (
        ROOT / "core",
        ROOT / "daemon",
        ROOT / "integrations" / "sources",
    ):
        paths.update(production_root.rglob("*.py"))
    return tuple(sorted(path.absolute() for path in paths))


def _recovery_execution_dependency_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in _recovery_execution_dependency_paths():
        try:
            relative = path.relative_to(ROOT)
        except ValueError:
            raise NativeArtifactInventoryError("native_parser_identity_unresolvable") from None
        if not path.is_file():
            raise NativeArtifactInventoryError("native_parser_identity_unresolvable")
        hashes[str(relative)] = f"sha256:{_file_sha256(path)}"
    return dict(sorted(hashes.items()))


def _native_snapshot_evidence(
    snapshot: SnapshotNativeSourceSet,
    sources: Iterable[Any],
) -> dict[str, Any]:
    """Bind parser code and the immutable artifact snapshot into one plan."""

    parser_source_hashes: dict[str, str] = {}
    for source in sources:
        source_name = str(getattr(source, "name", "") or "")
        identity_reader = getattr(source, "parser_identity_source", None)
        identity_source = identity_reader() if callable(identity_reader) else source
        source_file = inspect.getsourcefile(type(identity_source))
        if not source_name or not source_file:
            raise NativeArtifactInventoryError("native_parser_identity_unresolvable")
        parser_path = Path(source_file).expanduser().resolve(strict=False)
        if not parser_path.is_file():
            raise NativeArtifactInventoryError("native_parser_identity_unresolvable")
        parser_source_hashes[source_name] = f"sha256:{_file_sha256(parser_path)}"
    snapshot_contract = {
        key: value
        for key, value in snapshot.snapshot_evidence().items()
        if key not in {"stabilization_attempts", "stale_snapshot_dirs_cleaned"}
    }
    deferred_active_sessions: dict[str, Any] = {}
    for source in sources:
        evidence_reader = getattr(
            source,
            "deferred_active_session_evidence",
            None,
        )
        if callable(evidence_reader):
            evidence = evidence_reader()
            if isinstance(evidence, Mapping):
                deferred_active_sessions[str(source.name)] = dict(evidence)
    return {
        **snapshot_contract,
        "source_count": len(snapshot.inventory.sources),
        "artifact_count": len(snapshot.inventory.entries),
        "preparse_logical_bytes": sum(
            entry.logical_size_bytes for entry in snapshot.inventory.entries
        ),
        "parser_source_hashes": dict(sorted(parser_source_hashes.items())),
        "execution_dependency_hashes": _recovery_execution_dependency_hashes(),
        "runtime_execution_identity": runtime_execution_identity(),
        "parse_batching": "session-bounded",
        "post_live_inventory_check": "required",
        "active_session_cutoff": dict(sorted(deferred_active_sessions.items())),
    }


def _sqlite_snapshot_sha256(path: Path, *, immutable: bool = False) -> str:
    """Hash one logical SQLite snapshot, including committed WAL state."""
    try:
        source = connect_readonly_sqlite(
            Path(path),
            immutable=immutable,
        )
        try:
            source.execute("BEGIN")
            digest = hashlib.sha256()
            for statement in source.iterdump():
                digest.update(statement.encode("utf-8"))
                digest.update(b"\n")
            return digest.hexdigest()
        finally:
            source.close()
    except (OSError, sqlite3.Error):
        raise AgentSourceRawReconciliationError("sqlite_snapshot_hash_failed") from None


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _file_scope(path: Path, *, sqlite_file: bool = False) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve(strict=False)
    try:
        kind = inspect_path_kind(resolved)
    except DurableIOError:
        raise AgentSourceRawReconciliationError("recovery_state_path_unavailable") from None
    if kind not in {"missing", "file"}:
        raise AgentSourceRawReconciliationError("recovery_state_path_not_regular")
    present = kind == "file"
    return {
        "path": str(resolved),
        "present": present,
        "sha256": (
            f"sha256:{_sqlite_snapshot_sha256(resolved)}"
            if present and sqlite_file
            else f"sha256:{_file_sha256(resolved)}" if present else ""
        ),
        "hash_contract": "sqlite-logical-dump-v1" if sqlite_file else "file-bytes-v1",
    }


def _create_private_target(path: Path) -> None:
    """Create an empty target privately before copying sensitive bytes."""
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)


def _ensure_private_backup_dir(path: Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise AgentSourceRawReconciliationError("backup_directory_unsafe")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
        if resolved.is_symlink():
            raise AgentSourceRawReconciliationError("backup_directory_unsafe")
        metadata = resolved.stat()
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            os.chmod(resolved, 0o700)
            metadata = resolved.stat()
    except OSError:
        raise AgentSourceRawReconciliationError("backup_directory_unsafe") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise AgentSourceRawReconciliationError("backup_directory_unsafe")
    return resolved


def _ensure_private_new_file_parent(path: Path) -> Path:
    """Create a private output directory or validate it without chmod side effects."""
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise AgentSourceRawReconciliationError("new_receipt_parent_unsafe")
    try:
        if candidate.exists():
            resolved = candidate.resolve(strict=True)
        else:
            resolved = candidate.resolve(strict=False)
            resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
        metadata = resolved.stat()
    except OSError:
        raise AgentSourceRawReconciliationError("new_receipt_parent_unsafe") from None
    if (
        resolved.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise AgentSourceRawReconciliationError("new_receipt_parent_unsafe")
    return resolved


def _private_backup_file_ok(path: Path, backup_dir: Path) -> bool:
    try:
        resolved_dir = backup_dir.resolve(strict=True)
        directory_metadata = resolved_dir.lstat()
        preimage = secure_regular_file_preimage(resolved_dir, path.name)
        return bool(
            path.parent == resolved_dir
            and stat.S_ISDIR(directory_metadata.st_mode)
            and not stat.S_ISLNK(directory_metadata.st_mode)
            and directory_metadata.st_uid == os.getuid()
            and stat.S_IMODE(directory_metadata.st_mode) == 0o700
            and isinstance(preimage, Mapping)
            and preimage.get("mode") == 0o600
            and preimage.get("uid") == os.getuid()
            and preimage.get("nlink") == 1
        )
    except (DurableIOError, OSError):
        return False


def _read_private_backup_bytes(path: Path, backup_dir: Path) -> bytes:
    """Read one exact private receipt generation through no-follow dirfds."""

    try:
        resolved_dir = backup_dir.resolve(strict=True)
        candidate = Path(path)
        if candidate.parent != resolved_dir or not candidate.name:
            raise DurableIOError("private_backup_path_invalid")
        directory_before = resolved_dir.lstat()
        before = secure_regular_file_preimage(resolved_dir, candidate.name)
        content = secure_read_bytes(resolved_dir, candidate.name)
        after = secure_regular_file_preimage(resolved_dir, candidate.name)
        directory_after = resolved_dir.lstat()
    except (DurableIOError, OSError):
        raise DurableIOError("private_backup_file_unavailable") from None
    directory_identity_before = (
        int(directory_before.st_dev),
        int(directory_before.st_ino),
        int(directory_before.st_mode),
        int(directory_before.st_uid),
    )
    directory_identity_after = (
        int(directory_after.st_dev),
        int(directory_after.st_ino),
        int(directory_after.st_mode),
        int(directory_after.st_uid),
    )
    if (
        not stat.S_ISDIR(directory_before.st_mode)
        or stat.S_ISLNK(directory_before.st_mode)
        or directory_before.st_uid != os.getuid()
        or stat.S_IMODE(directory_before.st_mode) != 0o700
        or directory_identity_before != directory_identity_after
        or not isinstance(before, Mapping)
        or before != after
        or before.get("mode") != 0o600
        or before.get("uid") != os.getuid()
        or before.get("nlink") != 1
        or content is None
        or hashlib.sha256(content).hexdigest() != before.get("sha256")
    ):
        raise DurableIOError("private_backup_file_changed")
    return content


def _cleanup_failed_backup_target(target: Path, backup_dir: Path) -> None:
    try:
        for candidate in (*private_sqlite_sidecars(target), target):
            candidate.unlink(missing_ok=True)
        fsync_directory(backup_dir)
    except OSError:
        raise AgentSourceRawReconciliationError("failed_backup_cleanup_failed") from None


def _backup_sqlite(
    db_path: Path, backup_dir: Path, label: str
) -> tuple[Path | None, dict[str, Any]]:
    source_path = Path(db_path)
    try:
        source_kind = inspect_path_kind(source_path)
    except DurableIOError:
        raise AgentSourceRawReconciliationError("sqlite_backup_source_unavailable") from None
    if source_kind == "missing":
        return None, {"present": False, "integrity": "not_applicable", "sha256": ""}
    if source_kind != "file":
        raise AgentSourceRawReconciliationError("sqlite_backup_source_not_regular")
    _ensure_private_backup_dir(backup_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"{label}.{stamp}.{uuid.uuid4().hex[:12]}.sqlite"
    completed = False
    created = False
    try:
        try:
            _create_private_target(target)
        except FileExistsError:
            raise
        except BaseException:
            created = True
            raise
        created = True
        with owned_sqlite_connection_pair(
            lambda: connect_readonly_sqlite(source_path),
            lambda: sqlite3.connect(str(target)),
        ) as (source, destination):
            source.backup(destination)
            integrity = destination.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]) != "ok":
                raise AgentSourceRawReconciliationError("backup_integrity_check_failed")
            if destination.execute("PRAGMA foreign_key_check").fetchall():
                raise AgentSourceRawReconciliationError("backup_foreign_key_check_failed")
        normalize_private_sqlite_copy(target)
        if _integrity_sqlite(target, immutable=True) != "ok":
            raise AgentSourceRawReconciliationError("backup_integrity_check_failed")
    except AgentSourceRawReconciliationError:
        if created:
            _cleanup_failed_backup_target(target, backup_dir)
        raise
    except (DurableIOError, OSError, sqlite3.Error):
        if created:
            _cleanup_failed_backup_target(target, backup_dir)
        raise AgentSourceRawReconciliationError("sqlite_backup_failed") from None
    try:
        os.chmod(target, 0o600)
        fsync_regular_file(target)
        fsync_directory(backup_dir)
        digest = _file_sha256(target)
        completed = True
    except OSError:
        raise AgentSourceRawReconciliationError("backup_security_failed") from None
    finally:
        if created and not completed:
            _cleanup_failed_backup_target(target, backup_dir)
    return target, {
        "present": True,
        "integrity": "ok",
        "foreign_key_errors": [],
        "sha256": digest,
        "filename": target.name,
    }


def _backup_coverage(path: Path, backup_dir: Path) -> tuple[Path | None, dict[str, Any]]:
    source_path = Path(path)
    try:
        source_kind = inspect_path_kind(source_path)
    except DurableIOError:
        raise AgentSourceRawReconciliationError("coverage_backup_source_unavailable") from None
    if source_kind == "missing":
        return None, {"present": False, "integrity": "not_applicable", "sha256": ""}
    if source_kind != "file":
        raise AgentSourceRawReconciliationError("coverage_backup_source_not_regular")
    _ensure_private_backup_dir(backup_dir)
    target = backup_dir / (
        "agent-source-coverage."
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}."
        f"{uuid.uuid4().hex[:12]}.json"
    )
    completed = False
    created = False
    try:
        try:
            _create_private_target(target)
        except FileExistsError:
            raise
        except BaseException:
            created = True
            raise
        created = True
        shutil.copyfile(source_path, target)
        os.chmod(target, 0o600)
        fsync_regular_file(target)
        fsync_directory(backup_dir)
        result = {
            "present": True,
            "integrity": "ok",
            "sha256": _file_sha256(target),
            "filename": target.name,
        }
        completed = True
        return target, result
    except OSError:
        raise AgentSourceRawReconciliationError("coverage_backup_failed") from None
    finally:
        if created and not completed:
            _cleanup_failed_backup_target(target, backup_dir)


def _discard_unbound_backups(
    backups: Mapping[str, tuple[Path | None, Mapping[str, Any]]],
    *,
    backup_dir: Path,
) -> None:
    """Delete only backups from an attempt that never reached prepared intent."""
    targets = [Path(path) for path, _record in backups.values() if path is not None]
    _unlink_targets_durably(
        targets,
        error_code="unbound_backup_cleanup_failed",
    )
    if targets:
        fsync_directory(backup_dir)


def _sqlite_sidecars(path: Path) -> tuple[Path, Path, Path]:
    return (
        Path(f"{path}-journal"),
        Path(f"{path}-shm"),
        Path(f"{path}-wal"),
    )


def _integrity_sqlite(path: Path, *, immutable: bool = False) -> str:
    try:
        with connect_readonly_sqlite(
            Path(path),
            immutable=immutable,
        ) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                return "foreign_key_error"
    except (OSError, sqlite3.Error):
        return "unreadable"
    return str(row[0] or "") if row else "unreadable"


def _unlink_targets_durably(
    targets: Iterable[Path],
    *,
    error_code: str,
) -> None:
    mutated_parents: set[Path] = set()
    try:
        for target_value in targets:
            target = Path(target_value)
            try:
                target.lstat()
                existed = True
            except FileNotFoundError:
                existed = False
            target.unlink(missing_ok=True)
            if existed:
                mutated_parents.add(target.parent)
        for parent in sorted(mutated_parents):
            fsync_directory(parent)
    except OSError:
        raise AgentSourceRawReconciliationError(error_code) from None


def _remove_sqlite_target(path: Path) -> None:
    _unlink_targets_durably(
        (*_sqlite_sidecars(path), Path(path)),
        error_code="rollback_target_cleanup_failed",
    )


def _restore_sqlite_backup(backup: Path | None, target: Path) -> None:
    """Restore one explicit SQLite target through a validated temporary database."""
    target = Path(target)
    if backup is None:
        _remove_sqlite_target(target)
        return
    backup = Path(backup)
    try:
        backup_kind = inspect_path_kind(backup)
    except DurableIOError:
        raise AgentSourceRawReconciliationError("rollback_backup_invalid") from None
    if backup_kind != "file":
        raise AgentSourceRawReconciliationError("rollback_backup_invalid")
    try:
        validate_private_sqlite_copy(backup)
    except DurableIOError:
        raise AgentSourceRawReconciliationError("rollback_backup_invalid") from None
    if _integrity_sqlite(backup, immutable=True) != "ok":
        raise AgentSourceRawReconciliationError("rollback_backup_invalid")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.restore")
    temporary_created = False
    try:
        try:
            _create_private_target(temporary)
        except FileExistsError:
            raise
        except BaseException:
            temporary_created = True
            raise
        temporary_created = True
        with owned_sqlite_connection_pair(
            lambda: connect_readonly_sqlite(backup, immutable=True),
            lambda: sqlite3.connect(str(temporary)),
        ) as (source, destination):
            source.backup(destination)
        normalize_private_sqlite_copy(temporary)
        if _integrity_sqlite(temporary, immutable=True) != "ok":
            raise AgentSourceRawReconciliationError("rollback_restore_invalid")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        _unlink_targets_durably(
            _sqlite_sidecars(target),
            error_code="rollback_target_cleanup_failed",
        )
        fsync_regular_file(target)
        fsync_directory(target.parent)
        if _integrity_sqlite(target) != "ok":
            raise AgentSourceRawReconciliationError("rollback_restore_invalid")
    except AgentSourceRawReconciliationError:
        raise
    except (DurableIOError, OSError, sqlite3.Error):
        raise AgentSourceRawReconciliationError("rollback_sqlite_failed") from None
    finally:
        if temporary_created:
            _unlink_targets_durably(
                (*private_sqlite_sidecars(temporary), temporary),
                error_code="rollback_temporary_cleanup_failed",
            )


def _restore_recovery_state(
    *,
    backups: Mapping[str, tuple[Path | None, Mapping[str, Any]]],
    raw_db_path: Path,
    cursor_path: Path,
    coverage_path: Path,
) -> None:
    """Restore exactly the three mutable recovery targets and nothing else."""
    _restore_sqlite_backup(backups["raw"][0], raw_db_path)
    _restore_sqlite_backup(backups["cursor"][0], cursor_path)
    coverage_backup = backups["coverage"][0]
    try:
        if coverage_backup is None:
            _unlink_targets_durably(
                (coverage_path,),
                error_code="rollback_coverage_failed",
            )
        else:
            temporary = coverage_path.with_name(f".{coverage_path.name}.{uuid.uuid4().hex}.restore")
            temporary_created = False
            try:
                coverage_path.parent.mkdir(parents=True, exist_ok=True)
                _create_private_target(temporary)
                temporary_created = True
                shutil.copyfile(coverage_backup, temporary)
                os.chmod(temporary, 0o600)
                os.replace(temporary, coverage_path)
                fsync_regular_file(coverage_path)
                fsync_directory(coverage_path.parent)
            finally:
                if temporary_created:
                    temporary.unlink(missing_ok=True)
    except OSError:
        raise AgentSourceRawReconciliationError("rollback_coverage_failed") from None


def _backups_from_records(
    records: Mapping[str, Mapping[str, Any]],
    backup_dir: Path,
) -> dict[str, tuple[Path | None, Mapping[str, Any]]]:
    return {
        name: (
            (backup_dir / str(record.get("filename") or "") if record.get("present") else None),
            record,
        )
        for name, record in records.items()
    }


def _receipt_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically persist a content-free recovery receipt with restrictive mode."""
    target = Path(path)
    _ensure_private_backup_dir(target.parent)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    encoded = _receipt_bytes(payload)
    temporary_created = False
    try:
        with open(temporary, "xb") as handle:
            temporary_created = True
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        fsync_directory(target.parent)
    finally:
        if temporary_created:
            temporary.unlink(missing_ok=True)


def _write_new_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically publish a new receipt without an overwrite race."""
    target = Path(path)
    target = _ensure_private_new_file_parent(target.parent) / target.name
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    encoded = _receipt_bytes(payload)
    linked = False
    temporary_created = False
    try:
        with open(temporary, "xb") as handle:
            temporary_created = True
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            raise AgentSourceRawReconciliationError("receipt_target_must_be_new") from None
        linked = True
        temporary.unlink()
        fsync_directory(target.parent)
    except AgentSourceRawReconciliationError:
        raise
    except OSError:
        if linked:
            _unlink_targets_durably(
                (target,),
                error_code="new_receipt_cleanup_failed",
            )
        raise AgentSourceRawReconciliationError("new_receipt_write_failed") from None
    finally:
        if temporary_created:
            temporary.unlink(missing_ok=True)


def _mark_reconciliation_receipt_rolled_back(
    *,
    backup_dir: Path,
    applied: Mapping[str, Any],
) -> None:
    """Invalidate the inner completion receipt after outer certification rollback."""
    filename = str(applied.get("receipt_filename") or "")
    if not filename:
        return
    resolved_backup = Path(backup_dir).resolve(strict=True)
    if Path(filename).name != filename or not re.fullmatch(
        r"agent-source-raw-reconciliation-\d{8}T\d{12,18}Z\.json",
        filename,
    ):
        raise AgentSourceRawReconciliationError("rollback_receipt_source_unsafe")
    path = resolved_backup / filename
    if not _private_backup_file_ok(path, resolved_backup):
        raise AgentSourceRawReconciliationError("rollback_receipt_source_unsafe")
    try:
        completed_bytes = _read_private_backup_bytes(path, resolved_backup)
        previous = json.loads(completed_bytes)
        completed_sha256 = hashlib.sha256(completed_bytes).hexdigest()
        archive_path = Path(backup_dir) / (
            "agent-source-raw-reconciliation-invalidated." f"{completed_sha256}.json"
        )
        try:
            archive_kind = inspect_path_kind(archive_path)
        except DurableIOError:
            raise AgentSourceRawReconciliationError(
                "rollback_receipt_archive_unavailable"
            ) from None
        if archive_kind == "file":
            if (
                not _private_backup_file_ok(archive_path, Path(backup_dir))
                or _read_private_backup_bytes(archive_path, resolved_backup)
                != completed_bytes
            ):
                raise AgentSourceRawReconciliationError("rollback_receipt_archive_conflict")
        elif archive_kind == "missing":
            archive_temporary = archive_path.with_name(
                f".{archive_path.name}.{uuid.uuid4().hex}.tmp"
            )
            archive_temporary_created = False
            try:
                with open(archive_temporary, "xb") as handle:
                    archive_temporary_created = True
                    handle.write(completed_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(archive_temporary, 0o600)
                os.replace(archive_temporary, archive_path)
                fsync_directory(archive_path.parent)
            finally:
                if archive_temporary_created:
                    archive_temporary.unlink(missing_ok=True)
        else:
            raise AgentSourceRawReconciliationError("rollback_receipt_archive_conflict")
        _write_receipt(
            path,
            {
                **previous,
                "schema_version": SCHEMA_VERSION,
                "status": "rolled_back_by_migration_certification",
                "ok": False,
                "rollback_ok": True,
                "invalidated_receipt_status": previous.get("status"),
                "invalidated_receipt_filename": archive_path.name,
                "invalidated_receipt_sha256": (f"sha256:{completed_sha256}"),
                "invalidated_completion_receipt_sha256": (f"sha256:{completed_sha256}"),
            },
        )
    except AgentSourceRawReconciliationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AgentSourceRawReconciliationError("rollback_receipt_write_failed") from None


def _validate_active_sources(
    sources: Iterable[Any],
    *,
    require_all_active_sources: bool,
) -> tuple[list[Any], list[str]]:
    manifest = get_agent_source_support_manifest()
    selected: list[Any] = []
    names: list[str] = []
    for source in sources:
        source_name = str(getattr(source, "name", "") or "")
        try:
            spec = manifest.require_active_source(source_name)
        except AgentSourceSupportManifestError:
            raise AgentSourceRawReconciliationError("inactive_or_undeclared_source") from None
        if spec.name in names:
            raise AgentSourceRawReconciliationError("duplicate_active_source")
        names.append(spec.name)
        selected.append(source)
    if require_all_active_sources and set(names) != set(manifest.active_source_names):
        raise AgentSourceRawReconciliationError("active_source_roster_incomplete")
    if not selected:
        raise AgentSourceRawReconciliationError("active_source_roster_empty")
    return selected, sorted(names)


def load_manifest_active_sources() -> list[Any]:
    """Instantiate every manifest-active parser, including zero-session roots."""
    manifest = get_agent_source_support_manifest()
    sources: list[Any] = []
    for source_name in manifest.active_source_names:
        source_class = SourceRegistry.get_builtin_source_class(source_name)
        if source_class is None:
            raise AgentSourceRawReconciliationError("active_source_parser_missing")
        try:
            source = source_class()
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
            raise AgentSourceRawReconciliationError("active_source_initialization_failed") from None
        if str(source.name) != source_name:
            raise AgentSourceRawReconciliationError("active_source_identity_mismatch")
        sources.append(source)
    return sources


def _native_denominator_shape(
    sources: Iterable[Any],
    *,
    batch_turns: int,
) -> tuple[int, int, int]:
    """Return conservative bounds for a full current native denominator.

    A session with more than one turn batch is revisited only after its source
    roster completes a round-robin rotation.  The third result is therefore
    the largest per-session visit count, not merely a throughput statistic.
    """
    max_sessions = 0
    max_turns = 0
    max_turn_batches = 1
    for source in sources:
        seen: set[str] = set()
        try:
            sessions = list(source.discover_sessions() or [])
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            raise AgentSourceRawReconciliationError("native_discovery_failed") from None
        for session in sessions:
            try:
                canonical = canonicalize_session_info(session)
                session_id = str(canonical.session_id or "")
            except (AttributeError, TypeError, ValueError):
                raise AgentSourceRawReconciliationError("native_session_metadata_invalid") from None
            if not session_id or session_id in seen:
                raise AgentSourceRawReconciliationError("native_canonical_session_invalid")
            seen.add(session_id)
            try:
                turn_count = len(list(parse_discovered_session(source, session) or []))
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
            ):
                raise AgentSourceRawReconciliationError("native_session_parse_failed") from None
            max_turns = max(max_turns, turn_count)
            max_turn_batches = max(max_turn_batches, max(1, math.ceil(turn_count / batch_turns)))
        max_sessions = max(max_sessions, len(seen))
    return max_sessions, max_turns, max_turn_batches


def _raise_native_planning_evidence_invalid(
    reason_code: str,
    *,
    source_name: str = "",
) -> NoReturn:
    details = {"reason_code": str(reason_code)}
    if source_name:
        details["source_name"] = str(source_name)
    raise AgentSourceRawReconciliationError(
        "native_challenger_planning_evidence_invalid",
        details=details,
    )


def _native_denominator_shape_from_challenger(
    challenger_report: Mapping[str, Any],
    *,
    source_names: Iterable[str],
    batch_turns: int,
) -> tuple[int, int, int]:
    """Reuse the challenger's exact parse pass for content-free planning bounds."""

    reports = challenger_report.get("sources")
    expected_names = {str(name) for name in source_names}
    if not isinstance(reports, Mapping) or set(reports) != expected_names:
        _raise_native_planning_evidence_invalid("native_source_roster_mismatch")
    max_sessions = 0
    max_turns = 0

    def validated_parse_failures(
        value: Any,
        *,
        expected_source_name: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value:
            _raise_native_planning_evidence_invalid(
                "native_parse_failure_evidence_invalid",
                source_name=expected_source_name,
            )
        validated: list[dict[str, Any]] = []
        for item in value:
            if (
                not isinstance(item, Mapping)
                or not set(item).issubset(SNAPSHOT_PARSE_TERMINAL_EVIDENCE_KEYS)
                or item.get("source_name") != expected_source_name
                or not isinstance(item.get("error_code"), str)
                or item.get("error_code") not in SNAPSHOT_PARSE_TERMINAL_ERROR_CODES
                or not isinstance(item.get("session_id_hash"), str)
                or re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    str(item["session_id_hash"]),
                )
                is None
            ):
                _raise_native_planning_evidence_invalid(
                    "native_parse_failure_evidence_invalid",
                    source_name=expected_source_name,
                )
            attempt_count = item.get("attempt_count")
            if (
                not isinstance(attempt_count, int)
                or isinstance(attempt_count, bool)
                or attempt_count < 1
                or attempt_count > 2
            ):
                _raise_native_planning_evidence_invalid(
                    "native_parse_failure_attempt_invalid",
                    source_name=expected_source_name,
                )
            exception_type = item.get("exception_type")
            if exception_type is not None and (
                not isinstance(exception_type, str)
                or re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_.]{0,127}",
                    exception_type,
                )
                is None
            ):
                _raise_native_planning_evidence_invalid(
                    "native_parse_failure_exception_invalid",
                    source_name=expected_source_name,
                )
            reason_code = item.get("reason_code")
            if reason_code is not None and (
                not isinstance(reason_code, str)
                or re.fullmatch(
                    r"[a-z][a-z0-9_]{2,127}",
                    reason_code,
                )
                is None
            ):
                _raise_native_planning_evidence_invalid(
                    "native_parse_failure_reason_invalid",
                    source_name=expected_source_name,
                )
            failure_class = item.get("failure_class")
            if failure_class is not None and failure_class not in {
                "os_nontransient",
                "os_transient",
                "sqlite_nontransient",
                "sqlite_transient",
                "storage_untyped",
            }:
                _raise_native_planning_evidence_invalid(
                    "native_parse_failure_class_invalid",
                    source_name=expected_source_name,
                )
            sqlite_errorname = item.get("sqlite_errorname")
            if sqlite_errorname is not None and (
                not isinstance(sqlite_errorname, str)
                or re.fullmatch(
                    r"SQLITE_[A-Z0-9_]{1,96}",
                    sqlite_errorname,
                )
                is None
            ):
                _raise_native_planning_evidence_invalid(
                    "native_parse_failure_sqlite_name_invalid",
                    source_name=expected_source_name,
                )
            if any(
                isinstance(item.get(key), bool) or not isinstance(item.get(key), int)
                for key in ("os_errno", "sqlite_errorcode")
                if key in item
            ):
                _raise_native_planning_evidence_invalid(
                    "native_parse_failure_numeric_field_invalid",
                    source_name=expected_source_name,
                )
            signal_value = item.get("signal")
            if signal_value is not None and (
                not isinstance(signal_value, int)
                or isinstance(signal_value, bool)
                or signal_value < 1
                or signal_value > 127
            ):
                _raise_native_planning_evidence_invalid(
                    "native_parse_failure_signal_invalid",
                    source_name=expected_source_name,
                )
            if not snapshot_parse_terminal_evidence_is_valid(item):
                _raise_native_planning_evidence_invalid(
                    "native_parse_failure_evidence_invalid",
                    source_name=expected_source_name,
                )
            validated.append(dict(item))
        return validated

    def raise_source_planning_reasons(
        reasons: Iterable[str],
        *,
        source_name: str,
    ) -> NoReturn:
        unique_reasons = list(dict.fromkeys(str(item) for item in reasons))
        if len(unique_reasons) == 1:
            _raise_native_planning_evidence_invalid(
                unique_reasons[0],
                source_name=source_name,
            )
        failures = [
            {
                "error_code": ("native_challenger_planning_evidence_invalid"),
                "reason_code": reason_code,
                "source_name": source_name,
            }
            for reason_code in unique_reasons
        ]
        raise AgentSourceRawReconciliationError(
            "native_challenger_planning_evidence_invalid",
            details={
                "failure_count": len(failures),
                "failures": failures,
                "source_failure_count": 1,
            },
        )

    def validate_source_report(
        source_name: str,
    ) -> tuple[int, int]:
        report = reports.get(source_name)
        if not isinstance(report, Mapping):
            _raise_native_planning_evidence_invalid(
                "native_source_report_invalid",
                source_name=source_name,
            )
        errors = report.get("errors")
        if not isinstance(errors, list) or not all(isinstance(item, str) for item in errors):
            _raise_native_planning_evidence_invalid(
                "native_source_errors_invalid",
                source_name=source_name,
            )
        native_errors = sorted(error for error in errors if error.startswith("native_"))
        parse_failures: list[dict[str, Any]] = []
        if "native_session_parse_failed" in native_errors:
            parse_failures = validated_parse_failures(
                report.get("native_session_parse_failures"),
                expected_source_name=source_name,
            )
        other_native_errors = [
            error for error in native_errors if error != "native_session_parse_failed"
        ]
        if parse_failures and not other_native_errors:
            raise AgentSourceRawReconciliationError(
                "native_session_parse_failed",
                details={
                    "source_name": source_name,
                    "failures": parse_failures,
                },
            )
        if not parse_failures and len(other_native_errors) == 1:
            _raise_native_planning_evidence_invalid(
                other_native_errors[0],
                source_name=source_name,
            )
        if parse_failures or other_native_errors:
            aggregate_failures = [
                *parse_failures,
                *(
                    {
                        "error_code": ("native_challenger_planning_evidence_invalid"),
                        "reason_code": error,
                        "source_name": source_name,
                    }
                    for error in other_native_errors
                ),
            ]
            raise AgentSourceRawReconciliationError(
                "native_challenger_planning_evidence_invalid",
                details={
                    "failure_count": len(aggregate_failures),
                    "failures": aggregate_failures,
                    "source_failure_count": 1,
                },
            )
        values: dict[str, int] = {}
        invalid_value_reasons: list[str] = []
        for key in (
            "native_sessions",
            "native_parsed_turns",
            "native_session_turn_upper_bound",
            "native_identity_isolated_sessions",
            "native_parse_recovered_sessions",
        ):
            value = report.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                invalid_value_reasons.append(f"{key}_invalid")
                continue
            values[key] = value
        if invalid_value_reasons:
            raise_source_planning_reasons(
                invalid_value_reasons,
                source_name=source_name,
            )
        session_count = values["native_sessions"]
        parsed_turns = values["native_parsed_turns"]
        session_turn_upper_bound = values["native_session_turn_upper_bound"]
        identity_isolated_sessions = values["native_identity_isolated_sessions"]
        recovered_session_count = values["native_parse_recovered_sessions"]
        recovery_evidence = report.get("native_parse_recovery_evidence")
        shape_reasons: list[str] = []
        if session_turn_upper_bound > parsed_turns:
            shape_reasons.append("native_session_turn_upper_bound_invalid")
        if parsed_turns > session_count * session_turn_upper_bound:
            shape_reasons.append("native_parsed_turn_shape_invalid")
        if identity_isolated_sessions != session_count:
            shape_reasons.append("native_identity_isolation_count_mismatch")
        if not isinstance(recovery_evidence, list):
            shape_reasons.append(
                "native_parse_recovery_evidence_invalid",
            )
            recovery_evidence = []
        if len(recovery_evidence) != recovered_session_count:
            shape_reasons.append("native_parse_recovery_count_mismatch")
        recovery_session_hashes: set[str] = set()
        for item in recovery_evidence:
            if not isinstance(item, Mapping):
                shape_reasons.append(
                    "native_parse_recovery_evidence_invalid",
                )
                continue
            session_id_hash = str(item.get("session_id_hash") or "")
            if (
                item.get("attempt_count") != 2
                or re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    session_id_hash,
                )
                is None
                or not snapshot_parse_recovery_evidence_is_valid(
                    {
                        key: value
                        for key, value in item.items()
                        if key not in {"attempt_count", "session_id_hash"}
                    }
                )
            ):
                shape_reasons.append(
                    "native_parse_recovery_evidence_invalid",
                )
                continue
            if session_id_hash in recovery_session_hashes:
                shape_reasons.append(
                    "native_parse_recovery_identity_duplicate",
                )
            recovery_session_hashes.add(session_id_hash)
        if session_count == 0 and (
            parsed_turns != 0 or session_turn_upper_bound != 0 or recovered_session_count != 0
        ):
            shape_reasons.append(
                "native_empty_session_shape_invalid",
            )
        if shape_reasons:
            raise_source_planning_reasons(
                shape_reasons,
                source_name=source_name,
            )
        return session_count, session_turn_upper_bound

    source_failures: list[tuple[str, AgentSourceRawReconciliationError]] = []
    for source_name in sorted(expected_names):
        try:
            session_count, session_turn_upper_bound = validate_source_report(source_name)
        except AgentSourceRawReconciliationError as exc:
            source_failures.append((source_name, exc))
            continue
        max_sessions = max(max_sessions, session_count)
        max_turns = max(max_turns, session_turn_upper_bound)
    if source_failures:
        if len(source_failures) == 1:
            raise source_failures[0][1]
        aggregate_failures: list[dict[str, Any]] = []
        has_planning_failure = False
        for source_name, failure in source_failures:
            nested_failures = failure.details.get("failures")
            if isinstance(nested_failures, list):
                if failure.code != "native_session_parse_failed":
                    has_planning_failure = True
                aggregate_failures.extend(
                    dict(item) for item in nested_failures if isinstance(item, Mapping)
                )
                continue
            if failure.code == "native_session_parse_failed":
                has_planning_failure = True
                aggregate_failures.append(
                    {
                        "error_code": ("native_challenger_planning_evidence_invalid"),
                        "reason_code": ("native_parse_failure_evidence_invalid"),
                        "source_name": source_name,
                    }
                )
                continue
            has_planning_failure = True
            reason_code = str(failure.details.get("reason_code") or failure.code)
            aggregate_failures.append(
                {
                    "error_code": failure.code,
                    "reason_code": reason_code,
                    "source_name": source_name,
                }
            )
        raise AgentSourceRawReconciliationError(
            (
                "native_challenger_planning_evidence_invalid"
                if has_planning_failure
                else "native_session_parse_failed"
            ),
            details={
                "failure_count": len(aggregate_failures),
                "failures": aggregate_failures,
                "source_failure_count": len(source_failures),
            },
        )
    return (
        max_sessions,
        max_turns,
        max(
            1,
            math.ceil(max_turns / batch_turns),
        ),
    )


def _recovery_plan(
    sources: Iterable[Any],
    *,
    batch_sessions: int,
    batch_turns: int,
    minimum_generations: int,
    challenger_report: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """Bound recovery memory while deriving an explicit completion budget."""
    if batch_sessions <= 0 or batch_turns <= 0:
        raise AgentSourceRawReconciliationError("recovery_batch_limit_invalid")
    if minimum_generations < 2:
        raise AgentSourceRawReconciliationError("at_least_two_generations_required")
    source_list = list(sources)
    max_sessions, max_turns, max_turn_batches = (
        _native_denominator_shape_from_challenger(
            challenger_report,
            source_names=(str(getattr(source, "name", "") or "") for source in source_list),
            batch_turns=batch_turns,
        )
        if challenger_report is not None
        else _native_denominator_shape(
            source_list,
            batch_turns=batch_turns,
        )
    )
    estimated_generations = max(
        minimum_generations,
        math.ceil(max_sessions / batch_sessions) * max_turn_batches,
    )
    return {
        "tail_sessions_per_source": batch_sessions,
        "reconciliation_sessions_per_source": batch_sessions,
        "turns_per_session": batch_turns,
        "source_session_upper_bound": max_sessions,
        "session_turn_upper_bound": max_turns,
        "session_turn_batch_upper_bound": max_turn_batches,
        "minimum_generations": minimum_generations,
        "generation_budget": estimated_generations,
    }
