"""Idempotent Wiki page identity for one distillation input revision."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from core.frontmatter import fm_get, parse_frontmatter
from core.vaults.naming import safe_display_slug
from core.vaults.page_routing import allocate_routed_title_path


def distillation_fragment_hash(fragment: Any, ordinal: int = 0) -> str:
    """Stable logical fragment identity within an input revision."""
    payload = {
        "form": str(getattr(fragment, "form", "")),
        "title": str(getattr(fragment, "title", "")),
        "ordinal": int(ordinal),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def allocate_revision_page_path(
    *,
    wiki_base: Path,
    inbox_dir: Path,
    title: str,
    frontmatter: dict[str, Any],
    source_id: str,
    source_session: str,
    input_revision: str,
    fragment_hash: str,
    seen_slugs: set[str],
) -> tuple[str, Path]:
    """Reuse a page from the same immutable input revision; otherwise allocate normally."""
    page_id, candidate = allocate_routed_title_path(
        wiki_base=wiki_base,
        inbox_dir=inbox_dir,
        title=title,
        frontmatter=frontmatter,
        source_id=source_id,
        seen_slugs=seen_slugs,
    )
    if not input_revision:
        return page_id, candidate
    base_slug = safe_display_slug(title)
    for existing in sorted(candidate.parent.glob(f"{base_slug}*.md")):
        try:
            parsed, _ = parse_frontmatter(existing.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, TypeError):
            continue
        if not parsed:
            continue
        if (
            str(fm_get(parsed, "source_session", "")) == source_session
            and str(fm_get(parsed, "input_revision", "")) == input_revision
            and str(fm_get(parsed, "fragment_hash", "")) == fragment_hash
        ):
            seen_slugs.add(existing.stem)
            return existing.stem, existing
    return page_id, candidate
