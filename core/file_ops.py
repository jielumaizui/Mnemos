"""Minimal filesystem primitives that do not depend on runtime configuration."""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import uuid
from pathlib import Path
from typing import Union

_LOGGER = logging.getLogger(__name__)
PathLike = Union[str, Path]


def sha256_file(path: PathLike, *, chunk_size: int = 1024 * 1024) -> str:
    """Stream a complete SHA-256 digest without loading the file into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Atomically replace a text file without exposing a partial write."""
    temporary = path.with_suffix(f"{path.suffix}.tmp.{uuid.uuid4().hex}")
    temporary.write_text(content, encoding=encoding)
    temporary.replace(path)


def secure_directory(path: PathLike) -> bool:
    """Set an existing directory to owner-only permissions."""
    try:
        directory = Path(path)
        if not directory.exists():
            return False
        os.chmod(directory, stat.S_IRWXU)
        return True
    except OSError as exc:
        _LOGGER.warning("[permissions] 无法加固目录 %s: %s", path, exc)
        return False


def secure_file(path: PathLike) -> bool:
    """Set an existing regular file to owner-only read/write permissions."""
    try:
        target = Path(path)
        if not target.exists() or not target.is_file():
            return False
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
        return True
    except OSError as exc:
        _LOGGER.warning("[permissions] 无法加固文件 %s: %s", path, exc)
        return False


def set_sensitive_umask() -> int:
    """Set a restrictive process umask and return the prior value."""
    previous = os.umask(0o077)
    _LOGGER.debug("[permissions] 已设置敏感 umask 077 (旧 umask=%03o)", previous)
    return previous
