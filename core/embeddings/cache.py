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
from typing import Any, Dict, List, Optional, Tuple

from core.config import get_config
from core.db_utils import render_sql

# Constants extracted from magic numbers
EMBEDDING_CACHE_MAX_ENTRIES = 50000
EMBEDDING_CACHE_TTL_DAYS = 30
EMBEDDING_CACHE_SUBJECT_DELETION_SCHEMA_VERSION = "mnemos.embedding_cache_subject_deletion.v1"
EMBEDDING_CACHE_SUBJECT_DELETION_TABLE = "embedding_cache_subject_deletion_receipts"

logger = logging.getLogger(__name__)


def _subject_deletion_sql(template: str) -> str:
    return render_sql(
        template,
        identifiers={
            "subject_deletion_table": EMBEDDING_CACHE_SUBJECT_DELETION_TABLE
        },
    )


def default_db_path(config: Any | None = None) -> Path:
    """Resolve the default embedding cache DB path lazily."""
    cfg = config or get_config()
    return Path(cfg.database_dir) / "embedding_cache.db"


class EmbeddingCache:
    """Embedding 缓存 —— SQLite 持久化，支持多模型版本隔离"""

    # [P1-12] 缓存条目上限，防止数据库无限增长
    MAX_ENTRIES = EMBEDDING_CACHE_MAX_ENTRIES
    # 超过上限时清理的比例（清理最旧的 20%）
    CLEANUP_RATIO = 0.20
    # 缓存 TTL：超过 N 天未使用则清理
    TTL_DAYS = EMBEDDING_CACHE_TTL_DAYS

    def __init__(
        self,
        db_path: Optional[Path] = None,
        model_version: str = "BAAI/bge-m3",
        config: Any | None = None,
    ):
        self.db_path = Path(db_path) if db_path is not None else default_db_path(config)
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cache_model
                ON embedding_cache(model_version)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cache_last_used
                ON embedding_cache(last_used_at)
            """)
            # Explicit cache bootstrap ensures the eviction timestamp column.
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(embedding_cache)")}
            if "last_used_at" not in existing_cols:
                conn.execute(
                    "ALTER TABLE embedding_cache ADD COLUMN last_used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"  # noqa: E501
                )
            conn.commit()

    @staticmethod
    def _subject_deletion_receipt_id(
        *, request_id: str, scope_kind: str, scope_value_hash: str
    ) -> str:
        """Create an opaque, deterministic receipt identity for one cache flush."""

        material = "|".join((str(request_id), str(scope_kind), str(scope_value_hash)))
        return "embedding-cache-delete-" + hashlib.sha256(
            material.encode("utf-8")
        ).hexdigest()[:40]

    @staticmethod
    def _ensure_subject_deletion_schema(conn: sqlite3.Connection) -> None:
        """Create the cache owner's privacy receipt table in its own database."""

        conn.execute(
            _subject_deletion_sql(
                """
            CREATE TABLE IF NOT EXISTS {subject_deletion_table} (
                receipt_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                request_id TEXT NOT NULL,
                scope_kind TEXT NOT NULL,
                scope_value_hash TEXT NOT NULL,
                before_entry_count INTEGER NOT NULL,
                deleted_entry_count INTEGER NOT NULL DEFAULT 0,
                after_entry_count INTEGER NOT NULL DEFAULT -1,
                status TEXT NOT NULL CHECK(status IN ('planned', 'flushed', 'applied')),
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                applied_at TIMESTAMP,
                UNIQUE(request_id, scope_kind, scope_value_hash)
            )
            """
            )
        )

    @classmethod
    def delete_subject_scope(
        cls,
        *,
        db_path: Path | str,
        request_id: str,
        scope_kind: str,
        scope_value_hash: str,
    ) -> Dict[str, Any]:
        """Flush a source-unattributable cache under a typed delete receipt.

        The cache is intentionally keyed only by content hash, not by source
        asset.  A subject-specific purge therefore cannot prove a narrow row
        set.  The safe deletion contract is a global flush: it may cost future
        re-embedding work, but it cannot retain a deleted subject's vector in
        a cache that lacks provenance.
        """

        database = Path(db_path).expanduser()
        normalized_kind = str(scope_kind or "").strip().lower()
        normalized_hash = str(scope_value_hash or "").strip()
        if not database.is_file():
            return {
                "status": "not_initialized",
                "target_count": 0,
                "receipt_count": 0,
                "verified": True,
                "mode": "uninitialized_cache",
            }
        if not str(request_id or "").strip() or not normalized_kind or len(normalized_hash) < 32:
            raise ValueError("embedding cache subject deletion requires opaque request and scope IDs")

        try:
            with sqlite3.connect(str(database), timeout=10) as conn:
                cache_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='embedding_cache'"
                ).fetchone()
                if cache_table is None:
                    return {
                        "status": "blocked",
                        "target_count": 0,
                        "receipt_count": 0,
                        "verified": False,
                        "error": "embedding_cache_table_missing",
                    }
                cls._ensure_subject_deletion_schema(conn)
                receipt_id = cls._subject_deletion_receipt_id(
                    request_id=request_id,
                    scope_kind=normalized_kind,
                    scope_value_hash=normalized_hash,
                )
                existing = conn.execute(
                    _subject_deletion_sql(
                        """
                    SELECT * FROM {subject_deletion_table}
                    WHERE receipt_id=?
                    """
                    ),
                    (receipt_id,),
                ).fetchone()
                remaining_before = int(
                    conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0]
                )
                if existing is not None and str(existing[8]) == "applied":
                    if remaining_before == 0:
                        return {
                            "status": "existing",
                            "target_count": int(existing[5]),
                            "receipt_count": 1,
                            "deleted_entry_count": int(existing[6]),
                            "after_entry_count": 0,
                            "verified": True,
                            "mode": "global_unattributable_cache_flush",
                        }
                    return {
                        "status": "blocked",
                        "target_count": int(existing[5]),
                        "receipt_count": 1,
                        "verified": False,
                        "error": "embedding_cache_repopulated_after_delete",
                    }

                original_target_count = (
                    int(existing[5]) if existing is not None else remaining_before
                )
                prior_deleted_count = int(existing[6]) if existing is not None else 0

                conn.execute("PRAGMA secure_delete=ON")
                secure_delete = conn.execute("PRAGMA secure_delete").fetchone()
                if not secure_delete or int(secure_delete[0] or 0) < 1:
                    return {
                        "status": "blocked",
                        "target_count": original_target_count,
                        "receipt_count": 0,
                        "verified": False,
                        "error": "embedding_cache_secure_delete_unavailable",
                    }
                if existing is None:
                    conn.execute(
                        _subject_deletion_sql(
                            """
                        INSERT INTO {subject_deletion_table} (
                            receipt_id, schema_version, request_id, scope_kind,
                            scope_value_hash, before_entry_count, status
                        ) VALUES (?, ?, ?, ?, ?, ?, 'planned')
                        """
                        ),
                        (
                            receipt_id,
                            EMBEDDING_CACHE_SUBJECT_DELETION_SCHEMA_VERSION,
                            str(request_id),
                            normalized_kind,
                            normalized_hash,
                            remaining_before,
                        ),
                    )
                deleted = int(
                    conn.execute("DELETE FROM embedding_cache").rowcount or 0
                )
                deleted_total = prior_deleted_count + deleted
                remaining_after = int(
                    conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0]
                )
                if remaining_after:
                    conn.rollback()
                    return {
                        "status": "blocked",
                        "target_count": original_target_count,
                        "receipt_count": 1 if existing is not None else 0,
                        "verified": False,
                        "error": "embedding_cache_after_oracle_nonzero",
                    }
                conn.execute(
                    _subject_deletion_sql(
                        """
                    UPDATE {subject_deletion_table}
                    SET deleted_entry_count=?, after_entry_count=0, status='flushed'
                    WHERE receipt_id=?
                    """
                    ),
                    (deleted_total, receipt_id),
                )
                conn.commit()
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return {
                "status": "blocked",
                "target_count": 0,
                "receipt_count": 0,
                "verified": False,
                "error": "embedding_cache_subject_deletion_failed",
            }

        # SQLite WAL can retain pre-delete cache bytes until checkpointed.  Do
        # not mark the receipt applied until the checkpoint is terminal and an
        # independent post-checkpoint oracle sees zero live entries.
        try:
            with sqlite3.connect(str(database), timeout=10) as conn:
                checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is not None and int(checkpoint[0] or 0) != 0:
                    return {
                        "status": "pending_checkpoint",
                        "target_count": original_target_count,
                        "receipt_count": 1,
                        "deleted_entry_count": deleted_total,
                        "verified": False,
                        "error": "embedding_cache_wal_checkpoint_busy",
                    }
                remaining_after = int(
                    conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0]
                )
                if remaining_after:
                    return {
                        "status": "blocked",
                        "target_count": original_target_count,
                        "receipt_count": 1,
                        "deleted_entry_count": deleted_total,
                        "verified": False,
                        "error": "embedding_cache_post_checkpoint_oracle_nonzero",
                    }
                conn.execute(
                    _subject_deletion_sql(
                        """
                    UPDATE {subject_deletion_table}
                    SET status='applied', after_entry_count=0,
                        applied_at=CURRENT_TIMESTAMP
                    WHERE receipt_id=? AND status='flushed'
                    """
                    ),
                    (receipt_id,),
                )
                conn.commit()
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return {
                "status": "pending_checkpoint",
                "target_count": original_target_count,
                "receipt_count": 1,
                "deleted_entry_count": deleted_total,
                "verified": False,
                "error": "embedding_cache_wal_checkpoint_failed",
            }

        return {
            "status": "applied",
            "target_count": original_target_count,
            "receipt_count": 1,
            "deleted_entry_count": deleted_total,
            "after_entry_count": 0,
            "verified": True,
            "mode": "global_unattributable_cache_flush",
        }

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
                    "SELECT embedding FROM embedding_cache WHERE content_hash=? AND model_version=?",  # noqa: E501
                    (content_hash, mv),
                ).fetchone()
                if row:
                    # 更新最近使用时间
                    conn.execute(
                        "UPDATE embedding_cache SET last_used_at=CURRENT_TIMESTAMP WHERE content_hash=? AND model_version=?",  # noqa: E501
                        (content_hash, mv),
                    )
                    conn.commit()
                    return json.loads(row[0])  # type: ignore[no-any-return]
        except (sqlite3.Error, json.JSONDecodeError, TypeError) as e:
            logger.warning("[EmbeddingCache] 查询失败: %s", e, exc_info=True)
        return None

    def get_batch(
        self, texts: List[str], model_version: Optional[str] = None
    ) -> Tuple[List[Optional[List[float]]], List[int]]:
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

        if not texts:
            return results, missing_indices

        # 预计算所有 hash。相同文本可以在一个 batch 中出现多次；每个命中
        # 必须回填全部原始位置，不能只保留最后一个 index。
        hash_to_indices: Dict[str, List[int]] = {}
        for i, t in enumerate(texts):
            h = self.compute_hash(t)
            hash_to_indices.setdefault(h, []).append(i)
        hash_list = list(hash_to_indices)

        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                # [P0-3] 使用 IN 子句批量查询，替代 N+1 逐条查询
                placeholders = ",".join("?" * len(hash_list))
                cursor = conn.execute(
                    f"""SELECT content_hash, embedding FROM embedding_cache
                       WHERE content_hash IN ({placeholders}) AND model_version=?""",  # nosec B608
                    (*hash_list, mv),
                )
                hit_hashes: set = set()
                for row in cursor.fetchall():
                    h, emb_json = row
                    indices = hash_to_indices.get(h, [])
                    if indices:
                        embedding = json.loads(emb_json)
                        for idx in indices:
                            results[idx] = embedding
                        hit_hashes.add(h)
                # 更新命中的最近使用时间
                if hit_hashes:
                    placeholders = ",".join("?" * len(hit_hashes))
                    conn.execute(
                        f"UPDATE embedding_cache SET last_used_at=CURRENT_TIMESTAMP WHERE content_hash IN ({placeholders}) AND model_version=?",  # noqa: E501  # nosec B608
                        (*hit_hashes, mv),
                    )
                    conn.commit()
                # 未命中的索引
                for h, indices in hash_to_indices.items():
                    if h not in hit_hashes:
                        missing_indices.extend(indices)
                missing_indices.sort()
        except (sqlite3.Error, json.JSONDecodeError, TypeError) as e:
            logger.warning("[EmbeddingCache] 批量查询失败: %s", e)
            missing_indices = list(range(len(texts)))

        return results, missing_indices

    def set(
        self,
        text: str,
        embedding: List[float],
        model_version: Optional[str] = None,
        token_count: int = 0,
    ) -> None:
        """写入缓存"""
        content_hash = self.compute_hash(text)
        mv = model_version or self.model_version
        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO embedding_cache
                       (content_hash, embedding, model_version, token_count, last_used_at)
                       VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                    (content_hash, json.dumps(embedding), mv, token_count),
                )
                conn.commit()
                self._maybe_cleanup(conn)
        except (sqlite3.Error, TypeError, ValueError) as e:
            logger.warning("[EmbeddingCache] 写入失败: %s", e, exc_info=True)

    def set_batch(
        self,
        texts: List[str],
        embeddings: List[List[float]],
        model_version: Optional[str] = None,
        token_counts: Optional[List[int]] = None,
    ) -> None:
        """批量写入缓存（batch → individual fallback）"""
        if len(texts) != len(embeddings):
            raise ValueError("texts 和 embeddings 长度不一致")
        if not texts:
            return
        mv = model_version or self.model_version
        token_counts = token_counts or [0] * len(texts)

        rows = []
        for text, emb, tc in zip(texts, embeddings, token_counts):
            content_hash = self.compute_hash(text)
            rows.append((content_hash, json.dumps(emb), mv, tc))

        try:
            with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                conn.executemany(
                    """INSERT OR REPLACE INTO embedding_cache
                       (content_hash, embedding, model_version, token_count, last_used_at)
                       VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                    rows,
                )
                conn.commit()
                self._maybe_cleanup(conn)
        except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
            logger.warning("[EmbeddingCache] 批量写入失败，尝试逐条写入", exc_info=True)
            try:
                with sqlite3.connect(str(self.db_path), timeout=10) as conn:
                    for row in rows:
                        try:
                            conn.execute(
                                """INSERT OR REPLACE INTO embedding_cache
                                   (content_hash, embedding, model_version,
                                    token_count, last_used_at)
                                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                                row,
                            )
                        except (
                            OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError,
                            sqlite3.Error
                        ):
                            logger.warning(
                                "[EmbeddingCache] 逐条写入失败: %s", row[0], exc_info=True
                            )
                    conn.commit()
                    self._maybe_cleanup(conn)
            except (OSError, ValueError, TypeError, KeyError, ImportError, AttributeError, RuntimeError, sqlite3.Error):
                logger.warning("[EmbeddingCache] 逐条写入 fallback 也失败", exc_info=True)

    def _maybe_cleanup(self, conn: sqlite3.Connection) -> None:
        """当缓存条目超过上限或存在过期记录时，清理最旧/最久未用的一批"""
        try:
            # 1. 清理超过 TTL 天未使用的记录
            cursor = conn.execute(
                "DELETE FROM embedding_cache WHERE last_used_at < datetime('now', ?)",
                (f"-{self.TTL_DAYS} days",),
            )
            expired_deleted = cursor.rowcount

            # 2. 如果仍超过上限，按 last_used_at 清理最久未用的
            cursor = conn.execute("SELECT COUNT(*) FROM embedding_cache")
            count = cursor.fetchone()[0]
            over_limit_deleted = 0
            if count > self.MAX_ENTRIES:
                to_delete = int(count * self.CLEANUP_RATIO)
                cursor = conn.execute(
                    "DELETE FROM embedding_cache WHERE rowid IN (SELECT rowid FROM embedding_cache ORDER BY last_used_at ASC LIMIT ?)",  # noqa: E501
                    (to_delete,),
                )
                over_limit_deleted = cursor.rowcount

            if expired_deleted or over_limit_deleted:
                conn.commit()
                logger.info(
                    "[EmbeddingCache] 缓存清理完成: 过期 %s 条，超限 %s 条",
                    expired_deleted,
                    over_limit_deleted,
                )
        except sqlite3.Error as e:
            logger.warning("[EmbeddingCache] 缓存清理失败: %s", e)

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
        except sqlite3.Error as e:
            logger.warning("[EmbeddingCache] 失效失败: %s", e, exc_info=True)
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
        except sqlite3.Error as e:
            logger.warning("[EmbeddingCache] 统计失败: %s", e, exc_info=True)
            return {"total_entries": 0, "by_model": {}, "db_path": str(self.db_path)}
