"""Material-action authorization and terminal receipt coordination."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Sequence

from core.cognitive.state_contract import sha256_json
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.state_schema import decision_trace_enforcement_enabled

from core.cognitive.decision_trace_contracts import (
    MATERIAL_ACTION_COMMAND_TYPE,
    MaterialActionObservation,
    MaterialActionPermit,
    MaterialActionReceipt,
    MaterialActionTerminal,
    MaterialEffectOracle,
    _required,
    _sha256,
    _strings,
    _timestamp,
)
from core.cognitive.decision_trace_verification import (
    _normalize_material_prediction_refs,
    _verify_decision_bundle,
    _verify_prediction_refs,
)
from core.cognitive.decision_trace_payloads import (
    _verify_revision_payload_hash,
    _verify_snapshot_hash,
)


class MaterialActionCoordinator:
    """Resolve committed action commands into fail-closed typed permits."""

    def __init__(self, state_store: CognitiveStateStore):
        self.state_store = state_store

    def authorize(
        self,
        command_id: str,
        *,
        executor_id: str,
    ) -> MaterialActionPermit:
        """Return a non-terminal permit bound to the named executor."""

        return self._validated_permit(
            command_id,
            executor_id=executor_id,
            allow_terminal=False,
        )

    def bind(
        self,
        command_id: str,
        *,
        executor_id: str,
    ) -> "MaterialActionAuthorization":
        """Return one execution capability backed by the canonical command."""

        return MaterialActionAuthorization(
            coordinator=self,
            permit=self.authorize(command_id, executor_id=executor_id),
        )

    def bind_for_recovery(
        self,
        command_id: str,
        *,
        executor_id: str,
    ) -> "MaterialActionAuthorization":
        """Bind an existing pending or terminal command for read-only recovery."""

        return MaterialActionAuthorization(
            coordinator=self,
            permit=self._validated_permit(
                command_id,
                executor_id=executor_id,
                allow_terminal=True,
            ),
        )

    def validate_for_effect(
        self,
        permit: MaterialActionPermit,
        *,
        owner: str,
        executor_id: str,
        action_type: str,
        target_ref: str,
        input_hash: str,
    ) -> MaterialActionPermit:
        """Rehydrate and bind a typed permit to the effect a sink will run."""

        if not isinstance(permit, MaterialActionPermit):
            raise PermissionError("a typed MaterialActionPermit is required")
        normalized_executor = _required(executor_id, "executor_id")
        validated = self._validated_permit(
            permit.command_id,
            executor_id=normalized_executor,
            allow_terminal=False,
        )
        if validated != permit:
            raise PermissionError("material-action permit binding is invalid")
        _validate_material_effect_fields(
            validated,
            owner=owner,
            executor_id=normalized_executor,
            action_type=action_type,
            target_ref=target_ref,
            input_hash=input_hash,
        )
        return validated

    def validate_for_projection(
        self,
        permit: MaterialActionPermit,
        *,
        owner: str,
        executor_id: str,
        action_type: str,
        target_ref: str,
        input_hash: str,
        terminal_statuses: Sequence[str] = ("committed",),
    ) -> MaterialActionPermit:
        """Validate a projection against an already terminal target effect."""

        if not isinstance(permit, MaterialActionPermit):
            raise PermissionError("a typed MaterialActionPermit is required")
        normalized_executor = _required(executor_id, "executor_id")
        validated = self._validated_permit(
            permit.command_id,
            executor_id=normalized_executor,
            allow_terminal=True,
        )
        if validated != permit:
            raise PermissionError("material-action permit binding is invalid")
        _validate_material_effect_fields(
            validated,
            owner=owner,
            executor_id=normalized_executor,
            action_type=action_type,
            target_ref=target_ref,
            input_hash=input_hash,
        )
        allowed_statuses = set(
            _strings(
                terminal_statuses,
                "terminal_statuses",
                non_empty=True,
            )
        )
        with self.state_store._connect(read_only=True) as conn:  # noqa: SLF001
            terminal = conn.execute(
                """
                SELECT status FROM cognitive_state_effect_receipts
                WHERE command_id=?
                """,
                (permit.command_id,),
            ).fetchone()
        if terminal is None:
            raise PermissionError("material-action projection requires a terminal effect receipt")
        if str(terminal["status"]) not in allowed_statuses:
            raise PermissionError("material-action projection terminal status is not allowed")
        return validated

    def record_terminal(
        self,
        permit: MaterialActionPermit,
        terminal: MaterialActionTerminal,
    ) -> MaterialActionReceipt:
        """Commit and return the one terminal receipt for an exact permit."""

        if not isinstance(permit, MaterialActionPermit):
            raise ValueError("a typed MaterialActionPermit is required")
        if not isinstance(terminal, MaterialActionTerminal):
            raise ValueError("a typed MaterialActionTerminal is required")
        validated = self._validated_permit(
            permit.command_id,
            executor_id=permit.executor_id,
            allow_terminal=True,
        )
        if validated != permit:
            raise PermissionError("material-action permit binding is invalid")
        if terminal.status not in {
            "committed",
            "failed_terminal",
            "rejected",
            "revoked",
            "dead_letter",
            "intentional_skip",
        }:
            raise ValueError("unsupported material-action terminal status")
        if terminal.target_effect_id != permit.effect_id:
            raise ValueError("material-action terminal effect does not match its permit")
        before_hash = _sha256(terminal.before_hash, "terminal.before_hash")
        after_hash = _sha256(terminal.after_hash, "terminal.after_hash")
        evidence_refs = _strings(
            terminal.evidence_refs,
            "terminal.evidence_refs",
            non_empty=True,
        )
        prediction_evidence_refs = tuple(
            "prediction-revision:"
            f"{ref['prediction_revision_id']}:{ref['prediction_revision_hash']}"
            for ref in permit.prediction_refs
        )
        evidence_refs = tuple(dict.fromkeys((*evidence_refs, *prediction_evidence_refs)))
        required_refs = {
            f"material-command:{permit.command_id}",
            f"decision-revision:{permit.decision_revision_id}",
            f"material-effect:{permit.effect_id}",
            *prediction_evidence_refs,
        }
        if not required_refs.issubset(evidence_refs):
            raise ValueError("material-action terminal lacks reciprocal effect evidence")
        created_at = _timestamp(terminal.created_at, "terminal.created_at")
        effect = self.state_store.record_effect_receipt(
            permit.command_id,
            status=terminal.status,
            target_effect_id=terminal.target_effect_id,
            before_hash=before_hash,
            after_hash=after_hash,
            evidence_refs=evidence_refs,
            outcome=str(terminal.outcome or ""),
            terminal_reason_code=str(terminal.reason_code or ""),
            retry_exhausted=bool(terminal.retry_exhausted),
            created_at=created_at,
        )
        return MaterialActionReceipt(
            receipt_id=effect.receipt_id,
            command_id=permit.command_id,
            decision_revision_id=permit.decision_revision_id,
            action_id=permit.action_id,
            effect_id=permit.effect_id,
            status=effect.status,
            before_hash=effect.before_hash,
            after_hash=effect.after_hash,
            evidence_refs=effect.evidence_refs,
            reason_code=effect.reason_code,
            retry_exhausted=effect.retry_exhausted,
            created_at=effect.created_at,
            prediction_refs=permit.prediction_refs,
        )

    def recover(
        self,
        command_id: str,
        *,
        executor_id: str,
        oracle: MaterialEffectOracle,
    ) -> MaterialActionReceipt | None:
        """Close a pending command from an idempotent target observation.

        Recovery never executes the target.  It revalidates the immutable
        command, asks the target-family oracle for the exact effect identity,
        and records only the observed terminal state.  An absent observation
        deliberately leaves the command pending for its normal executor.
        """

        if not isinstance(oracle, MaterialEffectOracle):
            raise TypeError("a typed MaterialEffectOracle is required")
        permit = self._validated_permit(
            command_id,
            executor_id=executor_id,
            allow_terminal=True,
        )
        if (
            _required(oracle.owner, "oracle.owner") != permit.owner
            or _required(oracle.executor_id, "oracle.executor_id") != permit.executor_id
            or _required(oracle.action_type, "oracle.action_type") != permit.action_type
        ):
            raise PermissionError("material-effect oracle family does not match command")
        observation = oracle.observe(permit)
        existing = self._material_receipt(permit)
        if existing is not None:
            if observation is None:
                raise RuntimeError("terminal material command lacks its exact target observation")
            validate_material_receipt_observation(existing, observation)
            return existing
        if observation is None:
            return None
        if not isinstance(observation, MaterialActionObservation):
            raise TypeError("material-effect oracle returned an invalid observation")
        observed_refs = _strings(
            observation.evidence_refs,
            "observation.evidence_refs",
            non_empty=True,
        )
        return self.record_terminal(
            permit,
            MaterialActionTerminal(
                status=observation.status,
                target_effect_id=permit.effect_id,
                before_hash=observation.before_hash,
                after_hash=observation.after_hash,
                evidence_refs=tuple(
                    dict.fromkeys(
                        (
                            f"material-command:{permit.command_id}",
                            f"decision-revision:{permit.decision_revision_id}",
                            f"material-effect:{permit.effect_id}",
                            *observed_refs,
                        )
                    )
                ),
                reason_code=observation.reason_code,
                retry_exhausted=observation.retry_exhausted,
                outcome=observation.outcome,
                created_at=observation.observed_at,
            ),
        )

    def _material_receipt(
        self,
        permit: MaterialActionPermit,
    ) -> MaterialActionReceipt | None:
        with self.state_store._connect(read_only=True) as conn:  # noqa: SLF001
            row = conn.execute(
                """
                SELECT r.receipt_id, r.command_id, r.revision_id, r.status,
                       r.target_effect_id, r.before_hash, r.after_hash,
                       r.evidence_refs, r.created_at,
                       c.metadata AS consumption_metadata
                FROM cognitive_state_effect_receipts r
                JOIN cognitive_data_consumptions c
                  ON c.consumption_id=r.consumption_id
                WHERE r.command_id=?
                """,
                (permit.command_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            evidence_refs = tuple(str(value) for value in json.loads(str(row["evidence_refs"])))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("material-action receipt evidence is malformed") from exc
        try:
            consumption_metadata = json.loads(str(row["consumption_metadata"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("material-action receipt consumption metadata is malformed") from exc
        if not isinstance(consumption_metadata, Mapping):
            raise RuntimeError("material-action receipt consumption metadata must be an object")
        if (
            str(row["revision_id"]) != permit.decision_revision_id
            or str(row["target_effect_id"]) != permit.effect_id
        ):
            raise RuntimeError("material-action receipt does not match its command")
        prediction_evidence_refs = {
            "prediction-revision:"
            f"{ref['prediction_revision_id']}:{ref['prediction_revision_hash']}"
            for ref in permit.prediction_refs
        }
        if not prediction_evidence_refs.issubset(set(evidence_refs)):
            raise RuntimeError("material-action receipt lacks exact prediction revision evidence")
        return MaterialActionReceipt(
            receipt_id=str(row["receipt_id"]),
            command_id=permit.command_id,
            decision_revision_id=permit.decision_revision_id,
            action_id=permit.action_id,
            effect_id=permit.effect_id,
            status=str(row["status"]),
            before_hash=str(row["before_hash"]),
            after_hash=str(row["after_hash"]),
            evidence_refs=evidence_refs,
            reason_code=str(consumption_metadata.get("terminal_reason_code") or ""),
            retry_exhausted=(consumption_metadata.get("retry_exhausted") is True),
            created_at=str(row["created_at"]),
            prediction_refs=permit.prediction_refs,
        )

    def _validated_permit(
        self,
        command_id: str,
        *,
        executor_id: str,
        allow_terminal: bool,
    ) -> MaterialActionPermit:
        normalized_command = _required(command_id, "command_id")
        normalized_executor = _required(executor_id, "executor_id")
        with self.state_store._connect(read_only=True) as conn:  # noqa: SLF001
            if not decision_trace_enforcement_enabled(conn):
                raise RuntimeError("decision-trace enforcement migration_required")
            command = conn.execute(
                """
                SELECT command_id, revision_id, event_id, consumer_id,
                       command_type, payload_json, payload_hash, created_at
                FROM cognitive_state_outbox WHERE command_id=?
                """,
                (normalized_command,),
            ).fetchone()
            terminal = conn.execute(
                """
                SELECT receipt_id FROM cognitive_state_effect_receipts
                WHERE command_id=?
                """,
                (normalized_command,),
            ).fetchone()
        if command is None:
            raise ValueError("material-action command does not exist")
        if str(command["command_type"]) != MATERIAL_ACTION_COMMAND_TYPE:
            raise ValueError("command is not a material-action command")
        if terminal is not None and not allow_terminal:
            raise RuntimeError("material-action command is already terminal")
        try:
            payload = json.loads(str(command["payload_json"]))
        except json.JSONDecodeError as exc:
            raise RuntimeError("material-action command payload is malformed") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("material-action command payload is malformed")
        if sha256_json(payload) != str(command["payload_hash"]):
            raise RuntimeError("material-action command payload hash mismatch")
        if str(payload.get("executor") or "") != normalized_executor:
            raise PermissionError("material-action executor does not match its command")

        decision = self.state_store.revision(
            _required(payload.get("decision_revision_id"), "decision_revision_id")
        )
        snapshot = self.state_store.revision(
            _required(payload.get("snapshot_revision_id"), "snapshot_revision_id")
        )
        value_context = self.state_store.revision(
            _required(
                payload.get("value_context_revision_id"),
                "value_context_revision_id",
            )
        )
        if decision is None or decision.object_type != "decision_trace":
            raise RuntimeError("material-action decision revision is unavailable")
        if snapshot is None or snapshot.object_type != "cognitive_state_snapshot":
            raise RuntimeError("material-action snapshot revision is unavailable")
        if value_context is None or value_context.object_type != "value_context":
            raise RuntimeError("material-action ValueContext revision is unavailable")
        if decision.revision_id != str(command["revision_id"]):
            raise RuntimeError("material-action command is not bound to its decision")
        _verify_revision_payload_hash(decision)
        _verify_revision_payload_hash(snapshot)
        _verify_revision_payload_hash(value_context)
        _verify_snapshot_hash(snapshot)
        _verify_decision_bundle(
            self.state_store,
            decision=decision,
            snapshot=snapshot,
            value_context=value_context,
        )
        decision_payload = decision.payload
        if (
            decision_payload.get("snapshot_revision_id") != snapshot.revision_id
            or decision_payload.get("snapshot_hash") != snapshot.payload.get("snapshot_hash")
            or decision_payload.get("value_context_revision_id") != value_context.revision_id
            or decision_payload.get("value_context_hash") != value_context.payload_hash
            or payload.get("snapshot_hash") != snapshot.payload.get("snapshot_hash")
            or payload.get("value_context_hash") != value_context.payload_hash
            or payload.get("decision_hash") != decision.payload_hash
        ):
            raise RuntimeError("material-action canonical decision binding failed")
        action_id = _required(payload.get("action_id"), "action_id")
        action_matches = [
            dict(value)
            for value in decision_payload.get("action_specs", ())
            if isinstance(value, Mapping) and value.get("action_id") == action_id
        ]
        if len(action_matches) != 1:
            raise RuntimeError("material-action spec is unavailable or ambiguous")
        action = action_matches[0]
        for field_name in (
            "action_id",
            "effect_id",
            "action_type",
            "owner",
            "executor",
            "target_ref",
            "target_hash",
            "input_hash",
            "rollback_contract",
            "expected_effect",
        ):
            if action.get(field_name) != payload.get(field_name):
                raise RuntimeError(f"material-action {field_name} does not match its decision")
        if action.get("source_object") != payload.get("source_object"):
            raise RuntimeError("material-action source_object does not match its decision")
        if action_id not in decision_payload.get("action_refs", ()):
            raise RuntimeError("material-action ID is missing from decision refs")
        effect_id = _required(payload.get("effect_id"), "effect_id")
        if effect_id not in decision_payload.get("effect_refs", ()):
            raise RuntimeError("material-action effect is missing from decision refs")
        target_ref = _required(payload.get("target_ref"), "target_ref")
        target_hash = _sha256(payload.get("target_hash"), "target_hash")
        if target_hash != sha256_json(target_ref):
            raise RuntimeError("material-action target hash mismatch")
        command_prediction_refs = _normalize_material_prediction_refs(
            payload.get("prediction_refs", ())
        )
        decision_prediction_refs = tuple(
            dict(value)
            for value in decision_payload.get("prediction_refs", ())
            if isinstance(value, Mapping)
        )
        if (
            tuple(
                {
                    "prediction_id": value["prediction_id"],
                    "prediction_plan_hash": value["prediction_plan_hash"],
                }
                for value in command_prediction_refs
            )
            != decision_prediction_refs
        ):
            raise RuntimeError("material-action prediction refs do not match its decision")
        verified_prediction_ids = _verify_prediction_refs(
            self.state_store,
            decision,
        )
        if tuple(value["prediction_revision_id"] for value in command_prediction_refs) != (
            verified_prediction_ids
        ):
            raise RuntimeError("material-action prediction revisions are unavailable")
        for value in command_prediction_refs:
            revision = self.state_store.revision(value["prediction_revision_id"])
            if (
                revision is None
                or revision.object_type != "prediction_record"
                or revision.object_id != value["prediction_id"]
                or revision.payload_hash != value["prediction_revision_hash"]
            ):
                raise RuntimeError("material-action prediction revision hash mismatch")
        permit_identity: dict[str, Any] = {
            "schema_version": "mnemos.material_action_permit.v1",
            "command_id": normalized_command,
            "decision_revision_id": decision.revision_id,
            "action_id": action_id,
            "effect_id": effect_id,
            "action_type": _required(payload.get("action_type"), "action_type"),
            "owner": _required(payload.get("owner"), "owner"),
            "executor_id": normalized_executor,
            "target_ref": target_ref,
            "target_hash": target_hash,
            "input_hash": _sha256(payload.get("input_hash"), "input_hash"),
            "issued_at": str(command["created_at"]),
            "prediction_refs": command_prediction_refs,
        }
        return MaterialActionPermit(
            schema_version=str(permit_identity["schema_version"]),
            command_id=str(permit_identity["command_id"]),
            decision_revision_id=str(permit_identity["decision_revision_id"]),
            action_id=str(permit_identity["action_id"]),
            effect_id=str(permit_identity["effect_id"]),
            action_type=str(permit_identity["action_type"]),
            owner=str(permit_identity["owner"]),
            executor_id=str(permit_identity["executor_id"]),
            target_ref=str(permit_identity["target_ref"]),
            target_hash=str(permit_identity["target_hash"]),
            input_hash=str(permit_identity["input_hash"]),
            issued_at=str(permit_identity["issued_at"]),
            prediction_refs=command_prediction_refs,
            integrity_hash=sha256_json(permit_identity),
        )


def validate_material_receipt_observation(
    receipt: MaterialActionReceipt,
    observation: MaterialActionObservation,
) -> None:
    """Require a terminal receipt to equal the target's current observation."""

    observed_refs = set(observation.evidence_refs)
    receipt_refs = set(receipt.evidence_refs)
    if (
        receipt.status != observation.status
        or receipt.before_hash != observation.before_hash
        or receipt.after_hash != observation.after_hash
        or receipt.reason_code != observation.reason_code
        or receipt.retry_exhausted != observation.retry_exhausted
        or not observed_refs.issubset(receipt_refs)
    ):
        raise RuntimeError("target observation does not match its terminal receipt")


