# -*- coding: utf-8 -*-
"""
双索引融合检索器（ADR-019）

页面向量索引（EmbeddingIndexManager）+ 关联上下文向量索引（RelationEmbeddingManager）
融合策略：final_score = content_weight * content_sim + relation_boost

使用场景：
- context_search.py 的语义召回
- knowledge_graph.py 的语义搜索增强
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .index_manager import EmbeddingIndexManager
from .relation_manager import RelationEmbeddingManager
from .siliconflow_client import SiliconFlowEmbeddingClient

logger = logging.getLogger(__name__)


class DualIndexRetriever:
    """
    双索引融合检索器。

    索引 A：页面内容向量（EmbeddingIndexManager）
    索引 B：关联上下文向量（RelationEmbeddingManager）

    检索时同时查询两个索引，融合得分后重排。
    """

    def __init__(
        self,
        page_index: Optional[EmbeddingIndexManager] = None,
        relation_manager: Optional[RelationEmbeddingManager] = None,
        wiki_base: Optional[Path] = None,
        content_weight: float = 0.7,
        relation_weight: float = 0.3,
    ):
        self.page_index = page_index
        self.relation_manager = relation_manager
        self.wiki_base = wiki_base
        self.content_weight = content_weight
        self.relation_weight = relation_weight

        # 懒加载：如果未传入则自动创建
        self._page_index_lazy = page_index is None
        self._relation_manager_lazy = relation_manager is None

    def _ensure_page_index(self):
        if self.page_index is None and self._page_index_lazy:
            from core.config import get_config
            self.wiki_base = self.wiki_base or get_config().wiki_dir
            self.page_index = EmbeddingIndexManager(wiki_base=self.wiki_base)

    def _ensure_relation_manager(self):
        if self.relation_manager is None and self._relation_manager_lazy:
            self.relation_manager = RelationEmbeddingManager()

    def _get_relation_pages(self, relation_id: int) -> Tuple[Optional[str], Optional[str]]:
        """根据 relation_id 查询 source 和 target 页面路径"""
        if self.relation_manager is None:
            return None, None
        try:
            with sqlite3.connect(str(self.relation_manager.db_path), timeout=10) as conn:
                row = conn.execute(
                    "SELECT source, target FROM relations WHERE id=?",
                    (relation_id,),
                ).fetchone()
                if row:
                    return row[0], row[1]
        except Exception as e:
            logger.debug(f"[DualIndex] 查询关系页面失败: {e}")
        return None, None

    def search(
        self,
        query: str,
        top_k: int = 10,
        similarity_threshold: float = None,
        use_rerank: bool = True,
    ) -> List[Tuple[str, float]]:
        """
        双索引融合检索。

        Returns:
            [(页面相对路径, 融合分数), ...] 按分数降序
        """
        self._ensure_page_index()
        self._ensure_relation_manager()

        if self.page_index is None or self.page_index.client is None:
            return []

        # --- Phase 1: 内容检索（索引 A）---
        try:
            page_results = self.page_index.search(
                query,
                top_k=max(top_k * 3, 20),
                similarity_threshold=similarity_threshold,
                use_rerank=False,  # 双索引融合后再做 rerank
            )
        except Exception as e:
            logger.warning(f"[DualIndex] 页面检索失败: {e}")
            page_results = []

        content_scores: Dict[str, float] = {}
        for rel_path, sim in page_results:
            content_scores[rel_path] = sim

        # --- Phase 2: 关联检索（索引 B）---
        relation_boost: Dict[str, float] = defaultdict(float)
        if self.relation_manager is not None and self.relation_manager.client is not None:
            try:
                relation_results = self.relation_manager.search(query, top_k=20)
                for rel_id, rel_sim in relation_results:
                    source, target = self._get_relation_pages(rel_id)
                    boost = rel_sim * self.relation_weight
                    if source:
                        relation_boost[source] += boost
                    if target:
                        relation_boost[target] += boost
            except Exception as e:
                logger.debug(f"[DualIndex] 关联检索失败: {e}")

        # --- Phase 3: 融合得分 ---
        all_pages = set(content_scores.keys()) | set(relation_boost.keys())
        if not all_pages:
            return []

        fused_scores: Dict[str, float] = {}
        for page in all_pages:
            c_score = content_scores.get(page, 0.0)
            r_score = relation_boost.get(page, 0.0)
            fused_scores[page] = self.content_weight * c_score + r_score

        # --- Phase 4: Rerank 精排 ---
        top_candidates = sorted(fused_scores.items(), key=lambda x: x[1], reverse=True)

        if use_rerank and len(top_candidates) > top_k and self.page_index.client is not None:
            try:
                return self._rerank_candidates(query, top_candidates, top_k)
            except Exception as e:
                logger.debug(f"[DualIndex] Rerank 失败，回退到融合排序: {e}")

        return top_candidates[:top_k]

    def _rerank_candidates(
        self,
        query: str,
        candidates: List[Tuple[str, float]],
        top_k: int,
    ) -> List[Tuple[str, float]]:
        """对融合后的候选结果调用 Rerank API 精排"""
        wiki_base = self.wiki_base or self.page_index.wiki_base
        documents = []
        valid_paths = []

        for rel_path, _ in candidates[:top_k * 2]:
            page_path = wiki_base / rel_path
            try:
                text = page_path.read_text(encoding="utf-8", errors="ignore")
                # 简单移除 frontmatter
                if text.startswith("---"):
                    parts = text.split("---", 2)
                    if len(parts) >= 3:
                        text = parts[2]
                text = text.strip()[:2000]
                if text:
                    documents.append(text)
                    valid_paths.append(rel_path)
            except Exception:
                continue

        if not documents:
            return candidates[:top_k]

        reranked = self.page_index.client.rerank(
            query=query,
            documents=documents,
            top_n=top_k,
        )
        return [(valid_paths[idx], score) for idx, score in reranked if idx < len(valid_paths)]

    def get_stats(self) -> dict:
        """返回双索引统计"""
        return {
            "content_weight": self.content_weight,
            "relation_weight": self.relation_weight,
            "page_index": self.page_index.get_stats() if self.page_index else None,
            "relation_index": self.relation_manager.get_stats() if self.relation_manager else None,
        }
