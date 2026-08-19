"""Small fail-closed durability primitives for migration control files."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import (
    Callable,
    Iterable,
    Iterator,
    Literal,
    Mapping,
    TypeVar,
    overload,
)

from core.ops.durable_io_types import DurableIOError
from core.ops.durable_scope import (
    physical_entry as _physical_entry,
    physical_scope_signature as _physical_scope_signature,
)
from core.ops.native_file_io import (
    canonical_native_path,
    copy_native_file_to_descriptor,
    open_native_binary,
    open_native_text,
    read_native_bytes,
    read_native_bytes_with_metadata,
)


@dataclass(frozen=True)
class SecureImmutablePublishReceipt:
    """Exact ownership receipt for one immutable publication attempt."""

    path: Path
    created: bool
    preimage: dict[str, object]


_PRIVATE_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-shm", "-wal")
_PathKey = TypeVar("_PathKey", str, Path)


def _required_int(value: object, *, error: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DurableIOError(error)
    return value


def _inventory_entries(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise DurableIOError("durable_directory_inventory_invalid")
    return [dict(item) for item in value]


def _safe_relative_parts(relative_path: str | Path) -> tuple[str, ...]:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise DurableIOError("durable_relative_path_absolute")
    parts = tuple(candidate.parts)
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or any("\x00" in part for part in parts)
    ):
        raise DurableIOError("durable_relative_path_invalid")
    return parts


def _open_secure_directory_path(path: Path, *, create: bool) -> int:
    """Open every absolute directory component without following symlinks."""

    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        current_fd = os.open(os.sep, flags)
    except OSError:
        raise DurableIOError("durable_root_unavailable") from None
    try:
        for part in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError:
        os.close(current_fd)
        raise DurableIOError("durable_directory_path_unsafe") from None


@contextmanager
def _secure_parent_fd(
    root: Path,
    relative_path: str | Path,
    *,
    create: bool,
) -> Iterator[tuple[int, str]]:
    parts = _safe_relative_parts(relative_path)
    descriptors: list[int] = []
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptors.append(_open_secure_directory_path(Path(root), create=create))
        current_fd = descriptors[-1]
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, flags, dir_fd=current_fd)
            descriptors.append(next_fd)
            current_fd = next_fd
    except DurableIOError:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise
    except OSError:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise DurableIOError("durable_relative_path_unsafe") from None
    try:
        yield current_fd, parts[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise DurableIOError("durable_write_incomplete")
        offset += written


def _read_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _regular_file_preimage_from_descriptor(descriptor: int) -> dict[str, object]:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise DurableIOError("durable_target_not_regular")
    os.lseek(descriptor, 0, os.SEEK_SET)
    content = _read_all(descriptor)
    after = os.fstat(descriptor)
    before_identity = (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_mode),
        int(before.st_size),
        int(before.st_mtime_ns),
        int(before.st_ctime_ns),
    )
    after_identity = (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_mode),
        int(after.st_size),
        int(after.st_mtime_ns),
        int(after.st_ctime_ns),
    )
    if after_identity != before_identity:
        raise DurableIOError("durable_target_changed")
    return {
        "device": int(after.st_dev),
        "inode": int(after.st_ino),
        "mode": stat.S_IMODE(after.st_mode),
        "uid": int(after.st_uid),
        "gid": int(after.st_gid),
        "nlink": int(after.st_nlink),
        "size": int(after.st_size),
        "mtime_ns": int(after.st_mtime_ns),
        "ctime_ns": int(after.st_ctime_ns),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _preimage_matches(
    expected: Mapping[str, object],
    observed: Mapping[str, object],
) -> bool:
    return all(observed.get(key) == value for key, value in expected.items())


def _existing_regular_bytes(parent_fd: int, name: str) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise DurableIOError("durable_target_unsafe") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise DurableIOError("durable_target_not_regular")
        return _read_all(descriptor)
    finally:
        os.close(descriptor)


def secure_read_bytes(root: Path, relative_path: str | Path) -> bytes | None:
    """Read one regular file under a no-follow root, or return ``None`` if absent."""

    with _secure_parent_fd(root, relative_path, create=False) as (parent_fd, name):
        return _existing_regular_bytes(parent_fd, name)


def secure_regular_file_preimage(
    root: Path,
    relative_path: str | Path,
) -> dict[str, object] | None:
    """Bind one regular file's exact inode, metadata, and content digest."""

    with _secure_parent_fd(root, relative_path, create=False) as (parent_fd, name):
        return _regular_file_preimage_at(parent_fd, name)


