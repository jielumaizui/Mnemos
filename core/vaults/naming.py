"""Shared Obsidian vault filename policy."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional, Set, Tuple


_DISPLAY_SAFE_RE = re.compile(r"[^\w\u4e00-\u9fa5-]+")
_SOURCE_PREFIX_RE = re.compile(
    r"^(?:"
    r"session__|"
    r"session[-_]|"
    r"sess[-_]|"
    r"codex[-_][^_]{1,32}_|"
    r"[0-9a-f]{8}_|"
    r"hash\d*_|"
    r"hash[0-9a-f]{4,}[-_]"
    r")",
    re.IGNORECASE,
)


def safe_display_slug(value: object, *, max_chars: int = 64) -> str:
    """Return a readable, filesystem-safe slug for user-facing Markdown files."""
    text = str(value or "").strip().lower()
    slug = _DISPLAY_SAFE_RE.sub("-", text)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:max_chars] if slug else "untitled"


def short_hash(value: object, *, length: int = 8) -> str:
    """Return a stable short hash for collision-only suffixes."""
    text = str(value or "")
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:length]


def allocate_title_path(
    directory: Path,
    title: object,
    *,
    source_id: object = "",
    seen_slugs: Optional[Set[str]] = None,
    suffix: str = ".md",
    max_attempts: int = 10000,
) -> Tuple[str, Path]:
    """Allocate a path using the display title first, appending hashes only on collision."""
    directory = Path(directory)
    base_slug = safe_display_slug(title)
    slug = base_slug
    if seen_slugs is not None:
        counter = 1
        while slug in seen_slugs:
            slug = f"{base_slug}-{counter}"
            counter += 1
        seen_slugs.add(slug)

    page_id = slug
    file_path = directory / f"{page_id}{suffix}"
    if not file_path.exists():
        return page_id, file_path

    digest = short_hash(source_id or page_id)
    original_page_id = page_id
    page_id = f"{original_page_id}-{digest}"
    file_path = directory / f"{page_id}{suffix}"

    disk_counter = 2
    for _ in range(max_attempts):
        if not file_path.exists():
            return page_id, file_path
        page_id = f"{original_page_id}-{digest}-{disk_counter}"
        file_path = directory / f"{page_id}{suffix}"
        disk_counter += 1

    raise RuntimeError(f"无法为页面 {original_page_id} 找到可用文件名（已尝试 {max_attempts} 次）")


def is_source_prefixed_stem(stem: str) -> bool:
    """Return True for stems that expose source/session ids before the title."""
    return bool(_SOURCE_PREFIX_RE.match(stem or ""))


def shadow_projection_stem(relative_page_path: object) -> str:
    """Return a compact rebuildable shadow projection stem."""
    return f"shadow-{short_hash(relative_page_path, length=10)}"


def relation_projection_stem(source: object, relation_type: object, target: object) -> str:
    """Return a compact rebuildable KG relation projection stem."""
    return f"rel-{short_hash(f'{source}|{relation_type}|{target}', length=12)}"
