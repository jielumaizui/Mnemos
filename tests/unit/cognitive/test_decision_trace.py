from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from pathlib import Path

import pytest

from core.access_policy import PrincipalEnvelope
from core.application.cognitive_state import CognitiveStateApplicationService
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.belief_revision import BeliefRevisionCommand, BeliefRevisionStore
from core.cognitive.decision_trace import (
    DecisionCandidateEvaluation,
    DecisionRejectionEvaluation,
    DecisionTraceStore,
    MaterialActionObservation,
    MaterialActionCoordinator,
    MaterialActionRequest,
    MaterialActionTerminal,
    ProjectContractDecisionContext,
    ProjectContractDecisionEvaluation,
    ProjectContractMaterialActionResolver,
    authorize_exact_project_contract_action,
    build_exact_project_contract_evaluator,
    find_pending_material_action_authorization,
    resolve_material_action_authorization,
    resolve_material_action_recovery_authorization,
)
from core.cognitive.state_contract import sha256_json, validate_cognitive_state_payload
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore, CognitiveStateUnitOfWork
from core.evidence.source_authority import SourceAuthority
from tests.cognitive_decision_fixtures import (
    TEST_PROJECT_CONTRACT_TEXT,
    TEST_USER_GOAL_TEXT,
    decision_authority_catalog,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
FIXED_PRECEDENCE = [
    "safety_permission_privacy",
    "explicit_user_goal",
    "project_constraint",
    "scoped_preference",
    "cost_convenience",
]


def test_exact_project_contract_evaluator_rejects_any_binding_drift(
    tmp_path: Path,
) -> None:
    expected = MaterialActionRequest(
        owner="trusted_vault",
        executor_id="trusted_vault_mutation_service",
        action_type="formal_markdown_mutation",
        target_ref="markdown:/vault/page.md",
        input_hash=HASH_A,
        expected_state_db=str(tmp_path / "producer_consumer_ledger.db"),
    )
    facts_hash, evaluator = build_exact_project_contract_evaluator(
        expected_request=expected,
        source_facts={"page_id": "page-1", "receipt_id": "delete-1"},
        decision_checks={"subject_deletion_receipt_verified": True},
        approved_candidate_key="delete_receipted_subject_page",
        approved_candidate_summary="Delete the page bound to the verified receipt.",
        rejected_candidate_key="retain_unbound_subject_page",
        rejected_candidate_summary="Retain a page outside the verified receipt.",
        approved_reason_code="subject_deletion_binding_verified",
        rejected_reason_code="subject_deletion_binding_rejected",
        committed_metric="subject_page_delete_receipt",
        rejected_metric="unbound_subject_page_delete_count",
    )

    approved = evaluator(expected)
    rejected = evaluator(replace(expected, target_ref="markdown:/vault/other.md"))

    assert approved.source_facts_hash == facts_hash
    assert approved.approval_decision == "approved"
    assert approved.selection_key == "delete_receipted_subject_page"
    assert rejected.approval_decision == "rejected"
    assert rejected.selection_key == "retain_unbound_subject_page"

    _, failed_evaluator = build_exact_project_contract_evaluator(
        expected_request=expected,
        source_facts={"page_id": "page-1", "receipt_id": "delete-1"},
        decision_checks={"subject_deletion_receipt_verified": False},
        approved_candidate_key="delete_receipted_subject_page",
        approved_candidate_summary="Delete the page bound to the verified receipt.",
        rejected_candidate_key="retain_unbound_subject_page",
        rejected_candidate_summary="Retain a page outside the verified receipt.",
        approved_reason_code="subject_deletion_binding_verified",
        rejected_reason_code="subject_deletion_binding_rejected",
        committed_metric="subject_page_delete_receipt",
        rejected_metric="unbound_subject_page_delete_count",
    )
    failed = failed_evaluator(expected)
    assert failed.approval_decision == "rejected"
    assert failed.selection_key == "retain_unbound_subject_page"
    for candidate in failed.candidates:
        refs = set(candidate.supporting_evidence) | set(candidate.opposing_evidence)
        assert any(ref.startswith("request-binding:") for ref in refs)
        assert any(ref.startswith("source-facts:") for ref in refs)
    assert failed.candidates[0].violated_value_keys == ("safety",)


def test_exact_project_contract_rejection_seals_without_an_action(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    request = MaterialActionRequest(
        owner="test-owner",
        executor_id="test-executor",
        action_type="test-action",
        target_ref="test-target:rejected",
        input_hash=HASH_A,
        expected_state_db=str(db_path),
    )

    with pytest.raises(
        PermissionError,
        match="project-contract evaluator rejected",
    ):
        authorize_exact_project_contract_action(
            expected_request=request,
            state_db_path=db_path,
            contract_id="project-contract:test-rejection",
            contract_revision_id="mnemos.test_rejection.v1",
            contract_text="Reject an action whose exact preconditions are not met.",
            source_namespace="test-rejection",
            source_facts={"target": "rejected", "eligible": False},
            decision_checks={"target_is_eligible": False},
            evidence_refs=("test-evidence:rejection",),
            task="Evaluate a rejected action",
            goal="Do not emit an action when the exact check fails.",
            constraints=("Fail closed when the target is not eligible.",),
            created_at="2026-07-17T09:00:00+00:00",
            producer="decision-trace-test",
            producer_version="mnemos.test_rejection.v1",
            producer_code_hash=HASH_C,
            evaluator_id="test-rejection-evaluator",
            approved_candidate_key="apply_eligible_test_action",
            approved_candidate_summary="Apply the eligible test action.",
            rejected_candidate_key="retain_ineligible_test_target",
            rejected_candidate_summary="Retain the ineligible target unchanged.",
            approved_reason_code="eligible_test_action_verified",
            rejected_reason_code="ineligible_test_action_rejected",
            committed_metric="eligible_test_action_receipt",
            rejected_metric="ineligible_test_action_count",
        )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_revisions"
        ).fetchone() == (3,)
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_outbox "
            "WHERE command_type='material_action_execute'"
        ).fetchone() == (0,)