def _regular_file_preimage_at(
    parent_fd: int,
    name: str,
) -> dict[str, object] | None:
    """Read an exact regular-file preimage relative to one trusted parent."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError:
        raise DurableIOError("durable_target_unsafe") from None
    try:
        preimage = _regular_file_preimage_from_descriptor(descriptor)
    finally:
        os.close(descriptor)
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        raise DurableIOError("durable_target_changed") from None
    if (
        not stat.S_ISREG(current.st_mode)
        or int(current.st_dev)
        != _required_int(preimage.get("device"), error="durable_target_preimage_invalid")
        or int(current.st_ino)
        != _required_int(preimage.get("inode"), error="durable_target_preimage_invalid")
    ):
        raise DurableIOError("durable_target_changed")
    return preimage


def secure_remove_regular_file(
    root: Path,
    relative_path: str | Path,
    *,
    missing_ok: bool = False,
    expected_preimage: dict[str, object] | None = None,
) -> bool:
    """Durably remove one exact regular file without following path components."""

    with _secure_parent_fd(root, relative_path, create=False) as (parent_fd, name):
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return False
            raise DurableIOError("durable_target_missing") from None
        except OSError:
            raise DurableIOError("durable_target_unsafe") from None
        if not stat.S_ISREG(before.st_mode):
            raise DurableIOError("durable_target_not_regular")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        except OSError:
            raise DurableIOError("durable_target_unsafe") from None
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or int(opened.st_dev) != int(before.st_dev)
                or int(opened.st_ino) != int(before.st_ino)
            ):
                raise DurableIOError("durable_target_changed")
            if expected_preimage is not None:
                observed_preimage = _regular_file_preimage_from_descriptor(descriptor)
                if not _preimage_matches(
                    expected_preimage,
                    observed_preimage,
                ):
                    raise DurableIOError("durable_target_preimage_changed")
        finally:
            os.close(descriptor)
        quarantine_name: str | None = None
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(current.st_mode)
                or int(current.st_dev) != int(opened.st_dev)
                or int(current.st_ino) != int(opened.st_ino)
            ):
                raise DurableIOError("durable_target_changed")
            quarantine_name = f".{name}.{uuid.uuid4().hex}.remove"
            os.rename(
                name,
                quarantine_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            quarantined = os.stat(
                quarantine_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(quarantined.st_mode)
                or int(quarantined.st_dev) != int(opened.st_dev)
                or int(quarantined.st_ino) != int(opened.st_ino)
            ):
                try:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    try:
                        os.rename(
                            quarantine_name,
                            name,
                            src_dir_fd=parent_fd,
                            dst_dir_fd=parent_fd,
                        )
                        quarantine_name = None
                    except OSError:
                        pass
                raise DurableIOError("durable_target_changed")
            os.unlink(quarantine_name, dir_fd=parent_fd)
            quarantine_name = None
            os.fsync(parent_fd)
        except DurableIOError:
            raise
        except OSError:
            if quarantine_name is not None:
                try:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    try:
                        os.rename(
                            quarantine_name,
                            name,
                            src_dir_fd=parent_fd,
                            dst_dir_fd=parent_fd,
                        )
                    except OSError:
                        pass
            raise DurableIOError("durable_target_remove_failed") from None
    return True


def secure_atomic_write_bytes(
    root: Path,
    relative_path: str | Path,
    content: bytes,
) -> Path:
    """Durably replace one regular file without following any path component."""

    with _secure_parent_fd(root, relative_path, create=True) as (parent_fd, name):
        existing = _existing_regular_bytes(parent_fd, name)
        del existing
        temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        except OSError:
            raise DurableIOError("durable_temporary_create_failed") from None
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        else:
            os.close(descriptor)
        try:
            os.replace(
                temporary_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        except OSError:
            raise DurableIOError("durable_atomic_replace_failed") from None
        finally:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return Path(root) / Path(relative_path)


def secure_atomic_write_text(
    root: Path,
    relative_path: str | Path,
    content: str,
    *,
    encoding: str = "utf-8",
) -> Path:
    return secure_atomic_write_bytes(root, relative_path, content.encode(encoding))


@overload
def secure_publish_immutable_bytes(
    root: Path,
    relative_path: str | Path,
    content: bytes,
    *,
    return_receipt: Literal[True],
) -> SecureImmutablePublishReceipt: ...


@overload
def secure_publish_immutable_bytes(
    root: Path,
    relative_path: str | Path,
    content: bytes,
    *,
    return_receipt: Literal[False] = False,
) -> Path: ...


def secure_publish_immutable_bytes(
    root: Path,
    relative_path: str | Path,
    content: bytes,
    *,
    return_receipt: bool = False,
) -> Path | SecureImmutablePublishReceipt:
    """Publish one immutable file, accepting only an exact idempotent replay."""

    with _secure_parent_fd(root, relative_path, create=True) as (parent_fd, name):
        temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        except OSError:
            raise DurableIOError("durable_temporary_create_failed") from None
        temporary_exists = True
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
            created = False
            try:
                os.link(
                    temporary_name,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                created = True
            except FileExistsError:
                existing_preimage = _regular_file_preimage_at(parent_fd, name)
                if (
                    existing_preimage is None
                    or _required_int(
                        existing_preimage.get("size"),
                        error="durable_target_preimage_invalid",
                    )
                    != len(content)
                    or str(existing_preimage["sha256"]) != hashlib.sha256(content).hexdigest()
                ):
                    raise DurableIOError("durable_immutable_collision")
            os.unlink(temporary_name, dir_fd=parent_fd)
            temporary_exists = False
            # An exact replay also repairs a prior run whose directory fsync
            # outcome was uncertain after the immutable link became visible.
            os.fsync(parent_fd)
            receipt_preimage: dict[str, object] | None = None
            if return_receipt:
                observed_preimage = _regular_file_preimage_at(parent_fd, name)
                if observed_preimage is None:
                    raise DurableIOError("durable_immutable_receipt_missing")
                if created:
                    published_preimage = _regular_file_preimage_from_descriptor(descriptor)
                    if observed_preimage != published_preimage:
                        raise DurableIOError("durable_target_preimage_changed")
                receipt_preimage = observed_preimage
        except DurableIOError:
            raise
        except OSError:
            raise DurableIOError("durable_immutable_publish_failed") from None
        finally:
            os.close(descriptor)
            if temporary_exists:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
    path = Path(root) / Path(relative_path)
    if return_receipt:
        if receipt_preimage is None:
            raise DurableIOError("durable_immutable_receipt_missing")
        return SecureImmutablePublishReceipt(
            path=path,
            created=created,
            preimage=receipt_preimage,
        )
    return path


@overload
def secure_publish_immutable_text(
    root: Path,
    relative_path: str | Path,
    content: str,
    *,
    encoding: str = "utf-8",
    return_receipt: Literal[True],
) -> SecureImmutablePublishReceipt: ...


@overload
def secure_publish_immutable_text(
    root: Path,
    relative_path: str | Path,
    content: str,
    *,
    encoding: str = "utf-8",
    return_receipt: Literal[False] = False,
) -> Path: ...


@overload
def secure_publish_immutable_text(
    root: Path,
    relative_path: str | Path,
    content: str,
    *,
    encoding: str = "utf-8",
    return_receipt: bool,
) -> Path | SecureImmutablePublishReceipt: ...


def secure_publish_immutable_text(
    root: Path,
    relative_path: str | Path,
    content: str,
    *,
    encoding: str = "utf-8",
    return_receipt: bool = False,
) -> Path | SecureImmutablePublishReceipt:
    encoded = content.encode(encoding)
    if return_receipt:
        return secure_publish_immutable_bytes(
            root,
            relative_path,
            encoded,
            return_receipt=True,
        )
    return secure_publish_immutable_bytes(root, relative_path, encoded)


def validate_secure_immutable_publish_receipt(
    receipt: SecureImmutablePublishReceipt,
) -> None:
    """Fail closed unless an immutable publication still has its exact preimage."""

    observed = secure_regular_file_preimage(
        receipt.path.parent,
        receipt.path.name,
    )
    if observed != receipt.preimage:
        raise DurableIOError("durable_target_preimage_changed")


def validate_secure_created_file_receipts(
    root: Path,
    created_files: Mapping[_PathKey, Mapping[str, object]],
) -> None:
    """Fail closed unless every receipt-bound created file still matches."""

    for relative_path, expected in sorted(
        created_files.items(),
        key=lambda item: Path(item[0]).as_posix(),
    ):
        observed = secure_regular_file_preimage(root, relative_path)
        if observed is None or not _preimage_matches(
            dict(expected),
            observed,
        ):
            raise DurableIOError("durable_target_preimage_changed")


def _regular_file_sha256_at(
    directory_fd: int,
    name: str,
    *,
    expected: os.stat_result,
) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError:
        raise DurableIOError("durable_directory_inventory_changed") from None
    try:
        before = os.fstat(descriptor)
        expected_identity = (
            int(expected.st_dev),
            int(expected.st_ino),
            int(expected.st_mode),
            int(expected.st_size),
            int(expected.st_mtime_ns),
            int(expected.st_ctime_ns),
        )
        before_identity = (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_mode),
            int(before.st_size),
            int(before.st_mtime_ns),
            int(before.st_ctime_ns),
        )
        if not stat.S_ISREG(before.st_mode) or before_identity != expected_identity:
            raise DurableIOError("durable_directory_inventory_changed")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        after_identity = (
            int(after.st_dev),
            int(after.st_ino),
            int(after.st_mode),
            int(after.st_size),
            int(after.st_mtime_ns),
            int(after.st_ctime_ns),
        )
        if after_identity != before_identity:
            raise DurableIOError("durable_directory_inventory_changed")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _directory_inventory_entries(
    directory_fd: int,
    *,
    prefix: str = "",
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    try:
        names = sorted(entry.name for entry in os.scandir(directory_fd))
    except OSError:
        raise DurableIOError("durable_directory_inventory_failed") from None
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            raise DurableIOError("durable_directory_inventory_changed") from None
        common = {
            "path": relative,
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
            "mode": stat.S_IMODE(metadata.st_mode),
            "mtime_ns": int(metadata.st_mtime_ns),
            "ctime_ns": int(metadata.st_ctime_ns),
        }
        if stat.S_ISREG(metadata.st_mode):
            entries.append(
                {
                    **common,
                    "kind": "file",
                    "size": int(metadata.st_size),
                    "sha256": _regular_file_sha256_at(
                        directory_fd,
                        name,
                        expected=metadata,
                    ),
                }
            )
            continue
        if stat.S_ISDIR(metadata.st_mode):
            entries.append({**common, "kind": "directory", "size": 0})
            try:
                child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
            except OSError:
                raise DurableIOError("durable_directory_inventory_unsafe") from None
            try:
                entries.extend(_directory_inventory_entries(child_fd, prefix=relative))
            finally:
                os.close(child_fd)
            continue
        raise DurableIOError("durable_directory_inventory_unsafe")
    return entries


def _directory_inventory_from_fd(
    directory_fd: int,
    *,
    relative_directory: str | Path,
) -> dict[str, object]:
    root_metadata = os.fstat(directory_fd)
    entries = _directory_inventory_entries(directory_fd)
    material = {
        "schema_version": "mnemos.secure_directory_tree_inventory.v2",
        "relative_directory": Path(relative_directory).as_posix(),
        "root_device": int(root_metadata.st_dev),
        "root_inode": int(root_metadata.st_ino),
        "root_mode": stat.S_IMODE(root_metadata.st_mode),
        "root_mtime_ns": int(root_metadata.st_mtime_ns),
        "root_ctime_ns": int(root_metadata.st_ctime_ns),
        "entries": entries,
    }
    canonical = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        **material,
        "total_bytes": sum(
            _required_int(
                entry.get("size"),
                error="durable_directory_inventory_invalid",
            )
            for entry in entries
            if entry.get("kind") == "file"
        ),
        "inventory_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def secure_directory_tree_inventory(
    root: Path,
    relative_directory: str | Path,
) -> dict[str, object]:
    """Return a no-follow metadata preimage for one managed directory tree."""

    with _secure_parent_fd(root, relative_directory, create=False) as (parent_fd, name):
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError:
            raise DurableIOError("durable_directory_target_unsafe") from None
        try:
            return _directory_inventory_from_fd(
                directory_fd,
                relative_directory=relative_directory,
            )
        finally:
            os.close(directory_fd)


def _inventory_entry_matches(
    expected: dict[str, object] | None,
    metadata: os.stat_result,
    *,
    kind: str,
) -> bool:
    if expected is None or expected.get("kind") != kind:
        return False
    try:
        if kind == "file" and _required_int(
            expected.get("size"),
            error="durable_directory_inventory_invalid",
        ) != int(metadata.st_size):
            return False
        return (
            _required_int(
                expected.get("device"),
                error="durable_directory_inventory_invalid",
            )
            == int(metadata.st_dev)
            and _required_int(
                expected.get("inode"),
                error="durable_directory_inventory_invalid",
            )
            == int(metadata.st_ino)
            and _required_int(
                expected.get("mode"),
                error="durable_directory_inventory_invalid",
            )
            == stat.S_IMODE(metadata.st_mode)
            and _required_int(
                expected.get("mtime_ns"),
                error="durable_directory_inventory_invalid",
            )
            == int(metadata.st_mtime_ns)
            and _required_int(
                expected.get("ctime_ns"),
                error="durable_directory_inventory_invalid",
            )
            == int(metadata.st_ctime_ns)
        )
    except (KeyError, TypeError, ValueError):
        return False


def _remove_directory_contents(
    directory_fd: int,
    *,
    expected_entries: dict[str, dict[str, object]],
    prefix: str = "",
) -> None:
    try:
        names = sorted(entry.name for entry in os.scandir(directory_fd))
    except OSError:
        raise DurableIOError("durable_directory_delete_scan_failed") from None
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            raise DurableIOError("durable_directory_delete_changed") from None
        if stat.S_ISREG(metadata.st_mode):
            if not _inventory_entry_matches(
                expected_entries.get(relative),
                metadata,
                kind="file",
            ):
                raise DurableIOError("durable_directory_delete_changed")
            os.unlink(name, dir_fd=directory_fd)
            continue
        if stat.S_ISDIR(metadata.st_mode):
            if not _inventory_entry_matches(
                expected_entries.get(relative),
                metadata,
                kind="directory",
            ):
                raise DurableIOError("durable_directory_delete_changed")
            child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if not _inventory_entry_matches(
                    expected_entries.get(relative),
                    opened,
                    kind="directory",
                ):
                    raise DurableIOError("durable_directory_delete_changed")
                _remove_directory_contents(
                    child_fd,
                    expected_entries=expected_entries,
                    prefix=relative,
                )
            finally:
                os.close(child_fd)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not _inventory_entry_matches(
                expected_entries.get(relative),
                current,
                kind="directory",
            ):
                raise DurableIOError("durable_directory_delete_changed")
            os.rmdir(name, dir_fd=directory_fd)
            continue
        raise DurableIOError("durable_directory_delete_unsafe")


def secure_remove_directory_tree(
    root: Path,
    relative_directory: str | Path,
    *,
    expected_inventory_sha256: str,
    expected_root_preimage: dict[str, object] | None = None,
) -> tuple[int, int]:
    """Delete an unchanged managed directory tree without following symlinks."""

    with _secure_parent_fd(root, relative_directory, create=False) as (parent_fd, name):
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(name, flags, dir_fd=parent_fd)
            try:
                current = _directory_inventory_from_fd(
                    directory_fd,
                    relative_directory=relative_directory,
                )
                if current.get("inventory_sha256") != expected_inventory_sha256:
                    raise DurableIOError("durable_directory_preimage_changed")
                if expected_root_preimage is not None:
                    observed_root = {
                        "device": current.get("root_device"),
                        "inode": current.get("root_inode"),
                        "mode": current.get("root_mode"),
                    }
                    if not _preimage_matches(
                        expected_root_preimage,
                        observed_root,
                    ):
                        raise DurableIOError("durable_directory_root_preimage_changed")
                entries = _inventory_entries(current.get("entries"))
                file_count = sum(1 for entry in entries if entry.get("kind") == "file")
                total_bytes = _required_int(
                    current.get("total_bytes"),
                    error="durable_directory_inventory_invalid",
                )
                expected_entries = {
                    str(entry["path"]): entry
                    for entry in entries
                    if isinstance(entry.get("path"), str)
                }
                _remove_directory_contents(
                    directory_fd,
                    expected_entries=expected_entries,
                )
                opened = os.fstat(directory_fd)
            finally:
                os.close(directory_fd)
            current_name = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(current_name.st_mode)
                or int(current_name.st_dev) != int(opened.st_dev)
                or int(current_name.st_ino) != int(opened.st_ino)
            ):
                raise DurableIOError("durable_directory_target_changed")
            os.rmdir(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except DurableIOError:
            raise
        except OSError:
            raise DurableIOError("durable_directory_delete_failed") from None
    return file_count, total_bytes


def secure_cleanup_created_tree(
    root: Path,
    *,
    created_files: Mapping[_PathKey, Mapping[str, object]],
    created_directories: Mapping[_PathKey, Mapping[str, object]],
) -> dict[str, object]:
    """Remove only receipt-bound files and then exact empty created directories.

    Untracked files or directories are never inferred to be owned merely because
    they appeared below a directory created by the caller.
    """

    removed_files: list[str] = []
    removed_directories: list[str] = []
    preserved_directories: list[str] = []
    cleanup_failure: DurableIOError | None = None
    normalized_files = sorted(
        ((Path(path).as_posix(), dict(preimage)) for path, preimage in created_files.items()),
        key=lambda item: (-len(Path(item[0]).parts), item[0]),
    )
    for relative_path, preimage in normalized_files:
        try:
            if secure_remove_regular_file(
                root,
                relative_path,
                missing_ok=True,
                expected_preimage=preimage,
            ):
                removed_files.append(relative_path)
        except DurableIOError as exc:
            if cleanup_failure is None:
                cleanup_failure = exc

    normalized_directories = sorted(
        ((Path(path).as_posix(), dict(preimage)) for path, preimage in created_directories.items()),
        key=lambda item: (-len(Path(item[0]).parts), item[0]),
    )
    for relative_directory, root_preimage in normalized_directories:
        try:
            inventory = secure_directory_tree_inventory(
                root,
                relative_directory,
            )
            if inventory["entries"]:
                preserved_directories.append(relative_directory)
                continue
            secure_remove_directory_tree(
                root,
                relative_directory,
                expected_inventory_sha256=str(inventory["inventory_sha256"]),
                expected_root_preimage=root_preimage,
            )
            removed_directories.append(relative_directory)
        except DurableIOError as exc:
            if cleanup_failure is None:
                cleanup_failure = exc

    if cleanup_failure is not None:
        raise cleanup_failure
    return {
        "removed_files": removed_files,
        "removed_directories": removed_directories,
        "preserved_directories": preserved_directories,
    }


def inspect_path_kind(path: Path) -> str:
    """Return ``missing/file/directory/other`` without hiding inspection errors.

    ``Path.exists()``, ``Path.is_file()`` and ``Path.is_dir()`` intentionally
    collapse some ``OSError`` values into ``False``.  That behavior is unsafe
    for authoritative state: callers cannot distinguish a genuinely absent
    database or control file from a path they were not able to inspect.
    """

    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        raise DurableIOError("durable_path_inspection_failed") from None
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    return "other"


def fsync_regular_file(path: Path) -> None:
    """Durably synchronize one verified regular file without following links."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(Path(path), flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise DurableIOError("durable_file_not_regular")
        os.fsync(descriptor)
    except OSError:
        raise DurableIOError("durable_file_fsync_failed") from None
    finally:
        if "descriptor" in locals():
            os.close(descriptor)


