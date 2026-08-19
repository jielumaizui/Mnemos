"""Certification and recovery receipts for Agent Native-to-Raw migration."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Iterable, Mapping
import uuid

from core.agent_kit.source_capture_verification import verify_source_capture
from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
from core.ops.durable_io import (
    DurableIOError,
    fsync_directory,
    inspect_path_kind,
    normalize_private_sqlite_copy,
    owned_sqlite_connection_pair,
    physical_scope_signature,
    private_sqlite_sidecars,
    validate_private_sqlite_copy,
)
from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.ops.runtime_execution_identity import runtime_execution_identity
from core.sync_framework.native_artifact_inventory import (
    NativeArtifactInventoryError,
    build_native_artifact_inventory,
)
from core.sync_framework.native_raw_recovery_evidence import (
    NativeRawRecoveryEvidenceError,
    conservation_summary as _conservation_summary,
    raw_conservation_evidence as _raw_conservation_evidence,
)
from daemon import agent_source_coverage
from scripts.agent_source_raw_recovery_contract import (
    AgentSourceRawReconciliationError,
    SCHEMA_VERSION,
    raw_generation_worker_isolation_contract,
    reconciliation_error_from_typed_failure,
)
from scripts.agent_source_raw_recovery_support import (
    _backups_from_records,
    _canonical_hash,
    _create_private_target,
    _file_scope,
    _file_sha256,
    _mark_reconciliation_receipt_rolled_back,
    _private_backup_file_ok,
    _read_private_backup_bytes,
    _receipt_bytes,
    _recovery_execution_dependency_hashes,
    _restore_recovery_state,
    _sqlite_sidecars,
    _sqlite_snapshot_sha256,
    _write_receipt,
)


@dataclass(frozen=True)
class CertificationDependencies:
    """Context-local seams used while certifying one migration transaction."""

    archive_terminal_migration_receipt: Callable[..., str]
    backups_from_records: Callable[..., Any]
    file_sha256: Callable[[Path], str]
    post_apply_raw_gap: Callable[..., dict[str, Any]]
    recovery_execution_dependency_hashes: Callable[[], dict[str, str]]
    restore_recovery_state: Callable[..., None]
    write_receipt: Callable[[Path, Mapping[str, Any]], None]
    runtime_execution_identity: Callable[[], dict[str, Any]]


_ACTIVE_CERTIFICATION_DEPENDENCIES: ContextVar[
    CertificationDependencies | None
] = ContextVar(
    "agent_source_raw_certification_dependencies",
    default=None,
)


def _verify_session_identity_reconciliation_plan(*args, **kwargs):
    from scripts.reconcile_agent_source_raw_capture import (
        _verify_session_identity_reconciliation_plan as verify,
    )

    return verify(*args, **kwargs)


def _target_state(config: Any, raw_db_path: Path) -> dict[str, Any]:
    database_dir = Path(config.database_dir)
    return {
        "raw": _file_scope(raw_db_path, sqlite_file=True),
        "cursor": _file_scope(
            database_dir / "agent_sync_cursors.db",
            sqlite_file=True,
        ),
        "coverage": _file_scope(agent_source_coverage.coverage_state_path(database_dir)),
    }


def _safe_raw_conservation(raw_db_path: Path) -> dict[str, Any]:
    try:
        return _raw_conservation_evidence(raw_db_path)
    except NativeRawRecoveryEvidenceError as exc:
        raise AgentSourceRawReconciliationError(str(exc)) from None


def _migration_receipt_path(backup_dir: Path, plan_hash: str) -> Path:
    suffix = plan_hash.removeprefix("sha256:")
    return Path(backup_dir).expanduser().resolve(strict=False) / (
        f"agent-source-raw-migration.{suffix}.json"
    )


def _archive_terminal_migration_receipt(
    *,
    receipt_path: Path,
    backup_dir: Path,
    plan_hash: str,
) -> str:
    try:
        receipt_bytes = _read_private_backup_bytes(receipt_path, backup_dir)
        receipt = json.loads(receipt_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AgentSourceRawReconciliationError("migration_receipt_unreadable") from None
    if receipt.get("status") != "recovered_rollback":
        raise AgentSourceRawReconciliationError("migration_receipt_binding_mismatch")
    digest = hashlib.sha256(receipt_bytes).hexdigest()
    suffix = plan_hash.removeprefix("sha256:")
    archive_path = backup_dir / (f"agent-source-raw-migration-history.{suffix}.{digest}.json")
    try:
        archive_kind = inspect_path_kind(archive_path)
    except DurableIOError:
        raise AgentSourceRawReconciliationError("migration_receipt_history_unavailable") from None
    if archive_kind == "file":
        if (
            not _private_backup_file_ok(archive_path, backup_dir)
            or _read_private_backup_bytes(archive_path, backup_dir) != receipt_bytes
        ):
            raise AgentSourceRawReconciliationError("migration_receipt_history_conflict")
        return archive_path.name
    if archive_kind != "missing":
        raise AgentSourceRawReconciliationError("migration_receipt_history_conflict")
    temporary = archive_path.with_name(f".{archive_path.name}.{uuid.uuid4().hex}.tmp")
    temporary_created = False
    try:
        with open(temporary, "xb") as handle:
            temporary_created = True
            handle.write(receipt_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, archive_path)
        fsync_directory(backup_dir)
    except OSError:
        raise AgentSourceRawReconciliationError("migration_receipt_history_write_failed") from None
    finally:
        if temporary_created:
            temporary.unlink(missing_ok=True)
    return archive_path.name


def _archive_terminal_migration_lineage(
    *,
    receipt_path: Path,
    backup_dir: Path,
    plan_hash: str,
) -> list[str]:
    try:
        receipt = json.loads(
            _read_private_backup_bytes(receipt_path, backup_dir).decode("utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AgentSourceRawReconciliationError("migration_receipt_unreadable") from None
    inherited = receipt.get("prior_terminal_receipts")
    if (
        receipt.get("status") != "recovered_rollback"
        or not isinstance(inherited, list)
        or any(not isinstance(value, str) for value in inherited)
        or len(inherited) != len(set(inherited))
    ):
        raise AgentSourceRawReconciliationError("migration_receipt_history_invalid")
    current = _certification_dependencies().archive_terminal_migration_receipt(
        receipt_path=receipt_path,
        backup_dir=backup_dir,
        plan_hash=plan_hash,
    )
    if current in inherited:
        raise AgentSourceRawReconciliationError("migration_receipt_history_invalid")
    return [*inherited, current]


def _validate_prior_terminal_receipts(
    *,
    receipt: Mapping[str, Any],
    backup_dir: Path,
    expected_plan_hash: str,
) -> None:
    filenames = receipt.get("prior_terminal_receipts", [])
    if (
        not isinstance(filenames, list)
        or any(not isinstance(value, str) for value in filenames)
        or len(filenames) != len(set(filenames))
    ):
        raise AgentSourceRawReconciliationError("migration_receipt_history_invalid")
    plan_suffix = expected_plan_hash.removeprefix("sha256:")
    history_prefix = f"agent-source-raw-migration-history.{plan_suffix}."
    pattern = re.compile(rf"{re.escape(history_prefix)}([0-9a-f]{{64}})\.json")
    declared = {str(value) for value in filenames}
    actual: set[str] = set()
    try:
        history_paths = list(backup_dir.glob(f"{history_prefix}*.json"))
    except OSError:
        raise AgentSourceRawReconciliationError("migration_receipt_history_unavailable") from None
    for path in history_paths:
        try:
            history_kind = inspect_path_kind(path)
        except DurableIOError:
            raise AgentSourceRawReconciliationError(
                "migration_receipt_history_unavailable"
            ) from None
        if history_kind != "file":
            raise AgentSourceRawReconciliationError("migration_receipt_history_invalid")
        actual.add(path.name)
    allowed_actual = set(declared)
    if receipt.get("status") == "recovered_rollback":
        current_bytes = _receipt_bytes(receipt)
        current_archive_name = (
            f"{history_prefix}" f"{hashlib.sha256(current_bytes).hexdigest()}.json"
        )
        if current_archive_name in actual:
            allowed_actual.add(current_archive_name)
    if actual != allowed_actual:
        raise AgentSourceRawReconciliationError("migration_receipt_history_invalid")
    payloads: dict[str, Mapping[str, Any]] = {}
    for filename_value in sorted(actual):
        filename = str(filename_value)
        matched = pattern.fullmatch(filename)
        path = backup_dir / filename
        if (
            matched is None
            or Path(filename).name != filename
            or not _private_backup_file_ok(path, backup_dir)
        ):
            raise AgentSourceRawReconciliationError("migration_receipt_history_invalid")
        try:
            payload_bytes = _read_private_backup_bytes(path, backup_dir)
            payload = json.loads(payload_bytes)
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise AgentSourceRawReconciliationError("migration_receipt_history_invalid") from None
        if (
            hashlib.sha256(payload_bytes).hexdigest() != matched.group(1)
            or payload.get("schema_version") != "mnemos.agent_source_raw_migration_receipt.v1"
            or payload.get("status") != "recovered_rollback"
            or payload.get("plan_hash") != expected_plan_hash
            or payload.get("backup_dir") != str(backup_dir.resolve())
        ):
            raise AgentSourceRawReconciliationError("migration_receipt_history_invalid")
        payloads[filename] = payload
    for index, filename in enumerate(filenames):
        if payloads[filename].get("prior_terminal_receipts") != filenames[:index]:
            raise AgentSourceRawReconciliationError("migration_receipt_history_invalid")
    for filename in actual - declared:
        if payloads[filename].get("prior_terminal_receipts") != filenames:
            raise AgentSourceRawReconciliationError("migration_receipt_history_invalid")


def _migration_physical_signature(
    *,
    config: Any,
    raw_db_path: Path,
    backup_dir: Path,
    receipt_path: Path,
) -> dict[str, object]:
    database_dir = Path(config.database_dir)
    cursor_path = database_dir / "agent_sync_cursors.db"
    coverage_path = agent_source_coverage.coverage_state_path(database_dir)
    return physical_scope_signature(
        (
            raw_db_path,
            *_sqlite_sidecars(raw_db_path),
            cursor_path,
            *_sqlite_sidecars(cursor_path),
            coverage_path,
            receipt_path,
        ),
        inventory_directory=backup_dir,
    )


def _restore_drill_ok(
    *,
    before: Mapping[str, Any],
    backups: Mapping[str, Any],
    backup_dir: Path,
) -> bool:
    for name in ("raw", "cursor", "coverage"):
        expected = before[name]
        backup = backups[name]
        if bool(expected.get("present")) != bool(backup.get("present")):
            return False
        if not expected.get("present"):
            continue
        backup_path = backup_dir / str(backup.get("filename") or "")
        if not _private_backup_file_ok(backup_path, backup_dir):
            return False
        if (
            str(backup.get("sha256") or "")
            != _certification_dependencies().file_sha256(backup_path)
        ):
            return False
        if name in {"raw", "cursor"}:
            try:
                validate_private_sqlite_copy(backup_path)
            except DurableIOError:
                return False
            temporary = backup_path.with_name(
                f".{backup_path.name}.{uuid.uuid4().hex}.restore-drill"
            )
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
                    lambda: connect_readonly_sqlite(
                        backup_path,
                        immutable=True,
                    ),
                    lambda: sqlite3.connect(str(temporary)),
                ) as (source, destination):
                    source.backup(destination)
                normalize_private_sqlite_copy(temporary)
                if _integrity_sqlite(
                    temporary, immutable=True
                ) != "ok" or f"sha256:{_sqlite_snapshot_sha256(temporary, immutable=True)}" != expected.get(
                    "sha256"
                ):
                    return False
            except (DurableIOError, OSError, sqlite3.Error):
                return False
            finally:
                if temporary_created:
                    temporary.unlink(missing_ok=True)
                    for sidecar in private_sqlite_sidecars(temporary):
                        sidecar.unlink(missing_ok=True)
        else:
            temporary = backup_path.with_name(
                f".{backup_path.name}.{uuid.uuid4().hex}.restore-drill"
            )
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
                shutil.copyfile(backup_path, temporary)
                if (
                    "sha256:"
                    f"{_certification_dependencies().file_sha256(temporary)}"
                    != expected.get("sha256")
                ):
                    return False
            except OSError:
                return False
            finally:
                if temporary_created:
                    temporary.unlink(missing_ok=True)
    return True


def _backup_preimages_are_current(
    *,
    before: Mapping[str, Any],
    backups: Mapping[str, Any],
    backup_dir: Path,
) -> bool:
    """Read-only same-plan validation; the first apply already ran restore drills."""

    for name in ("raw", "cursor", "coverage"):
        expected = before[name]
        backup = backups[name]
        if bool(expected.get("present")) != bool(backup.get("present")):
            return False
        if not expected.get("present"):
            continue
        backup_path = backup_dir / str(backup.get("filename") or "")
        if not _private_backup_file_ok(backup_path, backup_dir) or str(
            backup.get("sha256") or ""
        ) != _certification_dependencies().file_sha256(backup_path):
            return False
        if name in {"raw", "cursor"}:
            try:
                validate_private_sqlite_copy(backup_path)
            except DurableIOError:
                return False
            if _integrity_sqlite(
                backup_path, immutable=True
            ) != "ok" or f"sha256:{_sqlite_snapshot_sha256(backup_path, immutable=True)}" != expected.get(
                "sha256"
            ):
                return False
        elif (
            "sha256:"
            f"{_certification_dependencies().file_sha256(backup_path)}"
            != expected.get("sha256")
        ):
            return False
    return True


def _require_quiescent_sqlite_main_file(path: Path) -> None:
    """Require a main-file-only snapshot before lock-free verification copies."""

    wal_path = Path(f"{path}-wal")
    try:
        wal_kind = inspect_path_kind(wal_path)
        if wal_kind not in {"missing", "file"}:
            raise AgentSourceRawReconciliationError("same_plan_live_wal_unreadable")
        if wal_kind == "file" and wal_path.stat().st_size != 0:
            raise AgentSourceRawReconciliationError("same_plan_live_wal_not_quiescent")
    except DurableIOError:
        raise AgentSourceRawReconciliationError("same_plan_live_wal_unreadable") from None


def _copy_verification_file(
    source: Path,
    target: Path,
    *,
    sqlite_file: bool = False,
) -> None:
    try:
        source_kind = inspect_path_kind(source)
        if source_kind == "missing":
            raise AgentSourceRawReconciliationError("same_plan_verification_source_missing")
        if source_kind != "file":
            raise AgentSourceRawReconciliationError("same_plan_verification_source_unavailable")
        _create_private_target(target)
        shutil.copyfile(source, target)
        if sqlite_file:
            normalize_private_sqlite_copy(target)
        os.chmod(target, 0o600)
    except AgentSourceRawReconciliationError:
        raise
    except DurableIOError:
        raise AgentSourceRawReconciliationError(
            "same_plan_verification_source_unavailable"
        ) from None
    except (DurableIOError, OSError):
        raise AgentSourceRawReconciliationError("same_plan_verification_copy_failed") from None


def _state_without_paths(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(name): {str(key): value for key, value in record.items() if str(key) != "path"}
        for name, record in state.items()
        if isinstance(record, Mapping)
    }


def _integrity_sqlite(path: Path, *, immutable: bool = False) -> str:
    try:
        with connect_readonly_sqlite(
            path,
            immutable=immutable,
        ) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                return "foreign_key_error"
    except (OSError, sqlite3.Error):
        return "unreadable"
    return str(row[0] or "") if row else "unreadable"


def _validated_receipt_plan(
    *,
    receipt: Mapping[str, Any],
    raw_db_path: Path,
    backup_dir: Path,
    expected_plan_hash: str,
) -> Mapping[str, Any]:
    reviewed_plan = receipt.get("reviewed_plan")
    if not isinstance(reviewed_plan, Mapping):
        raise AgentSourceRawReconciliationError("migration_receipt_binding_mismatch")
    material = dict(reviewed_plan)
    apply_scope = reviewed_plan.get("apply_scope")
    before_state = receipt.get("before_state")
    raw_scope = apply_scope.get("raw_db") if isinstance(apply_scope, Mapping) else None
    cursor_scope = apply_scope.get("cursor_db") if isinstance(apply_scope, Mapping) else None
    coverage_scope = apply_scope.get("coverage_state") if isinstance(apply_scope, Mapping) else None
    if (
        _canonical_hash(material) != expected_plan_hash
        or not isinstance(apply_scope, Mapping)
        or apply_scope.get("backup_dir") != str(backup_dir.resolve())
        or not isinstance(raw_scope, Mapping)
        or raw_scope.get("path") != str(Path(raw_db_path).resolve())
        or not isinstance(cursor_scope, Mapping)
        or not isinstance(coverage_scope, Mapping)
        or not isinstance(before_state, Mapping)
        or before_state.get("raw") != raw_scope
        or before_state.get("cursor") != cursor_scope
        or before_state.get("coverage") != coverage_scope
        or receipt.get("before_conservation") != reviewed_plan.get("raw_conservation")
        or receipt.get("native_inventory_hash")
        != reviewed_plan.get("native_artifact_inventory", {}).get("inventory_hash")
    ):
        raise AgentSourceRawReconciliationError("migration_receipt_binding_mismatch")
    return reviewed_plan


def _recover_prepared_raw_receipt(
    *,
    config: Any,
    raw_db_path: Path,
    backup_dir: Path,
    expected_plan_hash: str,
) -> str:
    """Recover a process-killed migration before recalculating its plan."""

    receipt_path = _migration_receipt_path(backup_dir, expected_plan_hash)
    try:
        receipt_kind = inspect_path_kind(receipt_path)
    except DurableIOError:
        raise AgentSourceRawReconciliationError("migration_receipt_unreadable") from None
    if receipt_kind == "missing":
        return ""
    if receipt_kind != "file":
        raise AgentSourceRawReconciliationError("migration_receipt_permissions_invalid")
    if not _private_backup_file_ok(receipt_path, backup_dir):
        raise AgentSourceRawReconciliationError("migration_receipt_permissions_invalid")
    try:
        receipt = json.loads(
            _read_private_backup_bytes(receipt_path, backup_dir).decode("utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AgentSourceRawReconciliationError("migration_receipt_unreadable") from None
    status = str(receipt.get("status") or "")
    if status == "completed":
        return status
    if status not in {"prepared", "recovered_rollback"}:
        raise AgentSourceRawReconciliationError("migration_receipt_binding_mismatch")
    required_backup_names = {"raw", "cursor", "coverage"}
    backups = receipt.get("backups")
    before_state = receipt.get("before_state")
    before_conservation = receipt.get("before_conservation")
    inner_receipt_filename = str(receipt.get("inner_receipt_filename") or "")
    inner_prepared_sha256 = str(receipt.get("inner_prepared_receipt_sha256") or "")
    if (
        receipt.get("schema_version") != "mnemos.agent_source_raw_migration_receipt.v1"
        or receipt.get("plan_hash") != expected_plan_hash
        or receipt.get("raw_db") != str(Path(raw_db_path).resolve())
        or receipt.get("backup_dir") != str(backup_dir.resolve())
        or not isinstance(backups, Mapping)
        or set(backups) != required_backup_names
        or not isinstance(before_state, Mapping)
        or set(before_state) != required_backup_names
        or not isinstance(before_conservation, Mapping)
        or not inner_receipt_filename
        or Path(inner_receipt_filename).name != inner_receipt_filename
        or not inner_receipt_filename.startswith("agent-source-raw-reconciliation-")
        or not re.fullmatch(r"[0-9a-f]{64}", inner_prepared_sha256)
    ):
        raise AgentSourceRawReconciliationError("migration_receipt_binding_mismatch")
    reviewed_plan = _validated_receipt_plan(
        receipt=receipt,
        raw_db_path=raw_db_path,
        backup_dir=backup_dir,
        expected_plan_hash=expected_plan_hash,
    )
    _validate_prior_terminal_receipts(
        receipt=receipt,
        backup_dir=backup_dir,
        expected_plan_hash=expected_plan_hash,
    )
    snapshot_contract = reviewed_plan.get("native_artifact_snapshot")
    if (
        not isinstance(snapshot_contract, Mapping)
        or snapshot_contract.get("execution_dependency_hashes")
        != _certification_dependencies().recovery_execution_dependency_hashes()
        or snapshot_contract.get("runtime_execution_identity")
        != _certification_dependencies().runtime_execution_identity()
        or reviewed_plan.get("support_manifest_hash")
        != get_agent_source_support_manifest().manifest_hash
    ):
        raise AgentSourceRawReconciliationError("migration_receipt_code_drift")
    for record in backups.values():
        if not isinstance(record, Mapping):
            raise AgentSourceRawReconciliationError("migration_receipt_backup_invalid")
        filename = str(record.get("filename") or "")
        if record.get("present") and (not filename or Path(filename).name != filename):
            raise AgentSourceRawReconciliationError("migration_receipt_backup_invalid")
    inner_receipt_path = backup_dir / inner_receipt_filename
    try:
        inner_receipt_bytes = _read_private_backup_bytes(
            inner_receipt_path,
            backup_dir,
        )
        inner_receipt = json.loads(inner_receipt_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AgentSourceRawReconciliationError("migration_receipt_binding_mismatch") from None
    inner_receipt_sha256 = hashlib.sha256(inner_receipt_bytes).hexdigest()
    if status == "recovered_rollback":
        if (
            receipt.get("rollback_ok") is not True
            or inner_receipt_sha256 != str(receipt.get("inner_receipt_sha256") or "")
            or inner_receipt.get("status") != "rolled_back_by_migration_certification"
            or inner_receipt.get("rollback_ok") is not True
            or inner_receipt.get("prepared_receipt_sha256") != inner_prepared_sha256
        ):
            raise AgentSourceRawReconciliationError("migration_receipt_binding_mismatch")
        return status
    inner_status = str(inner_receipt.get("status") or "")
    if inner_status == "prepared":
        inner_lineage_ok = inner_receipt_sha256 == inner_prepared_sha256
    else:
        inner_lineage_ok = bool(
            inner_status == "completed"
            and inner_receipt_sha256 == str(receipt.get("inner_receipt_sha256") or "")
            and receipt.get("inner_receipt_status") == "completed"
            and inner_receipt.get("prepared_receipt_sha256") == inner_prepared_sha256
        )
    if (
        not inner_lineage_ok
        or inner_receipt.get("backups") != backups
        or inner_receipt.get("before_state") != before_state
        or inner_receipt.get("reviewed_plan_hash") != expected_plan_hash
        or inner_receipt.get("support_manifest_hash") != reviewed_plan.get("support_manifest_hash")
        or inner_receipt.get("active_sources") != reviewed_plan.get("active_sources")
    ):
        raise AgentSourceRawReconciliationError("migration_receipt_binding_mismatch")
    if not _restore_drill_ok(
        before=before_state,
        backups=backups,
        backup_dir=backup_dir,
    ):
        raise AgentSourceRawReconciliationError("migration_receipt_backup_invalid")
    database_dir = Path(config.database_dir)
    dependencies = _certification_dependencies()
    dependencies.restore_recovery_state(
        backups=dependencies.backups_from_records(backups, backup_dir),
        raw_db_path=Path(raw_db_path),
        cursor_path=database_dir / "agent_sync_cursors.db",
        coverage_path=agent_source_coverage.coverage_state_path(database_dir),
    )
    if _target_state(config, raw_db_path) != before_state:
        raise AgentSourceRawReconciliationError("rollback_state_mismatch")
    if _conservation_summary(_safe_raw_conservation(raw_db_path)) != before_conservation:
        raise AgentSourceRawReconciliationError("rollback_state_mismatch")
    _mark_reconciliation_receipt_rolled_back(
        backup_dir=backup_dir,
        applied={"receipt_filename": inner_receipt_filename},
    )
    rolled_back_inner_sha256 = dependencies.file_sha256(inner_receipt_path)
    dependencies.write_receipt(
        receipt_path,
        {
            **receipt,
            "status": "recovered_rollback",
            "rollback_ok": True,
            "recovered_after_process_interruption": True,
            "inner_receipt_status": ("rolled_back_by_migration_certification"),
            "inner_receipt_sha256": rolled_back_inner_sha256,
        },
    )
    return "recovered_rollback"


def _post_apply_raw_gap(
    *,
    config: Any,
    raw_db_path: Path,
    sources: Iterable[Any],
    expected_inventory_hash: str,
    require_all_active_sources: bool,
    session_identity_reconciliation: Mapping[str, Any],
) -> dict[str, Any]:
    """Rerun the independent Native/Raw/cursor/coverage gap oracle."""
    from scripts.reconcile_agent_source_raw_capture import (
        _audit_native_to_raw_isolated,
    )

    source_list = tuple(sources)
    database_dir = Path(config.database_dir)
    try:
        current_inventory = build_native_artifact_inventory(source_list)
    except NativeArtifactInventoryError as exc:
        raise reconciliation_error_from_typed_failure(exc) from None
    if current_inventory.inventory_hash != expected_inventory_hash:
        raise AgentSourceRawReconciliationError("post_apply_native_snapshot_drift")
    manifest = get_agent_source_support_manifest()
    names = sorted(str(source.name) for source in source_list)
    if require_all_active_sources and set(names) != set(manifest.active_source_names):
        raise AgentSourceRawReconciliationError("post_apply_active_source_roster_gap")
    challenger = _audit_native_to_raw_isolated(
        source_list,
        raw_db_path=raw_db_path,
        require_all_host_sources=require_all_active_sources,
        source_scope="active",
    )
    coverage = agent_source_coverage.load_source_coverage_state(
        agent_source_coverage.coverage_state_path(database_dir)
    )
    cursor_path = database_dir / "agent_sync_cursors.db"
    source_capture = {
        source_name: verify_source_capture(
            source_name=source_name,
            coverage=coverage,
            cursor_db_path=cursor_path,
            raw_db_path=raw_db_path,
        )
        for source_name in names
    }
    blocking_capture = sorted(
        name for name, evidence in source_capture.items() if not bool(evidence.get("ok"))
    )
    integrity_gaps = [
        name
        for name, path in (
            ("raw", Path(raw_db_path)),
            ("cursor", cursor_path),
        )
        if _integrity_sqlite(path) != "ok"
    ]
    identity_reconciliation_ok = _verify_session_identity_reconciliation_plan(
        raw_db_path,
        session_identity_reconciliation,
    )
    required_gap = (
        len(challenger.get("blocking_sources") or [])
        + len(blocking_capture)
        + len(integrity_gaps)
        + (0 if identity_reconciliation_ok else 1)
    )
    return {
        "schema_version": "mnemos.agent_source_raw_post_gap.v1",
        "active_source_count": len(names),
        "native_inventory_hash": current_inventory.inventory_hash,
        "challenger_blocking_sources": challenger.get("blocking_sources") or [],
        "capture_blocking_sources": blocking_capture,
        "integrity_gaps": integrity_gaps,
        "session_identity_reconciliation_ok": (identity_reconciliation_ok),
        "required_gap": required_gap,
        "ok": required_gap == 0,
    }


def _default_certification_dependencies() -> CertificationDependencies:
    return CertificationDependencies(
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


def _certification_dependencies() -> CertificationDependencies:
    active = _ACTIVE_CERTIFICATION_DEPENDENCIES.get()
    return active or _default_certification_dependencies()


@contextmanager
def certification_dependency_scope(
    dependencies: CertificationDependencies,
) -> Iterator[None]:
    """Bind one explicit dependency bundle without cross-call global mutation."""

    token = _ACTIVE_CERTIFICATION_DEPENDENCIES.set(dependencies)
    try:
        yield
    finally:
        _ACTIVE_CERTIFICATION_DEPENDENCIES.reset(token)


def _validated_completed_inner_receipt(
    *,
    outer_receipt: Mapping[str, Any],
    reviewed_plan: Mapping[str, Any],
    backup_dir: Path,
    expected_plan_hash: str,
) -> Mapping[str, Any]:
    filename = str(outer_receipt.get("inner_receipt_filename") or "")
    expected_sha256 = str(outer_receipt.get("inner_receipt_sha256") or "")
    expected_prepared_sha256 = str(outer_receipt.get("inner_prepared_receipt_sha256") or "")
    if (
        not filename
        or Path(filename).name != filename
        or not filename.startswith("agent-source-raw-reconciliation-")
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            expected_prepared_sha256,
        )
    ):
        raise AgentSourceRawReconciliationError("migration_receipt_binding_mismatch")
    path = backup_dir / filename
    if not _private_backup_file_ok(path, backup_dir):
        raise AgentSourceRawReconciliationError("migration_receipt_permissions_invalid")
    try:
        inner = json.loads(
            _read_private_backup_bytes(path, backup_dir).decode("utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AgentSourceRawReconciliationError("migration_receipt_unreadable") from None
    if not isinstance(inner, Mapping):
        raise AgentSourceRawReconciliationError(
            "migration_receipt_binding_mismatch"
        )
    source_capture = inner.get("source_capture")
    projection_plan = reviewed_plan.get("current_projection_reconciliation")
    expected_projection = (
        {
            "schema_version": ("mnemos.raw_current_projection_reconciliation.v1"),
            "repaired_count": int(projection_plan.get("invalid_count") or 0),
            "restore_revision_count": int(projection_plan.get("restore_revision_count") or 0),
            "append_revision_count": int(projection_plan.get("append_revision_count") or 0),
            "invalid_after_count": 0,
        }
        if isinstance(projection_plan, Mapping)
        else None
    )
    cycles = inner.get("cycles")
    worker_count = inner.get("raw_generation_worker_count")
    planning_limits = reviewed_plan.get("planning_limits")
    worker_evidence_ok = bool(
        isinstance(worker_count, int)
        and not isinstance(worker_count, bool)
        and isinstance(cycles, list)
        and worker_count == len(cycles)
        and isinstance(planning_limits, Mapping)
        and worker_count >= int(planning_limits.get("minimum_generations") or 0)
        and worker_count <= int(planning_limits.get("generation_budget") or 0)
        and inner.get("process_write_scope_verified") is True
        and inner.get("raw_generation_worker_scope_verified") is True
    )
    if worker_evidence_ok:
        expected_worker_isolation = raw_generation_worker_isolation_contract()
        validated_cycles = cycles if isinstance(cycles, list) else []
        for generation, cycle in enumerate(validated_cycles, start=1):
            isolation = cycle.get("worker_isolation") if isinstance(cycle, Mapping) else None
            if (
                not isinstance(cycle, Mapping)
                or cycle.get("generation") != generation
                or not isinstance(isolation, Mapping)
                or dict(isolation) != expected_worker_isolation
            ):
                worker_evidence_ok = False
                break
    if (
        _certification_dependencies().file_sha256(path) != expected_sha256
        or inner.get("schema_version") != SCHEMA_VERSION
        or inner.get("status") != "completed"
        or inner.get("ok") is not True
        or inner.get("reviewed_plan_hash") != expected_plan_hash
        or inner.get("prepared_receipt_sha256") != expected_prepared_sha256
        or inner.get("support_manifest_hash") != reviewed_plan.get("support_manifest_hash")
        or inner.get("active_sources") != reviewed_plan.get("active_sources")
        or inner.get("backups") != outer_receipt.get("backups")
        or inner.get("before_state") != outer_receipt.get("before_state")
        or inner.get("coverage_state_reset") is not bool(reviewed_plan.get("reset_derived_state"))
        or inner.get("after_challenger", {}).get("ok") is not True
        or inner.get("raw_only_boundary_ok") is not True
        or not worker_evidence_ok
        or int(inner.get("unexpected_mutation_count") or 0) != 0
        or not isinstance(source_capture, Mapping)
        or not source_capture
        or set(source_capture) != set(reviewed_plan.get("active_sources") or [])
        or not all(
            bool(item.get("ok")) for item in source_capture.values() if isinstance(item, Mapping)
        )
        or len(source_capture)
        != len([item for item in source_capture.values() if isinstance(item, Mapping)])
        or inner.get("session_identity_reconciliation", {}).get("ok") is not True
        or expected_projection is None
        or inner.get("current_projection_reconciliation") != expected_projection
        or outer_receipt.get("current_projection_reconciliation") != expected_projection
    ):
        raise AgentSourceRawReconciliationError("migration_receipt_binding_mismatch")
    return dict(inner)


def _verify_completed_raw_receipt(
    *,
    config: Any,
    raw_db_path: Path,
    backup_dir: Path,
    sources: Iterable[Any],
    expected_plan_hash: str,
) -> dict[str, Any] | None:
    source_list = list(sources)
    receipt_path = _migration_receipt_path(backup_dir, expected_plan_hash)
    try:
        receipt_kind = inspect_path_kind(receipt_path)
    except DurableIOError:
        raise AgentSourceRawReconciliationError("migration_receipt_unreadable") from None
    if receipt_kind == "missing":
        return None
    if receipt_kind != "file":
        raise AgentSourceRawReconciliationError("migration_receipt_permissions_invalid")
    if not _private_backup_file_ok(receipt_path, backup_dir):
        raise AgentSourceRawReconciliationError("migration_receipt_permissions_invalid")
    database_dir = Path(config.database_dir)
    cursor_path = database_dir / "agent_sync_cursors.db"
    coverage_path = agent_source_coverage.coverage_state_path(database_dir)
    _require_quiescent_sqlite_main_file(raw_db_path)
    _require_quiescent_sqlite_main_file(cursor_path)
    physical_before = _migration_physical_signature(
        config=config,
        raw_db_path=raw_db_path,
        backup_dir=backup_dir,
        receipt_path=receipt_path,
    )
    try:
        receipt = json.loads(
            _read_private_backup_bytes(receipt_path, backup_dir).decode("utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AgentSourceRawReconciliationError("migration_receipt_unreadable") from None
    if (
        receipt.get("schema_version") != "mnemos.agent_source_raw_migration_receipt.v1"
        or receipt.get("status") != "completed"
        or receipt.get("plan_hash") != expected_plan_hash
        or receipt.get("raw_db") != str(Path(raw_db_path).resolve())
        or receipt.get("backup_dir") != str(backup_dir.resolve())
        or receipt.get("restore_drill_ok") is not True
        or receipt.get("required_gap") != 0
        or receipt.get("first_apply_comparator", {}).get("ok") is not True
        or receipt.get("post_apply_gap", {}).get("required_gap") != 0
        or not isinstance(receipt.get("require_all_active_sources"), bool)
        or not isinstance(receipt.get("backups"), Mapping)
        or not isinstance(receipt.get("before_state"), Mapping)
        or not isinstance(
            receipt.get("current_projection_reconciliation"),
            Mapping,
        )
    ):
        raise AgentSourceRawReconciliationError("migration_receipt_binding_mismatch")
    reviewed_plan = _validated_receipt_plan(
        receipt=receipt,
        raw_db_path=raw_db_path,
        backup_dir=backup_dir,
        expected_plan_hash=expected_plan_hash,
    )
    _validate_prior_terminal_receipts(
        receipt=receipt,
        backup_dir=backup_dir,
        expected_plan_hash=expected_plan_hash,
    )
    _validated_completed_inner_receipt(
        outer_receipt=receipt,
        reviewed_plan=reviewed_plan,
        backup_dir=backup_dir,
        expected_plan_hash=expected_plan_hash,
    )
    snapshot_contract = reviewed_plan.get("native_artifact_snapshot")
    if (
        not isinstance(snapshot_contract, Mapping)
        or snapshot_contract.get("execution_dependency_hashes")
        != _certification_dependencies().recovery_execution_dependency_hashes()
        or snapshot_contract.get("runtime_execution_identity")
        != _certification_dependencies().runtime_execution_identity()
        or reviewed_plan.get("support_manifest_hash")
        != get_agent_source_support_manifest().manifest_hash
    ):
        raise AgentSourceRawReconciliationError("migration_receipt_code_drift")
    try:
        current_inventory = build_native_artifact_inventory(source_list).inventory_hash
    except NativeArtifactInventoryError as exc:
        raise reconciliation_error_from_typed_failure(exc) from None
    if current_inventory != receipt.get("native_inventory_hash"):
        raise AgentSourceRawReconciliationError("same_plan_native_snapshot_drift")
    if not _backup_preimages_are_current(
        before=receipt["before_state"],
        backups=receipt["backups"],
        backup_dir=backup_dir,
    ):
        raise AgentSourceRawReconciliationError("migration_receipt_backup_invalid")
    raw_backup_record = receipt["backups"].get("raw")
    if not isinstance(raw_backup_record, Mapping) or raw_backup_record.get("present") is not True:
        raise AgentSourceRawReconciliationError("migration_receipt_backup_invalid")
    raw_backup_path = backup_dir / str(raw_backup_record.get("filename") or "")
    with tempfile.TemporaryDirectory(prefix="mnemos-raw-second-apply-") as temp_name:
        verification_dir = Path(temp_name)
        verification_raw = verification_dir / "raw_events.db"
        verification_cursor = verification_dir / "agent_sync_cursors.db"
        verification_coverage = agent_source_coverage.coverage_state_path(verification_dir)
        verification_backup = verification_dir / "pre_raw_events.db"
        _copy_verification_file(raw_db_path, verification_raw, sqlite_file=True)
        _copy_verification_file(cursor_path, verification_cursor, sqlite_file=True)
        _copy_verification_file(coverage_path, verification_coverage)
        _copy_verification_file(raw_backup_path, verification_backup, sqlite_file=True)
        verification_config = SimpleNamespace(database_dir=verification_dir)
        verification_state = _target_state(
            verification_config,
            verification_raw,
        )
        expected_post_state = receipt.get("post_state")
        if not isinstance(expected_post_state, Mapping) or _state_without_paths(
            verification_state
        ) != _state_without_paths(expected_post_state):
            raise AgentSourceRawReconciliationError("same_plan_post_state_drift")
        backup_conservation = _conservation_summary(_safe_raw_conservation(verification_backup))
        if backup_conservation != receipt["first_apply_comparator"].get("before"):
            raise AgentSourceRawReconciliationError("migration_receipt_comparator_drift")
        current_conservation = _conservation_summary(_safe_raw_conservation(verification_raw))
        if current_conservation != receipt["first_apply_comparator"].get("after"):
            raise AgentSourceRawReconciliationError("migration_receipt_comparator_drift")
        post_gap = _certification_dependencies().post_apply_raw_gap(
            config=verification_config,
            raw_db_path=verification_raw,
            sources=source_list,
            expected_inventory_hash=current_inventory,
            require_all_active_sources=bool(receipt.get("require_all_active_sources")),
            session_identity_reconciliation=reviewed_plan["session_identity_reconciliation"],
        )
        if post_gap != receipt.get("post_apply_gap"):
            raise AgentSourceRawReconciliationError("migration_receipt_post_gap_drift")
    physical_after = _migration_physical_signature(
        config=config,
        raw_db_path=raw_db_path,
        backup_dir=backup_dir,
        receipt_path=receipt_path,
    )
    if physical_after != physical_before:
        raise AgentSourceRawReconciliationError("migration_second_apply_physical_drift")
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "same_plan_second_apply",
        "ok": True,
        "reviewed_plan_hash": expected_plan_hash,
        "physical_delta": 0,
        "physical_pre_signature": physical_before,
        "physical_post_signature": physical_after,
        "semantic_delta": 0,
        "required_gap": 0,
        "receipt_filename": receipt_path.name,
    }
