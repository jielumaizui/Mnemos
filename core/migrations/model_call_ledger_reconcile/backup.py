"""Verified local SQLite backup mechanics for ledger reconciliation."""

from __future__ import annotations

import os
import sqlite3
import stat as stat_module
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from core.telemetry.model_call_ledger import ModelCallLedgerInvariantError
from core.telemetry.model_call_ledger.migration import LedgerReconciliation

from .contracts import ModelCallLedgerReconcileError
from .inventory import _connect_read_only, _require_regular_sqlite_file, _source_generation


def ensure_private_backup_directory(backup_dir: Path) -> Path:
    """Create a 0700 backup root without traversing a missing symlink parent."""
    requested = Path(backup_dir).expanduser()
    missing: list[Path] = []
    cursor = requested
    while True:
        try:
            metadata = cursor.lstat()
        except FileNotFoundError:
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise ModelCallLedgerReconcileError("backup_directory_parent_missing")
            cursor = parent
            continue
        except OSError as exc:
            raise ModelCallLedgerReconcileError("backup_directory_uninspectable") from exc
        if stat_module.S_ISLNK(metadata.st_mode) or not stat_module.S_ISDIR(metadata.st_mode):
            raise ModelCallLedgerReconcileError("backup_directory_must_be_a_non_symlink_directory")
        break
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        created = directory.lstat()
        if stat_module.S_ISLNK(created.st_mode) or not stat_module.S_ISDIR(created.st_mode):
            raise ModelCallLedgerReconcileError("backup_directory_must_be_a_non_symlink_directory")
    final = requested.resolve(strict=True)
    final_metadata = final.lstat()
    if stat_module.S_ISLNK(final_metadata.st_mode) or not stat_module.S_ISDIR(final_metadata.st_mode):
        raise ModelCallLedgerReconcileError("backup_directory_must_be_a_non_symlink_directory")
    os.chmod(final, 0o700)
    verified = final.lstat()
    if verified.st_mode & 0o777 != 0o700:
        raise ModelCallLedgerReconcileError("backup_directory_permissions_invalid")
    return final


def create_sqlite_backups(
    paths: Iterable[Path],
    backup_dir: Path,
    *,
    prepared_canonical_backup: object | None = None,
    return_canonical_backup_receipt: bool = False,
    private_backup_identities: dict[Path, str] | None = None,
) -> list[dict[str, str]] | tuple[list[dict[str, str]], object | None]:
    # SQLite creates the destination database as soon as ``connect`` opens the
    # path.  Reserve a private directory and file *before* that happens: a
    # post-write chmod would leave a window where a raw retired-store backup is
    # readable by the group or other local users.
    try:
        backup_dir = ensure_private_backup_directory(backup_dir)
    except ModelCallLedgerReconcileError:
        raise
    except OSError as exc:
        raise ModelCallLedgerReconcileError("sqlite_backup_io_error") from exc
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backups: list[dict[str, str]] = []
    canonical_backup_receipt: object | None = None
    source_candidates: set[Path] = set()
    for candidate in paths:
        path = Path(candidate).expanduser()
        if _require_regular_sqlite_file(path, allow_missing=True):
            source_candidates.add(path.resolve())
    for source in sorted(source_candidates):
        source_generation_before = _source_generation(source)
        target = backup_dir / f"{source.stem}.pre-model-call-ledger.{stamp}.db"
        target_created = False
        try:
            # O_EXCL rejects collisions (including a pre-existing symlink),
            # and fchmod happens while this descriptor is still private.
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            target_created = True
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            proof_source = (
                LedgerReconciliation.proof_source(
                    prepared_canonical_backup
                )
                if prepared_canonical_backup is not None
                else None
            )
            if proof_source is not None and Path(proof_source).expanduser().resolve() == source:
                canonical_backup_receipt = LedgerReconciliation.write_verified_backup(
                    prepared_canonical_backup,
                    target,
                )
                integrity = "ok"
            else:
                src = _connect_read_only(source)
                dst = sqlite3.connect(str(target))
                try:
                    src.backup(dst)
                    integrity = str(dst.execute("PRAGMA integrity_check").fetchone()[0])
                finally:
                    dst.close()
                    src.close()
            if integrity != "ok":
                raise ModelCallLedgerReconcileError("backup_integrity_check_failed")
            if _source_generation(source) != source_generation_before:
                raise ModelCallLedgerReconcileError("source_drift_during_backup")
            backup_identity = LedgerReconciliation.backup_identity(target)
        except (ModelCallLedgerReconcileError, ModelCallLedgerInvariantError):
            if target_created:
                remove_incomplete_backup_target(target)
            raise
        except (OSError, sqlite3.Error) as exc:
            if target_created:
                remove_incomplete_backup_target(target)
            if isinstance(exc, sqlite3.Error):
                raise ModelCallLedgerReconcileError("sqlite_backup_sqlite_error") from exc
            raise ModelCallLedgerReconcileError("sqlite_backup_io_error") from exc
        backups.append(
            {
                "source": str(source),
                "source_generation": source_generation_before,
                "path": str(target),
                "backup_generation": _source_generation(target),
                "integrity_check": integrity,
            }
        )
        if private_backup_identities is not None:
            private_backup_identities[target.resolve()] = backup_identity
    if return_canonical_backup_receipt:
        return backups, canonical_backup_receipt
    return backups


def remove_incomplete_backup_target(target: Path) -> None:
    """Remove a failed private backup and any SQLite sidecars it left behind."""
    for candidate in (
        target,
        Path(str(target) + "-journal"),
        Path(str(target) + "-wal"),
        Path(str(target) + "-shm"),
    ):
        try:
            # trusted-scan: backup owner=model_call_ledger target=private_reconcile_backup expires=never
            candidate.unlink(missing_ok=True)
        except OSError:
            # The directory was made private before any target existed, so a
            # cleanup race cannot expose raw data.  Preserve the original
            # backup failure rather than replacing it with unlink noise.
            continue
