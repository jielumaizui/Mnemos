"""Append-only ActionLedger persistence facade."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from uuid import uuid4

from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionObservation,
    MaterialActionPermit,
    MaterialActionRequest,
    MaterialActionTerminal,
    authorize_exact_project_contract_action,
    require_material_action,
    require_material_action_projection,
    resolve_material_action_recovery_authorization,
)
from core.cognitive.state_contract import sha256_json
from core.ops.action_ledger_schema import (
    ActionLedgerSchemaError,
    initialize_action_ledger_schema,
    inspect_action_ledger_schema,
)
from core.ops.action_ledger_subject_provenance import (
    action_tombstone,
    record_action_subject_provenance,
    redact_action_projection,
)
from core.privacy.content_redaction import redact_persistence_value


ACTION_LEDGER_MATERIAL_OWNER = "action_ledger"
ACTION_LEDGER_MATERIAL_EXECUTOR = "action_ledger"
ACTION_LEDGER_DIAGNOSTIC_PROOF_KEY = "_diagnostic_observation"
ACTION_LEDGER_DIAGNOSTIC_SCHEMA_VERSION = (
    "mnemos.action_ledger_diagnostic_observation.v1"
)
_DIAGNOSTIC_RESULT_STATUSES = frozenset(
    {
        "degraded",
        "failed_terminal",
        "needs_user",
        "produced",
        "queued",
        "verified",
    }
)
_FORBIDDEN_DIAGNOSTIC_DETAIL_KEYS = frozenset(
    {
        "material_action",
        "material_input_hash",
        "observed_action_type",
        ACTION_LEDGER_DIAGNOSTIC_PROOF_KEY,
    }
)


class ActionLedgerObservationType(str, Enum):
    """Closed set of append-only diagnostics that do not authorize effects."""

    QUALITY_GATE = "quality_gate"
    COGNITIVE_READINESS = "cognitive_readiness_gap"
    DATA_INVENTORY = "data_inventory"
    BENCHMARK_CONSUMER = "benchmark_consumer_verify"
    GOLDEN_BENCHMARK = "golden_benchmark_observation"


@dataclass(frozen=True)
class ActionLedgerObservation:
    """Typed diagnostic payload with no before/after or rollback semantics.

    The public constructor intentionally accepts an enum rather than an action
    type string.  Production callers use the named factories below, while
    ``ActionLedger.record`` treats every generic ``ActionRecord`` as material.
    """

    observation_id: str
    actor: str
    observation_type: ActionLedgerObservationType
    target: str
    evidence_refs: tuple[str, ...]
    result_status: str = "verified"
    details: Mapping[str, Any] = field(default_factory=dict)
    decision_id: str = ""
    subject_provenance: Mapping[str, Any] | None = None
    observed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    schema_version: str = "mnemos.action_ledger.v1"

    @property
    def action_id(self) -> str:
        """Return the stable observation identifier in ledger form."""

        return self.observation_id

    @property
    def action_type(self) -> str:
        """Return the code-owned diagnostic observation type."""

        return self.observation_type.value

    @property
    def status(self) -> str:
        """Return the diagnostic verification status."""

        return self.result_status

    @property
    def verification(self) -> Mapping[str, Any]:
        """Return immutable diagnostic details for persistence."""

        return self.details

    @property
    def quality_decision_id(self) -> str:
        """Return the optional upstream decision identity."""

        return self.decision_id

    @property
    def created_at(self) -> str:
        """Return the observation timestamp in ledger form."""

        return self.observed_at

    before_ref = ""
    after_ref = ""
    rollback_ref = ""

    def validate(self) -> list[str]:
        """Validate that this row is diagnostic rather than a disguised effect."""

        errors: list[str] = []
        if not self.observation_id:
            errors.append("observation_id is required")
        if not self.actor:
            errors.append("actor is required")
        if type(self.observation_type) is not ActionLedgerObservationType:
            errors.append("canonical ActionLedger observation type is required")
        if not self.target:
            errors.append("target is required")
        if not self.evidence_refs or any(
            not str(value).strip() for value in self.evidence_refs
        ):
            errors.append("evidence_refs must be non-empty")
        if self.result_status not in _DIAGNOSTIC_RESULT_STATUSES:
            errors.append("unsupported diagnostic result status")
        if not isinstance(self.details, Mapping):
            errors.append("diagnostic details must be an object")
        else:
            forbidden = sorted(
                _FORBIDDEN_DIAGNOSTIC_DETAIL_KEYS.intersection(self.details)
            )
            if forbidden:
                errors.append(
                    "diagnostic details cannot encode material-action aliases: "
                    + ",".join(forbidden)
                )
        if self.before_ref or self.after_ref or self.rollback_ref:
            errors.append("diagnostic observations cannot encode effect refs")
        return errors


def _make_action_ledger_observation(
    observation_type: ActionLedgerObservationType,
    *,
    actor: str,
    target: str,
    evidence_refs: tuple[str, ...],
    result_status: str = "verified",
    details: Mapping[str, Any] | None = None,
    decision_id: str = "",
    subject_provenance: Mapping[str, Any] | None = None,
    observation_id: str = "",
    observed_at: str = "",
) -> ActionLedgerObservation:
    return ActionLedgerObservation(
        observation_id=observation_id or f"obsact-{uuid4().hex[:16]}",
        actor=actor,
        observation_type=observation_type,
        target=target,
        evidence_refs=tuple(evidence_refs),
        result_status=result_status,
        details=dict(details or {}),
        decision_id=decision_id,
        subject_provenance=(
            dict(subject_provenance) if subject_provenance is not None else None
        ),
        **({"observed_at": observed_at} if observed_at else {}),
    )


def make_quality_gate_observation(**kwargs: Any) -> ActionLedgerObservation:
    """Build a typed observation of an already computed quality decision."""

    return _make_action_ledger_observation(
        ActionLedgerObservationType.QUALITY_GATE,
        **kwargs,
    )


def make_cognitive_readiness_observation(**kwargs: Any) -> ActionLedgerObservation:
    """Build a typed observation of a read-only cognitive-readiness audit."""

    return _make_action_ledger_observation(
        ActionLedgerObservationType.COGNITIVE_READINESS,
        **kwargs,
    )


def make_data_inventory_observation(**kwargs: Any) -> ActionLedgerObservation:
    """Build a typed observation of a read-only data inventory."""

    return _make_action_ledger_observation(
        ActionLedgerObservationType.DATA_INVENTORY,
        **kwargs,
    )


def make_benchmark_consumer_observation(**kwargs: Any) -> ActionLedgerObservation:
    """Build a typed observation of a deterministic benchmark consumer check."""

    return _make_action_ledger_observation(
        ActionLedgerObservationType.BENCHMARK_CONSUMER,
        **kwargs,
    )


def make_golden_benchmark_observation(**kwargs: Any) -> ActionLedgerObservation:
    """Build a typed observation scoped to a hermetic golden benchmark run."""

    return _make_action_ledger_observation(
        ActionLedgerObservationType.GOLDEN_BENCHMARK,
        **kwargs,
    )


class ActionLedgerEffectOracle:
    """Read-only recovery oracle for an append-only ActionLedger row."""

    owner = ACTION_LEDGER_MATERIAL_OWNER
    executor_id = ACTION_LEDGER_MATERIAL_EXECUTOR

    def __init__(self, db_path: Path, *, action_type: str):
        self.db_path = Path(db_path)
        self.action_type = str(action_type)

    def observe(
        self,
        permit: MaterialActionPermit,
    ) -> MaterialActionObservation | None:
        """Return the exact append-only ActionLedger effect for recovery."""

        with sqlite3.connect(
            f"file:{self.db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM action_ledger WHERE action_id=?",
                (permit.action_id,),
            ).fetchone()
        if row is None:
            return None
        evidence_refs = {
            str(value) for value in json.loads(str(row["evidence_refs_json"]) or "[]")
        }
        verification = dict(
            json.loads(str(row["verification_json"]) or "{}")
        )
        primary_binding = verification.get("material_action")
        if not isinstance(primary_binding, dict):
            return None
        required_refs = {
            f"material-command:{permit.command_id}",
            f"decision-revision:{permit.decision_revision_id}",
            f"material-effect:{permit.effect_id}",
        }
        if (
            str(row["action_type"]) != permit.action_type
            or str(row["target"]) != permit.target_ref
            or str(row["quality_decision_id"]) != permit.decision_revision_id
            or str(verification.get("material_input_hash") or "")
            != permit.input_hash
            or primary_binding
            != {
                "command_id": permit.command_id,
                "decision_revision_id": permit.decision_revision_id,
                "action_id": permit.action_id,
                "effect_id": permit.effect_id,
                "action_type": permit.action_type,
                "owner": permit.owner,
                "executor_id": permit.executor_id,
                "target_ref": permit.target_ref,
                "input_hash": permit.input_hash,
            }
            or not required_refs.issubset(evidence_refs)
        ):
            raise RuntimeError(
                "existing ActionLedger row does not match its pending material command"
            )
        after_hash = sha256_json(dict(row))
        return MaterialActionObservation(
            status="committed",
            before_hash=sha256_json(None),
            after_hash=after_hash,
            evidence_refs=(
                f"target-after:{after_hash}",
                f"target-oracle:action-ledger:{permit.action_id}:{after_hash}",
            ),
            outcome="observed append-only action ledger row after restart",
            observed_at=str(row["created_at"]),
        )


def action_ledger_material_action_input_hash(record: Any) -> str:
    """Bind a primary ledger effect to the exact immutable record content."""

    return str(sha256_json(
        {
            "schema_version": str(record.schema_version),
            "actor": str(record.actor),
            "action_type": str(record.action_type),
            "target": str(record.target),
            "before_ref": str(record.before_ref),
            "after_ref": str(record.after_ref),
            "evidence_refs": list(record.evidence_refs),
            "verification": dict(record.verification),
            "rollback_ref": str(record.rollback_ref),
            "status": str(record.status),
            "created_at": str(record.created_at),
        }
    ))


def _diagnostic_persisted_payload(
    *,
    schema_version: str,
    actor: str,
    action_type: str,
    target: str,
    evidence_refs: list[str],
    quality_decision_id: str,
    verification: Mapping[str, Any],
    status: str,
    created_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "actor": actor,
        "action_type": action_type,
        "target": target,
        "before_ref": "",
        "after_ref": "",
        "evidence_refs": list(evidence_refs),
        "quality_decision_id": quality_decision_id,
        "verification": dict(verification),
        "rollback_ref": "",
        "status": status,
        "created_at": created_at,
    }


def verify_action_ledger_diagnostic_row(row: Mapping[str, Any]) -> bool:
    """Verify a persisted row's system-issued non-material diagnostic proof."""

    try:
        action_type = ActionLedgerObservationType(str(row["action_type"]))
        evidence_refs = json.loads(str(row["evidence_refs_json"]))
        verification = json.loads(str(row["verification_json"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(evidence_refs, list) or not isinstance(verification, dict):
        return False
    proof = verification.pop(ACTION_LEDGER_DIAGNOSTIC_PROOF_KEY, None)
    if not isinstance(proof, Mapping):
        return False
    payload = _diagnostic_persisted_payload(
        schema_version=str(row.get("schema_version") or ""),
        actor=str(row.get("actor") or ""),
        action_type=action_type.value,
        target=str(row.get("target") or ""),
        evidence_refs=[str(value) for value in evidence_refs],
        quality_decision_id=str(row.get("quality_decision_id") or ""),
        verification=verification,
        status=str(row.get("status") or ""),
        created_at=str(row.get("created_at") or ""),
    )
    return (
        not str(row.get("before_ref") or "")
        and not str(row.get("after_ref") or "")
        and not str(row.get("rollback_ref") or "")
        and dict(proof)
        == {
            "schema_version": ACTION_LEDGER_DIAGNOSTIC_SCHEMA_VERSION,
            "observation_type": action_type.value,
            "payload_hash": sha256_json(payload),
        }
    )


def authorize_primary_action_ledger_record(
    record: Any,
    *,
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
) -> MaterialActionAuthorization:
    """Authorize one exact primary append to ActionLedger.

    This is identity plumbing only.  The domain caller must provide immutable
    source facts, independent eligibility checks, real candidate semantics,
    and evidence.  It cannot authorize an underlying effect that already ran;
    the returned capability governs only the append-only ledger row.
    """

    if type(record) is ActionLedgerObservation:
        raise TypeError("diagnostic observations do not use material decisions")
    validation_errors = tuple(str(value) for value in record.validate())
    if validation_errors:
        raise ValueError("; ".join(validation_errors))
    if ACTION_LEDGER_DIAGNOSTIC_PROOF_KEY in dict(record.verification or {}):
        raise ValueError("ActionLedger diagnostic proof key is system-owned")
    facts = {
        **dict(source_facts),
        "action_ledger_record_hash": action_ledger_material_action_input_hash(record),
        "action_type": str(record.action_type),
        "target": str(record.target),
    }
    checks = {
        **dict(decision_checks),
        "action_record_contract_valid": not validation_errors,
    }
    return authorize_exact_project_contract_action(
        expected_request=MaterialActionRequest(
            owner=ACTION_LEDGER_MATERIAL_OWNER,
            executor_id=ACTION_LEDGER_MATERIAL_EXECUTOR,
            action_type=str(record.action_type),
            target_ref=str(record.target),
            input_hash=action_ledger_material_action_input_hash(record),
            expected_state_db=str(Path(state_db_path)),
        ),
        state_db_path=Path(state_db_path),
        contract_id=contract_id,
        contract_revision_id=contract_revision_id,
        contract_text=contract_text,
        source_namespace=source_namespace,
        source_facts=facts,
        decision_checks=checks,
        evidence_refs=evidence_refs,
        task=task,
        goal=goal,
        constraints=constraints,
        created_at=str(record.created_at),
        producer=producer,
        producer_version=producer_version,
        producer_code_hash=producer_code_hash,
        evaluator_id=evaluator_id,
        approved_candidate_key=approved_candidate_key,
        approved_candidate_summary=approved_candidate_summary,
        rejected_candidate_key=rejected_candidate_key,
        rejected_candidate_summary=rejected_candidate_summary,
        approved_reason_code=approved_reason_code,
        rejected_reason_code=rejected_reason_code,
        committed_metric=committed_metric,
        rejected_metric=rejected_metric,
    )


class ActionLedger:
    """Persist operational action evidence without replacement semantics."""

    def __init__(
        self,
        db_path: Path,
        *,
        initialize: bool = False,
        ownership_config: Any | None = None,
    ):
        self.db_path = Path(db_path)
        if ownership_config is None:
            from core.config import get_config

            ownership_config = get_config()
        self._ownership_config = ownership_config
        self.__persistence_nonce = object()
        if initialize:
            initialize_action_ledger_schema(self.db_path)
        elif self.db_path.is_file():
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                state = inspect_action_ledger_schema(conn)
            if not state.ok:
                raise ActionLedgerSchemaError(
                    "action ledger migration required; run scripts/reconcile_action_ledger.py"
                )

    @classmethod
    def from_config(cls, config: Any, *, initialize: bool = False) -> "ActionLedger":
        return cls(
            Path(config.database_dir) / "action_ledger.db",
            initialize=initialize,
            ownership_config=config,
        )

    def record(
        self,
        record: Any,
        *,
        material_action: MaterialActionAuthorization | None = None,
    ) -> str:
        """Persist a material action only under an exact typed authorization."""

        if type(record) is ActionLedgerObservation:
            raise TypeError(
                "typed diagnostics must use ActionLedger.record_observation"
            )
        if not self.db_path.is_file():
            raise FileNotFoundError(
                f"action ledger is not initialized: {self.db_path}"
            )
        errors = record.validate()
        if errors:
            raise ValueError("; ".join(errors))
        if ACTION_LEDGER_DIAGNOSTIC_PROOF_KEY in dict(record.verification or {}):
            raise ValueError("ActionLedger diagnostic proof key is system-owned")
        # Reject a frozen subject before resolving or consuming an
        # authorization.  The write transaction repeats this check.
        self._assert_subject_not_frozen(record)
        if material_action is not None:
            expected_state_db = (
                self.db_path.parent / "producer_consumer_ledger.db"
            ).resolve(strict=False)
            actual_state_db = Path(
                material_action.coordinator.state_store.db_path
            ).resolve(strict=False)
            if actual_state_db != expected_state_db:
                raise PermissionError(
                    "material-action authorization belongs to a foreign canonical store"
                )
            permit = material_action.permit
            terminal_receipt = material_action.terminal_receipt()
            oracle = ActionLedgerEffectOracle(
                self.db_path,
                action_type=str(record.action_type),
            )
            if oracle.observe(permit) is not None:
                recovered = material_action.recover(oracle)
                if recovered is None:
                    raise RuntimeError(
                        "ActionLedger target exists without a recoverable receipt"
                    )
                return str(material_action.permit.action_id)
            if terminal_receipt is not None:
                return self._record_material_projection(record, material_action)
            material_action, _permit = resolve_material_action_recovery_authorization(
                material_action,
                owner=ACTION_LEDGER_MATERIAL_OWNER,
                executor_id=ACTION_LEDGER_MATERIAL_EXECUTOR,
                action_type=str(record.action_type),
                target_ref=str(record.target),
                input_hash=action_ledger_material_action_input_hash(record),
                expected_state_db=expected_state_db,
            )
            return self._record_primary_material(record, material_action)
        raise PermissionError(
            "canonical material-action authorization is required"
        )

    def record_observation(self, observation: ActionLedgerObservation) -> str:
        """Persist one typed diagnostic without granting any material effect."""

        if type(observation) is not ActionLedgerObservation:
            raise TypeError("canonical ActionLedgerObservation is required")
        if not self.db_path.is_file():
            raise FileNotFoundError(
                f"action ledger is not initialized: {self.db_path}"
            )
        errors = observation.validate()
        if errors:
            raise ValueError("; ".join(errors))
        self._assert_subject_not_frozen(observation)
        return self._record_diagnostic_observation(observation)

    def _record_diagnostic_observation(
        self,
        observation: ActionLedgerObservation,
    ) -> str:
        return self._persist_action_ledger_record(
            observation,
            action_id=str(observation.action_id),
            primary_authorization=None,
            permit=None,
            diagnostic_observation=True,
            persistence_nonce=self.__persistence_nonce,
        )

    def _assert_subject_not_frozen(self, record: Any) -> None:
        subject_provenance = getattr(record, "subject_provenance", None)
        if subject_provenance is None:
            return
        from core.privacy.ownership_freeze import (
            assert_cognitive_write_not_frozen,
        )

        assert_cognitive_write_not_frozen(
            self._ownership_config,
            subject_provenance,
            domain="action ledger",
        )

    def _record_primary_material(
        self,
        record: Any,
        authorization: MaterialActionAuthorization,
    ) -> str:
        input_hash = action_ledger_material_action_input_hash(record)
        permit = authorization.permit
        oracle = ActionLedgerEffectOracle(
            self.db_path,
            action_type=str(record.action_type),
        )
        recovered = authorization.recover(oracle)
        if recovered is not None:
            if oracle.observe(permit) is None:
                raise RuntimeError(
                    "terminal ActionLedger receipt lacks exact target evidence"
                )
            return str(permit.action_id)
        permit = require_material_action(
            authorization,
            owner=ACTION_LEDGER_MATERIAL_OWNER,
            executor_id=ACTION_LEDGER_MATERIAL_EXECUTOR,
            action_type=str(record.action_type),
            target_ref=str(record.target),
            input_hash=input_hash,
            expected_state_db=(
                self.db_path.parent / "producer_consumer_ledger.db"
            ),
        )
        return self._persist_action_ledger_record(
            record,
            action_id=str(permit.action_id),
            primary_authorization=authorization,
            permit=permit,
            diagnostic_observation=False,
            persistence_nonce=self.__persistence_nonce,
        )

    def _record_material_projection(
        self,
        record: Any,
        authorization: MaterialActionAuthorization,
    ) -> str:
        if not isinstance(authorization, MaterialActionAuthorization):
            raise PermissionError(
                "canonical material-action authorization is required"
            )
        permit = authorization.permit
        verification = dict(record.verification or {})
        input_hash = str(verification.get("material_input_hash") or "")
        terminal_statuses = (
            ("failed_terminal",)
            if str(record.status) == "failed_terminal"
            else ("committed",)
        )
        require_material_action_projection(
            authorization,
            owner=permit.owner,
            executor_id=permit.executor_id,
            action_type=str(record.action_type),
            target_ref=str(record.target),
            input_hash=input_hash,
            terminal_statuses=terminal_statuses,
            expected_state_db=(
                self.db_path.parent / "producer_consumer_ledger.db"
            ),
        )
        if str(record.action_id) != permit.action_id:
            raise PermissionError(
                "action ledger action_id does not match its material permit"
            )
        if str(record.quality_decision_id) != permit.decision_revision_id:
            raise PermissionError(
                "action ledger decision revision does not match its material permit"
            )
        required_refs = {
            f"material-command:{permit.command_id}",
            f"decision-revision:{permit.decision_revision_id}",
            f"material-effect:{permit.effect_id}",
        }
        if not required_refs.issubset(
            {str(ref) for ref in record.evidence_refs}
        ):
            raise PermissionError(
                "action ledger record lacks reciprocal material evidence"
            )
        return self._persist_action_ledger_record(
            record,
            action_id=str(record.action_id),
            primary_authorization=None,
            permit=None,
            diagnostic_observation=False,
            persistence_nonce=self.__persistence_nonce,
        )

    def _persist_action_ledger_record(
        self,
        record: Any,
        *,
        action_id: str,
        primary_authorization: MaterialActionAuthorization | None,
        permit: MaterialActionPermit | None,
        diagnostic_observation: bool,
        persistence_nonce: object,
    ) -> str:
        if persistence_nonce is not self.__persistence_nonce:
            raise PermissionError(
                "ActionLedger persistence requires an internal validated capability"
            )
        if (primary_authorization is None) != (permit is None):
            raise ValueError(
                "primary authorization and permit must be provided together"
            )
        if diagnostic_observation and permit is not None:
            raise ValueError("diagnostic observations cannot consume a material permit")
        evidence_refs = list(record.evidence_refs)
        quality_decision_id = str(record.quality_decision_id)
        verification = dict(record.verification)
        if permit is not None:
            evidence_refs.extend(
                (
                    f"material-command:{permit.command_id}",
                    f"decision-revision:{permit.decision_revision_id}",
                    f"material-effect:{permit.effect_id}",
                )
            )
            evidence_refs = list(dict.fromkeys(evidence_refs))
            quality_decision_id = permit.decision_revision_id
            verification["material_input_hash"] = permit.input_hash
            verification["material_action"] = {
                "command_id": permit.command_id,
                "decision_revision_id": permit.decision_revision_id,
                "action_id": permit.action_id,
                "effect_id": permit.effect_id,
                "action_type": permit.action_type,
                "owner": permit.owner,
                "executor_id": permit.executor_id,
                "target_ref": permit.target_ref,
                "input_hash": permit.input_hash,
            }
        redacted = redact_persistence_value(
            {
                "actor": record.actor,
                "action_type": record.action_type,
                "target": record.target,
                "before_ref": record.before_ref,
                "after_ref": record.after_ref,
                "evidence_refs": evidence_refs,
                "quality_decision_id": quality_decision_id,
                "verification": verification,
                "rollback_ref": record.rollback_ref,
                "status": record.status,
            }
        ).value
        if not isinstance(redacted, dict):
            raise ValueError("redacted action record must be an object")
        persisted_verification = dict(redacted["verification"])
        if ACTION_LEDGER_DIAGNOSTIC_PROOF_KEY in persisted_verification:
            raise ValueError("ActionLedger diagnostic proof key is system-owned")
        if diagnostic_observation:
            try:
                observation_type = ActionLedgerObservationType(
                    str(redacted["action_type"])
                )
            except ValueError as exc:
                raise ValueError(
                    "canonical ActionLedger observation type is required"
                ) from exc
            payload = _diagnostic_persisted_payload(
                schema_version=str(record.schema_version),
                actor=str(redacted["actor"]),
                action_type=observation_type.value,
                target=str(redacted["target"]),
                evidence_refs=[str(value) for value in redacted["evidence_refs"]],
                quality_decision_id=str(redacted["quality_decision_id"]),
                verification=persisted_verification,
                status=str(redacted["status"]),
                created_at=str(record.created_at),
            )
            persisted_verification[ACTION_LEDGER_DIAGNOSTIC_PROOF_KEY] = {
                "schema_version": ACTION_LEDGER_DIAGNOSTIC_SCHEMA_VERSION,
                "observation_type": observation_type.value,
                "payload_hash": sha256_json(payload),
            }
        redacted["verification"] = persisted_verification
        existing_replay = False
        with sqlite3.connect(str(self.db_path), timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM action_ledger WHERE action_id=?",
                (action_id,),
            ).fetchone()
            before_hash = sha256_json(dict(existing) if existing is not None else None)
            if existing is not None:
                stored = (
                    str(existing[1]),
                    str(existing[2]),
                    str(existing[3]),
                    str(existing[4]),
                    str(existing[5]),
                    str(existing[6]),
                    tuple(str(value) for value in json.loads(str(existing[7]) or "[]")),
                    str(existing[8]),
                    dict(json.loads(str(existing[9]) or "{}")),
                    str(existing[10]),
                    str(existing[11]),
                    str(existing[12]),
                )
                incoming = (
                    record.schema_version,
                    str(redacted["actor"]),
                    str(redacted["action_type"]),
                    str(redacted["target"]),
                    str(redacted["before_ref"]),
                    str(redacted["after_ref"]),
                    tuple(str(value) for value in redacted["evidence_refs"]),
                    str(redacted["quality_decision_id"]),
                    dict(redacted["verification"]),
                    str(redacted["rollback_ref"]),
                    str(redacted["status"]),
                    record.created_at,
                )
                if stored != incoming:
                    raise ValueError(
                        f"immutable action record conflict for action_id={action_id}"
                    )
                record_action_subject_provenance(
                    conn,
                    action_id=action_id,
                    subject_provenance=getattr(record, "subject_provenance", None),
                    ownership_config=self._ownership_config,
                )
                existing_replay = True
            else:
                conn.execute(
                    """
                    INSERT INTO action_ledger (
                        action_id, schema_version, actor, action_type, target,
                        before_ref, after_ref, evidence_refs_json, quality_decision_id,
                        verification_json, rollback_ref, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action_id,
                        record.schema_version,
                        str(redacted["actor"]),
                        str(redacted["action_type"]),
                        str(redacted["target"]),
                        str(redacted["before_ref"]),
                        str(redacted["after_ref"]),
                        json.dumps(list(redacted["evidence_refs"]), ensure_ascii=False),
                        str(redacted["quality_decision_id"]),
                        json.dumps(dict(redacted["verification"]), ensure_ascii=False),
                        str(redacted["rollback_ref"]),
                        str(redacted["status"]),
                        record.created_at,
                    ),
                )
                record_action_subject_provenance(
                    conn,
                    action_id=action_id,
                    subject_provenance=getattr(record, "subject_provenance", None),
                    ownership_config=self._ownership_config,
                )
            stored_row = conn.execute(
                "SELECT * FROM action_ledger WHERE action_id=?",
                (action_id,),
            ).fetchone()
            after_hash = sha256_json(dict(stored_row) if stored_row is not None else None)
        if primary_authorization is not None and permit is not None:
            if existing_replay:
                oracle = ActionLedgerEffectOracle(
                    self.db_path,
                    action_type=str(record.action_type),
                )
                recovered = primary_authorization.recover(
                    oracle
                )
                if recovered is None:
                    raise RuntimeError(
                        "ActionLedger replay could not observe its existing effect"
                    )
            else:
                primary_authorization.record_terminal(
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
                            f"target-oracle:action-ledger:{action_id}:{after_hash}",
                        ),
                        outcome="append-only action ledger record committed",
                        created_at=str(record.created_at),
                    )
                )
        return action_id

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.db_path.is_file():
            raise FileNotFoundError(
                f"action ledger is not initialized: {self.db_path}"
            )
        with sqlite3.connect(str(self.db_path), timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM action_ledger ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["evidence_refs"] = json.loads(item.pop("evidence_refs_json") or "[]")
                item["verification"] = json.loads(item.pop("verification_json") or "{}")
                tombstone = action_tombstone(conn, str(item["action_id"]))
                result.append(
                    redact_action_projection(item, tombstone) if tombstone is not None else item
                )
            return result
