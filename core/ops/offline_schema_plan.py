"""Exact, read-only plan material for offline schema reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping
import uuid

from core.ops.durable_io import (
    DurableIOError,
    ensure_private_directory,
    fsync_directory,
    fsync_regular_file,
    inspect_path_kind,
    normalize_private_sqlite_copy,
    owned_sqlite_connection_pair,
    physical_scope_signature,
    private_sqlite_sidecars,
    regular_file_sha256,
    secure_publish_immutable_bytes,
    secure_remove_regular_file,
    validate_private_sqlite_copy,
)
from core.ops.readiness_query_budget import connect_readonly_sqlite


SCHEMA_VERSION = "mnemos.offline_schema_plan.v1"


class OfflineSchemaPlanError(RuntimeError):
    """An offline schema plan, backup, or restore could not be proven safe."""


def _sha256_file(path: Path) -> str:
    try:
        return regular_file_sha256(Path(path))
    except DurableIOError:
        raise DurableIOError("offline_schema_source_signature_invalid") from None


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def build_offline_schema_plan(
    *,
    migration_id: str,
    db_path: Path,
    backup_dir: Path | None,
    before: Mapping[str, Any],
    source_paths: Iterable[Path],
    writers_inactive: bool,
) -> dict[str, Any]:
    """Bind one schema action to exact code, physical state, and backup scope."""

    database = Path(db_path).expanduser().absolute()
    backup = (
        Path(backup_dir).expanduser().absolute()
        if backup_dir is not None
        else None
    )
    sidecars = tuple(
        Path(f"{database}{suffix}")
        for suffix in ("", "-journal", "-shm", "-wal")
    )
    sources = {
        str(Path(path).absolute()): _sha256_file(Path(path))
        for path in sorted({Path(path).absolute() for path in source_paths})
    }
    status = str(before.get("status") or "")
    material = {
        "schema_version": SCHEMA_VERSION,
        "migration_id": str(migration_id),
        "db_path": str(database),
        "backup_dir": str(backup) if backup is not None else "",
        "before": dict(before),
        "physical_preimage": physical_scope_signature(
            sidecars,
            hash_max_bytes=(1 << 63) - 1,
        ),
        "source_sha256": sources,
        "writer_lock_state": (
            "writers_inactive"
            if writers_inactive
            else "active_or_unverified"
        ),
        "apply_required": status != "current",
        "apply_eligible": bool(
            writers_inactive
            and backup is not None
            and status in {"current", "migration_required", "uninitialized"}
        ),
    }
    return {
        **material,
        "plan_hash": _canonical_hash(material),
    }


def _remove_sqlite_scope(path: Path) -> None:
    for candidate in (*private_sqlite_sidecars(path), Path(path)):
        try:
            kind = inspect_path_kind(candidate)
        except DurableIOError:
            raise OfflineSchemaPlanError("offline_schema_cleanup_unavailable") from None
        if kind == "missing":
            continue
        if kind != "file":
            raise OfflineSchemaPlanError("offline_schema_cleanup_target_unsafe")
        try:
            secure_remove_regular_file(candidate.parent, candidate.name)
        except DurableIOError:
            raise OfflineSchemaPlanError("offline_schema_cleanup_failed") from None


def backup_sqlite_database(
    db_path: Path,
    backup_dir: Path,
    *,
    label: str,
) -> dict[str, Any]:
    """Create one private, standalone, collision-safe SQLite backup."""

    source = Path(db_path).expanduser().absolute()
    try:
        source_signature = physical_scope_signature((source,))
        source_entries = source_signature.get("entries")
        if (
            not isinstance(source_entries, list)
            or len(source_entries) != 1
            or not isinstance(source_entries[0], dict)
        ):
            raise OfflineSchemaPlanError(
                "offline_schema_backup_source_unavailable"
            )
        source_entry = source_entries[0]
    except DurableIOError:
        raise OfflineSchemaPlanError("offline_schema_backup_source_unavailable") from None
    if source_entry.get("present") is not True:
        return {
            "present": False,
            "path": "",
            "sha256": "",
            "integrity": "not_applicable",
        }
    if source_entry.get("kind") != "file":
        raise OfflineSchemaPlanError("offline_schema_backup_source_not_regular")
    try:
        private_dir = ensure_private_directory(backup_dir)
    except DurableIOError:
        raise OfflineSchemaPlanError("offline_schema_backup_directory_unsafe") from None
    target = private_dir / f"{label}.{uuid.uuid4().hex}.sqlite"
    created = False
    completed = False
    try:
        receipt = secure_publish_immutable_bytes(
            private_dir,
            target.name,
            b"",
            return_receipt=True,
        )
        if receipt.created is not True:
            raise OfflineSchemaPlanError("offline_schema_backup_collision")
        created = True
        with owned_sqlite_connection_pair(
            lambda: connect_readonly_sqlite(source),
            lambda: sqlite3.connect(str(target)),
        ) as (source_connection, destination):
            source_connection.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise OfflineSchemaPlanError("offline_schema_backup_integrity_failed")
            if destination.execute("PRAGMA foreign_key_check").fetchall():
                raise OfflineSchemaPlanError("offline_schema_backup_foreign_key_failed")
        normalize_private_sqlite_copy(target)
        validate_private_sqlite_copy(target)
        fsync_regular_file(target)
        fsync_directory(private_dir)
        result = {
            "present": True,
            "path": str(target),
            "filename": target.name,
            "sha256": f"sha256:{_sha256_file(target)}",
            "integrity": "ok",
            "foreign_key_errors": [],
        }
        completed = True
        return result
    except OfflineSchemaPlanError:
        raise
    except (DurableIOError, OSError, sqlite3.Error):
        raise OfflineSchemaPlanError("offline_schema_backup_failed") from None
    finally:
        if created and not completed:
            try:
                _remove_sqlite_scope(target)
            except OfflineSchemaPlanError:
                raise OfflineSchemaPlanError(
                    "offline_schema_failed_backup_cleanup_failed"
                ) from None


def restore_sqlite_database(
    backup_path: Path | None,
    db_path: Path,
    *,
    expected_target_identity: Mapping[str, object],
) -> None:
    """Restore an exact backup without overwriting a replacement generation."""

    target = Path(db_path).expanduser().absolute()
    expected_identity = {
        key: expected_target_identity.get(key)
        for key in ("device", "inode")
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in expected_identity.values()
    ):
        raise OfflineSchemaPlanError("offline_schema_restore_identity_invalid")

    def require_expected_target() -> None:
        try:
            signature = physical_scope_signature(
                (target,),
                hash_max_bytes=0,
            )
            entries = signature.get("entries")
            entry = entries[0] if isinstance(entries, list) and len(entries) == 1 else None
        except (DurableIOError, OSError, TypeError):
            raise OfflineSchemaPlanError(
                "offline_schema_restore_target_unavailable"
            ) from None
        if (
            not isinstance(entry, dict)
            or entry.get("present") is not True
            or entry.get("kind") != "file"
            or any(entry.get(key) != value for key, value in expected_identity.items())
        ):
            raise OfflineSchemaPlanError("offline_schema_restore_target_changed")

    require_expected_target()
    if backup_path is None:
        try:
            secure_remove_regular_file(
                target.parent,
                target.name,
                expected_preimage=expected_identity,
            )
        except DurableIOError:
            raise OfflineSchemaPlanError(
                "offline_schema_restore_target_changed"
            ) from None
        for sidecar in private_sqlite_sidecars(target):
            try:
                secure_remove_regular_file(
                    sidecar.parent,
                    sidecar.name,
                    missing_ok=True,
                )
            except DurableIOError:
                raise OfflineSchemaPlanError(
                    "offline_schema_restore_sidecar_cleanup_failed"
                ) from None
        fsync_directory(target.parent)
        return
    backup = Path(backup_path).expanduser().absolute()
    try:
        validate_private_sqlite_copy(backup)
        if backup_sqlite_integrity(backup) != "ok":
            raise OfflineSchemaPlanError("offline_schema_restore_backup_invalid")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.restore")
        receipt = secure_publish_immutable_bytes(
            target.parent,
            temporary.name,
            b"",
            return_receipt=True,
        )
        if receipt.created is not True:
            raise OfflineSchemaPlanError("offline_schema_restore_collision")
        try:
            with owned_sqlite_connection_pair(
                lambda: connect_readonly_sqlite(backup, immutable=True),
                lambda: sqlite3.connect(str(temporary)),
            ) as (source, destination):
                source.backup(destination)
            normalize_private_sqlite_copy(temporary)
            if backup_sqlite_integrity(temporary) != "ok":
                raise OfflineSchemaPlanError("offline_schema_restore_invalid")
            require_expected_target()
            os.replace(temporary, target)
            for sidecar in private_sqlite_sidecars(target):
                try:
                    secure_remove_regular_file(
                        sidecar.parent,
                        sidecar.name,
                        missing_ok=True,
                    )
                except DurableIOError:
                    raise OfflineSchemaPlanError(
                        "offline_schema_restore_sidecar_cleanup_failed"
                    ) from None
            fsync_regular_file(target)
            fsync_directory(target.parent)
            if backup_sqlite_integrity(target, immutable=False) != "ok":
                raise OfflineSchemaPlanError("offline_schema_restore_invalid")
        finally:
            try:
                _remove_sqlite_scope(temporary)
            except OfflineSchemaPlanError:
                raise OfflineSchemaPlanError(
                    "offline_schema_restore_temporary_cleanup_failed"
                ) from None
    except OfflineSchemaPlanError:
        raise
    except (DurableIOError, OSError, sqlite3.Error):
        raise OfflineSchemaPlanError("offline_schema_restore_failed") from None


def backup_sqlite_integrity(path: Path, *, immutable: bool = True) -> str:
    try:
        with connect_readonly_sqlite(path, immutable=immutable) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                return "foreign_key_error"
    except (DurableIOError, OSError, sqlite3.Error):
        return "unreadable"
    return str(row[0]) if row else "unreadable"


__all__ = [
    "OfflineSchemaPlanError",
    "SCHEMA_VERSION",
    "backup_sqlite_database",
    "backup_sqlite_integrity",
    "build_offline_schema_plan",
    "restore_sqlite_database",
]
