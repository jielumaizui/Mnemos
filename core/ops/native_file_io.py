"""Stable no-follow readers for native regular-file artifacts."""

from __future__ import annotations

from contextlib import contextmanager
import io
import os
from pathlib import Path
import stat
from typing import BinaryIO, Iterator, TextIO


def canonical_native_path(path: Path) -> Path:
    """Resolve parent aliases while retaining the final no-follow boundary."""

    candidate = Path(path).expanduser()
    return candidate.parent.resolve(strict=True) / candidate.name


def _stable_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


@contextmanager
def open_native_binary(path: Path) -> Iterator[BinaryIO]:
    """Open one stable regular file without following its leaf symlink."""

    candidate = Path(path)
    descriptor = -1
    handle: BinaryIO | None = None
    try:
        descriptor = os.open(
            candidate,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("native_artifact_not_regular")
        handle = os.fdopen(descriptor, "rb")
        descriptor = -1
        yield handle
        after = os.fstat(handle.fileno())
        current = os.stat(candidate, follow_symlinks=False)
        if (
            not stat.S_ISREG(after.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or _stable_file_identity(before) != _stable_file_identity(after)
            or _stable_file_identity(after) != _stable_file_identity(current)
        ):
            raise OSError("native_artifact_changed_during_read")
    finally:
        if handle is not None:
            handle.close()
        elif descriptor >= 0:
            os.close(descriptor)


def read_native_bytes(path: Path) -> bytes:
    """Read exact bytes without following a final-component symlink."""

    with open_native_binary(path) as handle:
        return handle.read()


def read_native_bytes_with_metadata(
    path: Path,
) -> tuple[bytes, os.stat_result]:
    """Read bytes and metadata from the same stable regular-file generation."""

    with open_native_binary(path) as handle:
        metadata = os.fstat(handle.fileno())
        return handle.read(), metadata


@contextmanager
def open_native_text(
    path: Path,
    *,
    encoding: str = "utf-8",
    errors: str = "strict",
) -> Iterator[TextIO]:
    """Stream decoded text through the same stable native descriptor owner."""

    with open_native_binary(path) as binary:
        handle = io.TextIOWrapper(binary, encoding=encoding, errors=errors)
        try:
            yield handle
        finally:
            handle.detach()


def copy_native_file_to_descriptor(path: Path, descriptor: int) -> int:
    """Copy one stable native file to an already-owned descriptor in full."""

    copied = 0
    with open_native_binary(path) as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                return copied
            offset = 0
            while offset < len(chunk):
                written = os.write(descriptor, chunk[offset:])
                if written <= 0:
                    raise OSError("native_artifact_copy_incomplete")
                offset += written
                copied += written


__all__ = [
    "canonical_native_path",
    "copy_native_file_to_descriptor",
    "open_native_binary",
    "open_native_text",
    "read_native_bytes",
    "read_native_bytes_with_metadata",
]
