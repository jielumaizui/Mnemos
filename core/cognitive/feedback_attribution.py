"""Canonical owner for user reactions and conservative feedback attribution."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Callable, Mapping

from core.access_policy import PrincipalEnvelope
from core.cognitive.access_control import (
    authorize_cognitive_write,
    validate_cognitive_access_envelope,
)
from core.cognitive.feedback_contract import (
    EXPLICIT_REACTION_KINDS,
    FEEDBACK_TARGET_REGISTRY_HASH,
    FEEDBACK_TARGET_REGISTRY_VERSION,
    FEEDBACK_TARGETS,
    attribution_input_set_hash,
    validate_user_reaction_payload,
)
from core.cognitive.feedback_command_failure import (
    _record_permanent_feedback_failure,
)
from core.cognitive.feedback_models import (
    AttributionReceipt,
    FeedbackVerification,
    FeedbackTargetAdapter,
    FeedbackTargetEffect,
    ReactionReceipt,
    ReplayBatchReceipt,
    TargetDispositionReceipt,
    UserReactionInput,
)
from core.cognitive.feedback_migration_barrier import (
    FeedbackMigrationInProgress,
    assert_feedback_writes_enabled,
)
from core.cognitive.feedback_owner_identity import (
    CanonicalFeedbackOwner,
    _ACTIVE_FEEDBACK_FAILURE_CONTEXT,
)
from core.cognitive.feedback_identity import (
    SUPPORTED_FEEDBACK_SOURCE_CHANNELS,
    attribution_principal_ref,
    feedback_attribution_id,
)
from core.cognitive.feedback_reaction_builder import (
    REACTION_BUILDER_CODE_HASH,
    build_reaction_id,
    build_reaction_payload,
)
from core.cognitive.feedback_attribution_spec import (
    FEEDBACK_ATTRIBUTION_CONFIG_HASH,
    FEEDBACK_ATTRIBUTION_METHOD,
    FEEDBACK_ATTRIBUTION_SPEC_HASH,
)
from core.cognitive.feedback_target_execution import invoke_target_adapter
from core.cognitive.feedback_update_receipt import (
    build_cognitive_update_receipt,
    build_ineligible_cognitive_update_receipt,
)
from core.cognitive.feedback_prior_state import inspect_prior_feedback_state
from core.cognitive.feedback_attribution_commands import FeedbackAttributionCommandMixin
from core.cognitive.state_contract import (
    CognitiveHeadPrecondition,
    CognitiveStateRevision,
    LocalConsumerCommand,
    sha256_json,
)
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.prediction_ledger import PredictionRecordStore
from core.cognitive.training_contract import (
    TRAINING_ADMISSION_COMMAND,
    TRAINING_ADMISSION_CONSUMER,
)
from core.cognitive.training_intake_derivation import (
    derive_training_admission_intake_command,
)
from core.cognitive.feedback_causal_refs import validate_reaction_causal_refs
from core.evidence.artifact_catalog import sha256_file
from core.ops.cognitive_data_contract import CognitiveDataEvent


FEEDBACK_ATTRIBUTION_CODE_HASH = sha256_json(
    {
        "owner": "sha256:" + sha256_file(__file__),
        "reaction_builder": REACTION_BUILDER_CODE_HASH,
    }
)

_CORRECTION_KINDS = frozenset({"inaccurate", "outdated"})


class FeedbackAttributionStore(FeedbackAttributionCommandMixin, CanonicalFeedbackOwner):
    """Deep canonical module for reaction identity, attribution, and outbox work."""

    def __init__(
        self,
        state_store: CognitiveStateStore,
        *,
        clock: Callable[[], str] | None = None,
        target_adapters: Mapping[str, FeedbackTargetAdapter] | None = None,
    ) -> None:
        self.state = state_store
        self.__feedback_failure_capability: object
        state_store._bind_feedback_owner_capability(self)
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self._target_adapters = dict(target_adapters or {})
        unknown_adapters = set(self._target_adapters) - set(FEEDBACK_TARGETS)
        if unknown_adapters:
            raise ValueError(
                "unknown feedback target adapter: " + ", ".join(sorted(unknown_adapters))
            )

    def record_reaction(
        self,
        reaction: UserReactionInput,
        principal: PrincipalEnvelope,
    ) -> ReactionReceipt:
        assert_feedback_writes_enabled(self.state.db_path.parent)
        if not isinstance(reaction, UserReactionInput):
            raise TypeError("reaction must be UserReactionInput")
        if reaction.source_channel not in SUPPORTED_FEEDBACK_SOURCE_CHANNELS:
            raise ValueError("unsupported_feedback_source_channel")
        access = validate_cognitive_access_envelope(
            reaction.access_control,
            expected_scope_type=reaction.scope_type,
            expected_scope_id=reaction.scope_id,
        )
        authorization = authorize_cognitive_write(
            access,
            principal=principal,
            scope_type=reaction.scope_type,
            scope_id=reaction.scope_id,
        )
        if not authorization.allowed:
            raise PermissionError(f"feedback write access denied: {authorization.reason}")
        recorded_at = self._clock()
        reaction_id = build_reaction_id(reaction, principal)
        current_reaction = self.state.current_revision(
            "user_reaction_event",
            reaction_id,
        )
        if current_reaction is not None:
            candidate_payload = build_reaction_payload(
                reaction,
                principal=principal,
                access=access,
                reaction_id=reaction_id,
                recorded_at=recorded_at,
                has_current=True,
                attribution_code_hash=FEEDBACK_ATTRIBUTION_CODE_HASH,
                attribution_spec_hash=FEEDBACK_ATTRIBUTION_SPEC_HASH,
            )
            validate_user_reaction_payload(candidate_payload)
            validate_reaction_causal_refs(self.state, candidate_payload)
            if (
                current_reaction.payload["reaction_input_hash"]
                == candidate_payload["reaction_input_hash"]
            ):
                return self._existing_receipt(current_reaction)
            if reaction.supersedes_event_id != current_reaction.source_event_id:
                raise ValueError("stale_reaction_supersedes")
        elif reaction.supersedes_event_id or reaction.correction_of_event_id:
            raise ValueError("reaction_supersedes_missing_head")

        reaction_payload = build_reaction_payload(
            reaction,
            principal=principal,
            access=access,
            reaction_id=reaction_id,
            recorded_at=recorded_at,
            has_current=current_reaction is not None,
            attribution_code_hash=FEEDBACK_ATTRIBUTION_CODE_HASH,
            attribution_spec_hash=FEEDBACK_ATTRIBUTION_SPEC_HASH,
        )
        validate_user_reaction_payload(reaction_payload)
        validate_reaction_causal_refs(self.state, reaction_payload)
        event_id = (
            "feedback-event-"
            + sha256_json(
                {
                    "reaction_id": reaction_id,
                    "input_hash": reaction_payload["reaction_input_hash"],
                }
            ).split(":", 1)[1][:32]
        )
        reaction_revision = CognitiveStateRevision.create(
            object_type="user_reaction_event",
            object_id=reaction_id,
            source_event_id=event_id,
            source_revision_id=reaction.source_revision_id,
            source_content_hash=reaction.source_content_hash,
            scope_type=reaction.scope_type,
            scope_id=reaction.scope_id,
            evidence_refs=reaction.evidence_refs,
            payload=reaction_payload,
            supersedes_revision_id=(
                current_reaction.revision_id if current_reaction is not None else ""
            ),
            correction_of_revision_id=(
                current_reaction.revision_id
                if current_reaction is not None and reaction.kind in _CORRECTION_KINDS
                else ""
            ),
            created_at=recorded_at,
        )
        attribution_revision = self._attribution_revision(
            reaction_revision,
            recorded_at=recorded_at,
        )
        commands = self._commands_for_attribution(
            attribution_revision,
            recorded_at=recorded_at,
        )
        event = CognitiveDataEvent(
            event_id=event_id,
            source_id=reaction.source_event_id,
            asset_id=reaction_id,
            source_kind="user_reaction",
            source_uri=f"mnemos://feedback/source/{reaction.source_event_id}",
            content_hash=reaction.source_content_hash,
            canonical_subject=f"user_reaction_event:{reaction_id}",
            data_type="user_reaction_event",
            producer="feedback_attribution_store",
            intended_consumers=tuple(sorted({command.consumer_id for command in commands})),
            privacy_level="private",
            confidence=1.0 if reaction.kind in EXPLICIT_REACTION_KINDS else 0.5,
            evidence_refs=tuple(
                dict.fromkeys((*reaction.evidence_refs, reaction_revision.revision_id))
            ),
            dedupe_key=f"feedback-reaction:{reaction_id}:{reaction_payload['reaction_input_hash']}",
            created_at=recorded_at,
            retention_policy="cognitive_state",
            metadata={
                "revision_ids": [
                    reaction_revision.revision_id,
                    attribution_revision.revision_id,
                ]
            },
        )
        expected_heads: list[CognitiveHeadPrecondition] = []
        if current_reaction is not None:
            expected_heads.append(
                CognitiveHeadPrecondition.create(
                    object_type=current_reaction.object_type,
                    object_id=current_reaction.object_id,
                    revision_id=current_reaction.revision_id,
                )
            )
        current_attribution = self.state.current_revision(
            "feedback_attribution_record",
            attribution_revision.object_id,
        )
        prior_feedback = inspect_prior_feedback_state(
            self.state,
            current_attribution,
        )
        if current_attribution is not None:
            expected_heads.append(
                CognitiveHeadPrecondition.create(
                    object_type=current_attribution.object_type,
                    object_id=current_attribution.object_id,
                    revision_id=current_attribution.revision_id,
                )
            )
        commit = self.state.unit_of_work().commit(
            revisions=(reaction_revision, attribution_revision),
            event=event,
            commands=commands,
            expected_heads=tuple(expected_heads),
            superseded_feedback_command_ids=tuple(
                str(command["command_id"]) for command in prior_feedback.pending_commands
            ),
        )
        return ReactionReceipt(
            status=commit.status,
            event_id=event_id,
            reaction_id=reaction_id,
            reaction_revision_id=reaction_revision.revision_id,
            attribution_id=attribution_revision.object_id,
            attribution_revision_id=attribution_revision.revision_id,
            command_ids=tuple(command.command_id for command in commands),
            disposition=str(attribution_revision.payload["disposition"]),
        )

    def verify(
        self,
        reaction_revision_id: str,
        principal: PrincipalEnvelope,
    ) -> FeedbackVerification:
        from core.cognitive.feedback_verification import verify_feedback_attribution

        return verify_feedback_attribution(
            self.state,
            reaction_revision_id,
            principal,
        )

    def correct_reaction(
        self,
        reaction: UserReactionInput,
        principal: PrincipalEnvelope,
    ) -> ReactionReceipt:
        """Append an exact correction through the same immutable reaction chain."""

        if not isinstance(reaction, UserReactionInput):
            raise TypeError("reaction must be UserReactionInput")
        if reaction.kind not in _CORRECTION_KINDS:
            raise ValueError("correction requires inaccurate or outdated reaction kind")
        if (
            reaction.correction_of_event_id != reaction.supersedes_event_id
            or not reaction.correction_target_ref
            or not reaction.correction_reason
        ):
            raise ValueError("reaction correction lineage is incomplete")
        return self.record_reaction(reaction, principal)

    def record_objective_outcome(
        self,
        outcome_revision: CognitiveStateRevision,
        principal: PrincipalEnvelope,
    ) -> AttributionReceipt:
        """Attach only an oracle-verified OutcomeMeasurement to attribution."""

        assert_feedback_writes_enabled(self.state.db_path.parent)

        if (
            not isinstance(outcome_revision, CognitiveStateRevision)
            or outcome_revision.object_type != "outcome_measurement"
        ):
            raise TypeError("outcome_revision must be an OutcomeMeasurement revision")
        stored = self.state.revision(outcome_revision.revision_id)
        if stored != outcome_revision:
            raise ValueError("outcome measurement is not the committed canonical revision")
        access = validate_cognitive_access_envelope(
            outcome_revision.payload["access_control"],
            expected_scope_type=outcome_revision.scope_type,
            expected_scope_id=outcome_revision.scope_id,
        )
        authorization = authorize_cognitive_write(
            access,
            principal=principal,
            scope_type=outcome_revision.scope_type,
            scope_id=outcome_revision.scope_id,
        )
        if not authorization.allowed:
            raise PermissionError(f"outcome attribution access denied: {authorization.reason}")
        PredictionRecordStore(self.state).verify_outcome_revision(outcome_revision.revision_id)
        subject_ref = dict(outcome_revision.payload["subject"])
        principal_ref = attribution_principal_ref(access)
        attribution_id = feedback_attribution_id(
            subject_ref=subject_ref,
            scope_type=outcome_revision.scope_type,
            scope_id=outcome_revision.scope_id,
            principal_ref=principal_ref,
        )
        current = self.state.current_revision(
            "feedback_attribution_record",
            attribution_id,
        )
        if current is not None and outcome_revision.revision_id in {
            str(item["revision_id"]) for item in current.payload["outcome_refs"]
        }:
            commands = self._target_commands(current, recorded_at=current.created_at)
            intake = self._training_admission_command(
                current,
                outcome_revision=outcome_revision,
                target_commands=commands,
                recorded_at=current.created_at,
            )
            stored_intake = self.state.command(intake.command_id)
            return AttributionReceipt(
                status="existing",
                attribution_id=current.object_id,
                attribution_revision_id=current.revision_id,
                command_ids=tuple(command.command_id for command in commands),
                disposition=str(current.payload["disposition"]),
                training_admission_command_id=(
                    intake.command_id if stored_intake is not None else ""
                ),
            )
        reaction_heads = tuple(
            revision
            for revision in self.state.current_revisions(
                object_type="user_reaction_event",
                scope_type=outcome_revision.scope_type,
                scope_id=outcome_revision.scope_id,
            )
            if dict(revision.payload["subject_ref"]) == subject_ref
            and attribution_principal_ref(revision.payload["access_control"]) == principal_ref
        )
        reaction_refs = [
            {
                "reaction_id": revision.object_id,
                "revision_id": revision.revision_id,
                "payload_hash": revision.payload_hash,
            }
            for revision in sorted(reaction_heads, key=lambda item: item.object_id)
        ]
        prior_outcomes = list(current.payload["outcome_refs"]) if current is not None else []
        corrected_outcome_revision_id = str(
            outcome_revision.correction_of_revision_id or ""
        )
        correction_of_attribution_revision_id = ""
        superseded_feedback_command_ids: tuple[str, ...] = ()
        if (
            current is not None
            and corrected_outcome_revision_id
            and any(
                str(item["revision_id"])
                == corrected_outcome_revision_id
                for item in prior_outcomes
            )
        ):
            correction_of_attribution_revision_id = current.revision_id
            prior_state = inspect_prior_feedback_state(self.state, current)
            superseded_feedback_command_ids = tuple(
                str(command["command_id"])
                for command in prior_state.pending_commands
                if command["command_type"]
                == "evaluate_feedback_target"
                and command["consumer_id"] in FEEDBACK_TARGETS
            )
        outcome_refs = sorted(
            [
                *prior_outcomes,
                {
                    "outcome_id": outcome_revision.object_id,
                    "revision_id": outcome_revision.revision_id,
                    "payload_hash": outcome_revision.payload_hash,
                },
            ],
            key=lambda item: (str(item["outcome_id"]), str(item["revision_id"])),
        )
        method = {
            "name": FEEDBACK_ATTRIBUTION_METHOD,
            "version": "v1",
            "code_hash": FEEDBACK_ATTRIBUTION_CODE_HASH,
            "spec_hash": FEEDBACK_ATTRIBUTION_SPEC_HASH,
            "config_hash": FEEDBACK_ATTRIBUTION_CONFIG_HASH,
        }
        registry = {
            "version": FEEDBACK_TARGET_REGISTRY_VERSION,
            "registry_hash": FEEDBACK_TARGET_REGISTRY_HASH,
            "targets": list(FEEDBACK_TARGETS),
        }
        eligible_targets = {"reflection_evidence", "training_evidence"}
        target_dispositions = [
            {
                "target_id": target_id,
                "eligible": target_id in eligible_targets,
                "exclusion_reason": (
                    "" if target_id in eligible_targets else "objective_target_not_eligible"
                ),
                "command_ref": {
                    "command_key": "feedback-target:"
                    + sha256_json(
                        {
                            "attribution_id": attribution_id,
                            "outcome_revision_id": outcome_revision.revision_id,
                            "target_id": target_id,
                        }
                    ).split(":", 1)[1][:32],
                    "command_type": "evaluate_feedback_target",
                },
            }
            for target_id in FEEDBACK_TARGETS
        ]
        independence_keys = sorted(
            {
                "session:"
                + str(revision.payload["exposure"]["session_id"])
                + "|exposure:"
                + str(revision.payload["exposure"]["exposure_id"])
                for revision in reaction_heads
            }
        )
        payload: dict[str, Any] = {
            "schema_version": "mnemos.feedback_attribution_record.v1",
            "attribution_id": attribution_id,
            "revision_state": "current",
            "subject_ref": subject_ref,
            "scope": {
                "type": outcome_revision.scope_type,
                "id": outcome_revision.scope_id,
                "project": str(access["scope"]["project"]),
                "session_id": str(access["scope"]["session_id"]),
            },
            "reaction_refs": reaction_refs,
            "outcome_refs": outcome_refs,
            "input_set_hash": "",
            "independence_keys": independence_keys,
            "method": method,
            "evidence_class": "objective_outcome",
            "materiality": {
                "decision": "objective_only",
                "observation_count": len(reaction_refs),
                "distinct_session_count": len(
                    {str(revision.payload["exposure"]["session_id"]) for revision in reaction_heads}
                ),
                "distinct_exposure_count": len(independence_keys),
                "span_seconds": 0,
                "minimum_event_count": 3,
                "minimum_independence_count": 2,
                "minimum_span_seconds": 86400,
                "conflict_state": "clear",
            },
            "competing_causes": list(outcome_revision.payload["attribution"]["competing_causes"]),
            "uncertainty": dict(outcome_revision.payload["uncertainty"]),
            "disposition": "objective_only",
            "post_neutralization_disposition": "objective_only",
            "target_registry": registry,
            "target_dispositions": target_dispositions,
            "supersedes_revision_id": current.revision_id if current is not None else "",
            "correction_of_revision_id": (
                correction_of_attribution_revision_id
            ),
            "access_control": dict(access),
        }
        payload["input_set_hash"] = attribution_input_set_hash(payload)
        created_at = self._clock()
        event_id = (
            "feedback-outcome-event-"
            + sha256_json(
                {
                    "attribution_id": attribution_id,
                    "outcome_revision_id": outcome_revision.revision_id,
                }
            ).split(":", 1)[1][:32]
        )
        evidence_refs = tuple(
            dict.fromkeys(
                [
                    outcome_revision.revision_id,
                    *(str(item["revision_id"]) for item in reaction_refs),
                ]
            )
        )
        revision = CognitiveStateRevision.create(
            object_type="feedback_attribution_record",
            object_id=attribution_id,
            source_event_id=event_id,
            source_revision_id=outcome_revision.revision_id,
            source_content_hash=outcome_revision.payload_hash,
            scope_type=outcome_revision.scope_type,
            scope_id=outcome_revision.scope_id,
            evidence_refs=evidence_refs,
            payload=payload,
            supersedes_revision_id=current.revision_id if current is not None else "",
            correction_of_revision_id=(
                correction_of_attribution_revision_id
            ),
            created_at=created_at,
        )
        commands = self._target_commands(revision, recorded_at=created_at)
        admission_command = self._training_admission_command(
            revision,
            outcome_revision=outcome_revision,
            target_commands=commands,
            recorded_at=created_at,
        )
        committed_commands = (*commands, admission_command)
        event = CognitiveDataEvent(
            event_id=event_id,
            source_id=outcome_revision.revision_id,
            asset_id=outcome_revision.object_id,
            source_kind="objective_outcome_attribution",
            source_uri=f"mnemos://feedback/outcome/{outcome_revision.revision_id}",
            content_hash=outcome_revision.payload_hash,
            canonical_subject=f"feedback_attribution_record:{attribution_id}",
            data_type="feedback_attribution_record",
            producer="feedback_attribution_store",
            intended_consumers=tuple(
                sorted({command.consumer_id for command in committed_commands})
            ),
            privacy_level="private",
            confidence=1.0,
            evidence_refs=evidence_refs,
            dedupe_key=f"feedback-outcome:{outcome_revision.revision_id}",
            created_at=created_at,
            retention_policy="cognitive_state",
            metadata={"revision_ids": [revision.revision_id]},
        )
        expected_heads = (
            (
                CognitiveHeadPrecondition.create(
                    object_type=current.object_type,
                    object_id=current.object_id,
                    revision_id=current.revision_id,
                ),
            )
            if current is not None
            else ()
        )
        committed = self.state.unit_of_work().commit(
            revisions=(revision,),
            event=event,
            commands=committed_commands,
            expected_heads=expected_heads,
            superseded_feedback_command_ids=(
                superseded_feedback_command_ids
            ),
        )
        return AttributionReceipt(
            status=committed.status,
            attribution_id=attribution_id,
            attribution_revision_id=revision.revision_id,
            command_ids=tuple(command.command_id for command in commands),
            disposition="objective_only",
            training_admission_command_id=admission_command.command_id,
        )

    def process_command(self, command_id: str) -> TargetDispositionReceipt:
        """Close one command as a verified effect or permanent typed failure."""

        assert_feedback_writes_enabled(self.state.db_path.parent)
        normalized_id = str(command_id or "").strip()
        if not normalized_id:
            raise ValueError("command_id is required")
        context_token = _ACTIVE_FEEDBACK_FAILURE_CONTEXT.set(
            (
                id(self),
                id(self.state),
                normalized_id,
                self.__feedback_failure_capability,
            )
        )
        try:
            try:
                return self._process_command_once(normalized_id)
            except FeedbackMigrationInProgress:
                raise
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise
                return self._record_permanent_failure(normalized_id, exc)
            except (
                KeyError,
                PermissionError,
                RuntimeError,
                TypeError,
                ValueError,
                sqlite3.DatabaseError,
            ) as exc:
                return self._record_permanent_failure(normalized_id, exc)
        finally:
            _ACTIVE_FEEDBACK_FAILURE_CONTEXT.reset(context_token)

    def _process_command_once(self, command_id: str) -> TargetDispositionReceipt:
        """Revalidate and execute one target command without classifying errors."""

        assert_feedback_writes_enabled(self.state.db_path.parent)

        normalized_id = str(command_id or "").strip()
        command = self.state.command(normalized_id)
        persisted_effect = self.state.effect_receipt(normalized_id)
        if persisted_effect is not None:
            if command is None:
                raise ValueError("feedback target command does not exist")
            return build_cognitive_update_receipt(self.state, normalized_id)
        if command is None:
            if persisted_effect is None:
                raise ValueError("feedback target command does not exist")
        payload = command["payload"]
        target_id = str(command["consumer_id"])
        command_type = str(command["command_type"])
        if (
            command_type not in {"evaluate_feedback_target", "neutralize_feedback_effect"}
            or target_id not in FEEDBACK_TARGETS
            or payload.get("target_id") != target_id
        ):
            raise ValueError("feedback target command contract mismatch")
        attribution = self.state.revision(str(command["revision_id"]))
        if (
            attribution is None
            or attribution.object_type != "feedback_attribution_record"
            or attribution.revision_id != payload.get("attribution_revision_id")
            or attribution.payload_hash != payload.get("attribution_payload_hash")
        ):
            raise ValueError("feedback target attribution binding mismatch")
        current_attribution = self.state.current_revision(
            "feedback_attribution_record",
            attribution.object_id,
        )
        if (
            current_attribution is None
            or current_attribution.revision_id != attribution.revision_id
        ):
            raise ValueError("stale feedback target command lacks a supersession receipt")
        if command_type == "neutralize_feedback_effect":
            return self._process_neutralization_command(
                command_id=normalized_id,
                target_id=target_id,
                payload=payload,
                attribution=attribution,
            )
        if tuple(
            payload.get("required_target_ids") or ()
        ) != FEEDBACK_TARGETS or attribution.payload["input_set_hash"] != payload.get(
            "input_set_hash"
        ):
            raise ValueError("feedback target command contract mismatch")
        target_rows = [
            item
            for item in attribution.payload["target_dispositions"]
            if item["target_id"] == target_id
        ]
        if len(target_rows) != 1:
            raise ValueError("feedback target disposition is unavailable")
        target = target_rows[0]
        if (
            target["eligible"] != payload.get("eligible")
            or target["exclusion_reason"] != payload.get("exclusion_reason")
            or target["command_ref"]["command_key"] != payload.get("command_key")
        ):
            raise ValueError("feedback target disposition binding mismatch")
        if target["eligible"]:
            adapter = self._target_adapters.get(target_id)
            if adapter is None:
                raise RuntimeError("eligible feedback target requires a reciprocal domain adapter")
            self.state._start_feedback_command_attempt(
                normalized_id,
                created_at=self._clock(),
            )
            target_effect = invoke_target_adapter(
                adapter=adapter,
                operation="apply",
                payload=payload,
            )
            if (
                not isinstance(target_effect, FeedbackTargetEffect)
                or target_effect.target_id != target_id
                or target_effect.disposition
                not in {
                    "committed_effect",
                    "proposal_committed",
                    "suppressed",
                    "compensated",
                }
                or not target_effect.target_effect_id
                or not target_effect.before_hash.startswith("sha256:")
                or not target_effect.after_hash.startswith("sha256:")
                or not target_effect.target_receipt_ref
            ):
                raise ValueError("feedback target reciprocal receipt verification failed")
            effect = self.state._record_feedback_effect_receipt(
                normalized_id,
                effect=target_effect,
                attribution_revision_id=attribution.revision_id,
                created_at=self._clock(),
            )
            return build_cognitive_update_receipt(
                self.state,
                normalized_id,
                command=command,
                effect=effect,
                attribution=attribution,
                disposition=target_effect.disposition,
            )
        effect = self.state._record_feedback_ineligible_receipt(
            normalized_id,
            attribution_revision_id=attribution.revision_id,
            created_at=self._clock(),
        )
        return build_cognitive_update_receipt(
            self.state,
            normalized_id,
            command=command,
            effect=effect,
            attribution=attribution,
            disposition="intentional_skip",
        )

    def _process_neutralization_command(
        self,
        *,
        command_id: str,
        target_id: str,
        payload: Mapping[str, Any],
        attribution: CognitiveStateRevision,
    ) -> TargetDispositionReceipt:
        if payload.get(
            "schema_version"
        ) != "mnemos.feedback_neutralization_command.v1" or payload.get(
            "neutralization_kind"
        ) not in {
            "suppress",
            "revoke",
            "compensate",
        }:
            raise ValueError("feedback neutralization command contract mismatch")
        chain_revision_ids = {
            revision.revision_id
            for revision in self.state.revision_chain(
                attribution.object_type,
                attribution.object_id,
            )
        }
        if (
            payload.get("prior_attribution_revision_id") not in chain_revision_ids
            or payload.get("prior_attribution_revision_id") == attribution.revision_id
        ):
            raise ValueError("feedback neutralization source is outside the attribution chain")
        prior_effect = self.state.effect_receipt(str(payload.get("prior_command_id") or ""))
        if (
            prior_effect is None
            or prior_effect["receipt_id"] != payload.get("prior_effect_receipt_id")
            or prior_effect["target_effect_id"] != payload.get("prior_target_effect_id")
            or prior_effect["consumer_id"] != target_id
            or prior_effect["after_hash"] != payload.get("prior_after_hash")
            or prior_effect["status"] != "committed"
        ):
            raise ValueError("feedback neutralization source effect mismatch")
        adapter = self._target_adapters.get(target_id)
        if adapter is None:
            raise RuntimeError("feedback neutralization requires a reciprocal domain adapter")
        self.state._start_feedback_command_attempt(
            command_id,
            created_at=self._clock(),
        )
        target_receipt_refs = tuple(
            str(ref)
            for ref in prior_effect["evidence_refs"]
            if str(ref).startswith(f"domain-feedback-receipt:{target_id}:")
        )
        if len(target_receipt_refs) != 1:
            raise ValueError("feedback neutralization lacks exact domain receipt")
        adapter_payload = dict(payload)
        adapter_payload["prior_target_receipt_ref"] = target_receipt_refs[0]
        target_effect = invoke_target_adapter(
            adapter=adapter,
            operation="neutralize",
            payload=adapter_payload,
        )
        expected_disposition = {
            "suppress": "suppressed",
            "revoke": "revoked",
            "compensate": "compensated",
        }[str(payload["neutralization_kind"])]
        if (
            not isinstance(target_effect, FeedbackTargetEffect)
            or target_effect.target_id != target_id
            or target_effect.disposition != expected_disposition
            or target_effect.before_hash != payload.get("prior_after_hash")
            or not target_effect.after_hash.startswith("sha256:")
            or not target_effect.target_effect_id
            or not target_effect.target_receipt_ref
        ):
            raise ValueError("feedback neutralization receipt verification failed")
        effect = self.state._record_feedback_effect_receipt(
            command_id,
            effect=target_effect,
            attribution_revision_id=attribution.revision_id,
            created_at=self._clock(),
        )
        command = self.state.command(command_id)
        if command is None:
            raise ValueError("feedback neutralization command disappeared")
        return build_cognitive_update_receipt(
            self.state,
            command_id,
            command=command,
            effect=effect,
            attribution=attribution,
            disposition=target_effect.disposition,
        )

    def _record_permanent_failure(
        self,
        command_id: str,
        error: Exception,
    ) -> TargetDispositionReceipt:
        return _record_permanent_feedback_failure(self, command_id, error)

    def _feedback_failure_context_matches(self, command_id: str) -> bool:
        active = _ACTIVE_FEEDBACK_FAILURE_CONTEXT.get()
        return bool(
            active is not None
            and active[0] == id(self)
            and active[1] == id(self.state)
            and active[2] == str(command_id or "")
            and self.state._feedback_terminal_capability_matches(id(self), active[3])
        )

    def replay_pending(self, limit: int = 100) -> ReplayBatchReceipt:
        """Converge feedback-owned work using stable keyset pages."""

        normalized_limit = int(limit)
        if normalized_limit <= 0:
            raise ValueError("replay limit must be positive")
        receipts: list[TargetDispositionReceipt] = []
        page_count = 0
        after_created_at = ""
        after_command_id = ""
        while True:
            page = self.state.pending_commands_page(
                after_created_at=after_created_at,
                after_command_id=after_command_id,
                revision_object_type="feedback_attribution_record",
                limit=normalized_limit,
            )
            if not page:
                reconciled = 0
                for revision in self.state.current_revisions(
                    object_type="feedback_attribution_record"
                ):
                    if revision.payload.get("disposition") != "correction_pending":
                        continue
                    result = self.reconcile_subject(
                        revision.payload["subject_ref"],
                        self._clock(),
                        attribution_id=revision.object_id,
                    )
                    if result.status == "committed" and result.command_ids:
                        reconciled += 1
                if not reconciled:
                    break
                after_created_at = ""
                after_command_id = ""
                continue
            page_count += 1
            selected = [
                command
                for command in page
                if not (
                    command.get("consumer_id") == TRAINING_ADMISSION_CONSUMER
                    and command.get("command_type") == TRAINING_ADMISSION_COMMAND
                )
            ]
            page_receipts: dict[str, TargetDispositionReceipt] = {}
            selected_by_id = {str(command["command_id"]): command for command in selected}
            batchable = [
                command
                for command in selected
                if command.get("command_type") == "evaluate_feedback_target"
                and command.get("consumer_id") in FEEDBACK_TARGETS
                and command.get("payload", {}).get("eligible") is False
            ]
            if batchable:
                try:
                    closed = self.state.close_ineligible_feedback_commands(
                        [str(command["command_id"]) for command in batchable],
                        registered_targets=FEEDBACK_TARGETS,
                        registry_hash=FEEDBACK_TARGET_REGISTRY_HASH,
                        created_at=self._clock(),
                    )
                except ValueError:
                    for command in batchable:
                        command_id = str(command["command_id"])
                        page_receipts[command_id] = self.process_command(command_id)
                else:
                    for item in closed:
                        command_id = str(item["command_id"])
                        page_receipts[command_id] = build_ineligible_cognitive_update_receipt(
                            selected_by_id[command_id],
                            item,
                        )
            for command in selected:
                command_id = str(command["command_id"])
                receipt = page_receipts.get(command_id)
                if receipt is None:
                    receipt = self.process_command(command_id)
                receipts.append(receipt)
            after_created_at = str(page[-1]["created_at"])
            after_command_id = str(page[-1]["command_id"])
        return ReplayBatchReceipt(
            processed_count=len(receipts),
            page_count=page_count,
            command_ids=tuple(receipt.command_id for receipt in receipts),
            dispositions=tuple(receipt.disposition for receipt in receipts),
        )

    def reconcile_subject(
        self,
        subject_ref: Mapping[str, Any],
        now: str | None = None,
        *,
        attribution_id: str = "",
    ) -> AttributionReceipt:
        """Release correction replacement commands only after exact neutralization."""

        assert_feedback_writes_enabled(self.state.db_path.parent)

        if not isinstance(subject_ref, Mapping) or set(subject_ref) != {"type", "id"}:
            raise ValueError("feedback subject_ref is invalid")
        normalized_subject = {"type": str(subject_ref["type"]), "id": str(subject_ref["id"])}
        normalized_attribution_id = str(attribution_id or "").strip()
        if normalized_attribution_id:
            selected = self.state.current_revision(
                "feedback_attribution_record",
                normalized_attribution_id,
            )
            matches: tuple[CognitiveStateRevision, ...] = (
                (selected,)
                if selected is not None
                and dict(selected.payload["subject_ref"]) == normalized_subject
                else ()
            )
        else:
            matches = tuple(
                revision
                for revision in self.state.current_revisions(
                    object_type="feedback_attribution_record"
                )
                if dict(revision.payload["subject_ref"]) == normalized_subject
            )
        if not matches:
            raise ValueError("feedback subject has no current attribution")
        if len(matches) != 1:
            raise ValueError("feedback subject attribution is scope-ambiguous")
        current = matches[0]
        if current.payload["disposition"] != "correction_pending":
            commands = self._target_commands(current, recorded_at=current.created_at)
            return AttributionReceipt(
                status="existing",
                attribution_id=current.object_id,
                attribution_revision_id=current.revision_id,
                command_ids=tuple(command.command_id for command in commands),
                disposition=str(current.payload["disposition"]),
            )
        neutralization_targets = {
            str(item["target_id"])
            for item in current.payload["target_dispositions"]
            if item["command_ref"]["command_type"] == "neutralize_feedback_effect"
        }
        terminal_rows = self.state.effect_receipts_for_revision(current.revision_id)
        terminal = {str(receipt["consumer_id"]): receipt for receipt in terminal_rows}
        correction_target_types = {
            str(item["target_id"]): str(item["command_ref"]["command_type"])
            for item in current.payload["target_dispositions"]
        }
        correction_receipts_complete = bool(
            len(terminal_rows) == len(FEEDBACK_TARGETS)
            and set(terminal) == set(FEEDBACK_TARGETS)
            and all(
                (
                    (
                        receipt["status"] in {"committed", "revoked"}
                        and receipt.get("consumption_outcome")
                        in {"suppressed", "revoked", "compensated"}
                    )
                    if correction_target_types[target_id] == "neutralize_feedback_effect"
                    else (
                        receipt["status"] == "intentional_skip"
                        and receipt.get("consumption_outcome")
                        == next(
                            str(item["exclusion_reason"])
                            for item in current.payload["target_dispositions"]
                            if str(item["target_id"]) == target_id
                        )
                        and receipt["before_hash"] == receipt["after_hash"]
                    )
                )
                for target_id, receipt in terminal.items()
            )
        )
        if (
            set(neutralization_targets)
            != {
                target_id
                for target_id, command_type in correction_target_types.items()
                if command_type == "neutralize_feedback_effect"
            }
            or not correction_receipts_complete
        ):
            return AttributionReceipt(
                status="compensation_pending",
                attribution_id=current.object_id,
                attribution_revision_id=current.revision_id,
                command_ids=(),
                disposition="compensation_pending",
            )
        created_at = str(now or self._clock())
        payload = json.loads(json.dumps(dict(current.payload), ensure_ascii=False, sort_keys=True))
        payload["revision_state"] = "current"
        desired_disposition = str(payload["post_neutralization_disposition"])
        if desired_disposition not in {"proposal_eligible", "record_only"}:
            raise ValueError("feedback correction replacement disposition is invalid")
        payload["disposition"] = desired_disposition
        payload["materiality"]["decision"] = desired_disposition
        payload["target_dispositions"] = [
            {
                "target_id": target_id,
                "eligible": desired_disposition == "proposal_eligible",
                "exclusion_reason": (
                    "" if desired_disposition == "proposal_eligible" else "feedback_record_only"
                ),
                "command_ref": {
                    "command_key": "feedback-target:"
                    + sha256_json(
                        {
                            "attribution_id": current.object_id,
                            "correction_revision_id": current.revision_id,
                            "target_id": target_id,
                            "command_type": "evaluate_feedback_target",
                        }
                    ).split(":", 1)[1][:32],
                    "command_type": "evaluate_feedback_target",
                },
            }
            for target_id in FEEDBACK_TARGETS
        ]
        payload["supersedes_revision_id"] = current.revision_id
        payload["correction_of_revision_id"] = current.revision_id
        payload["input_set_hash"] = attribution_input_set_hash(payload)
        proof = {
            "correction_attribution_revision_id": current.revision_id,
            "neutralization_receipt_ids": sorted(
                str(receipt["receipt_id"]) for receipt in terminal.values()
            ),
            "activated_at": created_at,
        }
        source_hash = sha256_json(proof)
        event_id = "feedback-reconcile-event-" + source_hash.split(":", 1)[1][:32]
        evidence_refs = tuple(
            dict.fromkeys(
                [
                    *(str(item["revision_id"]) for item in payload["reaction_refs"]),
                    *(
                        f"cognitive-effect-receipt:{receipt['receipt_id']}"
                        for receipt in terminal.values()
                    ),
                ]
            )
        )
        revision = CognitiveStateRevision.create(
            object_type="feedback_attribution_record",
            object_id=current.object_id,
            source_event_id=event_id,
            source_revision_id=f"feedback-reconcile:{current.revision_id}",
            source_content_hash=source_hash,
            scope_type=current.scope_type,
            scope_id=current.scope_id,
            evidence_refs=evidence_refs,
            payload=payload,
            supersedes_revision_id=current.revision_id,
            correction_of_revision_id=current.revision_id,
            created_at=created_at,
        )
        commands = self._target_commands(revision, recorded_at=created_at)
        event = CognitiveDataEvent(
            event_id=event_id,
            source_id=current.revision_id,
            asset_id=current.object_id,
            source_kind="feedback_correction_reconciliation",
            source_uri=f"mnemos://feedback/reconcile/{current.revision_id}",
            content_hash=source_hash,
            canonical_subject=f"feedback_attribution_record:{current.object_id}",
            data_type="feedback_attribution_record",
            producer="feedback_attribution_store",
            intended_consumers=FEEDBACK_TARGETS,
            privacy_level="private",
            confidence=1.0,
            evidence_refs=evidence_refs,
            dedupe_key=f"feedback-reconcile:{current.revision_id}",
            created_at=created_at,
            retention_policy="cognitive_state",
            metadata={"revision_ids": [revision.revision_id]},
        )
        committed = self.state.unit_of_work().commit(
            revisions=(revision,),
            event=event,
            commands=commands,
            expected_heads=(
                CognitiveHeadPrecondition.create(
                    object_type=current.object_type,
                    object_id=current.object_id,
                    revision_id=current.revision_id,
                ),
            ),
        )
        return AttributionReceipt(
            status=committed.status,
            attribution_id=revision.object_id,
            attribution_revision_id=revision.revision_id,
            command_ids=tuple(command.command_id for command in commands),
            disposition=desired_disposition,
        )

    def _attribution_revision(
        self,
        reaction_revision: CognitiveStateRevision,
        *,
        recorded_at: str,
    ) -> CognitiveStateRevision:
        subject_ref = dict(reaction_revision.payload["subject_ref"])
        principal_ref = attribution_principal_ref(reaction_revision.payload["access_control"])
        attribution_id = feedback_attribution_id(
            subject_ref=subject_ref,
            scope_type=reaction_revision.scope_type,
            scope_id=reaction_revision.scope_id,
            principal_ref=principal_ref,
        )
        current_attribution = self.state.current_revision(
            "feedback_attribution_record",
            attribution_id,
        )
        prior_feedback = inspect_prior_feedback_state(
            self.state,
            current_attribution,
        )
        reaction_heads = {
            revision.object_id: revision
            for revision in self.state.current_revisions(
                object_type="user_reaction_event",
                scope_type=reaction_revision.scope_type,
                scope_id=reaction_revision.scope_id,
            )
            if dict(revision.payload["subject_ref"]) == subject_ref
            and attribution_principal_ref(revision.payload["access_control"]) == principal_ref
        }
        reaction_heads[reaction_revision.object_id] = reaction_revision
        ordered_reactions = tuple(reaction_heads[key] for key in sorted(reaction_heads))
        reaction_refs = [
            {
                "reaction_id": revision.object_id,
                "revision_id": revision.revision_id,
                "payload_hash": revision.payload_hash,
            }
            for revision in ordered_reactions
        ]
        independence_keys = sorted(
            {
                "session:"
                + str(revision.payload["exposure"]["session_id"])
                + "|exposure:"
                + str(revision.payload["exposure"]["exposure_id"])
                for revision in ordered_reactions
            }
        )
        observed = sorted(
            datetime.fromisoformat(
                str(revision.payload["observed_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            for revision in ordered_reactions
        )
        evidence_classes = {
            str(revision.payload["attribution"]["evidence_class"]) for revision in ordered_reactions
        }
        distinct_session_count = len(
            {str(revision.payload["exposure"]["session_id"]) for revision in ordered_reactions}
        )
        distinct_exposure_count = len(
            {str(revision.payload["exposure"]["exposure_id"]) for revision in ordered_reactions}
        )
        span_seconds = int((observed[-1] - observed[0]).total_seconds())
        weak_materiality_met = bool(
            len(ordered_reactions) >= 3
            and max(distinct_session_count, distinct_exposure_count) >= 2
            and span_seconds >= 86400
        )
        if "explicit_correction" in evidence_classes:
            evidence_class = "explicit_correction"
            desired_disposition = "proposal_eligible"
        elif evidence_classes == {"weak_behavior"}:
            evidence_class = "weak_behavior"
            desired_disposition = "proposal_eligible" if weak_materiality_met else "record_only"
        else:
            evidence_class = "explicit_preference"
            desired_disposition = "record_only"
        disposition = (
            "correction_pending" if prior_feedback.active_effects_by_target else desired_disposition
        )
        eligible = desired_disposition == "proposal_eligible"
        method = {
            "name": FEEDBACK_ATTRIBUTION_METHOD,
            "version": "v1",
            "code_hash": FEEDBACK_ATTRIBUTION_CODE_HASH,
            "spec_hash": FEEDBACK_ATTRIBUTION_SPEC_HASH,
            "config_hash": FEEDBACK_ATTRIBUTION_CONFIG_HASH,
        }
        registry = {
            "version": FEEDBACK_TARGET_REGISTRY_VERSION,
            "registry_hash": FEEDBACK_TARGET_REGISTRY_HASH,
            "targets": list(FEEDBACK_TARGETS),
        }
        prior_by_target = dict(prior_feedback.active_effects_by_target)
        correction_requires_neutralization = disposition == "correction_pending" and bool(
            prior_by_target
        )
        target_dispositions = []
        for target_id in FEEDBACK_TARGETS:
            prior_effect = prior_by_target.get(target_id)
            if correction_requires_neutralization and prior_effect is not None:
                target_eligible = True
                exclusion_reason = ""
                command_type = "neutralize_feedback_effect"
                command_identity: Mapping[str, Any] = {
                    "prior_effect_receipt_id": prior_effect["receipt_id"],
                    "prior_target_effect_id": prior_effect["target_effect_id"],
                }
            elif correction_requires_neutralization:
                target_eligible = False
                exclusion_reason = "no_prior_active_effect"
                command_type = "evaluate_feedback_target"
                command_identity = {"no_prior_active_effect": True}
            else:
                target_eligible = eligible
                exclusion_reason = "" if eligible else "feedback_record_only"
                command_type = "evaluate_feedback_target"
                command_identity = {"reaction_refs": reaction_refs}
            target_dispositions.append(
                {
                    "target_id": target_id,
                    "eligible": target_eligible,
                    "exclusion_reason": exclusion_reason,
                    "command_ref": {
                        "command_key": "feedback-target:"
                        + sha256_json(
                            {
                                "attribution_id": attribution_id,
                                "target_id": target_id,
                                "command_type": command_type,
                                **command_identity,
                            }
                        ).split(":", 1)[1][:32],
                        "command_type": command_type,
                    },
                }
            )
        payload: dict[str, Any] = {
            "schema_version": "mnemos.feedback_attribution_record.v1",
            "attribution_id": attribution_id,
            "revision_state": "current",
            "subject_ref": subject_ref,
            "scope": dict(reaction_revision.payload["scope"]),
            "reaction_refs": reaction_refs,
            "outcome_refs": [],
            "input_set_hash": "",
            "independence_keys": independence_keys,
            "method": method,
            "evidence_class": evidence_class,
            "materiality": {
                "decision": disposition,
                "observation_count": len(ordered_reactions),
                "distinct_session_count": distinct_session_count,
                "distinct_exposure_count": distinct_exposure_count,
                "span_seconds": span_seconds,
                "minimum_event_count": 3,
                "minimum_independence_count": 2,
                "minimum_span_seconds": 86400,
                "conflict_state": "clear",
            },
            "competing_causes": [],
            "uncertainty": {"kind": "conservative", "value": None},
            "disposition": disposition,
            "post_neutralization_disposition": desired_disposition,
            "target_registry": registry,
            "target_dispositions": target_dispositions,
            "supersedes_revision_id": (
                current_attribution.revision_id if current_attribution is not None else ""
            ),
            "correction_of_revision_id": (
                current_attribution.revision_id
                if current_attribution is not None
                and (disposition == "correction_pending" or bool(prior_feedback.pending_commands))
                else ""
            ),
            "access_control": dict(reaction_revision.payload["access_control"]),
        }
        payload["input_set_hash"] = attribution_input_set_hash(payload)
        return CognitiveStateRevision.create(
            object_type="feedback_attribution_record",
            object_id=attribution_id,
            source_event_id=reaction_revision.source_event_id,
            source_revision_id=reaction_revision.revision_id,
            source_content_hash=reaction_revision.source_content_hash,
            scope_type=reaction_revision.scope_type,
            scope_id=reaction_revision.scope_id,
            evidence_refs=tuple(ref["revision_id"] for ref in reaction_refs),
            payload=payload,
            supersedes_revision_id=(
                current_attribution.revision_id if current_attribution is not None else ""
            ),
            correction_of_revision_id=(
                current_attribution.revision_id
                if current_attribution is not None
                and (disposition == "correction_pending" or bool(prior_feedback.pending_commands))
                else ""
            ),
            created_at=recorded_at,
        )

    @staticmethod
    def _training_admission_command(
        attribution: CognitiveStateRevision,
        *,
        outcome_revision: CognitiveStateRevision,
        target_commands: tuple[LocalConsumerCommand, ...],
        recorded_at: str,
    ) -> LocalConsumerCommand:
        """Bind one durable training obligation to the objective attribution."""
        return derive_training_admission_intake_command(
            attribution,
            outcome_revision=outcome_revision,
            target_commands=target_commands,
            recorded_at=recorded_at,
        )

    def _commands_for_attribution(
        self,
        attribution: CognitiveStateRevision,
        *,
        recorded_at: str,
    ) -> tuple[LocalConsumerCommand, ...]:
        neutralization = self._neutralization_commands(
            attribution,
            recorded_at=recorded_at,
        )
        evaluation = self._target_commands(attribution, recorded_at=recorded_at)
        return neutralization + evaluation

    def _existing_receipt(
        self,
        reaction_revision: CognitiveStateRevision,
    ) -> ReactionReceipt:
        subject_ref = dict(reaction_revision.payload["subject_ref"])
        attribution_id = feedback_attribution_id(
            subject_ref=subject_ref,
            scope_type=reaction_revision.scope_type,
            scope_id=reaction_revision.scope_id,
            principal_ref=attribution_principal_ref(reaction_revision.payload["access_control"]),
        )
        attribution = self.state.current_revision(
            "feedback_attribution_record",
            attribution_id,
        )
        if attribution is None or reaction_revision.revision_id not in {
            str(item["revision_id"]) for item in attribution.payload["reaction_refs"]
        }:
            raise RuntimeError("active reaction lacks current canonical attribution")
        commands = self._commands_for_attribution(
            attribution,
            recorded_at=attribution.created_at,
        )
        return ReactionReceipt(
            status="existing",
            event_id=reaction_revision.source_event_id,
            reaction_id=reaction_revision.object_id,
            reaction_revision_id=reaction_revision.revision_id,
            attribution_id=attribution.object_id,
            attribution_revision_id=attribution.revision_id,
            command_ids=tuple(command.command_id for command in commands),
            disposition=str(attribution.payload["disposition"]),
        )
