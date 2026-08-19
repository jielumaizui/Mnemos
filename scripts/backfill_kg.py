#!/usr/bin/env python3
"""
KG 批量回填脚本 — 增量回填已有 Wiki 页面到知识图谱

特性：
- 默认增量：根据 checkpoint 文件只处理新/修改页面
- 支持 --max-pages 限制一次性处理数量
- 预取候选页面列表，避免每页扫全库
- 定期写入 checkpoint，异常中断后可续跑
"""

import argparse
import sys
from pathlib import Path

# 将项目根目录加入路径
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from core.kia.knowledge_graph import KnowledgeGraph  # noqa: E402
from core.kia.entity_manager import EntityManager  # noqa: E402
from core.config import get_config  # noqa: E402


def _checkpoint_path(wiki_dir: Path) -> Path:
    return wiki_dir.parent / ".mnemos_backfill_kg_checkpoint"


def _load_checkpoint(wiki_dir: Path) -> float:
    cp = _checkpoint_path(wiki_dir)
    if not cp.exists():
        return 0.0
    try:
        return float(cp.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        # 检查点损坏或不存在时从 0 开始
        return 0.0


def _save_checkpoint(wiki_dir: Path, mtime: float) -> None:
    cp = _checkpoint_path(wiki_dir)
    cp.write_text(str(mtime), encoding="utf-8")


def _filter_pages(md_files: list[Path], since_mtime: float, max_pages: int | None) -> list[Path]:
    """保留 mtime > since_mtime 的页面，按 mtime 升序，限制数量。"""
    filtered = []
    for page in md_files:
        try:
            mtime = page.stat().st_mtime
        except OSError:
            continue
        if mtime > since_mtime:
            filtered.append((mtime, page))
    filtered.sort(key=lambda x: x[0])
    if max_pages is not None:
        filtered = filtered[:max_pages]
    return [p for _, p in filtered]


def main():
    parser = argparse.ArgumentParser(description="KG 批量回填脚本")
    parser.add_argument("--full", action="store_true", help="忽略 checkpoint，全量回填")
    parser.add_argument("--max-pages", type=int, default=None, help="最多处理 N 个页面")
    args = parser.parse_args()

    wiki_dir = Path(get_config().wiki_dir)
    if not wiki_dir.exists():
        print(f"Wiki 目录不存在: {wiki_dir}")
        sys.exit(1)

    kg = KnowledgeGraph()
    em = EntityManager()

    skip_dirs = {
        "99-Archive",
        "99-Reports",
        "L2.4-KG",
        "L3-Observations",
        "L4-Reflections",
        "L5-Feedback",
    }
    md_files = [
        p
        for p in wiki_dir.rglob("*.md")
        if not any(
            part.startswith(".") or part in skip_dirs
            for part in p.relative_to(wiki_dir).parts
        )
    ]

    since_mtime = 0.0 if args.full else _load_checkpoint(wiki_dir)
    pages_to_process = _filter_pages(md_files, since_mtime, args.max_pages)
    total = len(pages_to_process)

    print(f"发现 {len(md_files)} 个 Wiki 页面，本次处理 {total} 个（checkpoint_mtime={since_mtime:.0f}）")

    if total == 0:
        print("没有需要回填的页面")
        return

    # 预取候选现有页面，避免每页扫全库
    existing_pages = kg._candidate_existing_pages()

    relation_count = 0
    entity_count = 0
    max_mtime = since_mtime
    checkpoint_interval = max(1, total // 10)

    for idx, page in enumerate(pages_to_process, 1):
        try:
            discovered = kg.discover_relations(page, existing_pages=existing_pages)
            added = kg.apply_discovered(discovered, min_confidence=0.7)
            relation_count += added

            entities = em.ingest_from_wiki(page)
            entity_count += len(entities)

            page_mtime = page.stat().st_mtime
            if page_mtime > max_mtime:
                max_mtime = page_mtime

            if idx % checkpoint_interval == 0 or idx == total:
                _save_checkpoint(wiki_dir, max_mtime)
                print(
                    f"  进度: {idx}/{total} 页面, "
                    f"关系+{relation_count}, 实体+{entity_count}"
                )
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
            print(f"  跳过 {page}: {e}")

    # 最终 checkpoint
    _save_checkpoint(wiki_dir, max_mtime)

    print(f"\n回填完成: 处理了 {total} 个页面")
    print(f"  新增关系: {relation_count}")
    print(f"  提取实体: {entity_count}")


if __name__ == "__main__":
    main()
