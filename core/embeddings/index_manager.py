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

import hashlib
import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast
import uuid

from .index_persistence import EmbeddingIndexPersistenceMixin
from .siliconflow_client import (
    SiliconFlowEmbeddingClient,
    embedding_available,
    get_embedding_client,
    text_hash,
)
from core.config import ConfigProvider, get_config
from core.frontmatter import parse_frontmatter
from core.telemetry.prompt_call_log import ModelCallLedgerError
from core.telemetry.provider_request import ProviderRequestError

# Constants extracted from magic numbers
EMBEDDING_INDEX_MANAGER__EXTRACT_PAGE_TEXT_STRIP = 1200
MAX_CHUNK_2 = 1200
TEXT = 1200
EMBEDDING_INDEX_MANAGER__RERANK_RESULTS_PART = 3000
EMBEDDING_INDEX_MANAGER__RERANK_RESULTS_TEXT = 3000

logger = logging.getLogger(__name__)

HNSWLIB_AVAILABLE = False
try:
    import hnswlib

    HNSWLIB_AVAILABLE = True
except ImportError:
    logger.debug("[Embedding] hnswlib not installed, using memory fallback")


class EmbeddingIndexManager(EmbeddingIndexPersistenceMixin):
    """
    Wiki 页面 embedding 索引管理器

    使用场景：
    - core/app/context_search.py 的语义召回
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
    SIMILARITY_THRESHOLD = 0.5  # ANN 召回阈值：放宽以提升召回率，精排在融合/rerank 阶段完成

    def __init__(
        self,
        wiki_base: Optional[Path] = None,
        index_dir: Optional[Path] = None,
        client: Optional[SiliconFlowEmbeddingClient] = None,
        config: Optional[ConfigProvider] = None,
    ):
        cfg = config or get_config()
        self._runtime_config = cfg
        self.wiki_base = Path(wiki_base).expanduser() if wiki_base else cfg.wiki_dir
        if index_dir:
            self.index_dir = Path(index_dir).expanduser()
        elif wiki_base and self.wiki_base.resolve(strict=False) != Path(
            cfg.wiki_dir
        ).expanduser().resolve(strict=False):
            self.index_dir = self.wiki_base / ".kg" / "embedding_index"
        else:
            self.index_dir = cfg.database_dir / "embedding_index"

        self.client = client or get_embedding_client()
        self._index = None
        self._meta: Dict[str, Dict] = {}  # path -> {"id": int, "mtime": float, "hash": str}
        self._id_to_chunk: Dict[int, Tuple[str, int]] = {}
        self._memory_fallback: List[Tuple[str, int, List[float]]] = []  # hnswlib 不可用时使用
        self._use_rerank = cfg.get("embedding.use_rerank", True)

        self._index_path = self.index_dir / "wiki_index.bin"
        self._meta_path = self.index_dir / "wiki_meta.json"
        self._generation_manifest_path = self.index_dir / ".wiki_index_generation.json"
        self._generation_recovery_required = self._generation_manifest_path.is_file()

        self._load_meta()

    # ---- 元数据管理 ----
    # ---- 索引构建 ----

    def _extract_page_text(self, page_path: Path) -> str:
        """从 Markdown 文件提取用于 embedding 的文本"""
        try:
            content = page_path.read_text(encoding="utf-8", errors="ignore")
            _, body = parse_frontmatter(content)
            # 取前 1200 字符作为摘要（与 chunk 主流程保持一致）
            return body.strip()[:EMBEDDING_INDEX_MANAGER__EXTRACT_PAGE_TEXT_STRIP]
        except (OSError, IOError):
            return ""

    def _extract_chunks(self, page_path: Path) -> List[Dict[str, str]]:
        """[P1-6] 按 heading/段落切块，每块约 1000-1500 字符。"""
        try:
            content = page_path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, IOError):
            return []

        # 移除 frontmatter
        _, content = parse_frontmatter(content)
        if content is None:
            content = ""

        lines = content.splitlines()
        chunks = []
        current_heading = ""
        current_text = []  # type: ignore[var-annotated]
        current_len = 0
        MAX_CHUNK = MAX_CHUNK_2
        MIN_CHUNK = 200

        def _flush():
            nonlocal current_text, current_len
            if current_text:
                text = "\n".join(current_text).strip()
                if len(text) >= MIN_CHUNK:
                    chunks.append(
                        {
                            "text": text,
                            "heading": current_heading,
                        }
                    )
                current_text = []
                current_len = 0

        for line in lines:
            line_stripped = line.strip()
            # 检测 heading
            if line_stripped.startswith("## ") or line_stripped.startswith("### "):
                _flush()
                current_heading = line_stripped.lstrip("# ")
                continue
            if line_stripped.startswith("# "):
                _flush()
                current_heading = line_stripped.lstrip("# ")
                continue

            # 跳过空行和代码块标记
            if not line_stripped or line_stripped.startswith("```"):
                continue

            current_text.append(line_stripped)
            current_len += len(line_stripped)

            if current_len >= MAX_CHUNK:
                _flush()

        _flush()

        # 如果没有提取到有效 chunk，回退到前 1200 字符（与 chunk 主流程保持一致）
        if not chunks:
            text = content.strip()[:TEXT]
            if text:
                chunks.append({"text": text, "heading": ""})

        return chunks

    def _client_available(self) -> bool:
        """检查当前客户端是否可用"""
        if self.client is None:
            return False
        try:
            hc = self.client.health_check()
            return hc.get("available", False)  # type: ignore[no-any-return]
        except (
            ProviderRequestError,
            ModelCallLedgerError,
            OSError,
            RuntimeError,
            ValueError,
        ):
            return False

    # ---- 索引构建辅助方法 ----

    def _compute_chunk_hash(self, chunks: List[Dict[str, str]]) -> str:
        """计算页面 chunks 的聚合哈希，用于增量变更检测。"""
        return text_hash("\n".join(c["text"] for c in chunks))

    def _duplicate_label_paths(self) -> set[str]:
        """Return every metadata owner participating in a duplicate ANN label."""

        owners: Dict[int, set[str]] = {}
        for rel_path, meta in self._meta.items():
            for chunk in meta.get("chunks", []):
                label = chunk.get("id")
                if isinstance(label, int) and not isinstance(label, bool) and label >= 0:
                    owners.setdefault(label, set()).add(rel_path)
        return {rel_path for paths in owners.values() if len(paths) > 1 for rel_path in paths}

    @staticmethod
    def _labels_match_deterministic_order(
        meta: Dict[str, Any], chunks: List[Dict[str, str]], first_label: int
    ) -> bool:
        """Validate label and chunk positions against the canonical rebuild order."""

        persisted = meta.get("chunks", [])
        if len(persisted) != len(chunks):
            return False
        for chunk_idx, chunk in enumerate(persisted):
            label = chunk.get("id")
            stored_chunk_idx = chunk.get("chunk_idx")
            if (
                not isinstance(label, int)
                or isinstance(label, bool)
                or label != first_label + chunk_idx
                or not isinstance(stored_chunk_idx, int)
                or isinstance(stored_chunk_idx, bool)
                or stored_chunk_idx != chunk_idx
            ):
                return False
        return True

    def _classify_pages(
        self,
        pages: List[Path],
        force_full: bool,
    ) -> Tuple[
        Dict[str, Path],
        List[Tuple[str, Path, List[Dict[str, str]]]],
        List[Tuple[str, Path, List[Dict[str, str]]]],
        List[str],
        Dict[str, List[Dict[str, str]]],
    ]:
        """扫描页面并分类为新增/更新/删除，同时缓存 chunks 避免重复 IO。"""
        current_paths = {str(p.relative_to(self.wiki_base)): p for p in pages}
        to_add: List[Tuple[str, Path, List[Dict[str, str]]]] = []
        to_update: List[Tuple[str, Path, List[Dict[str, str]]]] = []
        to_remove: List[str] = []
        page_chunks: Dict[str, List[Dict[str, str]]] = {}
        requires_persisted_embeddings = self._select_backend(current_paths) == "memory"
        duplicate_label_paths = self._duplicate_label_paths()
        first_label = 0

        for rel_path, page_path in current_paths.items():
            meta = self._meta.get(rel_path)
            chunks = self._extract_chunks(page_path)
            page_chunks[rel_path] = chunks
            h = self._compute_chunk_hash(chunks)

            if meta is None:
                to_add.append((rel_path, page_path, chunks))
            elif (
                force_full
                or meta.get("hash") != h
                or rel_path in duplicate_label_paths
                or not self._labels_match_deterministic_order(meta, chunks, first_label)
                or (
                    requires_persisted_embeddings
                    and any("embedding" not in chunk for chunk in meta.get("chunks", []))
                )
            ):
                to_update.append((rel_path, page_path, chunks))
            first_label += len(chunks)

        for rel_path in list(self._meta.keys()):
            if rel_path not in current_paths:
                to_remove.append(rel_path)

        return current_paths, to_add, to_update, to_remove, page_chunks

    def _load_existing_hnsw_index(self, current_paths: Dict[str, Path]) -> None:
        """无变更时尝试加载已有的 hnswlib 索引文件。"""
        if self._index is None and HNSWLIB_AVAILABLE and self._index_path.exists():
            try:
                n_total = sum(
                    len(self._meta.get(rel_path, {}).get("chunks", []))
                    for rel_path in current_paths
                )
                index = hnswlib.Index(space="cosine", dim=self.DIM)
                index.load_index(str(self._index_path), max_elements=max(n_total * 2, 100))
                index.set_ef(self.EF_SEARCH)
                self._index = index
                logger.debug("[Embedding] 加载已有 hnswlib 索引")
            except (OSError, RuntimeError, ValueError) as e:
                logger.warning("[Embedding] 加载已有索引失败，将重建: %s", e)

    def _load_persisted_index_for_search(self) -> bool:
        """Load an already-built index without reading or embedding Wiki bodies."""

        if self._generation_manifest_path.is_file():
            self._generation_recovery_required = True
            return False
        if self._index is not None or self._memory_fallback:
            return True
        chunk_count = sum(len(meta.get("chunks", [])) for meta in self._meta.values())
        if chunk_count <= 0:
            return False
        if HNSWLIB_AVAILABLE and self._index_path.is_file():
            try:
                index = hnswlib.Index(space="cosine", dim=self.DIM)
                index.load_index(
                    str(self._index_path),
                    max_elements=max(chunk_count * 2, 100),
                )
                if index.get_current_count() != chunk_count:
                    return False
                index.set_ef(self.EF_SEARCH)
                self._index = index
                self._rebuild_id_to_chunk_from_meta()
                return len(self._id_to_chunk) == chunk_count
            except (OSError, RuntimeError, ValueError):
                logger.warning("[Embedding] persisted search index is invalid", exc_info=True)
                return False
        self._restore_memory_fallback_from_meta()
        return len(self._memory_fallback) == chunk_count

    def persisted_search_available(self) -> bool:
        """Load and validate the persisted read-only index without provider calls."""

        return self._load_persisted_index_for_search()

    @staticmethod
    def _page_subject_scope(page_path: Path) -> tuple[str, str]:
        """Return the exact Wiki asset whose visible bytes are embedded."""
        return "path", str(page_path.expanduser().resolve(strict=False))

    def _client_uses_ledger_subjects(self) -> bool:
        """Keep existing in-process test doubles compatible with the public client.

        Production uses ``SiliconFlowEmbeddingClient`` and always receives the
        explicit attribution arguments.  Existing fakes are non-provider test
        doubles, so they do not model a billable boundary and retain their
        narrow historical call signatures.
        """
        return isinstance(self.client, SiliconFlowEmbeddingClient)

    def _embed_batch_attributed(
        self,
        texts: List[str],
        subject_scopes: List[tuple[tuple[str, str], ...]],
    ) -> List[Optional[List[float]]]:
        if self._client_uses_ledger_subjects():
            return self.client.embed(  # type: ignore[union-attr, return-value]
                texts,
                subject_scopes=subject_scopes,
            )
        return self.client.embed(texts)  # type: ignore[union-attr, return-value]

    def _embed_single_attributed(
        self,
        text: str,
        subject_scope: tuple[str, str] | None,
    ) -> List[float]:
        if self._client_uses_ledger_subjects():
            return self.client.embed_single(  # type: ignore[union-attr]
                text,
                subject_scope=subject_scope,
            )
        return self.client.embed_single(text)  # type: ignore[union-attr]

    def _rerank_attributed(
        self,
        query: str,
        documents: List[str],
        top_k: int,
        subject_scopes: tuple[tuple[str, str], ...] | None,
    ) -> List[Tuple[int, float]]:
        if self._client_uses_ledger_subjects():
            return self.client.rerank(  # type: ignore[union-attr]
                query=query,
                documents=documents,
                top_n=top_k,
                subject_scopes=subject_scopes,
            )
        return self.client.rerank(  # type: ignore[union-attr]
            query=query,
            documents=documents,
            top_n=top_k,
        )

    def _collect_texts_to_embed(
        self,
        to_add: List[Tuple[str, Path, List[Dict[str, str]]]],
        to_update: List[Tuple[str, Path, List[Dict[str, str]]]],
        *,
        force_full: bool,
        backend: str,
    ) -> Tuple[
        List[str],
        List[Tuple[str, int, str]],
        List[tuple[tuple[str, str], ...]],
    ]:
        """收集需要 embedding 的 chunk 文本及顺序信息。"""
        texts_to_embed: List[str] = []
        chunk_order: List[Tuple[str, int, str]] = []
        chunk_subject_scopes: List[tuple[tuple[str, str], ...]] = []
        duplicate_label_paths = self._duplicate_label_paths()
        provider_items = list(to_add)
        provider_items.extend(
            item
            for item in to_update
            if self._requires_provider_embeddings(
                item[0],
                item[2],
                force_full=force_full,
                backend=backend,
                duplicate_label_paths=duplicate_label_paths,
            )
        )
        for rel_path, page_path, chunks in provider_items:
            page_subject_scope = self._page_subject_scope(page_path)
            for chunk_idx, chunk in enumerate(chunks):
                texts_to_embed.append(chunk["text"])
                chunk_order.append((rel_path, chunk_idx, chunk.get("heading", "")))
                chunk_subject_scopes.append((page_subject_scope,))
        return texts_to_embed, chunk_order, chunk_subject_scopes

    def _requires_provider_embeddings(
        self,
        rel_path: str,
        chunks: List[Dict[str, str]],
        *,
        force_full: bool,
        backend: str,
        duplicate_label_paths: set[str],
    ) -> bool:
        """Separate semantic rebuilds from vector-preserving label compaction."""

        if force_full or rel_path in duplicate_label_paths:
            return True
        meta = self._meta.get(rel_path)
        if meta is None or meta.get("hash") != self._compute_chunk_hash(chunks):
            return True
        old_chunks = meta.get("chunks", [])
        if len(old_chunks) != len(chunks):
            return True
        if backend == "memory":
            return any(
                not isinstance(chunk.get("embedding"), list) or len(chunk["embedding"]) != self.DIM
                for chunk in old_chunks
            )
        labels: set[int] = set()
        for chunk_idx, chunk in enumerate(old_chunks):
            label = chunk.get("id")
            if (
                not isinstance(label, int)
                or isinstance(label, bool)
                or label < 0
                or label in labels
                or chunk.get("chunk_idx") != chunk_idx
            ):
                return True
            labels.add(label)
        return False

    def _embed_texts(
        self,
        texts_to_embed: List[str],
        *,
        subject_scopes: List[tuple[tuple[str, str], ...]],
    ) -> List[Optional[List[float]]]:
        """批量获取 embedding，失败时回退到逐条获取。"""
        if not texts_to_embed:
            return []
        try:
            return self._embed_batch_attributed(texts_to_embed, subject_scopes)
        except ModelCallLedgerError:
            raise
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            logger.warning(
                "[Embedding] Batch embed failed, falling back to individual", exc_info=True
            )
        embeddings: List[Optional[List[float]]] = []
        for text, entry_subject_scopes in zip(texts_to_embed, subject_scopes):
            try:
                embeddings.append(
                    self._embed_single_attributed(
                        text,
                        entry_subject_scopes[0] if entry_subject_scopes else None,
                    )
                )
            except ModelCallLedgerError:
                raise
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
            ):
                logger.warning(
                    "[Embedding] Individual embed failed (len=%d), skipping",
                    len(text),
                    exc_info=True,
                )
                embeddings.append(None)  # type: ignore[arg-type]
        return embeddings

    def _select_backend(self, current_paths: Dict[str, Path]) -> str:
        """根据页面数量与 hnswlib 可用性选择索引后端。"""
        return "hnswlib" if HNSWLIB_AVAILABLE and len(current_paths) >= 10 else "memory"

    @staticmethod
    def _manifest_sha256(payload: Any) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _artifact_sha256(path: Path) -> str:
        if not path.is_file():
            return ""
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def reconciliation_plan(self, *, force_full: bool = False) -> Dict[str, Any]:
        """Return a frozen read-only plan for local or provider-bound repair."""

        eligible_pages, excluded_pages = self._scan_indexable_wiki_pages()
        current_paths, to_add, to_update, to_remove, page_chunks = self._classify_pages(
            eligible_pages,
            force_full,
        )
        backend = self._select_backend(current_paths)
        texts, chunk_order, _subject_scopes = self._collect_texts_to_embed(
            to_add,
            to_update,
            force_full=force_full,
            backend=backend,
        )
        provider_paths = sorted({rel_path for rel_path, _idx, _heading in chunk_order})
        eligible_manifest = [
            (
                rel_path,
                self._compute_chunk_hash(page_chunks[rel_path]),
                len(page_chunks[rel_path]),
            )
            for rel_path in current_paths
        ]
        excluded_manifest = sorted(
            (reason, path) for reason, paths in excluded_pages.items() for path in paths
        )
        payload: Dict[str, Any] = {
            "schema_version": "mnemos.wiki_search_index_reconciliation.v1",
            "force_full": bool(force_full),
            "backend": backend,
            "eligible_page_count": len(current_paths),
            "eligible_chunk_count": sum(len(value) for value in page_chunks.values()),
            "excluded_page_count": len(excluded_manifest),
            "excluded_pages_by_reason": {
                reason: len(paths) for reason, paths in sorted(excluded_pages.items())
            },
            "existing_meta_page_count": len(self._meta),
            "add_page_count": len(to_add),
            "update_page_count": len(to_update),
            "remove_page_count": len(to_remove),
            "provider_required_page_count": len(provider_paths),
            "provider_required_chunk_count": len(texts),
            "local_vector_reuse_page_count": len(to_update) - len(provider_paths),
            "eligible_manifest_sha256": self._manifest_sha256(eligible_manifest),
            "excluded_manifest_sha256": self._manifest_sha256(excluded_manifest),
            "provider_manifest_sha256": self._manifest_sha256(provider_paths),
            "index_artifact_sha256": self._artifact_sha256(self._index_path),
            "meta_artifact_sha256": self._artifact_sha256(self._meta_path),
        }
        payload["plan_hash"] = self._manifest_sha256(payload)
        return payload

    def build_index(self, force_full: bool = False) -> Dict[str, Any]:
        """Build one asset-attributable run around all index provider calls."""
        if self._generation_manifest_path.is_file():
            self._recover_persisted_generation()
            self._load_meta()
        # A label-only compaction or no-op is not a model call.  Keep it out of
        # the model-call ledger, then re-scan under a local-only guard so source
        # drift cannot smuggle a provider request into this path.
        preview = self.reconciliation_plan(force_full=force_full)
        allow_provider = int(preview["provider_required_chunk_count"]) > 0
        previous_meta = self._meta
        previous_index = self._index
        previous_id_to_chunk = dict(self._id_to_chunk)
        previous_memory_fallback = list(self._memory_fallback)
        try:
            # Memory fallback historically replaces page entries in place.
            # Isolate those writes so a later ledger control-plane error can
            # restore the exact pre-build view instead of exposing half a build.
            self._meta = dict(previous_meta)
            result = self._build_index(
                force_full=force_full,
                allow_provider=allow_provider,
            )
            if result.get("status") == "provider_required":
                self._meta = previous_meta
            return result
        except ModelCallLedgerError:
            self._meta = previous_meta
            self._index = previous_index
            self._id_to_chunk = previous_id_to_chunk
            self._memory_fallback = previous_memory_fallback
            logger.warning("[Embedding] Index build blocked by model-call ledger")
            return {
                "status": "blocked",
                "reason": "model_call_ledger",
                "added": 0,
                "updated": 0,
                "removed": 0,
                "total": len(self._meta),
            }
        except BaseException:
            self._meta = previous_meta
            self._index = previous_index
            self._id_to_chunk = previous_id_to_chunk
            self._memory_fallback = previous_memory_fallback
            raise

    def _build_index(
        self,
        force_full: bool = False,
        *,
        allow_provider: bool = True,
    ) -> Dict[str, Any]:
        """
        构建或增量更新索引

        Args:
            force_full: 强制全量重建

        Returns:
            {"added": int, "updated": int, "removed": int, "total": int}
        """
        eligible_pages, excluded_pages = self._scan_indexable_wiki_pages()
        current_paths, to_add, to_update, to_remove, page_chunks = self._classify_pages(
            eligible_pages, force_full
        )
        exclusion_counts = {reason: len(paths) for reason, paths in sorted(excluded_pages.items())}

        total_changes = len(to_add) + len(to_update) + len(to_remove)
        if total_changes == 0:
            if self._select_backend(current_paths) == "hnswlib":
                self._load_existing_hnsw_index(current_paths)
                self._rebuild_id_to_chunk_from_meta()
            else:
                self._restore_memory_fallback_from_meta()
            return {
                "added": 0,
                "updated": 0,
                "removed": 0,
                "total": len(current_paths),
                "status": "no_change",
                "provider_required_chunks": 0,
                "excluded_pages_by_reason": exclusion_counts,
            }

        logger.info(
            "[Embedding] 索引更新: +%s ~%s -%s (total=%s)",
            len(to_add),
            len(to_update),
            len(to_remove),
            len(current_paths),
        )

        backend = self._select_backend(current_paths)
        texts_to_embed, chunk_order, chunk_subject_scopes = self._collect_texts_to_embed(
            to_add,
            to_update,
            force_full=force_full,
            backend=backend,
        )
        if texts_to_embed and not allow_provider:
            return {
                "added": 0,
                "updated": 0,
                "removed": 0,
                "total": len(self._meta),
                "status": "provider_required",
                "reason": "source_changed_after_local_only_plan",
                "provider_required_chunks": len(texts_to_embed),
                "excluded_pages_by_reason": exclusion_counts,
            }
        if texts_to_embed and not self._client_available():
            logger.warning(
                "[Embedding] provider required for %s changed chunks but unavailable",
                len(texts_to_embed),
            )
            return {
                "added": 0,
                "updated": 0,
                "removed": 0,
                "total": len(self._meta),
                "status": "no_client",
                "provider_required_chunks": len(texts_to_embed),
                "excluded_pages_by_reason": exclusion_counts,
            }
        embeddings: List[Optional[List[float]]] = []
        if texts_to_embed:
            from core.telemetry.prompt_call_log import (
                current_model_call_run,
                model_call_run_scope,
            )

            root_scope = (
                None if current_model_call_run() is not None else ("source", "embedding_indexer")
            )
            # Starting a durable run is itself a write.  Do it only after an
            # exact rescan proves that at least one provider call will occur.
            with model_call_run_scope(
                self._runtime_config,
                "embedding_index_build",
                subject_scope=root_scope,
            ):
                embeddings = self._embed_texts(
                    texts_to_embed,
                    subject_scopes=chunk_subject_scopes,
                )

        self.index_dir.mkdir(parents=True, exist_ok=True)

        if backend == "hnswlib":
            self._update_hnsw_index(
                to_add, to_update, to_remove, chunk_order, embeddings, current_paths, page_chunks
            )
        else:
            self._update_memory_fallback(
                to_add, to_update, to_remove, chunk_order, embeddings, current_paths
            )

        self._persist_index_generation(backend)
        return {
            "added": len(to_add),
            "updated": len(to_update),
            "removed": len(to_remove),
            "total": len(current_paths),
            "status": "ok",
            "backend": backend,
            "provider_required_chunks": len(texts_to_embed),
            "excluded_pages_by_reason": exclusion_counts,
        }

    def audit_coverage(self) -> Dict[str, Any]:
        """Compare current Wiki chunks with persisted search metadata and index presence."""

        pages, excluded_pages = self._scan_indexable_wiki_pages()
        expected: Dict[str, int] = {}
        for page in pages:
            rel_path = str(page.relative_to(self.wiki_base))
            expected[rel_path] = len(self._extract_chunks(page))
        actual = {rel_path: len(meta.get("chunks", [])) for rel_path, meta in self._meta.items()}
        invalid_chunks = []
        seen_labels: set[int] = set()
        expected_label = 0
        for rel_path, meta in sorted(self._meta.items(), key=lambda item: Path(item[0])):
            for expected_chunk_idx, chunk in enumerate(meta.get("chunks", [])):
                label = chunk.get("id")
                reason = ""
                if not isinstance(label, int) or isinstance(label, bool) or label < 0:
                    reason = "missing_or_invalid_label"
                elif label in seen_labels:
                    reason = "duplicate_label"
                elif label != expected_label:
                    reason = "wrong_label_order"
                elif chunk.get("chunk_idx") != expected_chunk_idx:
                    reason = "wrong_chunk_order"
                else:
                    seen_labels.add(label)
                if reason:
                    invalid_chunks.append(
                        {
                            "page": rel_path,
                            "chunk_idx": int(chunk.get("chunk_idx", 0)),
                            "reason": reason,
                        }
                    )
                expected_label += 1
        missing_pages = sorted(set(expected) - set(actual))
        orphan_pages = sorted(set(actual) - set(expected))
        chunk_mismatches = [
            {
                "page": rel_path,
                "expected": expected[rel_path],
                "actual": actual.get(rel_path, 0),
            }
            for rel_path in sorted(set(expected) & set(actual))
            if expected[rel_path] != actual[rel_path]
        ]
        expected_chunks = sum(expected.values())
        actual_chunks = sum(actual.values())
        backend = self._select_backend({str(page): page for page in pages})
        if backend == "memory" and not self._generation_manifest_path.is_file():
            # A fresh verifier must reconstruct the durable memory backend from
            # metadata before deciding whether the index exists.  Otherwise a
            # valid restart state is indistinguishable from an empty process cache.
            self._restore_memory_fallback_from_meta()
        if not expected_chunks:
            index_exists = True
        elif backend == "hnswlib":
            index_exists = self._index_path.is_file()
        else:
            index_exists = len(self._memory_fallback) == expected_chunks
        return {
            "pages": len(expected),
            "expected_chunks": expected_chunks,
            "actual_chunks": actual_chunks,
            "missing_pages": missing_pages,
            "orphan_pages": orphan_pages,
            "chunk_mismatches": chunk_mismatches,
            "invalid_chunks": invalid_chunks,
            "index_exists": index_exists,
            "excluded_pages_by_reason": {
                reason: len(paths) for reason, paths in sorted(excluded_pages.items())
            },
            "excluded_page_count": sum(len(paths) for paths in excluded_pages.values()),
            "generation_recovery_required": self._generation_manifest_path.is_file(),
            "ok": not missing_pages
            and not orphan_pages
            and not chunk_mismatches
            and not invalid_chunks
            and index_exists
            and not self._generation_manifest_path.is_file(),
        }

    # ---- hnswlib 索引更新辅助方法 ----

    def _compute_total_chunks(
        self,
        current_paths: Dict[str, Path],
        to_remove: List[str],
        page_chunks: Dict[str, List[Dict[str, str]]],
    ) -> int:
        """估算索引需要容纳的总 chunk 数（含空页面占位）。"""
        total = 0
        for rel_path, page_path in current_paths.items():
            if rel_path in to_remove:
                continue
            chunks = page_chunks.get(rel_path) or self._extract_chunks(page_path)
            total += max(len(chunks), 1)
        return total

    def _create_hnsw_index(self, total_chunks: int) -> Any:
        """初始化新的 hnswlib 索引实例。"""
        index = hnswlib.Index(space="cosine", dim=self.DIM)
        index.init_index(
            max_elements=max(total_chunks * 2, 100),
            ef_construction=self.EF_CONSTRUCTION,
            M=self.M,
        )
        index.set_ef(self.EF_SEARCH)
        return index

    def _build_chunk_embedding_map(
        self,
        chunk_order: List[Tuple[str, int, str]],
        embeddings: List[Optional[List[float]]],
    ) -> Dict[Tuple[str, int], Optional[List[float]]]:
        """将 chunk 顺序与 embedding 列表映射为 (rel_path, chunk_idx) -> embedding。"""
        return {
            (rel_path, chunk_idx): emb
            for (rel_path, chunk_idx, _heading), emb in zip(chunk_order, embeddings)
        }

    def _load_persisted_hnsw_embeddings(
        self,
    ) -> Dict[Tuple[str, int], List[float]]:
        """Load existing vectors by their durable path/chunk metadata keys."""

        if not HNSWLIB_AVAILABLE or not self._index_path.is_file() or not self._meta:
            return {}
        index = self._index
        if index is None:
            try:
                index = hnswlib.Index(space="cosine", dim=self.DIM)
                chunk_count = sum(len(meta.get("chunks", [])) for meta in self._meta.values())
                index.load_index(str(self._index_path), max_elements=max(chunk_count * 2, 100))
            except (OSError, RuntimeError, ValueError):
                logger.warning("[Embedding] 无法加载旧 HNSW 向量用于增量复用", exc_info=True)
                return {}

        keys: List[Tuple[str, int]] = []
        ids: List[int] = []
        for rel_path, meta in self._meta.items():
            for chunk in meta.get("chunks", []):
                if "id" not in chunk:
                    logger.warning(
                        "[Embedding] 跳过缺少 durable label 的旧索引元数据: %s[%s]",
                        rel_path,
                        chunk.get("chunk_idx", 0),
                    )
                    continue
                keys.append((rel_path, int(chunk.get("chunk_idx", 0))))
                ids.append(int(chunk["id"]))
        if not ids:
            return {}
        try:
            vectors = index.get_items(ids)
        except (RuntimeError, ValueError):
            logger.warning("[Embedding] 无法读取旧 HNSW 向量用于增量复用", exc_info=True)
            return {}
        return {key: [float(value) for value in vector] for key, vector in zip(keys, vectors)}

    def _resolve_chunk_embedding(
        self,
        rel_path: str,
        chunk_idx: int,
        chunk: Dict[str, str],
        chunk_emb_map: Dict[Tuple[str, int], Optional[List[float]]],
        old_chunks: List[Dict[str, Any]],
        persisted_embeddings: Dict[Tuple[str, int], List[float]],
    ) -> Optional[List[float]]:
        """获取指定 chunk 的 embedding：优先新算，其次旧 meta，最后单条 embedding。"""
        emb = chunk_emb_map.get((rel_path, chunk_idx))
        if emb is not None:
            return emb
        persisted = persisted_embeddings.get((rel_path, chunk_idx))
        if persisted is not None:
            return persisted
        if chunk_idx < len(old_chunks) and "embedding" in old_chunks[chunk_idx]:
            return old_chunks[chunk_idx]["embedding"]  # type: ignore[no-any-return]
        try:
            return self._embed_single_attributed(
                chunk["text"],
                self._page_subject_scope(self.wiki_base / rel_path),
            )
        except ModelCallLedgerError:
            raise
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ):
            logger.warning(
                "Failed to embed chunk %s[%d], skipping",
                rel_path,
                chunk_idx,
                exc_info=True,
            )
            return None

    def _normalize_embedding(self, emb: Optional[List[float]]) -> Optional[List[float]]:
        """对 embedding 做 L2 归一化；零向量或 None 时返回 None。"""
        if emb is None or sum(abs(x) for x in emb) == 0:
            return None
        norm = math.sqrt(sum(x * x for x in emb))
        if norm == 0:
            return None
        return [x / norm for x in emb]

    def _save_hnsw_index(self, index: Any) -> None:
        """持久化 hnswlib 索引到磁盘。"""
        self.index_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.index_dir / f".{self._index_path.name}.{uuid.uuid4().hex}.tmp"
        try:
            index.save_index(str(temporary))
            os.replace(temporary, self._index_path)
            self._fsync_index_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def _build_id_to_chunk(self, new_meta: Dict[str, Dict[str, Any]]) -> None:
        """[P1-028] 构建 id -> (rel_path, chunk_idx) 映射。"""
        self._id_to_chunk = {}
        for rel_path, meta in new_meta.items():
            for chunk_info in meta.get("chunks", []):
                chunk_id = chunk_info["id"]
                chunk_idx = chunk_info.get("chunk_idx", 0)
                self._id_to_chunk[chunk_id] = (rel_path, chunk_idx)

    def _update_hnsw_index(
        self,
        to_add: List[Tuple[str, Path, List[Dict[str, str]]]],
        to_update: List[Tuple[str, Path, List[Dict[str, str]]]],
        to_remove: List[str],
        chunk_order: List[Tuple[str, int, str]],
        embeddings: List[Optional[List[float]]],
        current_paths: Dict[str, Path],
        page_chunks: Dict[str, List[Dict[str, str]]],
    ) -> None:
        """[P1-6] 使用 hnswlib 更新索引（chunk 级别）。"""
        total_chunks = self._compute_total_chunks(current_paths, to_remove, page_chunks)
        persisted_embeddings = self._load_persisted_hnsw_embeddings()
        changed_paths = {rel_path for rel_path, _chunk_idx, _heading in chunk_order}
        index = self._create_hnsw_index(total_chunks)

        new_meta: Dict[str, Dict[str, Any]] = {}
        idx = 0
        all_embeddings: List[List[float]] = []

        chunk_emb_map = self._build_chunk_embedding_map(chunk_order, embeddings)

        for rel_path, page_path in current_paths.items():
            if rel_path in to_remove:
                continue

            chunks = page_chunks.get(rel_path) or self._extract_chunks(page_path)
            h = self._compute_chunk_hash(chunks)
            mtime = page_path.stat().st_mtime

            old_meta = self._meta.get(rel_path, {})
            old_chunks = old_meta.get("chunks", [])

            chunk_ids: List[Dict[str, Any]] = []
            for chunk_idx, chunk in enumerate(chunks):
                emb = self._resolve_chunk_embedding(
                    rel_path,
                    chunk_idx,
                    chunk,
                    chunk_emb_map,
                    old_chunks,
                    persisted_embeddings if rel_path not in changed_paths else {},
                )
                norm_emb = self._normalize_embedding(emb)
                if norm_emb is None:
                    logger.warning("Skipping zero-vector chunk %s[%d]", rel_path, chunk_idx)
                    continue

                all_embeddings.append(norm_emb)
                chunk_ids.append(
                    {
                        "id": idx,
                        "chunk_idx": chunk_idx,
                        "heading": chunk.get("heading", ""),
                    }
                )
                idx += 1

            new_meta[rel_path] = {
                "mtime": mtime,
                "hash": h,
                "chunks": chunk_ids,
            }

        self._build_id_to_chunk(new_meta)

        if all_embeddings:
            data = [[float(x) for x in vec] for vec in all_embeddings]
            index.add_items(data, list(range(len(data))))

        self._index = index
        self._meta = new_meta

    def _update_memory_fallback(
        self,
        to_add: List[Tuple[str, Path, List[Dict[str, str]]]],
        to_update: List[Tuple[str, Path, List[Dict[str, str]]]],
        to_remove: List[str],
        chunk_order: List[Tuple[str, int, str]],
        embeddings: List[Optional[List[float]]],
        current_paths: Dict[str, Path],
    ) -> None:
        """[P1-6] hnswlib 不可用时使用内存列表（chunk 级别）"""
        new_fallback: List[Tuple[str, int, List[float]]] = []
        # Build a complete replacement map.  Mutating ``self._meta`` in place
        # leaves entries for deleted pages behind, which both leaks a removed
        # Wiki page into durable search metadata and makes the lifecycle
        # consumer retry forever on an orphan-page coverage gap.
        new_meta: Dict[str, Dict[str, Any]] = {}
        chunk_emb_map: Dict[Tuple[str, int], Optional[List[float]]] = {}
        for i, (rp, ci, heading) in enumerate(chunk_order):
            chunk_emb_map[(rp, ci)] = embeddings[i]

        for rel_path, page_path in current_paths.items():
            if rel_path in to_remove:
                continue

            chunks = self._extract_chunks(page_path)
            chunk_texts = [c["text"] for c in chunks]
            h = text_hash("\n".join(chunk_texts))
            mtime = page_path.stat().st_mtime

            kept_chunks = []
            for chunk_idx, chunk in enumerate(chunks):
                emb = chunk_emb_map.get((rel_path, chunk_idx))
                if emb is None:
                    old_meta = self._meta.get(rel_path, {})
                    old_chunks = old_meta.get("chunks", [])
                    if chunk_idx < len(old_chunks) and "embedding" in old_chunks[chunk_idx]:
                        emb = old_chunks[chunk_idx]["embedding"]
                    else:
                        try:
                            emb = self._embed_single_attributed(
                                chunk["text"],
                                self._page_subject_scope(page_path),
                            )
                        except ModelCallLedgerError:
                            raise
                        except (
                            OSError,
                            ValueError,
                            TypeError,
                            KeyError,
                            ImportError,
                            AttributeError,
                            RuntimeError,
                        ):
                            logger.warning(
                                "Failed to embed chunk %s[%d], skipping",
                                rel_path,
                                chunk_idx,
                                exc_info=True,
                            )
                            continue

                if emb is None or sum(abs(x) for x in emb) == 0:
                    logger.warning("Skipping zero-vector chunk %s[%d]", rel_path, chunk_idx)
                    continue

                new_fallback.append((rel_path, chunk_idx, cast(List[float], emb)))
                kept_chunks.append(
                    {
                        "id": len(new_fallback) - 1,
                        "chunk_idx": chunk_idx,
                        "heading": chunk.get("heading", ""),
                        "embedding": cast(List[float], emb),
                    }
                )

            new_meta[rel_path] = {
                "mtime": mtime,
                "hash": h,
                "chunks": kept_chunks,
            }

        # [P1-6] 构建 id -> (rel_path, chunk_idx) 映射（内存模式）
        self._id_to_chunk = {}
        idx = 0
        for rel_path, chunk_idx, emb in new_fallback:
            self._id_to_chunk[idx] = (rel_path, chunk_idx)
            idx += 1

        self._memory_fallback = new_fallback
        self._meta = new_meta

    # ---- 搜索 ----

    def _embed_query(
        self,
        query: str,
        *,
        subject_scope: tuple[str, str] | None = None,
    ) -> Optional[List[float]]:
        """嵌入查询并返回向量；失败或零向量时返回 None。"""
        if self.client is None:
            return None
        try:
            query_vec = self._embed_single_attributed(query, subject_scope)
        except (
            ProviderRequestError,
            ModelCallLedgerError,
            OSError,
            RuntimeError,
            ValueError,
        ) as e:
            logger.warning("[Embedding] query embedding 失败: %s", e, exc_info=True)
            return None
        if not query_vec or sum(abs(x) for x in query_vec) == 0:
            return None
        return query_vec

    def _hnsw_lookup(
        self,
        query_vec: List[float],
        threshold: float,
        use_rerank: bool,
        top_k: int,
        allowed_page_paths: set[str] | None = None,
    ) -> List[Tuple[str, int, float]]:
        """Use ANN recall with ACL-aware refill before page-level top-k."""
        assert self._index is not None
        total = self._index.get_current_count()
        if total <= 0:
            return []
        recall_k = (
            min(top_k * 3, self._index.get_current_count())
            if use_rerank
            else min(top_k * 2, self._index.get_current_count())
        )
        recall_k = min(max(recall_k, min(20, total)), total)
        import math

        norm = math.sqrt(sum(x * x for x in query_vec))
        q = [x / norm for x in query_vec] if norm > 0 else query_vec

        while True:
            chunk_results: List[Tuple[str, int, float]] = []
            labels, distances = self._index.knn_query([q], k=recall_k)
            for label, dist in zip(labels[0], distances[0]):
                sim = 1.0 - float(dist)
                if sim < threshold:
                    continue
                mapping = self._id_to_chunk.get(int(label))
                if not mapping:
                    continue
                rel_path, chunk_idx = mapping
                if allowed_page_paths is not None and rel_path not in allowed_page_paths:
                    continue
                chunk_results.append((rel_path, chunk_idx, sim))
            authorized_pages = {item[0] for item in chunk_results}
            if allowed_page_paths is None or len(authorized_pages) >= top_k or recall_k >= total:
                return chunk_results
            recall_k = min(total, max(recall_k + 1, recall_k * 2))

    def _fallback_lookup(
        self,
        query_vec: List[float],
        threshold: float,
        allowed_page_paths: set[str] | None = None,
    ) -> List[Tuple[str, int, float]]:
        """hnswlib 不可用时使用内存 fallback 线性搜索。"""
        chunk_results: List[Tuple[str, int, float]] = []
        import math

        q_norm = math.sqrt(sum(x * x for x in query_vec))
        idx = 0
        for rel_path, chunk_idx, vec in self._memory_fallback:  # type: ignore[misc]
            if allowed_page_paths is not None and rel_path not in allowed_page_paths:
                idx += 1
                continue
            dot = sum(x * y for x, y in zip(query_vec, vec))
            v_norm = math.sqrt(sum(x * x for x in vec))
            if q_norm == 0 or v_norm == 0:
                idx += 1
                continue
            sim = dot / (q_norm * v_norm)
            if sim >= threshold:
                chunk_results.append((rel_path, chunk_idx, sim))
            idx += 1
        return chunk_results

    def _lookup_chunks(
        self,
        query_vec: List[float],
        threshold: float,
        use_rerank: bool,
        top_k: int,
        allowed_page_paths: set[str] | None = None,
    ) -> List[Tuple[str, int, float]]:
        """在索引中召回 chunk 级别的候选结果。"""
        if HNSWLIB_AVAILABLE and self._index is not None:
            return self._hnsw_lookup(
                query_vec,
                threshold,
                use_rerank,
                top_k,
                allowed_page_paths,
            )
        return self._fallback_lookup(query_vec, threshold, allowed_page_paths)

    def _aggregate_page_scores(
        self, chunk_results: List[Tuple[str, int, float]]
    ) -> List[Tuple[str, float]]:
        """按页面聚合 chunk 结果，取每个页面的最高相似度并降序排序。"""
        page_scores: Dict[str, float] = {}
        for rel_path, chunk_idx, sim in chunk_results:
            if rel_path not in page_scores or sim > page_scores[rel_path]:
                page_scores[rel_path] = sim
        results = list(page_scores.items())
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def search(
        self,
        query: str,
        top_k: int = 10,
        similarity_threshold: float | None = None,
        use_rerank: bool | None = None,
        *,
        subject_scope: tuple[str, str] | None = None,
        allowed_page_paths: set[str] | None = None,
    ) -> List[Tuple[str, float]]:
        """Search within one request-scoped run when semantic APIs are needed."""
        # A model-call run is itself durable state.  Prove the persisted ANN is
        # available before opening that scope; a missing index is a zero-write
        # read result, not an attempted provider call.
        if self.client is None:
            return []
        if not self.persisted_search_available():
            return []

        from core.telemetry.prompt_call_log import current_model_call_run, model_call_run_scope

        # A context-owned run supplies its own exact subject.  A standalone
        # index query has no durable user asset, so it declares a fixed system
        # source rather than pretending the query digest identifies a person.
        effective_scope = subject_scope
        if effective_scope is None and current_model_call_run() is None:
            effective_scope = ("source", "embedding_index_search")
        with model_call_run_scope(
            self._runtime_config,
            "embedding_index_search",
            subject_scope=effective_scope,
        ):
            return self._search(
                query,
                top_k,
                similarity_threshold,
                use_rerank,
                subject_scope=effective_scope,
                allowed_page_paths=allowed_page_paths,
            )

    def _search(
        self,
        query: str,
        top_k: int = 10,
        similarity_threshold: float | None = None,
        use_rerank: bool | None = None,
        *,
        subject_scope: tuple[str, str] | None = None,
        allowed_page_paths: set[str] | None = None,
    ) -> List[Tuple[str, float]]:
        """
        语义搜索（两阶段：ANN 召回 + Rerank 精排）

        Args:
            query: 搜索查询
            top_k: 返回结果数
            similarity_threshold: 相似度阈值
            use_rerank: 是否启用 Rerank 精排（默认 True）

        Returns:
            [(页面相对路径, 相似度分数), ...] 按分数降序
        """
        if use_rerank is None:
            use_rerank = self._use_rerank
        threshold = similarity_threshold or self.SIMILARITY_THRESHOLD

        # Search is read-only. Index creation is a separate privileged workflow;
        # a restricted caller must never trigger full-vault body reads/provider calls.
        if not self.persisted_search_available():
            return []

        query_vec = self._embed_query(query, subject_scope=subject_scope)
        if query_vec is None:
            return []

        normalized_allowlist = (
            {str(Path(path).as_posix()) for path in allowed_page_paths}
            if allowed_page_paths is not None
            else None
        )
        chunk_results = self._lookup_chunks(
            query_vec,
            threshold,
            use_rerank,
            top_k,
            normalized_allowlist,
        )
        results = self._aggregate_page_scores(chunk_results)
        if normalized_allowlist is not None:
            results = [item for item in results if item[0] in normalized_allowlist]

        # --- Rerank 精排 ---
        if use_rerank and results and self.client:
            try:
                results = self._rerank_results(
                    query,
                    results,
                    top_k,
                    subject_scope=subject_scope,
                )
            except (
                ProviderRequestError,
                ModelCallLedgerError,
                OSError,
                RuntimeError,
                ValueError,
            ) as e:
                logger.warning("[Embedding] Rerank 失败，回退到 ANN 排序: %s", e)

        return results[:top_k]

    def _rerank_results(
        self,
        query: str,
        candidates: List[Tuple[str, float]],
        top_k: int,
        *,
        subject_scope: tuple[str, str] | None = None,
    ) -> List[Tuple[str, float]]:
        """[P1-6] 对候选结果调用 Rerank API 精排"""
        # 读取候选页面内容（使用 chunks 拼接，覆盖深层内容）
        documents = []
        valid_candidates = []
        document_subject_scopes: list[tuple[str, str]] = []
        for rel_path, _ in candidates:
            page_path = self.wiki_base / rel_path
            try:
                chunks = self._extract_chunks(page_path)
                if chunks:
                    # 拼接所有 chunks 的前 500 字符，总长度控制在 3000 以内
                    text_parts = []
                    total_len = 0
                    for chunk in chunks:
                        part = chunk["text"][:500]
                        if total_len + len(part) > EMBEDDING_INDEX_MANAGER__RERANK_RESULTS_PART:
                            break
                        text_parts.append(part)
                        total_len += len(part)
                    text = "\n".join(text_parts)
                else:
                    text = self._extract_page_text(page_path)
                if text.strip():
                    documents.append(text[:EMBEDDING_INDEX_MANAGER__RERANK_RESULTS_TEXT])
                    valid_candidates.append(rel_path)
                    document_subject_scopes.append(self._page_subject_scope(page_path))
            # DEBT(S8): 容错跳过，避免单条记录中断批量处理
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
            ):
                continue

        if not documents:
            return candidates[:top_k]

        entry_subject_scopes = set(document_subject_scopes)
        if subject_scope is not None:
            entry_subject_scopes.add(subject_scope)
        reranked = self._rerank_attributed(
            query,
            documents,
            top_k,
            tuple(sorted(entry_subject_scopes)) or None,
        )  # type: ignore[union-attr]
        # reranked: [(原始索引, 分数), ...]
        return [
            (valid_candidates[idx], score) for idx, score in reranked if idx < len(valid_candidates)
        ]

    def get_stats(self) -> Dict[str, Any]:
        """返回索引统计信息"""
        return {
            "total_pages": len(self._meta),
            "hnswlib_available": HNSWLIB_AVAILABLE,
            "client_available": self.client is not None and embedding_available(),
            "index_dir": str(self.index_dir),
            "wiki_base": str(self.wiki_base),
        }
