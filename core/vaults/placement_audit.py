"""Audit Obsidian vault page placement and basename collisions."""

from __future__ import annotations

import hashlib
import shutil
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


IGNORED_PARTS = {".git"}
FORMAL_DIRS = {"01-People", "02-Projects", "03-Tech", "04-Concepts", "06-Retrospectives"}
LOW_PRIORITY_DIRS = {".trash", "99-Archive"}


def _markdown_files(vault_dir: Path) -> List[Path]:
    return sorted(
        p for p in vault_dir.rglob("*.md") if not (IGNORED_PARTS & set(p.relative_to(vault_dir).parts))
    )


def _relative(path: Path, vault_dir: Path) -> str:
    return str(path.relative_to(vault_dir))


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8"), usedforsecurity=False).hexdigest()


def _folder_stats(vault_dir: Path, md_files: Iterable[Path]) -> Dict[str, Dict[str, int]]:
    files = list(md_files)
    dirs = {vault_dir}
    dirs.update(p.parent for p in files)
    dirs.update(p for p in vault_dir.rglob("*") if p.is_dir() and ".git" not in p.parts)

    stats: Dict[str, Dict[str, int]] = {}
    for directory in sorted(dirs):
        direct = [p for p in files if p.parent == directory]
        subtree = [p for p in files if p == directory or directory in p.parents]

        direct_names: Dict[str, List[Path]] = defaultdict(list)
        for path in direct:
            direct_names[path.stem].append(path)

        subtree_names: Dict[str, List[Path]] = defaultdict(list)
        for path in subtree:
            subtree_names[path.stem].append(path)

        rel = "." if directory == vault_dir else _relative(directory, vault_dir)
        stats[rel] = {
            "direct_files": len(direct),
            "subtree_files": len(subtree),
            "direct_duplicate_groups": sum(1 for paths in direct_names.values() if len(paths) > 1),
            "subtree_duplicate_groups": sum(
                1 for paths in subtree_names.values() if len(paths) > 1
            ),
        }
    return stats


def _duplicate_groups(vault_dir: Path, md_files: Iterable[Path]) -> List[Dict[str, Any]]:
    by_name: Dict[str, List[Path]] = defaultdict(list)
    for path in md_files:
        by_name[path.stem].append(path)

    groups: List[Dict[str, Any]] = []
    for name, paths in sorted(by_name.items()):
        if len(paths) <= 1:
            continue
        by_hash: Dict[str, List[Path]] = defaultdict(list)
        for path in paths:
            by_hash[_hash(path)].append(path)
        groups.append(
            {
                "name": name,
                "count": len(paths),
                "paths": [_relative(path, vault_dir) for path in paths],
                "identical_content": any(len(same) > 1 for same in by_hash.values()),
                "distinct_content_count": len(by_hash),
            }
        )
    return groups


def _keep_rank(vault_dir: Path, path: Path) -> tuple:
    rel = path.relative_to(vault_dir)
    first = rel.parts[0] if rel.parts else ""
    if first in FORMAL_DIRS:
        tier = 0
    elif first == "00-Inbox":
        tier = 1
    elif first.startswith("L"):
        tier = 2
    elif first in LOW_PRIORITY_DIRS:
        tier = 9
    else:
        tier = 5
    return (tier, len(rel.parts), str(rel))


def _duplicate_archive_path(
    vault_dir: Path,
    duplicate_path: Path,
    archive_date: str,
) -> Path:
    rel = duplicate_path.relative_to(vault_dir)
    path_digest = _text_hash(str(rel))[:10]
    archive_parent = (
        vault_dir
        / "99-Archive"
        / "DuplicateBasenames"
        / archive_date
        / rel.parent
    )
    candidate = archive_parent / f"{duplicate_path.stem}__duplicate-{path_digest}{duplicate_path.suffix}"
    if not candidate.exists():
        return candidate

    counter = 2
    while True:
        next_candidate = archive_parent / (
            f"{duplicate_path.stem}__duplicate-{path_digest}-{counter}{duplicate_path.suffix}"
        )
        if not next_candidate.exists():
            return next_candidate
        counter += 1


