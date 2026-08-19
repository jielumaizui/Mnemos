"""Secure file-descriptor operations for Raw projection publication."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import List
import uuid

from scripts.raw_projection_contract import (
    safe_projection_target as _safe_projection_target,
)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("Raw projection write made no progress")
        offset += written


def _decode_utf8_prefix(data: bytes) -> str:
    """Decode a raw byte prefix, tolerating a multibyte char cut by the slice.

    Slicing at a fixed byte count can split a trailing multibyte UTF-8
    sequence; that truncation is an artifact of the probe, not of the file,
    so drop the partial tail. Genuinely invalid bytes still raise.
    """
    while True:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data" and exc.start >= len(data) - 4:
                data = data[: exc.start]
                continue
            raise


def _open_secure_directory_path(path: Path, *, create: bool) -> int:
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.open(os.sep, flags)
    try:
        for part in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(part, dir_fd=current_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _ensure_safe_projection_root(raw_dir: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = _open_secure_directory_path(raw_dir, create=True)
    except OSError as exc:
        raise ValueError("Raw projection vault root is unsafe") from exc
    try:
        try:
            os.mkdir(".obsidian", dir_fd=root_fd)
        except FileExistsError:
            obsidian_fd = os.open(".obsidian", flags, dir_fd=root_fd)
            os.close(obsidian_fd)
    finally:
        os.close(root_fd)


def _acquire_projection_transaction_lock(raw_dir: Path) -> int:
    try:
        root_fd = _open_secure_directory_path(raw_dir, create=False)
    except OSError as exc:
        raise RuntimeError("Raw projection transaction root is unsafe") from exc
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        os.close(root_fd)
        raise RuntimeError("Raw projection transaction owner is active") from exc
    return root_fd


def _release_projection_transaction_lock(lock_fd: int) -> None:
    if lock_fd < 0:
        return
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


@contextlib.contextmanager
def _secure_projection_parent_fd(
    raw_dir: Path,
    relative_path: str,
    *,
    create: bool,
):
    relative = Path(relative_path)
    _safe_projection_target(raw_dir, relative)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: List[int] = []
    try:
        descriptors.append(_open_secure_directory_path(raw_dir, create=False))
        current_fd = descriptors[-1]
        for part in relative.parts[:-1]:
            if create:
                try:
                    os.mkdir(part, dir_fd=current_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, flags, dir_fd=current_fd)
            descriptors.append(next_fd)
            current_fd = next_fd
    except FileNotFoundError:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        if create:
            raise ValueError("Raw projection path changed or became unsafe")
        raise
    except OSError as exc:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise ValueError("Raw projection path changed or became unsafe") from exc
    try:
        yield current_fd, relative.name
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _fd_file_hash(parent_fd: int, name: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return ""
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Raw projection target is not a regular file")
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)
    finally:
        os.close(descriptor)


def _secure_publish_staged_file(
    raw_dir: Path,
    relative_path: str,
    staged: Path,
    *,
    expected_preimage_hash: str,
    target_hash: str,
) -> None:
    with _secure_projection_parent_fd(raw_dir, relative_path, create=True) as (
        parent_fd,
        name,
    ):
        stage_parent_fd = _open_secure_directory_path(staged.parent, create=False)
        staged_fd = -1
        temporary_fd = -1
        temporary_name = f".{staged.name}.{uuid.uuid4().hex}.publish"
        try:
            staged_fd = os.open(
                staged.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=stage_parent_fd,
            )
            if not stat.S_ISREG(os.fstat(staged_fd).st_mode):
                raise RuntimeError("Raw projection staged chunk is not regular")
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=stage_parent_fd,
            )
            digest = hashlib.sha256()
            while True:
                block = os.read(staged_fd, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                _write_all(temporary_fd, block)
            os.fsync(temporary_fd)
            if digest.hexdigest() != target_hash:
                raise RuntimeError("Raw projection staged chunk is missing or invalid")
            os.close(temporary_fd)
            temporary_fd = -1
            if _fd_file_hash(parent_fd, name) != expected_preimage_hash:
                raise RuntimeError("Raw projection target preimage changed before publish")
            os.replace(
                temporary_name,
                name,
                src_dir_fd=stage_parent_fd,
                dst_dir_fd=parent_fd,
            )
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            if staged_fd >= 0:
                os.close(staged_fd)
            try:
                os.unlink(temporary_name, dir_fd=stage_parent_fd)
            except FileNotFoundError:
                pass
            os.close(stage_parent_fd)
        if _fd_file_hash(parent_fd, name) != target_hash:
            raise RuntimeError("Raw projection publish hash verification failed")
        os.fsync(parent_fd)


def _secure_atomic_write_bytes(
    root: Path,
    relative_path: str,
    content: bytes,
) -> None:
    with _secure_projection_parent_fd(root, relative_path, create=True) as (
        parent_fd,
        name,
    ):
        temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
            try:
                _write_all(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(
                temporary_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        finally:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass


def _secure_atomic_write_text(
    root: Path,
    relative_path: str,
    text: str,
) -> None:
    _secure_atomic_write_bytes(root, relative_path, text.encode("utf-8"))


def _secure_read_file(
    raw_dir: Path,
    relative_path: str,
) -> tuple[bytes | None, str]:
    try:
        with _secure_projection_parent_fd(raw_dir, relative_path, create=False) as (
            parent_fd,
            name,
        ):
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                return None, ""
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("Raw projection control file is not regular")
                content = bytearray()
                digest = hashlib.sha256()
                while True:
                    block = os.read(descriptor, 1024 * 1024)
                    if not block:
                        return bytes(content), digest.hexdigest()
                    content.extend(block)
                    digest.update(block)
            finally:
                os.close(descriptor)
    except FileNotFoundError:
        return None, ""


def _secure_unlink_file(
    root: Path,
    relative_path: str,
    *,
    expected_hash: str,
) -> bool:
    with _secure_projection_parent_fd(root, relative_path, create=False) as (
        parent_fd,
        name,
    ):
        current_hash = _fd_file_hash(parent_fd, name)
        if not current_hash:
            return False
        if current_hash != expected_hash:
            raise RuntimeError("secure file changed before unlink")
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True


def _secure_delete_managed_file(
    raw_dir: Path,
    relative_path: str,
    *,
    expected_hash: str,
) -> bool:
    with _secure_projection_parent_fd(raw_dir, relative_path, create=False) as (
        parent_fd,
        name,
    ):
        current_hash = _fd_file_hash(parent_fd, name)
        if not current_hash:
            return False
        if current_hash != expected_hash:
            raise RuntimeError("Raw projection stale target changed before delete")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        try:
            try:
                marker = _decode_utf8_prefix(os.read(descriptor, 4096))
            except UnicodeDecodeError:
                raise RuntimeError(
                    "Raw projection stale target is not valid UTF-8"
                ) from None
        finally:
            os.close(descriptor)
        if not re.search(
            r'^mnemos_type:\s+["\']?raw_retention_projection(?:_index)?["\']?\s*$',
            marker,
            flags=re.MULTILINE,
        ):
            raise RuntimeError("Raw projection stale target lost its ownership marker")
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return True
