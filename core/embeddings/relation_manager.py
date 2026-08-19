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

import atexit
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from core.cognitive.material_effect_schema import initialize_material_effect_schema
from core.config import get_config
from core.db_utils import sqlite_conn
from core.telemetry.prompt_call_log import ModelCallLedgerError
from core.telemetry.provider_request import ProviderRequestError
from .siliconflow_client import SiliconFlowEmbeddingClient, get_embedding_client

# Constants extracted from magic numbers
RELATION_EMBEDDING_MANAGER__GET_HNSW_INDEX_LEN = 10000
MAX_ELEMENTS = 10000
RELATION_EMBEDDING_MANAGER__REBUILD_FROM_SQLITE_M = 10000

logger = logging.getLogger(__name__)

HNSWLIB_AVAILABLE = False
try:
    import hnswlib

    HNSWLIB_AVAILABLE = True
except ImportError:
    logger.debug("[RelationEmbedding] hnswlib not installed")

# bge-m3 维度
DIM = 1024


def _config_value(config: object, key: str, default: Any) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(key, default)
    return default


class RelationEmbeddingManager:
    """关联上下文向量管理器"""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        index_dir: Optional[Path] = None,
        client: Optional[SiliconFlowEmbeddingClient] = None,
        config: object | None = None,
    ):
        cfg = config or get_config()
        configured_database_dir = getattr(cfg, "database_dir", None)
        if configured_database_dir is None:
            raise TypeError("RelationEmbeddingManager config must provide database_dir")
        database_dir = Path(configured_database_dir).expanduser()
        self.db_path = db_path or (database_dir / "knowledge_graph.db")
        self.index_dir = (
            Path(index_dir).expanduser() if index_dir else (database_dir / "embedding_index")
        )
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.client = client or get_embedding_client()

        self._index = None
        self._next_id = 1
        self._rel_id_map: Dict[int, int] = {}  # relation_id -> hnswlib_id
        self._hid_to_rel_id: Dict[int, int] = {}  # hnswlib_id -> relation_id

        # 批量 flush 配置
        self._batch_flush = bool(_config_value(cfg, "relation_embedding.batch_flush", True))
        self._flush_interval_seconds = float(
            _config_value(cfg, "relation_embedding.flush_interval_seconds", 60)
        )
        self._flush_batch_size = max(
            1, int(_config_value(cfg, "relation_embedding.flush_batch_size", 10))
        )
        self._dirty_count = 0
        self._last_flush = time.time()
        self._flush_lock = threading.RLock()
        self._automatic_flush_defer_depth = 0
        self._closed = False

        self._load_existing()
        atexit.register(self.close)

    def _relation_subject_scopes(self, relation_id: int) -> tuple[tuple[str, str], ...]:
        """Resolve the durable Wiki assets whose relation context is embedded.

        Relation context is derived from the graph's source/target pages.  When
        a prior-format or isolated database cannot provide those identities, use one
        explicit controlled source instead of letting the provider client make
        an anonymous fallback attribution.
        """
        try:
            with sqlite_conn(str(self.db_path), timeout=10) as conn:
                row = conn.execute(
                    "SELECT source, target FROM relations WHERE id=?",
                    (int(relation_id),),
                ).fetchone()
        except (OSError, sqlite3.Error):
            row = None
        if row is not None:
            scopes = {
                ("wiki_page", str(value).strip()) for value in row if str(value or "").strip()
            }
            if scopes:
                return tuple(sorted(scopes))
        return (("source", "relation_embedding_manager"),)

    def _uses_remote_embedding_client(self) -> bool:
        """Only the production SiliconFlow client crosses a billable boundary."""
        return isinstance(self.client, SiliconFlowEmbeddingClient)

    def _embed_single_attributed(
        self,
        text: str,
        subject_scopes: tuple[tuple[str, str], ...],
    ) -> List[float]:
        if self._uses_remote_embedding_client():
            vectors = self.client.embed(  # type: ignore[union-attr]
                [text],
                subject_scopes=[subject_scopes],
            )
            return vectors[0] if vectors else []
        return [
            float(value) for value in self.client.embed_single(text)  # type: ignore[union-attr]
        ]

    def _embed_batch_attributed(
        self,
        texts: List[str],
        subject_scopes: List[tuple[tuple[str, str], ...]],
    ) -> List[List[float]]:
        if self._uses_remote_embedding_client():
            vectors = self.client.embed(  # type: ignore[union-attr]
                texts,
                subject_scopes=subject_scopes,
            )
        else:
            vectors = self.client.embed(texts)  # type: ignore[union-attr]
        return [[float(value) for value in vector] for vector in vectors]

    def _load_existing(self):
        """从数据库加载已有记录，初始化 hnswlib 索引"""
        try:
            with sqlite_conn(str(self.db_path), timeout=10) as conn:
                # This projection shares knowledge_graph.db.  On a fresh
                # database the canonical material-effect owner must initialize
                # first; otherwise this table would make the later
                # KnowledgeGraph constructor classify the store as an
                # unreconciled historical database. Existing domain-only stores
                # still fail closed and require the explicit migration.
                initialize_material_effect_schema(conn)
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
                    self._hid_to_rel_id[row_id] = rel_id
                    self._next_id = max(self._next_id, row_id + 1)
        except (sqlite3.Error, OSError) as e:
            logger.warning("[RelationEmbedding] 加载已有记录失败: %s", e)

    def _set_rebuild_required(self, required: bool) -> None:
        with sqlite_conn(str(self.db_path), timeout=10) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS relation_embedding_state (
                       key TEXT PRIMARY KEY, value TEXT NOT NULL
                   )""")
            conn.execute(
                """INSERT INTO relation_embedding_state(key, value)
                   VALUES ('rebuild_required', ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                ("1" if required else "0",),
            )
            conn.commit()

    def _rebuild_required(self) -> bool:
        try:
            with sqlite_conn(str(self.db_path), timeout=10) as conn:
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='relation_embedding_state'"
                ).fetchone()
                if table is None:
                    return False
                row = conn.execute(
                    "SELECT value FROM relation_embedding_state WHERE key='rebuild_required'"
                ).fetchone()
            return row is not None and str(row[0]) == "1"
        except (OSError, sqlite3.Error):
            return True

    def repair_persistent_index(self) -> bool:
        """Rebuild the binary index from authoritative SQLite after a failed save."""

        if not self._rebuild_required():
            return True
        if not HNSWLIB_AVAILABLE:
            self._set_rebuild_required(False)
            return True
        with sqlite_conn(str(self.db_path), timeout=10) as conn:
            count = int(
                conn.execute("SELECT COUNT(*) FROM relation_context_embeddings").fetchone()[0]
            )
        if count == 0:
            index_path = self.index_dir / "relation_index.bin"
            if index_path.exists():
                index_path.unlink()
            self._index = None
            self._set_rebuild_required(False)
            return True
        return self.rebuild_persistent_index()

    def audit_projection(self) -> Dict[str, Any]:
        """Return the canonical durable after-oracle for relation vectors.

        SQLite vectors are the persistent fallback when hnswlib is absent.
        A binary index is therefore required only for the HNSW backend; using
        its file as an unconditional health signal makes the fallback retry
        forever even when every durable vector is present.
        """

        try:
            with sqlite_conn(str(self.db_path), timeout=10) as conn:
                relation_count = int(conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0])
                embedding_count = int(
                    conn.execute("SELECT COUNT(*) FROM relation_context_embeddings").fetchone()[0]
                )
                missing = int(conn.execute("""SELECT COUNT(*) FROM relations AS relation
                           LEFT JOIN relation_context_embeddings AS embedding
                             ON embedding.relation_id=relation.id
                           WHERE embedding.relation_id IS NULL""").fetchone()[0])
                orphaned = int(
                    conn.execute("""SELECT COUNT(*) FROM relation_context_embeddings AS embedding
                           LEFT JOIN relations AS relation ON relation.id=embedding.relation_id
                           WHERE relation.id IS NULL""").fetchone()[0]
                )
        except (OSError, sqlite3.Error):
            return {
                "ok": False,
                "backend": "hnswlib" if HNSWLIB_AVAILABLE else "sqlite",
                "relation_count": 0,
                "embedding_count": 0,
                "missing_embeddings": 0,
                "orphaned_embeddings": 0,
                "index_required": bool(HNSWLIB_AVAILABLE),
                "index_exists": False,
                "rebuild_required": True,
                "error": "relation_embedding_projection_unreadable",
            }
        index_required = bool(HNSWLIB_AVAILABLE and relation_count)
        index_exists = (self.index_dir / "relation_index.bin").is_file()
        rebuild_required = self._rebuild_required()
        return {
            "ok": (
                missing == 0
                and orphaned == 0
                and not rebuild_required
                and (not index_required or index_exists)
            ),
            "backend": "hnswlib" if HNSWLIB_AVAILABLE else "sqlite",
            "relation_count": relation_count,
            "embedding_count": embedding_count,
            "missing_embeddings": missing,
            "orphaned_embeddings": orphaned,
            "index_required": index_required,
            "index_exists": index_exists,
            "rebuild_required": rebuild_required,
        }

    def rebuild_persistent_index(self) -> bool:
        """Force a durable ANN rebuild from authoritative SQLite vectors."""

        if not HNSWLIB_AVAILABLE:
            return False
        with sqlite_conn(str(self.db_path), timeout=10) as conn:
            row_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM relation_context_embeddings"
                ).fetchone()[0]
            )
        if row_count == 0:
            index_path = self.index_dir / "relation_index.bin"
            if index_path.exists():
                index_path.unlink()
            self._index = None
            self._rel_id_map.clear()
            self._hid_to_rel_id.clear()
            self._next_id = 0
            self._set_rebuild_required(False)
            return True
        index = hnswlib.Index(space="cosine", dim=DIM)
        if not self._rebuild_from_sqlite(index):
            return False
        index.set_ef(50)
        self._index = index
        self._set_rebuild_required(False)
        return True

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
                index.load_index(
                    str(index_path),
                    max_elements=max(
                        len(self._rel_id_map) * 2 + 1000,
                        RELATION_EMBEDDING_MANAGER__GET_HNSW_INDEX_LEN,
                    ),
                )
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("[RelationEmbedding] 加载索引失败，尝试从 SQLite 重建: %s", e)
                if self._rebuild_from_sqlite(index):
                    logger.info("[RelationEmbedding] 从 SQLite 重建 hnsw 索引成功")
                else:
                    index.init_index(max_elements=MAX_ELEMENTS, ef_construction=200, M=16)
        else:
            # [P1-5] 索引文件缺失但 SQLite 有数据时自动重建
            if self._rel_id_map and self._rebuild_from_sqlite(index):
                logger.info("[RelationEmbedding] 索引文件缺失，已从 SQLite 重建")
            else:
                index.init_index(max_elements=MAX_ELEMENTS, ef_construction=200, M=16)

        index.set_ef(50)
        self._index = index
        return index

    def add_relation_context(
        self,
        relation_id: int,
        context: str,
        model_version: str = "BAAI/bge-m3",
        force: bool = False,
    ) -> bool:
        """
        为关系上下文生成 embedding 并入库。

        Args:
            relation_id: 知识图谱 relations 表的 id
            context: 关联上下文文本
            model_version: embedding 模型版本
            force: 是否强制更新已存在的 embedding

        Returns:
            是否成功
        """
        if not self.repair_persistent_index():
            return False
        if not self.client or not context or not context.strip():
            return False

        # 检查是否已存在
        if relation_id in self._rel_id_map and not force:
            logger.debug("[RelationEmbedding] relation_id=%s 已存在，跳过", relation_id)
            return True

        # [P1-5] 更新已有记录时，先清理旧 hnsw id 避免重复向量
        old_hnsw_id = self._rel_id_map.pop(relation_id, None)
        if old_hnsw_id is not None:
            self._hid_to_rel_id.pop(old_hnsw_id, None)

        try:
            vec = self._embed_single_attributed(
                context,
                self._relation_subject_scopes(relation_id),
            )
            if not vec or sum(abs(x) for x in vec) == 0:
                return False

            import math

            norm = math.sqrt(sum(x * x for x in vec))
            if norm > 0:
                vec = [x / norm for x in vec]

            hnsw_id = self._next_id
            self._next_id += 1
            with sqlite_conn(str(self.db_path), timeout=10) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO relation_context_embeddings
                       (relation_id, embedding, model_version)
                       VALUES (?, ?, ?)""",
                    (relation_id, json.dumps(vec), model_version),
                )
                conn.commit()

            index = self._get_hnsw_index()
            if index is not None:
                index.add_items([vec], [hnsw_id])
                if old_hnsw_id is not None:
                    try:
                        index.mark_deleted(old_hnsw_id)
                    except RuntimeError as exc:
                        if not self._is_already_deleted_error(exc):
                            raise
                if self._batch_flush:
                    self._dirty_count += 1
                    self._maybe_flush()
                else:
                    self._save_index_atomic(index)

            self._rel_id_map[relation_id] = hnsw_id
            self._hid_to_rel_id[hnsw_id] = relation_id
            logger.debug("[RelationEmbedding] relation_id=%s → hnsw_id=%s", relation_id, hnsw_id)
            return True
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
            sqlite3.Error,
        ) as e:
            self._set_rebuild_required(True)
            logger.warning(
                "[RelationEmbedding] 添加失败 relation_id=%s: %s", relation_id, e, exc_info=True
            )
            return False

    @staticmethod
    def _is_already_deleted_error(exc: RuntimeError) -> bool:
        message = str(exc).lower()
        return "label not found" in message or "already deleted" in message

    def add_relation_contexts(
        self,
        contexts: Mapping[int, str],
        *,
        replace_ids: Optional[Set[int]] = None,
        model_version: str = "BAAI/bge-m3",
    ) -> Dict[str, int]:
        """Batch-embed and persist changed relation contexts in one client call."""

        if not self.repair_persistent_index():
            return {"total": len(contexts), "added": 0, "skipped": 0, "failed": len(contexts)}
        replace = set(replace_ids or set())
        pending: list[tuple[int, str]] = []
        skipped = 0
        for relation_id, context in contexts.items():
            text = str(context or "").strip()
            if not text:
                skipped += 1
                continue
            if relation_id in self._rel_id_map and relation_id not in replace:
                skipped += 1
                continue
            pending.append((int(relation_id), text))
        result = {
            "total": len(contexts),
            "added": 0,
            "skipped": skipped,
            "failed": 0,
        }
        if not pending:
            return result
        if not self.client:
            result["failed"] = len(pending)
            return result

        try:
            raw_vectors = self._embed_batch_attributed(
                [text for _, text in pending],
                [self._relation_subject_scopes(relation_id) for relation_id, _ in pending],
            )
        except (
            ProviderRequestError,
            ModelCallLedgerError,
            OSError,
            RuntimeError,
            ValueError,
        ):
            logger.warning("[RelationEmbedding] 批量 embedding 失败", exc_info=True)
            result["failed"] = len(pending)
            return result
        if len(raw_vectors) != len(pending):
            result["failed"] = len(pending)
            return result

        import math

        valid: list[tuple[int, list[float], int | None]] = []
        for (relation_id, _text), raw_vector in zip(pending, raw_vectors):
            if not raw_vector or sum(abs(value) for value in raw_vector) == 0:
                result["failed"] += 1
                continue
            norm = math.sqrt(sum(value * value for value in raw_vector))
            vector = [value / norm for value in raw_vector] if norm > 0 else list(raw_vector)
            old_hnsw_id = self._rel_id_map.get(relation_id)
            hnsw_id = self._next_id
            self._next_id += 1
            valid.append((relation_id, vector, old_hnsw_id))

        if not valid:
            return result
        try:
            with sqlite_conn(str(self.db_path), timeout=10) as conn:
                conn.executemany(
                    """INSERT OR REPLACE INTO relation_context_embeddings
                       (relation_id, embedding, model_version)
                       VALUES (?, ?, ?)""",
                    [
                        (relation_id, json.dumps(vector), model_version)
                        for relation_id, vector, _old_id in valid
                    ],
                )
                conn.commit()

            index = self._get_hnsw_index()
            new_ids = list(range(self._next_id - len(valid), self._next_id))
            if index is not None:
                required = index.get_current_count() + len(valid)
                if required > index.get_max_elements():
                    index.resize_index(max(required, index.get_max_elements() * 2))
                index.add_items([vector for _, vector, _ in valid], new_ids)
                for _relation_id, _vector, old_hnsw_id in valid:
                    if old_hnsw_id is None:
                        continue
                    try:
                        index.mark_deleted(old_hnsw_id)
                    except RuntimeError as exc:
                        if not self._is_already_deleted_error(exc):
                            raise

            for (relation_id, _vector, old_hnsw_id), hnsw_id in zip(valid, new_ids):
                if old_hnsw_id is not None:
                    self._hid_to_rel_id.pop(old_hnsw_id, None)
                self._rel_id_map[relation_id] = hnsw_id
                self._hid_to_rel_id[hnsw_id] = relation_id
            if index is not None:
                if self._batch_flush:
                    self._dirty_count += len(valid)
                    self._maybe_flush()
                else:
                    self._save_index_atomic(index)
            result["added"] = len(valid)
            return result
        except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error):
            self._set_rebuild_required(True)
            logger.warning("[RelationEmbedding] 批量写入失败", exc_info=True)
            result["failed"] += len(valid)
            return result

    def remove_relation_context(self, relation_id: int) -> bool:
        """删除关系的关联向量"""
        if relation_id not in self._rel_id_map:
            return False

        hnsw_id = self._rel_id_map.pop(relation_id)
        self._hid_to_rel_id.pop(hnsw_id, None)

        try:
            # 删除 SQLite 记录
            with sqlite_conn(str(self.db_path), timeout=10) as conn:
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
                    try:
                        index.mark_deleted(hnsw_id)
                    except RuntimeError as exc:
                        if not self._is_already_deleted_error(exc):
                            raise
                        logger.debug(
                            "[RelationEmbedding] relation_id=%s 的 HNSW label 已不存在",
                            relation_id,
                        )
                    if self._batch_flush:
                        self._dirty_count += 1
                        self._maybe_flush()
                    else:
                        self._save_index_atomic(index)

            return True
        except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error) as e:
            self._rel_id_map[relation_id] = hnsw_id
            self._hid_to_rel_id[hnsw_id] = relation_id
            logger.warning(
                "[RelationEmbedding] 删除失败 relation_id=%s: %s", relation_id, e, exc_info=True
            )
            return False

    def remove_relation_projection(self, relation_id: int, *, hnsw_id: int | None = None) -> bool:
        """Replay a durable delete even after its SQLite embedding row is gone."""

        if relation_id not in self._rel_id_map:
            if hnsw_id is None:
                return True
            self._rel_id_map[relation_id] = hnsw_id
            self._hid_to_rel_id[hnsw_id] = relation_id
            self._next_id = max(self._next_id, hnsw_id + 1)
        return self.remove_relation_context(relation_id)

    def flush(self) -> bool:
        """将内存中的 hnswlib 索引落盘。

        在 batch_flush 模式下，add/remove 只更新内存索引，调用 flush 时才持久化。
        立即模式下，每次 add/remove 已经自动落盘，flush 为无操作幂等调用。

        使用临时文件 + atomic rename，避免落盘过程中崩溃导致索引文件损坏。
        """
        with self._flush_lock:
            if self._closed:
                return False
            index = self._get_hnsw_index()
            if index is None or self._dirty_count == 0:
                self._last_flush = time.time()
                return True
            # 运行环境（如临时目录）已被清理时无需落盘
            if not self.index_dir.exists():
                self._last_flush = time.time()
                return True
            try:
                index_path = self.index_dir / "relation_index.bin"
                temp_path = self.index_dir / "relation_index.bin.tmp"
                index.save_index(str(temp_path))
                temp_path.replace(index_path)
                self._dirty_count = 0
                self._last_flush = time.time()
                logger.debug("[RelationEmbedding] 索引批量 flush 完成")
                return True
            except (OSError, RuntimeError) as e:
                self._set_rebuild_required(True)
                logger.warning("[RelationEmbedding] flush 失败: %s", e, exc_info=True)
                return False

    @contextmanager
    def defer_automatic_flush(self):
        """Suppress threshold/timer flushes until the caller persists the batch."""

        with self._flush_lock:
            self._automatic_flush_defer_depth += 1
        try:
            yield
        finally:
            with self._flush_lock:
                self._automatic_flush_defer_depth -= 1

    def close(self) -> None:
        """关闭并持久化索引，注销 atexit 钩子。"""
        try:
            self.flush()
            self._index = None
            self._closed = True
        except (OSError, RuntimeError) as e:
            logger.warning("[RelationEmbedding] close 失败: %s", e, exc_info=True)

    def _save_index_atomic(self, index) -> None:
        """原子方式保存 hnswlib 索引，避免写一半损坏。"""
        index_path = self.index_dir / "relation_index.bin"
        temp_path = self.index_dir / "relation_index.bin.tmp"
        index.save_index(str(temp_path))
        temp_path.replace(index_path)
        self._set_rebuild_required(False)

    def _maybe_flush(self, force: bool = False) -> None:
        """根据批量 flush 策略决定是否落盘。"""
        if not self._batch_flush:
            return
        with self._flush_lock:
            if self._automatic_flush_defer_depth:
                return
            if self._dirty_count >= self._flush_batch_size:
                self.flush()
            elif force or (time.time() - self._last_flush > self._flush_interval_seconds):
                self.flush()

    def _rebuild_from_sqlite(self, index) -> bool:
        """[P1-5] 从 SQLite relation_context_embeddings 重建 hnsw 索引。"""
        try:
            with sqlite_conn(str(self.db_path), timeout=10) as conn:
                rows = conn.execute(
                    """SELECT relation_id, id, embedding
                       FROM relation_context_embeddings ORDER BY id"""
                ).fetchall()
            if not rows:
                return False

            index.init_index(
                max_elements=max(
                    len(rows) * 2 + 1000, RELATION_EMBEDDING_MANAGER__REBUILD_FROM_SQLITE_M
                ),
                ef_construction=200,
                M=16,
            )
            relation_to_label: Dict[int, int] = {}
            label_to_relation: Dict[int, int] = {}
            for rel_id, row_id, emb_blob in rows:
                relation_id = int(rel_id)
                durable_label = int(row_id)
                vec = json.loads(emb_blob)
                index.add_items([vec], [durable_label])
                relation_to_label[relation_id] = durable_label
                label_to_relation[durable_label] = relation_id
            self._save_index_atomic(index)
            self._rel_id_map = relation_to_label
            self._hid_to_rel_id = label_to_relation
            self._next_id = max(label_to_relation, default=-1) + 1
            self._dirty_count = 0
            return True
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
            sqlite3.Error,
        ) as e:
            logger.warning("[RelationEmbedding] 从 SQLite 重建失败: %s", e, exc_info=True)
            return False

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """计算两个向量的 cosine 相似度。"""
        import math

        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def _search_sqlite_fallback(
        self,
        query_vec: List[float],
        top_k: int,
        *,
        allowed_relation_ids: Set[int] | None = None,
    ) -> List[Tuple[int, float]]:
        """[P1-5] hnsw 不可用时，遍历 SQLite 做 cosine 搜索。"""
        if allowed_relation_ids is not None and not allowed_relation_ids:
            return []
        results = []
        try:
            with sqlite_conn(str(self.db_path), timeout=10) as conn:
                if allowed_relation_ids is None:
                    rows = conn.execute(
                        "SELECT relation_id, embedding " "FROM relation_context_embeddings"
                    ).fetchall()
                else:
                    rows = []
                    ordered_ids = sorted(allowed_relation_ids)
                    for offset in range(0, len(ordered_ids), 500):
                        batch = ordered_ids[offset : offset + 500]
                        placeholders = ",".join("?" for _ in batch)
                        rows.extend(
                            conn.execute(
                                "SELECT relation_id, embedding "
                                "FROM relation_context_embeddings "
                                f"WHERE relation_id IN ({placeholders})",  # nosec B608
                                tuple(batch),
                            ).fetchall()
                        )
            for rel_id, emb_blob in rows:
                try:
                    vec = json.loads(emb_blob)
                    sim = self._cosine_similarity(query_vec, vec)
                    results.append((rel_id, sim))
                except (json.JSONDecodeError, ValueError):
                    continue
        except (OSError, sqlite3.Error) as e:
            logger.warning("[RelationEmbedding] SQLite fallback 搜索失败: %s", e, exc_info=True)
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def search(
        self,
        query: str,
        top_k: int = 20,
        *,
        subject_scope: tuple[str, str] | None = None,
        allowed_relation_ids: Set[int] | None = None,
    ) -> List[Tuple[int, float]]:
        """
        语义搜索关联上下文。

        Returns:
            [(relation_id, 相似度分数), ...] 按分数降序
        """
        normalized_allowlist = (
            {int(relation_id) for relation_id in allowed_relation_ids}
            if allowed_relation_ids is not None
            else None
        )
        if not self.client or not query or top_k <= 0 or normalized_allowlist == set():
            return []

        try:
            effective_scope = subject_scope
            if effective_scope is None:
                from core.telemetry.prompt_call_log import current_model_call_run

                if current_model_call_run() is None:
                    effective_scope = ("source", "relation_embedding_search")
            query_vec = self._embed_single_attributed(
                query,
                (effective_scope,) if effective_scope is not None else (),
            )
            if not query_vec:
                return []
        except (
            ProviderRequestError,
            ModelCallLedgerError,
            OSError,
            RuntimeError,
            ValueError,
        ) as e:
            logger.warning("[RelationEmbedding] query embedding 失败: %s", e, exc_info=True)
            return []

        import math

        norm = math.sqrt(sum(x * x for x in query_vec))
        if norm > 0:
            q = [x / norm for x in query_vec]
        else:
            q = query_vec

        # 批量模式下，查询前确保内存索引已落盘，保证查询可见性
        if self._batch_flush:
            self._maybe_flush(force=True)

        index = self._get_hnsw_index()
        if index is None or index.get_current_count() == 0:
            # [P1-5] hnsw 不可用或为空时回退到 SQLite cosine 遍历
            return self._search_sqlite_fallback(
                q,
                top_k,
                allowed_relation_ids=normalized_allowlist,
            )

        current_count = int(index.get_current_count())
        search_k = min(max(1, top_k), current_count)
        required_count = (
            min(top_k, len(normalized_allowlist)) if normalized_allowlist is not None else top_k
        )
        results_by_relation: Dict[int, float] = {}
        while True:
            labels, distances = index.knn_query([q], k=search_k)
            for label, dist in zip(labels[0], distances[0]):
                # [P1-29] 使用反向映射替代 O(N) 线性扫描
                rel_id = self._hid_to_rel_id.get(int(label))
                if rel_id is None:
                    continue
                if normalized_allowlist is not None and rel_id not in normalized_allowlist:
                    continue
                results_by_relation[rel_id] = max(
                    results_by_relation.get(rel_id, float("-inf")),
                    1.0 - float(dist),
                )
            if len(results_by_relation) >= required_count or search_k >= current_count:
                break
            search_k = min(current_count, max(search_k + 1, search_k * 2))

        results = sorted(
            results_by_relation.items(),
            key=lambda item: (-item[1], item[0]),
        )
        return results[:top_k]

    def get_stats(self) -> dict:
        """返回统计信息"""
        return {
            "total_relations": len(self._rel_id_map),
            "hnswlib_available": HNSWLIB_AVAILABLE,
            "client_available": self.client is not None,
            "index_dir": str(self.index_dir),
            "db_path": str(self.db_path),
        }