def fsync_directory(path: Path) -> None:
    """Durably synchronize one verified directory without following links."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(Path(path), flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise DurableIOError("durable_directory_not_directory")
        os.fsync(descriptor)
    except OSError:
        raise DurableIOError("durable_directory_fsync_failed") from None
    finally:
        if "descriptor" in locals():
            os.close(descriptor)


def ensure_private_directory(path: Path) -> Path:
    """Create or verify one owner-private directory without following symlinks."""

    candidate = Path(path).expanduser().absolute()
    descriptor = _open_secure_directory_path(candidate, create=True)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or int(metadata.st_uid) != int(os.getuid()):
            raise DurableIOError("durable_private_directory_unsafe")
        os.fchmod(descriptor, 0o700)
        os.fsync(descriptor)
        verified = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(verified.st_mode)
            or int(verified.st_uid) != int(os.getuid())
            or stat.S_IMODE(verified.st_mode) != 0o700
        ):
            raise DurableIOError("durable_private_directory_unsafe")
    except DurableIOError:
        raise
    except OSError:
        raise DurableIOError("durable_private_directory_unsafe") from None
    finally:
        os.close(descriptor)
    return candidate


def secure_create_directory(
    root: Path,
    relative_directory: str | Path,
) -> dict[str, object]:
    """Create one exact 0700 directory and return its root identity."""

    with _secure_parent_fd(root, relative_directory, create=False) as (
        parent_fd,
        name,
    ):
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            raise
        except OSError:
            raise DurableIOError("durable_directory_create_failed") from None
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
            try:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
                metadata = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(parent_fd)
        except OSError:
            raise DurableIOError("durable_directory_create_failed") from None
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise DurableIOError("durable_directory_create_failed")
        return {
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
            "mode": stat.S_IMODE(metadata.st_mode),
        }


def private_sqlite_sidecars(path: Path) -> tuple[Path, ...]:
    """Return every exact sidecar forbidden beside a private SQLite copy."""

    candidate = Path(path)
    return tuple(Path(f"{candidate}{suffix}") for suffix in _PRIVATE_SQLITE_SIDECAR_SUFFIXES)


@contextmanager
def owned_sqlite_connection_pair(
    source_factory: Callable[[], sqlite3.Connection],
    destination_factory: Callable[[], sqlite3.Connection],
) -> Iterator[tuple[sqlite3.Connection, sqlite3.Connection]]:
    """Own two SQLite acquisitions without leaking the first if the second fails."""

    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        source = source_factory()
        destination = destination_factory()
        yield source, destination
    finally:
        close_failure: BaseException | None = None
        for connection in (destination, source):
            if connection is None:
                continue
            try:
                connection.close()
            except (OSError, sqlite3.Error) as exc:
                if close_failure is None:
                    close_failure = exc
        if close_failure is not None:
            raise DurableIOError("sqlite_connection_pair_cleanup_failed") from None


def _sqlite_file_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(stat.S_IFMT(metadata.st_mode)),
    )


def _verify_anchored_sqlite_path(
    descriptor: int,
    path: Path,
    expected_identity: tuple[int, int, int],
) -> None:
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
    except OSError:
        raise DurableIOError("anchored_sqlite_identity_changed") from None
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or _sqlite_file_identity(opened) != expected_identity
        or _sqlite_file_identity(current) != expected_identity
    ):
        raise DurableIOError("anchored_sqlite_identity_changed")


@contextmanager
def anchored_sqlite_write_connection(
    path: Path,
    *,
    create: bool,
) -> Iterator[sqlite3.Connection]:
    """Open one SQLite writer while retaining a no-follow leaf identity anchor."""

    candidate = Path(path).expanduser()
    try:
        canonical = candidate.parent.resolve(strict=True) / candidate.name
    except OSError:
        raise DurableIOError("anchored_sqlite_parent_unavailable") from None
    kind = inspect_path_kind(canonical)
    if kind == "missing" and not create:
        raise FileNotFoundError(canonical)
    if kind not in {"missing", "file"}:
        raise DurableIOError("anchored_sqlite_path_not_regular")
    flags = (
        os.O_RDWR
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if kind == "missing":
        flags |= os.O_CREAT | os.O_EXCL
    descriptor = -1
    connection: sqlite3.Connection | None = None
    try:
        descriptor = os.open(canonical, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise DurableIOError("anchored_sqlite_path_not_regular")
        identity = _sqlite_file_identity(metadata)
        _verify_anchored_sqlite_path(descriptor, canonical, identity)
        connection = sqlite3.connect(str(canonical))
        _verify_anchored_sqlite_path(descriptor, canonical, identity)
        main_paths = [
            Path(str(row[2])).expanduser().parent.resolve(strict=True)
            / Path(str(row[2])).name
            for row in connection.execute("PRAGMA database_list").fetchall()
            if str(row[1]) == "main"
        ]
        if main_paths != [canonical]:
            raise DurableIOError("anchored_sqlite_identity_unverified")
        yield connection
        _verify_anchored_sqlite_path(descriptor, canonical, identity)
    except DurableIOError:
        raise
    except OSError:
        raise DurableIOError("anchored_sqlite_open_failed") from None
    finally:
        close_failure: BaseException | None = None
        if connection is not None:
            try:
                connection.close()
            except (OSError, sqlite3.Error) as exc:
                close_failure = exc
        if descriptor >= 0:
            os.close(descriptor)
        if close_failure is not None:
            raise DurableIOError("anchored_sqlite_close_failed") from None


def validate_private_sqlite_copy(path: Path) -> None:
    """Verify that one SQLite artifact is standalone without mutating it."""

    candidate = Path(path)
    descriptor = -1
    connection: sqlite3.Connection | None = None
    try:
        metadata = candidate.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise DurableIOError("private_sqlite_copy_not_regular")
        for sidecar in private_sqlite_sidecars(candidate):
            try:
                sidecar.lstat()
            except FileNotFoundError:
                continue
            raise DurableIOError("private_sqlite_copy_sidecar_present")
        canonical = candidate.parent.resolve(strict=True) / candidate.name
        descriptor = os.open(
            canonical,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        identity = _sqlite_file_identity(opened)
        _verify_anchored_sqlite_path(descriptor, canonical, identity)
        connection = sqlite3.connect(
            f"{canonical.as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
        _verify_anchored_sqlite_path(descriptor, canonical, identity)
        if connection.execute("PRAGMA journal_mode").fetchone() != ("delete",):
            raise DurableIOError("private_sqlite_copy_journal_mode_unverified")
        _verify_anchored_sqlite_path(descriptor, canonical, identity)
    except DurableIOError as exc:
        if str(exc) == "anchored_sqlite_identity_changed":
            raise DurableIOError("private_sqlite_copy_changed") from None
        raise
    except (OSError, sqlite3.Error):
        raise DurableIOError("private_sqlite_copy_validation_failed") from None
    finally:
        if connection is not None:
            connection.close()
        if descriptor >= 0:
            os.close(descriptor)


def normalize_private_sqlite_copy(path: Path) -> None:
    """Seal one private SQLite copy as a durable, standalone main file.

    SQLite's backup API can preserve WAL journal mode in the destination
    header.  Even a read-only open may then create or update ``-shm`` state
    that is outside the copied artifact's hash and receipt.  A migration
    backup, parser snapshot, restore stage, or restore drill therefore is not
    complete until it uses DELETE journal mode, has no sidecars, and can be
    opened with ``immutable=1`` without changing its physical scope.
    """

    candidate = Path(path)
    try:
        metadata = candidate.lstat()
        if candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise DurableIOError("private_sqlite_copy_not_regular")
        connection = sqlite3.connect(str(candidate))
        try:
            mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            connection.commit()
            if mode != ("delete",):
                raise DurableIOError("private_sqlite_copy_journal_mode_unverified")
        finally:
            connection.close()

        for sidecar in private_sqlite_sidecars(candidate):
            try:
                sidecar_metadata = sidecar.lstat()
            except FileNotFoundError:
                continue
            if sidecar.is_symlink() or not stat.S_ISREG(sidecar_metadata.st_mode):
                raise DurableIOError("private_sqlite_copy_sidecar_unsafe")
            sidecar.unlink()

        fsync_regular_file(candidate)
        fsync_directory(candidate.parent)
        validate_private_sqlite_copy(candidate)
    except DurableIOError:
        raise
    except (OSError, sqlite3.Error):
        raise DurableIOError("private_sqlite_copy_normalization_failed") from None


def physical_scope_signature(
    paths: Iterable[Path],
    *,
    inventory_directory: Path | None = None,
    hash_max_bytes: int = 16 * 1024 * 1024,
) -> dict[str, object]:
    """Capture physical metadata plus bounded hashes for zero-write verification."""
    return _physical_scope_signature(
        paths,
        inventory_directory=inventory_directory,
        hash_max_bytes=hash_max_bytes,
        entry_reader=_physical_entry,
        open_secure_directory=_open_secure_directory_path,
        inspect_path_kind=inspect_path_kind,
    )


def regular_file_sha256(path: Path) -> str:
    """Hash one exact regular-file generation without following replacement links."""

    signature = physical_scope_signature(
        (Path(path),),
        hash_max_bytes=(1 << 63) - 1,
    )
    entries = signature.get("entries")
    if not isinstance(entries, list) or len(entries) != 1:
        raise DurableIOError("durable_file_hash_invalid")
    entry = entries[0]
    if (
        not isinstance(entry, dict)
        or entry.get("present") is not True
        or entry.get("kind") != "file"
        or not isinstance(entry.get("sha256"), str)
    ):
        raise DurableIOError("durable_file_hash_invalid")
    return str(entry["sha256"])


__all__ = [
    "DurableIOError",
    "SecureImmutablePublishReceipt",
    "anchored_sqlite_write_connection",
    "canonical_native_path",
    "copy_native_file_to_descriptor",
    "ensure_private_directory",
    "fsync_directory",
    "fsync_regular_file",
    "inspect_path_kind",
    "normalize_private_sqlite_copy",
    "open_native_binary",
    "open_native_text",
    "owned_sqlite_connection_pair",
    "physical_scope_signature",
    "private_sqlite_sidecars",
    "read_native_bytes",
    "read_native_bytes_with_metadata",
    "regular_file_sha256",
    "secure_atomic_write_bytes",
    "secure_atomic_write_text",
    "secure_cleanup_created_tree",
    "secure_create_directory",
    "secure_directory_tree_inventory",
    "secure_publish_immutable_bytes",
    "secure_publish_immutable_text",
    "secure_read_bytes",
    "secure_regular_file_preimage",
    "secure_remove_directory_tree",
    "secure_remove_regular_file",
    "validate_secure_created_file_receipts",
    "validate_secure_immutable_publish_receipt",
    "validate_private_sqlite_copy",
]
