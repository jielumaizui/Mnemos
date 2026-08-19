"""Stable physical-scope inventory implementation for durable I/O."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import hashlib
import os
from pathlib import Path
import stat

from core.ops.durable_io_types import DurableIOError

PhysicalEntryReader = Callable[..., dict[str, object]]
SecureDirectoryOpener = Callable[..., int]
PathKindReader = Callable[[Path], str]


def physical_entry(path: Path, *, hash_max_bytes: int) -> dict[str, object]:
    """Capture one no-follow filesystem entry from a stable generation."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"path": str(path), "present": False}
    if path.is_symlink():
        return {
            "path": str(path),
            "present": True,
            "kind": "symlink",
            "mode": stat.S_IMODE(metadata.st_mode),
        }
    kind = (
        "file"
        if stat.S_ISREG(metadata.st_mode)
        else ("directory" if stat.S_ISDIR(metadata.st_mode) else "other")
    )
    result: dict[str, object] = {
        "path": str(path),
        "present": True,
        "kind": kind,
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "size": int(metadata.st_size),
        "mode": stat.S_IMODE(metadata.st_mode),
    }
    if kind == "file":
        # Always anchor a regular-file generation, including files above the
        # hashing budget. A preceding ``lstat`` alone is not a stable
        # generation receipt: the leaf could be replaced before its metadata
        # is recorded. Bounded files additionally receive a complete digest;
        # large files retain timestamp evidence after the same no-follow
        # descriptor/path revalidation.
        descriptor = -1
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or int(opened.st_dev) != int(metadata.st_dev)
                or int(opened.st_ino) != int(metadata.st_ino)
                or int(opened.st_mode) != int(metadata.st_mode)
                or int(opened.st_size) != int(metadata.st_size)
                or int(opened.st_mtime_ns) != int(metadata.st_mtime_ns)
                or int(opened.st_ctime_ns) != int(metadata.st_ctime_ns)
            ):
                raise DurableIOError("durable_signature_file_changed")
            digest = None
            if metadata.st_size <= hash_max_bytes:
                # SQLite read locks can cycle ctime on a small WAL ``-shm``
                # file while leaving every durable byte unchanged.
                digest = hashlib.sha256()
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            after = os.fstat(descriptor)
            current = path.lstat()
            if (
                not stat.S_ISREG(after.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or int(after.st_dev) != int(metadata.st_dev)
                or int(after.st_ino) != int(metadata.st_ino)
                or int(after.st_size) != int(metadata.st_size)
                or int(after.st_mode) != int(metadata.st_mode)
                or int(after.st_mtime_ns) != int(metadata.st_mtime_ns)
                or int(after.st_ctime_ns) != int(metadata.st_ctime_ns)
                or int(current.st_dev) != int(metadata.st_dev)
                or int(current.st_ino) != int(metadata.st_ino)
                or int(current.st_mode) != int(metadata.st_mode)
                or int(current.st_size) != int(metadata.st_size)
                or int(current.st_mtime_ns) != int(metadata.st_mtime_ns)
                or int(current.st_ctime_ns) != int(metadata.st_ctime_ns)
            ):
                raise DurableIOError("durable_signature_file_changed")
            if digest is not None:
                result["sha256"] = digest.hexdigest()
            else:
                result["ctime_ns"] = int(after.st_ctime_ns)
                result["mtime_ns"] = int(after.st_mtime_ns)
        except DurableIOError:
            raise
        except OSError:
            raise DurableIOError("durable_signature_file_unavailable") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    else:
        result["ctime_ns"] = int(metadata.st_ctime_ns)
        result["mtime_ns"] = int(metadata.st_mtime_ns)
    return result


def physical_scope_signature(
    paths: Iterable[Path],
    *,
    inventory_directory: Path | None,
    hash_max_bytes: int,
    entry_reader: PhysicalEntryReader,
    open_secure_directory: SecureDirectoryOpener,
    inspect_path_kind: PathKindReader,
) -> dict[str, object]:
    """Capture one stable scope using dependencies owned by the public façade."""

    selected = {Path(path).absolute() for path in paths}
    inventory_descriptor = -1
    inventory_identity: tuple[int, ...] | None = None
    inventory_names: tuple[str, ...] = ()
    directory = (
        Path(inventory_directory).absolute()
        if inventory_directory is not None
        else None
    )
    if directory is not None:
        selected.add(directory)
        try:
            inventory_descriptor = open_secure_directory(
                directory,
                create=False,
            )
            directory_metadata = os.fstat(inventory_descriptor)
            current_directory = directory.lstat()
            inventory_identity = (
                int(directory_metadata.st_dev),
                int(directory_metadata.st_ino),
                int(directory_metadata.st_mode),
                int(directory_metadata.st_size),
                int(directory_metadata.st_mtime_ns),
                int(directory_metadata.st_ctime_ns),
            )
            current_identity = (
                int(current_directory.st_dev),
                int(current_directory.st_ino),
                int(current_directory.st_mode),
                int(current_directory.st_size),
                int(current_directory.st_mtime_ns),
                int(current_directory.st_ctime_ns),
            )
            if (
                not stat.S_ISDIR(directory_metadata.st_mode)
                or inventory_identity != current_identity
            ):
                raise DurableIOError("durable_inventory_directory_changed")
            inventory_names = tuple(
                sorted(entry.name for entry in os.scandir(inventory_descriptor))
            )
            selected.update(directory / name for name in inventory_names)
        except DurableIOError:
            if inventory_descriptor >= 0:
                os.close(inventory_descriptor)
                inventory_descriptor = -1
            kind = inspect_path_kind(directory)
            if kind != "missing":
                raise
        except OSError:
            if inventory_descriptor >= 0:
                os.close(inventory_descriptor)
                inventory_descriptor = -1
            raise DurableIOError("durable_inventory_directory_unavailable") from None
    try:
        entries = [
            entry_reader(path, hash_max_bytes=hash_max_bytes)
            for path in sorted(selected)
        ]
        if inventory_descriptor >= 0 and inventory_identity is not None:
            if directory is None:
                raise DurableIOError("durable_inventory_directory_changed")
            after = os.fstat(inventory_descriptor)
            current = directory.lstat()
            after_identity = (
                int(after.st_dev),
                int(after.st_ino),
                int(after.st_mode),
                int(after.st_size),
                int(after.st_mtime_ns),
                int(after.st_ctime_ns),
            )
            current_identity = (
                int(current.st_dev),
                int(current.st_ino),
                int(current.st_mode),
                int(current.st_size),
                int(current.st_mtime_ns),
                int(current.st_ctime_ns),
            )
            after_names = tuple(
                sorted(entry.name for entry in os.scandir(inventory_descriptor))
            )
            if (
                inventory_identity != after_identity
                or after_identity != current_identity
                or inventory_names != after_names
            ):
                raise DurableIOError("durable_inventory_directory_changed")
            verified_entries = [
                entry_reader(path, hash_max_bytes=hash_max_bytes)
                for path in sorted(selected)
            ]
            final = os.fstat(inventory_descriptor)
            final_names = tuple(
                sorted(entry.name for entry in os.scandir(inventory_descriptor))
            )
            final_identity = (
                int(final.st_dev),
                int(final.st_ino),
                int(final.st_mode),
                int(final.st_size),
                int(final.st_mtime_ns),
                int(final.st_ctime_ns),
            )
            if (
                verified_entries != entries
                or final_identity != after_identity
                or final_names != after_names
            ):
                raise DurableIOError("durable_inventory_directory_changed")
            entries = verified_entries
    except DurableIOError:
        raise
    except OSError:
        raise DurableIOError("durable_inventory_directory_unavailable") from None
    finally:
        if inventory_descriptor >= 0:
            os.close(inventory_descriptor)
    return {
        "schema_version": "mnemos.physical_scope_signature.v3",
        "comparison_contract": "stable-nofollow-content-and-inventory-v3",
        "entries": entries,
    }


__all__ = ["physical_entry", "physical_scope_signature"]
