# -*- coding: utf-8 -*-
"""
双索引融合检索器（ADR-019）

页面向量索引（EmbeddingIndexManager）+ 关联上下文向量索引（RelationEmbeddingManager）
融合策略：final_score = content_weight * content_sim + relation_boost

使用场景：
- core/app/context_search.py 的语义召回
- knowledge_graph.py 的语义搜索增强
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .index_manager import EmbeddingIndexManager
from .relation_manager import RelationEmbeddingManager
from .siliconflow_client import SiliconFlowEmbeddingClient
from core.frontmatter import parse_frontmatter
from core.telemetry.prompt_call_log import ModelCallLedgerError
from core.telemetry.provider_request import ProviderRequestError

EMBEDDING_QUERY_ERRORS = (
    ProviderRequestError,
    ModelCallLedgerError,
    OSError,
    RuntimeError,
    ValueError,
    sqlite3.Error,
)

# Constants extracted from magic numbers
DUAL_INDEX_RETRIEVER__RERANK_CANDIDATES_PART = 3000
TEXT = 1200
# SiliconFlowEmbeddingClient imported lazily where needed

logger = logging.getLogger(__name__)


_UNSET = object()


class DualIndexRetriever:
    """
    双索引融合检索器。

    索引 A：页面内容向量（EmbeddingIndexManager）
    索引 B：关联上下文向量（RelationEmbeddingManager）

    检索时同时查询两个索引，融合得分后重排。
    """

    def __init__(
        self,
        page_index: Optional[EmbeddingIndexManager] = _UNSET,  # type: ignore[assignment]
        relation_manager: Optional[RelationEmbeddingManager] = _UNSET,  # type: ignore[assignment]
        wiki_base: Optional[Path] = None,
        content_weight: float = 0.7,
        relation_weight: float = 0.3,
    ):
        self.page_index = None if page_index is _UNSET else page_index
        self.relation_manager = None if relation_manager is _UNSET else relation_manager
        self.wiki_base = wiki_base
        self.content_weight = content_weight
        self.relation_weight = relation_weight

        from core.config import get_config

        self._use_rerank = get_config().get("embedding.use_rerank", True)

        # 懒加载：只有未传入参数时才自动创建；显式传入 None 表示禁用
        self._page_index_lazy = page_index is _UNSET
        self._relation_manager_lazy = relation_manager is _UNSET
        self.last_trace: Dict[str, Any] = self._new_trace(
            query="",
            top_k=0,
            similarity_threshold=None,
            use_rerank=self._use_rerank,
        )

    def _new_trace(
        self,
        query: str,
        top_k: int,
        similarity_threshold: float | None,
        use_rerank: bool,
    ) -> Dict[str, Any]:
        return {
            "query": query,
            "top_k": top_k,
            "similarity_threshold": similarity_threshold,
            "page_index_available": False,
            "page_search_attempted": False,
            "page_search_ok": False,
            "page_result_count": 0,
            "page_error": "",
            "relation_index_available": False,
            "relation_search_attempted": False,
            "relation_search_ok": False,
            "relation_result_count": 0,
            "relation_error": "",
            "relation_acl_candidate_count": None,
            "relation_acl_allowed_count": None,
            "fused_candidate_count": 0,
            "rerank_configured": bool(use_rerank),
            "rerank_attempted": False,
            "rerank_api_called": False,
            "rerank_applied": False,
            "rerank_degraded": False,
            "rerank_degraded_reason": "",
            "rerank_document_count": 0,
            "rerank_result_count": 0,
            "rerank_skipped_reason": "",
            "returned_count": 0,
            "degraded": False,
            "degraded_reasons": [],
        }

    @staticmethod
    def _mark_degraded(trace: Dict[str, Any], reason: str, error: str = "") -> None:
        trace["degraded"] = True
        if reason not in trace["degraded_reasons"]:
            trace["degraded_reasons"].append(reason)
        if error:
            trace.setdefault("errors", {})[reason] = error

    def get_last_trace(self) -> Dict[str, Any]:
        """Return retrieval-call evidence for the most recent search."""
        return dict(self.last_trace)

    def _ensure_page_index(self):
        if self.page_index is None and self._page_index_lazy:
            from core.config import get_config

            self.wiki_base = self.wiki_base or get_config().wiki_dir
            self.page_index = EmbeddingIndexManager(wiki_base=self.wiki_base)

    def _ensure_relation_manager(self):
        if self.relation_manager is None and self._relation_manager_lazy:
            from core.config import get_config

            cfg = get_config()
            current_wiki = (
                Path(self.wiki_base).expanduser().resolve(strict=False)
                if self.wiki_base is not None
                else Path(cfg.wiki_dir).expanduser().resolve(strict=False)
            )
            configured_wiki = Path(cfg.wiki_dir).expanduser().resolve(strict=False)
            if current_wiki != configured_wiki:
                db_path = current_wiki / ".kg" / "knowledge_graph.db"
                if not db_path.is_file():
                    self._relation_manager_lazy = False
                    return
                self.relation_manager = RelationEmbeddingManager(
                    db_path=db_path,
                    index_dir=current_wiki / ".kg" / "embedding_index",
                )
            else:
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
        except (sqlite3.Error, OSError) as e:
            logger.debug("[DualIndex] 查询关系页面失败: %s", e)
        return None, None

    @staticmethod
    def _effective_subject_scope(
        subject_scope: tuple[str, str] | None,
        *,
        fallback_source: str,
    ) -> tuple[str, str] | None:
        """Use caller identity when present, otherwise name this system path.

        A parent request-scoped ledger run already owns an exact subject.  Do
        not restate a different fixed source inside it, because ledger nesting
        deliberately rejects attribution mismatches.
        """
        if subject_scope is not None:
            return subject_scope
        from core.telemetry.prompt_call_log import current_model_call_run

        if current_model_call_run() is not None:
            return None
        return "source", fallback_source

    @staticmethod
    def _uses_remote_client(client: Any) -> bool:
        return isinstance(client, SiliconFlowEmbeddingClient)

    def _page_search_attributed(
        self,
        query: str,
        *,
        top_k: int,
        similarity_threshold: float | None,
        subject_scope: tuple[str, str] | None,
        allowed_page_paths: set[str] | None,
    ) -> List[Tuple[str, float]]:
        assert self.page_index is not None
        if self._uses_remote_client(self.page_index.client):
            results = self.page_index.search(
                query,
                top_k=top_k,
                similarity_threshold=similarity_threshold,
                use_rerank=False,
                subject_scope=subject_scope,
                allowed_page_paths=allowed_page_paths,
            )
        else:
            results = self.page_index.search(
                query,
                top_k=top_k,
                similarity_threshold=similarity_threshold,
                use_rerank=False,
                allowed_page_paths=allowed_page_paths,
            )
        return [(str(path), float(score)) for path, score in results]

    def _relation_search_attributed(
        self,
        query: str,
        *,
        top_k: int,
        subject_scope: tuple[str, str] | None,
        allowed_relation_ids: set[int] | None,
    ) -> List[Tuple[int, float]]:
        assert self.relation_manager is not None
        if self._uses_remote_client(self.relation_manager.client):
            return self.relation_manager.search(
                query,
                top_k=top_k,
                subject_scope=subject_scope,
                allowed_relation_ids=allowed_relation_ids,
            )
        return self.relation_manager.search(
            query,
            top_k=top_k,
            allowed_relation_ids=allowed_relation_ids,
        )

    def _allowed_relation_ids(
        self,
        allowed_page_paths: set[str],
    ) -> tuple[set[int], int]:
        """Derive the relation allowlist from two authorized Wiki endpoints."""

        assert self.relation_manager is not None
        db_path = Path(self.relation_manager.db_path).expanduser().resolve(strict=False)
        with sqlite3.connect(
            f"{db_path.as_uri()}?mode=ro",
            uri=True,
            timeout=10,
        ) as connection:
            rows = connection.execute(
                "SELECT id, source, target FROM relations ORDER BY id"
            ).fetchall()
        allowed_ids = {
            int(relation_id)
            for relation_id, source, target in rows
            if str(Path(str(source)).as_posix()) in allowed_page_paths
            and str(Path(str(target)).as_posix()) in allowed_page_paths
        }
        return allowed_ids, len(rows)

    @staticmethod
    def _page_path_subject_scope(wiki_base: Path, rel_path: str) -> tuple[str, str]:
        return "path", str((wiki_base / rel_path).expanduser().resolve(strict=False))

    def _rerank_attributed(
        self,
        client: Any,
        *,
        query: str,
        documents: List[str],
        top_n: int,
        subject_scopes: tuple[tuple[str, str], ...] | None,
    ) -> List[Tuple[int, float]]:
        if self._uses_remote_client(client):
            raw_results = client.rerank(
                query=query,
                documents=documents,
                top_n=top_n,
                subject_scopes=subject_scopes,
            )
        else:
            raw_results = client.rerank(
                query=query,
                documents=documents,
                top_n=top_n,
            )
        if not isinstance(raw_results, list):
            raise TypeError("rerank client must return a list")
        normalized: List[Tuple[int, float]] = []
        for item in raw_results:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise TypeError("rerank result must contain index/score pairs")
            normalized.append((int(item[0]), float(item[1])))
        return normalized

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
        """
        双索引融合检索的紧凑结果接口。

        Returns:
            [(页面相对路径, 融合分数), ...] 按融合分数降序
        """
        detailed = self.search_detailed(
            query,
            top_k,
            similarity_threshold,
            use_rerank,
            subject_scope=subject_scope,
            allowed_page_paths=allowed_page_paths,
        )
        return [(path, score) for path, score, _, _ in detailed]

    def search_detailed(
        self,
        query: str,
        top_k: int = 10,
        similarity_threshold: float | None = None,
        use_rerank: bool | None = None,
        *,
        subject_scope: tuple[str, str] | None = None,
        allowed_page_paths: set[str] | None = None,
    ) -> List[Tuple[str, float, float, float]]:
        """
        双索引融合检索（返回分解分数）。

        Returns:
            [(页面相对路径, 融合分数, 页面语义分, 关系boost分), ...] 按融合分数降序
        """
        if use_rerank is None:
            use_rerank = self._use_rerank
        trace = self._new_trace(query, top_k, similarity_threshold, use_rerank)
        normalized_allowlist = (
            {str(Path(path).as_posix()) for path in allowed_page_paths}
            if allowed_page_paths is not None
            else None
        )
        trace["acl_allowlist_count"] = (
            len(normalized_allowlist) if normalized_allowlist is not None else None
        )
        effective_subject_scope = self._effective_subject_scope(
            subject_scope,
            fallback_source="dual_index_retriever",
        )
        self._ensure_page_index()
        self._ensure_relation_manager()

        if self.page_index is None or self.page_index.client is None:
            self._mark_degraded(trace, "page_embedding_client_unavailable")
            self.last_trace = trace
            return []
        trace["page_index_available"] = True

        # --- Phase 1: 内容检索（索引 A）---
        trace["page_search_attempted"] = True
        try:
            page_results = self._page_search_attributed(
                query,
                top_k=max(top_k * 3, 20),
                similarity_threshold=similarity_threshold,
                subject_scope=effective_subject_scope,
                allowed_page_paths=normalized_allowlist,
            )
            trace["page_search_ok"] = True
            trace["page_result_count"] = len(page_results)
        except EMBEDDING_QUERY_ERRORS as e:
            logger.warning("[DualIndex] 页面检索失败: %s", e)
            trace["page_error"] = f"{type(e).__name__}: {e}"
            self._mark_degraded(trace, "page_embedding_search_failed", trace["page_error"])
            page_results = []

        content_scores: Dict[str, float] = {}
        for rel_path, sim in page_results:
            content_scores[rel_path] = sim

        # --- Phase 2: 关联检索（索引 B）---
        relation_boost: Dict[str, float] = defaultdict(float)
        if self.relation_manager is not None and self.relation_manager.client is not None:
            trace["relation_index_available"] = True
            trace["relation_search_attempted"] = True
            try:
                allowed_relation_ids: set[int] | None = None
                if normalized_allowlist is not None:
                    allowed_relation_ids, relation_candidate_count = self._allowed_relation_ids(
                        normalized_allowlist
                    )
                    trace["relation_acl_candidate_count"] = relation_candidate_count
                    trace["relation_acl_allowed_count"] = len(allowed_relation_ids)
                relation_results = self._relation_search_attributed(
                    query,
                    top_k=20,
                    subject_scope=effective_subject_scope,
                    allowed_relation_ids=allowed_relation_ids,
                )
                trace["relation_search_ok"] = True
                trace["relation_result_count"] = len(relation_results)
                for rel_id, rel_sim in relation_results:
                    source, target = self._get_relation_pages(rel_id)
                    boost = rel_sim * self.relation_weight
                    if source and (normalized_allowlist is None or source in normalized_allowlist):
                        relation_boost[source] += boost
                    if target and (normalized_allowlist is None or target in normalized_allowlist):
                        relation_boost[target] += boost
            except EMBEDDING_QUERY_ERRORS as e:
                logger.debug("[DualIndex] 关联检索失败: %s", e)
                trace["relation_error"] = f"{type(e).__name__}: {e}"
                self._mark_degraded(
                    trace,
                    "relation_embedding_search_failed",
                    trace["relation_error"],
                )

        # relation boost 封顶：单页不超过 0.25，避免关系噪音压过内容命中
        for page in relation_boost:
            relation_boost[page] = min(relation_boost[page], 0.25)

        # --- Phase 3: 融合得分 ---
        all_pages = set(content_scores.keys()) | set(relation_boost.keys())
        if normalized_allowlist is not None:
            all_pages.intersection_update(normalized_allowlist)
        if not all_pages:
            self.last_trace = trace
            return []
        trace["fused_candidate_count"] = len(all_pages)

        fused_scores: Dict[str, Tuple[float, float, float]] = {}
        for page in all_pages:
            c_score = content_scores.get(page, 0.0)
            r_score = relation_boost.get(page, 0.0)
            fused = self.content_weight * c_score + r_score
            fused_scores[page] = (fused, c_score, r_score)

        # --- Phase 4: Rerank 精排 ---
        top_candidates = sorted(fused_scores.items(), key=lambda x: x[1][0], reverse=True)

        if use_rerank and len(top_candidates) > top_k and self.page_index.client is not None:
            trace["rerank_attempted"] = True
            try:
                results = self._rerank_candidates(
                    query,
                    top_candidates,
                    top_k,
                    trace=trace,
                    subject_scope=effective_subject_scope,
                )
                trace["rerank_applied"] = bool(trace["rerank_api_called"])
                trace["returned_count"] = len(results)
                self.last_trace = trace
                return results
            except EMBEDDING_QUERY_ERRORS as e:
                logger.debug("[DualIndex] Rerank 失败，回退到融合排序: %s", e)
                trace["rerank_degraded"] = True
                trace["rerank_degraded_reason"] = f"{type(e).__name__}: {e}"
                self._mark_degraded(
                    trace,
                    "rerank_failed",
                    trace["rerank_degraded_reason"],
                )
        elif use_rerank:
            trace["rerank_skipped_reason"] = (
                "candidate_count_not_above_top_k"
                if len(top_candidates) <= top_k
                else "page_embedding_client_unavailable"
            )

        results = [
            (path, fused, page_sc, rel_sc)
            for path, (fused, page_sc, rel_sc) in top_candidates[:top_k]
        ]
        trace["returned_count"] = len(results)
        self.last_trace = trace
        return results

    def _rerank_candidates(
        self,
        query: str,
        candidates: List[Tuple[str, Tuple[float, float, float]]],
        top_k: int,
        trace: Optional[Dict[str, Any]] = None,
        *,
        subject_scope: tuple[str, str] | None = None,
    ) -> List[Tuple[str, float, float, float]]:
        """对融合后的候选结果调用 Rerank API 精排，保留 page/rel 分解分数"""
        wiki_base = self.wiki_base or self.page_index.wiki_base  # type: ignore[union-attr]
        documents = []
        valid_paths = []
        document_subject_scopes: list[tuple[str, str]] = []
        score_map: Dict[str, Tuple[float, float]] = {}

        for rel_path, (_, page_sc, rel_sc) in candidates[: top_k * 2]:
            score_map[rel_path] = (page_sc, rel_sc)
            page_path = wiki_base / rel_path
            try:
                # [P1-6] 使用 chunks 拼接覆盖深层内容
                extract_chunks = getattr(self.page_index, "_extract_chunks", None)
                if extract_chunks:
                    chunks = extract_chunks(page_path)
                else:
                    chunks = None
                if chunks:
                    text_parts = []
                    total_len = 0
                    for chunk in chunks:
                        part = chunk["text"][:500]
                        if total_len + len(part) > DUAL_INDEX_RETRIEVER__RERANK_CANDIDATES_PART:
                            break
                        text_parts.append(part)
                        total_len += len(part)
                    text = "\n".join(text_parts)
                else:
                    raw = page_path.read_text(encoding="utf-8", errors="ignore")
                    _, text = parse_frontmatter(raw)
                    text = (text or "").strip()[:TEXT]
                if text:
                    documents.append(text)
                    valid_paths.append(rel_path)
                    document_subject_scopes.append(
                        self._page_path_subject_scope(Path(wiki_base), rel_path)
                    )
            # DEBT(S8): 容错跳过，避免单条记录中断批量处理
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
                sqlite3.Error,
            ):
                continue

        if trace is not None:
            trace["rerank_document_count"] = len(documents)

        if not documents:
            if trace is not None:
                trace["rerank_degraded"] = True
                trace["rerank_degraded_reason"] = "rerank_no_documents"
                self._mark_degraded(trace, "rerank_no_documents")
            return [
                (path, fused, page_sc, rel_sc)
                for path, (fused, page_sc, rel_sc) in candidates[:top_k]
            ]

        if trace is not None:
            trace["rerank_api_called"] = True
        entry_subject_scopes = set(document_subject_scopes)
        if subject_scope is not None:
            entry_subject_scopes.add(subject_scope)
        reranked = self._rerank_attributed(
            self.page_index.client,  # type: ignore[union-attr]
            query=query,
            documents=documents,
            top_n=top_k,
            subject_scopes=tuple(sorted(entry_subject_scopes)) or None,
        )
        if trace is not None:
            trace["rerank_result_count"] = len(reranked)
        results = []
        for idx, score in reranked:
            if idx < len(valid_paths):
                path = valid_paths[idx]
                page_sc, rel_sc = score_map.get(path, (0.0, 0.0))
                results.append((path, score, page_sc, rel_sc))
        return results

    def rerank_authorized_documents(
        self,
        query: str,
        documents: List[str],
        *,
        top_n: int,
        subject_scope: tuple[str, str] | None = None,
    ) -> Tuple[List[int], Dict[str, Any]]:
        """Rerank only caller-supplied, already-authorized document text.

        This seam never reads Wiki files. Callers must perform item ACL checks before
        passing content so an external rerank provider cannot observe denied items.
        """
        trace = self._new_trace(
            query=query,
            top_k=top_n,
            similarity_threshold=None,
            use_rerank=self._use_rerank,
        )
        default_order = list(range(min(len(documents), max(0, top_n))))
        if not self._use_rerank:
            trace["rerank_skipped_reason"] = "rerank_disabled"
            return default_order, trace
        if len(documents) <= 1:
            trace["rerank_skipped_reason"] = "candidate_count_not_above_one"
            return default_order, trace

        self._ensure_page_index()
        client = self.page_index.client if self.page_index is not None else None
        if client is None:
            trace["rerank_skipped_reason"] = "page_embedding_client_unavailable"
            return default_order, trace

        trace["rerank_attempted"] = True
        trace["rerank_document_count"] = len(documents)
        try:
            trace["rerank_api_called"] = True
            effective_subject_scope = self._effective_subject_scope(
                subject_scope,
                fallback_source="dual_index_authorized_rerank",
            )
            reranked = self._rerank_attributed(
                client,
                query=query,
                documents=documents,
                top_n=min(len(documents), max(1, top_n)),
                subject_scopes=(effective_subject_scope,) if effective_subject_scope else None,
            )
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            ImportError,
            AttributeError,
            RuntimeError,
        ) as exc:
            trace["rerank_degraded"] = True
            trace["rerank_degraded_reason"] = f"{type(exc).__name__}: {exc}"
            self._mark_degraded(
                trace,
                "rerank_failed",
                trace["rerank_degraded_reason"],
            )
            return default_order, trace

        ordered = []
        for index, _score in reranked:
            if 0 <= index < len(documents) and index not in ordered:
                ordered.append(index)
        ordered.extend(index for index in range(len(documents)) if index not in ordered)
        trace["rerank_result_count"] = len(reranked)
        trace["rerank_applied"] = bool(reranked)
        trace["returned_count"] = min(len(ordered), max(0, top_n))
        return ordered[:top_n], trace

    def get_stats(self) -> dict:
        """返回双索引统计"""
        return {
            "content_weight": self.content_weight,
            "relation_weight": self.relation_weight,
            "page_index": self.page_index.get_stats() if self.page_index else None,
            "relation_index": self.relation_manager.get_stats() if self.relation_manager else None,
        }