def repair_identical_duplicate_basenames(
    vault_dir: str | Path,
    *,
    apply: bool = False,
    limit: Optional[int] = None,
    archive_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Archive redundant files only when same-basename files have identical content.

    This is intentionally conservative: different-content same-name pages are
    left in place for delta merge, dispute routing, or manual review.
    """
    root = Path(vault_dir).expanduser()
    md_files = _markdown_files(root)
    by_name: Dict[str, List[Path]] = defaultdict(list)
    for path in md_files:
        by_name[path.stem].append(path)

    archive_date = archive_date or date.today().isoformat()
    moves: List[Dict[str, Any]] = []
    for name, paths in sorted(by_name.items()):
        if len(paths) <= 1:
            continue

        by_hash: Dict[str, List[Path]] = defaultdict(list)
        for path in paths:
            by_hash[_hash(path)].append(path)

        for digest, same_content_paths in sorted(by_hash.items()):
            if len(same_content_paths) <= 1:
                continue
            sorted_paths = sorted(same_content_paths, key=lambda p: _keep_rank(root, p))
            keep = sorted_paths[0]
            for duplicate in sorted_paths[1:]:
                archive_path = _duplicate_archive_path(root, duplicate, archive_date)
                moves.append(
                    {
                        "name": name,
                        "content_sha256": digest,
                        "keep_path": _relative(keep, root),
                        "duplicate_path": _relative(duplicate, root),
                        "archive_path": _relative(archive_path, root),
                    }
                )
                if limit is not None and len(moves) >= limit:
                    break
            if limit is not None and len(moves) >= limit:
                break
        if limit is not None and len(moves) >= limit:
            break

    moved = 0
    if apply:
        for item in moves:
            src = root / item["duplicate_path"]
            dst = root / item["archive_path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved += 1

    return {
        "status": "applied" if apply else "dry_run",
        "vault_dir": str(root),
        "planned_moves": len(moves),
        "moved": moved,
        "moves": moves,
    }


def _casefold_groups(vault_dir: Path, md_files: Iterable[Path]) -> List[Dict[str, Any]]:
    by_name: Dict[str, List[Path]] = defaultdict(list)
    for path in md_files:
        by_name[path.stem.casefold()].append(path)
    groups = []
    for folded, paths in sorted(by_name.items()):
        variants = sorted({path.stem for path in paths})
        if len(variants) <= 1:
            continue
        groups.append(
            {
                "name": folded,
                "count": len(paths),
                "variants": variants,
                "paths": [_relative(path, vault_dir) for path in paths],
            }
        )
    return groups


def _kg_entity_collisions(vault_dir: Path, md_files: Iterable[Path]) -> List[Dict[str, Any]]:
    kg_dir = vault_dir / "L2.4-KG" / "Entities"
    kg_entities = {path.stem: path for path in kg_dir.glob("*.md")} if kg_dir.exists() else {}
    collisions: List[Dict[str, Any]] = []
    for path in md_files:
        rel = path.relative_to(vault_dir)
        if not rel.parts or rel.parts[0] not in FORMAL_DIRS:
            continue
        kg_path = kg_entities.get(path.stem)
        if kg_path is None:
            continue
        collisions.append(
            {
                "name": path.stem,
                "formal_path": _relative(path, vault_dir),
                "kg_entity_path": _relative(kg_path, vault_dir),
            }
        )
    return collisions


def audit_vault_placement(vault_dir: str | Path) -> Dict[str, Any]:
    """Return a JSON-serializable placement audit for a Mnemos Obsidian vault."""
    root = Path(vault_dir).expanduser()
    md_files = _markdown_files(root)
    duplicates = _duplicate_groups(root, md_files)
    casefold = _casefold_groups(root, md_files)
    kg_collisions = _kg_entity_collisions(root, md_files)
    identical_groups = [group for group in duplicates if group["identical_content"]]

    return {
        "vault_dir": str(root),
        "markdown_files": len(md_files),
        "folders": _folder_stats(root, md_files),
        "duplicate_basename_groups": len(duplicates),
        "duplicate_basename_files": sum(group["count"] for group in duplicates),
        "duplicate_basenames": duplicates,
        "identical_content_duplicate_groups": len(identical_groups),
        "casefold_duplicate_groups": len(casefold),
        "casefold_duplicates": casefold,
        "kg_entity_collision_count": len(kg_collisions),
        "kg_entity_collisions": kg_collisions,
    }


def format_placement_audit(report: Dict[str, Any], sample_limit: int = 20) -> str:
    """Render a compact human-readable placement audit."""
    lines = [
        "Vault placement audit",
        "=" * 40,
        f"vault_dir: {report['vault_dir']}",
        f"markdown_files: {report['markdown_files']}",
        f"duplicate_basename_groups: {report['duplicate_basename_groups']}",
        f"duplicate_basename_files: {report['duplicate_basename_files']}",
        f"identical_content_duplicate_groups: {report['identical_content_duplicate_groups']}",
        f"casefold_duplicate_groups: {report['casefold_duplicate_groups']}",
        f"kg_entity_collision_count: {report['kg_entity_collision_count']}",
    ]

    duplicates = report.get("duplicate_basenames", [])[:sample_limit]
    if duplicates:
        lines.extend(["", "Top duplicate basenames:"])
        for group in duplicates:
            lines.append(f"- [{group['count']}] {group['name']}")
            for path in group["paths"][:5]:
                lines.append(f"  - {path}")

    collisions = report.get("kg_entity_collisions", [])[:sample_limit]
    if collisions:
        lines.extend(["", "KG entity collisions:"])
        for item in collisions:
            lines.append(f"- {item['name']}: {item['formal_path']} <-> {item['kg_entity_path']}")

    return "\n".join(lines)


def format_placement_repair(report: Dict[str, Any], sample_limit: int = 20) -> str:
    """Render a compact human-readable placement repair report."""
    title = (
        "Vault placement repair applied"
        if report.get("status") == "applied"
        else "Vault placement repair dry-run"
    )
    lines = [
        title,
        "=" * 40,
        f"vault_dir: {report['vault_dir']}",
        f"planned_moves: {report['planned_moves']}",
        f"moved: {report['moved']}",
    ]
    moves = report.get("moves", [])[:sample_limit]
    if moves:
        lines.extend(["", "Moves:"])
        for item in moves:
            lines.append(f"- {item['duplicate_path']} -> {item['archive_path']}")
            lines.append(f"  keep: {item['keep_path']}")
    return "\n".join(lines)
