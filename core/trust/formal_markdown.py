"""Trusted entrypoint for formal Markdown vault mutations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionRequest,
    authorize_exact_project_contract_action,
    resolve_material_action_authorization,
)
from core.trust.config import load_trusted_push_config
from core.trust.models import sha256_text
from core.trust.vault_mutation_service import (
    TRUSTED_MARKDOWN_ACTION_TYPE,
    TRUSTED_MARKDOWN_EXECUTOR,
    TRUSTED_MARKDOWN_OWNER,
    TrustedVaultMutationResult,
    TrustedVaultMutationService,
    commit_trusted_markdown,
    trusted_markdown_material_action_binding,
)


@dataclass(frozen=True)
class TrustedMarkdownDecisionPolicy:
    """Domain-owned decision semantics for one formal Markdown family."""

    contract_id: str
    contract_revision_id: str
    contract_text: str
    source_namespace: str
    producer: str
    producer_code_hash: str
    evaluator_id: str
    constraints: tuple[str, ...]
    approved_candidate_key: str
    approved_candidate_summary: str
    rejected_candidate_key: str
    rejected_candidate_summary: str
    approved_reason_code: str
    rejected_reason_code: str
    committed_metric: str
    rejected_metric: str


def authorize_exact_markdown_action(
    *,
    policy: TrustedMarkdownDecisionPolicy,
    wiki_base: Path,
    target_path: Path,
    content: str,
    proposed_action: str,
    expected_existing_hash: str | None,
    source_facts: Mapping[str, Any],
    evidence_refs: Iterable[str],
    task: str,
    goal: str,
    created_at: str,
    source_path: str | Path = "",
    source_content_hash: str = "",
) -> MaterialActionAuthorization:
    """Seal a caller-owned exact Markdown decision before the trusted sink."""

    refs = tuple(dict.fromkeys(str(ref).strip() for ref in evidence_refs if str(ref).strip()))
    if not refs:
        raise ValueError("formal Markdown decision requires domain evidence refs")
    target = Path(target_path).expanduser().resolve(strict=False)
    source = (
        Path(source_path).expanduser().resolve(strict=False)
        if str(source_path or "")
        else None
    )
    binding = trusted_markdown_material_action_binding(
        target_path=target,
        content=content,
        proposed_action=proposed_action,
        expected_existing_hash=expected_existing_hash,
        source_path=source or "",
        source_content_hash=source_content_hash,
    )
    state_db_path = (
        load_trusted_push_config(wiki_base=Path(wiki_base).expanduser()).db_path.parent
        / "producer_consumer_ledger.db"
    ).resolve(strict=False)
    request = MaterialActionRequest(
        owner=TRUSTED_MARKDOWN_OWNER,
        executor_id=TRUSTED_MARKDOWN_EXECUTOR,
        action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
        expected_state_db=str(state_db_path),
    )
    try:
        authorization, _ = resolve_material_action_authorization(
            None,
            owner=request.owner,
            executor_id=request.executor_id,
            action_type=request.action_type,
            target_ref=request.target_ref,
            input_hash=request.input_hash,
            expected_state_db=state_db_path,
        )
        return authorization
    except PermissionError as exc:
        if "canonical material-action authorization is required" not in str(exc):
            raise
    mutation_facts = {
        "schema_version": "mnemos.formal_markdown_decision_facts.v1",
        "target_path": str(target),
        "content_hash": sha256_text(content),
        "proposed_action": proposed_action,
        "expected_existing_hash": expected_existing_hash or "",
        "source_path": str(source) if source is not None else "",
        "source_content_hash": source_content_hash,
    }
    return authorize_exact_project_contract_action(
        expected_request=request,
        state_db_path=state_db_path,
        contract_id=policy.contract_id,
        contract_revision_id=policy.contract_revision_id,
        contract_text=policy.contract_text,
        source_namespace=policy.source_namespace,
        source_facts={
            "mutation": mutation_facts,
            "domain_facts": dict(source_facts),
        },
        decision_checks={
            "target_within_wiki_root": target.is_relative_to(
                Path(wiki_base).expanduser().resolve(strict=False)
            ),
            "domain_policy_complete": bool(
                policy.contract_id
                and policy.contract_revision_id
                and policy.evaluator_id
                and policy.constraints
            ),
            "domain_facts_and_evidence_present": bool(source_facts) and bool(refs),
            "mutation_action_present": bool(str(proposed_action).strip()),
        },
        evidence_refs=refs,
        task=task,
        goal=goal,
        constraints=policy.constraints,
        created_at=created_at,
        producer=policy.producer,
        producer_version=policy.contract_revision_id,
        producer_code_hash=policy.producer_code_hash,
        evaluator_id=policy.evaluator_id,
        approved_candidate_key=policy.approved_candidate_key,
        approved_candidate_summary=policy.approved_candidate_summary,
        rejected_candidate_key=policy.rejected_candidate_key,
        rejected_candidate_summary=policy.rejected_candidate_summary,
        approved_reason_code=policy.approved_reason_code,
        rejected_reason_code=policy.rejected_reason_code,
        committed_metric=policy.committed_metric,
        rejected_metric=policy.rejected_metric,
    )


def submit_or_write_markdown(
    *,
    wiki_base: Path,
    target_path: Path,
    content: str,
    source: str,
    actor: str = "system",
    source_session_id: str = "",
    evidence_refs: Iterable[str] = (),
    proposed_action: str = "update_markdown",
    expected_existing_hash: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    encoding: str = "utf-8",
    material_action: MaterialActionAuthorization | None = None,
) -> TrustedVaultMutationResult:
    """Submit formal Markdown to trusted push, writing only when not intercepted."""

    target = Path(target_path).expanduser()
    result = TrustedVaultMutationService(wiki_base=Path(wiki_base).expanduser()).submit_markdown(
        target_path=target,
        content=content,
        source=source,
        actor=actor,
        source_session_id=source_session_id,
        evidence_refs=evidence_refs,
        proposed_action=proposed_action,
        expected_existing_hash=expected_existing_hash,
        metadata=metadata,
        material_action=material_action,
    )
    commit_trusted_markdown(
        result,
        target_path=target,
        content=content,
        encoding=encoding,
        material_action=result.material_action,
    )
    return result


def submit_or_write_markdown_with_decision(
    *,
    decision_policy: TrustedMarkdownDecisionPolicy,
    decision_facts: Mapping[str, Any],
    decision_task: str,
    decision_goal: str,
    decision_created_at: str,
    wiki_base: Path,
    target_path: Path,
    content: str,
    source: str,
    actor: str = "system",
    source_session_id: str = "",
    evidence_refs: Iterable[str] = (),
    proposed_action: str = "update_markdown",
    expected_existing_hash: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    encoding: str = "utf-8",
) -> TrustedVaultMutationResult:
    """Run an explicit domain decision, then the unchanged trusted sink."""

    refs = tuple(evidence_refs)
    material_action = authorize_exact_markdown_action(
        policy=decision_policy,
        wiki_base=wiki_base,
        target_path=target_path,
        content=content,
        proposed_action=proposed_action,
        expected_existing_hash=expected_existing_hash,
        source_facts=decision_facts,
        evidence_refs=refs,
        task=decision_task,
        goal=decision_goal,
        created_at=decision_created_at,
    )
    return submit_or_write_markdown(
        wiki_base=wiki_base,
        target_path=target_path,
        content=content,
        source=source,
        actor=actor,
        source_session_id=source_session_id,
        evidence_refs=refs,
        proposed_action=proposed_action,
        expected_existing_hash=expected_existing_hash,
        metadata=metadata,
        encoding=encoding,
        material_action=material_action,
    )
