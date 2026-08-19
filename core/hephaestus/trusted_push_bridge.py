"""Trusted push bridge for Hephaestus write paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from core.cognitive.state_contract import sha256_json
from core.trust import CandidateBundle, ProposalQueue
from core.trust.config import load_trusted_push_config
from core.trust.models import sha256_text
from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionRequest,
    authorize_exact_project_contract_action,
    build_exact_project_contract_evaluator,
    find_pending_material_action_authorization,
    find_material_action_recovery_authorization,
    resolve_material_action_authorization,
    resolve_material_action_recovery_authorization,
)
from core.trust.vault_mutation_service import (
    TRUSTED_MARKDOWN_ACTION_TYPE,
    TRUSTED_MARKDOWN_EXECUTOR,
    TRUSTED_MARKDOWN_OWNER,
    record_trusted_markdown_no_effect_terminal,
    trusted_markdown_target_state_hash,
    trusted_markdown_material_action_binding,
)


HEPHAESTUS_WIKI_DECISION_CONTRACT_ID = "project-contract:hephaestus-wiki-write"
HEPHAESTUS_WIKI_DECISION_CONTRACT_REVISION = "mnemos.hephaestus_wiki_write.v1"
HEPHAESTUS_WIKI_DECISION_CONTRACT_TEXT = (
    "An admitted Hephaestus artifact may create or update only its exact rendered "
    "Wiki target through the trusted-push mutation boundary."
)
HEPHAESTUS_WIKI_DECISION_PRODUCER_HASH = sha256_json(
    {
        "module": "core.hephaestus.trusted_push_bridge",
        "producer": "submit_wiki_write_candidate",
        "version": HEPHAESTUS_WIKI_DECISION_CONTRACT_REVISION,
    }
)


@dataclass(frozen=True)
class TrustedPushResult:
    action: str
    mode: str
    proposal_id: str = ""
    status: str = ""
    gate_decision: str = ""
    target_path: str = ""
    content_hash: str = ""
    expected_existing_hash: str | None = None
    source_path: str = ""
    source_content_hash: str = ""
    proposed_action: str = "update_markdown"
    material_command_id: str = ""
    material_target_ref: str = ""
    material_input_hash: str = ""
    material_effect_db_path: str = ""
    material_action: MaterialActionAuthorization | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def intercepted(self) -> bool:
        return self.action == "intercept"

    def to_dict(self) -> Dict[str, Any]:
        """Return the public trusted-push result without its live capability."""

        return {
            "action": self.action,
            "mode": self.mode,
            "proposal_id": self.proposal_id,
            "status": self.status,
            "gate_decision": self.gate_decision,
            "target_path": self.target_path,
            "content_hash": self.content_hash,
            "expected_existing_hash": self.expected_existing_hash,
            "source_path": self.source_path,
            "source_content_hash": self.source_content_hash,
            "proposed_action": self.proposed_action,
            "material_command_id": self.material_command_id,
            "material_target_ref": self.material_target_ref,
            "material_input_hash": self.material_input_hash,
            "material_effect_db_path": self.material_effect_db_path,
        }


def submit_wiki_write_candidate(
    *,
    wiki_base: Path,
    source: str,
    source_agent: str,
    source_session_id: str | None,
    target_path: Path,
    payload: Dict[str, Any],
    evidence_refs: Iterable[str],
    proposed_actions: List[str],
    confidence_score: float = 0.7,
    risk_level: str = "medium",
) -> TrustedPushResult:
    """Create a trusted push proposal when the feature guard is enabled."""

    trusted_config = load_trusted_push_config(wiki_base=wiki_base)
    content = str(payload.get("content", ""))
    expected_existing_hash = payload.get("expected_existing_hash")
    content_hash = sha256_text(content)
    expected_hash = str(expected_existing_hash) if expected_existing_hash is not None else None
    source_path = str(payload.get("source_path", ""))
    source_content_hash = str(payload.get("source_content_hash", ""))
    proposed_action = str(proposed_actions[0] if proposed_actions else "update_markdown")
    binding = trusted_markdown_material_action_binding(
        target_path=target_path,
        content=content,
        proposed_action=proposed_action,
        expected_existing_hash=expected_hash,
        source_path=source_path,
        source_content_hash=source_content_hash,
    )
    state_db_path = trusted_config.db_path.parent / "producer_consumer_ledger.db"
    expected_request = MaterialActionRequest(
        owner=TRUSTED_MARKDOWN_OWNER,
        executor_id=TRUSTED_MARKDOWN_EXECUTOR,
        action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
        expected_state_db=str(state_db_path),
    )
    try:
        material_action, permit = resolve_material_action_authorization(
            None,
            owner=expected_request.owner,
            executor_id=expected_request.executor_id,
            action_type=expected_request.action_type,
            target_ref=expected_request.target_ref,
            input_hash=expected_request.input_hash,
            expected_state_db=state_db_path,
        )
    except PermissionError as exc:
        if "canonical material-action authorization is required" not in str(exc):
            raise
        resolved_wiki = Path(wiki_base).expanduser().resolve(strict=False)
        resolved_target = Path(target_path).expanduser().resolve(strict=False)
        try:
            relative_target = resolved_target.relative_to(resolved_wiki).as_posix()
        except ValueError:
            relative_target = ""
        target_declared = str(payload.get("target_path") or "").strip()
        declared_matches = not target_declared or (
            Path(target_declared).expanduser().resolve(strict=False) == resolved_target
        )
        refs = tuple(
            dict.fromkeys(str(ref).strip() for ref in evidence_refs if str(ref).strip())
        )
        source_facts = {
            "schema_version": "mnemos.hephaestus_wiki_write_facts.v1",
            "source": str(source),
            "source_agent": str(source_agent),
            "source_session_id": str(source_session_id or ""),
            "target_path": str(resolved_target),
            "relative_target": relative_target,
            "content_hash": content_hash,
            "payload_hash": sha256_json(payload),
            "expected_existing_hash": str(expected_hash or ""),
            "proposed_action": proposed_action,
            "evidence_refs": list(refs),
        }
        decision_checks = {
            "target_is_within_wiki": bool(relative_target),
            "declared_target_matches": declared_matches,
            "rendered_content_is_bound": bool(content) and bool(content_hash),
            "source_is_bound": bool(str(source).strip()),
            "evidence_is_bound": bool(refs),
            "single_action_is_bound": bool(proposed_action),
        }
        approved_candidate_key = "apply_exact_hephaestus_wiki_mutation"
        approved_candidate_summary = (
            "Apply the exact admitted Hephaestus Wiki mutation through trusted push."
        )
        rejected_candidate_key = "reject_unbound_hephaestus_wiki_mutation"
        rejected_candidate_summary = (
            "Reject a Hephaestus Wiki mutation with an unbound source, target, or body."
        )
        facts_hash, _ = build_exact_project_contract_evaluator(
            expected_request=expected_request,
            source_facts=source_facts,
            decision_checks=decision_checks,
            approved_candidate_key=approved_candidate_key,
            approved_candidate_summary=approved_candidate_summary,
            rejected_candidate_key=rejected_candidate_key,
            rejected_candidate_summary=rejected_candidate_summary,
            approved_reason_code="hephaestus_wiki_binding_verified",
            rejected_reason_code="hephaestus_wiki_binding_rejected",
            committed_metric="hephaestus_wiki_mutation_receipt",
            rejected_metric="unbound_hephaestus_wiki_mutation_count",
        )
        facts_digest = facts_hash.split(":", 1)[1]
        pending = find_pending_material_action_authorization(
            state_db_path=state_db_path,
            owner=expected_request.owner,
            executor_id=expected_request.executor_id,
            action_type=expected_request.action_type,
            target_ref=expected_request.target_ref,
            input_hash=expected_request.input_hash,
            decision_source_revision_id=(
                f"hephaestus-wiki-write:{facts_digest}"
            ),
        )
        if pending is not None:
            return _submit_wiki_candidate_with_material_action(
                trusted_config=trusted_config,
                wiki_base=wiki_base,
                source=source,
                source_agent=source_agent,
                source_session_id=source_session_id,
                target_path=target_path,
                payload=payload,
                evidence_refs=evidence_refs,
                proposed_actions=proposed_actions,
                confidence_score=confidence_score,
                risk_level=risk_level,
                expected_hash=expected_hash,
                source_path=source_path,
                source_content_hash=source_content_hash,
                proposed_action=proposed_action,
                binding=binding,
                material_action=pending,
            )
        recovery = find_material_action_recovery_authorization(
            state_db_path=state_db_path,
            owner=expected_request.owner,
            executor_id=expected_request.executor_id,
            action_type=expected_request.action_type,
            target_ref=expected_request.target_ref,
            input_hash=expected_request.input_hash,
            decision_source_revision_id=(
                f"hephaestus-wiki-write:{facts_digest}"
            ),
        )
        if recovery is not None:
            return _submit_wiki_candidate_with_material_action(
                trusted_config=trusted_config,
                wiki_base=wiki_base,
                source=source,
                source_agent=source_agent,
                source_session_id=source_session_id,
                target_path=target_path,
                payload=payload,
                evidence_refs=evidence_refs,
                proposed_actions=proposed_actions,
                confidence_score=confidence_score,
                risk_level=risk_level,
                expected_hash=expected_hash,
                source_path=source_path,
                source_content_hash=source_content_hash,
                proposed_action=proposed_action,
                binding=binding,
                material_action=recovery,
            )
        material_action = authorize_exact_project_contract_action(
            expected_request=expected_request,
            state_db_path=state_db_path,
            contract_id=HEPHAESTUS_WIKI_DECISION_CONTRACT_ID,
            contract_revision_id=HEPHAESTUS_WIKI_DECISION_CONTRACT_REVISION,
            contract_text=HEPHAESTUS_WIKI_DECISION_CONTRACT_TEXT,
            source_namespace="hephaestus-wiki-write",
            source_facts=source_facts,
            decision_checks=decision_checks,
            evidence_refs=refs,
            task=f"Apply Hephaestus Wiki action {proposed_action}",
            goal="Persist only the exact admitted Hephaestus Wiki mutation.",
            constraints=(
                "The target must remain inside the configured Wiki vault.",
                "The rendered content, action, and observed prior hash must remain exact.",
                "Trusted push remains the sole formal Markdown mutation boundary.",
            ),
            created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            producer="hephaestus-trusted-push-bridge",
            producer_version=HEPHAESTUS_WIKI_DECISION_CONTRACT_REVISION,
            producer_code_hash=HEPHAESTUS_WIKI_DECISION_PRODUCER_HASH,
            evaluator_id="hephaestus-wiki-write-evaluator",
            approved_candidate_key=approved_candidate_key,
            approved_candidate_summary=approved_candidate_summary,
            rejected_candidate_key=rejected_candidate_key,
            rejected_candidate_summary=rejected_candidate_summary,
            approved_reason_code="hephaestus_wiki_binding_verified",
            rejected_reason_code="hephaestus_wiki_binding_rejected",
            committed_metric="hephaestus_wiki_mutation_receipt",
            rejected_metric="unbound_hephaestus_wiki_mutation_count",
        )
        material_action, permit = resolve_material_action_recovery_authorization(
            material_action,
            owner=expected_request.owner,
            executor_id=expected_request.executor_id,
            action_type=expected_request.action_type,
            target_ref=expected_request.target_ref,
            input_hash=expected_request.input_hash,
            expected_state_db=state_db_path,
        )
    return _submit_wiki_candidate_with_material_action(
        trusted_config=trusted_config,
        wiki_base=wiki_base,
        source=source,
        source_agent=source_agent,
        source_session_id=source_session_id,
        target_path=target_path,
        payload=payload,
        evidence_refs=evidence_refs,
        proposed_actions=proposed_actions,
        confidence_score=confidence_score,
        risk_level=risk_level,
        expected_hash=expected_hash,
        source_path=source_path,
        source_content_hash=source_content_hash,
        proposed_action=proposed_action,
        binding=binding,
        material_action=material_action,
    )


def _submit_wiki_candidate_with_material_action(
    *,
    trusted_config: Any,
    wiki_base: Path,
    source: str,
    source_agent: str,
    source_session_id: str | None,
    target_path: Path,
    payload: Dict[str, Any],
    evidence_refs: Iterable[str],
    proposed_actions: List[str],
    confidence_score: float,
    risk_level: str,
    expected_hash: str | None,
    source_path: str,
    source_content_hash: str,
    proposed_action: str,
    binding: Dict[str, str],
    material_action: MaterialActionAuthorization,
) -> TrustedPushResult:
    """Submit one candidate using an already sealed exact action command."""

    terminal_receipt = material_action.terminal_receipt()
    if terminal_receipt is not None:
        return _terminal_replay_result(
            trusted_config=trusted_config,
            wiki_base=wiki_base,
            target_path=target_path,
            content_hash=sha256_text(str(payload.get("content", ""))),
            expected_hash=expected_hash,
            source_path=source_path,
            source_content_hash=source_content_hash,
            proposed_action=proposed_action,
            binding=binding,
            material_action=material_action,
        )

    material_action, permit = resolve_material_action_authorization(
        material_action,
        owner=TRUSTED_MARKDOWN_OWNER,
        executor_id=TRUSTED_MARKDOWN_EXECUTOR,
        action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
        expected_state_db=trusted_config.db_path.parent / "producer_consumer_ledger.db",
    )
    content = str(payload.get("content", ""))
    content_hash = sha256_text(content)
    if not trusted_config.enabled:
        return TrustedPushResult(
            action="write",
            mode=trusted_config.mode,
            target_path=str(target_path),
            content_hash=content_hash,
            expected_existing_hash=expected_hash,
            source_path=source_path,
            source_content_hash=source_content_hash,
            proposed_action=proposed_action,
            material_command_id=permit.command_id,
            material_target_ref=binding["target_ref"],
            material_input_hash=binding["input_hash"],
            material_effect_db_path=str(trusted_config.db_path),
            material_action=material_action,
        )

    payload = {
        **payload,
        "material_before_hash": trusted_markdown_target_state_hash(target_path),
        "material_action": {
            "command_id": permit.command_id,
            "decision_revision_id": permit.decision_revision_id,
            "action_id": permit.action_id,
            "effect_id": permit.effect_id,
            "executor_id": permit.executor_id,
            "target_ref": binding["target_ref"],
            "input_hash": binding["input_hash"],
        },
    }

    candidate = CandidateBundle.from_payload(
        source=source,
        source_agent=source_agent,
        source_session_id=source_session_id,
        target_kind="markdown",
        target_path=str(target_path),
        payload=payload,
        evidence_refs=list(evidence_refs),
        confidence_score=confidence_score,
        risk_level=risk_level,
        proposed_actions=proposed_actions,
    )
    proposal = ProposalQueue(
        trusted_config.db_path,
        wiki_base=wiki_base,
        config=trusted_config,
    ).submit_candidate(candidate, shadow=trusted_config.shadow)
    action = "intercept" if trusted_config.enforce else "write"
    if trusted_config.enforce and proposal.status == "rejected":
        record_trusted_markdown_no_effect_terminal(
            material_action,
            target_path=target_path,
            status="rejected",
            reason_code="trusted_push_gate_rejected",
            evidence_ref=f"target-journal:trusted-gate-reject:{proposal.proposal_id}",
        )
    return TrustedPushResult(
        action=action,
        mode=trusted_config.mode,
        proposal_id=proposal.proposal_id,
        status=proposal.status,
        gate_decision=proposal.gate_decision,
        target_path=str(target_path),
        content_hash=content_hash,
        expected_existing_hash=expected_hash,
        source_path=source_path,
        source_content_hash=source_content_hash,
        proposed_action=proposed_action,
        material_command_id=permit.command_id,
        material_target_ref=binding["target_ref"],
        material_input_hash=binding["input_hash"],
        material_effect_db_path=str(trusted_config.db_path),
        material_action=material_action,
    )


def _terminal_replay_result(
    *,
    trusted_config: Any,
    wiki_base: Path,
    target_path: Path,
    content_hash: str,
    expected_hash: str | None,
    source_path: str,
    source_content_hash: str,
    proposed_action: str,
    binding: Dict[str, str],
    material_action: MaterialActionAuthorization,
) -> TrustedPushResult:
    """Return the original proposal receipt for one exact terminal command."""

    permit = material_action.permit
    proposals = ProposalQueue(
        trusted_config.db_path,
        wiki_base=wiki_base,
        config=trusted_config,
    ).find_by_material_command_id(permit.command_id)
    if len(proposals) != 1:
        raise RuntimeError(
            "terminal trusted-push replay requires one exact original proposal"
        )
    proposal = proposals[0]
    if (
        proposal.candidate.target_path != str(target_path)
        or sha256_text(str(proposal.candidate.payload.get("content", "")))
        != content_hash
    ):
        raise RuntimeError("terminal trusted-push replay proposal binding drifted")
    return TrustedPushResult(
        action="intercept" if trusted_config.enforce else "write",
        mode=trusted_config.mode,
        proposal_id=proposal.proposal_id,
        status=proposal.status,
        gate_decision=proposal.gate_decision,
        target_path=str(target_path),
        content_hash=content_hash,
        expected_existing_hash=expected_hash,
        source_path=source_path,
        source_content_hash=source_content_hash,
        proposed_action=proposed_action,
        material_command_id=permit.command_id,
        material_target_ref=binding["target_ref"],
        material_input_hash=binding["input_hash"],
        material_effect_db_path=str(trusted_config.db_path),
        material_action=material_action,
    )


def submit_distillation_page_candidate(
    *,
    wiki_base: Path,
    fragment: Any,
    result: Any,
    page_id: str,
    file_path: Path,
    page_content: str,
) -> tuple[TrustedPushResult, Any | None]:
    evidence_refs = [f"session:{result.session_id or 'unknown'}"]
    if result.source:
        evidence_refs.append(f"source:{result.source}")
    trusted = submit_wiki_write_candidate(
        wiki_base=wiki_base,
        source="hephaestus_distillation",
        source_agent=result.source or "distill",
        source_session_id=result.session_id,
        target_path=file_path,
        payload=_payload(fragment, page_id, file_path, page_content),
        evidence_refs=evidence_refs,
        proposed_actions=["create_wiki_page"],
    )
    if not trusted.proposal_id:
        return trusted, None
    from core.hephaestus.distillation_models import PipelineLayerResult

    return trusted, PipelineLayerResult(
        10,
        "trusted_push",
        trusted.status not in {"rejected", "failed"},
        {
            "mode": trusted.mode,
            "proposal_id": trusted.proposal_id,
            "status": trusted.status,
            "gate_decision": trusted.gate_decision,
            "target_path": str(file_path),
            "intercepted": trusted.intercepted,
        },
    )


def submit_document_page_candidate(
    *,
    wiki_base: Path,
    fragment: Any,
    session_id: str,
    source: str,
    page_id: str,
    file_path: Path,
    page_content: str,
) -> TrustedPushResult:
    evidence_refs = [f"document_session:{session_id or 'unknown'}"]
    if source:
        evidence_refs.append(f"source:{source}")
    return submit_wiki_write_candidate(
        wiki_base=wiki_base,
        source="hephaestus_document_pipeline",
        source_agent=source or "document",
        source_session_id=session_id,
        target_path=file_path,
        payload=_payload(fragment, page_id, file_path, page_content),
        evidence_refs=evidence_refs,
        proposed_actions=["create_wiki_page"],
    )


def submit_distill_action_candidate(
    *,
    wiki_base: Path,
    source_agent: str,
    source_session_id: str | None,
    target_path: Path,
    page_content: str,
    action: str,
    claim: Dict[str, Any],
    payload: Dict[str, Any],
    expected_existing_hash: str,
    evidence_refs: Iterable[str],
    confidence_score: float,
) -> TrustedPushResult:
    refs = list(evidence_refs)
    if source_session_id:
        refs.append(f"session:{source_session_id}")
    return submit_wiki_write_candidate(
        wiki_base=wiki_base,
        source="hephaestus_distill_action",
        source_agent=source_agent or "distill_action_router",
        source_session_id=source_session_id,
        target_path=target_path,
        payload={
            "title": f"Distill action: {action}",
            "content": page_content,
            "target_path": str(target_path),
            "expected_existing_hash": expected_existing_hash,
            "distill_action": action,
            "claim": claim,
            "source_event_ids": payload.get("source_event_ids", []),
            "gate_decision_id": payload.get("gate_decision_id", ""),
            "candidate_summary": payload.get("candidate_summary", ""),
        },
        evidence_refs=refs,
        confidence_score=confidence_score,
        risk_level="medium",
        proposed_actions=[f"distill_{action}"],
    )


def _payload(fragment: Any, page_id: str, path: Path, content: str) -> Dict[str, Any]:
    return {
        "title": fragment.title,
        "form": fragment.form,
        "frontmatter": fragment.frontmatter,
        "content": content,
        "page_id": page_id,
        "target_path": str(path),
    }
