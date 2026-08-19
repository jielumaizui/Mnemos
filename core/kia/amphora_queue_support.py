"""Pure identity, priority, and path-state helpers for Amphora."""

from __future__ import annotations

import hashlib
import stat
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class TaskPriority(Enum):
    NORMAL = 0
    HIGH = 1
    URGENT = 2


def task_id(
    session_id: str,
    source_agent: str = "",
    input_revision: str = "",
) -> str:
    """Return the stable task identity for one exact input generation."""

    material = (
        f"{source_agent}\0{session_id}\0{input_revision}"
        if input_revision
        else session_id
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def physical_message_path_kind(path: Path) -> str:
    """Inspect one Amphora message path without following its leaf."""

    try:
        metadata = Path(path).lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        raise RuntimeError("amphora_message_path_unavailable") from None
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    return "other"


def infer_priority(meta: Mapping[str, Any] | None) -> int:
    """Infer the bounded queue priority from explicit task metadata."""

    meta = meta or {}
    if meta.get("urgent") or meta.get("deadline"):
        return TaskPriority.URGENT.value
    if meta.get("important") or meta.get("user_requested"):
        return TaskPriority.HIGH.value
    return TaskPriority.NORMAL.value


def normalize_priority(
    priority: int | None,
    meta: Mapping[str, Any] | None,
) -> int:
    """Validate or infer one public Amphora task priority."""

    if priority is None:
        return infer_priority(meta)
    valid = {item.value for item in TaskPriority}
    if priority not in valid:
        raise ValueError(
            f"priority must be one of {sorted(valid)}, got {priority!r}"
        )
    return int(priority)
