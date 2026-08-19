"""Canonical pre-effect PredictionRecord lifecycle over CognitiveStateStore.

The ledger owns prediction identity, immutable inputs, maturity, eligible
OutcomeMeasurement binding, categorical error, and deterministic calibration.
Delivery databases are projections; reactions may prove exposure but never
objective usefulness.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Any, Callable, Mapping

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import (
    authorize_cognitive_write,
    cognitive_access_hash,
    validate_cognitive_access_envelope,
)
from core.cognitive.state_contract import (
    COGNITIVE_OBJECT_SCHEMA_VERSIONS,
    CognitiveHeadPrecondition,
    CognitiveStateRevision,
    LocalConsumerCommand,
    canonical_json,
    prediction_input_snapshot,
    sha256_json,
    validate_cognitive_state_payload,
)
from core.cognitive.state_schema import (
    initialize_cognitive_state_schema,
    prediction_enforcement_enabled,
)
from core.cognitive.prediction_ledger_support import (
    digest as _digest,
    file_sha256 as _file_sha256,
    normalized_route_payload as _normalized_route_payload,
    now as _now,
    prediction_access_control as _prediction_access_control,
    prediction_id as _prediction_id,
    required_text as _required,
    route_disposition as _route_disposition,
    score_band as _score_band,
    system_principal as _system_principal,
    terminal_matches as _terminal_matches,
    timestamp as _timestamp,
    window_hours as _window_hours,
)
from core.cognitive.prediction_outcome_support import (
    OUTCOME_MEASUREMENT_ISSUANCE_SCHEMA as OUTCOME_MEASUREMENT_ISSUANCE_SCHEMA,
    OUTCOME_MEASUREMENT_METHOD_REGISTRY as OUTCOME_MEASUREMENT_METHOD_REGISTRY,
    OUTCOME_MEASUREMENT_REGISTRY_HASH as OUTCOME_MEASUREMENT_REGISTRY_HASH,
    TASK_RESULT_OBSERVATION_SCHEMA as TASK_RESULT_OBSERVATION_SCHEMA,
    TASK_RESULT_ORACLE_CODE_HASH as TASK_RESULT_ORACLE_CODE_HASH,
    TASK_RESULT_ORACLE_ISSUER_ID as TASK_RESULT_ORACLE_ISSUER_ID,
    TASK_RESULT_ORACLE_METHOD_ID as TASK_RESULT_ORACLE_METHOD_ID,
    ObjectiveMeasurementIssuance as ObjectiveMeasurementIssuance,
    TaskResultOracle as TaskResultOracle,
    build_calibration_report,
    outcome_issuance_receipt_valid,
    reissue_objective_measurement,
)
from core.cognitive.state_store import CognitiveStateConflict, CognitiveStateStore
from core.ops.cognitive_data_contract import CognitiveDataEvent


__all__ = (
    "OUTCOME_MEASUREMENT_ISSUANCE_SCHEMA",
    "OUTCOME_MEASUREMENT_METHOD_REGISTRY",
    "OUTCOME_MEASUREMENT_REGISTRY_HASH",
    "TASK_RESULT_OBSERVATION_SCHEMA",
    "TASK_RESULT_ORACLE_CODE_HASH",
    "TASK_RESULT_ORACLE_ISSUER_ID",
    "TASK_RESULT_ORACLE_METHOD_ID",
    "ObjectiveMeasurementIssuance",
    "TaskResultOracle",
)


PREDICTION_KIND = "predictive_delivery_usefulness"
PREDICTION_METRIC_ID = "predictive_delivery_usefulness"
PREDICTION_UNIT = "class_label"
PREDICTION_CONFIDENCE_METHOD = "delivery_policy_score_band.v1"
PREDICTION_CONFIDENCE_VERSION = "v1"
PREDICTION_DEFAULT_WINDOW_HOURS = 168
PREDICTION_PROJECTION_CONSUMER = "prediction_delivery_projection"
PREDICTION_PROJECTION_COMMAND = "project_prediction_delivery"
PREDICTION_TERMINAL_CONSUMER = "prediction_calibration_read_model"
PREDICTION_TERMINAL_COMMAND = "project_prediction_terminal"
PREDICTION_OUTCOME_CONSUMER = "prediction_outcome_projection"
PREDICTION_OUTCOME_COMMAND = "project_prediction_outcome"
PREDICTION_CORRECTION_CONSUMER = "prediction_terminal_correction"
PREDICTION_CORRECTION_COMMAND = "correct_prediction_terminal_from_outcome"
PREDICTION_TERMINAL_STATES = frozenset(
    {"measured", "unknown", "censored", "confounded"}
)


_PREDICTION_CODE_PATH = Path(__file__).resolve()
_PREDICTION_SUPPORT_CODE_PATH = _PREDICTION_CODE_PATH.with_name(
    "prediction_ledger_support.py"
)
_PREDICTION_OUTCOME_CODE_PATH = _PREDICTION_CODE_PATH.with_name(
    "prediction_outcome_support.py"
)
_PREDICTION_SPEC_PATH = (
    _PREDICTION_CODE_PATH.parents[2]
    / "docs/superpowers/specs/2026-07-18-cog037-prediction-ledger-design.md"
)
PREDICTION_CODE_HASH = sha256_json(
    {
        _PREDICTION_CODE_PATH.name: _file_sha256(_PREDICTION_CODE_PATH),
        _PREDICTION_SUPPORT_CODE_PATH.name: _file_sha256(
            _PREDICTION_SUPPORT_CODE_PATH
        ),
        _PREDICTION_OUTCOME_CODE_PATH.name: _file_sha256(
            _PREDICTION_OUTCOME_CODE_PATH
        ),
    }
)
PREDICTION_SPEC_HASH = _file_sha256(_PREDICTION_SPEC_PATH)


@dataclass(frozen=True)
class PredictionPlan:
    """System-created immutable facts for one predictive route decision."""

    prediction_id: str
    prediction_plan_hash: str
    route_payload: Mapping[str, Any]
    delivery_event_id: str
    delivery_event_payload_hash: str
    route_disposition: str
    predicted_value: str
    score_band: str
    starts_at: str
    ends_at: str
    window_config_hash: str
    scope_type: str
    scope_id: str
    access_control_hash: str
    access_control: Mapping[str, Any]

    def decision_ref(self) -> dict[str, str]:
        """Return the plan identity embedded in its DecisionTrace."""

        return {
            "prediction_id": self.prediction_id,
            "prediction_plan_hash": self.prediction_plan_hash,
        }


@dataclass(frozen=True)
class PredictionSealReceipt:
    """Receipt for the atomic open PredictionRecord seal."""

    status: str
    event_id: str
    prediction_id: str
    revision_id: str
    revision_hash: str
    command_id: str = ""
    projection_effect_id: str = ""
    transaction_hash: str = ""

    def material_ref(self) -> dict[str, str]:
        """Return the exact revision reference copied to delivery effects."""

        return {
            "prediction_id": self.prediction_id,
            "prediction_revision_id": self.revision_id,
            "prediction_revision_hash": self.revision_hash,
        }


@dataclass(frozen=True)
class PredictionTerminalReceipt:
    """Receipt for one immutable terminal PredictionRecord revision."""

    status: str
    event_id: str
    prediction_id: str
    revision_id: str
    terminal_state: str
    outcome_revision_id: str
    transaction_hash: str


@dataclass(frozen=True)
class MaturityBatchReceipt:
    """Bounded daemon batch result split by terminal state."""

    selected: int
    measured: int
    unknown: int
    censored: int
    confounded: int
    existing: int
    failed: int
    remaining_mature_open: int
    revision_ids: tuple[str, ...] = ()
    failed_prediction_ids: tuple[str, ...] = ()
    retryable_failed: int = 0
    terminal_failed: int = 0
    retryable_failed_prediction_ids: tuple[str, ...] = ()
    terminal_failed_prediction_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PredictionVerification:
    """Independent verification receipt for one prediction revision."""

    status: str
    prediction_id: str
    revision_id: str
    terminal_state: str
    prediction_input_hash: str
    outcome_revision_id: str
    verification_hash: str


@dataclass(frozen=True)
class PredictionCalibrationReport:
    """Deterministic categorical coverage and confusion-matrix report."""

    status: str
    metric_id: str
    method: str
    method_version: str
    code_hash: str
    spec_hash: str
    starts_at: str
    ends_at: str
    measured: int
    unknown: int
    censored: int
    confounded: int
    calibration_eligible: int
    correct: int
    incorrect: int
    accuracy: float | None
    confusion_matrix: Mapping[str, Mapping[str, int]]
    exclusions: Mapping[str, int]
    coverage_ratios: Mapping[str, float]
    input_hash: str
    report_hash: str


def _canonical_prediction_plan(
    route_facts: Mapping[str, Any],
    config: Any | None,
) -> PredictionPlan:
    """Derive every system-owned plan field from route facts and config."""

    if not isinstance(route_facts, Mapping):
        raise TypeError("route_facts must be an object")
    forbidden = {
        "prediction_id",
        "prediction_input_hash",
        "route_disposition",
        "predicted_value",
        "score_band",
        "evaluation_window",
        "access_control",
    }
    supplied = sorted(forbidden.intersection(route_facts))
    if supplied:
        raise ValueError(
            "prediction identity and semantics are system-owned: "
            + ",".join(supplied)
        )
    route = _normalized_route_payload(route_facts)
    if route["channel"] != "predictive_push":
        raise ValueError("only predictive_push routes create PredictionRecords")
    event_id = _required(route["event_id"], "delivery event_id")
    disposition = _route_disposition(
        str(route["decision"]),
        str(route["delivered_level"]),
    )
    score_band = _score_band(
        float(route["trust_score"]),
        float(route["task_fit_score"]),
    )
    predicted_value = "not_useful" if disposition == "suppress" else "useful"
    starts_at = _timestamp(route.get("created_at") or _now()).isoformat()
    window_hours = _window_hours(config, PREDICTION_DEFAULT_WINDOW_HOURS)
    ends_at = (_timestamp(starts_at) + timedelta(hours=window_hours)).isoformat()
    config_hash = sha256_json(
        {
            "key": "prediction.predictive_delivery_window_hours",
            "value": window_hours,
            "source": "global_effective_config",
        }
    )
    source_access = validate_cognitive_access_envelope(
        route["source_access_control"]
    )
    scope_type = _required(
        source_access["scope"]["scope_type"],
        "prediction scope_type",
    )
    scope_id = _required(
        source_access["scope"]["scope_id"],
        "prediction scope_id",
    )
    route_hash = sha256_json(dict(route))
    access_control = _prediction_access_control(
        source_access_control=source_access,
    )
    access_control_hash = cognitive_access_hash(access_control)
    seed = {
        "schema_version": "mnemos.prediction_plan.v1",
        "delivery_event_id": event_id,
        "delivery_event_payload_hash": route_hash,
        "route_disposition": disposition,
        "predicted_value": predicted_value,
        "score_band": score_band,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "window_config_hash": config_hash,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "access_control_hash": access_control_hash,
    }
    prediction_id = "prediction-" + _digest(seed)[:32]
    plan_hash = sha256_json({**seed, "prediction_id": prediction_id})
    return PredictionPlan(
        prediction_id=prediction_id,
        prediction_plan_hash=plan_hash,
        route_payload=MappingProxyType(route),
        delivery_event_id=event_id,
        delivery_event_payload_hash=route_hash,
        route_disposition=disposition,
        predicted_value=predicted_value,
        score_band=score_band,
        starts_at=starts_at,
        ends_at=ends_at,
        window_config_hash=config_hash,
        scope_type=scope_type,
        scope_id=scope_id,
        access_control_hash=access_control_hash,
        access_control=MappingProxyType(access_control),
    )


class PredictionRecordStore:
    """Deep owner of prediction preparation, lifecycle, and read models."""

    def __init__(
        self,
        state_store_or_path: CognitiveStateStore | Path | str | Any,
        *,
        config: Any | None = None,
    ) -> None:
        if isinstance(state_store_or_path, CognitiveStateStore):
            self.state_store = state_store_or_path
            self.config = config or state_store_or_path.config
        else:
            self.state_store = CognitiveStateStore(state_store_or_path)
            self.config = config or self.state_store.config

    def prepare_route_prediction(
        self,
        route_facts: Mapping[str, Any],
    ) -> PredictionPlan:
        """Freeze one exact predictive delivery route before any route effect."""

        return _canonical_prediction_plan(route_facts, self.config)

    def build_atomic_revision(
        self,
        plan: PredictionPlan,
        *,
        event_id: str,
        source_revision_id: str,
        source_content_hash: str,
        decision_revision: CognitiveStateRevision,
        action_spec: Mapping[str, Any],
        access_control: Mapping[str, Any],
        created_at: str,
    ) -> CognitiveStateRevision:
        """Build the prediction revision committed inside a DecisionTrace UoW."""

        self._require_plan(plan)
        self._require_enforcement()
        if decision_revision.object_type != "decision_trace":
            raise ValueError("atomic prediction requires a DecisionTrace revision")
        validate_cognitive_access_envelope(
            access_control,
            expected_scope_type=decision_revision.scope_type,
            expected_scope_id=decision_revision.scope_id,
        )
        prediction_access = validate_cognitive_access_envelope(
            plan.access_control,
            expected_scope_type=plan.scope_type,
            expected_scope_id=plan.scope_id,
        )
        payload = self._open_payload(
            plan,
            decision_ref={
                "kind": "decision_trace",
                "decision_id": decision_revision.object_id,
                "revision_id": decision_revision.revision_id,
                "revision_hash": decision_revision.payload_hash,
            },
            action_ref={
                "action_id": _required(action_spec.get("action_id"), "action_id"),
                "effect_id": _required(action_spec.get("effect_id"), "effect_id"),
            },
            access_control=prediction_access,
            scope_type=plan.scope_type,
            scope_id=plan.scope_id,
        )
        return CognitiveStateRevision.create(
            object_type="prediction_record",
            object_id=plan.prediction_id,
            source_event_id=event_id,
            source_revision_id=source_revision_id,
            source_content_hash=source_content_hash,
            scope_type=plan.scope_type,
            scope_id=plan.scope_id,
            evidence_refs=self._prediction_evidence(plan),
            payload=payload,
            created_at=created_at,
        )

    def seal_nonmaterial(
        self,
        plan: PredictionPlan,
        principal: PrincipalEnvelope | None = None,
        *,
        _failpoint: Callable[[str], None] | None = None,
    ) -> PredictionSealReceipt:
        """Commit a suppress PredictionRecord before its non-material event."""

        self._require_plan(plan)
        if plan.route_disposition != "suppress":
            raise ValueError("seal_nonmaterial only accepts suppress predictions")
        self._require_enforcement()
        replay = self._seal_receipt(plan.prediction_id)
        if replay is not None:
            return replay
        effective_principal = principal or _system_principal()
        access = validate_cognitive_access_envelope(
            plan.access_control,
            expected_scope_type=plan.scope_type,
            expected_scope_id=plan.scope_id,
        )
        if effective_principal.principal_id != access["owner"]["principal_id"]:
            raise PermissionError("prediction writer does not own the route evidence")
        source_hash = plan.delivery_event_payload_hash
        source_revision_id = f"delivery-plan:{plan.prediction_plan_hash}"
        event_id = "cogevent-" + _digest(
            {
                "operation": "seal_nonmaterial_prediction",
                "prediction_id": plan.prediction_id,
                "prediction_plan_hash": plan.prediction_plan_hash,
            }
        )[:32]
        payload = self._open_payload(
            plan,
            decision_ref={
                "kind": "trust_decision",
                "decision_id": _required(
                    plan.route_payload.get("trust_decision_id"),
                    "trust decision id",
                ),
                "revision_id": "",
                "revision_hash": "",
            },
            action_ref={"action_id": "", "effect_id": ""},
            access_control=access,
        )
        revision = CognitiveStateRevision.create(
            object_type="prediction_record",
            object_id=plan.prediction_id,
            source_event_id=event_id,
            source_revision_id=source_revision_id,
            source_content_hash=source_hash,
            scope_type=plan.scope_type,
            scope_id=plan.scope_id,
            evidence_refs=self._prediction_evidence(plan),
            payload=payload,
            created_at=plan.starts_at,
        )
        projection_effect_id = "prediction-delivery-effect-" + _digest(
            {
                "prediction_revision_id": revision.revision_id,
                "delivery_event_id": plan.delivery_event_id,
                "delivery_event_payload_hash": plan.delivery_event_payload_hash,
            }
        )[:32]
        command = LocalConsumerCommand.create(
            revision_id=revision.revision_id,
            consumer_id=PREDICTION_PROJECTION_CONSUMER,
            command_type=PREDICTION_PROJECTION_COMMAND,
            payload={
                "schema_version": "mnemos.prediction_delivery_projection.v1",
                "prediction_id": plan.prediction_id,
                "prediction_revision_id": revision.revision_id,
                "prediction_revision_hash": revision.payload_hash,
                "delivery_event_id": plan.delivery_event_id,
                "delivery_event_payload": dict(plan.route_payload),
                "delivery_event_payload_hash": plan.delivery_event_payload_hash,
                "projection_effect_id": projection_effect_id,
            },
            created_at=plan.starts_at,
        )
        event = CognitiveDataEvent(
            event_id=event_id,
            source_id=f"delivery-plan:{plan.prediction_id}",
            asset_id=plan.delivery_event_payload_hash,
            source_kind="prediction_plan",
            source_uri=f"mnemos://prediction-plan/{plan.prediction_id}",
            content_hash=source_hash,
            canonical_subject=f"prediction_record:{plan.prediction_id}",
            data_type="prediction_record",
            producer="prediction_record_store",
            intended_consumers=(PREDICTION_PROJECTION_CONSUMER,),
            privacy_level=str(access["sensitivity"]),
            confidence=0.0,
            evidence_refs=self._prediction_evidence(plan),
            dedupe_key=f"prediction-record:{plan.prediction_id}:open",
            created_at=plan.starts_at,
            retention_policy=str(access["retention_policy"]),
            metadata={
                "revision_ids": [revision.revision_id],
                "access_control_hash": cognitive_access_hash(access),
            },
        )
        committed = self.state_store.unit_of_work().commit(
            revisions=(revision,),
            event=event,
            commands=(command,),
            failpoint=_failpoint,
        )
        return PredictionSealReceipt(
            status=committed.status,
            event_id=event_id,
            prediction_id=plan.prediction_id,
            revision_id=revision.revision_id,
            revision_hash=revision.payload_hash,
            command_id=command.command_id,
            projection_effect_id=projection_effect_id,
            transaction_hash=committed.transaction_hash,
        )

    def finalize(
        self,
        prediction_id: str,
        evidence: Mapping[str, Any] | None,
        now: datetime | str | None = None,
    ) -> PredictionTerminalReceipt:
        """Append the one code-derived terminal state for a mature prediction."""

        self._require_enforcement()
        normalized_id = _prediction_id(prediction_id)
        current = self.state_store.current_revision("prediction_record", normalized_id)
        if current is None:
            raise ValueError("prediction does not exist")
        current_payload = dict(current.payload)
        validate_cognitive_state_payload("prediction_record", current_payload)
        if current_payload["revision_state"] == "terminal":
            open_revision = self._open_ancestor(current)
            proposed = self._derive_terminal(
                open_revision,
                evidence=dict(evidence or {}),
                evaluated_at=_timestamp(now or current_payload["terminal"]["evaluated_at"]),
            )
            if not _terminal_matches(current_payload, proposed):
                raise CognitiveStateConflict(
                    "prediction already has a different immutable terminal state"
                )
            self._ensure_terminal_projection_receipt(current.revision_id)
            return _terminal_receipt_from_revision(current, status="existing")
        evaluated_at = _timestamp(now or _now())
        terminal = self._derive_terminal(
            current,
            evidence=dict(evidence or {}),
            evaluated_at=evaluated_at,
        )
        return self._append_terminal(
            current,
            terminal=terminal,
            evaluated_at=evaluated_at,
        )

    def correct_terminal(
        self,
        prediction_id: str,
        correction: Mapping[str, Any],
        principal: PrincipalEnvelope | None,
    ) -> PredictionTerminalReceipt:
        """Append an authorized terminal correction; ordinary finalize is immutable."""

        self._require_enforcement()
        normalized_id = _prediction_id(prediction_id)
        current = self.state_store.current_revision("prediction_record", normalized_id)
        if current is None or current.payload["revision_state"] != "terminal":
            raise ValueError("terminal prediction does not exist")
        if not isinstance(correction, Mapping):
            raise TypeError("correction must be an object")
        if correction.get("correction_of_revision_id") != current.revision_id:
            raise ValueError("correction must reference the current terminal revision")
        outcome_revision_id = _required(
            correction.get("outcome_revision_id"),
            "corrected outcome revision id",
        )
        outcome = self.state_store.revision(outcome_revision_id)
        if outcome is None or outcome.object_type != "outcome_measurement":
            raise ValueError("corrected outcome revision is unavailable")
        if outcome.correction_of_revision_id != current.payload["outcome_ref"][
            "revision_id"
        ]:
            raise ValueError("outcome correction does not supersede the prior measurement")
        access = validate_cognitive_access_envelope(
            outcome.payload["access_control"],
            expected_scope_type=outcome.scope_type,
            expected_scope_id=outcome.scope_id,
        )
        decision = authorize_cognitive_write(
            access,
            principal=principal,
            scope_type=outcome.scope_type,
            scope_id=outcome.scope_id,
        )
        if not decision.allowed:
            raise PermissionError(f"outcome correction access denied: {decision.reason}")
        evaluated_at = _timestamp(correction.get("evaluated_at") or _now())
        terminal = self._terminal_from_outcome(
            self._open_ancestor(current),
            outcome,
            evaluated_at=evaluated_at,
        )
        return self._append_terminal(
            current,
            terminal=terminal,
            evaluated_at=evaluated_at,
            correction_of_revision_id=current.revision_id,
        )

    def reconcile_matured(
        self,
        now: datetime | str | None = None,
        limit: int = 100,
    ) -> MaturityBatchReceipt:
        """Close a bounded, deterministic batch of mature open predictions."""

        self._require_enforcement()
        evaluated_at = _timestamp(now or _now())
        selected = self._mature_open(evaluated_at, limit=max(0, int(limit)))
        counts = {state: 0 for state in PREDICTION_TERMINAL_STATES}
        existing = 0
        revision_ids: list[str] = []
        retryable_failed_prediction_ids: list[str] = []
        terminal_failed_prediction_ids: list[str] = []
        for revision in selected:
            try:
                receipt = self.finalize(revision.object_id, {}, evaluated_at)
            except (OSError, sqlite3.OperationalError):
                retryable_failed_prediction_ids.append(revision.object_id)
                continue
            except (
                sqlite3.Error,
                CognitiveStateConflict,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                try:
                    receipt = self._terminalize_maturity_failure(
                        revision,
                        exc=exc,
                        evaluated_at=evaluated_at,
                    )
                except (OSError, sqlite3.OperationalError, CognitiveStateConflict):
                    retryable_failed_prediction_ids.append(revision.object_id)
                    continue
                terminal_failed_prediction_ids.append(revision.object_id)
            counts[receipt.terminal_state] += 1
            existing += int(receipt.status == "existing")
            revision_ids.append(receipt.revision_id)
        remaining = len(self._mature_open(evaluated_at, limit=0))
        failed_prediction_ids = (
            *retryable_failed_prediction_ids,
            *terminal_failed_prediction_ids,
        )
        return MaturityBatchReceipt(
            selected=len(selected),
            measured=counts["measured"],
            unknown=counts["unknown"],
            censored=counts["censored"],
            confounded=counts["confounded"],
            existing=existing,
            failed=len(failed_prediction_ids),
            remaining_mature_open=remaining,
            revision_ids=tuple(revision_ids),
            failed_prediction_ids=tuple(failed_prediction_ids),
            retryable_failed=len(retryable_failed_prediction_ids),
            terminal_failed=len(terminal_failed_prediction_ids),
            retryable_failed_prediction_ids=tuple(retryable_failed_prediction_ids),
            terminal_failed_prediction_ids=tuple(terminal_failed_prediction_ids),
        )

    def _terminalize_maturity_failure(
        self,
        current: CognitiveStateRevision,
        *,
        exc: Exception,
        evaluated_at: datetime,
    ) -> PredictionTerminalReceipt:
        """Persist a permanent semantic failure instead of retrying it forever."""

        error_type = type(exc).__name__
        failure_hash = sha256_json(
            {
                "schema_version": "mnemos.prediction_maturity_failure.v1",
                "prediction_revision_id": current.revision_id,
                "error_type": error_type,
                "error_message": str(exc),
                "evaluated_at": evaluated_at.isoformat(),
            }
        )
        failure_ref = (
            f"maturity-failure:{error_type}:{failure_hash.split(':', 1)[1]}"
        )
        suppressed = current.payload["route_disposition"] == "suppress"
        return self._append_terminal(
            current,
            terminal={
                "state": "censored",
                "reason": f"maturity_permanent_failure:{error_type}:"
                f"{failure_hash.split(':', 1)[1]}",
                "outcome": None,
                "exposure_status": "not_exposed" if suppressed else "unproven",
                "exposure_refs": (),
                "attribution_method": "maturity_evaluation_failed",
                "competing_causes": (),
                "error": None,
                "calibration_exclusion": "maturity_evaluation_failure",
            },
            evaluated_at=evaluated_at,
            extra_evidence_refs=(failure_ref,),
        )

    def verify(
        self,
        prediction_revision_id: str,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None = None,
    ) -> PredictionVerification:
        """Validate one authorized revision and its exact outcome binding."""

        revision, reason = self.state_store.authorized_revision(
            prediction_revision_id,
            principal=principal,
            narrowing=narrowing,
            purpose="prediction_read",
        )
        if revision is None:
            raise PermissionError(f"prediction revision unavailable: {reason}")
        if revision.object_type != "prediction_record":
            raise ValueError("revision is not a PredictionRecord")
        payload = dict(revision.payload)
        validate_cognitive_state_payload("prediction_record", payload)
        outcome_id = str(payload["outcome_ref"]["revision_id"])
        if outcome_id:
            outcome = self.state_store.revision(outcome_id)
            if outcome is None or outcome.payload_hash != payload["outcome_ref"][
                "payload_hash"
            ]:
                raise RuntimeError("prediction outcome binding is unavailable")
            self._verify_outcome_binding(revision, outcome)
        verification_hash = sha256_json(
            {
                "revision_id": revision.revision_id,
                "payload_hash": revision.payload_hash,
                "prediction_input_hash": payload["prediction_input_hash"],
                "outcome_revision_id": outcome_id,
            }
        )
        return PredictionVerification(
            status="verified",
            prediction_id=revision.object_id,
            revision_id=revision.revision_id,
            terminal_state=str(payload["terminal"]["state"]),
            prediction_input_hash=str(payload["prediction_input_hash"]),
            outcome_revision_id=outcome_id,
            verification_hash=verification_hash,
        )

    def calibration_report(
        self,
        query: Mapping[str, Any] | None = None,
    ) -> PredictionCalibrationReport:
        return build_calibration_report(
            state_store=self.state_store,
            query=query,
            verify_outcome_binding=self._verify_outcome_binding,
            open_ancestor=self._open_ancestor,
            code_hash=PREDICTION_CODE_HASH,
            spec_hash=PREDICTION_SPEC_HASH,
            report_factory=PredictionCalibrationReport,
            terminal_states=PREDICTION_TERMINAL_STATES,
            metric_id=PREDICTION_METRIC_ID,
            method=PREDICTION_CONFIDENCE_METHOD,
            method_version=PREDICTION_CONFIDENCE_VERSION,
        )

    def _open_payload(
        self,
        plan: PredictionPlan,
        *,
        decision_ref: Mapping[str, Any],
        action_ref: Mapping[str, Any],
        access_control: Mapping[str, Any],
        scope_type: str = "",
        scope_id: str = "",
    ) -> dict[str, Any]:
        route = dict(plan.route_payload)
        source_without_hash = {
            key: route[key]
            for key in (
                "source",
                "subject",
                "channel",
                "target",
                "decision",
                "delivered_level",
                "reason",
                "trust_decision_id",
                "trust_score",
                "task_fit_score",
                "interruption_cost",
                "evidence_refs",
            )
        }
        source_snapshot = {
            **source_without_hash,
            "snapshot_hash": sha256_json(source_without_hash),
        }
        payload: dict[str, Any] = {
            "schema_version": COGNITIVE_OBJECT_SCHEMA_VERSIONS["prediction_record"],
            "prediction_id": plan.prediction_id,
            "revision_state": "open",
            "prediction_plan_hash": plan.prediction_plan_hash,
            "prediction_input_hash": "",
            "supersedes_revision_id": "",
            "correction_of_revision_id": "",
            "subject": {"type": "knowledge_topic", "id": route["subject"]},
            "scope": {
                "type": scope_type or plan.scope_type,
                "id": scope_id or plan.scope_id,
            },
            "source_snapshot": source_snapshot,
            "decision_ref": dict(decision_ref),
            "action_ref": dict(action_ref),
            "delivery_ref": {
                "event_id": plan.delivery_event_id,
                "event_payload_hash": plan.delivery_event_payload_hash,
            },
            "route_disposition": plan.route_disposition,
            "prediction_kind": PREDICTION_KIND,
            "metric": {
                "metric_id": PREDICTION_METRIC_ID,
                "unit": PREDICTION_UNIT,
                "predicted_value": plan.predicted_value,
                "baseline": "unknown",
                "measurement_spec": {
                    "schema_version": "mnemos.predictive_delivery_measurement.v1",
                    "allowed_values": ["not_useful", "useful"],
                    "requires_independent_evidence": True,
                },
            },
            "confidence": {
                "method": PREDICTION_CONFIDENCE_METHOD,
                "method_version": PREDICTION_CONFIDENCE_VERSION,
                "code_hash": PREDICTION_CODE_HASH,
                "spec_hash": PREDICTION_SPEC_HASH,
                "is_probability": False,
                "score_band": plan.score_band,
                "inputs": {
                    "trust_score": float(route["trust_score"]),
                    "task_fit_score": float(route["task_fit_score"]),
                    "interruption_cost": float(route["interruption_cost"]),
                },
            },
            "evaluation_window": {
                "starts_at": plan.starts_at,
                "ends_at": plan.ends_at,
                "timezone": "UTC",
                "maturity_policy": "close_at_window_end.v1",
                "config_hash": plan.window_config_hash,
            },
            "causal_assumptions": [
                "delivery route is the intervention being evaluated",
                "objective usefulness requires independent outcome evidence",
            ],
            "exposure": {
                "status": "not_exposed" if plan.route_disposition == "suppress" else "unproven",
                "evidence_refs": [],
            },
            "outcome_ref": {"revision_id": "", "payload_hash": ""},
            "attribution": {"method": "not_evaluated", "competing_causes": []},
            "terminal": {"state": "open", "reason": "", "evaluated_at": ""},
            "error": {"kind": "none", "value": None},
            "calibration": {"eligible": False, "exclusion_reason": "open"},
            "access_control": dict(access_control),
        }
        payload["prediction_input_hash"] = sha256_json(prediction_input_snapshot(payload))
        validate_cognitive_state_payload("prediction_record", payload)
        return payload

    def _derive_terminal(
        self,
        prediction: CognitiveStateRevision,
        *,
        evidence: Mapping[str, Any],
        evaluated_at: datetime,
    ) -> dict[str, Any]:
        outcomes = self._eligible_outcomes(prediction)
        if len(outcomes) > 1:
            raise CognitiveStateConflict("prediction has multiple eligible outcomes")
        if outcomes:
            return self._terminal_from_outcome(
                prediction,
                outcomes[0],
                evaluated_at=evaluated_at,
            )
        payload = prediction.payload
        window_end = _timestamp(payload["evaluation_window"]["ends_at"])
        if evaluated_at < window_end:
            raise ValueError("prediction has not reached maturity")
        requested_exposure_refs = tuple(
            sorted(
                str(value)
                for value in evidence.get("exposure_evidence_refs", ())
                if str(value)
            )
        )
        exposure_refs = self._reaction_exposure_refs(prediction)
        if requested_exposure_refs and requested_exposure_refs != exposure_refs:
            raise ValueError(
                "prediction exposure evidence must resolve to canonical reaction revisions"
            )
        if exposure_refs:
            return {
                "state": "unknown",
                "reason": "exposure_proven_without_eligible_outcome",
                "outcome": None,
                "exposure_status": "proven",
                "exposure_refs": exposure_refs,
                "attribution_method": "no_eligible_measurement",
                "competing_causes": (),
                "error": None,
                "calibration_exclusion": "unknown_outcome",
            }
        return {
            "state": "censored",
            "reason": (
                "policy_suppressed_without_exposure"
                if payload["route_disposition"] == "suppress"
                else "presentation_or_follow_up_unproven"
            ),
            "outcome": None,
            "exposure_status": (
                "not_exposed" if payload["route_disposition"] == "suppress" else "unproven"
            ),
            "exposure_refs": (),
            "attribution_method": "not_identifiable",
            "competing_causes": (),
            "error": None,
            "calibration_exclusion": "censored_observation",
        }

    def _terminal_from_outcome(
        self,
        prediction: CognitiveStateRevision,
        outcome: CognitiveStateRevision,
        *,
        evaluated_at: datetime,
    ) -> dict[str, Any]:
        self._verify_outcome_binding(prediction, outcome)
        matured_at = _timestamp(outcome.payload["maturity"]["matured_at"])
        if evaluated_at < matured_at:
            raise ValueError("outcome has not reached its declared maturity")
        causes = tuple(
            json.loads(canonical_json(value))
            for value in outcome.payload["attribution"]["competing_causes"]
        )
        predicted = str(prediction.payload["metric"]["predicted_value"])
        observed = str(outcome.payload["observed_value"])
        state = "confounded" if causes else "measured"
        return {
            "state": state,
            "reason": "eligible_outcome_confounded" if causes else "eligible_outcome_measured",
            "outcome": outcome,
            "exposure_status": "proven",
            "exposure_refs": tuple(outcome.payload["raw_evidence"]["refs"]),
            "attribution_method": str(outcome.payload["attribution"]["method"]),
            "competing_causes": causes,
            "error": 0 if predicted == observed else 1,
            "calibration_exclusion": "confounded_attribution" if causes else "",
        }

    def _append_terminal(
        self,
        current: CognitiveStateRevision,
        *,
        terminal: Mapping[str, Any],
        evaluated_at: datetime,
        correction_of_revision_id: str = "",
        extra_evidence_refs: tuple[str, ...] = (),
    ) -> PredictionTerminalReceipt:
        payload = json.loads(canonical_json(current.payload))
        outcome = terminal.get("outcome")
        payload.update(
            {
                "revision_state": "terminal",
                "supersedes_revision_id": current.revision_id,
                "correction_of_revision_id": correction_of_revision_id,
                "exposure": {
                    "status": terminal["exposure_status"],
                    "evidence_refs": list(terminal["exposure_refs"]),
                },
                "outcome_ref": {
                    "revision_id": outcome.revision_id if outcome is not None else "",
                    "payload_hash": outcome.payload_hash if outcome is not None else "",
                },
                "attribution": {
                    "method": terminal["attribution_method"],
                    "competing_causes": list(terminal["competing_causes"]),
                },
                "terminal": {
                    "state": terminal["state"],
                    "reason": terminal["reason"],
                    "evaluated_at": evaluated_at.isoformat(),
                },
                "error": {
                    "kind": "categorical_miss" if terminal["error"] is not None else "none",
                    "value": terminal["error"],
                },
                "calibration": {
                    "eligible": terminal["state"] == "measured",
                    "exclusion_reason": terminal["calibration_exclusion"],
                },
            }
        )
        # Immutable input must remain byte-identical across lifecycle revisions.
        if payload["prediction_input_hash"] != current.payload["prediction_input_hash"]:
            raise RuntimeError("prediction immutable input drifted")
        validate_cognitive_state_payload("prediction_record", payload)
        source_hash = sha256_json(
            {
                "prediction_revision_id": current.revision_id,
                "terminal": payload["terminal"],
                "outcome_ref": payload["outcome_ref"],
            }
        )
        event_id = "cogevent-" + _digest(
            {
                "operation": "finalize_prediction",
                "prediction_id": current.object_id,
                "current_revision_id": current.revision_id,
                "source_hash": source_hash,
            }
        )[:32]
        existing = self._terminal_for_event(event_id)
        if existing is not None:
            self._ensure_terminal_projection_receipt(existing.revision_id)
            return existing
        evidence_refs = tuple(
            sorted(
                {
                    f"prediction-revision:{current.revision_id}",
                    *extra_evidence_refs,
                    *payload["exposure"]["evidence_refs"],
                    *(
                        (f"outcome-revision:{outcome.revision_id}",)
                        if outcome is not None
                        else ()
                    ),
                }
            )
        )
        revision = CognitiveStateRevision.create(
            object_type="prediction_record",
            object_id=current.object_id,
            source_event_id=event_id,
            source_revision_id=f"prediction-terminal:{current.revision_id}",
            source_content_hash=source_hash,
            scope_type=current.scope_type,
            scope_id=current.scope_id,
            evidence_refs=evidence_refs,
            payload=payload,
            supersedes_revision_id=current.revision_id,
            correction_of_revision_id=correction_of_revision_id,
            created_at=evaluated_at.isoformat(),
        )
        terminal_effect_id = "prediction-terminal-effect-" + _digest(
            {
                "prediction_id": current.object_id,
                "terminal_revision_id": revision.revision_id,
                "terminal_revision_hash": revision.payload_hash,
            }
        )[:32]
        command = LocalConsumerCommand.create(
            revision_id=revision.revision_id,
            consumer_id=PREDICTION_TERMINAL_CONSUMER,
            command_type=PREDICTION_TERMINAL_COMMAND,
            payload={
                "schema_version": "mnemos.prediction_terminal_projection.v1",
                "prediction_id": current.object_id,
                "terminal_revision_id": revision.revision_id,
                "terminal_revision_hash": revision.payload_hash,
                "terminal_state": terminal["state"],
                "projection_effect_id": terminal_effect_id,
            },
            created_at=evaluated_at.isoformat(),
        )
        event = CognitiveDataEvent(
            event_id=event_id,
            source_id=f"prediction-terminal:{current.object_id}",
            asset_id=source_hash,
            source_kind="prediction_maturity",
            source_uri=f"mnemos://prediction/{current.object_id}/terminal",
            content_hash=source_hash,
            canonical_subject=f"prediction_record:{current.object_id}",
            data_type="prediction_record",
            producer="prediction_record_store",
            intended_consumers=(PREDICTION_TERMINAL_CONSUMER,),
            privacy_level=str(payload["access_control"]["sensitivity"]),
            confidence=0.0,
            evidence_refs=evidence_refs,
            dedupe_key=f"prediction-record:{current.object_id}:terminal:{current.revision_id}",
            created_at=evaluated_at.isoformat(),
            retention_policy=str(payload["access_control"]["retention_policy"]),
            metadata={
                "revision_ids": [revision.revision_id],
                "access_control_hash": cognitive_access_hash(payload["access_control"]),
            },
        )
        committed = self.state_store.unit_of_work().commit(
            revisions=(revision,),
            event=event,
            commands=(command,),
            expected_heads=(
                CognitiveHeadPrecondition.create(
                    object_type="prediction_record",
                    object_id=current.object_id,
                    revision_id=current.revision_id,
                ),
            ),
        )
        self._ensure_terminal_projection_receipt(revision.revision_id)
        return PredictionTerminalReceipt(
            status=committed.status,
            event_id=event_id,
            prediction_id=current.object_id,
            revision_id=revision.revision_id,
            terminal_state=str(terminal["state"]),
            outcome_revision_id=outcome.revision_id if outcome is not None else "",
            transaction_hash=committed.transaction_hash,
        )

    def _ensure_terminal_projection_receipt(self, revision_id: str) -> None:
        with self.state_store._connect(read_only=True) as conn:  # noqa: SLF001
            row = conn.execute(
                """
                SELECT o.command_id, o.payload_json, r.command_id AS receipt_command_id
                FROM cognitive_state_outbox AS o
                LEFT JOIN cognitive_state_effect_receipts AS r
                  ON r.command_id=o.command_id
                WHERE o.revision_id=? AND o.command_type=?
                """,
                (revision_id, PREDICTION_TERMINAL_COMMAND),
            ).fetchone()
        if row is None or row["receipt_command_id"] is not None:
            return
        payload = json.loads(str(row["payload_json"]))
        before_hash = sha256_json(
            {"prediction_id": payload["prediction_id"], "state": "unprojected"}
        )
        after_hash = sha256_json(
            {
                "terminal_revision_id": payload["terminal_revision_id"],
                "terminal_revision_hash": payload["terminal_revision_hash"],
                "terminal_state": payload["terminal_state"],
            }
        )
        self.state_store.record_effect_receipt(
            str(row["command_id"]),
            status="committed",
            target_effect_id=str(payload["projection_effect_id"]),
            before_hash=before_hash,
            after_hash=after_hash,
            evidence_refs=(
                f"prediction-terminal-command:{row['command_id']}",
                f"prediction-revision:{revision_id}",
                f"prediction-terminal-projection:{after_hash}",
            ),
            outcome="deterministic prediction terminal read model available",
        )

    def _eligible_outcomes(
        self,
        prediction: CognitiveStateRevision,
    ) -> tuple[CognitiveStateRevision, ...]:
        rows = self.state_store.current_revisions(object_type="outcome_measurement")
        eligible: list[CognitiveStateRevision] = []
        for outcome in rows:
            try:
                self._verify_outcome_binding(prediction, outcome)
            except (RuntimeError, TypeError, ValueError):
                continue
            eligible.append(outcome)
        return tuple(sorted(eligible, key=lambda value: value.revision_id))

    def verify_outcome_revision(
        self,
        outcome_revision_id: str,
    ) -> CognitiveStateRevision:
        """Revalidate one committed OutcomeMeasurement and its oracle receipt."""

        outcome = self.state_store.revision(str(outcome_revision_id or ""))
        if outcome is None or outcome.object_type != "outcome_measurement":
            raise ValueError("outcome measurement revision is unavailable")
        prediction_ref = outcome.payload.get("prediction_ref")
        if not isinstance(prediction_ref, Mapping):
            raise ValueError("outcome prediction ref is unavailable")
        prediction = self.state_store.revision(str(prediction_ref.get("revision_id") or ""))
        if prediction is None or prediction.object_type != "prediction_record":
            raise ValueError("outcome prediction revision is unavailable")
        self._verify_outcome_binding(prediction, outcome)
        return outcome

    def _verify_outcome_binding(
        self,
        prediction: CognitiveStateRevision,
        outcome: CognitiveStateRevision,
    ) -> None:
        validate_cognitive_state_payload("outcome_measurement", outcome.payload)
        prediction_payload = prediction.payload
        outcome_payload = outcome.payload
        prediction_ref = outcome_payload["prediction_ref"]
        if (
            prediction_ref["prediction_id"] != prediction.object_id
            or prediction_ref["revision_id"]
            not in {prediction.revision_id, prediction.supersedes_revision_id}
            or prediction_ref["prediction_input_hash"]
            != prediction_payload["prediction_input_hash"]
            or outcome_payload["decision_ref"] != prediction_payload["decision_ref"]
            or outcome_payload["action_ref"] != prediction_payload["action_ref"]
            or outcome_payload["delivery_ref"] != prediction_payload["delivery_ref"]
            or outcome_payload["subject"] != prediction_payload["subject"]
            or outcome_payload["metric"]["metric_id"]
            != prediction_payload["metric"]["metric_id"]
            or outcome_payload["metric"]["unit"]
            != prediction_payload["metric"]["unit"]
            or outcome_payload["baseline"] != prediction_payload["metric"]["baseline"]
            or outcome_payload["access_control"]["scope"]
            != prediction_payload["access_control"]["scope"]
            or outcome_payload["access_control"]["owner"]
            != prediction_payload["access_control"]["owner"]
        ):
            raise RuntimeError("outcome does not bind the exact prediction")
        observation = outcome_payload["observation_window"]
        window = prediction_payload["evaluation_window"]
        if (
            _timestamp(observation["starts_at"]) < _timestamp(window["starts_at"])
            or _timestamp(observation["ends_at"]) > _timestamp(window["ends_at"])
            or _timestamp(outcome_payload["maturity"]["matured_at"])
            < _timestamp(observation["ends_at"])
        ):
            raise RuntimeError("outcome observation is outside the prediction window")
        if outcome_payload["source_authority"]["authority"] in {
            "assistant_inference",
            "external_content",
            "quoted_content",
        }:
            raise RuntimeError("outcome source authority is not independently eligible")
        oracle_prediction = prediction
        referenced_prediction_id = str(prediction_ref["revision_id"])
        if referenced_prediction_id != prediction.revision_id:
            referenced_prediction = self.state_store.revision(referenced_prediction_id)
            if (
                referenced_prediction is None
                or referenced_prediction.object_type != "prediction_record"
                or referenced_prediction.object_id != prediction.object_id
            ):
                raise RuntimeError("outcome prediction source revision is unavailable")
            oracle_prediction = referenced_prediction
        issuance = reissue_objective_measurement(
            state_store=self.state_store,
            prediction=oracle_prediction,
            outcome=outcome,
        )
        if not outcome_issuance_receipt_valid(
            state_store=self.state_store,
            outcome=outcome,
            issuance_hash=issuance.issuance_hash,
            command_type=PREDICTION_OUTCOME_COMMAND,
            consumer_id=PREDICTION_OUTCOME_CONSUMER,
        ):
            raise RuntimeError("outcome objective issuance receipt is missing or invalid")

    def _reaction_exposure_refs(
        self,
        prediction: CognitiveStateRevision,
    ) -> tuple[str, ...]:
        refs: list[str] = []
        for reaction in self.state_store.current_revisions(object_type="user_reaction_event"):
            delivery_ref = reaction.payload.get("delivery_ref")
            event_id = (
                str(delivery_ref.get("event_id") or "")
                if isinstance(delivery_ref, Mapping)
                else str(delivery_ref or "")
            )
            if event_id == prediction.payload["delivery_ref"]["event_id"]:
                refs.append(f"reaction-exposure:{reaction.revision_id}")
        return tuple(sorted(set(refs)))

    def _mature_open(
        self,
        now: datetime,
        *,
        limit: int,
    ) -> tuple[CognitiveStateRevision, ...]:
        rows = [
            revision
            for revision in self.state_store.current_revisions(
                object_type="prediction_record"
            )
            if revision.payload["revision_state"] == "open"
            and _timestamp(revision.payload["evaluation_window"]["ends_at"]) <= now
        ]
        rows.sort(
            key=lambda value: (
                str(value.payload["evaluation_window"]["ends_at"]),
                value.object_id,
            )
        )
        return tuple(rows if limit <= 0 else rows[:limit])

    def _seal_receipt(self, prediction_id: str) -> PredictionSealReceipt | None:
        if not self.state_store.db_path.is_file():
            return None
        current = self.state_store.current_revision("prediction_record", prediction_id)
        if current is None:
            return None
        with self.state_store._connect(read_only=True) as conn:  # noqa: SLF001
            row = conn.execute(
                """
                SELECT command_id, payload_json FROM cognitive_state_outbox
                WHERE revision_id=? AND command_type=?
                """,
                (current.revision_id, PREDICTION_PROJECTION_COMMAND),
            ).fetchone()
        if row is None:
            return None
        command_payload = json.loads(str(row["payload_json"]))
        return PredictionSealReceipt(
            status="existing",
            event_id=current.source_event_id,
            prediction_id=current.object_id,
            revision_id=current.revision_id,
            revision_hash=current.payload_hash,
            command_id=str(row["command_id"]),
            projection_effect_id=str(command_payload["projection_effect_id"]),
            transaction_hash="",
        )

    def _terminal_for_event(self, event_id: str) -> PredictionTerminalReceipt | None:
        if not self.state_store.db_path.is_file():
            return None
        with self.state_store._connect(read_only=True) as conn:  # noqa: SLF001
            row = conn.execute(
                """
                SELECT revision_id FROM cognitive_state_revisions
                WHERE object_type='prediction_record' AND source_event_id=?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        revision = self.state_store.revision(str(row["revision_id"]))
        if revision is None:
            raise RuntimeError("prediction terminal replay lacks its revision")
        return _terminal_receipt_from_revision(revision, status="existing")

    def _open_ancestor(
        self,
        revision: CognitiveStateRevision,
    ) -> CognitiveStateRevision:
        current = revision
        seen: set[str] = set()
        while current.payload["revision_state"] != "open":
            if current.revision_id in seen:
                raise RuntimeError("prediction revision lineage contains a cycle")
            seen.add(current.revision_id)
            previous_id = str(current.supersedes_revision_id or "")
            previous = self.state_store.revision(previous_id)
            if previous is None or previous.object_id != revision.object_id:
                raise RuntimeError("prediction terminal lacks its open ancestor")
            current = previous
        return current

    def _require_plan(self, plan: PredictionPlan) -> None:
        if not isinstance(plan, PredictionPlan):
            raise TypeError("typed PredictionPlan is required")
        source_access_control = plan.route_payload.get("source_access_control")
        if not isinstance(source_access_control, Mapping):
            raise ValueError("prediction route source access control is invalid")
        route_source_access = validate_cognitive_access_envelope(
            source_access_control
        )
        expected_access = _prediction_access_control(
            source_access_control=route_source_access,
        )
        supplied_access = validate_cognitive_access_envelope(
            plan.access_control,
            expected_scope_type=plan.scope_type,
            expected_scope_id=plan.scope_id,
        )
        expected_access_hash = cognitive_access_hash(expected_access)
        if (
            supplied_access != expected_access
            or plan.access_control_hash != expected_access_hash
        ):
            raise ValueError("PredictionPlan access control drift")
        expected_plan = _canonical_prediction_plan(plan.route_payload, self.config)
        if plan != expected_plan:
            raise ValueError("PredictionPlan canonical semantics drift")
        if sha256_json(
            {
                "schema_version": "mnemos.prediction_plan.v1",
                "delivery_event_id": plan.delivery_event_id,
                "delivery_event_payload_hash": plan.delivery_event_payload_hash,
                "route_disposition": plan.route_disposition,
                "predicted_value": plan.predicted_value,
                "score_band": plan.score_band,
                "starts_at": plan.starts_at,
                "ends_at": plan.ends_at,
                "window_config_hash": plan.window_config_hash,
                "scope_type": plan.scope_type,
                "scope_id": plan.scope_id,
                "access_control_hash": plan.access_control_hash,
                "prediction_id": plan.prediction_id,
            }
        ) != plan.prediction_plan_hash:
            raise ValueError("PredictionPlan hash mismatch")
        if sha256_json(dict(plan.route_payload)) != plan.delivery_event_payload_hash:
            raise ValueError("PredictionPlan delivery payload hash mismatch")

    def _require_enforcement(self) -> None:
        initialize_cognitive_state_schema(self.state_store.db_path)
        with self.state_store._connect(read_only=True) as conn:  # noqa: SLF001
            if not prediction_enforcement_enabled(conn):
                raise RuntimeError(
                    "prediction migration required; run "
                    "scripts/reconcile_prediction_history.py before predictive delivery"
                )

    @staticmethod
    def _prediction_evidence(plan: PredictionPlan) -> tuple[str, ...]:
        route_refs = tuple(str(value) for value in plan.route_payload["evidence_refs"])
        return tuple(
            sorted(
                {
                    f"delivery-event:{plan.delivery_event_id}",
                    f"delivery-plan:{plan.prediction_plan_hash}",
                    f"trust-decision:{plan.route_payload['trust_decision_id']}",
                    *route_refs,
                }
            )
        )


def _terminal_receipt_from_revision(
    revision: CognitiveStateRevision,
    *,
    status: str,
) -> PredictionTerminalReceipt:
    return PredictionTerminalReceipt(
        status=status,
        event_id=revision.source_event_id,
        prediction_id=revision.object_id,
        revision_id=revision.revision_id,
        terminal_state=str(revision.payload["terminal"]["state"]),
        outcome_revision_id=str(revision.payload["outcome_ref"]["revision_id"]),
        transaction_hash="",
    )
