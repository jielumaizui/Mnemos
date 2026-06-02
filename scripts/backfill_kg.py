#!/usr/bin/env python3
"""
KG 批量回填脚本 — 一次性回填已有 Wiki 页面到知识图谱
"""
import sys
from pathlib import Path

# 将项目根目录加入路径
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from core.kia.knowledge_graph import KnowledgeGraph
from core.kia.entity_manager import EntityManager


def main():
    wiki_dir = Path.home() / "Documents" / "Obsidian Vault" / "wiki"
    if not wiki_dir.exists():
        print(f"Wiki 目录不存在: {wiki_dir}")
        sys.exit(1)

    kg = KnowledgeGraph()
    em = EntityManager()

    md_files = list(wiki_dir.rglob("*.md"))
    total = len(md_files)
    print(f"发现 {total} 个 Wiki 页面，开始回填...")

    relation_count = 0
    entity_count = 0

    # 传递全部页面列表，启用关键词重叠/反模式/标题包含等策略
    all_pages = md_files
    for idx, page in enumerate(md_files, 1):
        try:
            discovered = kg.discover_relations(page, existing_pages=all_pages)
            added = kg.apply_discovered(discovered, min_confidence=0.3)
            relation_count += added

            entities = em.ingest_from_wiki(page)
            entity_count += len(entities)

            if idx % 20 == 0 or idx == total:
                print(f"  进度: {idx}/{total} 页面, 关系+{relation_count}, 实体+{entity_count}")
        except Exception as e:
            print(f"  跳过 {page}: {e}")

    print(f"\n回填完成: 处理了 {total} 个页面")
    print(f"  新增关系: {relation_count}")
    print(f"  提取实体: {entity_count}")


if __name__ == "__main__":
    main()
