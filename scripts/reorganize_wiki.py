#!/usr/bin/env python3
"""
一次性存量 Wiki 目录整理脚本。

根据 core/kia/charon.py 的 PageFolderResolver 把 01-People/02-Projects/03-Tech/
04-Concepts/06-Retrospectives 根目录下的页面移动到二级子目录，并同步：
- 更新 07-Shadow/ 中对应 shadow 文件的 shadow_for 路径
- 更新 knowledge_graph.db/entities.source_page
- 删除并重建 L2.4-KG/ 投影

用法：
    .venv/bin/python scripts/reorganize_wiki.py --dry-run
    .venv/bin/python scripts/reorganize_wiki.py --yes
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import get_config
from core.frontmatter import parse_frontmatter, write_frontmatter, fm_get
from core.kia.charon import (
    resolve_page_folder,
    EntityExtractor,
    WIKI_DIR,
    find_formal_basename_collisions,
)
from core.vaults.vault_sync import sync_kg_projection

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

SCAN_DIRS = ["01-People", "02-Projects", "03-Tech", "04-Concepts", "06-Retrospectives"]
SYSTEM_DIRS = {".obsidian", ".kg", "L2.4-KG", "07-Shadow", "99-Reports", "00-Inbox"}


def _should_scan(root: Path, md_file: Path) -> bool:
    rel = md_file.relative_to(root)
    # 只处理这些目录根目录下的 .md，不处理子目录里的文件
    if len(rel.parts) != 1:
        return False
    # 跳过系统生成的影子文件/报告
    if md_file.name.endswith(".shadow.md"):
        return False
    return True


def _find_moves(wiki_dir: Path, scan_dirs: List[str]) -> List[Tuple[Path, Path]]:
    """扫描需要移动的页面，返回 (旧路径, 目标目录) 列表。"""
    extractor = EntityExtractor(wiki_base=wiki_dir, bootstrap_from_existing=False)
    moves: List[Tuple[Path, Path]] = []

    for dir_name in scan_dirs:
        root = wiki_dir / dir_name
        if not root.exists():
            continue
        for md_file in root.glob("*.md"):
            if not _should_scan(root, md_file):
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
                fm, _ = parse_frontmatter(text)
            except (OSError, ValueError):
                fm = {}
            entities = extractor.extract(text)
            target_dir = resolve_page_folder(md_file, fm, entities)
            if target_dir is None:
                logger.info("SKIP (无法分类): %s", md_file.relative_to(wiki_dir))
                continue
            current_dir = md_file.parent
            if target_dir.resolve() == current_dir.resolve():
                continue
            moves.append((md_file, target_dir))

    return moves


def _update_shadow_files(wiki_dir: Path, old_path: Path, new_path: Path) -> int:
    """更新 07-Shadow/ 中 shadow_for 指向旧路径的文件。"""
    shadow_dir = wiki_dir / "07-Shadow"
    if not shadow_dir.exists():
        return 0
    old_abs = str(old_path)
    new_abs = str(new_path)
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
            # shadow_for 可能是相对路径或绝对路径；统一比较去除 .md 后的值
            sf_clean = str(wiki_dir / shadow_for).removesuffix(".md")
            if sf_clean == old_abs.removesuffix(".md"):
                fm["shadow_for"] = new_abs
                shadow_file.write_text(
                    write_frontmatter(fm, body),
                    encoding="utf-8",
                )
                updated += 1
        except (OSError, ValueError):
            logger.warning("更新 shadow 失败: %s", shadow_file, exc_info=True)
    return updated


def _update_kg_source_pages(db_path: Path, old_path: Path, new_path: Path) -> int:
    """把 entities.source_page 从旧路径更新为新路径。"""
    if not db_path.exists():
        return 0
    updated = 0
    old_abs = str(old_path)
    new_abs = str(new_path)
    try:
        with sqlite3.connect(str(db_path), timeout=10) as conn:
            cur = conn.execute(
                "UPDATE entities SET source_page = ? WHERE source_page = ?",
                (new_abs, old_abs),
            )
            updated = cur.rowcount
            conn.commit()
    except sqlite3.Error:
        logger.warning("更新 KG source_page 失败: %s -> %s", old_abs, new_abs, exc_info=True)
    return updated


def _execute_moves(wiki_dir: Path, moves: List[Tuple[Path, Path]]) -> Dict[str, int]:
    """执行移动并同步 shadow/KG。"""
    cfg = get_config()
    kg_db = Path(cfg.database_dir) / "knowledge_graph.db"
    stats = {"moved": 0, "shadows": 0, "kg_rows": 0, "duplicate_basename": 0}

    for old_path, target_dir in moves:
        target_dir.mkdir(parents=True, exist_ok=True)
        new_path = target_dir / old_path.name
        collisions = find_formal_basename_collisions(old_path)
        if collisions and new_path != old_path:
            stats["duplicate_basename"] += 1
            logger.warning(
                "同名页面已存在，跳过移动: %s -> %s; existing=%s",
                old_path.relative_to(wiki_dir),
                new_path.relative_to(wiki_dir),
                [str(path.relative_to(wiki_dir)) for path in collisions],
            )
            continue

        # 处理目标冲突
        if new_path.exists():
            stem = new_path.stem
            suffix = new_path.suffix
            for i in range(1, 1000):
                candidate = target_dir / f"{stem}_{i}{suffix}"
                if not candidate.exists():
                    new_path = candidate
                    break
            else:
                logger.error("目标冲突无法解决: %s", new_path)
                continue

        old_rel = str(old_path.relative_to(wiki_dir))
        new_rel = str(new_path.relative_to(wiki_dir))

        try:
            old_path.rename(new_path)
        except OSError:
            logger.error("移动失败: %s -> %s", old_path, new_path, exc_info=True)
            continue

        stats["moved"] += 1
        stats["shadows"] += _update_shadow_files(wiki_dir, old_path, new_path)
        stats["kg_rows"] += _update_kg_source_pages(kg_db, old_path, new_path)
        logger.info("MOVED: %s -> %s", old_rel, new_rel)

    return stats


def _rebuild_kg_projection(wiki_dir: Path) -> None:
    """删除旧 L2.4-KG 投影并重建。"""
    kg_dir = wiki_dir / "L2.4-KG"
    if kg_dir.exists():
        for sub in ["Entities", "Relations", "MOCs"]:
            subdir = kg_dir / sub
            if subdir.exists():
                for f in subdir.glob("*.md"):
                    f.unlink()
        logger.info("已清理旧 L2.4-KG 投影文件")
    result = sync_kg_projection(wiki_dir)
    logger.info("L2.4-KG 重建: %s", result)


def main() -> int:
    parser = argparse.ArgumentParser(description="整理 Wiki 目录到二级分类")
    parser.add_argument("--dry-run", action="store_true", help="只输出建议移动清单")
    parser.add_argument("--yes", action="store_true", help="确认执行移动")
    parser.add_argument("--dirs", nargs="+", default=SCAN_DIRS, help="要扫描的顶层目录")
    args = parser.parse_args()

    scan_dirs = args.dirs or SCAN_DIRS

    wiki_dir = Path(str(WIKI_DIR))
    if not wiki_dir.exists():
        logger.error("Wiki 目录不存在: %s", wiki_dir)
        return 1

    moves = _find_moves(wiki_dir, scan_dirs)

    if not moves:
        print("没有需要整理的页面。")
        return 0

    print(f"\n发现 {len(moves)} 个页面需要整理：\n")
    for old_path, target_dir in moves:
        print(f"  {old_path.relative_to(wiki_dir)}")
        print(f"    -> {target_dir.relative_to(wiki_dir)}\n")

    if args.dry_run:
        print("(--dry-run 模式，未执行移动)")
        return 0

    if not args.yes:
        print("请使用 --yes 确认执行，或使用 --dry-run 预览。")
        return 1

    stats = _execute_moves(wiki_dir, moves)
    _rebuild_kg_projection(wiki_dir)

    print(f"\n整理完成: 移动 {stats['moved']} 个页面, 更新 {stats['shadows']} 个 shadow, "
          f"更新 {stats['kg_rows']} 条 KG source_page")
    return 0


if __name__ == "__main__":
    sys.exit(main())
