"""Verified backup and rollback mechanics for cognitive reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Iterable

from core.cognitive.models import Dimension


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def ensure_private_backup_dir(path: Path) -> Path:
    """Create a non-symlink 0700 directory for private cognitive backups."""

    requested = path.expanduser()
    requested.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = requested.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("backup directory must be a non-symlink directory")
    os.chmod(requested, 0o700)
    if requested.lstat().st_mode & 0o777 != 0o700:
        raise ValueError("backup directory permissions are not private")
    return requested.resolve(strict=True)


def _reserve_private_file(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def backup_sqlite_databases(
    sources: Iterable[Path],
    backup_dir: Path,
    *,
    label: str = "calibration-reconcile",
) -> list[dict[str, Any]]:
    if not label or any(value not in "abcdefghijklmnopqrstuvwxyz0123456789-" for value in label):
        raise ValueError("backup label must be lowercase ASCII slug")
    root = ensure_private_backup_dir(backup_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    receipts: list[dict[str, Any]] = []
    for source in sorted({Path(value).resolve() for value in sources}):
        if not source.is_file():
            receipts.append(
                {
                    "kind": "sqlite",
                    "source": str(source),
                    "existed": False,
                    "path": "",
                }
            )
            continue
        target = root / f"{source.stem}.pre-{label}.{stamp}.db"
        _reserve_private_file(target)
        try:
            with sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True) as src:
                with sqlite3.connect(target) as dst:
                    src.backup(dst)
                    integrity = str(dst.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError("SQLite backup integrity check failed")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        receipts.append(
            {
                "kind": "sqlite",
                "source": str(source),
                "existed": True,
                "path": str(target),
                "sha256": _sha256_file(target),
                "integrity_check": integrity,
            }
        )
    return receipts


def backup_projection_files(
    projection_dir: Path,
    backup_dir: Path,
) -> list[dict[str, Any]]:
    root = ensure_private_backup_dir(backup_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    receipts: list[dict[str, Any]] = []
    for dimension in sorted(value.value for value in Dimension):
        source = projection_dir / f"{dimension}.md"
        if not source.is_file():
            receipts.append(
                {
                    "kind": "projection",
                    "source": str(source),
                    "existed": False,
                    "path": "",
                }
            )
            continue
        target = root / f"{dimension}.pre-calibration-reconcile.{stamp}.md"
        _reserve_private_file(target)
        try:
            with source.open("rb") as src, target.open("wb") as dst:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    dst.write(chunk)
            os.chmod(target, 0o600)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        receipts.append(
            {
                "kind": "projection",
                "source": str(source),
                "existed": True,
                "path": str(target),
                "sha256": _sha256_file(target),
            }
        )
    return receipts


def restore_backups(receipts: Iterable[dict[str, Any]]) -> None:
    """Restore exact preimages after a failed multi-store apply attempt."""

    for receipt in receipts:
        source = Path(str(receipt["source"]))
        existed = bool(receipt.get("existed"))
        if not existed:
            source.unlink(missing_ok=True)
            continue
        backup = Path(str(receipt["path"]))
        if _sha256_file(backup) != receipt.get("sha256"):
            raise RuntimeError("backup changed before rollback")
        source.parent.mkdir(parents=True, exist_ok=True)
        if receipt["kind"] == "sqlite":
            with sqlite3.connect(f"{backup.resolve().as_uri()}?mode=ro", uri=True) as src:
                with sqlite3.connect(source) as dst:
                    src.backup(dst)
                    integrity = str(dst.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError("restored SQLite integrity check failed")
        else:
            temporary = source.with_name(source.name + ".cognitive-rollback.tmp")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb") as dst, backup.open("rb") as src:
                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                        dst.write(chunk)
                temporary.replace(source)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise


__all__ = [
    "backup_projection_files",
    "backup_sqlite_databases",
    "ensure_private_backup_dir",
    "restore_backups",
]
