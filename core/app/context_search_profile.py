"""Persona-aware ranking and immutable profile-effect receipts for context search."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Dict, List

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.app.context_search_models import SearchResult

logger = logging.getLogger(__name__)


class ContextSearchProfileMixin:
    """Keep governed Persona ranking separate from recall and page access."""

    def _compute_persona_score(self, candidate: Dict, profile: Dict) -> float:
        score, _matched = self._compute_persona_score_with_matches(candidate, profile)
        return score

    @staticmethod
    def _profile_rank_candidate_id(result: SearchResult) -> str:
        return "|".join(
            (
                str(result.result_kind or "wiki_page"),
                str(result.object_type or "wiki_page"),
                str(result.object_id or result.page_path),
                str(result.revision_id or ""),
            )
        )

    @staticmethod
    def _build_profile_rank_effect(
        candidates: List[Dict[str, Any]],
        *,
        limit: int,
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], set[str], List[Dict[str, Any]]]:
        """Compare stable top-k identities and bind only assertions that moved them."""

        identities = [str(row.get("candidate_id") or "") for row in candidates]
        if any(not identity for identity in identities) or len(set(identities)) != len(identities):
            raise ValueError("context search rank candidates require unique identities")

        def ranked(score_field: str) -> List[Dict[str, Any]]:
            ordered = sorted(
                candidates,
                key=lambda row: (
                    -float(row.get(score_field, 0.0) or 0.0),
                    str(row["candidate_id"]),
                ),
            )[: max(0, int(limit))]
            return [
                {
                    "candidate_id": str(row["candidate_id"]),
                    "page_path": str(row.get("page_path") or ""),
                    "rank": index,
                }
                for index, row in enumerate(ordered, start=1)
            ]

        baseline = ranked("baseline_score")
        enabled = ranked("persona_enabled_score")
        if baseline == enabled:
            return baseline, enabled, set(), []

        baseline_ranks = {str(row["candidate_id"]): int(row["rank"]) for row in baseline}
        enabled_ranks = {str(row["candidate_id"]): int(row["rank"]) for row in enabled}
        changed_ids = {
            candidate_id
            for candidate_id in set(baseline_ranks) | set(enabled_ranks)
            if baseline_ranks.get(candidate_id) != enabled_ranks.get(candidate_id)
        }
        candidates_by_id = {str(row["candidate_id"]): row for row in candidates}
        changed: List[Dict[str, Any]] = []
        matched_assertions: set[str] = set()
        for candidate_id in sorted(changed_ids):
            row = candidates_by_id[candidate_id]
            row_matches = sorted(
                {
                    str(assertion_id)
                    for assertion_id in row.get("matched_assertion_ids") or ()
                    if str(assertion_id)
                }
            )
            matched_assertions.update(row_matches)
            changed.append(
                {
                    "candidate_id": candidate_id,
                    "baseline_rank": baseline_ranks.get(candidate_id),
                    "persona_enabled_rank": enabled_ranks.get(candidate_id),
                    "matched_assertion_ids": row_matches,
                }
            )
        return baseline, enabled, matched_assertions, changed

    def _compute_persona_score_with_matches(
        self,
        candidate: Dict,
        profile: Dict,
    ) -> tuple[float, List[str]]:
        """Score a candidate only when an authorized assertion matches it.

        Stored ``persona_alignment`` frontmatter is not an evidence-backed
        user profile and cannot affect ranking.  The profile assertions were
        ACL-authorized and revalidated against their evidence immediately
        before this score is computed.
        """

        assertions = profile.get("persona_assertions", []) or []
        if not assertions:
            return 0.0, []
        candidate_text = " ".join(
            (
                str(candidate.get("title", "")),
                str(candidate.get("content", "")),
                json.dumps(candidate.get("frontmatter", {}) or {}, ensure_ascii=False),
            )
        ).lower()
        if not candidate_text.strip():
            return 0.0, []
        best_score = 0.0
        best_assertion_ids: List[str] = []
        for assertion in assertions:
            claim_terms = self._persona_claim_terms(str(assertion.get("claim", "")))
            if not claim_terms:
                continue
            matches = sum(1 for term in claim_terms if term in candidate_text)
            # One generic bi-gram is not enough to establish relevance.  It
            # must match at least two terms, unless the assertion has exactly
            # one distinctive term.
            required_matches = 1 if len(claim_terms) == 1 else 2
            if matches < required_matches:
                continue
            coverage = matches / len(claim_terms)
            confidence = min(max(float(assertion.get("confidence", 0.0) or 0.0), 0.0), 1.0)
            assertion_score = confidence * coverage
            assertion_id = str(assertion.get("assertion_id") or "")
            if assertion_score > best_score:
                best_score = assertion_score
                best_assertion_ids = [assertion_id] if assertion_id else []
            elif assertion_score == best_score and assertion_id:
                best_assertion_ids.append(assertion_id)
        return min(best_score, 1.0), sorted(set(best_assertion_ids))

    @staticmethod
    def _persona_claim_terms(claim: str) -> List[str]:
        normalized = str(claim or "").lower()
        english = re.findall(r"[a-z0-9_\-]{3,}", normalized)
        chinese_runs = re.findall(r"[\u4e00-\u9fff]{2,}", normalized)
        chinese_terms: List[str] = []
        for run in chinese_runs:
            chinese_terms.extend(run[index : index + 2] for index in range(len(run) - 1))
        stop_terms = {"用户", "需要", "要求", "进行", "可以", "必须", "以及", "当前", "一个"}
        return sorted(
            {term for term in [*english, *chinese_terms] if term and term not in stop_terms}
        )

    def _get_profile_weights(
        self,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> Dict:
        """Load profile-derived weights through the assertion ACL seam."""
        if principal is None:
            return {}
        if self._database_dir is None:
            return {}
        try:
            from core.persona.psyche import SignalStore

            signal_db = self._database_dir / "user_signals.db"
            if not signal_db.exists():
                return {}
            store = SignalStore(db_path=signal_db)
            try:
                profile_v2, access = store.build_authorized_user_cognitive_profile_v2(
                    principal=principal,
                    narrowing=narrowing or AccessNarrowing(),
                    purpose="context_search_profile",
                    consumer="context_search",
                )
            finally:
                store.close()
            assertions = profile_v2.get("profile_assertions", []) or []

            if not assertions:
                return {}

            weights = {
                "domain_boost": 1.0,
                "confidence_boost": 1.0,
                "temporal_boost": 1.0,
                "persona_assertions": assertions,
            }
            authorized_revisions = {
                str(item.get("assertion_id")): str(item.get("current_revision_id"))
                for item in assertions
                if item.get("assertion_id") and item.get("current_revision_id")
            }
            if len(authorized_revisions) != len(assertions):
                raise ValueError("context search assertion lacks immutable revision")
            # Only assertions that actually determine a candidate's Persona
            # score are copied into the final effect receipt.
            self._profile_usage_evidence = {
                "authorized_revisions": authorized_revisions,
                "matched_assertion_ids": set(),
                "query_id": self._active_profile_query_id,
                "read_authorization_token": str(access.get("read_authorization_token") or ""),
            }
            return weights
        except (ImportError, OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
            logger.warning("画像加权系数获取失败", exc_info=True)
            return {}

    def _record_authorized_profile_usage(
        self,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
        baseline_output: Any = None,
        persona_enabled_output: Any = None,
    ) -> None:
        evidence = self._profile_usage_evidence
        if not evidence or self._database_dir is None:
            return
        query_id = str(evidence.get("query_id") or "")
        if not query_id or query_id != self._active_profile_query_id:
            raise ValueError("context search profile evidence query identity drift")
        matched_ids = sorted(evidence.get("matched_assertion_ids") or ())
        if not matched_ids:
            return
        if baseline_output == persona_enabled_output:
            raise ValueError("context search rank receipt requires a rank delta")
        rank_delta = list(evidence.get("rank_delta") or ())
        eligible_candidate_ids = {
            str(candidate_id)
            for candidate_id in evidence.get("eligible_candidate_ids") or ()
            if str(candidate_id)
        }
        changed_candidate_ids = {
            str(row.get("candidate_id") or "") for row in rank_delta if isinstance(row, dict)
        }
        delta_matches = {
            str(assertion_id)
            for row in rank_delta
            if isinstance(row, dict)
            for assertion_id in row.get("matched_assertion_ids") or ()
            if str(assertion_id)
        }
        if (
            not rank_delta
            or not changed_candidate_ids
            or not changed_candidate_ids <= eligible_candidate_ids
            or delta_matches != set(matched_ids)
        ):
            raise ValueError("context search rank delta evidence is incomplete")
        from core.persona.psyche import ProfileUsageLog, SignalStore
        from core.persona.profile_effect import compare_profile_effect

        authorized_revisions = dict(evidence.get("authorized_revisions") or {})
        matched_revisions = {
            assertion_id: authorized_revisions[assertion_id] for assertion_id in matched_ids
        }
        store = SignalStore(db_path=self._database_dir / "user_signals.db")
        try:
            store.record_profile_usage(
                ProfileUsageLog(
                    consumer="context_search",
                    profile_fields_used=matched_ids,
                    read_purpose="context_search_profile",
                    read_authorization_token=str(evidence.get("read_authorization_token") or ""),
                    target_receipt=compare_profile_effect(
                        owner="context_search",
                        target_type="ranking",
                        target_id="context_search_persona_candidates",
                        matched_assertion_revisions=matched_revisions,
                        baseline_output=baseline_output,
                        persona_enabled_output=persona_enabled_output,
                        expected_delta={
                            "kind": "rank_score_delta",
                            "target": "context_search_persona_candidates",
                            "query_id": query_id,
                            "baseline_ranking": list(baseline_output or ()),
                            "persona_enabled_ranking": list(persona_enabled_output or ()),
                            "changed_candidates": rank_delta,
                            "eligible_candidate_ids": sorted(eligible_candidate_ids),
                            "matched_assertion_revisions": matched_revisions,
                        },
                    ),
                    outcome="search_weight_adjusted",
                ),
                principal=principal,
                narrowing=narrowing,
            )
        finally:
            store.close()
