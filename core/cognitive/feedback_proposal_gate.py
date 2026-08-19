"""Trusted DecisionTrace/material gate for domain-owned feedback proposals."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionRequest,
    MaterialActionTerminal,
    authorize_exact_project_contract_action,
)
from core.cognitive.feedback_models import (
    CognitiveEntityReference,
    FeedbackProposalGate,
    FeedbackProposalTerminalProof,
)
from core.cognitive.state_contract import sha256_json
from core.trust.config import TrustedPushConfig
from core.trust.models import CandidateBundle
from core.trust.push_decision_gate import PushDecisionGate


FEEDBACK_PROPOSAL_ACTION_TYPE = "create_feedback_review_proposal"
FEEDBACK_PROPOSAL_EXECUTOR = "domain_feedback_proposal_store"
FEEDBACK_PROPOSAL_PRODUCER_VERSION = "mnemos.domain_feedback_proposal.v1"
FEEDBACK_PROPOSAL_PRODUCER_HASH = sha256_json(
    {
        "module": "core.cognitive.feedback_proposal_gate",
        "producer": "DecisionTraceFeedbackProposalGate",
        "version": FEEDBACK_PROPOSAL_PRODUCER_VERSION,
    }
)


class DecisionTraceFeedbackProposalGate:
    """Bind one trusted proposal to a canonical DecisionTrace material permit."""

    def __init__(
        self,
        *,
        database_dir: Path,
        target_id: str,
        owner_id: str,
        gate_contract_id: str,
        proposal: Mapping[str, Any],
    ) -> None:
        self.database_dir = Path(database_dir).expanduser()
        self.target_id = str(target_id)
        self.owner_id = str(owner_id)
        self.gate_contract_id = str(gate_contract_id)
        candidate = CandidateBundle.from_payload(
            source="canonical_feedback_attribution",
            source_agent="mnemos",
            source_session_id=str(proposal["attribution_revision_id"]),
            target_kind="native_store",
            payload=dict(proposal),
            evidence_refs=[
                f"feedback-command:{proposal['command_key']}",
                f"feedback-attribution:{proposal['attribution_revision_id']}",
            ],
            risk_level="medium",
            proposed_actions=[FEEDBACK_PROPOSAL_ACTION_TYPE],
        )
        trusted = PushDecisionGate(
            wiki_base=self.database_dir,
            config=TrustedPushConfig(
                mode="enforce",
                db_path=self.database_dir / "trust_decisions.db",
            ),
        ).evaluate(candidate)
        if not trusted.accepted:
            raise PermissionError("trusted push gate rejected feedback proposal")
        self.proposal: Mapping[str, Any] = {
            **dict(proposal),
            "trusted_gate": {
                "decision": trusted.decision,
                "risk_level": trusted.risk_level,
                "reasons": list(trusted.reasons),
                "missing_info": list(trusted.missing_info),
                "candidate_payload_hash": candidate.payload_hash,
            },
        }
        self.proposal_id = self._proposal_id(self.target_id, self.proposal)
        self._authorization = self._authorize()
        permit = self._authorization.permit
        self.material_command_id = permit.command_id
        self.material_effect_id = permit.effect_id
        decision = self._authorization.coordinator.state_store.revision(
            permit.decision_revision_id
        )
        if decision is None or decision.object_type != "decision_trace":
            raise RuntimeError("feedback proposal DecisionTrace is unavailable")
        action_specs = [
            dict(item)
            for item in decision.payload.get("action_specs") or ()
            if isinstance(item, Mapping) and item.get("action_id") == permit.action_id
        ]
        if len(action_specs) != 1:
            raise RuntimeError("feedback proposal material action is unavailable")
        self.decision_trace_refs: tuple[CognitiveEntityReference, ...] = (
            CognitiveEntityReference(
                id=decision.object_id,
                revision_id=decision.revision_id,
                content_hash=decision.payload_hash,
            ),
        )
        self.action_refs: tuple[CognitiveEntityReference, ...] = (
            CognitiveEntityReference(
                id=permit.action_id,
                revision_id=decision.revision_id,
                content_hash=sha256_json(action_specs[0]),
            ),
        )

    def terminal_proof(self) -> FeedbackProposalTerminalProof | None:
        terminal = self._authorization.terminal_receipt()
        if terminal is None:
            return None
        return FeedbackProposalTerminalProof(
            effect_id=terminal.effect_id,
            before_hash=terminal.before_hash,
            after_hash=terminal.after_hash,
        )

    def validate(self) -> None:
        self._authorization.validate(
            owner=self._material_owner,
            executor_id=FEEDBACK_PROPOSAL_EXECUTOR,
            action_type=FEEDBACK_PROPOSAL_ACTION_TYPE,
            target_ref=self._target_ref,
            input_hash=sha256_json(self.proposal),
        )

    def record_committed(
        self,
        *,
        before_hash: str,
        after_hash: str,
        target_receipt_ref: str,
        created_at: str,
    ) -> None:
        permit = self._authorization.permit
        self._authorization.record_terminal(
            MaterialActionTerminal(
                status="committed",
                target_effect_id=permit.effect_id,
                before_hash=before_hash,
                after_hash=after_hash,
                evidence_refs=(
                    f"material-command:{permit.command_id}",
                    f"decision-revision:{permit.decision_revision_id}",
                    f"material-effect:{permit.effect_id}",
                    f"target-after:{after_hash}",
                    (
                        "target-journal:feedback-proposal:"
                        f"{self.target_id}:{target_receipt_ref}"
                    ),
                    target_receipt_ref,
                ),
                outcome="feedback review proposal committed",
                created_at=created_at,
            )
        )

    @property
    def _material_owner(self) -> str:
        return f"feedback_proposal:{self.owner_id}"

    @property
    def _target_ref(self) -> str:
        return f"feedback-proposal:{self.target_id}:{self.proposal_id}"

    def _authorize(self) -> MaterialActionAuthorization:
        state_db = (self.database_dir / "producer_consumer_ledger.db").resolve(
            strict=False
        )
        request = MaterialActionRequest(
            owner=self._material_owner,
            executor_id=FEEDBACK_PROPOSAL_EXECUTOR,
            action_type=FEEDBACK_PROPOSAL_ACTION_TYPE,
            target_ref=self._target_ref,
            input_hash=sha256_json(self.proposal),
            expected_state_db=str(state_db),
        )
        trusted_gate = dict(self.proposal.get("trusted_gate") or {})
        return authorize_exact_project_contract_action(
            expected_request=request,
            state_db_path=state_db,
            contract_id=f"project-contract:{self.gate_contract_id}",
            contract_revision_id=self.gate_contract_id,
            contract_text=(
                "Canonical feedback attribution may create only an exact "
                f"pending-review proposal for {self.target_id}; it may not "
                "perform a direct domain or training update."
            ),
            source_namespace=f"feedback-domain-proposal-{self.target_id}",
            source_facts={
                "schema_version": "mnemos.feedback_proposal_decision_facts.v1",
                "proposal_id": self.proposal_id,
                "proposal": dict(self.proposal),
                "material_request": {
                    "owner": request.owner,
                    "executor_id": request.executor_id,
                    "action_type": request.action_type,
                    "target_ref": request.target_ref,
                    "input_hash": request.input_hash,
                    "expected_state_db": request.expected_state_db,
                },
            },
            decision_checks={
                "trusted_gate_accepted": trusted_gate.get("decision")
                in {
                    "allow_pending_user_decision",
                    "needs_manual_review",
                },
                "pending_review_only": self.proposal.get("status")
                == "pending_review",
                "no_direct_domain_update": self.proposal.get(
                    "direct_domain_update"
                )
                is False,
                "no_training_admission": self.proposal.get("training_admitted")
                is False,
                "exact_target": self.proposal.get("target_id") == self.target_id,
            },
            evidence_refs=(
                f"feedback-command:{self.proposal['command_key']}",
                f"feedback-attribution:{self.proposal['attribution_revision_id']}",
                f"trusted-gate:{trusted_gate.get('candidate_payload_hash')}",
            ),
            task=f"Create {self.target_id} feedback review proposal",
            goal="Persist only the exact trusted pending-review proposal.",
            constraints=(
                "The trusted gate must accept the exact candidate payload.",
                "The target remains pending review and cannot update domain truth.",
                "The material request is bound to the injected cognitive state store.",
            ),
            created_at=self._now(),
            producer="domain-feedback-proposal-store",
            producer_version=FEEDBACK_PROPOSAL_PRODUCER_VERSION,
            producer_code_hash=FEEDBACK_PROPOSAL_PRODUCER_HASH,
            evaluator_id="feedback-domain-proposal-material-evaluator",
            approved_candidate_key=f"create_{self.target_id}_pending_review",
            approved_candidate_summary=(
                f"Create the exact trusted {self.target_id} pending-review proposal."
            ),
            rejected_candidate_key=f"retain_{self.target_id}_current_state",
            rejected_candidate_summary=(
                f"Retain current {self.target_id} state when any binding fails."
            ),
            approved_reason_code="feedback_proposal_binding_verified",
            rejected_reason_code="feedback_proposal_binding_rejected",
            committed_metric="feedback_proposal_material_effect_committed",
            rejected_metric="unbound_feedback_proposal_count",
        )

    @staticmethod
    def _proposal_id(target_id: str, proposal: Mapping[str, Any]) -> str:
        suffix = sha256_json(proposal).split(":", 1)[1][:32]
        return f"{target_id}-proposal-{suffix}"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_decision_trace_feedback_proposal_gate(
    *,
    database_dir: Any,
    target_id: str,
    owner_id: str,
    gate_contract_id: str,
    proposal: Mapping[str, Any],
) -> FeedbackProposalGate:
    """Build the concrete trusted material gate injected into target owners."""

    return DecisionTraceFeedbackProposalGate(
        database_dir=Path(database_dir),
        target_id=target_id,
        owner_id=owner_id,
        gate_contract_id=gate_contract_id,
        proposal=proposal,
    )


def build_gated_feedback_target_adapters(
    database_dir: Path,
) -> dict[str, Any]:
    """Build runtime target adapters with the canonical proposal gate injected."""

    from core.cognitive.feedback_targets import build_feedback_target_adapters

    return build_feedback_target_adapters(
        database_dir,
        proposal_gate_factory=build_decision_trace_feedback_proposal_gate,
    )
