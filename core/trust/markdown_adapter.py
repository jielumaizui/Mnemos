"""Safe Markdown writes for trusted push."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from core.frontmatter import parse_frontmatter, write_frontmatter
from core.trust.models import sha256_text
from core.utils import atomic_write_text


def read_markdown_text(path: Path) -> str:
    """Central read used by trusted mutation concurrency checks."""

    return Path(path).read_text(encoding="utf-8")


@dataclass(frozen=True)
class MarkdownWriteResult:
    status: str
    path: Path
    content_hash: str
    conflict_path: Path | None = None
    error: str = ""


class MarkdownAdapter:
    """Write Markdown without silently overwriting external edits."""

    def __init__(self, wiki_base: Path):
        self.wiki_base = Path(wiki_base)

    def write(
        self,
        path: Path,
        content: str,
        *,
        expected_existing_hash: str | None = None,
        conflict_metadata: Dict[str, Any] | None = None,
    ) -> MarkdownWriteResult:
        path = Path(path)
        content_hash = sha256_text(content)
        if path.exists():
            existing = read_markdown_text(path)
            existing_hash = sha256_text(existing)
            if expected_existing_hash is None or existing_hash != expected_existing_hash:
                conflict_path = self._conflict_path(path)
                conflict_content = self._build_conflict_content(
                    existing,
                    content,
                    conflict_metadata or {},
                )
                conflict_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(conflict_path, conflict_content, encoding="utf-8")
                return MarkdownWriteResult(
                    status="conflict",
                    path=path,
                    conflict_path=conflict_path,
                    content_hash=content_hash,
                )

        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, content, encoding="utf-8")
        return MarkdownWriteResult(status="written", path=path, content_hash=content_hash)

    def _conflict_path(self, path: Path) -> Path:
        stem = path.stem
        suffix = path.suffix or ".md"
        candidate = path.with_name(f"{stem}.mnemos-conflict{suffix}")
        counter = 2
        while candidate.exists():
            candidate = path.with_name(f"{stem}.mnemos-conflict-{counter}{suffix}")
            counter += 1
        return candidate

    @staticmethod
    def _build_conflict_content(existing: str, proposed: str, metadata: Dict[str, Any]) -> str:
        fm, _ = parse_frontmatter(existing)
        frontmatter = dict(fm or {})
        frontmatter["mnemos_conflict"] = {
            "status": "unresolved",
            "reason": "external markdown modification",
            **metadata,
        }
        body = (
            "# Mnemos Conflict\n\n"
            "## Proposed Content\n\n"
            f"{proposed}\n"
        )
        return write_frontmatter(frontmatter, body)
