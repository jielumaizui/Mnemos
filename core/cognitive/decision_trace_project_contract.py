"""Exact project-contract decision resolvers and material-action guards."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from core.access_policy import PrincipalEnvelope
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.state_contract import sha256_json
from core.cognitive.state_store import CognitiveStateConflict, CognitiveStateStore
from core.cognitive.prediction_ledger import PredictionPlan
from core.evidence.source_authority import SourceAuthority, SourceAuthorityCatalog

from core.cognitive.decision_trace_contracts import (
    MATERIAL_ACTION_COMMAND_TYPE,
    DecisionCandidateEvaluation,
    DecisionRejectionEvaluation,
    DecisionSealReceipt,
    MaterialActionPermit,
    MaterialActionRequest,
    ProjectContractDecisionContext,
    ProjectContractDecisionEvaluation,
    _digest,
    _mapping_sequence,
    _required,
    _required_dead_letter_supersessions,
    _sha256,
    _strings,
    _text_hash,
    _timestamp,
)
from core.cognitive.decision_trace_material import (
    MaterialActionAuthorization,
    MaterialActionCoordinator,
    _validate_material_effect_fields,
)
from core.cognitive.decision_trace_store import DecisionTraceStore


def material_action_request_identity(
    request: MaterialActionRequest,
) -> dict[str, str]:
    """Return the canonical identity a domain evaluator must compare."""

    if not isinstance(request, MaterialActionRequest):
        raise TypeError("MaterialActionRequest is required")
    return {
        "owner": _required(request.owner, "request.owner"),
        "executor_id": _required(request.executor_id, "request.executor_id"),
        "action_type": _required(request.action_type, "request.action_type"),
        "target_ref": _required(request.target_ref, "request.target_ref"),
        "input_hash": _sha256(request.input_hash, "request.input_hash"),
        "expected_state_db": (
            str(Path(request.expected_state_db).expanduser().resolve(strict=False))
            if str(request.expected_state_db or "").strip()
            else ""
        ),
    }


def build_exact_project_contract_evaluator(
    *,
    expected_request: MaterialActionRequest,
    source_facts: Mapping[str, Any],
    decision_checks: Mapping[str, bool],
    approved_candidate_key: str,
    approved_candidate_summary: str,
    rejected_candidate_key: str,
    rejected_candidate_summary: str,
    approved_reason_code: str,
    rejected_reason_code: str,
    committed_metric: str,
    rejected_metric: str,
) -> tuple[
    str,
    Callable[[MaterialActionRequest], ProjectContractDecisionEvaluation],
]:
    """Build a one-binding evaluator without granting a family-wide capability.

    The expected effect identity is folded into the immutable source-facts
    hash.  The returned evaluator approves only that exact identity; a caller
    cannot use it to authorize a second target, body, executor, or store.
    Candidate names and outcome metrics remain domain-owned so this helper
    cannot manufacture generic ``execute``/``skip`` reasoning.
    """

    expected_identity = material_action_request_identity(expected_request)
    if not isinstance(source_facts, Mapping) or not source_facts:
        raise ValueError("exact project-contract source facts are required")
    if not isinstance(decision_checks, Mapping) or not decision_checks:
        raise ValueError("domain-owned project-contract decision checks are required")
    normalized_checks: dict[str, bool] = {}
    for raw_key, raw_value in sorted(decision_checks.items()):
        key = _required(raw_key, "decision_checks.key")
        if key in normalized_checks:
            raise ValueError("project-contract decision check keys must be unique")
        if type(raw_value) is not bool:
            raise TypeError("project-contract decision checks must be booleans")
        normalized_checks[key] = raw_value
    source_facts_hash = sha256_json(
        {
            "schema_version": "mnemos.exact_project_contract_facts.v1",
            "source_facts": dict(source_facts),
            "expected_request": expected_identity,
            "decision_checks": normalized_checks,
        }
    )
    approved_key = _required(
        approved_candidate_key,
        "approved_candidate_key",
    )
    rejected_key = _required(
        rejected_candidate_key,
        "rejected_candidate_key",
    )
    if approved_key == rejected_key:
        raise ValueError("project-contract candidate keys must be distinct")
    approved_summary = _required(
        approved_candidate_summary,
        "approved_candidate_summary",
    )
    rejected_summary = _required(
        rejected_candidate_summary,
        "rejected_candidate_summary",
    )
    approved_reason = _required(approved_reason_code, "approved_reason_code")
    rejected_reason = _required(rejected_reason_code, "rejected_reason_code")
    committed_metric_id = _required(committed_metric, "committed_metric")
    rejected_metric_id = _required(rejected_metric, "rejected_metric")

    def evaluate(
        request: MaterialActionRequest,
    ) -> ProjectContractDecisionEvaluation:
        """Evaluate one request against the frozen exact-contract facts."""

        actual_identity = material_action_request_identity(request)
        request_hash = sha256_json(
            {key: value for key, value in actual_identity.items() if key != "expected_state_db"}
        )
        request_matches = actual_identity == expected_identity
        failed_checks = tuple(key for key, satisfied in normalized_checks.items() if not satisfied)
        approved = request_matches and not failed_checks
        request_ref = f"request-binding:{request_hash}"
        facts_ref = f"source-facts:{source_facts_hash}"
        check_refs = tuple(
            f"decision-check:{key}:{'satisfied' if satisfied else 'failed'}"
            for key, satisfied in normalized_checks.items()
        )
        common_refs = (request_ref, facts_ref, *check_refs)
        failed_refs = tuple(ref for ref in common_refs if ref.endswith(":failed"))
        if not request_matches:
            failed_refs = (*failed_refs, "decision-check:request-binding:failed")
        rejection_refs = tuple(dict.fromkeys((*common_refs, *failed_refs)))
        return ProjectContractDecisionEvaluation(
            request_binding_hash=request_hash,
            source_facts_hash=source_facts_hash,
            candidates=(
                DecisionCandidateEvaluation(
                    key=approved_key,
                    summary=approved_summary,
                    supporting_evidence=common_refs if approved else (),
                    opposing_evidence=() if approved else rejection_refs,
                    violated_value_keys=(() if approved else ("safety",)),
                    satisfies_value_keys=(("safety", "project_contract") if approved else ()),
                ),
                DecisionCandidateEvaluation(
                    key=rejected_key,
                    summary=rejected_summary,
                    supporting_evidence=common_refs if not approved else (),
                    opposing_evidence=() if not approved else common_refs,
                    satisfies_value_keys=("safety",),
                ),
            ),
            selection_key=approved_key if approved else rejected_key,
            rejections=(
                DecisionRejectionEvaluation(
                    candidate_key=rejected_key if approved else approved_key,
                    reason_code=approved_reason if approved else rejected_reason,
                    evidence_refs=common_refs,
                ),
            ),
            expected_outcomes=(
                {
                    "metric": (committed_metric_id if approved else rejected_metric_id),
                    "operator": "equals",
                    "value": 1 if approved else 0,
                },
            ),
            approval_decision="approved" if approved else "rejected",
            approval_evidence_ref=facts_ref,
        )

    return source_facts_hash, evaluate


def authorize_exact_project_contract_action(
    *,
    expected_request: MaterialActionRequest,
    state_db_path: Path,
    contract_id: str,
    contract_revision_id: str,
    contract_text: str,
    source_namespace: str,
    source_facts: Mapping[str, Any],
    decision_checks: Mapping[str, bool],
    evidence_refs: tuple[str, ...],
    task: str,
    goal: str,
    constraints: tuple[str, ...],
    created_at: str,
    producer: str,
    producer_version: str,
    producer_code_hash: str,
    evaluator_id: str,
    approved_candidate_key: str,
    approved_candidate_summary: str,
    rejected_candidate_key: str,
    rejected_candidate_summary: str,
    approved_reason_code: str,
    rejected_reason_code: str,
    committed_metric: str,
    rejected_metric: str,
    source_uri: str = "",
    prediction_plan: PredictionPlan | None = None,
    prediction_config: Any | None = None,
) -> MaterialActionAuthorization:
    """Seal one domain-owned exact project-contract decision.

    This helper owns only canonical identity plumbing.  Callers must still
    supply the domain contract, immutable facts, real candidate names,
    rejection semantics, and outcome metrics; it cannot manufacture a generic
    authorization policy for an action family.
    """

    namespace = _required(source_namespace, "source_namespace")
    facts_hash, evaluator = build_exact_project_contract_evaluator(
        expected_request=expected_request,
        source_facts=source_facts,
        decision_checks=decision_checks,
        approved_candidate_key=approved_candidate_key,
        approved_candidate_summary=approved_candidate_summary,
        rejected_candidate_key=rejected_candidate_key,
        rejected_candidate_summary=rejected_candidate_summary,
        approved_reason_code=approved_reason_code,
        rejected_reason_code=rejected_reason_code,
        committed_metric=committed_metric,
        rejected_metric=rejected_metric,
    )
    digest = facts_hash.split(":", 1)[1]
    resolver = ProjectContractMaterialActionResolver(
        ProjectContractDecisionContext(
            state_db_path=Path(state_db_path),
            contract_id=contract_id,
            contract_revision_id=contract_revision_id,
            contract_text=contract_text,
            contract_evidence_ref=f"{contract_id}#{contract_revision_id}",
            source_id=f"{namespace}:{digest[:40]}",
            source_revision_id=f"{namespace}:{digest}",
            source_content_hash=facts_hash,
            source_uri=(source_uri or f"{namespace}://{digest[:40]}"),
            evidence_refs=evidence_refs,
            task=task,
            goal=goal,
            constraints=constraints,
            created_at=created_at,
            scope_prefix=namespace,
            producer=producer,
            producer_version=producer_version,
            producer_code_hash=producer_code_hash,
            evaluator_id=evaluator_id,
            evaluator=evaluator,
            prediction_plan=prediction_plan,
            prediction_config=prediction_config,
        )
    )
    return resolver(expected_request)


def _project_contract_catalog(
    *,
    contract_revision_id: str,
    contract_text: str,
    contract_hash: str,
) -> tuple[SourceAuthorityCatalog, str]:
    catalog = SourceAuthorityCatalog.from_messages(
        (
            {
                "role": "system",
                "content": contract_text,
                "source_authority": SourceAuthority.PROJECT_CONTRACT.value,
                "source_span": {
                    "revision_id": contract_revision_id,
                    "role": "system",
                    "span_start": 0,
                    "span_end": len(contract_text),
                    "content_hash": contract_hash,
                },
            },
        ),
        allowed_source_event_ids=(contract_revision_id,),
    )
    catalog.require_admissible()
    if len(catalog.entries) != 1:
        raise ValueError("project contract must be one exact unquoted authoritative span")
    entry = catalog.entries[0]
    if (
        entry.authority != SourceAuthority.PROJECT_CONTRACT
        or entry.source_event_id != contract_revision_id
        or entry.content_sha256 != contract_hash
        or entry.source_revision_sha256 != contract_hash
        or entry.span_start != 0
        or entry.span_end != len(contract_text)
        or entry.span_status != "exact"
    ):
        raise ValueError("project contract authority catalog binding is invalid")
    return catalog, entry.source_authority_id


def _normalize_project_contract_evaluation(
    evaluation: ProjectContractDecisionEvaluation,
    *,
    request_binding_hash: str,
) -> dict[str, Any]:
    if (
        _sha256(
            evaluation.request_binding_hash,
            "evaluation.request_binding_hash",
        )
        != request_binding_hash
    ):
        raise PermissionError("project-contract evaluation is bound to another request")
    source_facts_hash = _sha256(
        evaluation.source_facts_hash,
        "evaluation.source_facts_hash",
    )
    request_ref = f"request-binding:{request_binding_hash}"
    facts_ref = f"source-facts:{source_facts_hash}"
    candidates: list[dict[str, Any]] = []
    keys: set[str] = set()
    for candidate in evaluation.candidates:
        if not isinstance(candidate, DecisionCandidateEvaluation):
            raise TypeError("evaluation candidates must be typed")
        key = _required(candidate.key, "evaluation.candidate.key")
        if key in keys:
            raise ValueError("evaluation candidate keys must be unique")
        if key in {"execute_exact_effect", "skip_exact_effect"}:
            raise ValueError("generic template candidates are forbidden")
        keys.add(key)
        supporting = _strings(
            candidate.supporting_evidence,
            "evaluation.candidate.supporting_evidence",
        )
        opposing = _strings(
            candidate.opposing_evidence,
            "evaluation.candidate.opposing_evidence",
        )
        if not {request_ref, facts_ref}.issubset(set(supporting) | set(opposing)):
            raise ValueError("each evaluated candidate must bind the request and source facts")
        candidates.append(
            {
                "key": key,
                "summary": _required(
                    candidate.summary,
                    "evaluation.candidate.summary",
                ),
                "supporting_evidence": list(supporting),
                "opposing_evidence": list(opposing),
                "violated_value_keys": list(
                    _strings(
                        candidate.violated_value_keys,
                        "evaluation.candidate.violated_value_keys",
                    )
                ),
                "satisfies_value_keys": list(
                    _strings(
                        candidate.satisfies_value_keys,
                        "evaluation.candidate.satisfies_value_keys",
                    )
                ),
            }
        )
    if len(candidates) < 2:
        raise ValueError("project-contract evaluation requires two real candidates")
    selection_key = _required(evaluation.selection_key, "evaluation.selection_key")
    if selection_key not in keys:
        raise ValueError("project-contract evaluation selection is unavailable")
    rejections: list[dict[str, Any]] = []
    rejected_keys: set[str] = set()
    for rejection in evaluation.rejections:
        if not isinstance(rejection, DecisionRejectionEvaluation):
            raise TypeError("evaluation rejections must be typed")
        candidate_key = _required(
            rejection.candidate_key,
            "evaluation.rejection.candidate_key",
        )
        if candidate_key in rejected_keys:
            raise ValueError("evaluation rejection keys must be unique")
        rejected_keys.add(candidate_key)
        refs = _strings(
            rejection.evidence_refs,
            "evaluation.rejection.evidence_refs",
            non_empty=True,
        )
        if not {request_ref, facts_ref}.issubset(refs):
            raise ValueError("evaluation rejection must bind request and source facts")
        rejections.append(
            {
                "candidate_key": candidate_key,
                "reason_code": _required(
                    rejection.reason_code,
                    "evaluation.rejection.reason_code",
                ),
                "evidence_refs": list(refs),
            }
        )
    if rejected_keys != keys - {selection_key}:
        raise ValueError("evaluation must reject every non-selected candidate")
    approval_decision = _required(
        evaluation.approval_decision,
        "evaluation.approval_decision",
    )
    if approval_decision not in {"approved", "rejected"}:
        raise ValueError("evaluation approval must be approved or rejected")
    approval_evidence_ref = _required(
        evaluation.approval_evidence_ref,
        "evaluation.approval_evidence_ref",
    )
    selected = next(row for row in candidates if row["key"] == selection_key)
    if approval_evidence_ref not in set(selected["supporting_evidence"]):
        raise ValueError("evaluation approval evidence must support its selection")
    expected_outcomes = _mapping_sequence(
        evaluation.expected_outcomes,
        "evaluation.expected_outcomes",
        non_empty=True,
    )
    return {
        "request_binding_hash": request_binding_hash,
        "source_facts_hash": source_facts_hash,
        "candidates": tuple(candidates),
        "selection_key": selection_key,
        "rejections": tuple(rejections),
        "expected_outcomes": expected_outcomes,
        "approval_decision": approval_decision,
        "approval_evidence_ref": approval_evidence_ref,
    }


class ProjectContractMaterialActionResolver:
    """Seal an upstream evaluator's exact project-contract decision.

    The resolver owns identity, authority resolution, and atomic persistence;
    it does not invent candidates or choose a result.  A code-owned domain
    evaluator must compare the request against current authoritative facts.
    """

    def __init__(self, context: ProjectContractDecisionContext):
        if not isinstance(context, ProjectContractDecisionContext):
            raise TypeError("ProjectContractDecisionContext is required")
        self.context = context

    def __call__(self, request: MaterialActionRequest) -> MaterialActionAuthorization:
        if not isinstance(request, MaterialActionRequest):
            raise TypeError("MaterialActionRequest is required")
        context = self.context
        state_db = Path(context.state_db_path).expanduser().resolve(strict=False)
        if request.expected_state_db:
            requested_db = Path(request.expected_state_db).expanduser().resolve(strict=False)
            if requested_db != state_db:
                raise PermissionError(
                    "project-contract decision producer cannot authorize a foreign store"
                )

        contract_id = _required(context.contract_id, "contract_id")
        contract_revision_id = _required(
            context.contract_revision_id,
            "contract_revision_id",
        )
        contract_text = _required(context.contract_text, "contract_text")
        contract_hash = _text_hash(contract_text)
        authority_catalog, contract_authority_id = _project_contract_catalog(
            contract_revision_id=contract_revision_id,
            contract_text=contract_text,
            contract_hash=contract_hash,
        )
        source_id = _required(context.source_id, "source_id")
        source_revision_id = _required(
            context.source_revision_id,
            "source_revision_id",
        )
        source_hash = _sha256(context.source_content_hash, "source_content_hash")
        created_at = _timestamp(context.created_at, "created_at")
        evidence_refs = _strings(
            context.evidence_refs,
            "evidence_refs",
            non_empty=True,
        )
        contract_evidence = _required(
            context.contract_evidence_ref,
            "contract_evidence_ref",
        )
        producer_code_hash = _sha256(
            context.producer_code_hash,
            "producer_code_hash",
        )
        request_identity = {
            "owner": request.owner,
            "executor_id": request.executor_id,
            "action_type": request.action_type,
            "target_ref": request.target_ref,
            "input_hash": request.input_hash,
        }
        request_binding_hash = sha256_json(request_identity)
        request_digest = request_binding_hash.split(":", 1)[1]
        if not callable(context.evaluator):
            raise TypeError("project-contract decision evaluator must be callable")
        evaluation = context.evaluator(request)
        if not isinstance(evaluation, ProjectContractDecisionEvaluation):
            raise TypeError(
                "project-contract evaluator must return ProjectContractDecisionEvaluation"
            )
        normalized_evaluation = _normalize_project_contract_evaluation(
            evaluation,
            request_binding_hash=request_binding_hash,
        )
        scope_id = f"{_required(context.scope_prefix, 'scope_prefix')}:{request_digest[:24]}"
        principal = PrincipalEnvelope(
            principal_id=f"system:{_required(context.producer, 'producer')}",
            agent="mnemos",
            host_kind="daemon",
            capability_id="project-contract-material-decision",
            capabilities=frozenset({"memory_read", "memory_write"}),
            allowed_projects=frozenset({"mnemos"}),
        )
        source_access = make_cognitive_access_envelope(
            owner_principal_id=principal.principal_id,
            owner_agent=principal.agent,
            scope_type="session",
            scope_id=scope_id,
            session_id=scope_id,
            project="mnemos",
            purposes=("cognitive_state_read", "cognitive_state_write"),
            consent_provenance_refs=(source_id, contract_id),
            sensitivity="sensitive",
            retention_policy="decision_trace",
            source_acl_lineage=(source_hash, contract_hash),
        )
        end_at = (
            datetime.fromisoformat(created_at.replace("Z", "+00:00")) + timedelta(days=1)
        ).isoformat()
        from core.cognitive.state_schema import initialize_cognitive_state_schema

        initialize_cognitive_state_schema(state_db)
        actions = (
            [
                {
                    "key": normalized_evaluation["selection_key"],
                    "action_type": request.action_type,
                    "owner": request.owner,
                    "executor": request.executor_id,
                    "target_ref": request.target_ref,
                    "input_hash": request.input_hash,
                    "rollback_contract": "restore the exact observed before state",
                    "expected_effect": (
                        "the exact evaluated target reaches the approved input state"
                    ),
                }
            ]
            if normalized_evaluation["approval_decision"] == "approved"
            else []
        )
        command = {
            "idempotency_key": "project-contract-material-"
            + _digest(
                {
                    "contract_id": contract_id,
                    "contract_revision_id": contract_revision_id,
                    "source_revision_id": source_revision_id,
                    **request_identity,
                }
            )[:40],
            "source": {
                "source_id": source_id,
                "source_revision_id": source_revision_id,
                "source_kind": "project_contract_execution",
                "source_uri": _required(context.source_uri, "source_uri"),
                "content_hash": source_hash,
                "evidence_refs": list(evidence_refs),
                "created_at": created_at,
                "privacy_level": "private",
                "access_control": source_access,
            },
            "scope": {"type": "session", "id": scope_id},
            "task": _required(context.task, "task"),
            "goal": _required(context.goal, "goal"),
            "constraints": list(_strings(context.constraints, "constraints", non_empty=True)),
            "values": [
                {
                    "key": "safety",
                    "category": "safety_permission_privacy",
                    "constraint": "Execute only the exact target and input sealed before the effect.",
                    "source_authority_id": contract_authority_id,
                    "source_id": contract_revision_id,
                    "source_revision_id": contract_revision_id,
                    "source_content_hash": contract_hash,
                    "evidence_refs": [contract_evidence, contract_authority_id],
                    "valid_from": created_at,
                    "valid_until": "",
                    "changed_decision": True,
                },
                {
                    "key": "project_contract",
                    "category": "project_constraint",
                    "constraint": _required(context.goal, "goal"),
                    "source_authority_id": contract_authority_id,
                    "source_id": contract_revision_id,
                    "source_revision_id": contract_revision_id,
                    "source_content_hash": contract_hash,
                    "evidence_refs": [contract_evidence, contract_authority_id],
                    "valid_from": created_at,
                    "valid_until": "",
                    "changed_decision": True,
                },
            ],
            "candidates": list(normalized_evaluation["candidates"]),
            "selection_key": normalized_evaluation["selection_key"],
            "rejections": list(normalized_evaluation["rejections"]),
            "model_spec": {
                "provider": "system",
                "model": _required(context.evaluator_id, "evaluator_id"),
                "route": "local",
                "version": _required(context.producer_version, "producer_version"),
                "config_hash": contract_hash,
            },
            "tool_specs": [
                {
                    "name": request.executor_id,
                    "version": _required(context.producer_version, "producer_version"),
                    "code_hash": producer_code_hash,
                }
            ],
            "prompt_spec": {
                "prompt_id": "none:" + _required(context.evaluator_id, "evaluator_id"),
                "prompt_hash": contract_hash,
                "schema_hash": producer_code_hash,
            },
            "expected_outcomes": list(normalized_evaluation["expected_outcomes"]),
            "evaluation_window": {"starts_at": created_at, "ends_at": end_at},
            "approval": {
                "mode": "project_contract",
                "decision": normalized_evaluation["approval_decision"],
                "evidence_ref": normalized_evaluation["approval_evidence_ref"],
                "created_at": created_at,
            },
            "supersedes_decision_revision_ids": list(
                _required_dead_letter_supersessions(state_db, actions)
            ),
            "actions": actions,
        }
        store = CognitiveStateStore(state_db)
        sealed: DecisionSealReceipt | None = None
        for _ in range(3):
            try:
                sealed = DecisionTraceStore(store).seal(
                    command,
                    principal=principal,
                    source_authority_catalog=authority_catalog,
                    prediction_plan=context.prediction_plan,
                    prediction_config=context.prediction_config,
                )
                break
            except CognitiveStateConflict:
                continue
        if sealed is None:
            raise CognitiveStateConflict(
                "project-contract decision could not seal after concurrent head changes"
            )
        if normalized_evaluation["approval_decision"] == "rejected":
            if sealed.command_ids:
                raise RuntimeError("rejected project-contract decision emitted an action")
            raise PermissionError(
                "project-contract evaluator rejected the requested material effect"
            )
        if len(sealed.command_ids) != 1:
            raise RuntimeError("approved project-contract decision did not seal one action")
        return MaterialActionCoordinator(store).bind_for_recovery(
            sealed.command_ids[0],
            executor_id=request.executor_id,
        )


MaterialActionResolver = Callable[
    [MaterialActionRequest],
    MaterialActionAuthorization,
]


def find_pending_material_action_authorization(
    *,
    state_db_path: Path,
    owner: str,
    executor_id: str,
    action_type: str,
    target_ref: str,
    input_hash: str,
    decision_source_revision_id: str = "",
) -> MaterialActionAuthorization | None:
    """Return one exact pending command for a retry, without creating state."""

    database = Path(state_db_path).expanduser().resolve(strict=False)
    if not database.is_file():
        return None
    store = CognitiveStateStore(database)
    matches: list[str] = []
    for command in store.pending_commands():
        if str(command.get("command_type") or "") != MATERIAL_ACTION_COMMAND_TYPE:
            continue
        payload = command.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if decision_source_revision_id:
            decision = store.revision(str(payload.get("decision_revision_id") or ""))
            if (
                decision is None
                or decision.object_type != "decision_trace"
                or decision.source_revision_id != decision_source_revision_id
            ):
                continue
        if (
            str(payload.get("owner") or "") == owner
            and str(payload.get("executor") or "") == executor_id
            and str(payload.get("action_type") or "") == action_type
            and str(payload.get("target_ref") or "") == target_ref
            and str(payload.get("input_hash") or "") == input_hash
        ):
            matches.append(str(command["command_id"]))
    if len(matches) > 1:
        raise RuntimeError("multiple pending material-action commands match one exact effect")
    if not matches:
        return None
    return MaterialActionCoordinator(store).bind_for_recovery(
        matches[0],
        executor_id=executor_id,
    )


def find_material_action_recovery_authorization(
    *,
    state_db_path: Path,
    owner: str,
    executor_id: str,
    action_type: str,
    target_ref: str,
    input_hash: str,
    decision_source_revision_id: str = "",
) -> MaterialActionAuthorization | None:
    """Return one exact pending or terminal command without creating new state."""

    database = Path(state_db_path).expanduser().resolve(strict=False)
    if not database.is_file():
        return None
    store = CognitiveStateStore(database)
    with store._connect(read_only=True) as conn:  # noqa: SLF001
        rows = conn.execute(
            """
            SELECT command_id, payload_json
            FROM cognitive_state_outbox
            WHERE command_type=?
            ORDER BY created_at, command_id
            """,
            (MATERIAL_ACTION_COMMAND_TYPE,),
        ).fetchall()
    matches: list[str] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if decision_source_revision_id:
            decision = store.revision(str(payload.get("decision_revision_id") or ""))
            if (
                decision is None
                or decision.object_type != "decision_trace"
                or decision.source_revision_id != decision_source_revision_id
            ):
                continue
        if (
            str(payload.get("owner") or "") == owner
            and str(payload.get("executor") or "") == executor_id
            and str(payload.get("action_type") or "") == action_type
            and str(payload.get("target_ref") or "") == target_ref
            and str(payload.get("input_hash") or "") == input_hash
        ):
            matches.append(str(row["command_id"]))
    if len(matches) > 1:
        raise RuntimeError("multiple material-action commands match one exact recovery effect")
    if not matches:
        return None
    return MaterialActionCoordinator(store).bind_for_recovery(
        matches[0],
        executor_id=executor_id,
    )


_MATERIAL_ACTION_RESOLVER: ContextVar[MaterialActionResolver | None] = ContextVar(
    "mnemos_material_action_resolver",
    default=None,
)


@contextmanager
def material_action_resolution_scope(resolver: MaterialActionResolver):
    """Propagate canonical capabilities through one bounded execution call tree."""

    if not callable(resolver):
        raise TypeError("material-action resolver must be callable")
    token = _MATERIAL_ACTION_RESOLVER.set(resolver)
    try:
        yield
    finally:
        _MATERIAL_ACTION_RESOLVER.reset(token)


def resolve_material_action_authorization(
    authorization: MaterialActionAuthorization | None,
    *,
    owner: str,
    executor_id: str,
    action_type: str,
    target_ref: str,
    input_hash: str,
    expected_state_db: Path | str | None = None,
) -> tuple[MaterialActionAuthorization, MaterialActionPermit]:
    """Resolve only an explicit or upstream-scoped canonical capability."""

    resolved = authorization
    if resolved is None:
        resolver = _MATERIAL_ACTION_RESOLVER.get()
        if resolver is None:
            raise PermissionError("canonical material-action authorization is required")
        resolved = resolver(
            MaterialActionRequest(
                owner=owner,
                executor_id=executor_id,
                action_type=action_type,
                target_ref=target_ref,
                input_hash=input_hash,
                expected_state_db=(
                    str(Path(expected_state_db).expanduser().resolve(strict=False))
                    if expected_state_db is not None
                    else ""
                ),
            )
        )
    if not isinstance(resolved, MaterialActionAuthorization):
        raise PermissionError("material-action resolver did not return canonical authorization")
    if expected_state_db is not None:
        actual_path = (
            Path(resolved.coordinator.state_store.db_path).expanduser().resolve(strict=False)
        )
        expected_path = Path(expected_state_db).expanduser().resolve(strict=False)
        if actual_path != expected_path:
            raise PermissionError(
                "material-action authorization belongs to a foreign canonical store"
            )
    permit = resolved.validate(
        owner=owner,
        executor_id=executor_id,
        action_type=action_type,
        target_ref=target_ref,
        input_hash=input_hash,
    )
    return resolved, permit


def resolve_material_action_recovery_authorization(
    authorization: MaterialActionAuthorization | None,
    *,
    owner: str,
    executor_id: str,
    action_type: str,
    target_ref: str,
    input_hash: str,
    expected_state_db: Path | str | None = None,
) -> tuple[MaterialActionAuthorization, MaterialActionPermit]:
    """Resolve an exact pending or terminal capability for read-only recovery.

    This seam never authorizes execution.  Callers must first ask their typed
    target oracle for an existing effect and then call ``require_material_action``
    before any new mutation when recovery returns no receipt.
    """

    resolved = authorization
    if resolved is None:
        resolver = _MATERIAL_ACTION_RESOLVER.get()
        if resolver is None:
            raise PermissionError("canonical material-action authorization is required")
        resolved = resolver(
            MaterialActionRequest(
                owner=owner,
                executor_id=executor_id,
                action_type=action_type,
                target_ref=target_ref,
                input_hash=input_hash,
                expected_state_db=(
                    str(Path(expected_state_db).expanduser().resolve(strict=False))
                    if expected_state_db is not None
                    else ""
                ),
            )
        )
    if not isinstance(resolved, MaterialActionAuthorization):
        raise PermissionError("material-action resolver did not return canonical authorization")
    if expected_state_db is not None:
        actual_path = (
            Path(resolved.coordinator.state_store.db_path).expanduser().resolve(strict=False)
        )
        expected_path = Path(expected_state_db).expanduser().resolve(strict=False)
        if actual_path != expected_path:
            raise PermissionError(
                "material-action authorization belongs to a foreign canonical store"
            )
    permit = resolved.coordinator._validated_permit(  # noqa: SLF001
        resolved.permit.command_id,
        executor_id=executor_id,
        allow_terminal=True,
    )
    if permit != resolved.permit:
        raise PermissionError("material-action permit binding is invalid")
    _validate_material_effect_fields(
        permit,
        owner=owner,
        executor_id=executor_id,
        action_type=action_type,
        target_ref=target_ref,
        input_hash=input_hash,
    )
    return resolved, permit


def require_material_action(
    authorization: MaterialActionAuthorization | None,
    *,
    owner: str,
    executor_id: str,
    action_type: str,
    target_ref: str,
    input_hash: str,
    expected_state_db: Path | str | None = None,
) -> MaterialActionPermit:
    """Fail closed unless a canonical authorization exactly binds the effect."""

    _, permit = resolve_material_action_authorization(
        authorization,
        owner=owner,
        executor_id=executor_id,
        action_type=action_type,
        target_ref=target_ref,
        input_hash=input_hash,
        expected_state_db=expected_state_db,
    )
    return permit


def require_material_action_projection(
    authorization: MaterialActionAuthorization | None,
    *,
    owner: str,
    executor_id: str,
    action_type: str,
    target_ref: str,
    input_hash: str,
    terminal_statuses: Sequence[str] = ("committed",),
    expected_state_db: Path | str | None = None,
) -> MaterialActionPermit:
    """Fail closed unless a terminal canonical action permits this projection."""

    if not isinstance(authorization, MaterialActionAuthorization):
        raise PermissionError("canonical material-action authorization is required")
    if expected_state_db is not None:
        actual_path = (
            Path(authorization.coordinator.state_store.db_path).expanduser().resolve(strict=False)
        )
        expected_path = Path(expected_state_db).expanduser().resolve(strict=False)
        if actual_path != expected_path:
            raise PermissionError(
                "material-action authorization belongs to a foreign canonical store"
            )
    return authorization.validate_projection(
        owner=owner,
        executor_id=executor_id,
        action_type=action_type,
        target_ref=target_ref,
        input_hash=input_hash,
        terminal_statuses=terminal_statuses,
    )
