# -*- coding: utf-8 -*-
"""
关联上下文向量管理器（ADR-019）

职责：
1. 为 KG 关系的 context 文本生成 embedding（bge-m3）
2. 维护 hnswlib 关联向量索引
3. 与 relation_context_embeddings 表同步

生命周期：
    关系建立/更新 → 生成 context embedding → 写入 SQLite + hnswlib
    关系删除 → 标记删除 hnswlib 向量 → 删除 SQLite 记录
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .siliconflow_client import SiliconFlowEmbeddingClient, get_embedding_client

logger = logging.getLogger(__name__)

HNSWLIB_AVAILABLE = False
try:
    import hnswlib
    HNSWLIB_AVAILABLE = True
except ImportError:
    logger.debug("[RelationEmbedding] hnswlib not installed")

# bge-m3 维度
DIM = 1024


class RelationEmbeddingManager:
    """关联上下文向量管理器"""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        index_dir: Optional[Path] = None,
        client: Optional[SiliconFlowEmbeddingClient] = None,
    ):
        self.db_path = db_path or (Path.home() / ".mnemos" / "knowledge_graph.db")
        self.index_dir = Path(index_dir).expanduser() if index_dir else (
            Path.home() / ".mnemos" / "embedding_index"
        )
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.client = client or get_embedding_client()

        self._index = None
        self._next_id = 1
        self._rel_id_map: Dict[int, int] = {}  # relation_id -> hnswlib_id
        self._load_existing()

    def _load_existing(self):
        """从数据库加载已有记录，初始化 hnswlib 索引"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                # 确保表存在
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS relation_context_embeddings (
                        id INTEGER PRIMARY KEY,
                        relation_id INTEGER UNIQUE REFERENCES relations(id),
                        embedding BLOB,
                        model_version TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                rows = conn.execute(
                    "SELECT relation_id, id FROM relation_context_embeddings ORDER BY id"
                ).fetchall()
                for rel_id, row_id in rows:
                    self._rel_id_map[rel_id] = row_id
                    self._next_id = max(self._next_id, row_id + 1)
        except Exception as e:
            logger.warning(f"[RelationEmbedding] 加载已有记录失败: {e}")

    def _get_hnsw_index(self):
        """获取或创建 hnswlib 索引"""
        if self._index is not None:
            return self._index
        if not HNSWLIB_AVAILABLE:
            return None

        index_path = self.index_dir / "relation_index.bin"
        index = hnswlib.Index(space="cosine", dim=DIM)

        if index_path.exists():
            try:
                index.load_index(str(index_path), max_elements=max(len(self._rel_id_map) * 2 + 1000, 10000))
            except Exception as e:
                logger.warning(f"[RelationEmbedding] 加载索引失败，重建: {e}")
                index.init_index(max_elements=10000, ef_construction=200, M=16)
        else:
            index.init_index(max_elements=10000, ef_construction=200, M=16)

        index.set_ef(50)
        self._index = index
        return index

    def add_relation_context(self, relation_id: int, context: str, model_version: str = "BAAI/bge-m3") -> bool:
        """
        为关系上下文生成 embedding 并入库。

        Args:
            relation_id: 知识图谱 relations 表的 id
            context: 关联上下文文本
            model_version: embedding 模型版本

        Returns:
            是否成功
        """
        if not self.client or not context or not context.strip():
            return False

        # 检查是否已存在
        if relation_id in self._rel_id_map:
            logger.debug(f"[RelationEmbedding] relation_id={relation_id} 已存在，跳过")
            return True

        try:
            # 生成 embedding
            vec = self.client.embed_single(context)
            if not vec or sum(abs(x) for x in vec) == 0:
                return False

            # 归一化
            import math
            norm = math.sqrt(sum(x * x for x in vec))
            if norm > 0:
                vec = [x / norm for x in vec]

            hnsw_id = self._next_id
            self._next_id += 1

            # 写入 SQLite
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO relation_context_embeddings
                       (relation_id, embedding, model_version)
                       VALUES (?, ?, ?)""",
                    (relation_id, json.dumps(vec), model_version),
                )
                conn.commit()

            # 写入 hnswlib
            index = self._get_hnsw_index()
            if index is not None:
                index.add_items([vec], [hnsw_id])
                index.save_index(str(self.index_dir / "relation_index.bin"))

            self._rel_id_map[relation_id] = hnsw_id
            logger.debug(f"[RelationEmbedding] relation_id={relation_id} → hnsw_id={hnsw_id}")
            return True

        except Exception as e:
            logger.warning(f"[RelationEmbedding] 添加失败 relation_id={relation_id}: {e}")
            return False

    def remove_relation_context(self, relation_id: int) -> bool:
        """删除关系的关联向量"""
        if relation_id not in self._rel_id_map:
            return False

        hnsw_id = self._rel_id_map.pop(relation_id)

        try:
            # 删除 SQLite 记录
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.execute(
                    "DELETE FROM relation_context_embeddings WHERE relation_id=?",
                    (relation_id,),
                )
                conn.commit()

            # hnswlib 标记删除（如果全部删完，重置索引避免查询异常）
            index = self._get_hnsw_index()
            if index is not None:
                if not self._rel_id_map:
                    # 最后一个元素删除，直接删除索引文件重建
                    index_path = self.index_dir / "relation_index.bin"
                    if index_path.exists():
                        index_path.unlink()
                    self._index = None
                else:
                    index.mark_deleted(hnsw_id)
                    index.save_index(str(self.index_dir / "relation_index.bin"))

            return True
        except Exception as e:
            logger.warning(f"[RelationEmbedding] 删除失败 relation_id={relation_id}: {e}")
            return False

    def search(self, query: str, top_k: int = 20) -> List[Tuple[int, float]]:
        """
        语义搜索关联上下文。

        Returns:
            [(relation_id, 相似度分数), ...] 按分数降序
        """
        if not self.client or not query:
            return []

        try:
            query_vec = self.client.embed_single(query)
            if not query_vec:
                return []
        except Exception as e:
            logger.warning(f"[RelationEmbedding] query embedding 失败: {e}")
            return []

        index = self._get_hnsw_index()
        if index is None or index.get_current_count() == 0:
            return []

        import math
        norm = math.sqrt(sum(x * x for x in query_vec))
        if norm > 0:
            q = [x / norm for x in query_vec]
        else:
            q = query_vec

        labels, distances = index.knn_query([q], k=min(top_k, index.get_current_count()))
        results = []
        for label, dist in zip(labels[0], distances[0]):
            sim = 1.0 - float(dist)
            # 反向查找 relation_id
            for rel_id, hid in self._rel_id_map.items():
                if hid == int(label):
                    results.append((rel_id, sim))
                    break

        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def get_stats(self) -> dict:
        """返回统计信息"""
        return {
            "total_relations": len(self._rel_id_map),
            "hnswlib_available": HNSWLIB_AVAILABLE,
            "client_available": self.client is not None,
            "index_dir": str(self.index_dir),
            "db_path": str(self.db_path),
        }