def test_exact_project_contract_authorizer_seals_domain_owned_action(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    request = MaterialActionRequest(
        owner="test-owner",
        executor_id="test-executor",
        action_type="test-action",
        target_ref="test-target:exact",
        input_hash=HASH_A,
        expected_state_db=str(db_path),
    )

    authorization = authorize_exact_project_contract_action(
        expected_request=request,
        state_db_path=db_path,
        contract_id="project-contract:test-exact-action",
        contract_revision_id="mnemos.test_exact_action.v1",
        contract_text="Only the exact evaluated test action may run.",
        source_namespace="test-exact-action",
        source_facts={"target": "exact", "eligible": True},
        decision_checks={"target_is_eligible": True},
        evidence_refs=("test-evidence:exact",),
        task="Apply the exact test action",
        goal="Change only the evaluated test target.",
        constraints=("Do not change another target.",),
        created_at="2026-07-17T09:00:00+00:00",
        producer="decision-trace-test",
        producer_version="mnemos.test_exact_action.v1",
        producer_code_hash=HASH_C,
        evaluator_id="test-exact-action-evaluator",
        approved_candidate_key="apply_exact_test_action",
        approved_candidate_summary="Apply the exact evaluated test action.",
        rejected_candidate_key="reject_drifted_test_action",
        rejected_candidate_summary="Reject a drifted test action.",
        approved_reason_code="exact_test_action_verified",
        rejected_reason_code="exact_test_action_rejected",
        committed_metric="exact_test_action_receipt",
        rejected_metric="drifted_test_action_count",
    )

    assert authorization.permit.target_ref == "test-target:exact"
    recovered = find_pending_material_action_authorization(
        state_db_path=db_path,
        owner=request.owner,
        executor_id=request.executor_id,
        action_type=request.action_type,
        target_ref=request.target_ref,
        input_hash=request.input_hash,
    )
    assert recovered is not None
    assert recovered.permit.command_id == authorization.permit.command_id
    assert (
        find_pending_material_action_authorization(
            state_db_path=db_path,
            owner=request.owner,
            executor_id=request.executor_id,
            action_type=request.action_type,
            target_ref=request.target_ref,
            input_hash=HASH_B,
        )
        is None
    )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_revisions"
        ).fetchone() == (3,)

    permit = authorization.permit
    authorization.record_terminal(
        MaterialActionTerminal(
            status="committed",
            target_effect_id=permit.effect_id,
            before_hash=HASH_A,
            after_hash=HASH_B,
            evidence_refs=(
                f"material-command:{permit.command_id}",
                f"decision-revision:{permit.decision_revision_id}",
                f"material-effect:{permit.effect_id}",
                f"target-after:{HASH_B}",
                f"target-oracle:test-target:{HASH_B}",
            ),
            outcome="exact test target committed",
            created_at="2026-07-17T09:01:00+00:00",
        )
    )
    assert (
        find_pending_material_action_authorization(
            state_db_path=db_path,
            owner=request.owner,
            executor_id=request.executor_id,
            action_type=request.action_type,
            target_ref=request.target_ref,
            input_hash=request.input_hash,
        )
        is None
    )


