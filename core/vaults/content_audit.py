"""Audit Obsidian vault content presentation, classification, and structure."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.frontmatter import fm_get, parse_frontmatter
from core.vaults.naming import is_source_prefixed_stem, safe_display_slug


IGNORED_PARTS = {".git"}
FORMAL_DIRS = {"01-People", "02-Projects", "03-Tech", "04-Concepts", "06-Retrospectives"}
SYSTEM_DIRS = {
    ".trash",
    "05-MOCs",
    "07-Shadow",
    "08-Reminders",
    "99-Archive",
    "99-Reports",
    "L2.4-KG",
    "L3-Observations",
    "L4-Reflections",
}
REQUIRED_FIELDS = ("type", "name", "domain", "summary")
LONG_FILENAME_THRESHOLD = 80


def _markdown_files(vault_dir: Path) -> List[Path]:
    return sorted(
        p
        for p in vault_dir.rglob("*.md")
        if not (IGNORED_PARTS & set(p.relative_to(vault_dir).parts))
    )


def _relative(path: Path, vault_dir: Path) -> str:
    return str(path.relative_to(vault_dir))


def _top_dir(path: Path, vault_dir: Path) -> str:
    rel = path.relative_to(vault_dir)
    return rel.parts[0] if rel.parts else "."


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        match = re.match(r"^#\s+(.+)$", line.strip())
        if match:
            return match.group(1).strip()
    return ""


def _page_title(path: Path, fm: Optional[Dict[str, Any]], body: str) -> str:
    title = fm_get(fm, "name") or fm_get(fm, "title") or _first_heading(body)
    return str(title or path.stem).strip()


def _frontmatter_record(path: Path) -> tuple[Optional[Dict[str, Any]], str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, "", "read_error"
    fm, body = parse_frontmatter(text)
    if fm is None:
        return None, body, "missing"
    return fm, body, "ok"


def _sample(items: Iterable[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    return list(items)[:limit]


def _missing_required_fields(fm: Optional[Dict[str, Any]]) -> List[str]:
    return [field for field in REQUIRED_FIELDS if not fm_get(fm, field)]


def _classify_page(
    path: Path,
    vault_dir: Path,
    fm: Optional[Dict[str, Any]],
    body: str,
) -> Dict[str, Any]:
    title = _page_title(path, fm, body)
    title_slug = safe_display_slug(title)
    rel = _relative(path, vault_dir)
    top = _top_dir(path, vault_dir)
    missing = _missing_required_fields(fm)
    return {
        "path": rel,
        "top_dir": top,
        "stem": path.stem,
        "title": title,
        "title_slug": title_slug,
        "frontmatter_missing": fm is None,
        "missing_required_fields": missing,
        "needs_review": bool(fm_get(fm, "needs_review")) if fm else False,
        "is_source_prefixed": is_source_prefixed_stem(path.stem),
        "long_filename": len(path.name) > LONG_FILENAME_THRESHOLD,
    }


def audit_vault_content(vault_dir: str | Path, *, sample_limit: int = 20) -> Dict[str, Any]:
    """Return a JSON-serializable audit for content display/classification/structure."""
    root = Path(vault_dir).expanduser()
    md_files = _markdown_files(root)
    records: List[Dict[str, Any]] = []
    fm_status: Counter[str] = Counter()

    for path in md_files:
        fm, body, status = _frontmatter_record(path)
        fm_status[status] += 1
        records.append(_classify_page(path, root, fm, body))

    root_pages = [r for r in records if len((root / r["path"]).relative_to(root).parts) == 1]
    long_names = [r for r in records if r["long_filename"]]
    source_prefixed = [r for r in records if r["is_source_prefixed"]]
    formal_source_prefixed = [
        r for r in source_prefixed if r["top_dir"] in FORMAL_DIRS
    ]
    needs_review = [
        r for r in records if r["needs_review"] and r["top_dir"] not in SYSTEM_DIRS
    ]
    inbox_ready = [
        r
        for r in records
        if r["top_dir"] == "00-Inbox"
        and not r["frontmatter_missing"]
        and not r["missing_required_fields"]
        and not r["needs_review"]
        and not r["stem"].startswith("session__")
    ]

    by_title_slug: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record["top_dir"] in SYSTEM_DIRS:
            continue
        if record["top_dir"] == "00-Inbox":
            continue
        if record["needs_review"]:
            continue
        by_title_slug[record["title_slug"]].append(record)
    title_collisions = []
    for slug, items in sorted(by_title_slug.items()):
        if len(items) <= 1:
            continue
        if not any(item["stem"] != slug or item["top_dir"] in FORMAL_DIRS for item in items):
            continue
        title_collisions.append(
            {
                "title_slug": slug,
                "count": len(items),
                "paths": [item["path"] for item in items],
                "titles": sorted({item["title"] for item in items}),
            }
        )

    formal_missing_required = [
        r for r in records if r["top_dir"] in FORMAL_DIRS and r["missing_required_fields"]
    ]
    frontmatter_problem_pages = [
        r for r in records if r["frontmatter_missing"]
    ]
    unresolved_templates = []
    json_blocks = []
    for path in md_files:
        rel = _relative(path, root)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "{{" in text or "<%" in text:
            unresolved_templates.append({"path": rel})
        if "```json" in text or "``` JSON" in text:
            json_blocks.append({"path": rel})

    long_by_dir = Counter(r["top_dir"] for r in long_names)
    prefixed_by_dir = Counter(r["top_dir"] for r in source_prefixed)

    return {
        "vault_dir": str(root),
        "markdown_files": len(md_files),
        "display": {
            "root_pages": len(root_pages),
            "root_page_samples": _sample(root_pages, sample_limit),
            "long_filenames": len(long_names),
            "long_filenames_by_dir": dict(long_by_dir.most_common()),
            "long_filename_samples": _sample(long_names, sample_limit),
            "source_prefixed_filenames": len(source_prefixed),
            "source_prefixed_by_dir": dict(prefixed_by_dir.most_common()),
            "source_prefixed_samples": _sample(source_prefixed, sample_limit),
        },
        "classification": {
            "inbox_ready_to_classify": len(inbox_ready),
            "inbox_ready_samples": _sample(inbox_ready, sample_limit),
            "needs_review_pages": len(needs_review),
            "needs_review_samples": _sample(needs_review, sample_limit),
            "formal_source_prefixed_pages": len(formal_source_prefixed),
            "formal_source_prefixed_samples": _sample(formal_source_prefixed, sample_limit),
            "title_basename_collision_groups": len(title_collisions),
            "title_basename_collisions": _sample(title_collisions, sample_limit),
        },
        "structured_output": {
            "frontmatter_status": dict(fm_status),
            "frontmatter_problem_pages": len(frontmatter_problem_pages),
            "frontmatter_problem_samples": _sample(frontmatter_problem_pages, sample_limit),
            "formal_missing_required_fields": len(formal_missing_required),
            "formal_missing_required_samples": _sample(formal_missing_required, sample_limit),
            "unresolved_template_pages": len(unresolved_templates),
            "unresolved_template_samples": _sample(unresolved_templates, sample_limit),
            "json_block_pages": len(json_blocks),
            "json_block_samples": _sample(json_blocks, sample_limit),
        },
    }


def format_content_audit(report: Dict[str, Any], sample_limit: int = 10) -> str:
    """Render a compact human-readable content audit."""
    display = report["display"]
    classification = report["classification"]
    structured = report["structured_output"]
    lines = [
        "Vault content audit",
        "=" * 40,
        f"vault_dir: {report['vault_dir']}",
        f"markdown_files: {report['markdown_files']}",
        "",
        "[display]",
        f"root_pages: {display['root_pages']}",
        f"long_filenames: {display['long_filenames']}",
        f"source_prefixed_filenames: {display['source_prefixed_filenames']}",
        "",
        "[classification]",
        f"inbox_ready_to_classify: {classification['inbox_ready_to_classify']}",
        f"needs_review_pages: {classification['needs_review_pages']}",
        f"formal_source_prefixed_pages: {classification['formal_source_prefixed_pages']}",
        f"title_basename_collision_groups: {classification['title_basename_collision_groups']}",
        "",
        "[structured_output]",
        f"frontmatter_problem_pages: {structured['frontmatter_problem_pages']}",
        f"formal_missing_required_fields: {structured['formal_missing_required_fields']}",
        f"unresolved_template_pages: {structured['unresolved_template_pages']}",
        f"json_block_pages: {structured['json_block_pages']}",
    ]

    sections = [
        ("Inbox pages ready to classify", classification.get("inbox_ready_samples", [])),
        ("Pages marked needs_review", classification.get("needs_review_samples", [])),
        ("Title/basename collisions", classification.get("title_basename_collisions", [])),
        (
            "Formal pages missing required fields",
            structured.get("formal_missing_required_samples", []),
        ),
        ("Source-prefixed filenames", display.get("source_prefixed_samples", [])),
    ]
    for title, items in sections:
        if not items:
            continue
        lines.extend(["", f"{title}:"])
        for item in items[:sample_limit]:
            if "paths" in item:
                lines.append(f"- {item['title_slug']}: {', '.join(item['paths'][:4])}")
            else:
                missing = item.get("missing_required_fields") or []
                suffix = f" missing={missing}" if missing else ""
                lines.append(f"- {item.get('path')}{suffix}")

    return "\n".join(lines)
