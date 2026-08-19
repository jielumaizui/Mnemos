"""Resolve safe target folders for generated Wiki pages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from core.frontmatter import fm_get
from core.vaults.content_audit import FORMAL_DIRS
from core.vaults.naming import allocate_title_path, safe_display_slug


@dataclass(frozen=True)
class WikiRouteDecision:
    target_dir: Path
    status: str
    reason: str

    @property
    def routed(self) -> bool:
        return self.status == "direct"


def resolve_write_target_dir(
    *,
    wiki_base: Path,
    inbox_dir: Path,
    title: str,
    frontmatter: Mapping[str, Any] | None,
    enabled: bool = True,
) -> WikiRouteDecision:
    """Return the write directory for a generated Wiki page.

    Pages with a clear Charon classification are written directly to that
    formal directory. Ambiguous pages stay in Inbox with a reason in
    frontmatter for later routing.
    """
    if not enabled:
        return WikiRouteDecision(inbox_dir, "inbox", "wiki_route_disabled")

    fm = dict(frontmatter or {})
    display_title = str(fm_get(fm, "name") or title or "untitled")
    fm.setdefault("name", display_title)
    fake_page = inbox_dir / f"{safe_display_slug(display_title)}.md"
    try:
        from core.kia import charon

        target_dir = charon.resolve_page_folder(fake_page, fm, entities=None)
        configured_wiki = Path(str(charon.WIKI_DIR))
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        AttributeError,
        RuntimeError,
    ) as exc:
        return WikiRouteDecision(inbox_dir, "inbox", f"resolver_error:{type(exc).__name__}")

    if target_dir is None:
        return WikiRouteDecision(inbox_dir, "inbox", "unclassified")

    target = _remap_to_wiki_base(Path(target_dir), configured_wiki, wiki_base)
    if _formal_basename_collisions(wiki_base, safe_display_slug(display_title)):
        return WikiRouteDecision(inbox_dir, "inbox", "formal_basename_collision")
    return WikiRouteDecision(target, "direct", f"resolved:{_relative_reason(target, wiki_base)}")


def allocate_routed_title_path(
    *,
    wiki_base: Path,
    inbox_dir: Path,
    title: str,
    frontmatter: MutableMapping[str, Any],
    source_id: str,
    seen_slugs: set[str],
) -> tuple[str, Path]:
    """Allocate a page path after applying Wiki route frontmatter."""
    route = resolve_write_target_dir(
        wiki_base=wiki_base,
        inbox_dir=inbox_dir,
        title=title,
        frontmatter=frontmatter,
    )
    _apply_route_frontmatter(frontmatter, route, wiki_base)
    return allocate_title_path(
        route.target_dir,
        title,
        source_id=source_id,
        seen_slugs=seen_slugs,
    )


def _apply_route_frontmatter(
    frontmatter: MutableMapping[str, Any],
    route: WikiRouteDecision,
    wiki_base: Path,
) -> None:
    frontmatter["wiki_route_status"] = route.status
    frontmatter["wiki_route_reason"] = route.reason
    if route.routed:
        frontmatter["wiki_route_target"] = _relative_reason(route.target_dir, wiki_base)


def _remap_to_wiki_base(target_dir: Path, configured_wiki: Path, wiki_base: Path) -> Path:
    try:
        return wiki_base / target_dir.relative_to(configured_wiki)
    except ValueError:
        if target_dir.is_absolute():
            return wiki_base / target_dir.name
        return wiki_base / target_dir


def _relative_reason(path: Path, wiki_base: Path) -> str:
    try:
        return path.relative_to(wiki_base).as_posix()
    except ValueError:
        return path.as_posix()


def _formal_basename_collisions(wiki_base: Path, stem: str) -> list[Path]:
    collisions: list[Path] = []
    for top_dir in FORMAL_DIRS:
        root = wiki_base / top_dir
        if not root.exists():
            continue
        for candidate in root.rglob("*.md"):
            if candidate.stem == stem:
                collisions.append(candidate)
    return sorted(collisions)