def test_exact_project_contract_redacts_evidence_before_snapshot_identity(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    request = MaterialActionRequest(
        owner="knowledge_graph",
        executor_id="knowledge_graph",
        action_type="upsert_relation",
        target_ref="credential-handling->security:related_to",
        input_hash=HASH_A,
        expected_state_db=str(db_path),
    )

    authorization = authorize_exact_project_contract_action(
        expected_request=request,
        state_db_path=db_path,
        contract_id="project-contract:test-sensitive-evidence",
        contract_revision_id="mnemos.test_sensitive_evidence.v1",
        contract_text="Persist only the exact relation accepted by validation.",
        source_namespace="test-sensitive-evidence",
        source_facts={"target": request.target_ref, "eligible": True},
        decision_checks={"relation_is_eligible": True},
        evidence_refs=(
            "relation-evidence: "
            "api_key=DUMMY_CREDENTIAL_VALUE_FOR_REDACTION_TEST",
        ),
        task="Apply the exact relation",
        goal="Persist only the evaluated relation.",
        constraints=("Do not persist credentials from evidence.",),
        created_at="2026-07-21T09:00:00+00:00",
        producer="decision-trace-test",
        producer_version="mnemos.test_sensitive_evidence.v1",
        producer_code_hash=HASH_C,
        evaluator_id="test-sensitive-evidence-evaluator",
        approved_candidate_key="apply_redacted_relation",
        approved_candidate_summary="Apply the exact relation with redacted evidence.",
        rejected_candidate_key="reject_unbound_relation",
        rejected_candidate_summary="Reject a relation outside the exact binding.",
        approved_reason_code="redacted_relation_verified",
        rejected_reason_code="unbound_relation_rejected",
        committed_metric="redacted_relation_receipt",
        rejected_metric="unbound_relation_count",
    )

    store = CognitiveStateStore(db_path)
    decision = store.revision(authorization.permit.decision_revision_id)
    assert decision is not None
    snapshot = store.revision(str(decision.payload["snapshot_revision_id"]))
    assert snapshot is not None
    validate_cognitive_state_payload("cognitive_state_snapshot", snapshot.payload)
    serialized = json.dumps(dict(snapshot.payload), ensure_ascii=False, sort_keys=True)
    assert "DUMMY_CREDENTIAL_VALUE_FOR_REDACTION_TEST" not in serialized
    assert "[REDACTED:CREDENTIAL]" in serialized


def test_exact_project_contract_preserves_identity_refs_that_look_like_credentials(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    target_ref = "security-page->invalid-api-key:references"
    request = MaterialActionRequest(
        owner="knowledge_graph",
        executor_id="knowledge_graph",
        action_type="upsert_relation",
        target_ref=target_ref,
        input_hash=HASH_A,
        expected_state_db=str(db_path),
    )

    authorization = authorize_exact_project_contract_action(
        expected_request=request,
        state_db_path=db_path,
        contract_id="project-contract:test-identity-ref",
        contract_revision_id="mnemos.test_identity_ref.v1",
        contract_text="Persist only the exact relation accepted by validation.",
        source_namespace="test-identity-ref",
        source_facts={"target": target_ref, "eligible": True},
        decision_checks={"relation_is_eligible": True},
        evidence_refs=("relation-source:wiki_link",),
        task=f"Upsert relation {target_ref}",
        goal="Persist only the evaluated relation.",
        constraints=("Do not change the exact relation identity.",),
        created_at="2026-07-21T09:00:00+00:00",
        producer="decision-trace-test",
        producer_version="mnemos.test_identity_ref.v1",
        producer_code_hash=HASH_C,
        evaluator_id="test-identity-ref-evaluator",
        approved_candidate_key="apply_identity_bound_relation",
        approved_candidate_summary="Apply the exact identity-bound relation.",
        rejected_candidate_key="reject_identity_drift",
        rejected_candidate_summary="Reject any relation identity drift.",
        approved_reason_code="relation_identity_verified",
        rejected_reason_code="relation_identity_drifted",
        committed_metric="identity_bound_relation_receipt",
        rejected_metric="relation_identity_drift_count",
    )

    assert authorization.permit.target_ref == target_ref
    store = CognitiveStateStore(db_path)
    decision = store.revision(authorization.permit.decision_revision_id)
    assert decision is not None
    assert decision.payload["action_specs"][0]["target_ref"] == target_ref


def test_dead_letter_retry_explicitly_supersedes_failed_decision(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    request = MaterialActionRequest(
        owner="retry-owner",
        executor_id="retry-executor",
        action_type="retry-action",
        target_ref="retry-target:exact",
        input_hash=HASH_A,
        expected_state_db=str(db_path),
    )

    def authorize(attempt: int, created_at: str):
        return authorize_exact_project_contract_action(
            expected_request=request,
            state_db_path=db_path,
            contract_id="project-contract:test-dead-letter-retry",
            contract_revision_id="mnemos.test_dead_letter_retry.v1",
            contract_text="Retry only through a new decision after a dead letter.",
            source_namespace="test-dead-letter-retry",
            source_facts={"attempt": attempt, "eligible": True},
            decision_checks={"retry_is_explicit": True},
            evidence_refs=(f"test-retry-attempt:{attempt}",),
            task="Retry the exact test action",
            goal="Retry only after explicitly superseding the failed decision.",
            constraints=("Do not reopen a terminal action identity.",),
            created_at=created_at,
            producer="decision-trace-test",
            producer_version="mnemos.test_dead_letter_retry.v1",
            producer_code_hash=HASH_C,
            evaluator_id="test-dead-letter-retry-evaluator",
            approved_candidate_key="run_explicit_retry_generation",
            approved_candidate_summary="Run a new explicitly linked retry generation.",
            rejected_candidate_key="retain_dead_letter_terminal",
            rejected_candidate_summary="Retain the failed terminal without a superseding decision.",
            approved_reason_code="explicit_retry_verified",
            rejected_reason_code="unlinked_retry_rejected",
            committed_metric="retry_generation_receipt",
            rejected_metric="unlinked_retry_count",
        )

    first = authorize(1, "2026-07-17T09:00:00+00:00")
    permit = first.permit
    first.record_terminal(
        MaterialActionTerminal(
            status="dead_letter",
            target_effect_id=permit.effect_id,
            before_hash=HASH_B,
            after_hash=HASH_B,
            evidence_refs=(
                f"material-command:{permit.command_id}",
                f"decision-revision:{permit.decision_revision_id}",
                f"material-effect:{permit.effect_id}",
                f"attempted-effect:{permit.effect_id}",
                f"retry-budget-exhausted:{permit.command_id}",
                "target-oracle:test-dead-letter",
            ),
            reason_code="test_retry_budget_exhausted",
            retry_exhausted=True,
            outcome="first attempt exhausted",
            created_at="2026-07-17T09:01:00+00:00",
        )
    )

    second = authorize(2, "2026-07-17T09:02:00+00:00")
    assert second.permit.command_id != first.permit.command_id
    decision = second.coordinator.state_store.revision(
        second.permit.decision_revision_id
    )
    assert decision is not None
    assert decision.payload["supersedes_decision_revision_ids"] == [
        first.permit.decision_revision_id
    ]


def test_pending_material_action_lookup_does_not_initialize_missing_store(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "missing" / "producer_consumer_ledger.db"

    assert (
        find_pending_material_action_authorization(
            state_db_path=db_path,
            owner="test-owner",
            executor_id="test-executor",
            action_type="test-action",
            target_ref="test-target:missing",
            input_hash=HASH_A,
        )
        is None
    )
    assert not db_path.exists()


def test_project_contract_resolver_seals_a_real_pre_action_decision(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    facts_hash = sha256_json({"target": "test-target:1", "allowed": True})

    def evaluate(request: MaterialActionRequest) -> ProjectContractDecisionEvaluation:
        request_hash = sha256_json(
            {
                "owner": request.owner,
                "executor_id": request.executor_id,
                "action_type": request.action_type,
                "target_ref": request.target_ref,
                "input_hash": request.input_hash,
            }
        )
        refs = (
            f"request-binding:{request_hash}",
            f"source-facts:{facts_hash}",
        )
        approved = request.target_ref == "test-target:1"
        return ProjectContractDecisionEvaluation(
            request_binding_hash=request_hash,
            source_facts_hash=facts_hash,
            candidates=(
                DecisionCandidateEvaluation(
                    key="apply_verified_test_target",
                    summary="Apply the verified test target.",
                    supporting_evidence=refs if approved else (),
                    opposing_evidence=() if approved else refs,
                    satisfies_value_keys=("safety", "project_contract"),
                ),
                DecisionCandidateEvaluation(
                    key="reject_foreign_test_target",
                    summary="Reject a foreign test target.",
                    supporting_evidence=refs if not approved else (),
                    opposing_evidence=() if not approved else refs,
                    satisfies_value_keys=("safety",),
                ),
            ),
            selection_key=(
                "apply_verified_test_target"
                if approved
                else "reject_foreign_test_target"
            ),
            rejections=(
                DecisionRejectionEvaluation(
                    candidate_key=(
                        "reject_foreign_test_target"
                        if approved
                        else "apply_verified_test_target"
                    ),
                    reason_code="exact_test_target_evaluated",
                    evidence_refs=refs,
                ),
            ),
            expected_outcomes=(
                {"metric": "test_target_effect", "operator": "equals", "value": 1},
            ),
            approval_decision="approved" if approved else "rejected",
            approval_evidence_ref=f"source-facts:{facts_hash}",
        )

    resolver = ProjectContractMaterialActionResolver(
        ProjectContractDecisionContext(
            state_db_path=db_path,
            contract_id="project-contract:test-material-effect",
            contract_revision_id="mnemos.test_material_effect.v1",
            contract_text="Only the verified test target may be changed.",
            contract_evidence_ref="project-contract:test-material-effect#v1",
            source_id="event:test-material-effect",
            source_revision_id="event-payload:test-material-effect",
            source_content_hash=HASH_A,
            source_uri="event://test/material-effect",
            evidence_refs=("event:test-material-effect",),
            task="Apply one deterministic test effect",
            goal="Apply the exact project-contract effect.",
            constraints=("Do not change another target.",),
            created_at="2026-07-17T09:00:00+00:00",
            scope_prefix="test-material-effect",
            producer="decision-trace-test",
            producer_version="mnemos.test_material_effect.v1",
            producer_code_hash=HASH_C,
            evaluator_id="exact-test-target-evaluator",
            evaluator=evaluate,
        )
    )

    authorization = resolver(
        MaterialActionRequest(
            owner="test-owner",
            executor_id="test-executor",
            action_type="test-effect",
            target_ref="test-target:1",
            input_hash=HASH_A,
            expected_state_db=str(db_path),
        )
    )

    assert authorization.permit.target_ref == "test-target:1"
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_state_revisions"
        ).fetchone()[0] == 3
        value_payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM cognitive_state_revisions "
                "WHERE object_type='value_context'"
            ).fetchone()[0]
        )
    assert value_payload["user_goal"] == ""


def _principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="mcp:codex:test",
        agent="codex",
        host_kind="test",
        capability_id="decision-trace-test",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def _source() -> dict:
    source_id = "raw-event-cog036-1"
    return {
        "source_id": source_id,
        "source_revision_id": "raw-revision-cog036-1",
        "source_kind": "explicit_user_decision",
        "source_uri": "raw://decision/cog036/1",
        "content_hash": HASH_A,
        "evidence_refs": ["raw-event-cog036-1#0:64"],
        "created_at": "2026-07-17T09:00:00+00:00",
        "privacy_level": "private",
        "access_control": make_cognitive_access_envelope(
            owner_principal_id="mcp:codex:test",
            owner_agent="codex",
            scope_type="project",
            scope_id="mnemos",
            project="mnemos",
            purposes=("cognitive_state_read", "cognitive_state_write"),
            consent_provenance_refs=(source_id,),
            sensitivity="sensitive",
            retention_policy="cognitive_state",
            source_acl_lineage=(HASH_B,),
        ),
    }


def _decision_authorities():
    return decision_authority_catalog(
        source_id="raw-event-cog036-1",
        source_revision_id="raw-revision-cog036-1",
    )


def _record_decision(
    service: CognitiveStateApplicationService,
    request: dict,
    *,
    principal: PrincipalEnvelope | None = None,
) -> dict:
    catalog, _ = _decision_authorities()
    return service.record_decision(
        request,
        principal=principal or _principal(),
        source_authority_catalog=catalog,
    )


def _decision_request() -> dict:
    _, authorities = _decision_authorities()
    project_authority = authorities[SourceAuthority.PROJECT_CONTRACT.value]
    user_authority = authorities[SourceAuthority.EXPLICIT_USER.value]
    return {
        "idempotency_key": "cog036-tracer-bullet-1",
        "source": _source(),
        "scope": {"type": "project", "id": "mnemos"},
        "task": "Repair COG-036",
        "goal": "Require a canonical decision before every material action.",
        "constraints": ["one canonical state owner", "no legacy bypass"],
        "values": [
            {
                "key": "safety",
                "category": "safety_permission_privacy",
                "constraint": TEST_PROJECT_CONTRACT_TEXT,
                "source_authority_id": project_authority.source_authority_id,
                "source_id": project_authority.source_event_id,
                "source_revision_id": project_authority.source_event_id,
                "source_content_hash": project_authority.content_sha256,
                "evidence_refs": [
                    "audit:COG-036:value-contract",
                    project_authority.source_authority_id,
                ],
                "valid_from": "2026-07-12T00:00:00+00:00",
                "valid_until": "",
                "changed_decision": True,
            },
            {
                "key": "goal",
                "category": "explicit_user_goal",
                "constraint": TEST_USER_GOAL_TEXT,
                "source_authority_id": user_authority.source_authority_id,
                "source_id": user_authority.source_event_id,
                "source_revision_id": user_authority.source_event_id,
                "source_content_hash": user_authority.content_sha256,
                "evidence_refs": [
                    "raw-event-cog036-1#0:64",
                    user_authority.source_authority_id,
                ],
                "valid_from": "2026-07-17T09:00:00+00:00",
                "valid_until": "",
                "changed_decision": True,
            },
        ],
        "candidates": [
            {
                "key": "legacy",
                "summary": "Keep caller-authored payloads and add a final reason.",
                "supporting_evidence": ["legacy:record_decision"],
                "opposing_evidence": ["audit:COG-036"],
                "violated_value_keys": ["safety"],
            },
            {
                "key": "strict",
                "summary": "Seal canonical state and issue a bound action permit.",
                "supporting_evidence": ["audit:COG-036", "design:52276ec7"],
                "opposing_evidence": [],
                "violated_value_keys": [],
            },
        ],
        "selection_key": "strict",
        "rejections": [
            {
                "candidate_key": "legacy",
                "reason_code": "hard_constraint_violation",
                "evidence_refs": ["audit:COG-036:value-contract"],
            }
        ],
        "model_spec": {
            "provider": "system",
            "model": "deterministic-rule",
            "route": "local",
            "version": "mnemos.cog036.v1",
            "config_hash": HASH_C,
        },
        "tool_specs": [
            {
                "name": "CognitiveStateStore",
                "version": "mnemos.cognitive_state_store.v1",
                "code_hash": HASH_B,
            }
        ],
        "prompt_spec": {
            "prompt_id": "none:deterministic-rule",
            "prompt_hash": HASH_C,
            "schema_hash": HASH_B,
        },
        "expected_outcomes": [
            {
                "metric": "action_without_decision",
                "operator": "equals",
                "value": 0,
            }
        ],
        "evaluation_window": {
            "starts_at": "2026-07-17T09:00:00+00:00",
            "ends_at": "2026-07-18T09:00:00+00:00",
        },
        "approval": {
            "mode": "explicit_user",
            "decision": "approved",
            "evidence_ref": "raw-event-cog036-1#0:64",
            "created_at": "2026-07-17T09:00:00+00:00",
        },
        "actions": [
            {
                "key": "write",
                "action_type": "formal_write",
                "owner": "trusted_vault",
                "executor": "trusted_vault",
                "target_ref": "wiki://03-Tech/COG-036.md",
                "input_hash": HASH_C,
                "rollback_contract": "restore the exact before hash",
                "expected_effect": "target content hash equals the input hash",
            }
        ],
    }


def test_record_decision_seals_one_canonical_material_action_idempotently(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)

    first = _record_decision(service, _decision_request())
    replay = _record_decision(service, _decision_request())

    assert first["status"] == "committed"
    assert replay["status"] == "existing"
    assert replay["revision_ids"] == first["revision_ids"]
    assert first["decision"]["object_id"].startswith("decision-")
    assert first["value_context"]["payload"]["precedence"] == FIXED_PRECEDENCE
    assert first["snapshot"]["payload"]["value_context_revision_id"] == (
        first["value_context"]["revision_id"]
    )
    snapshot_payload = dict(first["snapshot"]["payload"])
    snapshot_hash = snapshot_payload.pop("snapshot_hash")
    assert snapshot_hash == sha256_json(snapshot_payload)
    assert first["decision"]["payload"]["snapshot_hash"] == snapshot_hash
    assert first["decision"]["payload"]["selection"]["candidate_key"] == "strict"
    assert len(first["outbox_ids"]) == 1

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        command = conn.execute(
            "SELECT * FROM cognitive_state_outbox WHERE command_id=?",
            (first["outbox_ids"][0],),
        ).fetchone()
    assert command is not None
    assert command["command_type"] == "execute_material_action"
    payload = json.loads(str(command["payload_json"]))
    assert payload["decision_revision_id"] == first["decision"]["revision_id"]
    assert payload["action_type"] == "formal_write"
    assert payload["executor"] == "trusted_vault"


def test_record_decision_consumes_same_scope_belief_through_typed_purpose(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    source = _source()
    belief_source_access = make_cognitive_access_envelope(
        owner_principal_id="mcp:codex:test",
        owner_agent="codex",
        scope_type="project",
        scope_id="mnemos",
        project="mnemos",
        purposes=("belief_read", "cognitive_state_read", "cognitive_state_write"),
        consent_provenance_refs=(str(source["source_id"]),),
        sensitivity="sensitive",
        retention_policy="cognitive_state",
        source_acl_lineage=(HASH_B,),
    )
    belief = BeliefRevisionStore(service.store).revise(
        BeliefRevisionCommand(
            claim="Phase 3 aggregate closure requires a connected cognitive chain.",
            claim_kind="fact",
            scope_type="project",
            scope_id="mnemos",
            source_id=str(source["source_id"]),
            source_revision_id=str(source["source_revision_id"]),
            source_content_hash=str(source["content_hash"]),
            source_access_control=belief_source_access,
            supporting_evidence=("raw-event-cog036-1#0:64",),
            valid_from="2026-07-17T09:00:00+00:00",
            invalidation_conditions=("Phase 3 chain contract changes",),
            created_at="2026-07-17T09:00:00+00:00",
        ),
        principal=_principal(),
    )

    sealed = _record_decision(service, _decision_request())

    snapshot = sealed["snapshot"]["payload"]
    assert snapshot["active_belief_refs"] == [belief.revision_id]
    assert sealed["decision"]["payload"]["belief_revision_refs"] == [
        belief.revision_id
    ]
    belief_entry = next(
        entry
        for entry in snapshot["consumed_state"]
        if entry["revision_id"] == belief.revision_id
    )
    assert belief_entry["source_read_purpose"] == "belief_read"
    assert belief_entry["source_purpose_contract_hash"].startswith("sha256:")
    completeness = snapshot["source_completeness"]
    assert completeness["contract"]["schema_version"] == (
        "mnemos.decision_snapshot_source_purposes.v1"
    )
    assert completeness["by_object_type"]["belief_revision"] == {
        "purpose": "belief_read",
        "candidate_count": 1,
        "authorized_count": 1,
        "denied_by_reason": {},
    }
    assert snapshot["access_control"]["scope"]["resolution"] == "resolved"


def test_material_action_permit_is_bound_to_the_committed_executor(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    sealed = _record_decision(service, _decision_request())
    coordinator = MaterialActionCoordinator(service.store)

    permit = coordinator.authorize(
        sealed["outbox_ids"][0],
        executor_id="trusted_vault",
    )

    assert permit.schema_version == "mnemos.material_action_permit.v1"
    assert permit.command_id == sealed["outbox_ids"][0]
    assert permit.decision_revision_id == sealed["decision"]["revision_id"]
    assert permit.action_type == "formal_write"
    assert permit.executor_id == "trusted_vault"
    assert permit.target_ref == "wiki://03-Tech/COG-036.md"
    assert permit.target_hash == sha256_json(permit.target_ref)
    assert permit.input_hash == HASH_C
    assert permit.effect_id.startswith("material-effect-")
    assert permit.integrity_hash.startswith("sha256:")

    with pytest.raises(PermissionError, match="executor"):
        coordinator.authorize(
            sealed["outbox_ids"][0],
            executor_id="different_executor",
        )


def test_material_action_sink_revalidates_the_canonical_effect_binding(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    sealed = _record_decision(service, _decision_request())
    coordinator = MaterialActionCoordinator(service.store)
    permit = coordinator.authorize(
        sealed["outbox_ids"][0],
        executor_id="trusted_vault",
    )

    validated = coordinator.validate_for_effect(
        permit,
        owner="trusted_vault",
        executor_id="trusted_vault",
        action_type="formal_write",
        target_ref="wiki://03-Tech/COG-036.md",
        input_hash=HASH_C,
    )

    assert validated == permit
    with pytest.raises(PermissionError, match="target_ref"):
        coordinator.validate_for_effect(
            permit,
            owner="trusted_vault",
            executor_id="trusted_vault",
            action_type="formal_write",
            target_ref="wiki://03-Tech/other.md",
            input_hash=HASH_C,
        )
    with pytest.raises(PermissionError, match="input_hash"):
        coordinator.validate_for_effect(
            permit,
            owner="trusted_vault",
            executor_id="trusted_vault",
            action_type="formal_write",
            target_ref="wiki://03-Tech/COG-036.md",
            input_hash=HASH_B,
        )
    with pytest.raises(PermissionError, match="binding"):
        coordinator.validate_for_effect(
            replace(permit, owner="forged-owner"),
            owner="trusted_vault",
            executor_id="trusted_vault",
            action_type="formal_write",
            target_ref="wiki://03-Tech/COG-036.md",
            input_hash=HASH_C,
        )


def test_existing_store_without_activation_marker_fails_closed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    sealed = _record_decision(service, _decision_request())
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "DELETE FROM mnemos_schema_registry "
            "WHERE component='decision_trace_enforcement'"
        )

    with pytest.raises(RuntimeError, match="migration_required"):
        MaterialActionCoordinator(service.store).authorize(
            sealed["outbox_ids"][0],
            executor_id="trusted_vault",
        )


def test_material_action_terminal_closes_the_exact_effect_idempotently(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    sealed = _record_decision(service, _decision_request())
    coordinator = MaterialActionCoordinator(service.store)
    permit = coordinator.authorize(
        sealed["outbox_ids"][0],
        executor_id="trusted_vault",
    )
    terminal = MaterialActionTerminal(
        status="committed",
        target_effect_id=permit.effect_id,
        before_hash=HASH_A,
        after_hash=HASH_B,
        evidence_refs=(
            f"material-command:{permit.command_id}",
            f"decision-revision:{permit.decision_revision_id}",
            f"material-effect:{permit.effect_id}",
            f"target-after:{HASH_B}",
            f"target-oracle:trusted-vault:{HASH_B}",
        ),
        outcome="formal target committed",
        created_at="2026-07-17T09:01:00+00:00",
    )

    first = coordinator.record_terminal(permit, terminal)
    replay = coordinator.record_terminal(permit, terminal)

    assert first.receipt_id == replay.receipt_id
    assert first.command_id == permit.command_id
    assert first.decision_revision_id == permit.decision_revision_id
    assert first.action_id == permit.action_id
    assert first.effect_id == permit.effect_id
    assert first.status == "committed"
    assert service.store.pending_commands() == []
    with pytest.raises(RuntimeError, match="already terminal"):
        coordinator.authorize(
            sealed["outbox_ids"][0],
            executor_id="trusted_vault",
        )


def test_recovery_authorization_accepts_only_the_exact_terminal_command(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    sealed = _record_decision(service, _decision_request())
    coordinator = MaterialActionCoordinator(service.store)
    authorization = coordinator.bind(
        sealed["outbox_ids"][0],
        executor_id="trusted_vault",
    )
    permit = authorization.permit
    coordinator.record_terminal(
        permit,
        MaterialActionTerminal(
            status="committed",
            target_effect_id=permit.effect_id,
            before_hash=HASH_A,
            after_hash=HASH_B,
            evidence_refs=(
                f"material-command:{permit.command_id}",
                f"decision-revision:{permit.decision_revision_id}",
                f"material-effect:{permit.effect_id}",
                f"target-after:{HASH_B}",
                f"target-oracle:trusted-vault:{HASH_B}",
            ),
            outcome="formal target committed",
            created_at="2026-07-17T09:01:00+00:00",
        ),
    )

    fields = {
        "owner": "trusted_vault",
        "executor_id": "trusted_vault",
        "action_type": "formal_write",
        "target_ref": "wiki://03-Tech/COG-036.md",
        "input_hash": HASH_C,
        "expected_state_db": db_path,
    }
    with pytest.raises(RuntimeError, match="already terminal"):
        resolve_material_action_authorization(authorization, **fields)

    resolved, recovered_permit = resolve_material_action_recovery_authorization(
        authorization,
        **fields,
    )

    assert resolved is authorization
    assert recovered_permit == permit
    with pytest.raises(PermissionError, match="target_ref"):
        resolve_material_action_recovery_authorization(
            authorization,
            **{**fields, "target_ref": "wiki://03-Tech/foreign.md"},
        )


def test_restart_recovery_observes_target_without_reexecuting_effect(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    sealed = _record_decision(service, _decision_request())
    permit = MaterialActionCoordinator(service.store).authorize(
        sealed["outbox_ids"][0],
        executor_id="trusted_vault",
    )
    target = tmp_path / "already-committed-target.md"
    target.write_text("effect committed before receipt", encoding="utf-8")
    after_hash = sha256_json({"content": target.read_text(encoding="utf-8")})
    calls = 0

    class TargetOracle:
        owner = "trusted_vault"
        executor_id = "trusted_vault"
        action_type = "formal_write"

        def observe(self, observed_permit):
            nonlocal calls
            calls += 1
            assert observed_permit.effect_id == permit.effect_id
            return MaterialActionObservation(
                status="committed",
                before_hash=HASH_A,
                after_hash=after_hash,
                evidence_refs=(
                    f"target-after:{after_hash}",
                    f"target-oracle:test-restart:{target}:{after_hash}",
                ),
                outcome="observed existing target effect after restart",
                observed_at="2026-07-17T09:01:00+00:00",
            )

    restarted = MaterialActionCoordinator(
        CognitiveStateApplicationService(db_path).store
    )
    recovered = restarted.recover(
        permit.command_id,
        executor_id="trusted_vault",
        oracle=TargetOracle(),
    )
    replay = restarted.recover(
        permit.command_id,
        executor_id="trusted_vault",
        oracle=TargetOracle(),
    )

    assert recovered is not None
    assert recovered.status == "committed"
    assert recovered.after_hash == after_hash
    assert replay == recovered
    assert calls == 2
    assert restarted.state_store.pending_commands() == []


def test_recovery_rejects_an_oracle_for_another_target_family(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    sealed = _record_decision(service, _decision_request())
    permit = MaterialActionCoordinator(service.store).authorize(
        sealed["outbox_ids"][0],
        executor_id="trusted_vault",
    )

    class ForgedOracle:
        owner = "different-owner"
        executor_id = "trusted_vault"
        action_type = "formal_write"

        def observe(self, observed_permit):
            raise AssertionError("foreign oracle must not be consulted")

    with pytest.raises(PermissionError, match="oracle family"):
        MaterialActionCoordinator(service.store).recover(
            permit.command_id,
            executor_id="trusted_vault",
            oracle=ForgedOracle(),
        )


@pytest.mark.parametrize(
    ("status", "reason_code", "retry_exhausted", "extra_evidence"),
    [
        (
            "failed_terminal",
            "target_write_failed",
            False,
            ("attempted-effect", "target-oracle:unchanged"),
        ),
        (
            "rejected",
            "human_rejected",
            False,
            ("no-effect-oracle",),
        ),
        (
            "revoked",
            "approval_revoked",
            False,
            ("no-effect-oracle",),
        ),
        (
            "dead_letter",
            "retry_budget_exhausted",
            True,
            ("attempted-effect", "retry-budget-exhausted", "target-oracle:unchanged"),
        ),
        (
            "intentional_skip",
            "approved_not_applicable",
            False,
            ("approved-skip", "no-effect-oracle"),
        ),
    ],
)
def test_material_action_non_success_terminal_is_typed_and_proves_no_open_effect(
    tmp_path: Path,
    status: str,
    reason_code: str,
    retry_exhausted: bool,
    extra_evidence: tuple[str, ...],
) -> None:
    db_path = tmp_path / status / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    request = _decision_request()
    request["idempotency_key"] += f"-{status}"
    sealed = _record_decision(service, request)
    coordinator = MaterialActionCoordinator(service.store)
    permit = coordinator.authorize(
        sealed["outbox_ids"][0],
        executor_id="trusted_vault",
    )
    evidence = [
        f"material-command:{permit.command_id}",
        f"decision-revision:{permit.decision_revision_id}",
        f"material-effect:{permit.effect_id}",
    ]
    for ref in extra_evidence:
        if ref == "attempted-effect":
            evidence.append(f"attempted-effect:{permit.effect_id}")
        elif ref == "no-effect-oracle":
            evidence.append(f"no-effect-oracle:{permit.effect_id}:{HASH_A}")
        elif ref == "retry-budget-exhausted":
            evidence.append(f"retry-budget-exhausted:{permit.command_id}")
        elif ref == "approved-skip":
            evidence.append(f"approved-skip:{permit.decision_revision_id}")
        else:
            evidence.append(ref)

    receipt = coordinator.record_terminal(
        permit,
        MaterialActionTerminal(
            status=status,
            target_effect_id=permit.effect_id,
            before_hash=HASH_A,
            after_hash=HASH_A,
            evidence_refs=tuple(evidence),
            reason_code=reason_code,
            retry_exhausted=retry_exhausted,
            outcome=f"terminal:{status}",
            created_at="2026-07-17T09:02:00+00:00",
        ),
    )

    assert receipt.status == status
    assert receipt.reason_code == reason_code
    assert service.store.pending_commands() == []


def test_decision_idempotency_key_rejects_changed_semantics_without_partial_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    _record_decision(service, _decision_request())
    changed = _decision_request()
    changed["goal"] = "Silently reuse a changed decision."

    with pytest.raises(RuntimeError, match="idempotency key"):
        _record_decision(service, changed)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_heads").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM cognitive_data_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_outbox").fetchone()[0] == 1


@pytest.mark.parametrize(
    "boundary",
    [
        "after_head_preconditions",
        "after_value_context_revision",
        "after_cognitive_state_snapshot_revision",
        "after_decision_trace_revision",
        "after_event",
        "after_outbox",
    ],
)
def test_decision_seal_crash_boundaries_roll_back_every_canonical_row(
    tmp_path: Path,
    boundary: str,
) -> None:
    db_path = tmp_path / boundary / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)

    def failpoint(name: str) -> None:
        if name == boundary:
            raise OSError(f"crash:{boundary}")

    with pytest.raises(OSError, match=boundary):
        authority_catalog, _ = _decision_authorities()
        DecisionTraceStore(service.store).seal(
            _decision_request(),
            principal=_principal(),
            source_authority_catalog=authority_catalog,
            _failpoint=failpoint,
        )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_heads").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cognitive_data_events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_outbox").fetchone()[0] == 0


def test_each_outbox_insert_boundary_rolls_back_a_multi_action_decision(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    request = _decision_request()
    request["idempotency_key"] = "cog036-two-actions-crash-second-outbox"
    request["actions"].append(
        {
            "key": "write-secondary",
            "action_type": "formal_write",
            "owner": "trusted_vault",
            "executor": "trusted_vault_secondary",
            "target_ref": "wiki://03-Tech/COG-036-secondary.md",
            "input_hash": HASH_B,
            "rollback_contract": "restore exact secondary before hash",
            "expected_effect": "secondary target hash equals input hash",
        }
    )
    outbox_count = 0

    def failpoint(name: str) -> None:
        nonlocal outbox_count
        if name == "after_outbox":
            outbox_count += 1
            if outbox_count == 2:
                raise OSError("crash:second-outbox")

    authority_catalog, _ = _decision_authorities()
    with pytest.raises(OSError, match="second-outbox"):
        DecisionTraceStore(service.store).seal(
            request,
            principal=_principal(),
            source_authority_catalog=authority_catalog,
            _failpoint=failpoint,
        )

    assert outbox_count == 2
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_heads").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cognitive_data_events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_outbox").fetchone()[0] == 0


def test_value_source_revision_must_match_the_system_authority_catalog(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    request = _decision_request()
    request["values"][0]["source_revision_id"] = "forged-unresolved-revision"

    with pytest.raises(ValueError, match="source revision"):
        _record_decision(service, request)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0] == 0


def test_scoped_preference_cannot_override_the_current_explicit_user_goal(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    request = _decision_request()
    _, authorities = _decision_authorities()
    user_authority = authorities[SourceAuthority.EXPLICIT_USER.value]
    request["values"][1]["conflicts_with_keys"] = ["preference"]
    request["values"].append(
        {
            "key": "preference",
            "category": "scoped_preference",
            "constraint": "Prefer the legacy caller-authored payload shape.",
            "source_authority_id": user_authority.source_authority_id,
            "source_id": user_authority.source_event_id,
            "source_revision_id": user_authority.source_event_id,
            "source_content_hash": user_authority.content_sha256,
            "evidence_refs": [
                "raw-event-cog036-1#0:64",
                user_authority.source_authority_id,
            ],
            "valid_from": "2026-07-17T09:00:00+00:00",
            "valid_until": "",
            "changed_decision": True,
            "conflicts_with_keys": ["goal"],
        }
    )
    request["candidates"][0]["violated_value_keys"] = []
    request["candidates"][0]["satisfies_value_keys"] = ["preference"]
    request["candidates"][1]["satisfies_value_keys"] = ["goal"]
    request["selection_key"] = "legacy"
    request["rejections"] = [
        {
            "candidate_key": "strict",
            "reason_code": "caller_preferred_legacy_shape",
            "evidence_refs": ["raw-event-cog036-1#0:64"],
        }
    ]

    with pytest.raises(ValueError, match="lower-precedence value"):
        _record_decision(service, request)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cognitive_data_events").fetchone()[0] == 0


def test_human_rejected_decision_is_terminal_without_an_executable_action(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    request = _decision_request()
    request["idempotency_key"] = "cog036-human-reject"
    request["approval"] = {
        "mode": "explicit_user",
        "decision": "rejected",
        "evidence_ref": "raw-event-cog036-1#0:64",
        "created_at": "2026-07-17T09:00:00+00:00",
    }
    request["actions"] = []

    first = _record_decision(service, request)
    replay = _record_decision(service, request)

    assert first["decision"]["payload"]["decision_state"] == "rejected"
    assert first["decision"]["payload"]["action_refs"] == []
    assert first["decision"]["payload"]["effect_refs"] == []
    assert first["outbox_ids"] == []
    assert replay["status"] == "existing"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT intended_consumers FROM cognitive_data_events"
        ).fetchone()
    assert json.loads(row[0]) == []


def test_exact_concurrent_decision_replay_commits_one_canonical_trace(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)

    def seal(_: int) -> str:
        return _record_decision(
            CognitiveStateApplicationService(db_path),
            _decision_request(),
        )["status"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(seal, range(8)))

    assert set(statuses) <= {"committed", "existing"}
    assert "committed" in statuses
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM cognitive_data_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_outbox").fetchone()[0] == 1


def test_decision_bundle_can_be_authorized_and_recomputed_from_readable_payloads(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    sealed = _record_decision(service, _decision_request())

    verified = DecisionTraceStore(service.store).verify(
        sealed["decision"]["revision_id"],
        principal=_principal(),
    )

    assert verified.status == "verified"
    assert verified.decision_revision_id == sealed["decision"]["revision_id"]
    assert verified.snapshot_revision_id == sealed["snapshot"]["revision_id"]
    assert verified.value_context_revision_id == sealed["value_context"]["revision_id"]
    assert verified.action_ids == tuple(sealed["decision"]["payload"]["action_refs"])
    assert verified.effect_ids == tuple(sealed["decision"]["payload"]["effect_refs"])
    assert verified.bundle_hash.startswith("sha256:")


@pytest.mark.parametrize("damage", ["tamper_snapshot", "delete_snapshot", "delete_value"])
def test_decision_verifier_fails_on_missing_or_tampered_canonical_state(
    tmp_path: Path,
    damage: str,
) -> None:
    db_path = tmp_path / damage / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    sealed = _record_decision(service, _decision_request())
    with sqlite3.connect(db_path) as conn:
        if damage == "tamper_snapshot":
            conn.execute("DROP TRIGGER cognitive_state_revisions_no_update")
            payload = dict(sealed["snapshot"]["payload"])
            payload["task"] = "tampered after commit"
            conn.execute(
                "UPDATE cognitive_state_revisions SET payload_json=? WHERE revision_id=?",
                (json.dumps(payload, sort_keys=True), sealed["snapshot"]["revision_id"]),
            )
            conn.execute(
                """
                CREATE TRIGGER cognitive_state_revisions_no_update
                BEFORE UPDATE ON cognitive_state_revisions BEGIN
                    SELECT RAISE(ABORT, 'cognitive_state_revisions are immutable');
                END
                """
            )
        else:
            conn.execute("DROP TRIGGER cognitive_state_revisions_no_delete")
            revision_id = (
                sealed["snapshot"]["revision_id"]
                if damage == "delete_snapshot"
                else sealed["value_context"]["revision_id"]
            )
            conn.execute(
                "DELETE FROM cognitive_state_revisions WHERE revision_id=?",
                (revision_id,),
            )
            conn.execute(
                """
                CREATE TRIGGER cognitive_state_revisions_no_delete
                BEFORE DELETE ON cognitive_state_revisions BEGIN
                    SELECT RAISE(ABORT, 'cognitive_state_revisions are immutable');
                END
                """
            )

    with pytest.raises(RuntimeError, match="unavailable|hash mismatch"):
        DecisionTraceStore(service.store).verify(
            sealed["decision"]["revision_id"],
            principal=_principal(),
        )


def test_concurrent_changed_semantics_cannot_share_a_decision_idempotency_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(db_path)
    barrier = Barrier(2)
    original_commit = CognitiveStateUnitOfWork.commit

    def synchronized_commit(self, **kwargs):
        barrier.wait(timeout=5)
        return original_commit(self, **kwargs)

    monkeypatch.setattr(CognitiveStateUnitOfWork, "commit", synchronized_commit)

    def seal(goal: str) -> str:
        request = _decision_request()
        request["goal"] = goal
        try:
            _record_decision(
                CognitiveStateApplicationService(db_path),
                request,
            )
        except RuntimeError:
            return "conflict"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(seal, ("goal-a", "goal-b")))

    assert sorted(statuses) == ["committed", "conflict"]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM cognitive_data_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM cognitive_state_outbox").fetchone()[0] == 1
