#!/usr/bin/env python3
"""Repair Wiki quality debt without dropping source content or weakening budgets."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.frontmatter import DISPLAY_TO_CANONICAL  # noqa: E402
from core.utils import atomic_write_text  # noqa: E402
from core.wiki_navigation import (  # noqa: E402,F401
    NAV_MARKER,
    PAGE_NAV_MARKER,
    rebuild_navigation,
)
from core.vaults.link_audit import (  # noqa: E402
    repair_broken_wikilinks,
    repair_vault_absolute_wikilinks,
)
from scripts import wiki_lint  # noqa: E402


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iter_pages(vault_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in vault_dir.rglob("*.md")
        if not any(part.startswith(".") for part in path.relative_to(vault_dir).parts)
    )


def _backup_pages(vault_dir: Path, backup_dir: Path) -> dict[str, Any]:
    manifest_path = backup_dir / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        return {
            "backup_dir": str(backup_dir),
            "page_count": int(existing.get("page_count", 0)),
            "reused": True,
        }
    destination = backup_dir / "vault"
    manifest: list[dict[str, Any]] = []
    for page in _iter_pages(vault_dir):
        rel = page.relative_to(vault_dir)
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(page, target)
        manifest.append(
            {"path": rel.as_posix(), "sha256": _hash_file(page), "size_bytes": page.stat().st_size}
        )
    payload = {
        "schema_version": "mnemos.wiki_quality_backup.v1",
        "vault_dir": str(vault_dir),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "page_count": len(manifest),
        "pages": manifest,
    }
    atomic_write_text(
        backup_dir / "manifest.json",
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"backup_dir": str(backup_dir), "page_count": len(manifest)}


def _remove_stub_padding_artifacts(vault_dir: Path) -> int:
    """Remove obsolete boilerplate that previously masked short-page debt."""

    removed = 0
    home = (vault_dir / "00-Mnemos-Home.md").resolve(strict=False)
    for page in _iter_pages(vault_dir):
        if page.resolve(strict=False) == home:
            continue
        text = page.read_text(encoding="utf-8")
        if PAGE_NAV_MARKER not in text:
            continue
        atomic_write_text(
            page,
            text.split(PAGE_NAV_MARKER, 1)[0].rstrip() + "\n",
            encoding="utf-8",
        )
        removed += 1
    return removed


def _quality_state(vault_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from core.vaults.link_audit import (
        build_vault_target_aliases,
        build_vault_target_index,
        canonical_wiki_target_key,
    )

    wiki_lint.WIKI_DIR = vault_dir
    pages = wiki_lint.scan_all_pages()
    page_index = wiki_lint.build_page_index(pages)
    target_aliases = build_vault_target_aliases(vault_dir)
    target_index = build_vault_target_index(vault_dir)
    incoming: set[str] = set()
    for page in pages:
        for link in page["links"]:
            targets = target_index.get(canonical_wiki_target_key(link), ())
            if len(targets) == 1:
                incoming.add(targets[0])
    results = [
        wiki_lint.lint_page(
            page,
            pages,
            page_index,
            wiki_lint.STALE_DAYS,
            wiki_lint.STUB_THRESHOLD,
            target_aliases,
            incoming,
        )
        for page in pages
    ]
    report = wiki_lint.build_quality_report(results, vault_dir=vault_dir)
    return results, report


def _repair_metadata(vault_dir: Path) -> dict[str, Any]:
    wiki_lint.WIKI_DIR = vault_dir
    pages = wiki_lint.scan_all_pages()
    page_index = wiki_lint.build_page_index(pages)
    results, _report = _quality_state(vault_dir)
    return wiki_lint.auto_fix(results, pages, page_index, log=None)


def _restore_producer_metadata(vault_dir: Path, backup_dir: Path) -> dict[str, int]:
    """Restore producer-owned fields erased by an older strict round trip."""

    backup_root = backup_dir / "vault"
    restored_pages = 0
    restored_fields = 0
    for page in _iter_pages(vault_dir):
        backup_page = backup_root / page.relative_to(vault_dir)
        if not backup_page.is_file():
            continue
        current = page.read_text(encoding="utf-8")
        backup = backup_page.read_text(encoding="utf-8")
        current_fm, _current_body = wiki_lint.extract_frontmatter(current)
        backup_fm, _backup_body = wiki_lint.extract_frontmatter(backup)
        if current_fm is None or backup_fm is None:
            continue
        additions = {
            key: value
            for key, value in backup_fm.items()
            if str(key) not in DISPLAY_TO_CANONICAL and key not in current_fm
        }
        if not additions:
            continue
        current_fm.update(additions)
        match = wiki_lint.FRONTMATTER_RE.match(current)
        if match is None:
            continue
        import yaml

        normalized = wiki_lint.to_chinese_frontmatter_preserving_unknown(current_fm)
        frontmatter = yaml.safe_dump(
            normalized, allow_unicode=True, sort_keys=False
        ).strip()
        atomic_write_text(
            page,
            f"---\n{frontmatter}\n---\n" + current[match.end() :],
            encoding="utf-8",
        )
        restored_pages += 1
        restored_fields += len(additions)
    return {"restored_pages": restored_pages, "restored_fields": restored_fields}


def _repair_source_provenance(vault_dir: Path, backup_dir: Path) -> dict[str, Any]:
    """Replace ambiguous zero-source metadata with exact pre-migration snapshot refs."""

    manifest_path = backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hashes = {str(item["path"]): str(item["sha256"]) for item in manifest.get("pages", [])}
    repaired = 0
    missing_backup = 0
    for page in _iter_pages(vault_dir):
        rel = page.relative_to(vault_dir).as_posix()
        content = page.read_text(encoding="utf-8")
        fm, _body = wiki_lint.extract_frontmatter(content)
        if fm is None:
            continue
        source_count = wiki_lint.fm_get(fm, "source_count", 0)
        sources = fm.get("sources", fm.get("来源", []))
        if int(source_count or 0) != 0 or sources:
            continue
        digest = hashes.get(rel)
        if not digest:
            missing_backup += 1
            continue
        fm["来源"] = [f"wiki-backup-sha256:{digest}"]
        fm["来源数量"] = 1
        fm["来源状态"] = "legacy_snapshot_preserved"
        match = wiki_lint.FRONTMATTER_RE.match(content)
        if match is None:
            continue
        import yaml

        normalized = wiki_lint.to_chinese_frontmatter_preserving_unknown(fm)
        new_frontmatter = yaml.safe_dump(
            normalized, allow_unicode=True, sort_keys=False
        ).strip()
        new_content = f"---\n{new_frontmatter}\n---\n" + content[match.end() :]
        atomic_write_text(page, new_content, encoding="utf-8")
        repaired += 1
    return {"repaired_zero_source_pages": repaired, "missing_backup_refs": missing_backup}


def reconcile(vault_dir: Path, backup_dir: Path, *, apply: bool) -> dict[str, Any]:
    """Back up and repair objective Wiki metadata, links, and navigation debt."""

    before_results, before = _quality_state(vault_dir)
    payload: dict[str, Any] = {
        "schema_version": "mnemos.wiki_quality_reconcile.v1",
        "applied": apply,
        "vault_dir": str(vault_dir),
        "before": before["summary"],
        "before_budgets": before["budgets"],
        "planned": {
            "metadata_pages": sum(
                1
                for result in before_results
                if any(issue["type"] == "missing_meta" for issue in result["issues"])
            ),
            "stub_pages": int(before["summary"]["issue_counts"].get("stub", 0)),
        },
    }
    if not apply:
        payload["link_repair"] = repair_broken_wikilinks(vault_dir).to_dict()
        return payload

    payload["backup"] = _backup_pages(vault_dir, backup_dir)
    payload["removed_stub_padding_artifacts"] = _remove_stub_padding_artifacts(vault_dir)
    payload["producer_metadata"] = _restore_producer_metadata(vault_dir, backup_dir)
    payload["metadata"] = _repair_metadata(vault_dir)
    payload["source_provenance"] = _repair_source_provenance(vault_dir, backup_dir)
    payload["absolute_links"] = repair_vault_absolute_wikilinks(
        vault_dir, apply=True
    ).to_dict()
    payload["broken_links"] = repair_broken_wikilinks(vault_dir, apply=True).to_dict()
    navigation = rebuild_navigation(vault_dir)
    payload["navigation"] = {
        key: value for key, value in navigation.items() if key != "page_to_nav"
    }
    _after_results, after = _quality_state(vault_dir)
    payload["after"] = after["summary"]
    payload["after_budgets"] = after["budgets"]
    payload["ok"] = bool(after["budgets"]["ok"])
    return payload


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for backed-up Wiki quality reconciliation."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-dir", default="")
    parser.add_argument("--backup-dir", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    cfg = get_config()
    vault_dir = Path(args.vault_dir).expanduser() if args.vault_dir else Path(cfg.wiki_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = (
        Path(args.backup_dir).expanduser()
        if args.backup_dir
        else Path(cfg.database_dir) / "backups" / f"root007-wiki-quality-{stamp}"
    )
    payload = reconcile(vault_dir, backup_dir, apply=args.apply)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            "Wiki quality reconciliation: "
            f"applied={payload['applied']} before={payload['before']} after={payload.get('after')}"
        )
    return 0 if (not args.apply or payload.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
