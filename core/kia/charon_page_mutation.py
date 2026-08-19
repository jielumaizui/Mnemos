"""Pure rendering helpers for Charon page classification mutations."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def render_classified_page(text: str, target_dir: Path, wiki_base: Path) -> str:
    """Return classified Markdown content without performing filesystem mutation."""

    try:
        rel = target_dir.relative_to(wiki_base)
    except ValueError:
        rel = Path(target_dir.name)
    category = rel.parts[0] if rel.parts else "unknown"
    subfolder = rel.parts[1] if len(rel.parts) > 1 else ""
    now = datetime.now().isoformat(timespec="seconds")
    frontmatter = {"auto_classified": True, "classified_at": now, "category": category}
    if subfolder:
        frontmatter["subfolder"] = subfolder

    import yaml

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            current = yaml.safe_load(parts[1]) or {}
            current.update(frontmatter)
            rendered = yaml.dump(current, allow_unicode=True, sort_keys=False)
            return f"---\n{rendered}---\n{parts[2]}"
    rendered = yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)
    return f"---\n{rendered}---\n{text}"
