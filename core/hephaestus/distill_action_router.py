"""Route validated distill_output_v4 actions into auditable Wiki changes."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from core.app.dispute_resolver import DisputeAssertion, DisputeResolver
from core.cognitive.trust_scorer import (
    KnowledgeTrustOptions,
    KnowledgeTrustScorer,
)
from core.cognitive.cognition_episode_persistence import (
    validate_cognition_episode_route_binding,
)
from core.frontmatter import fm_get, parse_frontmatter, to_chinese_frontmatter, write_frontmatter
from core.hephaestus.distillation_contract import (
    COGNITIVE_ACTIONS,
    CONFLICT_RELATIONS,
    RECOMMENDED_ACTIONS,
    validate_admitted_extraction_root,
    validate_distill_output_contract,
    canonical_fragment_payload,
)
from core.hephaestus.distill_action_store import (
    ARTIFACT_SCHEMA_VERSION,
    DistillActionStore,
    sha256_json,
    stable_id,
)
from core.hephaestus.distill_persistence_projection import (
    build_action_sink_projection,
)
from core.hephaestus.distill_input_spec import DistillInputSpec
from core.evidence.source_authority import (
    CognitiveAuthorityDecision,
    claim_cognitive_authority,
)
from core.hephaestus.chunk_aggregate import validate_session_chunk_aggregate
from core.hephaestus.distillation_failure import publish_wiki_page_updated
from core.hephaestus.distill_action_models import (
    DistillActionRouteResult,
    MergeDecisionCard,
    action_receipt_from_row,
    is_ordered_identity_subsequence,
)
from core.hephaestus.distillation_models import (
    DistillationResult,
    FragmentRouteCapability,
    KnowledgeFragment,
)
from core.hephaestus.trusted_push_bridge import submit_distill_action_candidate
from core.trust.markdown_adapter import read_markdown_text
from core.trust.models import sha256_text
from core.trust.vault_mutation_service import commit_trusted_markdown
from core.utils import atomic_write_text
from core.wiki_metrics import WikiMetrics
from core.privacy.content_redaction import REDACTION_POLICY

logger = logging.getLogger(__name__)
COGNITIVE_ACTION_TARGETS = {
    "create_observation": "observation_queue",
    "create_reflection_seed": "reflection_seed",
    "propose_policy_patch": "policy_patch_candidate",
    "propose_methodology": "methodology_candidate",
    "propose_pitfall_pattern": "pitfall_pattern",
    "update_relation": "relation_update",
    "record_reinforcement": "reinforcement_signal",
}
LOW_AUTHORITY_ALWAYS_PENDING_ACTIONS = frozenset({"record_reinforcement"})
LOW_AUTHORITY_COGNITIVE_PAGE_ACTIONS = frozenset(
    {"update_page", "merge_into_page"}
)
CreatePages = Callable[
    [Sequence[KnowledgeFragment]],
    tuple[list[str], list[tuple[Path, KnowledgeFragment]]],
]


@dataclass(frozen=True)
class DistillActionRouterOptions:
    """Runtime options for the distill action router."""

    database_dir: Path
    wiki_dir: Path
    cognitive_state_database_dir: Path | None = None
    db_path: Path | None = None
    backup_dir: str = "distill_action_backups"
    shadow_dir: str = "07-Shadow/distill-actions"
    min_merge_confidence: float = 0.72
    max_direct_conflict_strength: float = 0.35

    @classmethod
    def from_config(cls, cfg: Any, wiki_base: Path | None = None) -> "DistillActionRouterOptions":
        configured_wiki_dir = getattr(cfg, "wiki_dir", None)
        wiki_dir = Path(wiki_base or configured_wiki_dir or ".").expanduser()
        configured_db = cfg.get("distill.action_router.db_path") if hasattr(cfg, "get") else None
        cfg_wiki_dir = (
            Path(configured_wiki_dir).expanduser() if configured_wiki_dir else None
        )
        if (
            wiki_base is not None
            and cfg_wiki_dir is not None
            and wiki_dir != cfg_wiki_dir
        ):
            database_dir = wiki_dir / ".mnemos"
        else:
            database_dir = Path(getattr(cfg, "database_dir", "") or (wiki_dir / ".mnemos"))
        database_dir = database_dir.expanduser()
        cognitive_state_database_dir = Path(
            getattr(cfg, "database_dir", "") or database_dir
        ).expanduser()
        return cls(
            database_dir=database_dir,
            wiki_dir=wiki_dir,
            cognitive_state_database_dir=cognitive_state_database_dir,
            db_path=Path(configured_db).expanduser() if configured_db else None,
            backup_dir=str(
                _cfg_get(cfg, "distill.action_router.backup_dir", "distill_action_backups")
            ),
            shadow_dir=str(
                _cfg_get(cfg, "distill.action_router.shadow_dir", "07-Shadow/distill-actions")
            ),
            min_merge_confidence=float(
                _cfg_get(cfg, "distill.action_router.min_merge_confidence", 0.72) or 0.72
            ),
            max_direct_conflict_strength=float(
                _cfg_get(cfg, "distill.action_router.max_direct_conflict_strength", 0.35) or 0.35
            ),
        )


class DistillActionRouter:
    """Route structured distillation claims to auditable Wiki and cognitive outcomes."""

    def __init__(
        self,
        options: DistillActionRouterOptions,
        dispute_resolver: DisputeResolver | None = None,
        trust_scorer: KnowledgeTrustScorer | None = None,
        ensure_db: bool = True,
    ):
        self.options = options
        self.wiki_dir = options.wiki_dir
        self.database_dir = options.database_dir
        self.cognitive_state_database_dir = (
            options.cognitive_state_database_dir or options.database_dir
        )
        self.db_path = options.db_path or (self.database_dir / "distill_actions.db")
        self.backup_root = self.database_dir / options.backup_dir
        self.shadow_dir = self.wiki_dir / options.shadow_dir
        self._store = DistillActionStore(self.db_path, ensure_db=ensure_db)
        self._dispute_resolver = dispute_resolver
        self._trust_scorer = trust_scorer or KnowledgeTrustScorer(
            options=KnowledgeTrustOptions.from_config(database_dir=self.database_dir),
            ensure_db=ensure_db,
        )

    def route(
        self,
        result: DistillationResult,
        fragments: Sequence[KnowledgeFragment],
        create_pages: CreatePages,
    ) -> DistillActionRouteResult:
        """Execute all validated actions from ``result.structured_output``."""
        routed = DistillActionRouteResult()
        if not isinstance(result.input_spec, DistillInputSpec):
            routed.errors.append("live action routing requires an immutable DistillInputSpec")
            return routed
        aggregate_errors = validate_session_chunk_aggregate(result)
        if aggregate_errors:
            routed.errors.extend(
                f"session chunk aggregate rejected: {error}" for error in aggregate_errors
            )
            return routed
        validation = validate_distill_output_contract(
            result.structured_output,
            input_spec=result.input_spec,
        )
        if not validation.valid:
            routed.errors.extend(issue.message for issue in validation.errors)
            return routed

        root_validation = validate_admitted_extraction_root(
            input_spec=result.input_spec,
            structured_output=result.structured_output,
            extraction_contract_valid=result.extraction_contract_valid,
            extraction_output=result.extraction_output,
            extraction_output_hash=result.extraction_output_hash,
            extraction_judgment=result.extraction_judgment,
        )
        if not root_validation.valid:
            routed.errors.extend(
                f"admitted extraction root rejected: {issue.path}: {issue.message}"
                for issue in root_validation.issues
            )
            return routed

        episode_error = validate_cognition_episode_route_binding(
            result,
            self.cognitive_state_database_dir,
        )
        if episode_error:
            routed.errors.append(episode_error)
            return routed

        try:
            projection = build_action_sink_projection(result, fragments)
        except (TypeError, ValueError) as exc:
            routed.errors.append(f"chunked page provenance rejected before write: {exc}")
            return routed
        payload = projection.payload
        claims = projection.claims
        claim_page_refs = projection.claim_page_refs
        claim_fragments = {
            id(claim): tuple(
                fragment
                for fragment in fragments
                if str(claim.get("claim_id") or "")
                in tuple(getattr(fragment, "claim_ids", ()) or ())
            )
            for claim in claims
        }
        missing_mapping = [
            str(claim.get("claim_id") or "")
            for claim in claims
            if not claim_fragments[id(claim)]
        ]
        if missing_mapping:
            routed.errors.append(
                "accepted claims lost their exact fragment mapping: "
                + ", ".join(missing_mapping)
            )
            return routed

        existing_actions = {
            id(claim): self.get_action(
                _action_id(
                    result.session_id,
                    str(claim.get("claim_id") or claim.get("recommended_action") or ""),
                    str(claim.get("recommended_action") or ""),
                )
            )
            for claim in claims
        }
        create_claims = [
            claim
            for claim in claims
            if claim.get("recommended_action") == "create_page"
            and existing_actions[id(claim)] is None
        ]
        created_paths: list[str] = []
        created_fragments: list[tuple[Path, KnowledgeFragment]] = []
        claim_created_paths: dict[int, list[str]] = {id(claim): [] for claim in claims}
        if create_claims:
            capability = result.fragment_route_capability
            if not isinstance(capability, FragmentRouteCapability):
                routed.errors.append(
                    "create_page fragments require a post-admission route capability"
                )
                return routed
            if capability.extraction_output_hash != result.extraction_output_hash:
                routed.errors.append(
                    "create_page fragment route capability is not bound to the admitted root"
                )
                return routed
            if capability.input_spec_hash != result.input_spec.input_spec_hash:
                routed.errors.append(
                    "create_page fragment route capability is not bound to the immutable input"
                )
                return routed
            if not is_ordered_identity_subsequence(fragments, capability.fragments):
                routed.errors.append(
                    "create_page fragments must be an ordered, duplicate-free "
                    "identity-subsequence of the post-admission route capability"
                )
                return routed
            create_claim_ids = {
                str(claim.get("claim_id") or "") for claim in create_claims
            }
            selected_fragments = [
                fragment
                for fragment in fragments
                if create_claim_ids.intersection(
                    str(value) for value in (getattr(fragment, "claim_ids", ()) or ())
                )
            ]
            created_paths, created_fragments = create_pages(selected_fragments)
            routed.written.extend(created_paths)
            routed.file_fragments.extend(created_fragments)
            for path, fragment in created_fragments:
                for claim in create_claims:
                    if str(claim.get("claim_id") or "") in tuple(
                        getattr(fragment, "claim_ids", ()) or ()
                    ):
                        claim_created_paths[id(claim)].append(str(path))

        for claim in claims:
            action = str(claim.get("recommended_action") or "")
            if action not in RECOMMENDED_ACTIONS:
                continue
            authority_decision = claim_cognitive_authority(
                claim,
                result.input_spec.source_authority_catalog,
            )
            try:
                existing = existing_actions[id(claim)]
                if existing is not None:
                    action_id = str(existing["action_id"])
                elif action == "create_page":
                    action_id = self._log_create_action(
                        result,
                        payload,
                        claim,
                        claim_created_paths[id(claim)],
                    )
                elif not authority_decision.authorized and (
                    action in LOW_AUTHORITY_ALWAYS_PENDING_ACTIONS
                    or (
                        action in LOW_AUTHORITY_COGNITIVE_PAGE_ACTIONS
                        and _cognitive_actions(claim)
                    )
                ):
                    action_id = self._route_authority_pending(
                        result,
                        payload,
                        claim,
                        action,
                        authority_decision,
                    )
                elif action == "update_page":
                    action_id = self._route_update_or_merge(result, payload, claim, "update_page")
                elif action == "merge_into_page":
                    action_id = self._route_update_or_merge(
                        result, payload, claim, "merge_into_page"
                    )
                elif action == "route_to_dispute":
                    action_id = self._route_to_dispute(result, payload, claim)
                elif action == "record_reinforcement":
                    action_id = self._record_reinforcement(result, payload, claim)
                else:
                    action_id = self._log_skip(result, payload, claim)
                self._log_cognitive_actions(
                    result,
                    payload,
                    claim,
                    action_id,
                    action,
                    claim_fragments[id(claim)],
                )
                routed.action_ids.append(action_id)
                self._append_action_receipt(routed, action_id)
                if action != "create_page":
                    self._append_routed_written_path(
                        routed,
                        action_id,
                        claim_page_refs.get(id(claim), ()),
                    )
            except (OSError, ValueError, TypeError, sqlite3.Error, RuntimeError) as exc:
                routed.errors.append(str(exc))
                error_action_id = self._log_action(
                    result=result,
                    payload=payload,
                    claim=claim,
                    action=action,
                    target_page=_first_target_page(claim),
                    target_kind="error",
                    status="error",
                    error=str(exc),
                )
                self._log_cognitive_actions(
                    result,
                    payload,
                    claim,
                    error_action_id,
                    action,
                    claim_fragments[id(claim)],
                )
        return routed

    def _route_authority_pending(
        self,
        result: DistillationResult,
        payload: Mapping[str, Any],
        claim: Mapping[str, Any],
        action: str,
        decision: CognitiveAuthorityDecision,
    ) -> str:
        """Preserve low-authority knowledge without mutating active state."""
        target_page = _first_target_page(claim)
        card = MergeDecisionCard(
            claim_id=str(claim.get("claim_id") or ""),
            action=action,
            target_page=target_page,
            confidence=_claim_confidence(claim),
            relation_type=_relation_type(claim),
            match_signals=[f"source_authority:{value}" for value in decision.authorities],
            conflicting_signals=["low_authority_cannot_mutate_active_cognition"],
            rollback_path="",
            safe_to_apply=False,
            decision_reason="source_authority_pending_hypothesis",
        )
        shadow_path = self._write_shadow(result, payload, claim, action, card)
        return self._log_action(
            result=result,
            payload=payload,
            claim=claim,
            action=action,
            target_page=str(shadow_path),
            target_kind="authority_pending_hypothesis",
            status="proposed",
            result_detail={
                "authority_status": "pending_hypothesis",
                "authority_reason": decision.reason,
                "source_authority_ids": list(decision.source_authority_ids),
                "source_authorities": list(decision.authorities),
            },
            merge_decision_card=card,
        )

    def _append_action_receipt(
        self,
        routed: DistillActionRouteResult,
        action_id: str,
    ) -> None:
        """Expose the durable action-log outcome to the pipeline receipt."""
        row = self.get_action(action_id)
        if not row:
            routed.errors.append(f"action receipt missing: {action_id}")
            return
        routed.action_receipts.append(action_receipt_from_row(row))

    def _append_routed_written_path(
        self,
        routed: DistillActionRouteResult,
        action_id: str,
        raw_event_refs: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        row = self.get_action(action_id)
        if not row:
            return
        if row.get("result_status") != "applied":
            return
        target = str(row.get("target_page") or "")
        if not target or ";" in target:
            return
        path = self.wiki_dir / target
        routed.written.append(str(path))
        if raw_event_refs:
            routed.page_raw_event_refs.append(
                (path, tuple(dict(ref) for ref in raw_event_refs))
            )

    def get_action(self, action_id: str) -> dict[str, Any] | None:
        """Return one action log row for tests/CLI consumers."""
        return self._store.get_action(action_id)

    def list_actions_for_session(self, session_id: str) -> list[dict[str, Any]]:
        """Return action rows for a session in write order."""
        return self._store.list_actions_for_session(session_id)

    def list_recent_actions(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent action rows for operator inspection."""
        return self._store.list_recent_actions(limit)

    def list_knowledge_actions(self, action_id: str) -> list[dict[str, Any]]:
        """Return knowledge change rows for one distill action."""
        return self._store.list_knowledge_actions(action_id)

    def list_cognitive_actions(self, action_id: str) -> list[dict[str, Any]]:
        """Return cognitive downstream action rows for one distill action."""
        return self._store.list_cognitive_actions(action_id)

    def list_cognitive_action_intents(self, action_id: str) -> list[dict[str, Any]]:
        """Return the complete child denominator, including blocked parents."""
        return self._store.list_cognitive_intents(action_id)

    def cognitive_action_counts(self) -> dict[str, int]:
        """Return action counts for health/drift style read-only reports."""
        return self._store.cognitive_action_counts()

    def _route_update_or_merge(
        self,
        result: DistillationResult,
        payload: Mapping[str, Any],
        claim: Mapping[str, Any],
        action: str,
    ) -> str:
        target_page = _first_target_page(claim)
        confidence = _claim_confidence(claim)
        relation_type = _relation_type(claim)
        target_path = self._resolve_wiki_path(target_page) if target_page else None
        backup_path = self._backup_page(target_path) if target_path and target_path.exists() else ""
        card = self._build_merge_card(claim, action, target_page, backup_path)
        trust_decision = self._trust_scorer.decide(
            source="distill_action_router",
            subject=target_page or str(claim.get("claim_id") or result.session_id),
            action=action,
            evidence_refs=_evidence_refs(claim),
            task_fit_score=confidence,
            interruption_cost=0.05,
            active_risk=False,
            scope_type="wiki_page",
            scope_value=target_page,
            metadata={
                "claim_id": str(claim.get("claim_id") or ""),
                "relation_type": relation_type,
                "conflict_strength": _conflict_strength(claim),
            },
        )
        trust_allows_direct = trust_decision.decision == "apply"
        if not trust_allows_direct:
            card = MergeDecisionCard(
                claim_id=card.claim_id,
                action=card.action,
                target_page=card.target_page,
                confidence=card.confidence,
                relation_type=card.relation_type,
                match_signals=card.match_signals,
                conflicting_signals=[
                    *card.conflicting_signals,
                    f"trust_gate:{trust_decision.reason}",
                ],
                rollback_path=card.rollback_path,
                safe_to_apply=False,
                decision_reason="route_to_shadow",
            )

        if (
            not target_path
            or not target_path.exists()
            or not card.safe_to_apply
            or not trust_allows_direct
        ):
            shadow_path = self._write_shadow(result, payload, claim, action, card)
            action_id = self._log_action(
                result=result,
                payload=payload,
                claim=claim,
                action=action,
                target_page=str(shadow_path.relative_to(self.wiki_dir)),
                target_kind="shadow",
                status="proposed",
                backup_path=backup_path,
                merge_decision_card=card,
                result_detail={
                    "reason": "routed_to_shadow",
                    "target_missing": not bool(target_path and target_path.exists()),
                    "confidence": confidence,
                    "relation_type": relation_type,
                    "trust_decision_id": trust_decision.decision_id,
                    "trust_decision": trust_decision.to_dict(),
                },
            )
            self._log_knowledge_action(
                action_id,
                change_type="shadow_write",
                target_page=str(shadow_path.relative_to(self.wiki_dir)),
                backup_path="",
                event_type="wiki_page_updated",
                detail=card.to_dict(),
            )
            publish_wiki_page_updated(shadow_path, update_type="create")
            return action_id

        existing_text = read_markdown_text(target_path)
        proposed_text = self._claim_update_text(existing_text, claim, action, payload)
        trusted_push = submit_distill_action_candidate(
            wiki_base=self.wiki_dir,
            source_agent=str(payload.get("source_agent") or result.source or ""),
            source_session_id=result.session_id,
            target_path=target_path,
            page_content=proposed_text,
            action=action,
            claim=dict(claim),
            payload=dict(payload),
            expected_existing_hash=sha256_text(existing_text),
            evidence_refs=_evidence_refs(claim),
            confidence_score=confidence,
        )
        if trusted_push.intercepted:
            action_id = self._log_action(
                result=result,
                payload=payload,
                claim=claim,
                action=action,
                target_page=str(target_path.relative_to(self.wiki_dir)),
                target_kind="trusted_proposal",
                status="proposed",
                backup_path=backup_path,
                merge_decision_card=MergeDecisionCard(
                    claim_id=card.claim_id,
                    action=card.action,
                    target_page=card.target_page,
                    confidence=card.confidence,
                    relation_type=card.relation_type,
                    match_signals=card.match_signals,
                    conflicting_signals=card.conflicting_signals,
                    rollback_path=card.rollback_path,
                    safe_to_apply=False,
                    decision_reason="route_to_trusted_proposal",
                ),
                result_detail={
                    "confidence": confidence,
                    "relation_type": relation_type,
                    "trust_decision_id": trust_decision.decision_id,
                    "trust_decision": trust_decision.to_dict(),
                    "trusted_push": trusted_push.to_dict(),
                },
            )
            self._log_knowledge_action(
                action_id,
                change_type="trusted_push_proposal",
                target_page=str(target_path.relative_to(self.wiki_dir)),
                backup_path=backup_path,
                event_type="proposal_submitted",
                detail={"proposal_id": trusted_push.proposal_id, "action": action},
            )
            return action_id

        commit_trusted_markdown(
            trusted_push,
            target_path=target_path,
            content=proposed_text,
        )
        updated = target_path
        publish_wiki_page_updated(updated, update_type="update")
        rel_target = str(updated.relative_to(self.wiki_dir))
        action_id = self._log_action(
            result=result,
            payload=payload,
            claim=claim,
            action=action,
            target_page=rel_target,
            target_kind="wiki_page",
            status="applied",
            backup_path=backup_path,
            merge_decision_card=card,
            result_detail={
                "confidence": confidence,
                "relation_type": relation_type,
                "trust_decision_id": trust_decision.decision_id,
                "trust_decision": trust_decision.to_dict(),
                "trusted_push": trusted_push.to_dict(),
            },
        )
        self._log_knowledge_action(
            action_id,
            change_type="page_append",
            target_page=rel_target,
            backup_path=backup_path,
            event_type="wiki_page_updated",
            detail=card.to_dict(),
        )
        return action_id

    def _route_to_dispute(
        self,
        result: DistillationResult,
        payload: Mapping[str, Any],
        claim: Mapping[str, Any],
    ) -> str:
        resolver = self._dispute_resolver or DisputeResolver(wiki_base=str(self.wiki_dir))
        source_page = _first_target_page(claim)
        new_assertion = DisputeAssertion(
            page_path=f"distill:{result.session_id}",
            title=str(
                claim.get("claim_text") or payload.get("candidate_summary") or "distill dispute"
            )[:80],
            content=str(claim.get("claim_text") or ""),
            reference_count=len(_evidence_refs(claim)),
            relation_context=str(_relation_reason(claim)),
            relation_evidence=_evidence_refs(claim),
            source_method="distill_action_router",
            confidence=_claim_confidence(claim),
            strength=_conflict_strength(claim),
        )
        existing = [
            DisputeAssertion(
                page_path=source_page or "",
                title=Path(source_page or "existing").stem,
                content=str(_relation_delta(claim)),
                reference_count=0,
                relation_context=str(_relation_reason(claim)),
                relation_evidence=[],
                source_method="distill_action_router",
                confidence=0.0,
                strength=_conflict_strength(claim),
            )
        ]
        dispute = resolver.create_dispute_page(
            new_assertion=new_assertion,
            conflicts=existing,
            conflict_strength=max(_conflict_strength(claim), _claim_confidence(claim)),
            is_core_knowledge=False,
            pair_key=self._claim_pair_key(result, claim),
        )
        action_id = self._log_action(
            result=result,
            payload=payload,
            claim=claim,
            action="route_to_dispute",
            target_page=dispute.page_path,
            target_kind="dispute",
            status="applied",
            result_detail={"conflict_strength": dispute.conflict_strength},
        )
        self._log_knowledge_action(
            action_id,
            change_type="dispute_write",
            target_page=dispute.page_path,
            backup_path="",
            event_type="wiki_page_updated",
            detail={"pair_key": self._claim_pair_key(result, claim)},
        )
        publish_wiki_page_updated(self.wiki_dir / dispute.page_path, update_type="create")
        return action_id

    def _record_reinforcement(
        self,
        result: DistillationResult,
        payload: Mapping[str, Any],
        claim: Mapping[str, Any],
    ) -> str:
        target_page = _first_target_page(claim)
        target_path = self._resolve_wiki_path(target_page) if target_page else None
        if not target_path or not target_path.exists():
            return self._log_action(
                result=result,
                payload=payload,
                claim=claim,
                action="record_reinforcement",
                target_page=target_page,
                target_kind="missing_target",
                status="error",
                error="reinforcement target page is missing",
            )

        backup_path = self._backup_page(target_path)
        rel_target = str(target_path.relative_to(self.wiki_dir))
        existing_text = read_markdown_text(target_path)
        proposed_text = self._reinforcement_text(existing_text, payload, claim)
        trusted_push = submit_distill_action_candidate(
            wiki_base=self.wiki_dir,
            source_agent=str(payload.get("source_agent") or result.source or ""),
            source_session_id=result.session_id,
            target_path=target_path,
            page_content=proposed_text,
            action="record_reinforcement",
            claim=dict(claim),
            payload=dict(payload),
            expected_existing_hash=sha256_text(existing_text),
            evidence_refs=_evidence_refs(claim),
            confidence_score=_claim_confidence(claim),
        )
        if trusted_push.intercepted:
            action_id = self._log_action(
                result=result,
                payload=payload,
                claim=claim,
                action="record_reinforcement",
                target_page=rel_target,
                target_kind="trusted_proposal",
                status="proposed",
                backup_path=backup_path,
                result_detail={"reinforced": False, "trusted_push": trusted_push.to_dict()},
            )
            self._log_knowledge_action(
                action_id,
                change_type="trusted_push_proposal",
                target_page=rel_target,
                backup_path=backup_path,
                event_type="proposal_submitted",
                detail={
                    "proposal_id": trusted_push.proposal_id,
                    "action": "record_reinforcement",
                },
            )
            return action_id

        commit_trusted_markdown(
            trusted_push,
            target_path=target_path,
            content=proposed_text,
        )
        self._update_metrics_reinforcement(rel_target, claim)
        publish_wiki_page_updated(target_path, update_type="update")
        action_id = self._log_action(
            result=result,
            payload=payload,
            claim=claim,
            action="record_reinforcement",
            target_page=rel_target,
            target_kind="wiki_page",
            status="applied",
            backup_path=backup_path,
            result_detail={"reinforced": True},
        )
        self._log_knowledge_action(
            action_id,
            change_type="frontmatter_reinforcement",
            target_page=rel_target,
            backup_path=backup_path,
            event_type="wiki_page_updated",
            detail={"source_event_ids": _source_event_ids(payload)},
        )
        return action_id

    def _log_create_action(
        self,
        result: DistillationResult,
        payload: Mapping[str, Any],
        claim: Mapping[str, Any],
        created_paths: Sequence[str],
    ) -> str:
        target_pages = [
            (
                str(Path(path).relative_to(self.wiki_dir))
                if _is_relative_to(Path(path), self.wiki_dir)
                else str(path)
            )
            for path in created_paths
        ]
        pending_proposals = [
            dict(layer.detail)
            for layer in result.layer_results
            if layer.name == "trusted_push"
            and layer.detail.get("intercepted") is True
            and layer.detail.get("proposal_id")
        ]
        pending_targets = [
            str(item.get("target_path") or "")
            for item in pending_proposals
            if item.get("target_path")
        ]
        is_proposed = not target_pages and bool(pending_proposals)
        trust_decision = self._trust_scorer.decide(
            source="distill_action_router",
            subject=";".join(target_pages) or str(claim.get("claim_id") or result.session_id),
            action="extract",
            evidence_refs=_evidence_refs(claim),
            task_fit_score=_claim_confidence(claim),
            interruption_cost=0.0,
            active_risk=False,
            scope_type="distill_claim",
            scope_value=str(claim.get("claim_id") or ""),
            metadata={
                "claim_id": str(claim.get("claim_id") or ""),
                "target_pages": target_pages,
                "distill_intent": str(payload.get("distill_intent") or ""),
            },
        )
        action_id = self._log_action(
            result=result,
            payload=payload,
            claim=claim,
            action="create_page",
            target_page=";".join(target_pages or pending_targets),
            target_kind="trusted_proposal" if is_proposed else "wiki_page",
            status="applied" if target_pages else ("proposed" if is_proposed else "error"),
            result_detail={
                "created_pages": target_pages,
                "trusted_push": pending_proposals[0] if pending_proposals else {},
                "trusted_push_proposals": pending_proposals,
                "trust_decision_id": trust_decision.decision_id,
                "trust_decision": trust_decision.to_dict(),
            },
            error=(
                ""
                if target_pages or is_proposed
                else "create_page produced no page"
            ),
        )
        for target_page in target_pages:
            self._log_knowledge_action(
                action_id,
                change_type="page_create",
                target_page=target_page,
                backup_path="",
                event_type="wiki_page_updated",
                detail={"source_event_ids": _source_event_ids(payload)},
            )
        return action_id

    def _log_skip(
        self,
        result: DistillationResult,
        payload: Mapping[str, Any],
        claim: Mapping[str, Any],
    ) -> str:
        return self._log_action(
            result=result,
            payload=payload,
            claim=claim,
            action="skip",
            target_page="",
            target_kind="none",
            status="skipped",
        )

    def _build_merge_card(
        self,
        claim: Mapping[str, Any],
        action: str,
        target_page: str,
        backup_path: str,
    ) -> MergeDecisionCard:
        confidence = _claim_confidence(claim)
        relation_type = _relation_type(claim)
        conflict_strength = _conflict_strength(claim)
        match_signals = [
            f"relation:{relation_type or 'unknown'}",
            f"confidence:{confidence:.2f}",
        ]
        if _relation_reason(claim):
            match_signals.append(f"reason:{_relation_reason(claim)}")
        conflicting_signals: list[str] = []
        if relation_type in CONFLICT_RELATIONS:
            conflicting_signals.append(f"conflict_relation:{relation_type}")
        if confidence < self.options.min_merge_confidence:
            conflicting_signals.append(f"low_confidence:{confidence:.2f}")
        if conflict_strength > self.options.max_direct_conflict_strength:
            conflicting_signals.append(f"conflict_strength:{conflict_strength:.2f}")
        safe = not conflicting_signals and bool(target_page and backup_path)
        reason = "apply_direct" if safe else "route_to_shadow"
        return MergeDecisionCard(
            claim_id=str(claim.get("claim_id") or ""),
            action=action,
            target_page=target_page,
            confidence=confidence,
            relation_type=relation_type,
            match_signals=match_signals,
            conflicting_signals=conflicting_signals,
            rollback_path=backup_path,
            safe_to_apply=safe,
            decision_reason=reason,
        )

    def _claim_update_text(
        self,
        text: str,
        claim: Mapping[str, Any],
        action: str,
        payload: Mapping[str, Any],
    ) -> str:
        stamp = _now_utc()
        section = [
            "",
            "",
            "<!-- mnemos-distill-action -->",
            f"## Mnemos {action} ({stamp})",
            "",
            f"- Claim: {claim.get('claim_text', '')}",
            f"- Source events: {', '.join(_source_event_ids(payload)) or 'unknown'}",
            f"- Evidence: {'; '.join(_evidence_refs(claim)) or 'none'}",
        ]
        delta = _relation_delta(claim)
        if delta:
            section.append(f"- Delta: {delta}")
        return text.rstrip() + "\n".join(section) + "\n"

    def _write_shadow(
        self,
        result: DistillationResult,
        payload: Mapping[str, Any],
        claim: Mapping[str, Any],
        action: str,
        card: MergeDecisionCard,
    ) -> Path:
        self.shadow_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{_timestamp_slug()}-{_safe_slug(card.claim_id or result.session_id)}.md"
        path = self.shadow_dir / filename
        frontmatter = {
            "type": "distill_action_shadow",
            "status": "pending_review",
            "source": "distill_action_router",
            "source_session": result.session_id,
            "source_event_ids": _source_event_ids(payload),
            "distill_action": action,
            "target_page": card.target_page,
            "confidence": card.confidence,
            "evidence_refs": _evidence_refs(claim),
            "merge_decision_card": card.to_dict(),
            "created_at": _now_utc(),
        }
        body = [
            f"# {payload.get('candidate_summary') or claim.get('claim_id') or 'Distill action shadow'}",
            "",
            "## Claim",
            str(claim.get("claim_text") or ""),
            "",
            "## Decision",
            card.decision_reason,
            "",
            "## Conflicting Signals",
        ]
        body.extend(f"- {signal}" for signal in card.conflicting_signals)
        if not card.conflicting_signals:
            body.append("- none")
        body.extend(["", "## Rollback", card.rollback_path or "not_applicable", ""])
        atomic_write_text(
            path,
            write_frontmatter(to_chinese_frontmatter(frontmatter), "\n".join(body)),
            encoding="utf-8",
        )
        return path

    def _reinforcement_text(
        self,
        text: str,
        payload: Mapping[str, Any],
        claim: Mapping[str, Any],
    ) -> str:
        fm, body = parse_frontmatter(text)
        fm = dict(fm or {})
        count = _int_or_zero(fm_get(fm, "reinforcement_count", 0)) + 1
        refs = _as_list(fm_get(fm, "reinforcement_source_event_ids", []))
        refs.extend(_source_event_ids(payload))
        evidence = _as_list(fm_get(fm, "evidence_refs", []))
        evidence.extend(_evidence_refs(claim))
        fm.update(
            {
                "reinforcement_count": count,
                "reinforced_at": _now_utc(),
                "reinforcement_source_event_ids": _dedup(refs),
                "evidence_refs": _dedup(evidence),
            }
        )
        return write_frontmatter(fm, body)

    def _update_metrics_reinforcement(self, rel_target: str, claim: Mapping[str, Any]) -> None:
        try:
            metrics = WikiMetrics(wiki_dir=str(self.wiki_dir))
            page = metrics.get_page(rel_target)
            source_count = 1
            if page is not None:
                source_count = max(0, int(page.source_count or 0)) + 1
            metrics.upsert_page(
                rel_target,
                source_count=source_count,
                evidence_level=max(1, len(_evidence_refs(claim))),
            )
        except (OSError, ValueError, TypeError, sqlite3.Error, RuntimeError):
            logger.debug("reinforcement metrics update failed for %s", rel_target, exc_info=True)

    def _backup_page(self, target_path: Path | None) -> str:
        if target_path is None or not target_path.exists():
            return ""
        if _is_relative_to(target_path, self.wiki_dir):
            rel = target_path.relative_to(self.wiki_dir)
        else:
            rel = Path(target_path.name)
        backup_path = self.backup_root / _timestamp_slug() / Path(rel)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target_path, backup_path)
        return str(backup_path)

    def _resolve_wiki_path(self, page_path: str) -> Path:
        candidate = Path(page_path)
        if candidate.is_absolute():
            return candidate
        path = self.wiki_dir / candidate
        if path.exists():
            return path
        if path.suffix != ".md":
            md_path = self.wiki_dir / f"{page_path}.md"
            if md_path.exists():
                return md_path
        return path

    def _claim_pair_key(self, result: DistillationResult, claim: Mapping[str, Any]) -> str:
        raw = json.dumps(
            {
                "session_id": result.session_id,
                "claim_id": claim.get("claim_id"),
                "target": _first_target_page(claim),
                "relation": _relation_type(claim),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _log_action(
        self,
        *,
        result: DistillationResult,
        payload: Mapping[str, Any],
        claim: Mapping[str, Any],
        action: str,
        target_page: str,
        target_kind: str,
        status: str,
        backup_path: str = "",
        result_detail: Mapping[str, Any] | None = None,
        error: str = "",
        merge_decision_card: MergeDecisionCard | None = None,
    ) -> str:
        action_id = _action_id(result.session_id, str(claim.get("claim_id") or action), action)
        self._store.insert_parent_action(
            {
                "action_id": action_id,
                "created_at": _now_utc(),
                "session_id": result.session_id,
                "source_agent": str(payload.get("source_agent") or result.source or ""),
                "action": action,
                "distill_intent": str(payload.get("distill_intent") or ""),
                "claim_id": str(claim.get("claim_id") or ""),
                "target_page": target_page,
                "target_kind": target_kind,
                "source_event_ids": _json_dumps(_source_event_ids(payload)),
                "evidence_refs": _json_dumps(_evidence_refs(claim)),
                "backup_path": backup_path,
                "result_status": status,
                "result_detail": _json_dumps(dict(result_detail or {})),
                "error": error,
                "merge_decision_card": _json_dumps(
                    merge_decision_card.to_dict() if merge_decision_card else {}
                ),
            }
        )
        return action_id

    def _log_knowledge_action(
        self,
        action_id: str,
        *,
        change_type: str,
        target_page: str,
        backup_path: str,
        event_type: str,
        detail: Mapping[str, Any],
    ) -> None:
        self._store.insert_knowledge_action(
            action_id,
            change_type=change_type,
            target_page=target_page,
            backup_path=backup_path,
            event_type=event_type,
            detail=detail,
        )

    def _log_cognitive_actions(
        self,
        result: DistillationResult,
        payload: Mapping[str, Any],
        claim: Mapping[str, Any],
        distill_action_id: str,
        recommended_action: str,
        fragments: Sequence[KnowledgeFragment],
    ) -> None:
        actions = _cognitive_actions(claim)
        if not actions:
            return
        parent = self.get_action(distill_action_id)
        if parent is None:
            raise RuntimeError(f"parent action receipt missing: {distill_action_id}")
        created_at = str(parent["created_at"])
        episode_id = str(result.cognition_episode_revision_id or "")
        if not episode_id:
            raise RuntimeError("canonical cognition episode revision is required before routing")
        fragment_refs = [
            {
                "fragment_id": _fragment_identity(fragment),
                "claim_ids": list(getattr(fragment, "claim_ids", ()) or ()),
                "title": str(fragment.title or ""),
                "content_hash": sha256_json(canonical_fragment_payload(fragment)),
            }
            for fragment in fragments
        ]
        fragment_ids = [str(ref["fragment_id"]) for ref in fragment_refs]
        parent_target_pages = [
            value for value in str(parent.get("target_page") or "").split(";") if value
        ]
        acl = {
            "visibility": "private",
            "owner": "local_user",
            "redaction_policy": REDACTION_POLICY,
            "encryption": "none",
        }
        for cognitive_action in actions:
            target_kind = COGNITIVE_ACTION_TARGETS.get(cognitive_action, "cognitive_action")
            cognitive_action_id = _cognitive_action_id(
                distill_action_id,
                str(claim.get("claim_id") or ""),
                cognitive_action,
            )
            authority_decision = claim_cognitive_authority(
                claim,
                result.input_spec.source_authority_catalog,
            )
            artifact_path, artifact = self._build_cognitive_action_artifact(
                result=result,
                payload=payload,
                claim=claim,
                distill_action_id=distill_action_id,
                cognitive_action_id=cognitive_action_id,
                cognitive_action=cognitive_action,
                target_kind=target_kind,
                recommended_action=recommended_action,
                created_at=created_at,
                episode_id=episode_id,
                fragment_refs=fragment_refs,
                parent_target_pages=parent_target_pages,
                acl=acl,
            )
            command_created = self._store.record_cognitive_intent(
                cognitive_action_id=cognitive_action_id,
                parent=parent,
                cognitive_action=cognitive_action,
                episode_id=episode_id,
                fragment_ids=fragment_ids,
                artifact=artifact,
                artifact_path=artifact_path,
                target_kind=target_kind,
                evidence_refs=_evidence_refs(claim),
                acl=acl,
                input_spec_hash=result.input_spec.input_spec_hash,
                extraction_output_hash=result.extraction_output_hash,
                allow_command=authority_decision.authorized,
                detail={
                    "recommended_action": recommended_action,
                    "claim_type": str(claim.get("claim_type") or ""),
                    "relation_type": _relation_type(claim),
                    "authority_reason": authority_decision.reason,
                    "source_authority_ids": list(
                        authority_decision.source_authority_ids
                    ),
                    "source_authorities": list(authority_decision.authorities),
                },
            )
            if command_created:
                try:
                    artifact_path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_text(
                        artifact_path,
                        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True),
                        encoding="utf-8",
                    )
                except OSError:
                    # The canonical artifact is already durable in SQLite.
                    # A missing materialized JSON projection is recoverable
                    # and must not roll the command back to a false terminal.
                    logger.warning(
                        "cognitive action artifact projection failed for %s",
                        cognitive_action_id,
                        exc_info=True,
                    )

    def _build_cognitive_action_artifact(
        self,
        *,
        result: DistillationResult,
        payload: Mapping[str, Any],
        claim: Mapping[str, Any],
        distill_action_id: str,
        cognitive_action_id: str,
        cognitive_action: str,
        target_kind: str,
        recommended_action: str,
        created_at: str,
        episode_id: str,
        fragment_refs: Sequence[Mapping[str, Any]],
        parent_target_pages: Sequence[str],
        acl: Mapping[str, Any],
    ) -> tuple[Path, dict[str, Any]]:
        date_dir = created_at[:10]
        artifact_dir = self.database_dir / "distill_cognitive_actions" / date_dir
        artifact_path = artifact_dir / f"{cognitive_action_id}.json"
        artifact = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "cognitive_action_id": cognitive_action_id,
            "distill_action_id": distill_action_id,
            "episode_id": episode_id,
            "created_at": created_at,
            "session_id": result.session_id,
            "source_agent": str(payload.get("source_agent") or result.source or ""),
            "claim_id": str(claim.get("claim_id") or ""),
            "cognitive_action": cognitive_action,
            "target_kind": target_kind,
            "recommended_action": recommended_action,
            "source_event_ids": _source_event_ids(payload),
            "evidence_refs": _evidence_refs(claim),
            "input_spec_hash": result.input_spec.input_spec_hash,
            "extraction_output_hash": result.extraction_output_hash,
            "raw_event_refs": [dict(ref) for ref in result.raw_event_refs or []],
            "fragment_ids": [str(ref.get("fragment_id") or "") for ref in fragment_refs],
            "fragment_refs": [dict(ref) for ref in fragment_refs],
            "mapping_quality": "exact",
            "parent_target_pages": list(parent_target_pages),
            "claim": dict(claim),
            "user_behavior_intent": dict(payload.get("user_behavior_intent") or {}),
            "source_authority": {
                "catalog_hash": result.input_spec.source_authority_catalog.catalog_hash,
                **claim_cognitive_authority(
                    claim,
                    result.input_spec.source_authority_catalog,
                ).__dict__,
            },
            "acl": dict(acl),
        }
        return artifact_path, artifact

    def _ensure_db(self) -> None:
        self._store.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        return self._store.connect()


