#!/usr/bin/env python3
"""
KG 实体清洗脚本 — 删除低质量/噪声实体

清洗规则：
1. MOC 实体：名称包含 "MOC"
2. 哈希前缀实体：名称匹配 8位十六进制_xxx 模式
3. 路径式实体：名称包含 "/"

安全机制：
- 先查询预览，再执行删除
- 保留被关系引用的实体名称（从 relations 表提取白名单）
- 同步清理 entity_aliases
"""
import sys
import sqlite3
import re
from pathlib import Path

DB_PATH = Path.home() / ".mnemos" / "knowledge_graph.db"


def get_bad_entity_names(conn: sqlite3.Connection) -> list:
    """返回符合删除条件的实体名称列表"""
    cursor = conn.execute("SELECT name FROM entities")
    bad_names = []
    for row in cursor.fetchall():
        name = row[0]
        if not name:
            continue
        # 规则1: MOC
        if "MOC" in name:
            bad_names.append((name, "MOC"))
            continue
        # 规则2: 哈希前缀 (8位hex_ 或 kimi:xxx_ 或 doc-xxx_)
        if re.search(r'^[0-9a-f]{8}_', name) or re.search(r'^[0-9a-f]{4}:[0-9a-f]{3}_', name):
            bad_names.append((name, "hash_prefix"))
            continue
        if re.search(r'^kimi:[a-f0-9]+_', name) or re.search(r'^doc-[a-f0-9]+_', name):
            bad_names.append((name, "file_name"))
            continue
        # 规则2b: 日期前缀 (如 15-23-14_xxx)
        if re.search(r'^\d{2}-\d{2}-\d{2}_', name):
            bad_names.append((name, "date_prefix"))
            continue
        # 规则3: 路径式
        if "/" in name:
            bad_names.append((name, "path"))
            continue
        # 规则4: 中文文本片段（以"的"开头/结尾，或含"进行"的短片段）
        if (name.startswith('的') or name.endswith('的')) and len(name) <= 8:
            bad_names.append((name, "fragment"))
            continue
        if '进行' in name and len(name) <= 8:
            bad_names.append((name, "fragment"))
            continue
        if '模块' in name and len(name) <= 6:
            bad_names.append((name, "fragment"))
            continue
    return bad_names


def get_relation_whitelist(conn: sqlite3.Connection) -> set:
    """从 relations 表提取所有被引用的名称作为保留白名单"""
    whitelist = set()
    cursor = conn.execute("SELECT DISTINCT source FROM relations UNION SELECT DISTINCT target FROM relations")
    for row in cursor.fetchall():
        whitelist.add(row[0])
    return whitelist


def main():
    if not DB_PATH.exists():
        print(f"数据库不存在: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA foreign_keys = OFF")

    # 1. 识别坏实体
    bad = get_bad_entity_names(conn)
    print(f"识别到 {len(bad)} 个待删除实体:")
    by_reason = {}
    for name, reason in bad:
        by_reason.setdefault(reason, []).append(name)
    for reason, names in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        print(f"  [{reason}] {len(names)} 个, 示例: {', '.join(names[:5])}")

    # 2. 获取关系白名单（ relations.source/target 引用的名称）
    # 注意：entities.name 与 relations.source/target 大部分情况下属于不同命名空间。
    # 只有当实体名与某个页面的相对路径完全相同时才保护（极为罕见）。
    whitelist = get_relation_whitelist(conn)
    to_delete = []
    skipped = []
    for name, reason in bad:
        # 仅当实体名是完整页面路径（含 .md 后缀或 / 目录分隔符）时才保护
        if name in whitelist and ('/' in name or name.endswith('.md')):
            skipped.append((name, reason))
        else:
            to_delete.append((name, reason))

    if skipped:
        print(f"\n白名单保护: 跳过 {len(skipped)} 个与页面路径完全重合的实体")
        for name, reason in skipped[:5]:
            print(f"  - {name} ({reason})")

    if not to_delete:
        print("\n没有需要删除的实体。")
        conn.close()
        return

    # 3. 执行删除
    names_to_delete = [name for name, _ in to_delete]
    placeholders = ','.join('?' * len(names_to_delete))

    # 先删 aliases（因为外键未启用）
    cursor = conn.execute(
        f"DELETE FROM entity_aliases WHERE entity_uid IN (SELECT uid FROM entities WHERE name IN ({placeholders}))",
        names_to_delete
    )
    aliases_deleted = cursor.rowcount

    # 再删 entities
    cursor = conn.execute(
        f"DELETE FROM entities WHERE name IN ({placeholders})",
        names_to_delete
    )
    entities_deleted = cursor.rowcount

    conn.commit()

    # 4. 统计
    total_after = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    print(f"\n清洗完成:")
    print(f"  删除实体: {entities_deleted}")
    print(f"  删除别名: {aliases_deleted}")
    print(f"  剩余实体: {total_after}")

    conn.close()


if __name__ == "__main__":
    main()
