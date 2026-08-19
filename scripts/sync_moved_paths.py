#!/usr/bin/env python3
"""
修复已执行移动的 Wiki 页面在 KG source_page 和 shadow_for 中的绝对路径引用。

逻辑：
- 遍历 knowledge_graph.db 的 entities.source_page，若文件已不存在但 vault 中仍有同名 .md，
  则更新为新的绝对路径。
- 遍历 07-Shadow/*.shadow.md 的 shadow_for，同样进行失效路径修复。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from core.config import get_config
from core.frontmatter import fm_get, parse_frontmatter, write_frontmatter


EXCLUDED = {".obsidian", ".kg", "L2.4-KG", "07-Shadow"}


def _build_name_index(wiki_dir: Path) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for path in wiki_dir.rglob("*.md"):
        rel = path.relative_to(wiki_dir)
        if any(part in EXCLUDED for part in rel.parts):
            continue
        name = path.name
        if name in index:
            # 保留非 00-Inbox 路径，若已有则跳过（由调用方处理歧义）
            if "00-Inbox" in str(index[name]):
                index[name] = path
        else:
            index[name] = path
    return index


def _update_kg_source_pages(db_path: Path, index: dict[str, Path]) -> int:
    updated = 0
    with sqlite3.connect(str(db_path), timeout=10) as conn:
        rows = conn.execute("SELECT uid, source_page FROM entities WHERE source_page != ''").fetchall()
        for uid, old in rows:
            old_path = Path(old)
            if old_path.exists():
                continue
            new_path = index.get(old_path.name)
            if new_path is None:
                continue
            conn.execute("UPDATE entities SET source_page = ? WHERE uid = ?", (str(new_path), uid))
            updated += 1
        conn.commit()
    return updated


def _update_shadow_files(wiki_dir: Path, index: dict[str, Path]) -> int:
    shadow_dir = wiki_dir / "07-Shadow"
    if not shadow_dir.exists():
        return 0
    updated = 0
    for shadow_file in shadow_dir.glob("*.shadow.md"):
        try:
            text = shadow_file.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(text)
            if not fm:
                continue
            shadow_for = fm_get(fm, "shadow_for") or ""
            if not shadow_for:
                continue
            sf_path = Path(shadow_for)
            if sf_path.exists():
                continue
            new_path = index.get(sf_path.name)
            if new_path is None:
                continue
            fm["shadow_for"] = str(new_path)
            shadow_file.write_text(write_frontmatter(fm, body), encoding="utf-8")
            updated += 1
        except (OSError, ValueError):
            continue
    return updated


def main() -> int:
    cfg = get_config()
    wiki_dir = Path(cfg.wiki_dir)
    db_path = Path(cfg.database_dir) / "knowledge_graph.db"
    index = _build_name_index(wiki_dir)
    print(f"建立 vault 文件名索引: {len(index)} 个")
    kg_updated = _update_kg_source_pages(db_path, index)
    shadow_updated = _update_shadow_files(wiki_dir, index)
    print(f"更新 KG source_page: {kg_updated} 条")
    print(f"更新 shadow_for: {shadow_updated} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
