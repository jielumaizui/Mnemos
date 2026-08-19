"""Generic append-only JSONL rotation helpers.

Provides size-based rotation so that unbounded ``.jsonl`` files do not grow
indefinitely.  Archives are named ``<stem>.1.jsonl``, ``<stem>.2.jsonl``, ...
with ``.1`` being the most recent archive.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

DEFAULT_MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
DEFAULT_MAX_ARCHIVES = 5


def rotate_jsonl(
    path: Path,
    *,
    max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES,
    max_archives: int = DEFAULT_MAX_ARCHIVES,
) -> bool:
    """Rotate *path* if its size exceeds *max_size_bytes*.

    Archives are shifted up (``.1`` -> ``.2``, etc.) and the current file is
    moved to ``.1``.  Archives older than *max_archives* are deleted.

    Returns:
        True if rotation was performed, False otherwise.
    """
    path = Path(path)
    if not path.exists():
        return False

    if path.stat().st_size <= max_size_bytes:
        return False

    archive_paths = _list_archives(path)
    # Delete oldest archives that would exceed max_archives after the new one.
    # After rotation there will be max_archives archives at most.
    while len(archive_paths) >= max_archives:
        oldest = archive_paths.pop(-1)
        try:
            oldest.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("[jsonl_rotation] 删除最旧归档失败 %s: %s", oldest, exc)

    # Shift existing archives upward: N -> N+1, N-1 -> N, ...
    for old_archive in reversed(archive_paths):
        old_num = _archive_number(old_archive)
        new_archive = _archive_path(path, old_num + 1)
        try:
            shutil.move(str(old_archive), str(new_archive))
        except OSError as exc:
            logger.warning("[jsonl_rotation] 移动归档失败 %s -> %s: %s", old_archive, new_archive, exc)

    new_archive = _archive_path(path, 1)
    try:
        shutil.move(str(path), str(new_archive))
    except OSError as exc:
        logger.warning("[jsonl_rotation] 轮转失败 %s -> %s: %s", path, new_archive, exc)
        return False

    logger.info(
        "[jsonl_rotation] 已轮转 %s -> %s (max_size=%d MB, max_archives=%d)",
        path,
        new_archive,
        max_size_bytes // (1024 * 1024),
        max_archives,
    )
    return True


def iter_jsonl_lines(path: Path) -> Iterator[str]:
    """Yield JSON lines from *path* and its archives in chronological order.

    Oldest archive first, then ..., ``.2``, ``.1``, current file.
    """
    path = Path(path)
    archives = _list_archives(path)
    # oldest archive has the highest number
    for archive in reversed(archives):
        yield from _iter_file_lines(archive)
    if path.exists():
        yield from _iter_file_lines(path)


def _iter_file_lines(path: Path) -> Iterator[str]:
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                yield line
    except OSError as exc:
        logger.warning("[jsonl_rotation] 读取失败 %s: %s", path, exc)


def _list_archives(path: Path) -> list[Path]:
    """Return archive paths sorted by archive number ascending (1 newest)."""
    if not path.parent.exists():
        return []
    pattern = re.compile(re.escape(path.stem) + r"\.(\d+)" + re.escape(path.suffix) + "$")
    archives: list[tuple[int, Path]] = []
    for p in path.parent.iterdir():
        m = pattern.match(p.name)
        if m:
            archives.append((int(m.group(1)), p))
    archives.sort(key=lambda x: x[0])
    return [p for _, p in archives]


def _archive_number(path: Path) -> int:
    m = re.search(r"\.(\d+)" + re.escape(path.suffix) + "$", path.name)
    return int(m.group(1)) if m else 0


def _archive_path(path: Path, number: int) -> Path:
    return path.parent / f"{path.stem}.{number}{path.suffix}"


def jsonl_total_size(path: Path) -> int:
    """Return total size in bytes of *path* and all archives."""
    path = Path(path)
    total = path.stat().st_size if path.exists() else 0
    for archive in _list_archives(path):
        try:
            total += archive.stat().st_size
        except OSError:
            pass
    return total


def cleanup_jsonl_archives(
    path: Path,
    *,
    max_total_size_bytes: Optional[int] = None,
    max_age_days: Optional[int] = None,
) -> int:
    """Delete old archives by total-size or age, keeping the current file.

    Returns the number of deleted archives.
    """
    path = Path(path)
    archives = _list_archives(path)
    removed = 0

    if max_age_days is not None:
        import time

        cutoff = time.time() - max_age_days * 86400
        survivors: list[Path] = []
        for archive in archives:
            try:
                if archive.stat().st_mtime < cutoff:
                    archive.unlink(missing_ok=True)
                    removed += 1
                    continue
            except OSError as exc:
                logger.warning("[jsonl_rotation] 删除过期归档失败 %s: %s", archive, exc)
            survivors.append(archive)
        archives = survivors

    if max_total_size_bytes is not None:
        current_size = path.stat().st_size if path.exists() else 0
        total = current_size + sum(
            a.stat().st_size for a in archives if a.exists()
        )
        # delete oldest archives until total is under limit
        while archives and total > max_total_size_bytes:
            oldest = archives.pop(-1)
            try:
                size = oldest.stat().st_size
                oldest.unlink(missing_ok=True)
                total -= size
                removed += 1
            except OSError as exc:
                logger.warning("[jsonl_rotation] 删除归档失败 %s: %s", oldest, exc)

    return removed
