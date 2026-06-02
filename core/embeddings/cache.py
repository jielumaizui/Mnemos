# -*- coding: utf-8 -*-
"""
Embedding 缓存管理器

基于 SQLite 的 content_hash → embedding 缓存。
- Key: sha256(text.strip())
- Value: embedding 向量（JSON 文本存储，便于跨版本兼容）
- TTL: 永久（同一内容的 embedding 不变），模型变更时自动失效

表结构：
    embedding_cache (
        content_hash TEXT PRIMARY KEY,
        embedding TEXT NOT NULL,        -- JSON 编码的 float 列表
        model_version TEXT NOT NULL,    -- 如 "BAAI/bge-m3"
        token_count INTEGER,            -- 预估 token 数（用于限流统计）
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / ".mnemos" / "embedding_cache.db"


class EmbeddingCache:
    """Embedding 缓存 —— SQLite 持久化，支持多模型版本隔离"""

    def __init__(self, db_path: Optional[Path] = None, model_version: str = "BAAI/bge-m3"):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.model_version = model_version
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embedding_cache (
                    content_hash TEXT PRIMARY KEY,
                    embedding TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    token_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cache_model
                ON embedding_cache(model_version)
            """)
            conn.commit()

    @staticmethod
    def compute_hash(text: str) -> str:
        """计算文本的缓存键 —— 去除前后空白后 sha256"""
        return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()

    def get(self, text: str, model_version: Optional[str] = None) -> Optional[List[float]]:
        """
        查询缓存。

        Returns:
            embedding 向量，或 None（未命中 / 模型版本不匹配）
        """
        content_hash = self.compute_hash(text)
        mv = model_version or self.model_version
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                row = conn.execute(
                    "SELECT embedding FROM embedding_cache WHERE content_hash=? AND model_version=?",
                    (content_hash, mv),
                ).fetchone()
                if row:
                    return json.loads(row[0])
        except Exception as e:
            logger.warning(f"[EmbeddingCache] 查询失败: {e}")
        return None

    def get_batch(self, texts: List[str], model_version: Optional[str] = None) -> Tuple[List[Optional[List[float]]], List[int]]:
        """
        批量查询缓存。

        Returns:
            (results, missing_indices)
            - results: 与 texts 等长的列表，命中为 embedding，未命中为 None
            - missing_indices: 未命中的索引列表
        """
        mv = model_version or self.model_version
        results: List[Optional[List[float]]] = [None] * len(texts)
        missing_indices: List[int] = []

        # 预计算所有 hash
        hashes = [(i, self.compute_hash(t)) for i, t in enumerate(texts)]

        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                for idx, h in hashes:
                    row = conn.execute(
                        "SELECT embedding FROM embedding_cache WHERE content_hash=? AND model_version=?",
                        (h, mv),
                    ).fetchone()
                    if row:
                        results[idx] = json.loads(row[0])
                    else:
                        missing_indices.append(idx)
        except Exception as e:
            logger.warning(f"[EmbeddingCache] 批量查询失败: {e}")
            missing_indices = list(range(len(texts)))

        return results, missing_indices

    def set(self, text: str, embedding: List[float], model_version: Optional[str] = None, token_count: int = 0) -> None:
        """写入缓存"""
        content_hash = self.compute_hash(text)
        mv = model_version or self.model_version
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO embedding_cache
                       (content_hash, embedding, model_version, token_count)
                       VALUES (?, ?, ?, ?)""",
                    (content_hash, json.dumps(embedding), mv, token_count),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"[EmbeddingCache] 写入失败: {e}")

    def set_batch(self, texts: List[str], embeddings: List[List[float]], model_version: Optional[str] = None, token_counts: Optional[List[int]] = None) -> None:
        """批量写入缓存"""
        if len(texts) != len(embeddings):
            raise ValueError("texts 和 embeddings 长度不一致")
        mv = model_version or self.model_version
        token_counts = token_counts or [0] * len(texts)
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                for text, emb, tc in zip(texts, embeddings, token_counts):
                    content_hash = self.compute_hash(text)
                    conn.execute(
                        """INSERT OR REPLACE INTO embedding_cache
                           (content_hash, embedding, model_version, token_count)
                           VALUES (?, ?, ?, ?)""",
                        (content_hash, json.dumps(emb), mv, tc),
                    )
                conn.commit()
        except Exception as e:
            logger.warning(f"[EmbeddingCache] 批量写入失败: {e}")

    def invalidate_model(self, model_version: str) -> int:
        """使指定模型版本的所有缓存失效（模型升级时调用）"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                cursor = conn.execute(
                    "DELETE FROM embedding_cache WHERE model_version=?",
                    (model_version,),
                )
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            logger.warning(f"[EmbeddingCache] 失效失败: {e}")
            return 0

    def get_stats(self) -> dict:
        """返回缓存统计"""
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                total = conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0]
                by_model = conn.execute(
                    "SELECT model_version, COUNT(*) FROM embedding_cache GROUP BY model_version"
                ).fetchall()
                return {
                    "total_entries": total,
                    "by_model": {mv: cnt for mv, cnt in by_model},
                    "db_path": str(self.db_path),
                }
        except Exception as e:
            logger.warning(f"[EmbeddingCache] 统计失败: {e}")
            return {"total_entries": 0, "by_model": {}, "db_path": str(self.db_path)}
