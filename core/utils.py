# -*- coding: utf-8 -*-
"""
核心工具函数 — 跨模块共享的通用工具。

避免在多个模块中重复实现相同功能。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.file_ops import atomic_write_text, secure_directory, secure_file, set_sensitive_umask

__all__ = [
    "atomic_write_text",
    "secure_directory",
    "secure_file",
    "set_sensitive_umask",
    "read_bytes_value",
    "read_text_value",
    "load_json_value",
    "LazyPath",
    "WIKI_DIRS",
    "EXCLUDED_DIRS",
]


def read_text_value(path: Path, *, errors: str = "strict") -> str:
    """Read one UTF-8 text value through the shared runtime-data IO owner."""

    return Path(path).read_text(encoding="utf-8", errors=errors)


def read_bytes_value(path: Path) -> bytes:
    """Read one binary value through the shared runtime-data IO owner."""

    return Path(path).read_bytes()


def load_json_value(path: Path) -> Any:
    """Load one UTF-8 JSON value through the shared runtime-data IO owner."""

    return json.loads(read_text_value(path))


def _get_wiki_dir():
    """Lazy-load wiki directory to avoid side effects at import time."""
    from core.config import get_config

    return get_config().wiki_dir


class LazyPath:
    """延迟路径 — 仅在访问时才解析 get_config()，避免模块导入时的副作用。

    用法：
        WIKI_DIR = LazyPath("wiki_dir")
        INBOX = WIKI_DIR / "00-Inbox"  # 返回新的 LazyPath
        str(INBOX)  # 此时才解析为实际 Path
    """

    __slots__ = ("_base", "_segments")

    def __init__(self, base: str = "data_dir", *segments: str):
        self._base = base
        self._segments = segments

    def __truediv__(self, other: str) -> "LazyPath":
        return LazyPath(self._base, *self._segments, other)

    def __rtruediv__(self, other):
        raise NotImplementedError

    def _resolve(self) -> Path:
        from core.config import get_config

        config = get_config()
        if self._base == "data_dir":
            result = config.data_dir
        elif self._base == "wiki_dir":
            result = config.wiki_dir
        elif self._base == "database_dir":
            result = config.database_dir
        else:
            result = config.data_dir
        for seg in self._segments:
            result = result / seg
        return result

    def __str__(self) -> str:
        return str(self._resolve())

    def __repr__(self) -> str:
        return f"LazyPath({self._base}:{'/'.join(self._segments)})"

    def __fspath__(self) -> str:
        return str(self._resolve())

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._resolve(), name)

    def __hash__(self) -> int:
        return hash((self._base, self._segments))

    def __eq__(self, other) -> bool:
        if isinstance(other, LazyPath):
            return self._base == other._base and self._segments == other._segments
        return self._resolve() == other  # type: ignore[no-any-return]

    def __iter__(self):
        return iter(self._resolve())


# Wiki 目录结构常量 — 统一引用，避免各模块硬编码
WIKI_DIRS = [
    "00-Inbox",
    "01-People",
    "02-Projects",
    "03-Tech",
    "04-Concepts",
    "05-MOCs",
    "06-Retrospectives",
    "07-Shadow",
    "08-Reminders",
    "99-Reports",
]

# 搜索/扫描时应排除的目录
EXCLUDED_DIRS = {".git", ".obsidian", ".kg", "__pycache__"}
