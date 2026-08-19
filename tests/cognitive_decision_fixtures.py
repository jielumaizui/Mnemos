"""Shared canonical DecisionTrace fixtures for material-sink tests."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4
from contextlib import contextmanager
import hashlib
from typing import Any

from core.access_policy import PrincipalEnvelope
from core.application.cognitive_state import CognitiveStateApplicationService
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionCoordinator,
    MaterialActionRequest,
    material_action_resolution_scope,
)
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.evidence.source_authority import SourceAuthority, SourceAuthorityCatalog


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
TEST_PROJECT_CONTRACT_TEXT = (
    "Every material test effect must match its approved target and input."
)
TEST_USER_GOAL_TEXT = "Execute the selected exact material test effect."


def _text_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def decision_authority_catalog(
    *,
    source_id: str,
    source_revision_id: str | None = None,
    user_text: str = TEST_USER_GOAL_TEXT,
    project_revision_id: str = "audit:2026-07-12",
    project_text: str = TEST_PROJECT_CONTRACT_TEXT,
) -> tuple[SourceAuthorityCatalog, dict[str, Any]]:
    """Build exact system-owned authority refs for DecisionTrace tests."""

    project_hash = _text_hash(project_text)
    user_hash = _text_hash(user_text)
    catalog = SourceAuthorityCatalog.from_messages(
        (
            {
                "role": "system",
                "content": project_text,
                "source_authority": SourceAuthority.PROJECT_CONTRACT.value,
                "source_span": {
                    "revision_id": project_revision_id,
                    "role": "system",
                    "span_start": 0,
                    "span_end": len(project_text),
                    "content_hash": project_hash,
                },
            },
            {
                "role": "user",
                "content": user_text,
                "source_span": {
                    "revision_id": source_revision_id or source_id,
                    "role": "user",
                    "span_start": 0,
                    "span_end": len(user_text),
                    "content_hash": user_hash,
                },
            },
        ),
        allowed_source_event_ids=(
            project_revision_id,
            source_revision_id or source_id,
        ),
    )
    catalog.require_admissible()
    entries = {entry.authority.value: entry for entry in catalog.entries}
    return catalog, entries


def material_action_authorization(
    database_dir: Path,
    *,
    action_type: str,
    owner: str,
    executor: str,
    target_ref: str,
    input_hash: str,
    source_object: dict[str, str] | None = None,
    nonce: str = "",
) -> MaterialActionAuthorization:
    """Seal one real canonical decision and return its bound test capability."""

    database_dir = Path(database_dir)
    db_path = database_dir / "producer_consumer_ledger.db"
    if not db_path.is_file():
        initialize_cognitive_state_schema(db_path)
    service = CognitiveStateApplicationService(db_path)
    token = nonce or uuid4().hex
    source_id = f"raw-event-material-sink-{token}"
    source_revision_id = f"raw-revision-material-sink-{token}"
    principal = PrincipalEnvelope(
        principal_id="mcp:codex:material-sink-test",
        agent="codex",
        host_kind="test",
        capability_id="material-sink-test",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )
    source_access = make_cognitive_access_envelope(
        owner_principal_id=principal.principal_id,
        owner_agent=principal.agent,
        scope_type="project",
        scope_id="mnemos",
        project="mnemos",
        purposes=("cognitive_state_read", "cognitive_state_write"),
        consent_provenance_refs=(source_id,),
        sensitivity="sensitive",
        retention_policy="test_retention",
        source_acl_lineage=(HASH_B,),
    )
    authority_catalog, authorities = decision_authority_catalog(
        source_id=source_id,
        source_revision_id=source_revision_id,
    )
    project_authority = authorities[SourceAuthority.PROJECT_CONTRACT.value]
    user_authority = authorities[SourceAuthority.EXPLICIT_USER.value]
    request = {
        "idempotency_key": f"material-sink-test-{token}",
        "source": {
            "source_id": source_id,
            "source_revision_id": source_revision_id,
            "source_kind": "explicit_user_decision",
            "source_uri": f"raw://material-sink/{token}",
            "content_hash": HASH_A,
            "evidence_refs": [f"{source_id}#0:64"],
            "created_at": "2026-07-17T09:00:00+00:00",
            "privacy_level": "private",
            "access_control": source_access,
        },
        "scope": {"type": "project", "id": "mnemos"},
        "task": f"Execute {action_type}",
        "goal": "Execute one exact material effect only after canonical approval.",
        "constraints": ["fail closed without an exact material permit"],
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
                    f"{source_id}#0:64",
                    user_authority.source_authority_id,
                ],
                "valid_from": "2026-07-17T09:00:00+00:00",
                "valid_until": "",
                "changed_decision": True,
            },
        ],
        "candidates": [
            {
                "key": "execute",
                "summary": "Execute the exact approved material effect.",
                "supporting_evidence": [f"{source_id}#0:64"],
                "opposing_evidence": [],
                "violated_value_keys": [],
                "satisfies_value_keys": ["safety", "goal"],
            },
            {
                "key": "skip",
                "summary": "Do not execute the approved material effect.",
                "supporting_evidence": ["audit:COG-036:value-contract"],
                "opposing_evidence": [f"{source_id}#0:64"],
                "violated_value_keys": [],
                "satisfies_value_keys": ["safety"],
            },
        ],
        "selection_key": "execute",
        "rejections": [
            {
                "candidate_key": "skip",
                "reason_code": "explicit_approved_effect_selected",
                "evidence_refs": [f"{source_id}#0:64"],
            }
        ],
        "model_spec": {
            "provider": "system",
            "model": "deterministic-test-rule",
            "route": "local",
            "version": "mnemos.material_sink_test.v1",
            "config_hash": HASH_C,
        },
        "tool_specs": [
            {
                "name": executor,
                "version": "mnemos.material_sink_test.v1",
                "code_hash": HASH_B,
            }
        ],
        "prompt_spec": {
            "prompt_id": "none:deterministic-test-rule",
            "prompt_hash": HASH_C,
            "schema_hash": HASH_B,
        },
        "expected_outcomes": [
            {"metric": "exact_effect_committed", "operator": "equals", "value": 1}
        ],
        "evaluation_window": {
            "starts_at": "2026-07-17T09:00:00+00:00",
            "ends_at": "2026-07-18T09:00:00+00:00",
        },
        "approval": {
            "mode": "explicit_user",
            "decision": "approved",
            "evidence_ref": f"{source_id}#0:64",
            "created_at": "2026-07-17T09:00:00+00:00",
        },
        "actions": [
            {
                "key": "execute",
                "action_type": action_type,
                "owner": owner,
                "executor": executor,
                "target_ref": target_ref,
                "input_hash": input_hash,
                "rollback_contract": "restore the exact before hash",
                "expected_effect": "the exact target reaches the approved input hash",
                **(
                    {"source_object": dict(source_object)}
                    if source_object is not None
                    else {}
                ),
            }
        ],
    }
    sealed = service.record_decision(
        request,
        principal=principal,
        source_authority_catalog=authority_catalog,
    )
    return MaterialActionCoordinator(service.store).bind(
        sealed["outbox_ids"][0],
        executor_id=executor,
    )


def material_action_resolver(
    database_dir: Path,
    *,
    action_type: str,
    owner: str,
    executor: str,
):
    """Resolve any exact sink binding through a fresh canonical test decision."""

    def _resolve(binding):
        resolved_dir = (
            Path(str(binding["expected_state_db"])).parent
            if str(binding.get("expected_state_db") or "").strip()
            else Path(database_dir)
        )
        return material_action_authorization(
            resolved_dir,
            action_type=action_type,
            owner=owner,
            executor=executor,
            target_ref=str(binding["target_ref"]),
            input_hash=str(binding["input_hash"]),
        )

    return _resolve


@contextmanager
def canonical_material_action_scope(database_dir: Path | None = None):
    """Supply real, exactly bound DecisionTrace commands to a nested test flow."""

    def _resolve(request: MaterialActionRequest) -> MaterialActionAuthorization:
        resolved_dir = (
            Path(request.expected_state_db).parent
            if request.expected_state_db
            else Path(database_dir) if database_dir is not None
            else None
        )
        if resolved_dir is None:
            raise PermissionError(
                "test material-action scope requires a canonical state database"
            )
        return material_action_authorization(
            resolved_dir,
            action_type=request.action_type,
            owner=request.owner,
            executor=request.executor_id,
            target_ref=request.target_ref,
            input_hash=request.input_hash,
        )

    with material_action_resolution_scope(_resolve):
        yield


def delivery_action_authorization(
    database_dir: Path,
    **route_kwargs,
) -> MaterialActionAuthorization:
    """Build the canonical capability for one exact delivery-router request."""

    from core.cognitive.delivery_router import (
        DELIVERY_MATERIAL_ACTION_TYPE,
        DELIVERY_MATERIAL_EXECUTOR,
        DELIVERY_MATERIAL_OWNER,
        delivery_material_action_binding,
    )

    binding = delivery_material_action_binding(
        **{key: value for key, value in route_kwargs.items() if key != "principal"}
    )
    return material_action_authorization(
        database_dir,
        action_type=DELIVERY_MATERIAL_ACTION_TYPE,
        owner=DELIVERY_MATERIAL_OWNER,
        executor=DELIVERY_MATERIAL_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
    )


def predictive_route_access(
    principal: PrincipalEnvelope,
    *,
    subject: str,
    session_id: str,
    project: str,
) -> dict[str, Any]:
    """Build an exact source ACL for a predictive-route test source."""

    normalized_subject = str(subject or "").strip().lower()
    return make_cognitive_access_envelope(
        owner_principal_id=principal.principal_id,
        owner_agent=principal.agent,
        scope_type="topic",
        scope_id=normalized_subject,
        session_id=session_id,
        project=project,
        purposes=(
            "cognitive_state_read",
            "cognitive_state_write",
            "prediction_read",
        ),
        consent_provenance_refs=(f"wiki:{normalized_subject}",),
        sensitivity="sensitive",
        retention_policy="prediction_source",
        source_acl_lineage=(_text_hash(f"wiki:{normalized_subject}"),),
    )


def trusted_markdown_action_authorization(
    database_dir: Path,
    **mutation_kwargs,
) -> MaterialActionAuthorization:
    """Build the canonical capability for one exact trusted Markdown effect."""

    from core.trust.vault_mutation_service import (
        TRUSTED_MARKDOWN_ACTION_TYPE,
        TRUSTED_MARKDOWN_EXECUTOR,
        TRUSTED_MARKDOWN_OWNER,
        trusted_markdown_material_action_binding,
    )

    binding = trusted_markdown_material_action_binding(**mutation_kwargs)
    return material_action_authorization(
        database_dir,
        action_type=TRUSTED_MARKDOWN_ACTION_TYPE,
        owner=TRUSTED_MARKDOWN_OWNER,
        executor=TRUSTED_MARKDOWN_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
    )


def knowledge_vault_action_authorization(
    database_dir: Path,
    *,
    proposal_id: str,
    target_uri: str,
    content: str,
    expected_existing_hash: str | None = None,
) -> MaterialActionAuthorization:
    """Build the canonical capability for one approved vault proposal."""

    from core.trust.knowledge_vault_writer import (
        KNOWLEDGE_VAULT_ACTION_TYPE,
        KNOWLEDGE_VAULT_EXECUTOR,
        KNOWLEDGE_VAULT_OWNER,
        knowledge_vault_material_action_binding,
    )

    binding = knowledge_vault_material_action_binding(
        proposal_id=proposal_id,
        target_uri=target_uri,
        content=content,
        expected_existing_hash=expected_existing_hash,
    )
    return material_action_authorization(
        database_dir,
        action_type=KNOWLEDGE_VAULT_ACTION_TYPE,
        owner=KNOWLEDGE_VAULT_OWNER,
        executor=KNOWLEDGE_VAULT_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
    )


def formal_mutation_action_authorization(
    database_dir: Path,
    *,
    asset_kind: str,
    action: str,
    target_ref: str,
    actor: str,
    reason: str = "",
    metadata: dict | None = None,
    owner: str,
    executor: str,
) -> MaterialActionAuthorization:
    """Build a capability for one formal non-Markdown mutation journal row."""

    from core.trust.formal_cognitive_mutation import (
        formal_cognitive_mutation_input_hash,
    )

    input_hash = formal_cognitive_mutation_input_hash(
        asset_kind=asset_kind,
        action=action,
        target_ref=target_ref,
        actor=actor,
        reason=reason,
        metadata=metadata,
    )
    return material_action_authorization(
        database_dir,
        action_type=action,
        owner=owner,
        executor=executor,
        target_ref=target_ref,
        input_hash=input_hash,
    )


def policy_patch_proposal_authorization(
    database_dir: Path,
    *,
    lesson: dict,
    options,
) -> MaterialActionAuthorization:
    """Build the canonical capability for one eligible policy patch proposal."""

    from core.cognitive.policy_patch import (
        POLICY_PATCH_EXECUTOR,
        POLICY_PATCH_OWNER,
        POLICY_PATCH_PROPOSE_ACTION,
        policy_patch_proposal_binding,
    )

    binding = policy_patch_proposal_binding(lesson, options)
    if binding is None:
        raise ValueError("test lesson does not produce an eligible policy patch")
    return material_action_authorization(
        database_dir,
        action_type=POLICY_PATCH_PROPOSE_ACTION,
        owner=POLICY_PATCH_OWNER,
        executor=POLICY_PATCH_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
    )


def policy_patch_feedback_authorization(
    database_dir: Path,
    *,
    patch_id: str,
    outcome: str,
    evidence: dict | None = None,
    source_event_id: str = "",
) -> MaterialActionAuthorization:
    from core.cognitive.policy_patch import (
        POLICY_PATCH_EXECUTOR,
        POLICY_PATCH_FEEDBACK_ACTION,
        POLICY_PATCH_OWNER,
        policy_patch_feedback_binding,
    )

    binding = policy_patch_feedback_binding(
        patch_id=patch_id,
        outcome=outcome,
        evidence=evidence,
        source_event_id=source_event_id,
    )
    return material_action_authorization(
        database_dir,
        action_type=POLICY_PATCH_FEEDBACK_ACTION,
        owner=POLICY_PATCH_OWNER,
        executor=POLICY_PATCH_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
    )


def policy_patch_reconcile_authorization(
    database_dir: Path,
    *,
    changes: list[dict],
) -> MaterialActionAuthorization:
    from core.cognitive.policy_patch import (
        POLICY_PATCH_EXECUTOR,
        POLICY_PATCH_OWNER,
        POLICY_PATCH_RECONCILE_ACTION,
        policy_patch_reconcile_binding,
    )

    binding = policy_patch_reconcile_binding(changes)
    return material_action_authorization(
        database_dir,
        action_type=POLICY_PATCH_RECONCILE_ACTION,
        owner=POLICY_PATCH_OWNER,
        executor=POLICY_PATCH_EXECUTOR,
        target_ref=binding["target_ref"],
        input_hash=binding["input_hash"],
    )
