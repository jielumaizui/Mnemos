"""Adapters that delegate COG-038 target commands to domain-owned journals."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, cast

from core.cognitive.feedback_contract import FEEDBACK_TARGETS
from core.cognitive.feedback_models import (
    FeedbackProposalGateFactory,
    FeedbackTargetAdapter,
    FeedbackTargetEffect,
)
from core.cognitive.feedback_target_registry import (
    TARGET_DB_FILE_BY_ID,
    TARGET_DOMAIN_TABLES,
    build_registered_feedback_proposal_owner,
)

__all__ = [
    "TARGET_DB_FILE_BY_ID",
    "TARGET_DOMAIN_TABLES",
    "DomainOwnedFeedbackTargetAdapter",
    "build_feedback_target_adapters",
]


class DomainOwnedFeedbackTargetAdapter:
    """Expose one domain owner's proposal journal through the target protocol."""

    def __init__(self, *, target_id: str, owner: Any) -> None:
        normalized = str(target_id or "").strip()
        if normalized not in FEEDBACK_TARGETS:
            raise ValueError("unknown feedback target adapter")
        if str(getattr(owner, "target_id", "")) != normalized:
            raise ValueError("feedback target owner identity mismatch")
        self.target_id = normalized
        self.owner = owner

    def apply(self, command: Mapping[str, Any]) -> FeedbackTargetEffect:
        """Ask the domain owner to persist and prove a reviewable proposal."""

        return cast(FeedbackTargetEffect, self.owner.apply(command))

    def neutralize(self, command: Mapping[str, Any]) -> FeedbackTargetEffect:
        """Ask the domain owner to persist and prove a neutralization action."""

        return cast(FeedbackTargetEffect, self.owner.neutralize(command))

    def recover_command_effect(
        self,
        command: Mapping[str, Any],
    ) -> FeedbackTargetEffect | None:
        """Re-read an exact domain receipt after an interrupted adapter call."""

        recovered = self.owner.recover_command_effect(command)
        return cast(FeedbackTargetEffect | None, recovered)

    def inspect_command_effect(
        self,
        command: Mapping[str, Any],
    ) -> FeedbackTargetEffect | None:
        """Inspect exact domain state without collapsing errors into absence."""

        inspected = self.owner.inspect_command_effect(command)
        return cast(FeedbackTargetEffect | None, inspected)

    def verify(self, effect: FeedbackTargetEffect) -> bool:
        """Re-read the domain proposal/action and reciprocal receipt."""

        return bool(self.owner.verify(effect))

    def verify_command_effect(
        self,
        command: Mapping[str, Any],
        effect: FeedbackTargetEffect,
    ) -> bool:
        """Bind a domain proof to the immutable canonical command payload."""

        return bool(self.owner.verify_command_effect(command, effect))


def build_feedback_target_adapters(
    database_dir: Path,
    *,
    proposal_gate_factory: FeedbackProposalGateFactory | None = None,
) -> dict[str, FeedbackTargetAdapter]:
    """Build adapters from the shallow canonical domain-journal registry."""

    root = Path(database_dir)
    owners = {
        target_id: build_registered_feedback_proposal_owner(
            root,
            target_id,
            proposal_gate_factory=proposal_gate_factory,
        )
        for target_id in FEEDBACK_TARGETS
    }
    if tuple(sorted(owners)) != FEEDBACK_TARGETS:
        raise RuntimeError("domain feedback target registry drift")
    return {
        target_id: DomainOwnedFeedbackTargetAdapter(
            target_id=target_id,
            owner=owners[target_id],
        )
        for target_id in FEEDBACK_TARGETS
    }
