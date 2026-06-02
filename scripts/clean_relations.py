#!/usr/bin/env python3
"""
KG 关系清洗脚本 — 删除低质量/噪声关系

清洗规则：
1. 悬空引用（dangling references）：target 页面不存在
2. 通用关键词相似（generic similar_to）：因过于宽泛的关键词（technology/技术/concept/未分类）建立的关系
3. 高密度相似（dense similar_to）：每个 source 的 similar_to 出度超过 10 时，只保留 confidence 最高的前 8 个
4. 低置信度关系：confidence < 0.7（但 backfill 已用 0.7 过滤，此项为兜底）
"""
import sys
import sqlite3
import re
from pathlib import Path

DB_PATH = Path.home() / ".mnemos" / "knowledge_graph.db"
WIKI_DIR = Path.home() / "Documents" / "Obsidian Vault" / "wiki"

# 通用关键词黑名单（导致过度连接的宽泛标签）
GENERIC_KEYWORDS = {"technology", "技术", "tech", "concept", "概念", "未分类", "wiki", "obsidian"}


def page_exists(page_ref: str) -> bool:
    """检查页面引用是否对应实际存在的文件（递归搜索所有子目录）"""
    if not page_ref:
        return False
    # 准备搜索名称（有无 .md 后缀）
    names = [page_ref]
    if not page_ref.endswith('.md'):
        names.append(page_ref + '.md')

    # 递归搜索整个 wiki 目录
    for name in names:
        # 直接匹配文件名
        for p in WIKI_DIR.rglob(name):
            if p.is_file():
                return True
    return False


def main():
    if not DB_PATH.exists():
        print(f"数据库不存在: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))

    total_before = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    print(f"清洗前关系总数: {total_before}")

    deleted = 0

    # 1. 删除悬空引用（dangling references）
    cursor = conn.execute("SELECT id, source, target FROM relations WHERE relation_type='references'")
    dangling_ids = []
    for row in cursor.fetchall():
        rel_id, source, target = row
        if not page_exists(target):
            dangling_ids.append(rel_id)

    if dangling_ids:
        placeholders = ','.join('?' * len(dangling_ids))
        conn.execute(f"DELETE FROM relation_evidence WHERE relation_id IN ({placeholders})", dangling_ids)
        conn.execute(f"DELETE FROM relations_fts WHERE rowid IN ({placeholders})", dangling_ids)
        cursor = conn.execute(f"DELETE FROM relations WHERE id IN ({placeholders})", dangling_ids)
        deleted += cursor.rowcount
        print(f"  删除悬空引用: {cursor.rowcount}")

    # 2. 删除通用关键词相似关系
    cursor = conn.execute("""
        SELECT r.id, e.content FROM relations r
        JOIN relation_evidence e ON r.id = e.relation_id
        WHERE r.relation_type = 'similar_to'
    """)
    generic_ids = []
    for row in cursor.fetchall():
        rel_id, content = row
        if content and content.startswith("共同关键词:"):
            # 提取关键词列表
            kw_part = content.replace("共同关键词:", "").strip()
            kws = [k.strip().lower() for k in kw_part.split(",")]
            # 如果共同关键词全部属于通用词，则删除
            if all(kw in GENERIC_KEYWORDS for kw in kws):
                generic_ids.append(rel_id)
            # 如果共同关键词只有一个且是通用词，也删除
            elif len(kws) == 1 and kws[0] in GENERIC_KEYWORDS:
                generic_ids.append(rel_id)

    if generic_ids:
        placeholders = ','.join('?' * len(generic_ids))
        conn.execute(f"DELETE FROM relation_evidence WHERE relation_id IN ({placeholders})", generic_ids)
        conn.execute(f"DELETE FROM relations_fts WHERE rowid IN ({placeholders})", generic_ids)
        cursor = conn.execute(f"DELETE FROM relations WHERE id IN ({placeholders})", generic_ids)
        deleted += cursor.rowcount
        print(f"  删除通用关键词相似: {cursor.rowcount}")

    # 3. 高密度相似出度限制：每个 source 保留 confidence 最高的前 8 个
    cursor = conn.execute("""
        SELECT source, COUNT(*) as cnt FROM relations
        WHERE relation_type = 'similar_to'
        GROUP BY source HAVING cnt > 10
    """)
    excess_ids = []
    for row in cursor.fetchall():
        source, cnt = row
        # 获取该 source 的所有 similar_to，按 confidence 降序
        rels = conn.execute(
            "SELECT id, confidence FROM relations WHERE source=? AND relation_type='similar_to' ORDER BY confidence DESC",
            (source,)
        ).fetchall()
        # 保留前 8 个，删除剩余的
        if len(rels) > 8:
            for rel_id, _ in rels[8:]:
                excess_ids.append(rel_id)

    if excess_ids:
        placeholders = ','.join('?' * len(excess_ids))
        conn.execute(f"DELETE FROM relation_evidence WHERE relation_id IN ({placeholders})", excess_ids)
        conn.execute(f"DELETE FROM relations_fts WHERE rowid IN ({placeholders})", excess_ids)
        cursor = conn.execute(f"DELETE FROM relations WHERE id IN ({placeholders})", excess_ids)
        deleted += cursor.rowcount
        print(f"  删除高密度相似溢出: {cursor.rowcount}")

    conn.commit()

    # 4. 清理孤立的 relation_evidence 和 FTS 索引（兜底）
    conn.execute("""
        DELETE FROM relation_evidence WHERE relation_id NOT IN (SELECT id FROM relations)
    """)
    conn.execute("""
        DELETE FROM relations_fts WHERE rowid NOT IN (SELECT id FROM relations)
    """)
    conn.commit()

    total_after = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    print(f"\n清洗完成:")
    print(f"  删除关系: {deleted}")
    print(f"  剩余关系: {total_after}")

    # 统计
    print("\n关系类型分布:")
    cursor = conn.execute("SELECT relation_type, COUNT(*) FROM relations GROUP BY relation_type ORDER BY COUNT(*) DESC")
    for row in cursor.fetchall():
        print(f"  {row[0]}: {row[1]}")

    conn.close()


if __name__ == "__main__":
    main()
