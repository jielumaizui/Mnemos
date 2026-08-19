"""Command derivation for canonical feedback attribution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.cognitive.feedback_contract import FEEDBACK_TARGETS
from core.cognitive.feedback_prior_state import inspect_prior_feedback_state
from core.cognitive.state_contract import CognitiveStateRevision, LocalConsumerCommand


class FeedbackAttributionCommandMixin:
    """Derive target and neutralization commands from an attribution revision."""

    if TYPE_CHECKING:
        from core.cognitive.state_store import CognitiveStateStore

        state: CognitiveStateStore

    @staticmethod
    def _training_outcome_ref(
        attribution: CognitiveStateRevision,
    ) -> dict[str, str]:
        matches = [
            item
            for item in attribution.payload["outcome_refs"]
            if item["revision_id"] == attribution.source_revision_id
        ]
        if len(matches) == 1:
            outcome = matches[0]
            return {
                "state": "available",
                "outcome_id": str(outcome["outcome_id"]),
                "revision_id": str(outcome["revision_id"]),
                "payload_hash": str(outcome["payload_hash"]),
                "unavailable_reason": "",
            }
        return {
            "state": "unavailable",
            "outcome_id": "",
            "revision_id": "",
            "payload_hash": "",
            "unavailable_reason": "objective_outcome_not_bound_to_command",
        }

    @staticmethod
    def _target_commands(
        attribution: CognitiveStateRevision,
        *,
        recorded_at: str,
    ) -> tuple[LocalConsumerCommand, ...]:
        return tuple(
            LocalConsumerCommand.create(
                revision_id=attribution.revision_id,
                consumer_id=str(disposition["target_id"]),
                command_type="evaluate_feedback_target",
                payload={
                    "schema_version": "mnemos.feedback_target_command.v1",
                    "attribution_revision_id": attribution.revision_id,
                    "attribution_payload_hash": attribution.payload_hash,
                    "input_set_hash": attribution.payload["input_set_hash"],
                    "target_id": disposition["target_id"],
                    "eligible": disposition["eligible"],
                    "exclusion_reason": disposition["exclusion_reason"],
                    "command_key": disposition["command_ref"]["command_key"],
                    "effect_kind": ("proposal" if disposition["eligible"] else "intentional_skip"),
                    "required_target_ids": list(FEEDBACK_TARGETS),
                    **(
                        {
                            "objective_outcome_ref": (
                                FeedbackAttributionCommandMixin._training_outcome_ref(attribution)
                            )
                        }
                        if disposition["target_id"] == "training_evidence"
                        else {}
                    ),
                },
                created_at=recorded_at,
            )
            for disposition in attribution.payload["target_dispositions"]
            if disposition["command_ref"]["command_type"] == "evaluate_feedback_target"
        )

    def _neutralization_commands(
        self,
        attribution: CognitiveStateRevision,
        *,
        recorded_at: str,
    ) -> tuple[LocalConsumerCommand, ...]:
        if not attribution.supersedes_revision_id:
            return ()
        prior_revision = self.state.revision(attribution.supersedes_revision_id)
        if prior_revision is None:
            raise RuntimeError("correction attribution predecessor is missing")
        prior_effects = dict(
            inspect_prior_feedback_state(
                self.state,
                prior_revision,
            ).active_effects_by_target
        )
        commands: list[LocalConsumerCommand] = []
        for disposition in attribution.payload["target_dispositions"]:
            if disposition["command_ref"]["command_type"] != "neutralize_feedback_effect":
                continue
            target_id = str(disposition["target_id"])
            prior = prior_effects.get(target_id)
            if prior is None:
                raise RuntimeError("correction neutralization source effect is missing")
            prior_outcome = str(prior.get("consumption_outcome") or "")
            neutralization_kind = (
                "suppress" if prior_outcome == "proposal_committed" else "compensate"
            )
            commands.append(
                LocalConsumerCommand.create(
                    revision_id=attribution.revision_id,
                    consumer_id=target_id,
                    command_type="neutralize_feedback_effect",
                    payload={
                        "schema_version": "mnemos.feedback_neutralization_command.v1",
                        "attribution_revision_id": attribution.revision_id,
                        "attribution_payload_hash": attribution.payload_hash,
                        "target_id": target_id,
                        "command_key": disposition["command_ref"]["command_key"],
                        "prior_attribution_revision_id": prior["revision_id"],
                        "prior_effect_receipt_id": prior["receipt_id"],
                        "prior_command_id": prior["command_id"],
                        "prior_target_effect_id": prior["target_effect_id"],
                        "prior_before_hash": prior["before_hash"],
                        "prior_after_hash": prior["after_hash"],
                        "neutralization_kind": neutralization_kind,
                    },
                    created_at=recorded_at,
                )
            )
        return tuple(commands)
