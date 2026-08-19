"""Build-relation-index command for Mnemos CLI."""

import logging

from core.cli.helpers import _get_config

logger = logging.getLogger(__name__)


def cmd_build_relation_index(args):
    """重建关联上下文向量索引"""
    try:
        from core.kia.knowledge_graph import KnowledgeGraph
        from core.embeddings.relation_manager import RelationEmbeddingManager

        config = _get_config()
        wiki_dir = config.wiki_dir
        db_path = getattr(config, "database_dir", config.data_dir) / "knowledge_graph.db"

        print("重建关联上下文向量索引...")
        kg = KnowledgeGraph(db_path=str(db_path), wiki_base=str(wiki_dir))

        # 先清理旧索引
        rel_mgr = RelationEmbeddingManager(db_path=db_path)
        stats_old = rel_mgr.get_stats()
        print(f"  当前索引: {stats_old['total_relations']} 个 embedding")

        # 批量重建
        result = kg.rebuild_relation_index(batch_size=50)
        print(f"  处理完成: {result['total']} 个关系")
        print(f"  成功更新: {result['updated']} 个")
        if result["failed"] > 0:
            print(f"  失败: {result['failed']} 个")
        if result["skipped"] > 0:
            print(f"  跳过: {result['skipped']} 个")

        stats_new = rel_mgr.get_stats()
        print(f"  重建后索引: {stats_new['total_relations']} 个 embedding")
    except (ImportError, AttributeError, OSError) as e:
        print(f"重建失败: {e}")
