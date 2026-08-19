# -*- coding: utf-8 -*-
"""
ContextAwareSearch — 上下文感知搜索

知识图谱召回 + 画像加权评分。
4 维加权：confidence×0.4 + relevance×0.3 + continuity×0.2 + freshness×0.1
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, cast, Dict, List, Optional
from uuid import uuid4

from core.access_policy import AccessNarrowing, PrincipalEnvelope, authorize_item
from core.app.context_search_models import SearchResult
from core.app.context_search_authorization import ContextSearchAuthorizationMixin
from core.app.context_search_feedback import ContextSearchFeedbackMixin
from core.app.context_search_profile import ContextSearchProfileMixin
from core.frontmatter import parse_frontmatter
from core.utils import EXCLUDED_DIRS
from core.config import get_config
from core.kia.policy import get_effective_policy
from core.ops import search_flow_receipts
from core.telemetry.provider_request import safe_provider_error_category

# Constants extracted from magic numbers
CONTEXT_AWARE_SEARCH_DURATION_BUCKET_MONTH_DAYS = 30
STATS_DAYS = 30

logger = logging.getLogger(__name__)


class ContextAwareSearch(
    ContextSearchProfileMixin,
    ContextSearchAuthorizationMixin,
    ContextSearchFeedbackMixin,
):
    """上下文感知搜索"""

    MAX_RESULTS = 10
    EXCLUDED_DIRS = EXCLUDED_DIRS  # 从 core.utils 统一引用

    # 五维评分权重默认值
    DEFAULT_WEIGHTS = {
        "confidence": 0.35,
        "relevance": 0.25,
        "continuity": 0.15,
        "freshness": 0.10,
        "persona": 0.15,
    }

    def __init__(
        self,
        wiki_base: Optional[str] = None,
        *,
        database_dir: Path | str | None = None,
        wiki_projection_db: Path | str | None = None,
        cognitive_state_db: Path | str | None = None,
        cognitive_graph_db: Path | str | None = None,
        evidence_graph_db: Path | str | None = None,
        knowledge_graph_db: Path | str | None = None,
        embedding_index_dir: Path | str | None = None,
    ):
        cfg = get_config()
        self._default_wiki_dir = Path(getattr(cfg, "wiki_dir", wiki_base or Path.cwd()))
        self._default_database_dir = Path(
            getattr(cfg, "database_dir", self._default_wiki_dir / ".kg")
        )
        if wiki_base:
            self.wiki_base = Path(wiki_base).expanduser().resolve(strict=False)
        else:
            self.wiki_base = self._default_wiki_dir.expanduser().resolve(strict=False)
        current_wiki = self.wiki_base
        configured_wiki = self._default_wiki_dir.expanduser().resolve(strict=False)
        exact_database_dir = (
            Path(database_dir).expanduser()
            if database_dir is not None
            else (self._default_database_dir if current_wiki == configured_wiki else None)
        )

        def exact_path(value: Path | str | None, filename: str) -> Path | None:
            if value is not None:
                return Path(value).expanduser()
            return exact_database_dir / filename if exact_database_dir is not None else None

        self._database_dir = exact_database_dir
        self._search_session_db = (
            exact_database_dir / "mnemos.db" if exact_database_dir is not None else None
        )
        self._wiki_projection_db = exact_path(wiki_projection_db, "wiki_projection.db")
        self._cognitive_state_db = exact_path(
            cognitive_state_db,
            "producer_consumer_ledger.db",
        )
        self._cognitive_graph_db = exact_path(cognitive_graph_db, "cognitive_graph.db")
        self._evidence_graph_db = exact_path(evidence_graph_db, "evidence_graph.db")
        self._knowledge_graph_db = exact_path(knowledge_graph_db, "knowledge_graph.db")
        self._embedding_index_dir = (
            Path(embedding_index_dir).expanduser()
            if embedding_index_dir is not None
            else (
                exact_database_dir / "embedding_index" if exact_database_dir is not None else None
            )
        )
        self.weights = self._load_weights()
        self.last_query_trace: Dict[str, Any] = {}
        self._profile_usage_evidence: Dict[str, Any] | None = None
        self._active_profile_query_id = ""
        self._dual_index_retriever: Any | None = None
        self._cognitive_searcher: Any | None = None
        # This map is constructed from frontmatter-only reads before any body
        # retrieval.  A missing map is deliberately indistinguishable from an
        # empty authorization result to every recall helper.
        self._authorized_page_frontmatter: Dict[str, Dict[str, Any]] | None = None

    def _new_query_trace(self, query: str, limit: int, embedding_enabled: bool) -> Dict[str, Any]:
        return {
            "query": query,
            "limit": limit,
            "embedding_enabled": bool(embedding_enabled),
            "embedding_attempted": False,
            "embedding_degraded": False,
            "semantic_candidates": 0,
            "keyword_candidates": 0,
            "kg_attempted": False,
            "kg_candidates": 0,
            "cognitive_state_attempted": False,
            "cognitive_state_candidates": 0,
            "cognitive_state_access": {},
            "fallback_mode": False,
            "rerank_configured": False,
            "rerank_attempted": False,
            "rerank_api_called": False,
            "rerank_applied": False,
            "rerank_degraded": False,
            "dual_index": {},
            "result_count": 0,
            "degraded": False,
            "degraded_reasons": [],
            "access_filter": {},
        }

    def _merge_trace_degradation(
        self,
        reason: str,
        *,
        embedding: bool = False,
        rerank: bool = False,
    ) -> None:
        self.last_query_trace["degraded"] = True
        if embedding:
            self.last_query_trace["embedding_degraded"] = True
        if rerank:
            self.last_query_trace["rerank_degraded"] = True
        reasons = self.last_query_trace.setdefault("degraded_reasons", [])
        if reason not in reasons:
            reasons.append(reason)

    def _merge_dual_index_trace(self, dual_trace: Dict[str, Any]) -> None:
        self.last_query_trace["dual_index"] = dict(dual_trace)
        self.last_query_trace["embedding_attempted"] = bool(dual_trace.get("page_search_attempted"))
        for key in [
            "rerank_configured",
            "rerank_attempted",
            "rerank_api_called",
            "rerank_applied",
            "rerank_degraded",
        ]:
            self.last_query_trace[key] = bool(dual_trace.get(key))
        if dual_trace.get("degraded"):
            for reason in dual_trace.get("degraded_reasons", []):
                self._merge_trace_degradation(
                    reason,
                    embedding=str(reason).startswith(("page_", "relation_")),
                    rerank=str(reason).startswith("rerank"),
                )

    def get_last_query_trace(self) -> Dict[str, Any]:
        """Return retrieval evidence for the most recent context search."""
        return dict(self.last_query_trace)

    @staticmethod
    def _model_call_subject_scope(
        context: Optional[Dict] = None,
        *,
        fallback_source: str,
    ) -> tuple[str, str]:
        """Choose a real request scope before falling back to a fixed system source."""
        context = context or {}
        for key in ("session_id", "session"):
            value = str(context.get(key) or "").strip()
            if value:
                return "session", value
        project = str(context.get("project") or "").strip()
        if project:
            return "project", project
        for key in ("working_dir", "path"):
            value = str(context.get(key) or "").strip()
            if value:
                return "path", value
        return "source", fallback_source

    def _load_weights(self) -> Dict[str, float]:
        """从配置读取评分权重，未配置时使用默认值。"""
        try:
            from core.config import get_config

            cfg = get_config()
            cfg_weights = cfg.get("search.weights", {})
            if cfg_weights and isinstance(cfg_weights, dict):
                weights = dict(self.DEFAULT_WEIGHTS)
                weights.update(
                    {
                        k: float(v)
                        for k, v in cfg_weights.items()
                        if k in weights and isinstance(v, (int, float))
                    }
                )
                total = sum(weights.values())
                if total > 0:
                    # 归一化，确保总和为 1
                    weights = {k: v / total for k, v in weights.items()}
                return weights
        except ImportError:
            logger.debug("搜索权重配置不可用，使用默认权重", exc_info=True)
        return dict(self.DEFAULT_WEIGHTS)

    def search(
        self,
        query: str,
        context: Optional[Dict] = None,
        limit: int | None = None,
        *,
        allow_embedding: bool = True,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> List[SearchResult]:
        """
        上下文感知搜索。

        Args:
            query: 搜索查询
            context: 上下文（working_dir, active_entities, recent_pages 等）
            limit: 最大结果数
            allow_embedding: 是否允许语义召回；高频启动钩子可关闭以避免同步索引重建
            principal: 服务端解析的调用主体；缺失时不读取任何 Wiki 正文
            narrowing: 仅能收窄主体授权的请求范围

        Returns:
            排序后的搜索结果列表
        """
        context = context or {}
        limit = limit or self.MAX_RESULTS
        self._profile_usage_evidence = None
        self._active_profile_query_id = f"context-search:{uuid4().hex}"
        self._authorized_page_frontmatter = None
        self._dual_index_retriever = None
        self._cognitive_searcher = None

        if principal is None:
            self.last_query_trace = self._new_query_trace(query, limit, False)
            self.last_query_trace["access_filter"] = {"principal_required": 1}
            return []

        effective_narrowing = narrowing or AccessNarrowing()
        authorized_pages, access_filter = self._authorized_frontmatter_pages(
            principal,
            effective_narrowing,
        )
        self._authorized_page_frontmatter = authorized_pages

        from core.config import get_config

        cfg = get_config()
        embedding_configured = cfg.get("embedding.enabled", True)
        embedding_enabled = bool(embedding_configured and allow_embedding and authorized_pages)
        self.last_query_trace = self._new_query_trace(query, limit, embedding_enabled)
        self.last_query_trace["access_filter"] = access_filter
        self.last_query_trace["embedding_configured"] = bool(embedding_configured)
        self.last_query_trace["embedding_allowed"] = bool(allow_embedding)
        self.last_query_trace["cognitive_state_attempted"] = True
        cognitive_results, cognitive_access = self._recall_from_cognitive_state(
            query,
            principal=principal,
            narrowing=effective_narrowing,
            # Preserve a typed-cognition candidate pool for the final Wiki +
            # state + graph + evidence fusion rather than truncating it at the
            # caller's final page limit inside the typed retriever.
            limit=max(20, limit * 4),
        )
        self.last_query_trace["cognitive_state_candidates"] = len(cognitive_results)
        self.last_query_trace["cognitive_state_access"] = cognitive_access

        # 0. 语义召回 + 关键词召回 并行执行，确保关键词命中不被关系 boost 淹没
        semantic_candidates = (
            self._recall_from_embedding(query, context=context) if embedding_enabled else []
        )
        self.last_query_trace["semantic_candidates"] = len(semantic_candidates)
        keyword_candidates = self._recall_from_files(query)
        self.last_query_trace["keyword_candidates"] = len(keyword_candidates)

        # 检测是否处于降级检索模式
        fallback_mode = not semantic_candidates and not embedding_enabled
        self.last_query_trace["fallback_mode"] = fallback_mode
        if fallback_mode:
            logger.debug(
                "[ContextAwareSearch] 当前为降级检索（embedding 未启用），仅使用关键词/图谱召回"
            )

        # 1. 融合：语义 + 关键词 + KG
        candidates = self._merge_candidates(semantic_candidates, keyword_candidates)
        # KG is an independent channel; a full text candidate set must not suppress it.
        self.last_query_trace["kg_attempted"] = True
        kg_candidates = self._recall_from_kg(query)
        self.last_query_trace["kg_candidates"] = len(kg_candidates)
        candidates = self._merge_candidates(candidates, kg_candidates)

        candidates = self._filter_superseded_recap_candidates(candidates)

        if not candidates:
            cognitive_results.sort(key=lambda result: result.score, reverse=True)
            selected = cognitive_results[:limit]
            self.last_query_trace["result_count"] = len(selected)
            return selected

        freshness_checker = self._get_freshness_checker()

        # 2. 画像加权评分
        profile = self._get_profile_weights(principal, effective_narrowing)
        search_session_db = self._get_search_session_db_path()
        candidate_lifecycle = self._wiki_tombstone_states(
            [
                self.wiki_base / str(candidate.get("path") or "")
                for candidate in candidates
                if str(candidate.get("path") or "")
            ]
        )
        results = list(cognitive_results)
        profile_candidate_effects: Dict[str, Dict[str, Any]] = {}
        for candidate in candidates:
            candidate_path = str(candidate.get("path") or "").replace("\\", "/")
            if self._authorized_frontmatter_for(candidate_path) is None:
                continue
            acl_frontmatter = self._read_canonical_acl_frontmatter(candidate)
            current_decision = authorize_item(
                principal,
                {"page_path": candidate_path, "frontmatter": acl_frontmatter},
                effective_narrowing,
            )
            if not current_decision.allowed:
                access_filter[current_decision.reason] = (
                    access_filter.get(current_decision.reason, 0) + 1
                )
                continue
            lifecycle_state = candidate_lifecycle.get(
                str((self.wiki_base / candidate_path).resolve(strict=False)),
                None,
            )
            if lifecycle_state is not False:
                reason = (
                    "subject_deleted" if lifecycle_state is True else "wiki_tombstone_unavailable"
                )
                access_filter[reason] = access_filter.get(reason, 0) + 1
                continue
            snapshot = self._read_wiki_snapshot(
                candidate_path,
                lifecycle_state=lifecycle_state,
            )
            if snapshot is None:
                access_filter["wiki_snapshot_unavailable"] = (
                    access_filter.get("wiki_snapshot_unavailable", 0) + 1
                )
                continue
            snapshot_content, snapshot_revision_id = snapshot
            candidate = dict(candidate)
            candidate["content"] = snapshot_content
            candidate["frontmatter"] = acl_frontmatter
            relevance = self._compute_relevance(query, candidate)
            confidence = self._compute_confidence(candidate)
            continuity = self._compute_continuity(candidate, context)
            freshness = self._compute_freshness(candidate)
            persona_score, matched_assertion_ids = self._compute_persona_score_with_matches(
                candidate, profile
            )
            context_boost = self._compute_context_boost(candidate, context)
            match_details = self._keyword_match_details(query, candidate)

            # 加权总分
            W = self.weights
            score = (
                confidence * W["confidence"] * profile.get("confidence_boost", 1.0)
                + relevance * W["relevance"] * profile.get("domain_boost", 1.0)
                + continuity * W["continuity"]
                + freshness * W["freshness"] * profile.get("temporal_boost", 1.0)
                + persona_score * W["persona"]
            )
            score = min(score * context_boost, 1.0)
            baseline_score = (
                confidence * W["confidence"] * profile.get("confidence_boost", 1.0)
                + relevance * W["relevance"] * profile.get("domain_boost", 1.0)
                + continuity * W["continuity"]
                + freshness * W["freshness"] * profile.get("temporal_boost", 1.0)
            )
            baseline_score = min(baseline_score * context_boost, 1.0)
            freshness_alert = freshness_checker.check(candidate) if freshness_checker else None

            # 命中来源：语义召回的 candidate 带有 match_type；
            # 融合时若同时被语义和 KG/文件召回，标记为 hybrid
            match_src = candidate.get("match_type", "keyword")
            if match_src == "semantic" and candidate.get("_has_kg_match"):
                match_src = "hybrid"
            heat = self._get_heat_info(candidate.get("path", ""))
            matched_field, source_span_ids = self._wiki_match_trace(
                candidate,
                match_details,
                snapshot_revision_id,
            )
            search_result = SearchResult(
                page_path=candidate.get("path", ""),
                title=candidate.get("title", ""),
                snippet=self._extract_snippet(candidate, query),
                score=score,
                relevance=relevance,
                confidence=confidence,
                continuity=continuity,
                freshness=freshness,
                persona_score=persona_score,
                context_boost=context_boost,
                final_score=score,
                match_reason=self._explain_match(
                    relevance, confidence, continuity, freshness, context_boost, match_details
                ),
                freshness_alert=freshness_alert,
                verification=candidate.get("verification", ""),
                source=candidate.get("source", ""),
                last_modified=candidate.get("last_modified", ""),
                match_source=match_src,
                page_embedding_score=candidate.get("page_embedding_score", 0.0),
                relation_score=candidate.get("relation_score", 0.0),
                keyword_score=candidate.get("keyword_score", 0.0),
                score_breakdown=self._build_score_breakdown(
                    candidate,
                    relevance,
                    confidence,
                    continuity,
                    freshness,
                    persona_score,
                    context_boost,
                    score,
                    match_details,
                ),
                matched_terms=match_details["matched_terms"],
                heat_level=heat["level"],
                heat_score=heat["score"],
                last_accessed=heat["last_accessed"],
                scope=str(acl_frontmatter.get("scope") or ""),
                source_agent=str(acl_frontmatter.get("source_agent") or ""),
                session_id=str(acl_frontmatter.get("session_id") or ""),
                project=str(acl_frontmatter.get("project") or ""),
                tags=list(acl_frontmatter.get("tags") or []),
                acl_schema_version=self._acl_schema_version(acl_frontmatter),
                acl_metadata_complete=(acl_frontmatter.get("acl_metadata_complete") is True),
                acl_reconciliation_status=str(
                    acl_frontmatter.get("acl_reconciliation_status") or ""
                ),
                entity=str(candidate.get("entity") or ""),
                result_kind="wiki_page",
                object_type="wiki_page",
                object_id=candidate_path,
                revision_id=snapshot_revision_id,
                matched_field=matched_field,
                source_revision_id=snapshot_revision_id,
                source_span_ids=source_span_ids,
                acl_decision="authorized",
            )
            results.append(search_result)
            profile_candidate_effects[self._profile_rank_candidate_id(search_result)] = {
                "baseline_score": baseline_score,
                "persona_enabled_score": score,
                "matched_assertion_ids": matched_assertion_ids,
            }

        # 3. 过滤低质量内容
        # relevance < 0.15 表示 query token 几乎没有命中 title/content，属于弱相关
        results = [r for r in results if r.confidence >= 0.5 and r.relevance >= 0.15]

        # 4. 排序并截取
        results.sort(
            key=lambda result: (
                -float(result.score),
                self._profile_rank_candidate_id(result),
            )
        )
        selected = results[:limit]
        self.last_query_trace["result_count"] = len(selected)
        if self._profile_usage_evidence:
            rank_candidates: List[Dict[str, Any]] = []
            for result in results:
                candidate_id = self._profile_rank_candidate_id(result)
                effect = profile_candidate_effects.get(candidate_id, {})
                rank_candidates.append(
                    {
                        "candidate_id": candidate_id,
                        "page_path": result.page_path,
                        "baseline_score": float(effect.get("baseline_score", result.score)),
                        "persona_enabled_score": float(
                            effect.get("persona_enabled_score", result.score)
                        ),
                        "matched_assertion_ids": list(effect.get("matched_assertion_ids") or ()),
                    }
                )
            (
                baseline_output,
                persona_enabled_output,
                matched_assertion_ids,
                changed_candidates,
            ) = self._build_profile_rank_effect(rank_candidates, limit=limit)
            selected_ids = [self._profile_rank_candidate_id(result) for result in selected]
            if selected_ids != [str(row["candidate_id"]) for row in persona_enabled_output]:
                raise RuntimeError("context search enabled rank receipt drift")
            if matched_assertion_ids and baseline_output != persona_enabled_output:
                self._profile_usage_evidence["matched_assertion_ids"] = matched_assertion_ids
                self._profile_usage_evidence["rank_delta"] = changed_candidates
                self._profile_usage_evidence["eligible_candidate_ids"] = sorted(
                    str(row["candidate_id"]) for row in rank_candidates
                )
                persona_flow_item_id = ""
                if search_session_db is not None:
                    authorized_revisions = dict(
                        self._profile_usage_evidence.get("authorized_revisions") or {}
                    )
                    persona_flow_item_id = search_flow_receipts.start_persona_search(
                        search_session_db.parent,
                        (
                            f"{assertion_id}@{authorized_revisions[assertion_id]}"
                            for assertion_id in sorted(matched_assertion_ids)
                        ),
                    )
                try:
                    self._record_authorized_profile_usage(
                        principal=principal,
                        narrowing=effective_narrowing,
                        baseline_output=baseline_output,
                        persona_enabled_output=persona_enabled_output,
                    )
                    if persona_flow_item_id:
                        assert search_session_db is not None
                        search_flow_receipts.finish_persona_search(
                            search_session_db.parent,
                            persona_flow_item_id,
                            len(selected),
                        )
                finally:
                    self._profile_usage_evidence = None
            else:
                self._profile_usage_evidence = None
        return selected

    def _authorized_frontmatter_pages(
        self,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> tuple[Dict[str, Dict[str, Any]], Dict[str, int]]:
        """Build the body-read allowlist from canonical frontmatter only."""
        from core.frontmatter import normalize_frontmatter, read_frontmatter_only

        allowed: Dict[str, Dict[str, Any]] = {}
        summary: Dict[str, int] = {}
        wiki_root = self.wiki_base.expanduser().resolve(strict=False)
        if not wiki_root.is_dir():
            summary["wiki_unavailable"] = 1
            return allowed, summary

        candidate_pages: List[tuple[Path, str]] = []
        for page_path in sorted(wiki_root.rglob("*.md")):
            if page_path.is_symlink() or self._is_excluded_path(page_path):
                continue
            try:
                resolved_path = page_path.resolve(strict=True)
                relative_path = resolved_path.relative_to(wiki_root).as_posix()
                candidate_pages.append((resolved_path, relative_path))
            except (OSError, ValueError):
                summary["acl_metadata_missing"] = summary.get("acl_metadata_missing", 0) + 1

        tombstones = self._wiki_tombstone_states(
            [resolved_path for resolved_path, _relative_path in candidate_pages]
        )
        for resolved_path, relative_path in candidate_pages:
            tombstone = tombstones.get(str(resolved_path), None)
            if tombstone is True:
                summary["subject_deleted"] = summary.get("subject_deleted", 0) + 1
                continue
            if tombstone is None:
                summary["wiki_tombstone_unavailable"] = (
                    summary.get("wiki_tombstone_unavailable", 0) + 1
                )
                continue
            try:
                frontmatter = normalize_frontmatter(
                    read_frontmatter_only(resolved_path, errors="strict")
                )
            except (OSError, UnicodeError, ValueError):
                summary["acl_metadata_missing"] = summary.get("acl_metadata_missing", 0) + 1
                continue
            decision = authorize_item(
                principal,
                {"page_path": relative_path, "frontmatter": frontmatter},
                narrowing,
            )
            summary[decision.reason] = summary.get(decision.reason, 0) + 1
            if decision.allowed:
                allowed[relative_path] = frontmatter
        return allowed, summary

    def _authorized_frontmatter_for(self, relative_path: str) -> Dict[str, Any] | None:
        """Return an already-authorized frontmatter record for one page path."""
        if self._authorized_page_frontmatter is None:
            return None
        normalized = str(relative_path or "").replace("\\", "/")
        return self._authorized_page_frontmatter.get(normalized)

    def read_authorized_page(
        self,
        page_path: str,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None = None,
    ) -> str | None:
        """Read one Wiki page only after frontmatter-only authorization.

        Question-answer assembly and other prompt builders must use this
        boundary rather than opening a result path directly.
        """
        if principal is None:
            return None
        from core.frontmatter import normalize_frontmatter, read_frontmatter_only, read_markdown

        wiki_root = self.wiki_base.expanduser().resolve(strict=False)
        normalized = str(page_path or "").replace("\\", "/")
        if not normalized.endswith(".md"):
            normalized += ".md"
        target = (wiki_root / normalized).resolve(strict=False)
        try:
            relative_path = target.relative_to(wiki_root).as_posix()
        except ValueError:
            return None
        if not target.is_file():
            return None
        tombstone = self._wiki_tombstone_state(target)
        if tombstone is not False:
            return None
        try:
            frontmatter = normalize_frontmatter(read_frontmatter_only(target, errors="strict"))
        except (OSError, UnicodeError, ValueError):
            return None
        decision = authorize_item(
            principal,
            {"page_path": relative_path, "frontmatter": frontmatter},
            narrowing or AccessNarrowing(),
        )
        if not decision.allowed:
            return None
        try:
            return cast(str, read_markdown(target, errors="strict"))
        except (OSError, UnicodeDecodeError):
            return None

    def _filter_superseded_recap_candidates(self, candidates: List[Dict]) -> List[Dict]:
        """Exclude recap pages whose durable correction state supersedes retrieval."""
        recap_candidates = [
            candidate
            for candidate in candidates
            if str((candidate.get("frontmatter") or {}).get("recap_id") or "")
        ]
        if not recap_candidates:
            return candidates
        if self._database_dir is None:
            return candidates
        db_path = self._database_dir / "recap_tasks.db"
        if not db_path.exists():
            return candidates
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2) as conn:
                table = conn.execute("""
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='recap_effect_states'
                    """).fetchone()
                if not table:
                    return candidates
                superseded = {str(row[0]) for row in conn.execute("""
                        SELECT recap_id FROM recap_effect_states
                        WHERE canonical_target='knowledge_retrieval'
                          AND status IN ('superseded', 'blocked')
                        """).fetchall()}
        except sqlite3.Error:
            logger.warning(
                "Recap correction state is unavailable; suppressing retrospective candidates",
                exc_info=True,
            )
            return [
                candidate
                for candidate in candidates
                if not str((candidate.get("frontmatter") or {}).get("recap_id") or "")
            ]
        return [
            candidate
            for candidate in candidates
            if str((candidate.get("frontmatter") or {}).get("recap_id") or "") not in superseded
        ]

    def _get_metrics(self):
        """获取与当前 wiki 对应的热力指标账本。"""
        if self._database_dir is None:
            return None
        try:
            from core.wiki_metrics import WikiMetrics

            return WikiMetrics(
                db_path=str(self._database_dir / "wiki_metrics.db"),
                wiki_dir=str(self.wiki_base.expanduser().resolve()),
            )
        except (ImportError, OSError):
            logger.debug("热力指标账本不可用", exc_info=True)
            return None

    def _get_heat_info(self, page_path: str) -> Dict[str, Any]:
        if self._database_dir is None:
            return {"level": "cold", "score": 0.0, "last_accessed": ""}
        db_path = self._database_dir / "wiki_metrics.db"
        if not db_path.exists():
            return {"level": "cold", "score": 0.0, "last_accessed": ""}
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    """
                    SELECT heat_level, heat_score, last_accessed
                    FROM page_metrics WHERE wiki_path = ?
                    """,
                    (page_path,),
                ).fetchone()
            if row:
                return {
                    "level": row[0] or "cold",
                    "score": float(row[1] or 0.0),
                    "last_accessed": row[2] or "",
                }
        except (OSError, ValueError, sqlite3.Error):
            logger.debug("读取热力指标失败: %s", page_path, exc_info=True)
        return {"level": "cold", "score": 0.0, "last_accessed": ""}

    def _recall_from_embedding(
        self,
        query: str,
        *,
        context: Optional[Dict] = None,
    ) -> List[Dict]:
        """语义召回：双索引融合检索（页面向量 + 关联上下文向量）"""
        if self._authorized_page_frontmatter is None:
            return []
        try:
            cfg = get_config()
            if not cfg.get("embedding.enabled", True):
                return []

            from core.embeddings.dual_index import DualIndexRetriever
            from core.embeddings.index_manager import EmbeddingIndexManager

            index_dir = self._embedding_index_dir
            if index_dir is None or not index_dir.is_dir():
                return []
            page_index = EmbeddingIndexManager(
                wiki_base=self.wiki_base,
                index_dir=index_dir,
            )
            if page_index.client is None:
                self._merge_trace_degradation(
                    "page_embedding_client_unavailable",
                    embedding=True,
                )
                return []
            if not page_index.persisted_search_available():
                self._merge_trace_degradation(
                    "page_embedding_index_unavailable",
                    embedding=True,
                )
                return []
            relation_manager = None
            graph_db = self._knowledge_graph_db
            if graph_db is not None and graph_db.is_file():
                from core.embeddings.relation_manager import RelationEmbeddingManager

                relation_manager = RelationEmbeddingManager(
                    db_path=graph_db,
                    index_dir=index_dir,
                )
            retriever = DualIndexRetriever(
                page_index=page_index,
                relation_manager=relation_manager,
                wiki_base=self.wiki_base,
            )
            self._dual_index_retriever = retriever
            from core.telemetry.prompt_call_log import model_call_run_scope

            with model_call_run_scope(
                cfg,
                "context_search_embedding",
                subject_scope=self._model_call_subject_scope(
                    context,
                    fallback_source="context_search_embedding",
                ),
            ):
                semantic_results = retriever.search_detailed(
                    query,
                    top_k=15,
                    use_rerank=False,
                    allowed_page_paths=set(self._authorized_page_frontmatter),
                )
            self._merge_dual_index_trace(retriever.get_last_trace())
            if not semantic_results:
                return []

            candidates = []
            for rel_path, sim, page_sc, rel_sc in semantic_results:
                rel_parts = Path(rel_path).parts
                if any(part in self.EXCLUDED_DIRS or part.startswith(".") for part in rel_parts):
                    continue
                canonical_rel_path = str(Path(rel_path).as_posix())
                frontmatter = self._authorized_frontmatter_for(canonical_rel_path)
                if frontmatter is None:
                    continue
                page_path = self.wiki_base / rel_path
                if not page_path.exists():
                    continue
                try:
                    from core.frontmatter import read_markdown

                    content = read_markdown(page_path, errors="ignore")
                    title = frontmatter.get("名称") or frontmatter.get("title") or page_path.stem
                    candidates.append(
                        {
                            "path": canonical_rel_path,
                            "title": title,
                            "content": content,
                            "entity": title,
                            "frontmatter": frontmatter,
                            "semantic_score": sim,
                            "page_embedding_score": page_sc,
                            "relation_score": rel_sc,
                            "match_type": "semantic",
                        }
                    )
                except (OSError, UnicodeDecodeError):
                    continue
            return candidates
        except (
            ImportError,
            OSError,
            ValueError,
            TypeError,
            AttributeError,
            RuntimeError,
        ) as e:
            # Provider/client exceptions can embed the search input, response
            # body, credentials, or a caller-controlled exception class name.
            # The query trace is public diagnostic output, so keep only the
            # reviewed category rather than rendering the exception/traceback.
            error_category = safe_provider_error_category(e)
            logger.debug("语义召回失败: category=%s", error_category)
            self.last_query_trace["embedding_attempted"] = True
            self._merge_trace_degradation(
                f"semantic_recall_exception:{error_category}",
                embedding=True,
            )
            return []

    def _merge_candidates(self, semantic: List[Dict], traditional: List[Dict]) -> List[Dict]:
        """融合语义召回和传统召回结果，去重；同时标记 hybrid 来源"""
        seen = {}
        merged = []
        for c in semantic:
            path = c.get("path", "")
            if path:
                seen[path] = c
                merged.append(c)
        for c in traditional:
            path = c.get("path", "")
            if not path:
                continue
            if path in seen:
                # 同时被语义和传统召回命中，标记为 hybrid，并保留关键词相关度
                existing = seen[path]
                existing["match_type"] = "hybrid"
                existing["_has_keyword_match"] = True
                # 保留关键词召回的 confidence / verification 等更丰富的字段
                for key in [
                    "confidence",
                    "verification",
                    "source",
                    "last_modified",
                    "keyword_score",
                ]:
                    if key in c and (key not in existing or not existing.get(key)):
                        existing[key] = c[key]
                # hybrid 时保留语义分解分数（如果语义侧有的话）
                for key in ["page_embedding_score", "relation_score"]:
                    if key in existing and existing.get(key, 0.0) > 0.0:
                        pass  # 保留语义侧已有分数
                    elif key in c:
                        existing[key] = c[key]
            else:
                seen[path] = c
                merged.append(c)
        return merged

    def _recall_from_kg(self, query: str) -> List[Dict]:
        """从知识图谱召回候选页面"""
        if self._authorized_page_frontmatter is None:
            return []
        try:
            from core.kia.knowledge_graph import KnowledgeGraph

            db_path = self._knowledge_graph_db
            if db_path is None or not db_path.is_file():
                return []
            kg = KnowledgeGraph(
                db_path=str(db_path),
                wiki_base=str(self.wiki_base),
                embedding_index_dir=(
                    str(self._embedding_index_dir)
                    if self._embedding_index_dir is not None
                    else None
                ),
                initialize=False,
                read_only=True,
            )
            results = kg.search(
                query,
                limit=20,
                allowed_page_paths=self._authorized_page_frontmatter.keys(),
                # ContextAwareSearch owns the one Wiki semantic channel above.
                # Re-entering KnowledgeGraph's semantic path would duplicate
                # provider/rerank calls and ignore allow_embedding=False.
                allow_semantic=False,
            )
            filtered = []
            for r in results:
                path = str(r.get("page_path", "")).replace("\\", "/")
                frontmatter = self._authorized_frontmatter_for(path)
                if frontmatter is None:
                    continue
                if path:
                    rel_parts = Path(path).parts
                    if any(
                        part in self.EXCLUDED_DIRS or part.startswith(".") for part in rel_parts
                    ):
                        continue
                filtered.append(
                    {
                        "path": path,
                        "title": r.get("title", ""),
                        "content": r.get("content", ""),
                        "entity": r.get("entity_name", ""),
                        "frontmatter": frontmatter,
                        "match_type": "graph",
                    }
                )
            return filtered
        except (ImportError, OSError) as e:
            logger.debug("KG 召回失败: %s", e, exc_info=True)
            return []

    def _is_excluded_path(self, md_file: Path) -> bool:
        """检查文件路径是否位于排除目录中。"""
        try:
            rel_parts = md_file.relative_to(self.wiki_base).parts
        except ValueError:
            return True
        return any(part in self.EXCLUDED_DIRS or part.startswith(".") for part in rel_parts)

    def _build_search_text(self, content_lower: str, fm: Optional[Dict]) -> str:
        """拼接正文与 frontmatter 中文字段作为搜索文本。"""
        search_text = content_lower
        if not fm:
            return search_text
        for field_name in ["名称", "title", "摘要", "description", "关键词", "keywords"]:
            val = fm.get(field_name)
            if isinstance(val, str):
                search_text += " " + val.lower()
            elif isinstance(val, list):
                search_text += " " + " ".join(str(v).lower() for v in val)
        return search_text

    def _candidate_from_file(
        self,
        md_file: Path,
        keywords: List[str],
        frontmatter: Dict[str, Any],
    ) -> Optional[Dict]:
        """从单个 Markdown 文件构建召回候选。"""
        from core.frontmatter import read_markdown

        content = read_markdown(md_file, errors="ignore")
        content_lower = content.lower()
        title = frontmatter.get("名称") or frontmatter.get("title") or md_file.stem
        search_text = self._build_search_text(content_lower, frontmatter)
        matched = [kw for kw in keywords if kw in search_text]
        if not matched:
            return None

        keyword_score = min(len(set(matched)) / max(1, min(len(keywords), 4)), 1.0)
        verification = ""
        confidence = 0.5
        source = ""
        if frontmatter:
            verification = frontmatter.get("验证状态", frontmatter.get("verification", ""))
            confidence = float(frontmatter.get("置信度", frontmatter.get("confidence", 0.5)) or 0.5)
            source = frontmatter.get("来源", frontmatter.get("source", ""))
        if verification == "pending-verification":
            return None

        rel_path = str(md_file.relative_to(self.wiki_base))
        return {
            "path": rel_path,
            "title": title,
            "content": content,
            "frontmatter": frontmatter,
            "verification": verification,
            "source": source,
            "confidence": confidence,
            "last_modified": (
                datetime.fromtimestamp(md_file.stat().st_mtime).isoformat()
                if md_file.exists()
                else ""
            ),
            "keyword_score": keyword_score,
            "match_type": "keyword",
        }

    def _recall_from_files(self, query: str) -> List[Dict]:
        """从文件系统召回（回退方案）"""
        if self._authorized_page_frontmatter is None:
            return []
        candidates: List[Dict] = []
        keywords = self._query_terms(query)

        for relative_path, frontmatter in sorted(self._authorized_page_frontmatter.items()):
            md_file = self.wiki_base / relative_path
            try:
                if not md_file.is_file() or self._is_excluded_path(md_file):
                    continue
                candidate = self._candidate_from_file(md_file, keywords, frontmatter)
                if candidate:
                    candidates.append(candidate)
            except (OSError, UnicodeDecodeError):
                logger.warning("文件召回异常: %s", md_file, exc_info=True)
                continue

        candidates.sort(
            key=lambda c: c.get("keyword_score", 0.0), reverse=True  # type: ignore[arg-type, return-value]  # noqa: E501
        )  # type: ignore[arg-type, return-value]
        return candidates

    def _compute_relevance(self, query: str, candidate: Dict) -> float:
        """计算查询与候选内容的相关性（纳入 frontmatter 中文字段）"""
        details = self._keyword_match_details(query, candidate)
        keywords = details["terms"]
        if not keywords:
            return 0.0

        denominator = details["denominator"]
        field_matches = details["field_matches"]
        title_score = len(field_matches["title"]) / denominator
        fm_score = len(field_matches["frontmatter"]) / denominator
        content_score = len(field_matches["content"]) / denominator
        path_score = len(field_matches["path"]) / denominator
        keyword_relevance = min(
            title_score * 0.40 + fm_score * 0.30 + content_score * 0.25 + path_score * 0.05,
            1.0,
        )
        coverage = min(len(details["matched_terms"]) / denominator, 1.0)
        if details["matched_terms"]:
            keyword_relevance = max(keyword_relevance, coverage * 0.35)

        # 语义召回的结果：如果关键词完全不命中，大幅降级，避免关系 boost 淹没关键词
        if candidate.get("match_type") == "semantic":
            semantic_score = candidate.get("semantic_score", 0.0)
            if keyword_relevance < 0.1:
                # 纯语义结果但无关键词命中：relevance 最多给语义分的 30%
                return semantic_score * 0.3  # type: ignore[no-any-return]
            # 有少量关键词命中，语义分与关键词分加权
            return max(semantic_score * 0.5, keyword_relevance)  # type: ignore[no-any-return]

        # hybrid / keyword 结果：关键词相关性保底
        if candidate.get("match_type") in ("hybrid", "keyword"):
            # exact match 保护：如果核心 token 出现在标题或路径中，relevance 至少 0.8
            core_terms = [kw for kw in keywords if len(kw) >= 3]
            high_signal_terms = (
                set(field_matches["title"])
                | set(field_matches["frontmatter"])
                | set(field_matches["path"])
            )
            exact_hits = sum(1 for kw in core_terms if kw in high_signal_terms)
            if core_terms and exact_hits / len(core_terms) >= 0.5:
                keyword_relevance = max(keyword_relevance, 0.8)
            return keyword_relevance  # type: ignore[no-any-return]

        return keyword_relevance  # type: ignore[no-any-return]

    def _compute_confidence(self, candidate: Dict) -> float:
        """计算候选页面的置信度"""
        candidate_confidence = self._coerce_score(candidate.get("confidence"), default=0.5)
        entity = candidate.get("entity", "")
        if not entity:
            return candidate_confidence

        try:
            db_path = self._knowledge_graph_db
            if db_path is None or not db_path.is_file():
                return candidate_confidence
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    """
                    SELECT confidence FROM entities
                    WHERE name = ? ORDER BY (status = 'active') DESC LIMIT 1
                    """,
                    (entity,),
                ).fetchone()
            if row:
                return max(candidate_confidence, float(row[0]))
        except (OSError, ValueError, sqlite3.Error):
            logger.warning("置信度计算失败", exc_info=True)
        return candidate_confidence

    def _record_authorized_entity_accesses(
        self,
        results: List[SearchResult],
    ) -> None:
        entities = {result.entity for result in results if result.entity}
        if not entities:
            return
        try:
            from core.kia.kg_event_handler import KGEventHandler

            current_wiki = self.wiki_base.expanduser().resolve(strict=False)
            db_path = self._knowledge_graph_db
            if db_path is None or not db_path.is_file():
                return
            handler = KGEventHandler(
                db_path=db_path,
                wiki_base=current_wiki,
                embedding_index_dir=self._embedding_index_dir,
            )
            for entity in sorted(entities):
                handler.on_entity_accessed(entity)
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
            logger.debug("授权后实体访问事件发射失败", exc_info=True)

    def _compute_continuity(self, candidate: Dict, context: Dict) -> float:
        """计算浏览连续性 — 与当前上下文的关联程度"""
        if not context:
            return 0.3

        score = 0.0
        candidate_path = candidate.get("path", "")

        # 检查是否与最近访问的页面有链接关系
        recent_pages = context.get("recent_pages", [])
        for rp in recent_pages:
            if candidate_path in str(rp) or str(rp) in candidate_path:
                score += 0.3
                break

        # 检查是否与活跃实体匹配
        active_entities = context.get("active_entities", [])
        content = candidate.get("content", "").lower()
        for entity in active_entities:
            if entity.lower() in content:
                score += 0.2
                break

        # 工作目录相关
        working_dir = context.get("working_dir", "")
        if working_dir and working_dir.lower() in content:
            score += 0.2

        return min(score, 1.0)

    def _compute_context_boost(self, candidate: Dict, context: Dict) -> float:
        if not context:
            return 1.0
        boost = 1.0
        candidate_path = candidate.get("path", "")
        working_dir = context.get("working_dir", "")
        if working_dir:
            parts = [p.lower() for p in Path(working_dir).parts if len(p) > 2]
            if any(part in candidate_path.lower() for part in parts):
                boost *= 1.05
        recent_pages = {str(p) for p in context.get("recent_pages", [])}
        if candidate_path in recent_pages:
            boost *= 1.05
        return min(boost, 1.15)

    def _compute_freshness(self, candidate: Dict) -> float:
        """计算内容新鲜度 — 基于半衰期衰减"""
        path = candidate.get("path", "")
        if not path:
            return 0.5

        try:
            md_file = self.wiki_base / path
            if md_file.exists():
                mtime = datetime.fromtimestamp(md_file.stat().st_mtime)
                age_days = (datetime.now() - mtime).days
                # 半衰期衰减
                half_life = get_effective_policy().get(
                    "knowledge_graph.freshness_decay_half_life_days",
                    CONTEXT_AWARE_SEARCH_DURATION_BUCKET_MONTH_DAYS,
                )
                try:
                    half_life_days = float(half_life)
                except (TypeError, ValueError):
                    logger.debug("新鲜度半衰期配置无效，使用默认值: %r", half_life)
                    half_life_days = float(CONTEXT_AWARE_SEARCH_DURATION_BUCKET_MONTH_DAYS)
                freshness = 0.5 ** (age_days / max(half_life_days, 1.0))
                return freshness  # type: ignore[no-any-return]
        except (OSError, ValueError, TypeError):
            logger.warning("新鲜度计算失败", exc_info=True)
        return 0.5

    def _extract_snippet(self, candidate: Dict, query: str) -> str:
        """Extract a bounded snippet centered on the earliest matched term."""
        content = str(candidate.get("content", "") or "")
        keywords = self._query_terms(query)
        if not content:
            return ""
        lowered = content.lower()
        offsets = [lowered.find(keyword) for keyword in keywords]
        matched_offsets = [offset for offset in offsets if offset >= 0]
        offset = min(matched_offsets) if matched_offsets else 0
        matched_length = max(
            (len(keyword) for keyword in keywords if lowered.find(keyword) == offset),
            default=0,
        )
        radius = 100
        start = max(0, offset - radius)
        end = min(len(content), offset + matched_length + radius)
        snippet = content[start:end].strip()
        return ("…" if start else "") + snippet + ("…" if end < len(content) else "")

    @staticmethod
    def _coerce_score(value: Any, default: float = 0.0) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return default
        return max(0.0, min(score, 1.0))

    @staticmethod
    def _dedupe_terms(terms: List[str]) -> List[str]:
        seen = set()
        deduped = []
        for term in terms:
            if not term or term in seen:
                continue
            seen.add(term)
            deduped.append(term)
        return deduped

    @staticmethod
    def _strip_intent_noise(query: str) -> str:
        cleaned = query.lower()
        for phrase in (
            "查知识",
            "查上下文",
            "查一下",
            "查询一下",
            "查询",
            "搜索一下",
            "搜索",
            "搜一下",
            "找一下",
            "帮我查",
            "帮我找",
            "请帮我",
            "看看",
            "相关知识",
        ):
            cleaned = cleaned.replace(phrase, " ")
        return cleaned.strip() or query.lower()

    def _keyword_match_details(self, query: str, candidate: Dict) -> Dict[str, Any]:
        terms = self._query_terms(query)
        field_texts = self._field_texts(candidate)
        field_matches = {
            field: [term for term in terms if term in text] for field, text in field_texts.items()
        }
        matched_terms = self._dedupe_terms(
            [term for matches in field_matches.values() for term in matches]
        )
        return {
            "terms": terms,
            "denominator": max(1, min(len(terms), 4)),
            "field_matches": field_matches,
            "matched_terms": matched_terms,
        }

    def _field_texts(self, candidate: Dict) -> Dict[str, str]:
        fm_text = ""
        fm = candidate.get("frontmatter", {}) or {}
        for field_name in [
            "名称",
            "title",
            "摘要",
            "description",
            "关键词",
            "keywords",
            "aliases",
            "领域",
            "domain",
            "tags",
        ]:
            val = fm.get(field_name)
            if isinstance(val, str):
                fm_text += " " + val.lower()
            elif isinstance(val, list):
                fm_text += " " + " ".join(str(v).lower() for v in val)

        return {
            "title": str(candidate.get("title", "")).lower(),
            "frontmatter": fm_text.lower(),
            "content": str(candidate.get("content", "")).lower(),
            "path": str(candidate.get("path", "")).lower(),
        }

    def _build_score_breakdown(
        self,
        candidate: Dict,
        relevance: float,
        confidence: float,
        continuity: float,
        freshness: float,
        persona_score: float,
        context_boost: float,
        final_score: float,
        match_details: Dict[str, Any],
    ) -> Dict[str, float]:
        field_matches = match_details["field_matches"]
        return {
            "final": round(final_score, 3),
            "relevance": round(relevance, 3),
            "confidence": round(confidence, 3),
            "continuity": round(continuity, 3),
            "freshness": round(freshness, 3),
            "persona": round(persona_score, 3),
            "context_boost": round(context_boost, 3),
            "keyword": round(float(candidate.get("keyword_score", 0.0) or 0.0), 3),
            "semantic": round(float(candidate.get("semantic_score", 0.0) or 0.0), 3),
            "title_matches": len(field_matches["title"]),
            "frontmatter_matches": len(field_matches["frontmatter"]),
            "content_matches": len(field_matches["content"]),
            "path_matches": len(field_matches["path"]),
        }

    @classmethod
    def _query_terms(cls, query: str) -> List[str]:
        cleaned = cls._strip_intent_noise(query)
        raw_tokens = re.findall(r"[\u4e00-\u9fa5]+|[a-zA-Z0-9_\-]{2,}", cleaned)
        terms: List[str] = []
        for token in raw_tokens:
            if re.fullmatch(r"[\u4e00-\u9fa5]+", token):
                if len(token) >= 2:
                    terms.append(token)
                if len(token) > 2:
                    terms.extend(token[i : i + 2] for i in range(len(token) - 1))
                if len(token) > 4:
                    terms.extend(token[i : i + 3] for i in range(len(token) - 2))
            else:
                terms.append(token)
        return cls._dedupe_terms(terms) or [query.lower()]

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict:
        fm, _ = parse_frontmatter(content)
        return fm or {}

    def _explain_match(
        self,
        relevance: float,
        confidence: float,
        continuity: float,
        freshness: float,
        context_boost: float,
        match_details: Optional[Dict[str, Any]] = None,
    ) -> str:
        reasons = []
        if relevance >= 0.5:
            reasons.append("关键词匹配")
        if match_details:
            matched_terms = match_details.get("matched_terms", [])
            if matched_terms:
                reasons.append("关键词命中:" + ",".join(matched_terms[:6]))
            field_labels = {
                "title": "标题",
                "frontmatter": "frontmatter",
                "content": "正文",
                "path": "路径",
            }
            matched_fields = [
                field_labels[field]
                for field, matches in match_details.get("field_matches", {}).items()
                if matches
            ]
            if matched_fields:
                reasons.append("命中字段:" + "/".join(matched_fields))
        if confidence >= 0.7:
            reasons.append("高置信知识")
        if continuity >= 0.5:
            reasons.append("上下文连续")
        if freshness >= 0.7:
            reasons.append("内容较新")
        if context_boost > 1.0:
            reasons.append("情境加权")
        reasons.append(f"分数:相关{relevance:.2f}/置信{confidence:.2f}/新鲜{freshness:.2f}")
        return "、".join(reasons) or "基础相关"

    @staticmethod
    def _get_freshness_checker():
        try:
            from core.kia.proteus import KnowledgeFreshnessChecker

            return KnowledgeFreshnessChecker()
        except (ImportError, OSError):
            logger.warning("新鲜度检查器初始化失败", exc_info=True)
            return None
