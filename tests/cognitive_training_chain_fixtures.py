"""Public-API fixtures for complete Decision-to-Training provenance chains."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from core.access_policy import PrincipalEnvelope
from core.app.outcome_recorder import OutcomeRecorder
from core.application.cognitive_state import CognitiveStateApplicationService
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.delivery_router import (
    DeliveryBudgetPolicy,
    KnowledgeDeliveryRouter,
)
from core.cognitive.prediction_ledger import PredictionRecordStore
from core.cognitive.state_contract import CognitiveStateRevision, sha256_json
from core.cognitive.training_contract import derive_dataset_assignment
from core.cognitive.training_governance import TrainingGovernanceStore
from core.cognitive.trust_scorer import TrustDecision
from tests.unit.cognitive.test_prediction_ledger import (
    _objective_outcome_request,
)


class _TrainingChainTrust:
    """Deterministic trust input; production routing still owns all effects."""

    def decide(self, **kwargs: Any) -> TrustDecision:
        subject = str(kwargs["subject"])
        decision_id = (
            "trust-training-chain-"
            + sha256_json({"subject": subject}).split(":", 1)[1][:24]
        )
        return TrustDecision(
            decision_id=decision_id,
            source=str(kwargs["source"]),
            subject=subject,
            action=str(kwargs["action"]),
            decision="deliver",
            reason="deterministic_training_chain_fixture",
            trust_score=0.9,
            task_fit_score=float(kwargs["task_fit_score"]),
            interruption_cost=float(kwargs["interruption_cost"]),
            outcome_score=0.0,
            evidence_refs=list(kwargs.get("evidence_refs") or ()),
            metadata={},
        )


def subjects_for_required_splits(
    *,
    scope: Mapping[str, str],
    prefix: str,
) -> tuple[tuple[dict[str, str], str], ...]:
    """Find the canonical 20/2/2 deterministic split denominator."""

    wanted = {"train": 20, "validation": 2, "holdout": 2}
    found: dict[str, list[dict[str, str]]] = {key: [] for key in wanted}
    index = 0
    while any(len(found[key]) < count for key, count in wanted.items()):
        subject = {
            "type": "knowledge_topic",
            "id": f"{prefix}-{index:04d}",
        }
        split = derive_dataset_assignment(
            subject=subject,
            scope=scope,
        )["split"]
        if len(found[split]) < wanted[split]:
            found[split].append(subject)
        index += 1
    return (
        *((subject, "train") for subject in found["train"]),
        *((subject, "validation") for subject in found["validation"]),
        *((subject, "holdout") for subject in found["holdout"]),
    )


def build_ready_public_admissions(
    governance: TrainingGovernanceStore,
    *,
    access_override: Mapping[str, Any] | None = None,
    scope_override: Mapping[str, str] | None = None,
    subject_prefix: str = "training-chain",
) -> tuple[CognitiveStateRevision, ...]:
    """Create 24 admissions only through public production entrypoints."""

    state = governance.state
    root = Path(governance.database_dir)
    scope = dict(scope_override or {"type": "project", "id": "mnemos"})
    if access_override is None:
        owner_principal_id = "system:training-chain"
        owner_agent = "mnemos"
        project = "mnemos"
        session_id = "training-chain-session"
        sensitivity = "sensitive"
    else:
        owner = dict(access_override["owner"])
        access_scope = dict(access_override["scope"])
        owner_principal_id = str(owner["principal_id"])
        owner_agent = str(owner["agent"])
        project = str(access_scope["project"])
        session_id = str(access_scope["session_id"])
        sensitivity = str(access_override["sensitivity"])
        if (
            scope["type"] != access_scope["scope_type"]
            or scope["id"] != access_scope["scope_id"]
        ):
            raise ValueError("training fixture scope/access mismatch")
    principal = PrincipalEnvelope(
        principal_id=owner_principal_id,
        agent=owner_agent,
        host_kind="test",
        capability_id="public-training-chain-fixture",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({project}),
    )
    source_access = make_cognitive_access_envelope(
        owner_principal_id=owner_principal_id,
        owner_agent=owner_agent,
        scope_type=scope["type"],
        scope_id=scope["id"],
        session_id=session_id,
        project=project,
        purposes=(
            "cognitive_state_read",
            "cognitive_state_write",
            "prediction_read",
        ),
        consent_provenance_refs=(
            f"test-fixture:{subject_prefix}",
        ),
        sensitivity=sensitivity,
        retention_policy="prediction_source",
        source_acl_lineage=(
            sha256_json(
                {
                    "subject_prefix": subject_prefix,
                    "scope": scope,
                }
            ),
        ),
    )
    router = KnowledgeDeliveryRouter(
        db_path=root / "delivery_events.db",
        database_dir=root,
        config={"prediction.predictive_delivery_window_hours": 168},
        policy=DeliveryBudgetPolicy(
            daily_total=100,
            per_task_total=100,
            per_task_hint=100,
            per_task_warn=100,
            same_topic_cooldown_hours=0,
        ),
        trust_scorer=_TrainingChainTrust(),
    )
    admissions: list[CognitiveStateRevision] = []
    for index, (subject, expected_split) in enumerate(
        subjects_for_required_splits(
            scope=scope,
            prefix=subject_prefix,
        ),
        start=1,
    ):
        observed_value = "useful" if index % 2 else "not_useful"
        route = router.route_candidate(
            source="predictive_push",
            subject=subject["id"],
            channel="predictive_push",
            target=f"03-Tech/{subject['id']}.md",
            evidence_refs=[f"wiki:{subject['id']}"],
            task_fit_score=0.85 if observed_value == "useful" else 0.35,
            requested_level="hint",
            task_key=f"training-chain-task-{index:04d}",
            cooldown_key=f"training-chain-cooldown-{index:04d}",
            scope_type=scope["type"],
            scope_value=scope["id"],
            source_access_control=source_access,
            principal=principal,
        )
        matches = [
            revision
            for revision in state.current_revisions(
                object_type="prediction_record"
            )
            if revision.payload["delivery_ref"]["event_id"] == route.event_id
        ]
        if len(matches) != 1:
            raise RuntimeError("public training fixture prediction gap")
        prediction = matches[0]
        request, outcome_principal, _observed_at, catalog = (
            _objective_outcome_request(
                prediction,
                root / "raw_events.db",
                observed_value=observed_value,
                source_suffix=f"{subject_prefix}-{index:04d}",
                observed_hours=index,
            )
        )
        result = CognitiveStateApplicationService(state).apply_outcome(
            request,
            principal=outcome_principal,
            source_authority_catalog=catalog,
        )
        outcome = state.revision(str(result["revision_ids"][0]))
        if outcome is None:
            raise RuntimeError("public training fixture outcome gap")
        PredictionRecordStore(state).finalize(
            prediction.object_id,
            {},
            outcome.payload["maturity"]["matured_at"],
        )
        recorded = OutcomeRecorder(
            state_db=state.db_path,
            governance_clock=lambda value=str(
                outcome.payload["maturity"]["matured_at"]
            ): value,
        ).record_objective_outcome(
            outcome,
            principal=outcome_principal,
        )
        admission = state.revision(
            str(recorded["training_admission"]["admission_revision_id"])
        )
        if admission is None:
            raise RuntimeError("public training fixture admission gap")
        if admission.payload["dataset_assignment"]["split"] != expected_split:
            raise RuntimeError("public training fixture split drift")
        admissions.append(admission)
    return tuple(admissions)
