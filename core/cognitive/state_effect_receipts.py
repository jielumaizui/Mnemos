"""Outbox paging and independently verified effect-receipt closure."""

from __future__ import annotations

from contextvars import ContextVar
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence, TYPE_CHECKING

from core.cognitive.state_contract import (
    CognitiveStateRevision,
    canonical_json,
    now_utc,
    sha256_json,
)
from core.cognitive.state_types import (
    COGNITIVE_TOMBSTONE_COMMAND_TYPE,
    CognitiveEffectReceipt,
    CognitiveStateConflict,
)
from core.cognitive.feedback_owner_identity import (
    feedback_failure_context_matches_state,
)
from core.cognitive.state_effect_receipt_queries import StateEffectReceiptQueryMixin
from core.cognitive.training_governance_projection_audit import (
    validate_admission_projection_receipt,
)
from core.ops.cognitive_event_ledger import insert_data_consumption_in_connection

_ACTIVE_FEEDBACK_TERMINAL_FAILURE: ContextVar[tuple[int, str, object] | None] = ContextVar(
    "active_feedback_terminal_failure", default=None
)

_COGNITION_EPISODE_COMMAND = "project_cognition_episode"
_COGNITION_EPISODE_CONSUMERS = frozenset({"wiki", "knowledge_graph", "cognitive_graph"})
_DEMO_OMISSION_REASON = "synthetic_fixture_source_not_in_canonical_raw"
_DEMO_OMISSION_OUTCOME = "synthetic demo object retired without projection"


