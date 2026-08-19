"""ACL-first authorization helpers for context-aware retrieval."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast, Dict, List, Optional, TYPE_CHECKING

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.app.context_search_models import SearchResult
from core.config import get_config
from core.utils import read_bytes_value

logger = logging.getLogger(__name__)
_LIFECYCLE_UNCHECKED = object()


def build_search_subject_provenance(
    *,
    query: str,
    principal: PrincipalEnvelope | None,
    narrowing: AccessNarrowing | None,
) -> dict[str, Any] | None:
    """Bind a search-derived scoring object to the authenticated request."""

    if principal is None:
        return None
    effective = narrowing or AccessNarrowing()
    session_id = str(effective.session_id or "").strip()
    project = str(effective.project or "").strip().lower()
    if session_id:
        scope_type, scope_id = "session", session_id
    elif project:
        scope_type, scope_id = "project", project
    else:
        scope_type, scope_id = "agent", str(principal.agent or "").strip().lower()
    if not scope_id:
        return None
    from core.cognitive.access_control import make_cognitive_access_envelope

    request_hash = hashlib.sha256(str(query).encode("utf-8")).hexdigest()
    scope_resolved = bool(session_id or project)
    return cast(
        dict[str, Any],
        make_cognitive_access_envelope(
            owner_principal_id=principal.principal_id,
            owner_agent=principal.agent,
            scope_type=scope_type,
            scope_id=scope_id,
            session_id=session_id,
            project=project,
            purposes=(
                "cognitive_state_read",
                "cognitive_state_write",
                "reflection_experience_read",
                "reflection_read",
                "score_training",
                "search_feedback",
            ),
            consent_provenance_refs=(
                (f"search-request:sha256:{request_hash}",) if scope_resolved else ()
            ),
            sensitivity="sensitive" if scope_resolved else "restricted",
            retention_policy="mnemos_search_sessions",
            source_acl_lineage=(f"sha256:{request_hash}",),
            visibility="private" if scope_resolved else "restricted",
            scope_resolution="resolved" if scope_resolved else "restricted_unknown",
            consent_status="granted" if scope_resolved else "restricted_unknown",
        ),
    )


class ContextSearchAuthorizationMixin:
    """Pre-body ACL, typed cognition authorization, and authorized reranking."""

    if TYPE_CHECKING:
        wiki_base: Path
        _database_dir: Path | None
        _wiki_projection_db: Path | None
        _cognitive_state_db: Path | None
        _cognitive_graph_db: Path | None
        _evidence_graph_db: Path | None
        _cognitive_searcher: Any | None
        _dual_index_retriever: Any | None
        last_query_trace: Dict[str, Any]

        @staticmethod
        def _model_call_subject_scope(
            context: Optional[Dict[str, Any]],
            *,
            fallback_source: str,
        ) -> tuple[str, str]: ...

        def _merge_trace_degradation(self, reason: str, *, rerank: bool = False) -> None: ...

        def _record_search_hits(self, results: List[SearchResult]) -> None: ...

        def _record_search_session(
            self,
            query: str,
            results: List[SearchResult],
            *,
            subject_provenance: Mapping[str, Any] | None = None,
        ) -> None: ...

        def _record_authorized_profile_usage(
            self,
            *,
            principal: PrincipalEnvelope,
            narrowing: AccessNarrowing,
        ) -> None: ...

        def _record_authorized_entity_accesses(self, results: List[SearchResult]) -> None: ...

    def _wiki_projection_db_path(self) -> Path | None:
        """Resolve the ledger that owns this searcher's exact Wiki root."""

        return self._wiki_projection_db

    def _cognitive_state_db_path(self) -> Path | None:
        return self._cognitive_state_db

    def _cognitive_database_dir(self) -> Path | None:
        return self._database_dir

    def _cognitive_graph_db_path(self) -> Path | None:
        return self._cognitive_graph_db

    def _evidence_graph_db_path(self) -> Path | None:
        return self._evidence_graph_db

    def _wiki_tombstone_state(self, page_path: Path) -> bool | None:
        db_path = self._wiki_projection_db_path()
        if db_path is None:
            return False
        from core.wiki_projection_lifecycle import WikiProjectionLedger

        return WikiProjectionLedger.tombstone_state(db_path, page_path)

    def _wiki_tombstone_states(
        self,
        page_paths: List[Path],
    ) -> Dict[str, bool | None]:
        """Resolve a set of page lifecycle headers from one DB snapshot."""

        if not page_paths:
            return {}
        db_path = self._wiki_projection_db_path()
        if db_path is None:
            return {str(path.expanduser().resolve(strict=False)): False for path in page_paths}
        from core.wiki_projection_lifecycle import WikiProjectionLedger

        return WikiProjectionLedger.tombstone_states(db_path, page_paths)

    @staticmethod
    def _acl_schema_version(frontmatter: Dict[str, Any]) -> int:
        try:
            return int(frontmatter.get("acl_schema_version") or 0)
        except (TypeError, ValueError):
            return 0

    def _read_canonical_acl_frontmatter(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        """Read ACL only from canonical frontmatter, never a recall cache/body."""
        from core.frontmatter import normalize_frontmatter, read_frontmatter_only

        relative_path = str(candidate.get("path") or "").strip()
        if not relative_path:
            return {}
        wiki_root = self.wiki_base.expanduser().resolve()
        page_path = (wiki_root / relative_path).resolve()
        try:
            page_path.relative_to(wiki_root)
            frontmatter = read_frontmatter_only(page_path, errors="strict")
        except (OSError, UnicodeError, ValueError):
            return {}
        return cast(Dict[str, Any], normalize_frontmatter(frontmatter))

    def _read_wiki_snapshot(
        self,
        relative_path: str,
        *,
        lifecycle_state: bool | None | object = _LIFECYCLE_UNCHECKED,
    ) -> tuple[str, str] | None:
        """Read one already-authorized page and bind its exact path/content bytes."""

        wiki_root = self.wiki_base.expanduser().resolve(strict=False)
        try:
            unresolved_path = wiki_root / relative_path
            if unresolved_path.is_symlink():
                return None
            page_path = unresolved_path.resolve(strict=True)
            canonical_path = page_path.relative_to(wiki_root).as_posix()
            if canonical_path != str(Path(relative_path).as_posix()):
                return None
            if lifecycle_state is _LIFECYCLE_UNCHECKED:
                lifecycle_state = self._wiki_tombstone_state(page_path)
            if lifecycle_state is not False:
                return None
            payload = read_bytes_value(page_path)
            content = payload.decode("utf-8", errors="strict")
        except (OSError, UnicodeError, ValueError):
            return None
        digest = hashlib.sha256(canonical_path.encode("utf-8") + b"\0" + payload).hexdigest()
        return content, f"wiki_page:{canonical_path}:sha256:{digest}"

    @staticmethod
    def _wiki_match_trace(
        candidate: Dict[str, Any],
        match_details: Dict[str, Any],
        revision_id: str,
    ) -> tuple[str, List[str]]:
        """Return the exact matched field and a content offset or structural span."""

        field_matches = match_details.get("field_matches") or {}
        field_name = next(
            (
                name
                for name in ("content", "title", "frontmatter", "path")
                if field_matches.get(name)
            ),
            "content",
        )
        matched_field = f"wiki.{field_name}"
        content = str(candidate.get("content") or "")
        lowered = content.lower()
        terms = [str(term) for term in field_matches.get(field_name, ()) if str(term)]
        offsets = [
            (lowered.find(term.lower()), term) for term in terms if lowered.find(term.lower()) >= 0
        ]
        if offsets:
            start, term = min(offsets, key=lambda item: item[0])
            return matched_field, [f"{revision_id}#{start}:{start + len(term)}"]
        if content:
            return matched_field, [f"{revision_id}#0:{len(content)}"]
        return matched_field, []

    def _recall_from_cognitive_state(
        self,
        query: str,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
        limit: int,
    ) -> tuple[List[SearchResult], Dict[str, Any]]:
        from core.cognitive.search import CognitiveSearch

        state_db = self._cognitive_state_db_path()
        if state_db is None:
            return [], {"channel_not_configured": 1}
        cognitive_search = CognitiveSearch(
            state_db=state_db,
            cognitive_graph_db=self._cognitive_graph_db_path(),
            evidence_graph_db=self._evidence_graph_db_path(),
        )
        self._cognitive_searcher = cognitive_search
        hits, access = cognitive_search.search(
            query,
            principal=principal,
            narrowing=narrowing,
            limit=limit,
        )
        results = [
            SearchResult(
                page_path=(
                    f"mnemos://{hit.channel.replace('_', '-')}/"
                    f"{hit.object_type}/{hit.object_id}/{hit.revision_id}"
                ),
                title=hit.title,
                snippet=hit.snippet,
                score=hit.score,
                relevance=hit.score,
                confidence=hit.confidence,
                freshness=1.0,
                final_score=hit.score,
                match_reason=f"typed cognition field: {hit.matched_field}",
                source=f"{hit.channel}_store",
                match_source=hit.channel,
                score_breakdown={"final": round(hit.score, 3), "typed_cognition": 1.0},
                matched_terms=list(hit.matched_terms),
                scope=hit.scope_type,
                project=narrowing.project,
                acl_schema_version=1,
                acl_metadata_complete=True,
                acl_reconciliation_status="canonical_cognitive_acl",
                result_kind=hit.channel,
                object_type=hit.object_type,
                object_id=hit.object_id,
                revision_id=hit.revision_id,
                matched_field=hit.matched_field,
                source_revision_id=hit.source_revision_id,
                source_span_ids=list(hit.source_span_ids),
                acl_decision=hit.acl_decision,
                supersedes_revision_id=hit.supersedes_revision_id,
                is_current=hit.is_current,
            )
            for hit in hits
        ]
        return results, access

    def reauthorize_cognitive_result(
        self,
        result: SearchResult,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> tuple[bool, str]:
        """Reauthorize one non-Wiki result at the application exposure seam."""

        searcher = self._cognitive_searcher
        if searcher is None:
            from core.cognitive.search import CognitiveSearch

            state_db = self._cognitive_state_db_path()
            if state_db is None:
                return False, "channel_not_configured"
            searcher = CognitiveSearch(
                state_db=state_db,
                cognitive_graph_db=self._cognitive_graph_db_path(),
                evidence_graph_db=self._evidence_graph_db_path(),
            )
        return cast(
            tuple[bool, str],
            searcher.authorize_identity(
                channel=result.result_kind,
                object_type=result.object_type,
                object_id=result.object_id,
                revision_id=result.revision_id,
                source_revision_id=result.source_revision_id,
                source_span_ids=result.source_span_ids,
                matched_field=result.matched_field,
                acl_decision=result.acl_decision,
                is_current=result.is_current,
                principal=principal,
                narrowing=narrowing,
            ),
        )

    def record_authorized_search(
        self,
        query: str,
        results: List[SearchResult],
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing | None = None,
    ) -> dict[str, Any] | None:
        """Record side effects only for results that passed access policy."""

        subject_provenance = build_search_subject_provenance(
            query=query,
            principal=principal,
            narrowing=narrowing,
        )
        wiki_results = [
            result
            for result in results
            if getattr(result, "result_kind", "wiki_page") == "wiki_page"
        ]
        self._record_search_hits(wiki_results)
        self._record_search_session(
            query,
            results,
            subject_provenance=subject_provenance,
        )
        self._record_authorized_entity_accesses(wiki_results)
        return subject_provenance

    def rerank_authorized(
        self,
        query: str,
        results: List[SearchResult],
        *,
        limit: int,
        context: Optional[Dict] = None,
    ) -> List[SearchResult]:
        """Rerank content only after the application layer authorizes each item."""
        if not results:
            return []
        retriever = self._dual_index_retriever
        if retriever is None:
            return results[:limit]
        documents = [f"{result.title}\n{result.snippet}" for result in results]
        from core.telemetry.prompt_call_log import model_call_run_scope

        with model_call_run_scope(
            get_config(),
            "context_search_rerank",
            subject_scope=self._model_call_subject_scope(
                context,
                fallback_source="context_search_rerank",
            ),
        ):
            order, trace = retriever.rerank_authorized_documents(
                query,
                documents,
                top_n=limit,
            )
        for key in (
            "rerank_configured",
            "rerank_attempted",
            "rerank_api_called",
            "rerank_applied",
            "rerank_degraded",
        ):
            self.last_query_trace[key] = bool(trace.get(key))
        if trace.get("degraded"):
            for reason in trace.get("degraded_reasons", []):
                self._merge_trace_degradation(str(reason), rerank=True)
        selected = [results[index] for index in order if index < len(results)]
        self.last_query_trace["result_count"] = len(selected)
        return selected


__all__ = ["ContextSearchAuthorizationMixin", "build_search_subject_provenance"]
