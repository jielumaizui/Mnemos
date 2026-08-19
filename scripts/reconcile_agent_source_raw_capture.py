#!/usr/bin/env python3
"""Rebuild a frozen 12-source Native-to-Raw generation without semantic writes.

This is a controlled recovery tool, not a replacement for the daemon's normal
poller.  ``--apply`` requires a verified inactive daemon, takes verified
backups of only the mutable Raw/cursor/coverage state, resets only derived
cursor evidence, then runs at least two fresh Raw-only engine generations.
It records counters and hashes only; transcript bodies and native paths never
enter the receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shutil
import signal
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import psutil

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.agent_kit.native_raw_challenger import audit_native_to_raw
from core.agent_kit.source_capture_verification import verify_source_capture
from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
from core.config import Config
from core.ops.offline_migration_lock import offline_migration_lock
from core.ops.durable_io import (
    DurableIOError,
    fsync_directory,
    fsync_regular_file,
    inspect_path_kind,
    normalize_private_sqlite_copy,
    owned_sqlite_connection_pair,
    private_sqlite_sidecars,
)
from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.ops.runtime_execution_identity import runtime_execution_identity
from core.sync_framework.native_artifact_inventory import (
    NativeArtifactInventory,
    NativeArtifactInventoryError,
    SnapshotNativeSourceSet,
    isolated_bounded_parse_spool,
    snapshot_native_sources,
)
from core.ops.durable_io import read_native_bytes
from core.sync_framework.native_raw_recovery_evidence import (
    compare_raw_conservation as _compare_raw_conservation,
    conservation_summary as _conservation_summary,
    raw_conservation_findings as _raw_conservation_findings,
)
from core.sync_framework.native_sqlite import active_native_sqlite_read_path
from core.sync_framework.raw_current_projection_reconciliation import (
    RawCurrentProjectionReconciliationError,
    apply_current_projection_reconciliation,
    plan_current_projection_reconciliation,
)
from core.sync_framework.raw_event_store import RawEventStore
from core.sync_framework.registry import SourceRegistry
from daemon import agent_source_coverage, raw_sync
from daemon.agent_sync_cursor import AgentSyncCursorError, AgentSyncCursorStore
from daemon.raw_only_sync_engine import RawOnlySyncEngine
from scripts.agent_source_raw_recovery_contract import (
    AgentSourceRawReconciliationError,
    PLAN_VERSION,
    RAW_GENERATION_WORKER_MAX_REPORT_BYTES,
    RAW_GENERATION_WORKER_MAX_RSS_BYTES,
    RAW_GENERATION_WORKER_MAX_SECONDS,
    SCHEMA_VERSION,
    reconciliation_error_from_typed_failure,
)
from scripts.agent_source_raw_reconciliation_cli import (
    CliDependencies as _CliDependencies,
    main as _cli_main,
)
from scripts.agent_source_raw_reconciliation_support import (  # noqa: F401
    _apply_session_identity_reconciliation_plan,
    _coverage_generation_complete,
    _database_file_state,
    _default_runtime_writers_are_inactive,
    _safe_cycle_report,
    _safe_sync_error_evidence,
    _session_identity_reconciliation_plan,
    _unexpected_database_mutations,
    _verify_session_identity_reconciliation_plan,
)
from scripts.agent_source_raw_worker_runtime import (
    ChallengerWorkerDependencies,
    RawGenerationWorkerDependencies,
    audit_native_to_raw_isolated as _worker_audit_native_to_raw_isolated,
    run_raw_generation_isolated as _worker_run_raw_generation_isolated,
)
from scripts.phase1_governance_data import PHASE1_REVALIDATION_SEQUENCE

NON_RETRYABLE_RAW_SYNC_ERROR_CODES = frozenset(
    {
        "source_session_identity_reconciliation_required",
        "raw_event_identity_schema_reconciliation_required",
        "raw_event_identity_alias_reconciliation_required",
        "raw_current_revision_projection_reconciliation_required",
    }
)


def _raw_sync_error_is_nonretryable(code: object) -> bool:
    """Stop one frozen plan after deterministic or exhausted Native failures."""

    value = str(code or "")
    return bool(value in NON_RETRYABLE_RAW_SYNC_ERROR_CODES or value.startswith("native_"))


_CHALLENGER_WORKER_MAX_RSS_BYTES = 8 * 1024 * 1024 * 1024
_CHALLENGER_WORKER_MAX_REPORT_BYTES = 16 * 1024 * 1024
_CHALLENGER_WORKER_MAX_SECONDS = 15 * 60
_CHALLENGER_REPORT_SCHEMA = "mnemos.agent_source_native_raw_challenger.v3"
_RAW_GENERATION_WORKER_MAX_RSS_BYTES = RAW_GENERATION_WORKER_MAX_RSS_BYTES
_RAW_GENERATION_WORKER_MAX_REPORT_BYTES = RAW_GENERATION_WORKER_MAX_REPORT_BYTES
_RAW_GENERATION_WORKER_MAX_SECONDS = RAW_GENERATION_WORKER_MAX_SECONDS
_RAW_GENERATION_REPORT_SCHEMA = "mnemos.agent_source_raw_generation_worker.v1"
_RAW_GENERATION_FAILURE_SCHEMA = "mnemos.raw_generation_worker_failure.v3"
_ACTIVE_RAW_GENERATION_NUMBER = 0
_PROCESS_WRITE_SCOPE_LOCK = threading.RLock()
_ACTIVE_PROCESS_WRITE_SCOPE: "_ProcessDatabaseWriteScope | None" = None
_PROCESS_WRITE_AUDIT_HOOK_INSTALLED = False


from scripts.agent_source_raw_recovery_support import (
    _ProcessDatabaseWriteScope,
    _audit_event_has_ambiguous_relative_open,
    _audit_event_write_paths,
    _close_inherited_regular_file_descriptors,
    _create_recovery_worker_root,
    _install_worker_filesystem_sandbox,
    _with_explicit_current_codex_cutoff,
    _StaticSourceRegistry,
    _file_sha256,
    _recovery_execution_dependency_hashes,
    _native_snapshot_evidence,
    _canonical_hash,
    _file_scope,
    _create_private_target,
    _ensure_private_backup_dir,
    _backup_sqlite,
    _backup_coverage,
    _discard_unbound_backups,
    _sqlite_sidecars,
    _unlink_targets_durably,
    _restore_sqlite_backup,
    _restore_recovery_state,
    _backups_from_records,
    _read_private_backup_bytes,
    _receipt_bytes,
    _write_receipt,
    _write_new_receipt,
    _mark_reconciliation_receipt_rolled_back,
    _validate_active_sources,
    load_manifest_active_sources,
    _recovery_plan,
)

_PHASE1_LEDGER_PATH = (
    ROOT / "docs" / "acceptance" / "cognitive_remediation_phase_1_ledger.json"
)
_PHASE1_EXECUTION_EVIDENCE_PATH = (
    ROOT / "docs" / "acceptance" / "phase1_historical_defect_execution_evidence.json"
)
_PHASE1_GOVERNANCE_DATA_PATH = ROOT / "scripts" / "phase1_governance_data.json"


def _read_governance_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(read_native_bytes(path).decode("utf-8"))
    except (DurableIOError, OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("phase1 governance artifact is unavailable") from None
    if not isinstance(value, dict):
        raise ValueError("phase1 governance artifact is not an object")
    return value


def _governance_file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(read_native_bytes(path)).hexdigest()
    except (DurableIOError, OSError):
        return ""


def _phase1_governance_generation_binding() -> dict[str, Any]:
    """Bind a recovery plan to the current evidence-backed governance successor."""
    errors: list[str] = []
    record_id = str(PHASE1_REVALIDATION_SEQUENCE[-1][1])
    predecessor_id = str(PHASE1_REVALIDATION_SEQUENCE[-2][1])
    record: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    try:
        ledger = _read_governance_object(_PHASE1_LEDGER_PATH)
        evidence = _read_governance_object(_PHASE1_EXECUTION_EVIDENCE_PATH)
        candidate_snapshot = evidence.get("candidate_snapshot")
        if not isinstance(candidate_snapshot, dict):
            errors.append("execution_evidence_candidate_snapshot_invalid")
            candidate_snapshot = {}
        record_value = ledger.get(record_id)
        if not isinstance(record_value, dict):
            errors.append("current_governance_record_missing")
        else:
            record = record_value
        verification = record.get("verification")
        if not isinstance(verification, dict):
            errors.append("current_governance_verification_missing")
            verification = {}
        evidence_hash = str(evidence.get("evidence_hash") or "")
        if (
            len(evidence_hash) != 64
            or any(character not in "0123456789abcdef" for character in evidence_hash)
        ):
            errors.append("execution_evidence_hash_invalid")
        if verification.get("phase1_execution_evidence_hash") != evidence_hash:
            errors.append("governance_execution_evidence_binding_mismatch")
        if record.get("root_id") != "COG-045":
            errors.append("current_governance_root_mismatch")
        if record.get("sequence_predecessor") != predecessor_id:
            errors.append("current_governance_predecessor_mismatch")
        if record.get("supersedes_evidence_record") != predecessor_id:
            errors.append("current_governance_supersession_mismatch")
        post_review = record.get("post_deep_review_contract")
        if not isinstance(post_review, dict) or not post_review:
            errors.append("post_deep_review_contract_missing")
            post_review = {}
        if not isinstance(record.get("governance_revalidation"), dict) or not record.get(
            "governance_revalidation"
        ):
            errors.append("governance_revalidation_missing")
        if not isinstance(record.get("artifacts"), dict) or not record.get("artifacts"):
            errors.append("governance_artifact_binding_missing")
        closure_boundary = record.get("closure_boundary")
        if (
            not isinstance(closure_boundary, dict)
            or closure_boundary.get("root_closed") is not False
            or closure_boundary.get("release_eligible") is not False
            or closure_boundary.get("production_effect") != "not verified"
        ):
            errors.append("governance_production_boundary_invalid")
        from scripts.phase1_governance_execution_validation import (
            phase1_execution_snapshot,
        )

        current_snapshot = phase1_execution_snapshot()
        if {
            "path_count": candidate_snapshot.get("path_count"),
            "sha256": candidate_snapshot.get("sha256"),
        } != current_snapshot:
            errors.append("execution_evidence_candidate_snapshot_stale")
    except (IndexError, TypeError, ValueError):
        errors.append("phase1_governance_binding_unreadable")
        candidate_snapshot = {}
        current_snapshot = {}
        post_review = {}
        evidence_hash = ""
    return {
        "schema_version": "mnemos.phase1_recovery_governance_binding.v1",
        "ok": not errors,
        "record_id": record_id,
        "record_hash": _canonical_hash(record),
        "execution_evidence_hash": evidence_hash,
        "execution_evidence_file_sha256": _governance_file_sha256(
            _PHASE1_EXECUTION_EVIDENCE_PATH
        ),
        "candidate_snapshot": {
            "path_count": candidate_snapshot.get("path_count"),
            "sha256": candidate_snapshot.get("sha256"),
        },
        "current_candidate_snapshot": current_snapshot,
        "post_deep_review_contract_hash": _canonical_hash(post_review),
        "sequence_predecessor": predecessor_id,
        "governance_data_sha256": _governance_file_sha256(
            _PHASE1_GOVERNANCE_DATA_PATH
        ),
        "errors": sorted(set(errors)),
    }


__all__ = (
    "SourceRegistry",
    "_receipt_bytes",
    "_restore_sqlite_backup",
    "tempfile",
)


def _set_active_raw_generation_number(value: int) -> None:
    global _ACTIVE_RAW_GENERATION_NUMBER
    _ACTIVE_RAW_GENERATION_NUMBER = int(value)


def _audit_native_to_raw_isolated(
    sources: Iterable[Any],
    *,
    raw_db_path: Path,
    manifest: Any | None = None,
    require_all_host_sources: bool = True,
    source_scope: str = "host",
) -> dict[str, Any]:
    dependencies = ChallengerWorkerDependencies(
        create_recovery_worker_root=_create_recovery_worker_root,
        create_private_challenger_raw_snapshot=(
            _create_private_challenger_raw_snapshot
        ),
        create_private_target=_create_private_target,
        remove_worker_root=_remove_challenger_worker_root,
        close_inherited_descriptors=(
            _close_inherited_regular_file_descriptors
        ),
        establish_parent_death_guard=_establish_parent_death_guard,
        install_filesystem_sandbox=_install_worker_filesystem_sandbox,
        install_read_only_guard=_install_challenger_read_only_guard,
        isolated_parse_spool=isolated_bounded_parse_spool,
        audit_native_to_raw=audit_native_to_raw,
        read_guard_violations=_read_guard_violations,
        complete_parent_death_guard=_complete_parent_death_guard,
        kill_worker_process_group=_kill_worker_process_group,
        max_report_bytes=_CHALLENGER_WORKER_MAX_REPORT_BYTES,
        max_rss_bytes=_CHALLENGER_WORKER_MAX_RSS_BYTES,
        max_seconds=_CHALLENGER_WORKER_MAX_SECONDS,
    )
    return _worker_audit_native_to_raw_isolated(
        sources,
        raw_db_path=raw_db_path,
        manifest=manifest,
        require_all_host_sources=require_all_host_sources,
        source_scope=source_scope,
        dependencies=dependencies,
    )


def _run_raw_generation_isolated(
    *,
    config: Any,
    raw_db_path: Path,
    cursor_path: Path,
    coverage_path: Path,
    sources: Iterable[Any],
    source_names: Iterable[str],
    limits: Mapping[str, int],
    process_write_scope: _ProcessDatabaseWriteScope,
    generation_number: int,
) -> dict[str, Any]:
    dependencies = RawGenerationWorkerDependencies(
        assert_parent_handles_closed=(
            _assert_raw_generation_parent_handles_closed
        ),
        create_recovery_worker_root=_create_recovery_worker_root,
        create_private_target=_create_private_target,
        remove_worker_root=_remove_raw_generation_worker_root,
        close_inherited_descriptors=(
            _close_inherited_regular_file_descriptors
        ),
        establish_parent_death_guard=_establish_parent_death_guard,
        install_filesystem_sandbox=_install_worker_filesystem_sandbox,
        sqlite_sidecars=_sqlite_sidecars,
        install_write_guard=_install_raw_generation_write_guard,
        fsync_regular_file=fsync_regular_file,
        fsync_directory=fsync_directory,
        raw_event_store_factory=RawEventStore,
        raw_only_engine_factory=RawOnlySyncEngine,
        cursor_store_factory=AgentSyncCursorStore,
        load_source_coverage_state=(
            agent_source_coverage.load_source_coverage_state
        ),
        isolated_parse_spool=isolated_bounded_parse_spool,
        run_raw_service=raw_sync.run_service,
        static_source_registry_factory=_StaticSourceRegistry,
        safe_sync_error_evidence=_safe_sync_error_evidence,
        read_guard_violations=_read_guard_violations,
        safe_cycle_report=_safe_cycle_report,
        complete_parent_death_guard=_complete_parent_death_guard,
        kill_worker_process_group=_kill_worker_process_group,
        set_active_generation=_set_active_raw_generation_number,
        max_report_bytes=_RAW_GENERATION_WORKER_MAX_REPORT_BYTES,
        max_rss_bytes=_RAW_GENERATION_WORKER_MAX_RSS_BYTES,
        max_seconds=_RAW_GENERATION_WORKER_MAX_SECONDS,
    )
    return _worker_run_raw_generation_isolated(
        config=config,
        raw_db_path=raw_db_path,
        cursor_path=cursor_path,
        coverage_path=coverage_path,
        sources=sources,
        source_names=source_names,
        limits=limits,
        process_write_scope=process_write_scope,
        generation_number=generation_number,
        dependencies=dependencies,
    )


def _remove_challenger_worker_root(worker_root: Path) -> None:
    try:
        shutil.rmtree(worker_root)
    except FileNotFoundError:
        return
    except OSError:
        raise AgentSourceRawReconciliationError("native_challenger_cleanup_failed") from None
    try:
        worker_root.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise AgentSourceRawReconciliationError("native_challenger_cleanup_failed") from None
    else:
        raise AgentSourceRawReconciliationError("native_challenger_cleanup_failed")


def _create_private_challenger_raw_snapshot(
    raw_db_path: Path,
    worker_root: Path,
) -> Path:
    snapshot_path = worker_root / "raw-read-snapshot.sqlite"
    completed = False
    created = False
    try:
        try:
            _create_private_target(snapshot_path)
        except FileExistsError:
            raise
        except BaseException:
            created = True
            raise
        created = True
        with owned_sqlite_connection_pair(
            lambda: connect_readonly_sqlite(raw_db_path),
            lambda: sqlite3.connect(str(snapshot_path)),
        ) as (source, destination):
            source.backup(destination)
        normalize_private_sqlite_copy(snapshot_path)
        os.chmod(snapshot_path, 0o600)
        fsync_regular_file(snapshot_path)
        if _integrity_sqlite(snapshot_path, immutable=True) != "ok":
            raise AgentSourceRawReconciliationError("native_challenger_raw_snapshot_invalid")
        completed = True
    except (DurableIOError, OSError, sqlite3.Error):
        raise AgentSourceRawReconciliationError("native_challenger_raw_snapshot_failed") from None
    finally:
        if created and not completed:
            for candidate in (*private_sqlite_sidecars(snapshot_path), snapshot_path):
                candidate.unlink(missing_ok=True)
    return snapshot_path


def _establish_parent_death_guard(
    parent_watch_read: int,
    worker_root: Path,
) -> tuple[int, int]:
    """Create a same-session guardian that kills the worker tree on parent EOF."""
    try:
        os.setsid()
        worker_life_read, worker_life_write = os.pipe()
        guardian_pid = os.fork()
    except OSError:
        raise AgentSourceRawReconciliationError("worker_parent_death_guard_unavailable") from None
    if guardian_pid == 0:  # pragma: no branch - minimal lifecycle guardian
        try:
            worker_pid = os.getppid()
            os.setpgid(0, 0)
            os.close(worker_life_write)
            while True:
                readable, _writable, _errors = select.select(
                    [parent_watch_read, worker_life_read],
                    [],
                    [],
                )
                if parent_watch_read in readable:
                    if os.read(parent_watch_read, 1) == b"":
                        try:
                            os.killpg(worker_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        for _attempt in range(100):
                            shutil.rmtree(worker_root, ignore_errors=True)
                            try:
                                worker_root.lstat()
                            except FileNotFoundError:
                                break
                            except OSError:
                                os._exit(96)
                            time.sleep(0.01)
                        try:
                            worker_root.lstat()
                        except FileNotFoundError:
                            os._exit(0)
                        except OSError:
                            os._exit(96)
                        os._exit(96)
                if worker_life_read in readable:
                    if os.read(worker_life_read, 1) == b"":
                        os._exit(0)
        except BaseException:
            os._exit(95)
    os.close(parent_watch_read)
    os.close(worker_life_read)
    return guardian_pid, worker_life_write


def _complete_parent_death_guard(
    guardian_pid: int,
    worker_life_write: int,
) -> None:
    try:
        os.close(worker_life_write)
    except OSError as exc:
        raise AgentSourceRawReconciliationError(
            "worker_parent_death_guard_failed",
            details={
                "phase": "close_worker_life",
                "os_errno": int(exc.errno or 0),
            },
        ) from None
    try:
        waited_pid, status = os.waitpid(guardian_pid, 0)
    except (ChildProcessError, OSError):
        raise AgentSourceRawReconciliationError(
            "worker_parent_death_guard_failed",
            details={"phase": "wait_guardian"},
        ) from None
    exit_code = os.waitstatus_to_exitcode(status)
    if waited_pid != guardian_pid or exit_code != 0:
        raise AgentSourceRawReconciliationError(
            "worker_parent_death_guard_failed",
            details={
                "phase": "guardian_exit",
                "guardian_exit_code": int(exit_code),
            },
        )


def _kill_worker_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def _append_guard_violation(marker: Path | None, name: str) -> None:
    """Persist a content-free marker that survives parser-grandchild exit."""

    if marker is None:
        return
    digest = hashlib.sha256(str(name).encode("utf-8")).hexdigest()[:16]
    try:
        descriptor = os.open(
            marker,
            os.O_WRONLY | os.O_APPEND,
        )
        try:
            os.write(descriptor, f"{digest}\n".encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        return


def _read_guard_violations(marker: Path) -> set[str]:
    try:
        values = {
            line.strip()
            for line in read_native_bytes(marker).decode("ascii").splitlines()
            if line.strip()
        }
    except (OSError, UnicodeError):
        raise AgentSourceRawReconciliationError("worker_write_guard_evidence_unreadable") from None
    if any(not re.fullmatch(r"[0-9a-f]{16}", value) for value in values):
        raise AgentSourceRawReconciliationError("worker_write_guard_evidence_invalid")
    return values


def _install_challenger_read_only_guard(
    *,
    allowed_write_roots: Iterable[Path],
    blocked_name_hashes: set[str],
    allowed_read_paths: Iterable[Path] = (),
    allowed_read_roots: Iterable[Path] = (),
    violation_marker: Path | None = None,
) -> None:
    allowed_roots = tuple(
        sorted(
            {Path(path).expanduser().resolve(strict=False) for path in allowed_write_roots},
            key=str,
        )
    )
    if not allowed_roots:
        raise AgentSourceRawReconciliationError("native_challenger_write_guard_unavailable")
    exact_read_paths = {
        Path(path).expanduser().resolve(strict=False) for path in allowed_read_paths
    }
    read_roots = tuple(Path(path).expanduser().resolve(strict=False) for path in allowed_read_roots)

    def reject_formal_writes(event: str, args: tuple[object, ...]) -> None:
        if event in {
            "subprocess.Popen",
            "os.posix_spawn",
            "os.posix_spawnp",
            "os.exec",
            "os.execve",
            "os.chdir",
            "os.fchdir",
        }:
            name_hash = hashlib.sha256(b"<exec-child>").hexdigest()[:16]
            blocked_name_hashes.add(name_hash)
            _append_guard_violation(violation_marker, "<exec-child>")
            raise PermissionError("native_challenger_exec_forbidden")
        if _audit_event_has_ambiguous_relative_open(event, args):
            name_hash = hashlib.sha256(b"<relative-write-open>").hexdigest()[:16]
            blocked_name_hashes.add(name_hash)
            _append_guard_violation(
                violation_marker,
                "<relative-write-open>",
            )
            raise PermissionError("native_challenger_relative_write_forbidden")
        if event == "sqlite3.connect" and args:
            read_path = active_native_sqlite_read_path(args[0])
            if read_path is not None and (
                read_path in exact_read_paths
                or any(read_path == root or root in read_path.parents for root in read_roots)
            ):
                return
        write_paths = _audit_event_write_paths(event, args)
        for path in write_paths:
            if path == Path(os.devnull).resolve():
                continue
            if any(path == root or root in path.parents for root in allowed_roots):
                continue
            blocked_name_hashes.add(hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:16])
            _append_guard_violation(violation_marker, path.name)
            raise PermissionError("native_challenger_write_scope_violation")

    sys.addaudithook(reject_formal_writes)


def _remove_raw_generation_worker_root(worker_root: Path) -> None:
    try:
        shutil.rmtree(worker_root)
    except FileNotFoundError:
        return
    except OSError:
        raise AgentSourceRawReconciliationError("raw_generation_worker_cleanup_failed") from None
    try:
        worker_root.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise AgentSourceRawReconciliationError("raw_generation_worker_cleanup_failed") from None
    else:
        raise AgentSourceRawReconciliationError("raw_generation_worker_cleanup_failed")


def _install_raw_generation_write_guard(
    *,
    database_dir: Path,
    allowed_names: Iterable[str],
    allowed_write_roots: Iterable[Path],
    blocked_name_hashes: set[str],
    allowed_read_roots: Iterable[Path] = (),
    violation_marker: Path | None = None,
) -> None:
    database_root = Path(database_dir).expanduser().resolve(strict=False)
    allowed = frozenset(str(name) for name in allowed_names)
    allowed_roots = tuple(
        Path(path).expanduser().resolve(strict=False) for path in allowed_write_roots
    )
    if not allowed_roots:
        raise AgentSourceRawReconciliationError("raw_generation_worker_write_guard_unavailable")
    read_roots = tuple(Path(path).expanduser().resolve(strict=False) for path in allowed_read_roots)

    def is_allowed_database_path(path: Path) -> bool:
        if path.parent != database_root:
            return False
        name = path.name
        if name in allowed:
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
            for base in allowed
        )

    def reject_out_of_scope_writes(event: str, args: tuple[object, ...]) -> None:
        if event in {
            "subprocess.Popen",
            "os.posix_spawn",
            "os.posix_spawnp",
            "os.exec",
            "os.execve",
            "os.chdir",
            "os.fchdir",
        }:
            blocked_name_hashes.add(hashlib.sha256(b"<exec-child>").hexdigest()[:16])
            _append_guard_violation(violation_marker, "<exec-child>")
            raise PermissionError("raw_generation_worker_exec_forbidden")
        if _audit_event_has_ambiguous_relative_open(event, args):
            blocked_name_hashes.add(hashlib.sha256(b"<relative-write-open>").hexdigest()[:16])
            _append_guard_violation(
                violation_marker,
                "<relative-write-open>",
            )
            raise PermissionError("raw_generation_worker_relative_write_forbidden")
        if event == "sqlite3.connect" and args:
            read_path = active_native_sqlite_read_path(args[0])
            if read_path is not None and any(
                read_path == root or root in read_path.parents for root in read_roots
            ):
                return
        for path in _audit_event_write_paths(event, args):
            if path == Path(os.devnull).resolve():
                continue
            if event == "os.mkdir" and path == database_root and path.is_dir():
                continue
            if is_allowed_database_path(path) or any(
                path == root or root in path.parents for root in allowed_roots
            ):
                continue
            blocked_name_hashes.add(hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:16])
            _append_guard_violation(violation_marker, path.name)
            raise PermissionError("raw_generation_worker_write_scope_violation")

    sys.addaudithook(reject_out_of_scope_writes)


def _assert_raw_generation_parent_handles_closed(
    *,
    raw_db_path: Path,
    cursor_path: Path,
    coverage_path: Path,
) -> None:
    protected = {
        str(Path(path).expanduser().resolve(strict=False))
        for base in (raw_db_path, cursor_path, coverage_path)
        for path in (base, *_sqlite_sidecars(base))
    }
    try:
        opened = psutil.Process().open_files()
    except psutil.Error:
        raise AgentSourceRawReconciliationError(
            "raw_generation_parent_handle_audit_failed"
        ) from None
    if any(str(Path(item.path).expanduser().resolve(strict=False)) in protected for item in opened):
        raise AgentSourceRawReconciliationError("raw_generation_parent_database_handle_open")


def _reconcile_active_source_raw_capture_unlocked(
    *,
    config: Any,
    raw_db_path: Path,
    backup_dir: Path,
    sources: Iterable[Any],
    apply: bool,
    cycles: int = 2,
    batch_sessions: int = 100,
    batch_turns: int = 100,
    reset_derived_state: bool = True,
    require_all_active_sources: bool = True,
    runtime_writers_are_inactive: Callable[[], bool] | None = None,
    prepared_intent_sink: Callable[[Mapping[str, Any]], None] | None = None,
    session_identity_reconciliation: Mapping[str, Any] | None = None,
    reviewed_before_challenger: Mapping[str, Any] | None = None,
    reviewed_current_projection_reconciliation: Mapping[str, Any] | None = None,
    reviewed_plan_hash: str = "",
) -> dict[str, Any]:
    """Run a bounded, content-free Native-to-Raw recovery generation.

    A successful result establishes Raw-only reconciliation evidence only.  It
    intentionally does not mint Agent runtime receipts or claim a daemon
    restart: those require their own authenticated host/daemon paths.
    """
    raw_db_path = Path(raw_db_path).expanduser().resolve(strict=False)
    backup_dir = Path(backup_dir).expanduser().resolve(strict=False)
    database_dir = Path(config.database_dir).expanduser().resolve(strict=False)
    if raw_db_path.parent != database_dir:
        raise AgentSourceRawReconciliationError("raw_database_scope_mismatch")
    selected_sources, source_names = _validate_active_sources(
        sources,
        require_all_active_sources=require_all_active_sources,
    )
    before_challenger = (
        dict(reviewed_before_challenger)
        if reviewed_before_challenger is not None
        else _audit_native_to_raw_isolated(
            selected_sources,
            raw_db_path=raw_db_path,
            require_all_host_sources=require_all_active_sources,
            source_scope="active",
        )
    )
    planning_limits = _recovery_plan(
        selected_sources,
        batch_sessions=batch_sessions,
        batch_turns=batch_turns,
        minimum_generations=cycles,
        challenger_report=before_challenger,
    )
    try:
        current_projection_reconciliation = plan_current_projection_reconciliation(raw_db_path)
    except RawCurrentProjectionReconciliationError as exc:
        raise AgentSourceRawReconciliationError(exc.code) from None
    if (
        reviewed_current_projection_reconciliation is not None
        and current_projection_reconciliation != dict(reviewed_current_projection_reconciliation)
    ):
        raise AgentSourceRawReconciliationError("raw_current_projection_plan_drift")
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply" if apply else "dry_run",
        "support_manifest_hash": get_agent_source_support_manifest().manifest_hash,
        "active_sources": source_names,
        "before_challenger": before_challenger,
        "planning_limits": planning_limits,
        "daemon_restart_verified": False,
        "continuity_scope": "raw_only_engine_reopen_generations_v1",
        "current_projection_reconciliation": (current_projection_reconciliation),
    }
    if apply:
        result["reviewed_plan_hash"] = reviewed_plan_hash
    if not apply:
        result["ok"] = bool(before_challenger["ok"])
        return result
    is_inactive = runtime_writers_are_inactive or (
        lambda: _default_runtime_writers_are_inactive(database_dir)
    )
    if not is_inactive():
        raise AgentSourceRawReconciliationError("daemon_not_inactive")
    try:
        raw_db_kind = inspect_path_kind(raw_db_path)
    except DurableIOError:
        raise AgentSourceRawReconciliationError("raw_database_unavailable") from None
    if raw_db_kind == "missing":
        raise AgentSourceRawReconciliationError("raw_database_missing")
    if raw_db_kind != "file":
        raise AgentSourceRawReconciliationError("raw_database_not_regular")

    cursor_path = database_dir / "agent_sync_cursors.db"
    coverage_path = agent_source_coverage.coverage_state_path(database_dir)
    backups: dict[str, tuple[Path | None, Mapping[str, Any]]] = {}
    receipt_path = backup_dir / (
        "agent-source-raw-reconciliation-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    )
    try:
        backups["raw"] = _backup_sqlite(
            raw_db_path,
            backup_dir,
            "pre-agent-source-raw",
        )
        backups["cursor"] = _backup_sqlite(
            cursor_path,
            backup_dir,
            "pre-agent-source-cursor",
        )
        backups["coverage"] = _backup_coverage(coverage_path, backup_dir)
        result["backups"] = {name: record for name, (_path, record) in backups.items()}
        result["receipt_filename"] = receipt_path.name
        before_targets = _target_state(config, raw_db_path)
        result["before_state"] = before_targets
        prepared_intent = {
            **result,
            "status": "prepared",
            "before_state": before_targets,
        }
        _write_receipt(receipt_path, prepared_intent)
        result["prepared_receipt_sha256"] = _file_sha256(receipt_path)
        if prepared_intent_sink is not None:
            try:
                prepared_intent_sink(prepared_intent)
            except (OSError, ValueError, TypeError, KeyError):
                raise AgentSourceRawReconciliationError("migration_intent_write_failed") from None
    except BaseException:
        _discard_unbound_backups(backups, backup_dir=backup_dir)
        _unlink_targets_durably(
            (receipt_path,),
            error_code="unbound_receipt_cleanup_failed",
        )
        raise

    def rollback(status: str) -> None:
        try:
            _restore_recovery_state(
                backups=backups,
                raw_db_path=raw_db_path,
                cursor_path=cursor_path,
                coverage_path=coverage_path,
            )
            if _target_state(config, raw_db_path) != before_targets:
                raise AgentSourceRawReconciliationError("rollback_state_mismatch")
        except AgentSourceRawReconciliationError:
            _write_receipt(
                receipt_path,
                {**result, "status": "rollback_failed", "rollback_ok": False},
            )
            raise AgentSourceRawReconciliationError("rollback_failed") from None
        _write_receipt(
            receipt_path,
            {**result, "status": status, "rollback_ok": True},
        )

    allowed_names = {
        raw_db_path.name,
        f"{raw_db_path.name}-wal",
        f"{raw_db_path.name}-shm",
        cursor_path.name,
        f"{cursor_path.name}-wal",
        f"{cursor_path.name}-shm",
        coverage_path.name,
        ".mnemos_offline_migration.lock",
        "daemon.pid",
        ".mnemos_runtime_writer.lock",
    }
    before_files = _database_file_state(database_dir, allowed_names)
    process_write_scope = _ProcessDatabaseWriteScope(
        database_dir=database_dir,
        allowed_names=allowed_names,
        allowed_subtrees=(backup_dir,),
    )
    raw_generation_scope_evidence: list[dict[str, Any]] = []

    def record_process_write_scope_evidence() -> None:
        evidence = process_write_scope.evidence()
        blocked_values = evidence.get(
            "blocked_process_mutation_name_hashes"
        )
        if (
            not isinstance(blocked_values, list)
            or not all(isinstance(value, str) for value in blocked_values)
        ):
            raise AgentSourceRawReconciliationError(
                "process_write_scope_evidence_invalid"
            )
        blocked_hashes = set(
            blocked_values
        )
        worker_scopes_verified = True
        for worker_evidence in raw_generation_scope_evidence:
            worker_scope = worker_evidence["process_write_scope"]
            worker_guard = worker_evidence["worker_guard"]
            worker_scopes_verified = bool(
                worker_scopes_verified and worker_scope["process_write_scope_verified"] is True
            )
            blocked_hashes.update(
                str(value) for value in worker_scope["blocked_process_mutation_name_hashes"]
            )
            blocked_hashes.update(
                str(value) for value in worker_guard["blocked_process_mutation_name_hashes"]
            )
        blocked_count = len(blocked_hashes)
        foreign_names = _unexpected_database_mutations(
            before_files,
            _database_file_state(database_dir, allowed_names),
        )
        result.update(
            {
                **evidence,
                "process_write_scope_verified": bool(
                    evidence["process_write_scope_verified"] and worker_scopes_verified
                ),
                "raw_only_boundary_ok": blocked_count == 0,
                "unexpected_mutation_count": blocked_count,
                "blocked_process_mutation_count": blocked_count,
                "blocked_process_mutation_name_hashes": sorted(blocked_hashes),
                "unexpected_mutation_name_hashes": sorted(blocked_hashes),
                "raw_generation_worker_count": len(raw_generation_scope_evidence),
                "raw_generation_worker_scope_verified": (worker_scopes_verified),
                "foreign_concurrent_mutation_count": len(foreign_names),
                "foreign_concurrent_mutation_name_hashes": [
                    hashlib.sha256(name.encode("utf-8")).hexdigest()[:16] for name in foreign_names
                ],
            }
        )

    try:
        process_write_scope.start()
        try:
            result["current_projection_reconciliation"] = apply_current_projection_reconciliation(
                raw_db_path,
                expected_plan=current_projection_reconciliation,
            )
        except RawCurrentProjectionReconciliationError as exc:
            rollback("failed")
            raise AgentSourceRawReconciliationError(exc.code) from None
        identity_reconciliation = (
            _apply_session_identity_reconciliation_plan(
                raw_db_path,
                plan=session_identity_reconciliation,
                reviewed_plan_hash=reviewed_plan_hash,
            )
            if session_identity_reconciliation is not None
            else {
                "required_receipt_count": 0,
                "recorded_receipt_count": 0,
                "receipt_id_set_hash": _canonical_hash([]),
                "schema_created": False,
                "ok": True,
            }
        )
        result["session_identity_reconciliation"] = identity_reconciliation
        cursor_store = AgentSyncCursorStore(database_dir)
        resets = (
            [
                cursor_store.reset_source_reconciliation(source_name).__dict__
                for source_name in source_names
            ]
            if reset_derived_state
            else []
        )
        if reset_derived_state:
            try:
                _unlink_targets_durably(
                    (coverage_path,),
                    error_code="coverage_state_reset_failed",
                )
            except AgentSourceRawReconciliationError:
                rollback("failed")
                raise
        result["coverage_state_reset"] = bool(reset_derived_state)
        cycles_report: list[dict[str, Any]] = []
        for generation in range(int(planning_limits["generation_budget"])):
            limits = {
                key: int(planning_limits[key])
                for key in (
                    "tail_sessions_per_source",
                    "reconciliation_sessions_per_source",
                    "turns_per_session",
                )
            }
            worker_report = _run_raw_generation_isolated(
                config=config,
                raw_db_path=raw_db_path,
                cursor_path=cursor_path,
                coverage_path=coverage_path,
                sources=selected_sources,
                source_names=source_names,
                limits=limits,
                process_write_scope=process_write_scope,
                generation_number=generation + 1,
            )
            raw_generation_scope_evidence.append(
                {
                    "process_write_scope": dict(worker_report["process_write_scope"]),
                    "worker_guard": dict(worker_report["worker_guard"]),
                }
            )
            cycle_report = dict(worker_report["cycle"])
            error_evidence = list(worker_report["error_evidence"])
            reported_error_count = int(worker_report["reported_error_count"])
            typed_error_count = int(worker_report["typed_error_count"])
            cycles_report.append(
                {
                    "generation": generation + 1,
                    **cycle_report,
                    "error_evidence": error_evidence,
                    "typed_error_count": typed_error_count,
                    "error_evidence_conserved": (reported_error_count == typed_error_count),
                    "worker_isolation": dict(worker_report["worker_isolation"]),
                }
            )
            child_blocked_count = sum(
                int(evidence["blocked_process_mutation_count"])
                for evidence in (
                    worker_report["process_write_scope"],
                    worker_report["worker_guard"],
                )
            )
            if child_blocked_count:
                result.update(
                    {
                        "resets": resets,
                        "cycles": cycles_report,
                    }
                )
                record_process_write_scope_evidence()
                raise AgentSourceRawReconciliationError(
                    "raw_generation_worker_write_scope_violation"
                )
            non_retryable_codes = sorted(
                {
                    str(item.get("error_code") or "")
                    for item in error_evidence
                    if _raw_sync_error_is_nonretryable(item.get("error_code"))
                }
            )
            if non_retryable_codes:
                result.update(
                    {
                        "resets": resets,
                        "cycles": cycles_report,
                        "terminal_failure_codes": non_retryable_codes,
                    }
                )
                rollback("failed")
                raise AgentSourceRawReconciliationError(
                    "raw_reconciliation_nonretryable_source_failure"
                )
            coverage_after_cycle = agent_source_coverage.load_source_coverage_state(coverage_path)
            source_capture_after_cycle = {
                source_name: verify_source_capture(
                    source_name=source_name,
                    coverage=coverage_after_cycle,
                    cursor_db_path=cursor_path,
                    raw_db_path=raw_db_path,
                )
                for source_name in source_names
            }
            cycles_report[-1]["source_capture_ok"] = {
                source_name: bool(evidence.get("ok"))
                for source_name, evidence in source_capture_after_cycle.items()
            }
            if (
                generation + 1 >= int(planning_limits["minimum_generations"])
                and _coverage_generation_complete(coverage_after_cycle, source_names)
                and all(
                    bool(evidence.get("ok")) for evidence in source_capture_after_cycle.values()
                )
            ):
                break
        after_challenger = _audit_native_to_raw_isolated(
            selected_sources,
            raw_db_path=raw_db_path,
            require_all_host_sources=require_all_active_sources,
            source_scope="active",
        )
        coverage = agent_source_coverage.load_source_coverage_state(coverage_path)
        source_capture = {
            source_name: verify_source_capture(
                source_name=source_name,
                coverage=coverage,
                cursor_db_path=cursor_path,
                raw_db_path=raw_db_path,
            )
            for source_name in source_names
        }
        record_process_write_scope_evidence()
        result.update(
            {
                "resets": resets,
                "cycles": cycles_report,
                "after_challenger": after_challenger,
                "source_capture": source_capture,
            }
        )
        retryable_errors = [
            item
            for cycle in cycles_report
            for item in (cycle.get("error_evidence") or [])
            if not _raw_sync_error_is_nonretryable(item.get("error_code"))
        ]
        retryable_error_count = sum(int(item.get("count") or 0) for item in retryable_errors)
        recovered_retryable_error_count = sum(
            int(item.get("count") or 0)
            for item in retryable_errors
            if bool(source_capture.get(str(item.get("source") or ""), {}).get("ok"))
        )
        result.update(
            {
                "retryable_error_count": retryable_error_count,
                "recovered_retryable_error_count": (recovered_retryable_error_count),
                "unrecovered_retryable_error_count": (
                    retryable_error_count - recovered_retryable_error_count
                ),
                "retryable_error_codes": sorted(
                    {
                        str(item.get("error_code") or "")
                        for item in retryable_errors
                        if str(item.get("error_code") or "")
                    }
                ),
                "retryable_error_types": sorted(
                    {
                        str(item.get("error_type") or "")
                        for item in retryable_errors
                        if str(item.get("error_type") or "")
                    }
                ),
            }
        )
        source_capture_complete = all(
            bool(evidence.get("ok")) for evidence in source_capture.values()
        )
        error_evidence_conservation_ok = all(
            cycle.get("error_evidence_conserved") is True for cycle in cycles_report
        )
        result["error_evidence_conservation_ok"] = error_evidence_conservation_ok
        result["ok"] = bool(
            after_challenger["ok"]
            and source_capture_complete
            and result["raw_only_boundary_ok"]
            and result["unrecovered_retryable_error_count"] == 0
            and error_evidence_conservation_ok
        )
        if not result["ok"]:
            result["failure_reasons"] = [
                reason
                for reason, failed in (
                    (
                        "after_challenger_incomplete",
                        after_challenger["ok"] is not True,
                    ),
                    (
                        "source_capture_incomplete",
                        not source_capture_complete,
                    ),
                    (
                        "raw_only_boundary_violation",
                        not result["raw_only_boundary_ok"],
                    ),
                    (
                        "retryable_errors_unrecovered",
                        result["unrecovered_retryable_error_count"] > 0,
                    ),
                    (
                        "cycle_error_evidence_mismatch",
                        not error_evidence_conservation_ok,
                    ),
                )
                if failed
            ]
            rollback("failed")
            raise AgentSourceRawReconciliationError("raw_reconciliation_incomplete")
        try:
            with sqlite3.connect(str(raw_db_path), timeout=10) as conn:
                checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is not None and int(checkpoint[0] or 0) != 0:
                    raise AgentSourceRawReconciliationError("raw_reconciliation_checkpoint_busy")
        except AgentSourceRawReconciliationError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise AgentSourceRawReconciliationError(
                "raw_reconciliation_checkpoint_failed"
            ) from None
        record_process_write_scope_evidence()
    except KeyboardInterrupt:
        record_process_write_scope_evidence()
        rollback("interrupted")
        raise AgentSourceRawReconciliationError("reconciliation_interrupted") from None
    except AgentSourceRawReconciliationError:
        record_process_write_scope_evidence()
        if _target_state(config, raw_db_path) != before_targets:
            rollback("failed")
        else:
            _write_receipt(
                receipt_path,
                {**result, "status": "failed", "rollback_ok": True},
            )
        raise
    except AgentSyncCursorError as exc:
        record_process_write_scope_evidence()
        rollback("failed")
        if "schema" in str(exc):
            raise AgentSourceRawReconciliationError(
                "cursor_schema_reconciliation_required"
            ) from None
        raise AgentSourceRawReconciliationError("agent_sync_cursor_failure") from exc
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, RuntimeError):
        record_process_write_scope_evidence()
        rollback("failed")
        raise AgentSourceRawReconciliationError("raw_reconciliation_failed") from None
    finally:
        process_write_scope.close()
    _write_receipt(receipt_path, {**result, "status": "completed" if result["ok"] else "failed"})
    return result


def _bind_recovery_plan(
    result: Mapping[str, Any],
    *,
    config: Any,
    raw_db_path: Path,
    backup_dir: Path,
    native_inventory: NativeArtifactInventory,
    snapshot_evidence: Mapping[str, Any],
    reset_derived_state: bool,
    require_all_active_sources: bool,
    writers_inactive: bool,
    session_identity_reconciliation: Mapping[str, Any],
) -> dict[str, Any]:
    database_dir = Path(config.database_dir).expanduser().resolve(strict=False)
    current_state_ok = bool(result.get("ok"))
    phase1_governance_generation = _phase1_governance_generation_binding()
    try:
        raw_conservation = _conservation_summary(
            _safe_raw_conservation(raw_db_path)
        )
        raw_conservation_prestate_ok = True
    except AgentSourceRawReconciliationError as exc:
        raw_conservation = {
            "schema_version": "mnemos.raw_conservation_plan_binding.v1",
            "status": "unavailable",
            "error_code": str(exc),
        }
        raw_conservation_prestate_ok = False
    plan_material = {
        "plan_version": PLAN_VERSION,
        "root_id": "COG-045",
        "substate": "RM-IDENTITY",
        "apply_scope": {
            "raw_db": _file_scope(raw_db_path, sqlite_file=True),
            "cursor_db": _file_scope(
                database_dir / "agent_sync_cursors.db",
                sqlite_file=True,
            ),
            "coverage_state": _file_scope(agent_source_coverage.coverage_state_path(database_dir)),
            "backup_dir": str(Path(backup_dir).expanduser().resolve(strict=False)),
        },
        "support_manifest_hash": result["support_manifest_hash"],
        "active_sources": result["active_sources"],
        "native_artifact_inventory": native_inventory.to_evidence(),
        "native_artifact_snapshot": dict(snapshot_evidence),
        "native_snapshot_evidence": result["before_challenger"],
        "raw_conservation": raw_conservation,
        "raw_conservation_prestate_ok": raw_conservation_prestate_ok,
        "planning_limits": result["planning_limits"],
        "session_identity_reconciliation": dict(session_identity_reconciliation),
        "current_projection_reconciliation": dict(result["current_projection_reconciliation"]),
        "phase1_governance_generation": phase1_governance_generation,
        "reset_derived_state": reset_derived_state,
        "require_all_active_sources": require_all_active_sources,
        "writer_lock_state": ("writers_inactive" if writers_inactive else "active_or_unverified"),
        "allowed_delta": {
            "canonical_raw": (
                "replay visible native events through canonical upsert; existing identities "
                "may append observation rows and content changes may append revisions"
            ),
            "cursor": "reset_and_rebuild_derived_generation_evidence",
            "coverage": "replace_with_current_verified_generation",
            "semantic_writes": 0,
        },
        "ambiguous": [],
        "unresolved": list(session_identity_reconciliation.get("unresolved") or []),
        "apply_eligible": bool(
            writers_inactive
            and session_identity_reconciliation.get("ok") is True
            and result["current_projection_reconciliation"].get("ok") is True
            and phase1_governance_generation.get("ok") is True
            and raw_conservation_prestate_ok
        ),
    }
    return {
        **dict(result),
        "current_state_ok": current_state_ok,
        **plan_material,
        "ok": bool(plan_material["apply_eligible"]),
        "canonical_plan": plan_material,
        "plan_hash": _canonical_hash(plan_material),
    }


def _execute_unresolved_active_source_raw_capture_for_test(
    *,
    config: Any,
    raw_db_path: Path,
    backup_dir: Path,
    sources: Iterable[Any],
    apply: bool,
    cycles: int = 2,
    batch_sessions: int = 100,
    batch_turns: int = 100,
    reset_derived_state: bool = True,
    require_all_active_sources: bool = True,
    runtime_writers_are_inactive: Callable[[], bool] | None = None,
    expected_plan_hash: str = "",
    reviewed_plan_sink: Callable[[Mapping[str, Any]], None] | None = None,
    prepared_intent_sink: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Plan or run one exact-plan, offline-locked Raw-only recovery."""
    database_dir = Path(config.database_dir)
    source_list = list(sources)
    is_inactive = runtime_writers_are_inactive or (
        lambda: _default_runtime_writers_are_inactive(database_dir)
    )

    def build_plan(
        *,
        writers_inactive: bool,
        snapshot: SnapshotNativeSourceSet,
    ) -> dict[str, Any]:
        snapshot_evidence = _native_snapshot_evidence(snapshot, source_list)
        planned = _reconcile_active_source_raw_capture_unlocked(
            config=config,
            raw_db_path=raw_db_path,
            backup_dir=backup_dir,
            sources=snapshot.sources,
            apply=False,
            cycles=cycles,
            batch_sessions=batch_sessions,
            batch_turns=batch_turns,
            reset_derived_state=reset_derived_state,
            require_all_active_sources=require_all_active_sources,
            runtime_writers_are_inactive=lambda: writers_inactive,
        )
        identity_reconciliation = _session_identity_reconciliation_plan(
            raw_db_path,
            snapshot.sources,
        )
        return _bind_recovery_plan(
            planned,
            config=config,
            raw_db_path=raw_db_path,
            backup_dir=backup_dir,
            native_inventory=snapshot.inventory,
            snapshot_evidence=snapshot_evidence,
            reset_derived_state=reset_derived_state,
            require_all_active_sources=require_all_active_sources,
            writers_inactive=writers_inactive,
            session_identity_reconciliation=identity_reconciliation,
        )

    writers_inactive = bool(is_inactive())
    if not apply:
        try:
            with snapshot_native_sources(source_list) as snapshot:
                return build_plan(
                    writers_inactive=writers_inactive,
                    snapshot=snapshot,
                )
        except NativeArtifactInventoryError as exc:
            raise reconciliation_error_from_typed_failure(exc) from None
    if not expected_plan_hash:
        raise AgentSourceRawReconciliationError("expected_plan_hash_required")
    if not writers_inactive:
        raise AgentSourceRawReconciliationError("daemon_not_inactive")
    try:
        with offline_migration_lock(
            database_dir,
            daemon_check=lambda _database_dir: bool(is_inactive()),
        ):
            try:
                with snapshot_native_sources(source_list) as snapshot:
                    locked_plan = build_plan(
                        writers_inactive=True,
                        snapshot=snapshot,
                    )
                    if expected_plan_hash != locked_plan["plan_hash"]:
                        raise AgentSourceRawReconciliationError("expected_plan_hash_mismatch")
                    if locked_plan.get("apply_eligible") is not True:
                        raise AgentSourceRawReconciliationError("recovery_plan_not_apply_eligible")
                    if reviewed_plan_sink is not None:
                        reviewed_plan_sink(locked_plan)
                    applied = _reconcile_active_source_raw_capture_unlocked(
                        config=config,
                        raw_db_path=raw_db_path,
                        backup_dir=backup_dir,
                        sources=snapshot.sources,
                        apply=True,
                        cycles=cycles,
                        batch_sessions=batch_sessions,
                        batch_turns=batch_turns,
                        reset_derived_state=reset_derived_state,
                        require_all_active_sources=require_all_active_sources,
                        runtime_writers_are_inactive=lambda: True,
                        prepared_intent_sink=prepared_intent_sink,
                        session_identity_reconciliation=locked_plan[
                            "session_identity_reconciliation"
                        ],
                        reviewed_before_challenger=locked_plan["native_snapshot_evidence"],
                        reviewed_current_projection_reconciliation=locked_plan[
                            "current_projection_reconciliation"
                        ],
                        reviewed_plan_hash=expected_plan_hash,
                    )
            except NativeArtifactInventoryError as exc:
                raise reconciliation_error_from_typed_failure(exc) from None
    except AgentSourceRawReconciliationError:
        raise
    except RuntimeError:
        raise AgentSourceRawReconciliationError("writer_lock_unavailable") from None
    applied["reviewed_plan_hash"] = expected_plan_hash
    return applied


