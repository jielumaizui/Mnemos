# -*- coding: utf-8 -*-
"""
Embedding 索引管理器

功能：
1. 为 Wiki 页面建立 hnswlib 向量索引
2. 支持增量更新（按 mtime 判断变更）
3. 持久化到 ~/.mnemos/embedding_index/
4. hnswlib 不可用时 fallback 到纯内存列表（小数据集）

目录结构：
    ~/.mnemos/embedding_index/
        wiki_index.bin          # hnswlib 索引文件
        wiki_meta.json          # 页面路径 → id 映射 + mtime
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .siliconflow_client import (
    SiliconFlowEmbeddingClient,
    cosine_similarity,
    embedding_available,
    get_embedding_client,
    text_hash,
)

logger = logging.getLogger(__name__)

HNSWLIB_AVAILABLE = False
try:
    import hnswlib
    HNSWLIB_AVAILABLE = True
except ImportError:
    logger.debug("[Embedding] hnswlib not installed, using memory fallback")


class EmbeddingIndexManager:
    """
    Wiki 页面 embedding 索引管理器

    使用场景：
    - context_search.py 的语义召回
    - predictive_push.py 的语义 relevance gate
    - blindspot_discovery.py 的语义知识空白检测
    """

    # bge-m3 维度
    DIM = 1024
    # hnswlib 参数
    M = 16
    EF_CONSTRUCTION = 200
    EF_SEARCH = 50
    # 语义相似度阈值
    SIMILARITY_THRESHOLD = 0.75

    def __init__(
        self,
        wiki_base: Optional[Path] = None,
        index_dir: Optional[Path] = None,
        client: Optional[SiliconFlowEmbeddingClient] = None,
    ):
        from core.config import get_config

        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else get_config().wiki_dir
        self.index_dir = Path(index_dir).expanduser() if index_dir else (
            get_config().data_dir / "embedding_index"
        )
        self.index_dir.mkdir(parents=True, exist_ok=True)

        self.client = client or get_embedding_client()
        self._index = None
        self._meta: Dict[str, Dict] = {}  # path -> {"id": int, "mtime": float, "hash": str}
        self._memory_fallback: List[Tuple[str, List[float]]] = []  # hnswlib 不可用时使用

        self._index_path = self.index_dir / "wiki_index.bin"
        self._meta_path = self.index_dir / "wiki_meta.json"

        self._load_meta()

    # ---- 元数据管理 ----

    def _load_meta(self):
        if self._meta_path.exists():
            try:
                self._meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
            except Exception:
                self._meta = {}

    def _save_meta(self):
        try:
            self._meta_path.write_text(
                json.dumps(self._meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[Embedding] meta 保存失败: {e}")

    # ---- 索引构建 ----

    def _extract_page_text(self, page_path: Path) -> str:
        """从 Markdown 文件提取用于 embedding 的文本"""
        try:
            content = page_path.read_text(encoding="utf-8", errors="ignore")
            # 简单移除 frontmatter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    content = parts[2]
            # 取前 2000 字符作为摘要（控制成本）
            return content.strip()[:2000]
        except Exception:
            return ""

    def _scan_wiki_pages(self) -> List[Path]:
        """扫描所有 Wiki Markdown 文件"""
        if not self.wiki_base.exists():
            return []
        return list(self.wiki_base.rglob("*.md"))

    def build_index(self, force_full: bool = False) -> Dict[str, any]:
        """
        构建或增量更新索引

        Args:
            force_full: 强制全量重建

        Returns:
            {"added": int, "updated": int, "removed": int, "total": int}
        """
        if self.client is None or not embedding_available():
            logger.warning("[Embedding] 客户端不可用，跳过索引构建")
            return {"added": 0, "updated": 0, "removed": 0, "total": 0, "status": "no_client"}

        pages = self._scan_wiki_pages()
        current_paths = {str(p.relative_to(self.wiki_base)): p for p in pages}

        # 判断需要新增/更新/删除的页面
        to_add = []
        to_update = []
        to_remove = []

        for rel_path, page_path in current_paths.items():
            mtime = page_path.stat().st_mtime
            meta = self._meta.get(rel_path)
            text = self._extract_page_text(page_path)
            h = text_hash(text)

            if meta is None:
                to_add.append((rel_path, page_path, text))
            elif force_full or meta.get("mtime") != mtime or meta.get("hash") != h:
                to_update.append((rel_path, page_path, text))

        for rel_path in list(self._meta.keys()):
            if rel_path not in current_paths:
                to_remove.append(rel_path)

        total_changes = len(to_add) + len(to_update) + len(to_remove)
        if total_changes == 0:
            return {"added": 0, "updated": 0, "removed": 0, "total": len(current_paths), "status": "no_change"}

        logger.info(
            f"[Embedding] 索引更新: +{len(to_add)} ~{len(to_update)} -{len(to_remove)} "
            f"(total={len(current_paths)})"
        )

        # 收集所有需要 embedding 的文本
        texts_to_embed = []
        path_order = []

        for rel_path, page_path, text in to_add + to_update:
            texts_to_embed.append(text)
            path_order.append(rel_path)

        # 批量获取 embedding
        if texts_to_embed:
            try:
                embeddings = self.client.embed(texts_to_embed)
            except Exception as e:
                logger.error(f"[Embedding] 批量 embedding 失败: {e}")
                return {"added": 0, "updated": 0, "removed": 0, "total": 0, "status": "error", "error": str(e)}
        else:
            embeddings = []

        # 更新索引
        if HNSWLIB_AVAILABLE and len(current_paths) >= 10:
            self._update_hnsw_index(
                to_add, to_update, to_remove, path_order, embeddings, current_paths
            )
        else:
            self._update_memory_fallback(
                to_add, to_update, to_remove, path_order, embeddings, current_paths
            )

        self._save_meta()
        return {
            "added": len(to_add),
            "updated": len(to_update),
            "removed": len(to_remove),
            "total": len(current_paths),
            "status": "ok",
            "backend": "hnswlib" if HNSWLIB_AVAILABLE and len(current_paths) >= 10 else "memory",
        }

    def _update_hnsw_index(
        self,
        to_add, to_update, to_remove,
        path_order, embeddings, current_paths,
    ):
        """使用 hnswlib 更新索引"""
        n_total = len(current_paths)
        # 重建索引（hnswlib 不支持动态删除，小数据集重建成本可接受）
        index = hnswlib.Index(space="cosine", dim=self.DIM)
        index.init_index(max_elements=max(n_total * 2, 100), ef_construction=self.EF_CONSTRUCTION, M=self.M)
        index.set_ef(self.EF_SEARCH)

        new_meta = {}
        idx = 0
        all_embeddings = []

        for rel_path, page_path in current_paths.items():
            if rel_path in to_remove:
                continue

            text = self._extract_page_text(page_path)
            h = text_hash(text)
            mtime = page_path.stat().st_mtime

            # 获取 embedding
            if rel_path in path_order:
                emb = embeddings[path_order.index(rel_path)]
            else:
                # 未变更的页面，从 meta 中找旧 embedding（这里简化处理：重新嵌入）
                old_meta = self._meta.get(rel_path, {})
                if "embedding" in old_meta:
                    emb = old_meta["embedding"]
                else:
                    try:
                        emb = self.client.embed_single(text)
                    except Exception:
                        emb = [0.0] * self.DIM

            # 归一化（hnswlib cosine 空间需要单位向量）
            import math
            norm = math.sqrt(sum(x * x for x in emb))
            if norm > 0:
                emb = [x / norm for x in emb]

            all_embeddings.append(emb)
            new_meta[rel_path] = {
                "id": idx,
                "mtime": mtime,
                "hash": h,
            }
            idx += 1

        if all_embeddings:
            data = [[float(x) for x in vec] for vec in all_embeddings]
            index.add_items(data, list(range(len(data))))
            index.save_index(str(self._index_path))

        self._index = index
        self._meta = new_meta

    def _update_memory_fallback(
        self,
        to_add, to_update, to_remove,
        path_order, embeddings, current_paths,
    ):
        """hnswlib 不可用时使用内存列表"""
        new_fallback = []
        emb_map = {rel_path: emb for rel_path, emb in zip(path_order, embeddings)}

        for rel_path, page_path in current_paths.items():
            if rel_path in to_remove:
                continue

            text = self._extract_page_text(page_path)
            h = text_hash(text)
            mtime = page_path.stat().st_mtime

            if rel_path in emb_map:
                emb = emb_map[rel_path]
            elif rel_path in self._meta and "embedding" in self._meta[rel_path]:
                emb = self._meta[rel_path]["embedding"]
            else:
                try:
                    emb = self.client.embed_single(text)
                except Exception:
                    emb = [0.0] * self.DIM

            new_fallback.append((rel_path, emb))
            self._meta[rel_path] = {
                "id": len(new_fallback) - 1,
                "mtime": mtime,
                "hash": h,
                "embedding": emb,  # 内存模式缓存 embedding
            }

        self._memory_fallback = new_fallback

    # ---- 搜索 ----

    def search(
        self,
        query: str,
        top_k: int = 10,
        similarity_threshold: float = None,
    ) -> List[Tuple[str, float]]:
        """
        语义搜索

        Returns:
            [(页面相对路径, 相似度分数), ...] 按分数降序
        """
        threshold = similarity_threshold or self.SIMILARITY_THRESHOLD

        if self.client is None:
            return []

        try:
            query_vec = self.client.embed_single(query)
        except Exception as e:
            logger.warning(f"[Embedding] query embedding 失败: {e}")
            return []

        if not query_vec or sum(abs(x) for x in query_vec) == 0:
            return []

        # 确保索引已构建
        if self._index is None and not self._memory_fallback:
            self.build_index()

        results = []

        if HNSWLIB_AVAILABLE and self._index is not None:
            # hnswlib 搜索
            import math
            norm = math.sqrt(sum(x * x for x in query_vec))
            if norm > 0:
                q = [x / norm for x in query_vec]
            else:
                q = query_vec

            labels, distances = self._index.knn_query([q], k=min(top_k * 2, self._index.get_current_count()))
            for label, dist in zip(labels[0], distances[0]):
                # cosine 距离 = 1 - cosine_similarity
                sim = 1.0 - float(dist)
                if sim >= threshold:
                    # 反向查找路径
                    for rel_path, meta in self._meta.items():
                        if meta.get("id") == int(label):
                            results.append((rel_path, sim))
                            break
        else:
            # 内存 fallback
            import math
            q_norm = math.sqrt(sum(x * x for x in query_vec))
            for rel_path, vec in self._memory_fallback:
                dot = sum(x * y for x, y in zip(query_vec, vec))
                v_norm = math.sqrt(sum(x * x for x in vec))
                if q_norm == 0 or v_norm == 0:
                    continue
                sim = dot / (q_norm * v_norm)
                if sim >= threshold:
                    results.append((rel_path, sim))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def get_stats(self) -> Dict[str, any]:
        """返回索引统计信息"""
        return {
            "total_pages": len(self._meta),
            "hnswlib_available": HNSWLIB_AVAILABLE,
            "client_available": self.client is not None and embedding_available(),
            "index_dir": str(self.index_dir),
            "wiki_base": str(self.wiki_base),
        }
