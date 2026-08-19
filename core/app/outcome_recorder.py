"""Explicit compatibility adapter for canonical feedback attribution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from core.access_policy import PrincipalEnvelope
from core.cognitive.feedback_attribution import (
    FeedbackAttributionStore,
    UserReactionInput,
)
from core.cognitive.feedback_proposal_gate import (
    build_gated_feedback_target_adapters,
)
from core.cognitive.state_contract import CognitiveStateRevision
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.training_governance import TrainingGovernanceStore
from core.runtime_paths import RuntimePaths


@dataclass
class OutcomeRecorder:
    """Route typed reactions or verified outcomes without deriving labels."""

    state_db: Path | None = None
    database_dir: Path | None = None
    governance_clock: Callable[[], str] | None = None

    def _owner(self) -> FeedbackAttributionStore:
        if self.state_db is not None:
            path = Path(self.state_db)
        else:
            root = Path(self.database_dir or RuntimePaths.from_config().database_dir)
            path = root / "producer_consumer_ledger.db"
        state = CognitiveStateStore(path)
        return FeedbackAttributionStore(
            state,
            target_adapters=build_gated_feedback_target_adapters(path.parent),
        )

    def record_reaction(
        self,
        reaction: UserReactionInput,
        *,
        principal: PrincipalEnvelope,
    ) -> dict[str, Any]:
        """Delegate one already-typed reaction without semantic promotion."""

        if not isinstance(reaction, UserReactionInput):
            raise TypeError("reaction must be UserReactionInput")
        receipt = self._owner().record_reaction(reaction, principal)
        return {
            "success": True,
            "schema_version": "mnemos.feedback_reaction_receipt.v1",
            **asdict(receipt),
            "derived_label": None,
            "direct_domain_updates": 0,
        }

    def record_objective_outcome(
        self,
        outcome_revision: CognitiveStateRevision,
        *,
        principal: PrincipalEnvelope,
        _failpoint: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Delegate only an oracle-verified committed OutcomeMeasurement."""

        owner = self._owner()
        receipt = owner.record_objective_outcome(
            outcome_revision,
            principal,
        )
        if not receipt.training_admission_command_id:
            raise RuntimeError("objective attribution lacks a durable training intake")
        if _failpoint is not None:
            _failpoint("before_feedback_target_completion")
        terminal_receipts = []
        for command_id in receipt.command_ids:
            terminal_receipts.append(owner.process_command(command_id))
            command = owner.state.command(command_id)
            if (
                _failpoint is not None
                and command is not None
                and command["consumer_id"] == "training_evidence"
            ):
                _failpoint("after_training_evidence_target_receipt")
        admission = TrainingGovernanceStore(
            owner.state,
            database_dir=owner.state.db_path.parent,
            clock=self.governance_clock,
        ).process_admission_intake(
            receipt.training_admission_command_id,
            _failpoint=_failpoint,
        )
        return {
            "success": True,
            "schema_version": "mnemos.feedback_objective_attribution_receipt.v1",
            **asdict(receipt),
            "terminal_receipts": [asdict(item) for item in terminal_receipts],
            "training_admission": asdict(admission),
        }

    def record_outcome(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        """Reject the retired reaction-to-label fanout signature."""

        raise RuntimeError(
            "ambiguous_outcome_recorder_signature_retired; use record_reaction "
            "or record_objective_outcome"
        )