def _cfg_get(cfg: Any, key: str, default: Any) -> Any:
    try:
        return cfg.get(key, default)
    except (AttributeError, TypeError):
        return default


def _source_event_ids(payload: Mapping[str, Any]) -> list[str]:
    return [str(item) for item in _as_list(payload.get("source_event_ids")) if str(item)]


def _first_target_page(claim: Mapping[str, Any]) -> str:
    relation = claim.get("relation_to_existing")
    if not isinstance(relation, Mapping):
        return ""
    targets = _as_list(relation.get("target_pages"))
    return str(targets[0]) if targets else ""


def _relation_type(claim: Mapping[str, Any]) -> str:
    relation = claim.get("relation_to_existing")
    return str(relation.get("type") or "") if isinstance(relation, Mapping) else ""


def _relation_reason(claim: Mapping[str, Any]) -> str:
    relation = claim.get("relation_to_existing")
    return str(relation.get("reason") or "") if isinstance(relation, Mapping) else ""


def _relation_delta(claim: Mapping[str, Any]) -> str:
    relation = claim.get("relation_to_existing")
    return str(relation.get("delta_text") or "") if isinstance(relation, Mapping) else ""


def _claim_confidence(claim: Mapping[str, Any]) -> float:
    value = claim.get("confidence", 0.0)
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _conflict_strength(claim: Mapping[str, Any]) -> float:
    relation = claim.get("relation_to_existing")
    if not isinstance(relation, Mapping):
        return 0.0
    raw = relation.get("conflict_strength", 0.0)
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 0.0