from scripts import (
    agent_source_raw_migration_certification as _migration_certification,
)
from scripts.agent_source_raw_migration_certification import (
    _target_state,
    _safe_raw_conservation,
    _migration_receipt_path,
    _archive_terminal_migration_receipt,
    _archive_terminal_migration_lineage,
    _restore_drill_ok,
    _integrity_sqlite,
    _recover_prepared_raw_receipt,
    _post_apply_raw_gap,
    _verify_completed_raw_receipt,
)


from scripts import agent_source_raw_migration_runtime as _migration_runtime


def reconcile_active_source_raw_capture(
    *,
    config: Any,
    raw_db_path: Path,
    backup_dir: Path,
    sources: Iterable[Any],
    apply: bool,
    cycles: int = 2,
    batch_sessions: int = 100,
    batch_turns: int = 100,
    reset_derived_state: bool = True,
    require_all_active_sources: bool = True,
    runtime_writers_are_inactive: Callable[[], bool] | None = None,
    expected_plan_hash: str = "",
) -> dict[str, Any]:
    """Delegate through the CLI owner's injectable transaction seams."""
    certification_dependencies = (
        _migration_certification.CertificationDependencies(
            archive_terminal_migration_receipt=(
                _archive_terminal_migration_receipt
            ),
            backups_from_records=_backups_from_records,
            file_sha256=_file_sha256,
            post_apply_raw_gap=_post_apply_raw_gap,
            recovery_execution_dependency_hashes=(
                _recovery_execution_dependency_hashes
            ),
            restore_recovery_state=_restore_recovery_state,
            write_receipt=_write_receipt,
            runtime_execution_identity=runtime_execution_identity,
        )
    )
    runtime_dependencies = _migration_runtime.RuntimeDependencies(
        archive_terminal_migration_lineage=(
            _archive_terminal_migration_lineage
        ),
        backups_from_records=_backups_from_records,
        compare_raw_conservation=_compare_raw_conservation,
        conservation_summary=_conservation_summary,
        default_runtime_writers_are_inactive=(
            _default_runtime_writers_are_inactive
        ),
        ensure_private_backup_dir=_ensure_private_backup_dir,
        execute_recovery=(
            _execute_unresolved_active_source_raw_capture_for_test
        ),
        file_sha256=_file_sha256,
        mark_reconciliation_receipt_rolled_back=(
            _mark_reconciliation_receipt_rolled_back
        ),
        migration_receipt_path=_migration_receipt_path,
        post_apply_raw_gap=_post_apply_raw_gap,
        raw_conservation_findings=_raw_conservation_findings,
        read_private_backup_bytes=_read_private_backup_bytes,
        recover_prepared_raw_receipt=_recover_prepared_raw_receipt,
        restore_drill_ok=_restore_drill_ok,
        restore_recovery_state=_restore_recovery_state,
        safe_raw_conservation=_safe_raw_conservation,
        target_state=_target_state,
        unlink_targets_durably=_unlink_targets_durably,
        verify_completed_raw_receipt=_verify_completed_raw_receipt,
        write_receipt=_write_receipt,
        coverage_state_path=agent_source_coverage.coverage_state_path,
        offline_migration_lock=offline_migration_lock,
    )
    with _migration_certification.certification_dependency_scope(
        certification_dependencies
    ):
        return _migration_runtime.reconcile_active_source_raw_capture(
            dependencies=runtime_dependencies,
            config=config,
            raw_db_path=raw_db_path,
            backup_dir=backup_dir,
            sources=sources,
            apply=apply,
            cycles=cycles,
            batch_sessions=batch_sessions,
            batch_turns=batch_turns,
            reset_derived_state=reset_derived_state,
            require_all_active_sources=require_all_active_sources,
            runtime_writers_are_inactive=runtime_writers_are_inactive,
            expected_plan_hash=expected_plan_hash,
        )


def main(argv: list[str] | None = None) -> int:
    dependencies = _CliDependencies(
        config_factory=Config,
        load_active_sources=lambda: list(load_manifest_active_sources()),
        with_current_codex_cutoff=_with_explicit_current_codex_cutoff,
        reconcile=reconcile_active_source_raw_capture,
        write_new_receipt=_write_new_receipt,
        file_sha256=_file_sha256,
    )
    return _cli_main(argv, dependencies=dependencies)


if __name__ == "__main__":
    raise SystemExit(main())