class CognitiveStateEffectReceiptMixin(StateEffectReceiptQueryMixin):
    """Read and append specialized terminal state command receipts."""

    if TYPE_CHECKING:
        config: Any | None
        db_path: Path

        def _connect(self, *, read_only: bool = False) -> sqlite3.Connection: ...

        def revision(self, revision_id: str) -> CognitiveStateRevision | None: ...

        def current_revision(
            self,
            object_type: str,
            object_id: str,
        ) -> CognitiveStateRevision | None: ...

    def record_effect_receipt(
        self,
        command_id: str,
        *,
        status: str,
        target_effect_id: str,
        before_hash: str = "",
        after_hash: str = "",
        evidence_refs: Sequence[str],
        outcome: str = "",
        terminal_reason_code: str = "",
        retry_exhausted: bool = False,
        created_at: str = "",
    ) -> CognitiveEffectReceipt:
        """Close a non-feedback command or a feedback command with no effect."""

        command = self.command(str(command_id or ""))
        revision = None if command is None else self.revision(str(command.get("revision_id") or ""))
        if command is not None and command.get("command_type") == _COGNITION_EPISODE_COMMAND:
            raise PermissionError(
                "cognition episode commands require a specialized projection receipt"
            )
        governed_training_intake = False
        if command is not None:
            from core.cognitive.training_contract import (
                TRAINING_ADMISSION_COMMAND,
                TRAINING_ADMISSION_CONSUMER,
                validate_training_admission_intake_payload,
            )

            if (
                command.get("consumer_id") == TRAINING_ADMISSION_CONSUMER
                and command.get("command_type") == TRAINING_ADMISSION_COMMAND
            ):
                validate_training_admission_intake_payload(command["payload"])
                attribution_ref = command["payload"]["attribution_ref"]
                governed_training_intake = bool(
                    revision is not None
                    and revision.object_id == attribution_ref["object_id"]
                    and revision.revision_id == attribution_ref["revision_id"]
                    and revision.payload_hash == attribution_ref["payload_hash"]
                )
        if (
            revision is not None
            and revision.object_type == "feedback_attribution_record"
            and not governed_training_intake
        ):
            raise PermissionError("feedback commands require canonical specialized receipt closure")
        return self._record_effect_receipt(
            command_id,
            status=status,
            target_effect_id=target_effect_id,
            before_hash=before_hash,
            after_hash=after_hash,
            evidence_refs=evidence_refs,
            outcome=outcome,
            terminal_reason_code=terminal_reason_code,
            retry_exhausted=retry_exhausted,
            created_at=created_at,
        )

    def record_cognition_episode_projection_receipt(
        self,
        command_id: str,
        *,
        proof: Any,
    ) -> CognitiveEffectReceipt:
        """Close one episode consumer only after its fixed target oracle agrees."""

        from core.cognitive.cognition_episode_projection_receipt import (
            CognitionEpisodeProjectionProof,
            CognitionEpisodeProjectionTargets,
            verify_cognition_episode_projection,
        )

        if not isinstance(proof, CognitionEpisodeProjectionProof):
            raise TypeError("cognition episode projection proof type mismatch")
        command = self.command(str(command_id or ""))
        if command is None:
            raise LookupError("cognition episode projection command is missing")
        revision = self.revision(str(command.get("revision_id") or ""))
        if revision is None:
            raise LookupError("cognition episode projection revision is missing")
        targets = CognitionEpisodeProjectionTargets.from_config(
            self.config,
            state_db_path=self.db_path,
        )
        verified = verify_cognition_episode_projection(
            targets=targets,
            command=command,
            revision=revision,
            proof=proof,
        )
        return self._record_effect_receipt(
            str(command["command_id"]),
            status="committed",
            target_effect_id=proof.effect_id,
            before_hash=proof.before_hash,
            after_hash=proof.after_hash,
            evidence_refs=verified["evidence_refs"],
            outcome=str(verified["outcome"]),
            created_at=str(command["created_at"]),
        )

    def record_cognition_episode_omission_receipt(
        self,
        command_id: str,
        *,
        quarantine_id: str,
    ) -> CognitiveEffectReceipt:
        """Close a retired synthetic episode only from its exact quarantine row."""

        normalized_quarantine = str(quarantine_id or "").strip()
        if not normalized_quarantine:
            raise ValueError("cognition episode omission quarantine_id is required")
        with self._connect(read_only=True) as conn:
            quarantine = conn.execute(
                """SELECT payload_json
                   FROM cognitive_state_migration_quarantine
                   WHERE quarantine_id=?""",
                (normalized_quarantine,),
            ).fetchone()
        if quarantine is None:
            raise LookupError("cognition episode omission quarantine is missing")
        try:
            payload = json.loads(str(quarantine["payload_json"]))
            if not isinstance(payload, Mapping):
                raise TypeError("quarantine payload is not an object")
            fixture_source_hash = str(payload["fixture_source_hash"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("cognition episode omission quarantine payload is invalid") from exc
        return self._record_effect_receipt(
            command_id,
            status="intentional_skip",
            target_effect_id=("retired-demo-fixture:" + str(payload.get("revision_id") or "")),
            evidence_refs=(
                f"cognition-revision:{payload.get('revision_id') or ''}",
                f"cognitive-quarantine:{normalized_quarantine}",
                f"demo-fixture-source:{fixture_source_hash}",
            ),
            outcome=_DEMO_OMISSION_OUTCOME,
            terminal_reason_code=_DEMO_OMISSION_REASON,
        )

    def _validate_cognition_episode_omission_receipt_derivation(
        self,
        conn: sqlite3.Connection,
        command: Mapping[str, Any],
        *,
        status: str,
        target_effect_id: str,
        before_hash: str,
        after_hash: str,
        evidence_refs: Sequence[str],
        outcome: str,
        terminal_reason_code: str,
    ) -> None:
        """Re-derive the only permitted episode omission inside the write transaction."""

        revision_id = str(command["revision_id"])
        object_id = str(command["object_id"])
        command_id = str(command["command_id"])
        consumer_id = str(command["consumer_id"])
        try:
            command_payload = json.loads(str(command["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("cognition episode omission command payload is invalid") from exc
        expected_command_payload = {
            "primary_revision_id": revision_id,
            "object_type": "cognition_episode",
            "object_id": object_id,
        }
        quarantine_refs = [
            ref for ref in evidence_refs if str(ref).startswith("cognitive-quarantine:")
        ]
        if len(quarantine_refs) != 1:
            raise ValueError("cognition episode omission requires one exact quarantine ref")
        quarantine_id = quarantine_refs[0].split(":", 1)[1]
        quarantine = conn.execute(
            """SELECT source_table, source_key, reason_code, field_manifest,
                      payload_json, payload_hash
               FROM cognitive_state_migration_quarantine
               WHERE quarantine_id=?""",
            (quarantine_id,),
        ).fetchone()
        if quarantine is None:
            raise ValueError("cognition episode omission quarantine is missing")
        try:
            field_manifest = json.loads(str(quarantine["field_manifest"]))
            quarantine_payload = json.loads(str(quarantine["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("cognition episode omission quarantine is malformed") from exc
        if not isinstance(field_manifest, Mapping) or not isinstance(quarantine_payload, Mapping):
            raise ValueError("cognition episode omission quarantine is malformed")
        fixture_source_hash = str(quarantine_payload.get("fixture_source_hash") or "")
        expected_refs = {
            f"cognition-revision:{revision_id}",
            f"cognitive-quarantine:{quarantine_id}",
            f"demo-fixture-source:{fixture_source_hash}",
        }
        head = conn.execute(
            """SELECT 1 FROM cognitive_state_heads
               WHERE object_type='cognition_episode' AND object_id=?""",
            (object_id,),
        ).fetchone()
        if any(
            (
                str(command["object_type"]) != "cognition_episode",
                consumer_id not in _COGNITION_EPISODE_CONSUMERS,
                command_payload != expected_command_payload,
                str(command["payload_hash"]) != sha256_json(expected_command_payload),
                status != "intentional_skip",
                target_effect_id != f"retired-demo-fixture:{revision_id}",
                bool(before_hash),
                bool(after_hash),
                outcome != _DEMO_OMISSION_OUTCOME,
                terminal_reason_code != _DEMO_OMISSION_REASON,
                len(evidence_refs) != len(expected_refs),
                set(evidence_refs) != expected_refs,
                str(quarantine["source_table"]) != "cognitive_state_revisions",
                str(quarantine["source_key"]) != revision_id,
                str(quarantine["reason_code"]) != _DEMO_OMISSION_REASON,
                str(quarantine["payload_hash"]) != str(command["revision_payload_hash"]),
                command_id not in field_manifest.get("command_ids", ()),
                set(quarantine_payload)
                != {
                    "schema_version",
                    "object_type",
                    "object_id",
                    "revision_id",
                    "payload_hash",
                    "fixture_source_hash",
                },
                quarantine_payload.get("schema_version") != "mnemos.demo_fixture_quarantine.v1",
                quarantine_payload.get("object_type") != "cognition_episode",
                quarantine_payload.get("object_id") != object_id,
                quarantine_payload.get("revision_id") != revision_id,
                quarantine_payload.get("payload_hash") != str(command["revision_payload_hash"]),
                len(fixture_source_hash) != 71,
                not fixture_source_hash.startswith("sha256:"),
                any(value not in "0123456789abcdef" for value in fixture_source_hash[7:]),
                head is not None,
            )
        ):
            raise ValueError("cognition episode omission derivation mismatch")

    def _record_feedback_effect_receipt(
        self,
        command_id: str,
        *,
        effect: Any,
        attribution_revision_id: str,
        created_at: str = "",
    ) -> CognitiveEffectReceipt:
        """Close a feedback effect after fixed-registry domain verification."""

        command, attribution = self._feedback_command_context(
            command_id,
            attribution_revision_id=attribution_revision_id,
        )
        command_type = str(command.get("command_type") or "")
        if command_type not in {
            "evaluate_feedback_target",
            "neutralize_feedback_effect",
        }:
            raise ValueError("command is not a feedback effect command")
        target_id = str(command.get("consumer_id") or "")
        if (
            str(getattr(effect, "target_id", "")) != target_id
            or not str(getattr(effect, "target_effect_id", "")).strip()
            or not str(getattr(effect, "before_hash", "")).startswith("sha256:")
            or not str(getattr(effect, "after_hash", "")).startswith("sha256:")
        ):
            raise ValueError("feedback effect is not bound to its command target")
        disposition = str(getattr(effect, "disposition", ""))
        allowed_dispositions = (
            {"suppressed", "revoked", "compensated"}
            if command_type == "neutralize_feedback_effect"
            else {"committed_effect", "proposal_committed", "suppressed", "compensated"}
        )
        if disposition not in allowed_dispositions:
            raise ValueError("feedback effect disposition is invalid for its command")
        target_receipt_ref = str(getattr(effect, "target_receipt_ref", ""))
        if not target_receipt_ref.startswith(f"domain-feedback-receipt:{target_id}:"):
            raise ValueError("feedback effect lacks an exact domain receipt")
        from core.cognitive.feedback_models import feedback_entity_evidence_ref

        decision_trace_refs = tuple(getattr(effect, "decision_trace_refs", ()) or ())
        action_refs = tuple(getattr(effect, "action_refs", ()) or ())
        if disposition == "proposal_committed" and (
            len(decision_trace_refs) != 1 or len(action_refs) != 1
        ):
            raise ValueError(
                "feedback target reciprocal receipt verification failed: "
                "proposal lacks DecisionTrace material refs"
            )
        command_payload = dict(command.get("payload") or {})
        prior_effect_receipt_id = ""
        if command_type == "neutralize_feedback_effect":
            prior_effect = self.effect_receipt(str(command_payload.get("prior_command_id") or ""))
            if (
                prior_effect is None
                or prior_effect["receipt_id"] != command_payload.get("prior_effect_receipt_id")
                or prior_effect["target_effect_id"] != command_payload.get("prior_target_effect_id")
                or prior_effect["consumer_id"] != target_id
                or prior_effect["after_hash"] != command_payload.get("prior_after_hash")
            ):
                raise ValueError("feedback neutralization source effect mismatch")
            prior_domain_refs = tuple(
                str(ref)
                for ref in prior_effect["evidence_refs"]
                if str(ref).startswith(f"domain-feedback-receipt:{target_id}:")
            )
            if len(prior_domain_refs) != 1:
                raise ValueError("feedback neutralization lacks its prior domain receipt")
            command_payload["prior_target_receipt_ref"] = prior_domain_refs[0]
            prior_effect_receipt_id = str(prior_effect["receipt_id"])
        verifier = self._feedback_target_verifier(target_id)
        if not verifier.verify_command_effect(command_payload, effect):
            raise ValueError("feedback target reciprocal receipt verification failed")
        evidence_refs = [
            f"feedback-command:{command_id}",
            f"feedback-attribution:{attribution.revision_id}",
        ]
        if command_type == "neutralize_feedback_effect":
            evidence_refs.append(f"feedback-prior-effect:{prior_effect_receipt_id}")
        evidence_refs.append(target_receipt_ref)
        evidence_refs.extend(
            feedback_entity_evidence_ref("decision_trace", reference)
            for reference in decision_trace_refs
        )
        evidence_refs.extend(
            feedback_entity_evidence_ref("action", reference) for reference in action_refs
        )
        return self._record_effect_receipt(
            command_id,
            status="revoked" if disposition == "revoked" else "committed",
            target_effect_id=str(effect.target_effect_id),
            before_hash=str(effect.before_hash),
            after_hash=str(effect.after_hash),
            evidence_refs=tuple(evidence_refs),
            outcome=disposition,
            created_at=created_at,
        )

    def _record_feedback_ineligible_receipt(
        self,
        command_id: str,
        *,
        attribution_revision_id: str,
        created_at: str = "",
    ) -> CognitiveEffectReceipt:
        """Derive the only valid skip from the immutable target registry row."""

        from core.cognitive.feedback_contract import (
            FEEDBACK_TARGET_REGISTRY_HASH,
            FEEDBACK_TARGETS,
        )

        command, attribution = self._feedback_command_context(
            command_id,
            attribution_revision_id=attribution_revision_id,
        )
        payload = dict(command.get("payload") or {})
        target_id = str(command.get("consumer_id") or "")
        rows = [
            item
            for item in attribution.payload.get("target_dispositions") or ()
            if str(item.get("target_id") or "") == target_id
        ]
        if (
            command.get("command_type") != "evaluate_feedback_target"
            or target_id not in FEEDBACK_TARGETS
            or tuple(payload.get("required_target_ids") or ()) != FEEDBACK_TARGETS
            or len(rows) != 1
            or rows[0].get("eligible") is not False
            or payload.get("eligible") is not False
            or rows[0].get("exclusion_reason") != payload.get("exclusion_reason")
            or rows[0].get("command_ref", {}).get("command_key") != payload.get("command_key")
        ):
            raise ValueError("feedback target skip is not registry-ineligible")
        unchanged_hash = sha256_json(
            {
                "attribution_revision_id": attribution.revision_id,
                "target_id": target_id,
                "state": "unchanged",
            }
        )
        target_effect_id = (
            "feedback-skip:" + target_id + ":" + attribution.revision_id.removeprefix("cogrev-")
        )
        return self._record_effect_receipt(
            command_id,
            status="intentional_skip",
            target_effect_id=target_effect_id,
            before_hash=unchanged_hash,
            after_hash=unchanged_hash,
            evidence_refs=(
                f"feedback-command:{command_id}",
                f"feedback-attribution:{attribution.revision_id}",
                f"feedback-target-registry:{FEEDBACK_TARGET_REGISTRY_HASH}",
            ),
            outcome=str(rows[0]["exclusion_reason"]),
            terminal_reason_code="feedback_target_ineligible",
            created_at=created_at,
        )

    def _record_feedback_terminal_failure(
        self,
        command_id: str,
        *,
        proof: Any,
        created_at: str = "",
    ) -> CognitiveEffectReceipt:
        """Persist only an independently reproducible malformed-command proof."""

        from core.cognitive.feedback_command_failure import (
            derive_feedback_command_failure,
        )

        if not feedback_failure_context_matches_state(self, command_id):
            raise PermissionError(
                "feedback terminal failure requires active canonical owner context"
            )
        command, attribution = self._feedback_command_context(command_id)
        derived = derive_feedback_command_failure(self, command_id)
        if derived is None or proof != derived:
            raise ValueError("feedback command lacks deterministic failure proof")
        target_id = str(command.get("consumer_id") or "feedback_owner")
        failure_hash = derived.proof_hash
        unchanged_hash = sha256_json(
            {
                "command_id": command_id,
                "target_id": target_id,
                "state": "unchanged_after_permanent_failure",
                "failure_hash": failure_hash,
            }
        )
        target_effect_id = (
            f"feedback-failed:{target_id}:"
            + sha256_json({"command_id": command_id}).split(":", 1)[1][:32]
        )
        context_token = _ACTIVE_FEEDBACK_TERMINAL_FAILURE.set((id(self), command_id, object()))
        try:
            return self._record_effect_receipt(
                command_id,
                status="failed_terminal",
                target_effect_id=target_effect_id,
                before_hash=unchanged_hash,
                after_hash=unchanged_hash,
                evidence_refs=(
                    f"feedback-command:{command_id}",
                    f"feedback-attribution:{attribution.revision_id}",
                    f"feedback-permanent-failure:{failure_hash}",
                ),
                outcome="failed_terminal",
                terminal_reason_code="feedback_target_" + derived.reason_code,
                created_at=created_at,
            )
        finally:
            _ACTIVE_FEEDBACK_TERMINAL_FAILURE.reset(context_token)

    def _validate_training_admission_intake_receipt_derivation(
        self,
        command: Mapping[str, Any],
        *,
        status: str,
        target_effect_id: str,
        before_hash: str,
        after_hash: str,
        evidence_refs: tuple[str, ...],
        outcome: str,
        terminal_reason_code: str,
    ) -> None:
        """Recompute the sole non-feedback receipt on an attribution revision."""

        from core.cognitive.training_contract import (
            TRAINING_ADMISSION_SUPERSEDED_REASON,
            validate_training_admission_intake_payload,
        )
        from core.cognitive.training_governance_types import (
            TRAINING_PROJECTION_COMMAND,
            TRAINING_PROJECTION_CONSUMER,
        )

        try:
            payload = json.loads(str(command["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("training admission intake payload is malformed") from exc
        validate_training_admission_intake_payload(payload)
        command_id = str(command["command_id"])
        attribution_revision_id = str(payload["attribution_ref"]["revision_id"])
        outcome_revision_id = str(payload["outcome_ref"]["revision_id"])
        target_command_id = str(payload["training_target_ref"]["command_id"])
        if status == "rejected":
            source_outcome = self.revision(outcome_revision_id)
            current_outcome = (
                None
                if source_outcome is None
                else self.current_revision(
                    "outcome_measurement",
                    source_outcome.object_id,
                )
            )
            cursor = current_outcome
            seen: set[str] = set()
            while cursor is not None and cursor.revision_id != outcome_revision_id:
                if (
                    cursor.revision_id in seen
                    or not cursor.correction_of_revision_id
                    or cursor.supersedes_revision_id != cursor.correction_of_revision_id
                    or cursor.payload["correction_of_revision_id"]
                    != cursor.correction_of_revision_id
                    or cursor.payload["supersedes_revision_id"] != cursor.correction_of_revision_id
                ):
                    cursor = None
                    break
                seen.add(cursor.revision_id)
                cursor = self.revision(cursor.correction_of_revision_id)
            corrected_revision_id = str(
                "" if current_outcome is None else current_outcome.revision_id
            )
            unchanged_hash = str(command["payload_hash"])
            expected_effect = (
                "governed-training-admission-intake-superseded-"
                + command_id.removeprefix("cogcmd-")
            )
            expected_superseded_refs = (
                f"training-admission-intake:{command_id}",
                f"outcome-measurement:{outcome_revision_id}",
                f"corrected-outcome:{corrected_revision_id}",
                f"no-effect-oracle:{command_id}:{unchanged_hash}",
            )
            if (
                source_outcome is None
                or current_outcome is None
                or current_outcome == source_outcome
                or cursor != source_outcome
                or target_effect_id != expected_effect
                or before_hash != unchanged_hash
                or after_hash != unchanged_hash
                or evidence_refs != expected_superseded_refs
                or outcome != "superseded_before_admission"
                or terminal_reason_code != TRAINING_ADMISSION_SUPERSEDED_REASON
            ):
                raise ValueError("training admission intake supersession mismatch")
            return
        admission_refs = tuple(
            ref.removeprefix("training-admission:")
            for ref in evidence_refs
            if ref.startswith("training-admission:")
        )
        if len(admission_refs) != 1:
            raise ValueError("training admission intake receipt lacks one admission")
        admission = self.revision(admission_refs[0])
        projection_commands = (
            ()
            if admission is None
            else tuple(
                item
                for item in self.commands_for_revision(admission.revision_id)
                if item["consumer_id"] == TRAINING_PROJECTION_CONSUMER
                and item["command_type"] == TRAINING_PROJECTION_COMMAND
            )
        )
        if (
            admission is None
            or admission.object_type != "training_admission_record"
            or admission.payload["training_evidence_ref"]["command_id"] != target_command_id
            or len(projection_commands) != 1
        ):
            raise ValueError("training admission intake receipt source is unavailable")
        projection = projection_commands[0]
        verified_sample_id = validate_admission_projection_receipt(
            state_db_path=self.db_path,
            scoring_db_path=self.db_path.parent / "mnemos.db",
            revision=admission,
            command=projection,
        )
        if verified_sample_id != projection["payload"]["sample_id"]:
            raise ValueError("training admission intake projection identity mismatch")
        sample_id = str(projection["payload"]["sample_id"])
        projection_receipt_id = str(projection["payload"]["projection_receipt_id"])
        expected_committed_refs = (
            f"training-admission-intake:{command_id}",
            f"feedback-attribution:{attribution_revision_id}",
            f"outcome-measurement:{outcome_revision_id}",
            f"feedback-command:{target_command_id}",
            f"training-admission:{admission.revision_id}",
            f"governed-training-sample:{sample_id}",
            f"governed-training-receipt:{projection_receipt_id}",
        )
        expected_before = sha256_json({"command_id": command_id, "state": "pending"})
        expected_after = sha256_json(
            {
                "command_id": command_id,
                "admission_revision_id": admission.revision_id,
                "sample_id": sample_id,
                "projection_command_id": projection["command_id"],
                "projection_receipt_id": projection_receipt_id,
                "state": "committed",
            }
        )
        expected_effect = "governed-training-admission-intake-effect-" + command_id.removeprefix(
            "cogcmd-"
        )
        if (
            status != "committed"
            or target_effect_id != expected_effect
            or before_hash != expected_before
            or after_hash != expected_after
            or evidence_refs != expected_committed_refs
            or outcome != "governed training admission intake committed"
            or terminal_reason_code
        ):
            raise ValueError("training admission intake receipt derivation mismatch")

    def _feedback_command_context(
        self,
        command_id: str,
        *,
        attribution_revision_id: str = "",
    ) -> tuple[dict[str, Any], CognitiveStateRevision]:
        command = self.command(command_id)
        if command is None:
            raise ValueError("cognitive outbox command does not exist")
        revision_id = str(command.get("revision_id") or "")
        if attribution_revision_id and revision_id != str(attribution_revision_id):
            raise ValueError("feedback effect attribution revision mismatch")
        attribution = self.revision(revision_id)
        if attribution is None or attribution.object_type != "feedback_attribution_record":
            raise ValueError("command is not owned by a feedback attribution")
        return command, attribution

    def _feedback_target_verifier(self, target_id: str) -> Any:
        from core.cognitive.feedback_targets import build_feedback_target_adapters

        adapters = build_feedback_target_adapters(self.db_path.parent)
        verifier = adapters.get(target_id)
        if verifier is None:
            raise ValueError("feedback target verifier is not registered")
        return verifier

    def _validate_feedback_receipt_derivation(
        self,
        command: Mapping[str, Any],
        *,
        status: str,
        target_effect_id: str,
        before_hash: str,
        after_hash: str,
        evidence_refs: tuple[str, ...],
        outcome: str,
        terminal_reason_code: str,
    ) -> None:
        """Re-derive every feedback terminal before the shared insert path."""

        from core.cognitive.feedback_contract import (
            FEEDBACK_TARGET_REGISTRY_HASH,
            FEEDBACK_TARGETS,
        )
        from core.cognitive.feedback_models import (
            FeedbackTargetEffect,
            parse_feedback_entity_evidence_ref,
        )

        decision_trace_refs = []
        action_refs = []
        for ref in evidence_refs:
            parsed = parse_feedback_entity_evidence_ref(str(ref))
            if parsed is None:
                continue
            kind, reference = parsed
            if kind == "decision_trace":
                decision_trace_refs.append(reference)
            elif kind == "action":
                action_refs.append(reference)

        command_id = str(command["command_id"])
        revision_id = str(command["revision_id"])
        target_id = str(command["consumer_id"])
        command_type = str(command["command_type"])
        payload = json.loads(str(command["payload_json"]))
        if status in {"committed", "revoked"}:
            domain_refs = tuple(
                ref
                for ref in evidence_refs
                if ref.startswith(f"domain-feedback-receipt:{target_id}:")
            )
            expected_status = "revoked" if outcome == "revoked" else "committed"
            if (
                command_type not in {"evaluate_feedback_target", "neutralize_feedback_effect"}
                or target_id not in FEEDBACK_TARGETS
                or status != expected_status
                or len(domain_refs) != 1
                or not before_hash.startswith("sha256:")
                or not after_hash.startswith("sha256:")
                or f"feedback-command:{command_id}" not in evidence_refs
                or f"feedback-attribution:{revision_id}" not in evidence_refs
            ):
                raise ValueError("feedback canonical owner derivation mismatch")
            if command_type == "neutralize_feedback_effect":
                prior = self.effect_receipt(str(payload.get("prior_command_id") or ""))
                if prior is None:
                    raise ValueError("feedback neutralization source effect missing")
                prior_refs = tuple(
                    str(ref)
                    for ref in prior["evidence_refs"]
                    if str(ref).startswith(f"domain-feedback-receipt:{target_id}:")
                )
                if (
                    prior["receipt_id"] != payload.get("prior_effect_receipt_id")
                    or len(prior_refs) != 1
                    or f"feedback-prior-effect:{prior['receipt_id']}" not in evidence_refs
                ):
                    raise ValueError("feedback neutralization receipt derivation mismatch")
                payload = dict(payload)
                payload["prior_target_receipt_ref"] = prior_refs[0]
            effect = FeedbackTargetEffect(
                target_id=target_id,
                target_effect_id=target_effect_id,
                disposition=outcome,
                before_hash=before_hash,
                after_hash=after_hash,
                target_receipt_ref=domain_refs[0],
                decision_trace_refs=tuple(decision_trace_refs),
                action_refs=tuple(action_refs),
            )
            if not self._feedback_target_verifier(target_id).verify_command_effect(payload, effect):
                raise ValueError("feedback domain proof is not independently valid")
            return
        if status == "intentional_skip":
            attribution = self.revision(revision_id)
            rows = (
                []
                if attribution is None
                else [
                    item
                    for item in attribution.payload.get("target_dispositions") or ()
                    if str(item.get("target_id") or "") == target_id
                ]
            )
            unchanged_hash = sha256_json(
                {
                    "attribution_revision_id": revision_id,
                    "target_id": target_id,
                    "state": "unchanged",
                }
            )
            expected_effect = f"feedback-skip:{target_id}:" + revision_id.removeprefix("cogrev-")
            if (
                command_type != "evaluate_feedback_target"
                or target_id not in FEEDBACK_TARGETS
                or len(rows) != 1
                or rows[0].get("eligible") is not False
                or payload.get("eligible") is not False
                or outcome != str(rows[0].get("exclusion_reason") or "")
                or terminal_reason_code != "feedback_target_ineligible"
                or target_effect_id != expected_effect
                or before_hash != unchanged_hash
                or after_hash != unchanged_hash
                or evidence_refs
                != (
                    f"feedback-command:{command_id}",
                    f"feedback-attribution:{revision_id}",
                    f"feedback-target-registry:{FEEDBACK_TARGET_REGISTRY_HASH}",
                )
            ):
                raise ValueError("feedback canonical owner derivation mismatch for skip")
            return
        if status == "failed_terminal":
            from core.cognitive.feedback_command_failure import (
                derive_feedback_command_failure,
            )

            failure_refs = tuple(
                ref for ref in evidence_refs if ref.startswith("feedback-permanent-failure:sha256:")
            )
            expected_effect = (
                f"feedback-failed:{target_id}:"
                + sha256_json({"command_id": command_id}).split(":", 1)[1][:32]
            )
            active_failure = _ACTIVE_FEEDBACK_TERMINAL_FAILURE.get()
            if (
                active_failure is None
                or active_failure[0] != id(self)
                or active_failure[1] != command_id
            ):
                raise PermissionError("feedback terminal failure lacks canonical owner capability")
            if len(failure_refs) != 1:
                raise ValueError("feedback failure receipt lacks exact evidence")
            failure_hash = failure_refs[0].removeprefix("feedback-permanent-failure:")
            derived = derive_feedback_command_failure(self, command_id)
            unchanged_hash = sha256_json(
                {
                    "command_id": command_id,
                    "target_id": target_id,
                    "state": "unchanged_after_permanent_failure",
                    "failure_hash": failure_hash,
                }
            )
            if (
                target_effect_id != expected_effect
                or derived is None
                or failure_hash != derived.proof_hash
                or terminal_reason_code != "feedback_target_" + derived.reason_code
                or before_hash != unchanged_hash
                or after_hash != unchanged_hash
                or outcome != "failed_terminal"
                or not terminal_reason_code.startswith("feedback_target_")
                or f"feedback-command:{command_id}" not in evidence_refs
                or f"feedback-attribution:{revision_id}" not in evidence_refs
            ):
                raise ValueError("feedback failure receipt derivation mismatch")
            return
        raise ValueError("feedback terminal status is not owner-derived")

    def _record_effect_receipt(
        self,
        command_id: str,
        *,
        status: str,
        target_effect_id: str,
        before_hash: str = "",
        after_hash: str = "",
        evidence_refs: Sequence[str],
        outcome: str = "",
        terminal_reason_code: str = "",
        retry_exhausted: bool = False,
        created_at: str = "",
    ) -> CognitiveEffectReceipt:
        """Atomically close an outbox command and its consumer-pair receipt."""

        normalized_status = str(status or "")
        if normalized_status not in {
            "committed",
            "failed_terminal",
            "intentional_skip",
            "rejected",
            "revoked",
            "dead_letter",
        }:
            raise ValueError("unsupported cognitive effect receipt status")
        normalized_command = str(command_id or "").strip()
        normalized_target = str(target_effect_id or "").strip()
        normalized_refs = tuple(str(value).strip() for value in evidence_refs)
        normalized_reason = str(terminal_reason_code or "").strip()
        if not normalized_command:
            raise ValueError("command_id is required")
        if not normalized_target:
            raise ValueError("target_effect_id is required")
        if not normalized_refs or any(not value for value in normalized_refs):
            raise ValueError("evidence_refs must be non-empty")
        if normalized_status == "committed" and (not before_hash or not after_hash):
            raise ValueError("committed effect receipts require before_hash and after_hash")
        timestamp = created_at or now_utc()
        identity = {
            "command_id": normalized_command,
            "status": normalized_status,
            "target_effect_id": normalized_target,
            "before_hash": str(before_hash or ""),
            "after_hash": str(after_hash or ""),
            "evidence_refs": list(normalized_refs),
            "terminal_reason_code": normalized_reason,
            "retry_exhausted": bool(retry_exhausted),
        }
        receipt_id = "cogeffect-" + sha256_json(identity).split(":", 1)[1][:32]
        reciprocal_ref = f"cognitive-effect-receipt:{receipt_id}"
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            command = conn.execute(
                """
                SELECT o.command_id, o.revision_id, o.event_id, o.consumer_id,
                       o.command_type, o.payload_json, o.payload_hash,
                       r.object_type, r.object_id,
                       r.payload_hash AS revision_payload_hash
                FROM cognitive_state_outbox AS o
                JOIN cognitive_state_revisions AS r
                  ON r.revision_id=o.revision_id
                WHERE o.command_id=?
                """,
                (normalized_command,),
            ).fetchone()
            if command is None:
                raise ValueError("cognitive outbox command does not exist")
            revision_id = str(command["revision_id"])
            event_id = str(command["event_id"])
            consumer_id = str(command["consumer_id"])
            command_type = str(command["command_type"])
            if (
                command_type == _COGNITION_EPISODE_COMMAND
                and normalized_status == "intentional_skip"
            ):
                self._validate_cognition_episode_omission_receipt_derivation(
                    conn,
                    command,
                    status=normalized_status,
                    target_effect_id=normalized_target,
                    before_hash=str(before_hash or ""),
                    after_hash=str(after_hash or ""),
                    evidence_refs=normalized_refs,
                    outcome=str(outcome or ""),
                    terminal_reason_code=normalized_reason,
                )
            if str(command["object_type"]) == "feedback_attribution_record":
                from core.cognitive.training_contract import (
                    TRAINING_ADMISSION_COMMAND,
                    TRAINING_ADMISSION_CONSUMER,
                )

                if (
                    consumer_id == TRAINING_ADMISSION_CONSUMER
                    and command_type == TRAINING_ADMISSION_COMMAND
                ):
                    self._validate_training_admission_intake_receipt_derivation(
                        command,
                        status=normalized_status,
                        target_effect_id=normalized_target,
                        before_hash=str(before_hash or ""),
                        after_hash=str(after_hash or ""),
                        evidence_refs=normalized_refs,
                        outcome=str(outcome or ""),
                        terminal_reason_code=normalized_reason,
                    )
                else:
                    self._validate_feedback_receipt_derivation(
                        command,
                        status=normalized_status,
                        target_effect_id=normalized_target,
                        before_hash=str(before_hash or ""),
                        after_hash=str(after_hash or ""),
                        evidence_refs=normalized_refs,
                        outcome=str(outcome or ""),
                        terminal_reason_code=normalized_reason,
                    )
            if command_type == COGNITIVE_TOMBSTONE_COMMAND_TYPE:
                try:
                    tombstone_payload = json.loads(str(command["payload_json"]))
                    request_id = str(tombstone_payload["request_id"])
                    expected_target = f"tombstone:{consumer_id}:{request_id}"
                    expected_before_hash = str(tombstone_payload["before_hash"])
                    expected_after_hash = str(tombstone_payload["tombstone_hash"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise CognitiveStateConflict("tombstone command payload is malformed") from exc
                if normalized_status != "committed":
                    raise ValueError("tombstone receipt must be committed")
                if normalized_target != expected_target:
                    raise ValueError(
                        "tombstone receipt target_effect_id is not bound to its consumer"
                    )
                if str(before_hash or "") != expected_before_hash:
                    raise ValueError("tombstone receipt before_hash does not match the command")
                if str(after_hash or "") != expected_after_hash:
                    raise ValueError("tombstone receipt after_hash does not match the command")
                if f"tombstone-command:{normalized_command}" not in normalized_refs:
                    raise ValueError("tombstone receipt must reference its immutable command")
                if not any(ref.startswith("tombstone-oracle:") for ref in normalized_refs):
                    raise ValueError("tombstone receipt requires independent oracle evidence")
            if command_type == "project_belief_revision" and normalized_status == "committed":
                try:
                    belief_payload = json.loads(str(command["payload_json"]))
                    expected_target = str(belief_payload["projection_effect_id"])
                    expected_revision = str(belief_payload["revision_id"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise CognitiveStateConflict(
                        "belief projection command payload is malformed"
                    ) from exc
                if expected_revision != revision_id:
                    raise ValueError(
                        "belief projection receipt revision does not match its command"
                    )
                if normalized_target != expected_target:
                    raise ValueError("belief projection receipt target does not match its command")
                required_refs = {
                    f"belief-command:{normalized_command}",
                    f"belief-revision:{revision_id}",
                    f"graph-projection:{after_hash}",
                }
                if not required_refs.issubset(normalized_refs):
                    raise ValueError("belief projection receipt lacks reciprocal effect evidence")
                if not str(before_hash).startswith("sha256:") or not str(after_hash).startswith(
                    "sha256:"
                ):
                    raise ValueError("belief projection receipt requires canonical graph hashes")
            if command_type == "project_prediction_delivery":
                try:
                    prediction_payload = json.loads(str(command["payload_json"]))
                    expected_target = str(prediction_payload["projection_effect_id"])
                    expected_revision = str(prediction_payload["prediction_revision_id"])
                    expected_after = str(prediction_payload["delivery_event_payload_hash"])
                    delivery_event_id = str(prediction_payload["delivery_event_id"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise CognitiveStateConflict(
                        "prediction delivery command payload is malformed"
                    ) from exc
                if normalized_status != "committed":
                    raise ValueError("prediction delivery projection must commit exactly")
                if expected_revision != revision_id:
                    raise ValueError(
                        "prediction delivery receipt revision does not match its command"
                    )
                if normalized_target != expected_target:
                    raise ValueError(
                        "prediction delivery receipt target does not match its command"
                    )
                if str(after_hash) != expected_after:
                    raise ValueError("prediction delivery receipt hash does not match its command")
                required_refs = {
                    f"prediction-delivery-command:{normalized_command}",
                    f"prediction-revision:{revision_id}",
                    f"delivery-event:{delivery_event_id}",
                    f"delivery-event-payload:{expected_after}",
                }
                if not required_refs.issubset(normalized_refs):
                    raise ValueError("prediction delivery receipt lacks reciprocal evidence")
                if not str(before_hash).startswith("sha256:") or not str(after_hash).startswith(
                    "sha256:"
                ):
                    raise ValueError("prediction delivery receipt requires canonical hashes")
            if command_type == "project_prediction_outcome":
                try:
                    outcome_payload = json.loads(str(command["payload_json"]))
                    projection_schema = str(outcome_payload["schema_version"])
                    expected_target = str(outcome_payload["projection_effect_id"])
                    expected_revision = str(outcome_payload["outcome_revision_id"])
                    expected_after = str(outcome_payload["outcome_revision_hash"])
                    prediction_revision_id = str(outcome_payload["prediction_revision_id"])
                    oracle_issuance_hash = str(outcome_payload["oracle_issuance_hash"])
                    oracle_source_revision_id = str(outcome_payload["oracle_source_revision_id"])
                    oracle_source_content_hash = str(outcome_payload["oracle_source_content_hash"])
                    correction_of_outcome = str(
                        outcome_payload.get("correction_of_outcome_revision_id") or ""
                    )
                    prior_terminal_revision_id = str(
                        outcome_payload.get("prior_prediction_terminal_revision_id") or ""
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise CognitiveStateConflict(
                        "prediction outcome command payload is malformed"
                    ) from exc
                if normalized_status != "committed":
                    raise ValueError("prediction outcome projection must commit exactly")
                if expected_revision != revision_id:
                    raise ValueError(
                        "prediction outcome receipt revision does not match its command"
                    )
                if normalized_target != expected_target:
                    raise ValueError("prediction outcome receipt target does not match its command")
                if str(after_hash) != expected_after:
                    raise ValueError("prediction outcome receipt hash does not match its command")
                required_refs = {
                    f"prediction-outcome-command:{normalized_command}",
                    f"outcome-revision:{revision_id}",
                    f"prediction-revision:{prediction_revision_id}",
                    f"objective-oracle-issuance:{oracle_issuance_hash}",
                    "objective-oracle-source:"
                    f"{oracle_source_revision_id}:{oracle_source_content_hash}",
                    f"prediction-outcome-projection:{expected_after}",
                }
                if not required_refs.issubset(normalized_refs):
                    raise ValueError("prediction outcome receipt lacks reciprocal evidence")
                if bool(correction_of_outcome) != bool(prior_terminal_revision_id):
                    raise ValueError("prediction outcome correction fields are incomplete")
                if correction_of_outcome:
                    outcome_revision = self.revision(revision_id)
                    if (
                        projection_schema != "mnemos.prediction_outcome_projection.v2"
                        or outcome_revision is None
                        or outcome_revision.object_type != "outcome_measurement"
                        or outcome_revision.correction_of_revision_id != correction_of_outcome
                        or outcome_revision.supersedes_revision_id != correction_of_outcome
                    ):
                        raise ValueError("prediction outcome correction lineage mismatch")
                elif projection_schema not in {
                    "mnemos.prediction_outcome_projection.v1",
                    "mnemos.prediction_outcome_projection.v2",
                }:
                    raise ValueError("unsupported prediction outcome projection schema")
                if not str(before_hash).startswith("sha256:") or not str(after_hash).startswith(
                    "sha256:"
                ):
                    raise ValueError("prediction outcome receipt requires canonical hashes")
            if command_type == "correct_prediction_terminal_from_outcome":
                try:
                    correction_payload = json.loads(str(command["payload_json"]))
                    if (
                        correction_payload["schema_version"]
                        != "mnemos.prediction_terminal_correction.v1"
                    ):
                        raise ValueError("unsupported prediction correction schema")
                    outcome_revision_id = str(correction_payload["outcome_revision_id"])
                    outcome_revision_hash = str(correction_payload["outcome_revision_hash"])
                    prior_outcome_revision_id = str(
                        correction_payload["correction_of_outcome_revision_id"]
                    )
                    prediction_id = str(correction_payload["prediction_id"])
                    prior_terminal_revision_id = str(
                        correction_payload["prior_prediction_terminal_revision_id"]
                    )
                    prior_terminal_hash = str(correction_payload["prior_prediction_terminal_hash"])
                    correction_effect_id = str(correction_payload["correction_effect_id"])
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                ) as exc:
                    raise CognitiveStateConflict(
                        "prediction correction command payload is malformed"
                    ) from exc
                outcome_revision = self.revision(outcome_revision_id)
                prior_terminal = self.revision(prior_terminal_revision_id)
                current_terminal = (
                    None
                    if prior_terminal is None
                    else self.current_revision(
                        "prediction_record",
                        prior_terminal.object_id,
                    )
                )
                terminal_commands = (
                    ()
                    if current_terminal is None
                    else tuple(
                        item
                        for item in self.commands_for_revision(current_terminal.revision_id)
                        if item["command_type"] == "project_prediction_terminal"
                    )
                )
                terminal_receipt = (
                    None
                    if len(terminal_commands) != 1
                    else self.effect_receipt(str(terminal_commands[0]["command_id"]))
                )
                expected_refs = (
                    f"prediction-terminal-correction-command:{normalized_command}",
                    f"outcome-revision:{outcome_revision_id}",
                    "prior-prediction-terminal:"
                    f"{prior_terminal_revision_id}:{prior_terminal_hash}",
                    "corrected-prediction-terminal:"
                    + str("" if current_terminal is None else current_terminal.revision_id)
                    + ":"
                    + str("" if current_terminal is None else current_terminal.payload_hash),
                    "prediction-terminal-effect-receipt:"
                    + str("" if terminal_receipt is None else terminal_receipt["receipt_id"]),
                )
                if (
                    normalized_status != "committed"
                    or outcome_revision_id != revision_id
                    or outcome_revision is None
                    or outcome_revision.object_type != "outcome_measurement"
                    or outcome_revision.payload_hash != outcome_revision_hash
                    or outcome_revision.correction_of_revision_id != prior_outcome_revision_id
                    or outcome_revision.supersedes_revision_id != prior_outcome_revision_id
                    or prior_terminal is None
                    or prior_terminal.object_type != "prediction_record"
                    or prior_terminal.object_id != prediction_id
                    or prior_terminal.payload_hash != prior_terminal_hash
                    or prior_terminal.payload["outcome_ref"]["revision_id"]
                    != prior_outcome_revision_id
                    or current_terminal is None
                    or current_terminal.correction_of_revision_id != prior_terminal_revision_id
                    or current_terminal.supersedes_revision_id != prior_terminal_revision_id
                    or current_terminal.payload["outcome_ref"]
                    != {
                        "revision_id": outcome_revision_id,
                        "payload_hash": outcome_revision_hash,
                    }
                    or terminal_receipt is None
                    or terminal_receipt["status"] != "committed"
                    or normalized_target != correction_effect_id
                    or str(before_hash) != prior_terminal_hash
                    or str(after_hash) != current_terminal.payload_hash
                    or normalized_refs != expected_refs
                    or outcome != "corrected prediction terminal available"
                    or terminal_reason_code
                ):
                    raise ValueError("prediction terminal correction receipt mismatch")
            if command_type == "project_prediction_terminal":
                try:
                    terminal_payload = json.loads(str(command["payload_json"]))
                    if (
                        terminal_payload["schema_version"]
                        != "mnemos.prediction_terminal_projection.v1"
                    ):
                        raise ValueError("unsupported terminal projection schema")
                    prediction_id = str(terminal_payload["prediction_id"])
                    expected_revision = str(terminal_payload["terminal_revision_id"])
                    expected_revision_hash = str(terminal_payload["terminal_revision_hash"])
                    expected_state = str(terminal_payload["terminal_state"])
                    expected_target = str(terminal_payload["projection_effect_id"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise CognitiveStateConflict(
                        "prediction terminal command payload is malformed"
                    ) from exc
                terminal_revision = conn.execute(
                    "SELECT object_id, payload_hash, payload_json "
                    "FROM cognitive_state_revisions WHERE revision_id=?",
                    (expected_revision,),
                ).fetchone()
                try:
                    terminal_revision_payload = json.loads(str(terminal_revision["payload_json"]))
                    actual_state = str(terminal_revision_payload["terminal"]["state"])
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                ) as exc:
                    raise CognitiveStateConflict(
                        "prediction terminal revision is missing or malformed"
                    ) from exc
                expected_before = sha256_json(
                    {"prediction_id": prediction_id, "state": "unprojected"}
                )
                expected_after = sha256_json(
                    {
                        "terminal_revision_id": expected_revision,
                        "terminal_revision_hash": expected_revision_hash,
                        "terminal_state": expected_state,
                    }
                )
                if normalized_status != "committed":
                    raise ValueError("prediction terminal projection must commit exactly")
                if expected_revision != revision_id:
                    raise ValueError(
                        "prediction terminal receipt revision does not match its command"
                    )
                if (
                    str(terminal_revision["object_id"]) != prediction_id
                    or str(terminal_revision["payload_hash"]) != expected_revision_hash
                    or actual_state != expected_state
                ):
                    raise ValueError(
                        "prediction terminal command does not bind the canonical revision"
                    )
                if normalized_target != expected_target:
                    raise ValueError(
                        "prediction terminal receipt target does not match its command"
                    )
                if str(before_hash) != expected_before or str(after_hash) != expected_after:
                    raise ValueError("prediction terminal receipt hashes do not match its command")
                required_refs = {
                    f"prediction-terminal-command:{normalized_command}",
                    f"prediction-revision:{expected_revision}",
                    f"prediction-terminal-projection:{expected_after}",
                }
                if not required_refs.issubset(normalized_refs):
                    raise ValueError("prediction terminal receipt lacks reciprocal evidence")
            if command_type == "execute_material_action":
                try:
                    material_payload = json.loads(str(command["payload_json"]))
                    expected_effect = str(material_payload["effect_id"])
                    expected_decision = str(material_payload["decision_revision_id"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise CognitiveStateConflict(
                        "material-action command payload is malformed"
                    ) from exc
                if expected_decision != revision_id:
                    raise ValueError("material-action decision does not match its command")
                if normalized_target != expected_effect:
                    raise ValueError("material-action receipt target does not match its command")
                if normalized_status != "committed" and not normalized_reason:
                    raise ValueError("non-success material-action receipt requires a reason code")
                if normalized_status == "committed" and normalized_reason:
                    raise ValueError("committed material-action receipt cannot carry a reason code")
                if bool(retry_exhausted) != (normalized_status == "dead_letter"):
                    raise ValueError(
                        "retry_exhausted must be true exactly for material dead_letter"
                    )
                if not str(before_hash).startswith("sha256:") or not str(after_hash).startswith(
                    "sha256:"
                ):
                    raise ValueError("material-action receipt requires canonical target hashes")
                required_refs = {
                    f"material-command:{normalized_command}",
                    f"decision-revision:{revision_id}",
                    f"material-effect:{expected_effect}",
                }
                if not required_refs.issubset(normalized_refs):
                    raise ValueError("material-action receipt lacks reciprocal effect evidence")
                if normalized_status == "committed":
                    if f"target-after:{after_hash}" not in normalized_refs:
                        raise ValueError(
                            "committed material action lacks its target-after evidence"
                        )
                    if not any(
                        ref.startswith("target-oracle:") or ref.startswith("target-journal:")
                        for ref in normalized_refs
                    ):
                        raise ValueError(
                            "committed material action requires independent target evidence"
                        )
                elif normalized_status in {"failed_terminal", "dead_letter"}:
                    if f"attempted-effect:{expected_effect}" not in normalized_refs:
                        raise ValueError("failed material action lacks attempted-effect evidence")
                    if before_hash != after_hash and not any(
                        ref.startswith("rollback:") for ref in normalized_refs
                    ):
                        raise ValueError(
                            "failed material action requires unchanged-state or rollback evidence"
                        )
                    if not any(
                        ref.startswith("target-oracle:") or ref.startswith("rollback:")
                        for ref in normalized_refs
                    ):
                        raise ValueError(
                            "failed material action requires an independent state oracle"
                        )
                    if normalized_status == "dead_letter" and (
                        f"retry-budget-exhausted:{normalized_command}" not in normalized_refs
                    ):
                        raise ValueError("dead-letter material action lacks retry-budget evidence")
                else:
                    no_effect_ref = f"no-effect-oracle:{expected_effect}:{before_hash}"
                    if before_hash != after_hash or no_effect_ref not in normalized_refs:
                        raise ValueError(
                            "non-executed material action requires exact no-effect evidence"
                        )
                    if normalized_status == "intentional_skip" and (
                        f"approved-skip:{revision_id}" not in normalized_refs
                    ):
                        raise ValueError("intentional skip lacks explicit approval evidence")
            consumption_id, _ = insert_data_consumption_in_connection(
                conn,
                event_id,
                consumer_id=consumer_id,
                status=normalized_status,
                outcome=str(outcome or ""),
                idempotency_key=f"cognitive-effect:{receipt_id}",
                target_effect_id=normalized_target,
                before_hash=str(before_hash or ""),
                after_hash=str(after_hash or ""),
                effect_evidence_refs=(*normalized_refs, reciprocal_ref),
                metadata={
                    "command_id": normalized_command,
                    "effect_receipt_id": receipt_id,
                    "terminal_reason_code": normalized_reason,
                    "retry_exhausted": bool(retry_exhausted),
                },
                created_at=timestamp,
            )
            expected = (
                normalized_command,
                revision_id,
                event_id,
                consumer_id,
                consumption_id,
                normalized_status,
                normalized_target,
                str(before_hash or ""),
                str(after_hash or ""),
                canonical_json(list(normalized_refs)),
            )
            existing = conn.execute(
                """
                SELECT command_id, revision_id, event_id, consumer_id,
                       consumption_id, status, target_effect_id, before_hash,
                       after_hash, evidence_refs, created_at
                FROM cognitive_state_effect_receipts WHERE command_id=?
                """,
                (normalized_command,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO cognitive_state_effect_receipts (
                        receipt_id, command_id, revision_id, event_id, consumer_id,
                        consumption_id, status, target_effect_id, before_hash,
                        after_hash, evidence_refs, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (receipt_id, *expected, timestamp),
                )
            elif tuple(existing[:-1]) != expected:
                raise CognitiveStateConflict("immutable cognitive effect receipt conflict")
            else:
                timestamp = str(existing[-1])
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return CognitiveEffectReceipt(
            receipt_id=receipt_id,
            command_id=normalized_command,
            revision_id=revision_id,
            event_id=event_id,
            consumer_id=consumer_id,
            consumption_id=consumption_id,
            status=normalized_status,
            target_effect_id=normalized_target,
            before_hash=str(before_hash or ""),
            after_hash=str(after_hash or ""),
            evidence_refs=normalized_refs,
            reason_code=normalized_reason,
            retry_exhausted=bool(retry_exhausted),
            created_at=timestamp,
        )