def _evidence_refs(claim: Mapping[str, Any]) -> list[str]:
    refs: list[str] = []
    for item in _as_list(claim.get("evidence")):
        if not isinstance(item, Mapping):
            continue
        source_id = str(item.get("source_event_id") or "")
        quote = str(item.get("quote") or "")
        if source_id and quote:
            refs.append(f"{source_id}: {quote}")
        elif source_id:
            refs.append(source_id)
    return _dedup(refs)


def _cognitive_actions(claim: Mapping[str, Any]) -> list[str]:
    actions = [
        str(item)
        for item in _as_list(claim.get("cognitive_actions"))
        if str(item) in COGNITIVE_ACTIONS
    ]
    return _dedup(actions)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _dedup(values: Iterable[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value)
    return cleaned.strip("-_")[:80] or "distill-action"


def _action_id(session_id: str, claim_id: str, action: str) -> str:
    raw = f"{session_id}|{claim_id}|{action}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"da_{digest}"


def _cognitive_action_id(distill_action_id: str, claim_id: str, cognitive_action: str) -> str:
    raw = f"{distill_action_id}|{claim_id}|{cognitive_action}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"dca_{digest}"


def _fragment_identity(fragment: KnowledgeFragment) -> str:
    return stable_id("fragment", canonical_fragment_payload(fragment))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