@dataclass(frozen=True)
class MaterialActionAuthorization:
    """Coordinator-backed capability that sinks must validate before effects."""

    coordinator: MaterialActionCoordinator
    permit: MaterialActionPermit

    def validate(
        self,
        *,
        owner: str,
        executor_id: str,
        action_type: str,
        target_ref: str,
        input_hash: str,
    ) -> MaterialActionPermit:
        """Validate this capability against an exact material sink binding."""

        if not isinstance(self.coordinator, MaterialActionCoordinator):
            raise PermissionError("canonical MaterialActionCoordinator is required")
        return self.coordinator.validate_for_effect(
            self.permit,
            owner=owner,
            executor_id=executor_id,
            action_type=action_type,
            target_ref=target_ref,
            input_hash=input_hash,
        )

    def record_terminal(
        self,
        terminal: MaterialActionTerminal,
    ) -> MaterialActionReceipt:
        """Record the terminal outcome through the backing coordinator."""

        return self.coordinator.record_terminal(self.permit, terminal)

    def recover(
        self,
        oracle: MaterialEffectOracle,
    ) -> MaterialActionReceipt | None:
        """Recover an already committed target effect through a typed oracle."""

        return self.coordinator.recover(
            self.permit.command_id,
            executor_id=self.permit.executor_id,
            oracle=oracle,
        )

    def terminal_receipt(self) -> MaterialActionReceipt | None:
        """Return the exact terminal receipt without authorizing a new effect."""

        validated = self.coordinator._validated_permit(  # noqa: SLF001
            self.permit.command_id,
            executor_id=self.permit.executor_id,
            allow_terminal=True,
        )
        if validated != self.permit:
            raise PermissionError("material-action permit binding is invalid")
        return self.coordinator._material_receipt(validated)  # noqa: SLF001

    def validate_projection(
        self,
        *,
        owner: str,
        executor_id: str,
        action_type: str,
        target_ref: str,
        input_hash: str,
        terminal_statuses: Sequence[str] = ("committed",),
    ) -> MaterialActionPermit:
        """Validate a projection that depends on an allowed terminal effect."""

        return self.coordinator.validate_for_projection(
            self.permit,
            owner=owner,
            executor_id=executor_id,
            action_type=action_type,
            target_ref=target_ref,
            input_hash=input_hash,
            terminal_statuses=terminal_statuses,
        )


def _validate_material_effect_fields(
    permit: MaterialActionPermit,
    *,
    owner: str,
    executor_id: str,
    action_type: str,
    target_ref: str,
    input_hash: str,
) -> None:
    actual = {
        "owner": _required(owner, "owner"),
        "executor_id": _required(executor_id, "executor_id"),
        "action_type": _required(action_type, "action_type"),
        "target_ref": _required(target_ref, "target_ref"),
        "input_hash": _sha256(input_hash, "input_hash"),
    }
    for field_name, value in actual.items():
        if getattr(permit, field_name) != value:
            raise PermissionError(f"material-action {field_name} does not match its permit")
