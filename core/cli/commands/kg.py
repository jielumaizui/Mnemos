# -*- coding: utf-8 -*-
"""
mnemos kg — 知识图谱运维命令

- doctor: 诊断 KG/CG 数据库健康状态
- rebuild-entities: 扫描全 Wiki 重建 entities 表
- build-graph: 扫描 00-Inbox 重建 Wiki 关系图
- export-dataview: 导出可复制到 Obsidian Dataview 的查询块
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from core.config import get_config
from core.db_utils import validate_sql_identifier


def _count_table(db_path: Path, table: str) -> int:
    if not db_path.exists():
        return -1
    try:
        with sqlite3.connect(str(db_path), timeout=5) as conn:
            query = " ".join([
                "SELECT COUNT(*) FROM",
                validate_sql_identifier(table),
            ])
            row = conn.execute(query).fetchone()
            return row[0] if row else 0
    except sqlite3.Error:
        return -1


def _table_exists(db_path: Path, table: str) -> bool:
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(str(db_path), timeout=5) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            return row is not None
    except sqlite3.Error:
        return False


def _relation_projection_status(cfg, relation_count: int) -> tuple[str, int, int]:
    """Return (status, file_count, max_files) for L2.4-KG/Relations projection."""
    max_files = int(cfg.get("knowledge_graph.projection_max_relations", 200) or 200)
    projection_dir = Path(cfg.wiki_dir) / "L2.4-KG" / "Relations"
    file_count = len(list(projection_dir.glob("*.md"))) if projection_dir.exists() else 0
    if relation_count <= 0:
        return "empty-db", file_count, max_files
    if file_count == 0:
        return "missing", file_count, max_files
    if file_count > max_files:
        return "over-cap", file_count, max_files
    return "ok", file_count, max_files


def cmd_kg_doctor(args) -> int:
    """运行知识图谱健康检查。"""
    cfg = get_config()
    kg_db = Path(cfg.database_dir) / "knowledge_graph.db"
    cg_db = Path(getattr(cfg, "cognitive_graph_db_path", cfg.database_dir / "cognitive_graph.db"))

    print("知识图谱健康检查")
    print("=" * 40)

    # KG 检查
    print(f"\nKG 数据库: {kg_db}")
    if not kg_db.exists():
        print("  ✗ 数据库不存在")
        print("  建议: 运行 mnemos init")
    else:
        relation_count = 0
        for table in ("relations", "entities"):
            exists = _table_exists(kg_db, table)
            count = _count_table(kg_db, table) if exists else -1
            if not exists:
                print(f"  ✗ 表 {table} 不存在")
            elif count == 0:
                print(f"  ⚠ 表 {table}: 0 条记录")
            else:
                print(f"  ✓ 表 {table}: {count} 条记录")
            if table == "relations" and count > 0:
                relation_count = count

        from core.kia.kg_consistency import audit_kg_consistency

        consistency = audit_kg_consistency(kg_db, wiki_base=cfg.wiki_dir)
        hard_errors = consistency.get("errors") or []
        if hard_errors:
            print("  ✗ KG 硬一致性异常:")
            for error in hard_errors[:10]:
                print(f"    - {error}")
            print("    建议: mnemos kg consistency --apply")
        else:
            print("  ✓ KG 硬一致性: ok")

        endpoint_gaps = consistency.get("endpoint_gaps") or {}
        if endpoint_gaps.get("count"):
            print(
                "  ⚠ 关系端点未映射到实体或现有 Wiki 文件: "
                f"{endpoint_gaps.get('count')} 个"
            )
            for name in endpoint_gaps.get("samples", [])[:10]:
                print(f"    - {name}")

        projection_status, projection_files, projection_cap = _relation_projection_status(
            cfg, relation_count
        )
        if projection_status == "missing":
            print(
                f"  ⚠ Relations 投影为空: DB 有 {relation_count} 条关系，"
                f"L2.4-KG/Relations 当前 0 个文件"
            )
            print("    建议: 触发一次 distill 或运行 mnemos vaults sync")
        elif projection_status == "over-cap":
            print(
                f"  ⚠ Relations 投影超过上限: {projection_files}/{projection_cap} 个文件"
            )
        elif projection_status == "ok":
            print(f"  ✓ Relations 投影: {projection_files}/{projection_cap} 个文件")

    # CG 检查
    print(f"\nCG 数据库: {cg_db}")
    if not cg_db.exists():
        print("  ✗ 数据库不存在")
    else:
        for table in ("cognitive_relations", "canonical_nodes"):
            exists = _table_exists(cg_db, table)
            count = _count_table(cg_db, table) if exists else -1
            if not exists:
                print(f"  ✗ 表 {table} 不存在")
            elif count == 0:
                print(f"  ⚠ 表 {table}: 0 条记录")
            else:
                print(f"  ✓ 表 {table}: {count} 条记录")

    print("\n修复建议:")
    print("  - entities 为空: mnemos kg rebuild-entities")
    print("  - canonical_nodes 为空: mnemos cognitive-graph reconcile")
    print("  - relations 为空: 触发一次 distill 或运行 charon connect")
    return 0


def cmd_kg_rebuild_entities(args) -> int:
    """扫描全 Wiki 重建 knowledge_graph.db/entities 表。"""
    cfg = get_config()
    wiki_dir = cfg.wiki_dir

    from core.kia.entity_manager import EntityManager

    em = EntityManager()
    count = 0
    for page in wiki_dir.rglob("*.md"):
        # 跳过系统目录与报告目录
        rel_parts = page.relative_to(wiki_dir).parts
        if any(part.startswith(".") or part in {"99-Reports", "07-Shadow"} for part in rel_parts):
            continue
        try:
            entities = em.ingest_from_wiki(page)
            count += len(entities)
        # DEBT(S8): 容错跳过，避免单条记录中断批量处理
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            continue

    print(f"已重建 entities 表，共写入/更新 {count} 个实体")
    return 0


def cmd_kg_consistency(args) -> int:
    """审计或修复 knowledge_graph.db 的硬一致性问题。"""
    cfg = get_config()
    kg_db = Path(cfg.database_dir) / "knowledge_graph.db"

    from core.kia.kg_consistency import emit_report, repair_kg_consistency

    apply = bool(getattr(args, "apply", False))
    payload = repair_kg_consistency(
        kg_db,
        apply=apply,
        wiki_base=cfg.wiki_dir,
        create_backup=apply and not bool(getattr(args, "no_backup", False)),
    )
    emit_report(payload, json_output=bool(getattr(args, "json", False)))
    if apply and payload.get("status") != "ok":
        return 1
    return 0


def cmd_kg_normalize_endpoints(args) -> int:
    """审计或修复 KG endpoint 语义归一化/路径迁移问题。"""
    cfg = get_config()
    kg_db = Path(cfg.database_dir) / "knowledge_graph.db"

    from core.kia.kg_endpoint_normalizer import emit_report, normalize_kg_endpoints

    apply = bool(getattr(args, "apply", False))
    payload = normalize_kg_endpoints(
        kg_db,
        wiki_base=cfg.wiki_dir,
        apply=apply,
        create_backup=apply and not bool(getattr(args, "no_backup", False)),
        min_concept_refs=int(getattr(args, "min_concept_refs", 2) or 2),
        prune_invalid=bool(getattr(args, "prune_invalid", False)),
    )
    emit_report(payload, json_output=bool(getattr(args, "json", False)))
    if apply and payload.get("status") != "ok":
        return 1
    return 0


def cmd_kg_build_graph(args) -> int:
    """扫描 Wiki 00-Inbox 并重建知识图谱关系。"""
    from core.kia.knowledge_graph import build_graph_for_wiki

    graph = build_graph_for_wiki(wiki_base=getattr(args, "wiki_base", None) or None)
    print(f"已构建 Wiki 知识图谱: {graph.wiki_base}")
    return 0


def cmd_kg_export_dataview(args) -> int:
    """导出某个页面的 Obsidian Dataview 查询块。"""
    cfg = get_config()
    kg_db = Path(cfg.database_dir) / "knowledge_graph.db"

    from core.kia.knowledge_graph import KnowledgeGraph

    graph = KnowledgeGraph(db_path=str(kg_db), wiki_base=str(cfg.wiki_dir))
    print(graph.export_dataview_query(args.page))
    return 0
