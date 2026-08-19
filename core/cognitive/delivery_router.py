# -*- coding: utf-8 -*-
"""Delivery routing, budgets, and auditable delivery/outcome ledgers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from core.access_policy import PrincipalEnvelope
from core.cognitive.access_control import (
    authorize_cognitive_write,
    cognitive_access_hash,
    validate_cognitive_access_envelope,
)
from core.config import get_config
from core.cognitive.decision_trace import (
    MaterialActionCoordinator,
    MaterialActionAuthorization,
    MaterialActionObservation,
    MaterialActionPermit,
    MaterialActionRequest,
    MaterialActionTerminal,
    authorize_exact_project_contract_action,
    require_material_action,
    resolve_material_action_recovery_authorization,
)
from core.cognitive.state_contract import sha256_json
from core.cognitive.prediction_ledger import (
    PredictionPlan,
    PredictionRecordStore,
)
from core.cognitive.trust_scorer import (
    KnowledgeTrustOptions,
    KnowledgeTrustScorer,
    TrustDecision,
)
from core.db_utils import validate_sql_identifier
from core.cognitive.delivery_router_support import (
    _cfg_get,
    _json_dumps,
    _norm,
    _now,
    _row_to_dict,
    _to_float,
    _to_int,
)


SCHEMA_VERSION = "mnemos.delivery_events.v1"
DELIVERY_PRESENTATION_RECEIPT_SCHEMA_VERSION = "mnemos.delivery_presentation_receipt.v1"
DELIVERY_LEVELS = ("silent", "hint", "warn", "force_open")
DELIVERY_EVENT_KEY_COLUMNS = frozenset({"task_key", "cooldown_key"})
DELIVERY_MATERIAL_ACTION_TYPE = "outward_delivery"
DELIVERY_MATERIAL_OWNER = "knowledge_delivery"
DELIVERY_MATERIAL_EXECUTOR = "knowledge_delivery_router"
DELIVERY_DECISION_CONTRACT_ID = "project-contract:knowledge-delivery-routing"
DELIVERY_DECISION_CONTRACT_REVISION = "mnemos.knowledge_delivery_routing.v1"
DELIVERY_DECISION_CONTRACT_TEXT = (
    "The delivery router may emit only an exact outward delivery selected by "
    "the current trust gate, interruption budget, cooldown, and risk policy."
)
DELIVERY_DECISION_PRODUCER_HASH = sha256_json(
    {
        "module": "core.cognitive.delivery_router",
        "producer": "KnowledgeDeliveryRouter.route_candidate",
        "version": DELIVERY_DECISION_CONTRACT_REVISION,
    }
)
DELIVERY_NONMATERIAL_PROOF_KEY = "_nonmaterial_suppression"
DELIVERY_NONMATERIAL_SCHEMA_VERSION = "mnemos.delivery_nonmaterial_suppression.v1"
DELIVERY_PREDICTION_METADATA_KEY = "prediction_record"
_DELIVERY_RESERVED_METADATA_KEYS = frozenset(
    {
        "material_action",
        DELIVERY_NONMATERIAL_PROOF_KEY,
        DELIVERY_PREDICTION_METADATA_KEY,
        "delivery_principal",
    }
)


def resolve_delivery_db_path(
    *,
    config: Any,
    database_dir: Path,
    explicit: Path | None = None,
) -> Path:
    """Resolve the canonical delivery-event database path."""

    configured = _cfg_get(config, "delivery.db_path", None)
    return Path(explicit or configured or Path(database_dir) / "delivery_events.db").expanduser()


class DeliveryEventEffectOracle:
    """Read-only recovery oracle for one append-only outward-delivery event."""

    owner = DELIVERY_MATERIAL_OWNER
    executor_id = DELIVERY_MATERIAL_EXECUTOR
    action_type = DELIVERY_MATERIAL_ACTION_TYPE

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def observe(
        self,
        permit: MaterialActionPermit,
    ) -> MaterialActionObservation | None:
        """Return the exact committed delivery effect for one permit."""

        if not self.db_path.is_file():
            return None
        with sqlite3.connect(
            f"file:{self.db_path.resolve(strict=True)}?mode=ro",
            uri=True,
        ) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM delivery_events
                WHERE json_extract(
                    metadata_json,
                    '$.material_action.command_id'
                )=?
                """,
                (permit.command_id,),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise RuntimeError(
                "material command maps to multiple delivery events"
            )
        row = _row_to_dict(rows[0])
        material = dict(row.get("metadata", {})).get("material_action")
        expected = _delivery_material_metadata(permit)
        if (
            row.get("decision") != "deliver"
            or not isinstance(material, Mapping)
            or dict(material) != expected
        ):
            raise RuntimeError(
                "existing delivery event does not match its pending material command"
            )
        after_hash = delivery_effect_hash(row)
        event_id = str(row["event_id"])
        return MaterialActionObservation(
            status="committed",
            before_hash=sha256_json(
                {"event_id": event_id, "state": "absent"}
            ),
            after_hash=after_hash,
            evidence_refs=(
                f"delivery-event:{event_id}",
                f"delivery-event-hash:{after_hash}",
                f"target-after:{after_hash}",
                f"target-oracle:delivery-event:{event_id}:{after_hash}",
                "transport-status:routed-not-presented",
            ),
            outcome="observed append-only delivery event after restart",
            observed_at=str(row["created_at"]),
        )


def delivery_material_action_binding(
    *,
    source: str,
    subject: str,
    channel: str,
    target: str = "",
    evidence_refs: list[str] | None = None,
    task_fit_score: float = 0.5,
    requested_level: str = "hint",
    task_key: str = "",
    cooldown_key: str = "",
    scope_type: str = "",
    scope_value: str = "",
    active_risk: bool = False,
    metadata: Mapping[str, Any] | None = None,
    source_access_control: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Return the exact target and input hash a delivery permit must bind."""

    normalized_subject = _norm(subject)
    normalized_channel = str(channel or "delivery")
    normalized_target = str(target or "")
    effective_cooldown = _norm(cooldown_key or normalized_subject)
    effective_task = _norm(task_key or normalized_channel or "global")
    effective_scope_type = _norm(scope_type) or (
        "topic" if normalized_channel == "predictive_push" else "delivery"
    )
    effective_scope_value = _norm(scope_value) or effective_cooldown
    target_ref = (
        f"delivery:{normalized_channel}:"
        f"{normalized_target or normalized_subject}"
    )
    payload = {
        "schema_version": "mnemos.delivery_material_input.v1",
        "source": str(source or ""),
        "subject": normalized_subject,
        "channel": normalized_channel,
        "target": normalized_target,
        "evidence_refs": sorted(str(ref) for ref in (evidence_refs or []) if str(ref)),
        "task_fit_score": float(task_fit_score),
        "requested_level": _norm_level(requested_level),
        "task_key": effective_task,
        "cooldown_key": effective_cooldown,
        "scope_type": effective_scope_type,
        "scope_value": effective_scope_value,
        "active_risk": bool(active_risk),
        "metadata": dict(metadata or {}),
    }
    if normalized_channel == "predictive_push":
        if not isinstance(source_access_control, Mapping):
            raise ValueError("predictive_push requires an exact source access_control")
        payload["source_access_control_hash"] = cognitive_access_hash(
            source_access_control
        )
    elif source_access_control is not None:
        raise ValueError("source access_control is only valid for predictive_push")
    return {"target_ref": target_ref, "input_hash": sha256_json(payload)}


@dataclass(frozen=True)
class DeliveryBudgetPolicy:
    """Config-backed delivery budget profile."""

    preference: str = "balanced"
    daily_total: int = 12
    per_task_total: int = 3
    per_task_hint: int = 2
    per_task_warn: int = 1
    force_open_daily: int = 0
    same_topic_cooldown_hours: int = 24
    dismiss_cooldown_days: int = 14
    overflow_defer_hours: int = 1

    @classmethod
    def from_config(cls, cfg: Any | None = None) -> "DeliveryBudgetPolicy":
        cfg = cfg or get_config()
        preference = str(_cfg_get(cfg, "delivery.preference", "balanced") or "balanced")
        profile_prefix = f"delivery.profiles.{preference}"
        fallback_prefix = "delivery.profiles.balanced"
        if _cfg_get(cfg, profile_prefix, None) is None and preference != "balanced":
            preference = "balanced"
            profile_prefix = fallback_prefix

        def value(name: str, default: Any) -> Any:
            configured = _cfg_get(cfg, f"{profile_prefix}.{name}", None)
            if configured is None:
                configured = _cfg_get(cfg, f"{fallback_prefix}.{name}", default)
            return configured

        from core.kia.policy import get_shadowed_value

        per_task_default = get_shadowed_value(
            "app.push_max_items",
            _cfg_get(cfg, "app.push_max_items", 3),
        )
        return cls(
            preference=preference,
            daily_total=_to_int(value("daily_total", 12), 12),
            per_task_total=_to_int(value("per_task_total", per_task_default), 3),
            per_task_hint=_to_int(value("per_task_hint", 2), 2),
            per_task_warn=_to_int(value("per_task_warn", 1), 1),
            force_open_daily=_to_int(value("force_open_daily", 0), 0),
            same_topic_cooldown_hours=_to_int(value("same_topic_cooldown_hours", 24), 24),
            dismiss_cooldown_days=_to_int(value("dismiss_cooldown_days", 14), 14),
            overflow_defer_hours=_to_int(value("overflow_defer_hours", 1), 1),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DeliveryDecision:
    """One delivery routing decision."""

    event_id: str
    source: str
    subject: str
    channel: str
    target: str
    decision: str
    reason: str
    requested_level: str
    delivered_level: str
    profile: str
    cooldown_key: str
    trust_decision_id: str = ""
    trust_score: float = 0.0
    task_fit_score: float = 0.0
    interruption_cost: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class KnowledgeDeliveryRouter:
    """Route knowledge delivery through trust, budget, cooldown, and ledgers."""

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        database_dir: Path | None = None,
        config: Any | None = None,
        trust_scorer: KnowledgeTrustScorer | None = None,
        policy: DeliveryBudgetPolicy | None = None,
        ensure_db: bool = True,
    ):
        cfg = config or get_config()
        self.config = cfg
        base_dir = Path(database_dir or getattr(cfg, "database_dir", "") or Path.home() / ".mnemos")
        self.database_dir = base_dir.expanduser()
        self.db_path = resolve_delivery_db_path(
            config=cfg,
            database_dir=base_dir,
            explicit=db_path,
        )
        self.policy = policy or DeliveryBudgetPolicy.from_config(cfg)
        self.trust_scorer = trust_scorer or KnowledgeTrustScorer(
            options=KnowledgeTrustOptions.from_config(cfg, database_dir=base_dir),
            ensure_db=ensure_db,
        )
        if ensure_db:
            self._ensure_schema()

    def route_candidate(
        self,
        *,
        source: str,
        subject: str,
        channel: str,
        target: str = "",
        evidence_refs: list[str] | None = None,
        task_fit_score: float = 0.5,
        requested_level: str = "hint",
        task_key: str = "",
        cooldown_key: str = "",
        scope_type: str = "",
        scope_value: str = "",
        active_risk: bool = False,
        metadata: Mapping[str, Any] | None = None,
        material_action: MaterialActionAuthorization | None = None,
        source_access_control: Mapping[str, Any] | None = None,
        principal: PrincipalEnvelope | None = None,
    ) -> DeliveryDecision:
        predictive_source_access: dict[str, Any] | None = None
        if str(channel or "delivery") == "predictive_push":
            if not isinstance(source_access_control, Mapping):
                raise ValueError(
                    "predictive_push requires a server-resolved source access_control"
                )
            expected_scope_type = _norm(scope_type) or "topic"
            expected_scope_id = _norm(scope_value) or _norm(subject)
            predictive_source_access = validate_cognitive_access_envelope(
                source_access_control,
                expected_scope_type=expected_scope_type,
                expected_scope_id=expected_scope_id,
            )
            source_scope = predictive_source_access["scope"]
            write = authorize_cognitive_write(
                predictive_source_access,
                principal=principal,
                scope_type=str(source_scope["scope_type"]),
                scope_id=str(source_scope["scope_id"]),
            )
            if not write.allowed:
                raise PermissionError(
                    f"predictive source access denied: {write.reason}"
                )
        elif source_access_control is not None:
            raise ValueError(
                "source access authorization is only valid for predictive_push"
            )
        supplied_metadata = dict(metadata or {})
        reserved = sorted(_DELIVERY_RESERVED_METADATA_KEYS.intersection(supplied_metadata))
        if reserved:
            raise ValueError(
                "delivery metadata contains system-owned keys: " + ",".join(reserved)
            )
        supplied_binding = delivery_material_action_binding(
            source=source,
            subject=subject,
            channel=channel,
            target=target,
            evidence_refs=evidence_refs,
            task_fit_score=task_fit_score,
            requested_level=requested_level,
            task_key=task_key,
            cooldown_key=cooldown_key,
            scope_type=scope_type,
            scope_value=scope_value,
            active_risk=active_risk,
            metadata=supplied_metadata,
            source_access_control=predictive_source_access,
        )
        recovered_prediction = self._recover_pending_prediction_event(
            supplied_binding
        )
        if recovered_prediction is not None:
            return recovered_prediction
        if isinstance(material_action, MaterialActionAuthorization):
            material_action, _ = resolve_material_action_recovery_authorization(
                material_action,
                owner=DELIVERY_MATERIAL_OWNER,
                executor_id=DELIVERY_MATERIAL_EXECUTOR,
                action_type=DELIVERY_MATERIAL_ACTION_TYPE,
                target_ref=supplied_binding["target_ref"],
                input_hash=supplied_binding["input_hash"],
                expected_state_db=(
                    self.database_dir / "producer_consumer_ledger.db"
                ),
            )
            replay = self._recover_delivery_event(material_action)
            if replay is not None:
                return replay
        level = _norm_level(requested_level)
        subject_key = _norm(subject)
        cooldown = _norm(cooldown_key or subject_key)
        task = _norm(task_key or channel or "global")
        trust_scope_type = _norm(scope_type) or (
            "topic" if channel == "predictive_push" else "delivery"
        )
        trust_scope_value = _norm(scope_value) or cooldown
        trust_decision = self.trust_scorer.decide(
            source=source,
            subject=subject_key,
            action=channel or "delivery",
            evidence_refs=evidence_refs or [],
            task_fit_score=task_fit_score,
            interruption_cost=_interruption_cost(level),
            active_risk=active_risk,
            scope_type=trust_scope_type,
            scope_value=trust_scope_value,
            metadata=dict(metadata or {}),
        )
        decision, reason, delivered_level = self._budget_decision(
            level=level,
            trust_decision=trust_decision,
            cooldown_key=cooldown,
            task_key=task,
            active_risk=active_risk,
        )
        event = DeliveryDecision(
            event_id=_event_id(source, subject_key, channel),
            source=str(source or ""),
            subject=subject_key,
            channel=str(channel or "delivery"),
            target=str(target or ""),
            decision=decision,
            reason=reason,
            requested_level=level,
            delivered_level=delivered_level,
            profile=self.policy.preference,
            cooldown_key=cooldown,
            trust_decision_id=trust_decision.decision_id,
            trust_score=trust_decision.trust_score,
            task_fit_score=trust_decision.task_fit_score,
            interruption_cost=trust_decision.interruption_cost,
            metadata={
                **supplied_metadata,
                "task_key": task,
                "trust_decision": trust_decision.to_dict(),
                **(
                    {
                        "delivery_principal": {
                            "principal_id": principal.principal_id,
                            "agent": principal.agent,
                            "capability_id": principal.capability_id,
                            "access_control_hash": (
                                cognitive_access_hash(predictive_source_access)
                                if predictive_source_access is not None
                                else ""
                            ),
                        }
                    }
                    if principal is not None
                    else {}
                ),
            },
        )
        prediction_plan: PredictionPlan | None = None
        if event.channel == "predictive_push":
            assert predictive_source_access is not None
            prediction_plan = PredictionRecordStore(
                self.database_dir / "producer_consumer_ledger.db",
                config=self.config,
            ).prepare_route_prediction(
                _prediction_route_facts(
                    event,
                    evidence_refs=evidence_refs or [],
                    scope_type=str(
                        predictive_source_access["scope"]["scope_type"]
                    ),
                    scope_id=str(
                        predictive_source_access["scope"]["scope_id"]
                    ),
                    created_at=_now(),
                    request_binding=supplied_binding,
                    source_access_control=predictive_source_access,
                )
            )
        resolved_delivery = self._delivery_material_barrier(
            event,
            material_action=material_action,
            binding=supplied_binding,
            prediction_plan=prediction_plan,
        )
        authorization: MaterialActionAuthorization | None = None
        if resolved_delivery is None:
            if prediction_plan is None:
                self._log_nonmaterial_event(event)
                return event
            prediction_store = PredictionRecordStore(
                self.database_dir / "producer_consumer_ledger.db",
                config=self.config,
            )
            sealed = prediction_store.seal_nonmaterial(
                prediction_plan,
                principal=principal,
            )
            event = replace(
                event,
                metadata={
                    **event.metadata,
                    DELIVERY_PREDICTION_METADATA_KEY: {
                        **sealed.material_ref(),
                        "prediction_plan_hash": prediction_plan.prediction_plan_hash,
                        "delivery_event_payload_hash": prediction_plan.delivery_event_payload_hash,
                    },
                },
            )
            before_hash, _, committed_at = self._log_nonmaterial_event(event)
            self._record_prediction_projection_receipt(
                prediction_store=prediction_store,
                command_id=sealed.command_id,
                projection_effect_id=sealed.projection_effect_id,
                prediction_revision_id=sealed.revision_id,
                delivery_event_id=event.event_id,
                delivery_event_payload_hash=prediction_plan.delivery_event_payload_hash,
                before_hash=before_hash,
                committed_at=committed_at,
            )
            return event
        authorization, replay = resolved_delivery
        if replay is not None:
            return replay
        event = replace(
            event,
            metadata={
                **event.metadata,
                "material_action": _delivery_material_metadata(
                    authorization.permit
                ),
                **(
                    {
                        DELIVERY_PREDICTION_METADATA_KEY: {
                            **dict(authorization.permit.prediction_refs[0]),
                            "delivery_event_payload_hash": (
                                prediction_plan.delivery_event_payload_hash
                                if prediction_plan is not None
                                else ""
                            ),
                        }
                    }
                    if authorization.permit.prediction_refs
                    else {}
                ),
            },
        )
        require_material_action(
            authorization,
            owner=DELIVERY_MATERIAL_OWNER,
            executor_id=DELIVERY_MATERIAL_EXECUTOR,
            action_type=DELIVERY_MATERIAL_ACTION_TYPE,
            target_ref=supplied_binding["target_ref"],
            input_hash=supplied_binding["input_hash"],
            expected_state_db=(
                self.database_dir / "producer_consumer_ledger.db"
            ),
        )
        before_hash, after_hash, committed_at = self._log_event(event)
        permit = authorization.permit
        authorization.record_terminal(
            MaterialActionTerminal(
                status="committed",
                target_effect_id=permit.effect_id,
                before_hash=before_hash,
                after_hash=after_hash,
                evidence_refs=(
                    f"material-command:{permit.command_id}",
                    f"decision-revision:{permit.decision_revision_id}",
                    f"material-effect:{permit.effect_id}",
                    f"delivery-event:{event.event_id}",
                    f"delivery-event-hash:{after_hash}",
                    f"target-after:{after_hash}",
                    f"target-oracle:delivery-event:{event.event_id}:{after_hash}",
                    "transport-status:routed-not-presented",
                ),
                outcome="delivery route committed; presentation is unclaimed",
                created_at=committed_at,
            )
        )
        return event

    def _delivery_material_barrier(
        self,
        event: DeliveryDecision,
        *,
        material_action: MaterialActionAuthorization | None,
        binding: Mapping[str, str],
        prediction_plan: PredictionPlan | None,
    ) -> tuple[
        MaterialActionAuthorization,
        DeliveryDecision | None,
    ] | None:
        """Classify non-deliver rows and authorize every outward effect."""

        if event.decision != "deliver":
            if material_action is not None:
                raise PermissionError(
                    "suppressed delivery cannot consume an outward-delivery permit"
                )
            return None
        if material_action is None:
            state_db_path = (
                self.database_dir / "producer_consumer_ledger.db"
            ).resolve(strict=False)
            request = MaterialActionRequest(
                owner=DELIVERY_MATERIAL_OWNER,
                executor_id=DELIVERY_MATERIAL_EXECUTOR,
                action_type=DELIVERY_MATERIAL_ACTION_TYPE,
                target_ref=str(binding["target_ref"]),
                input_hash=str(binding["input_hash"]),
                expected_state_db=str(state_db_path),
            )
            trust_evidence = list(
                dict(event.metadata.get("trust_decision") or {}).get(
                    "evidence_refs"
                )
                or []
            )
            material_action = authorize_exact_project_contract_action(
                expected_request=request,
                state_db_path=state_db_path,
                contract_id=DELIVERY_DECISION_CONTRACT_ID,
                contract_revision_id=DELIVERY_DECISION_CONTRACT_REVISION,
                contract_text=DELIVERY_DECISION_CONTRACT_TEXT,
                source_namespace="knowledge-delivery-routing",
                source_facts={
                    "schema_version": "mnemos.knowledge_delivery_decision_facts.v1",
                    "delivery": event.to_dict(),
                    "policy": self.policy.to_dict(),
                    "material_binding": dict(binding),
                },
                decision_checks={
                    "delivery_selected": event.decision == "deliver",
                    "trust_decision_present": bool(event.trust_decision_id),
                    "delivery_target_present": bool(event.target or event.subject),
                },
                evidence_refs=tuple(
                    dict.fromkeys(
                        (
                            f"trust-decision:{event.trust_decision_id}",
                            f"delivery-target:{event.target or event.subject}",
                            *(str(ref) for ref in trust_evidence),
                        )
                    )
                ),
                task=f"Route {event.channel} delivery for {event.subject}",
                goal=(
                    "Emit only the exact outward delivery selected by trust and "
                    "interruption policy."
                ),
                constraints=(
                    "Trust and task-fit requirements must select delivery.",
                    "Cooldown and interruption budgets must remain available.",
                    "The channel, target, level, evidence, and request binding cannot drift.",
                ),
                created_at=(
                    prediction_plan.starts_at
                    if prediction_plan is not None
                    else _now()
                ),
                producer="knowledge-delivery-router",
                producer_version=DELIVERY_DECISION_CONTRACT_REVISION,
                producer_code_hash=DELIVERY_DECISION_PRODUCER_HASH,
                evaluator_id="knowledge-delivery-routing-evaluator",
                approved_candidate_key="deliver_exact_policy_selected_knowledge",
                approved_candidate_summary=(
                    "Deliver the exact candidate selected by trust, budget, and risk policy."
                ),
                rejected_candidate_key="suppress_delivery_under_policy",
                rejected_candidate_summary=(
                    "Suppress the candidate when trust, cooldown, budget, or risk policy blocks it."
                ),
                approved_reason_code="delivery_policy_selection_verified",
                rejected_reason_code="delivery_policy_selection_rejected",
                committed_metric="outward_delivery_route_receipt",
                rejected_metric="unbound_outward_delivery_count",
                prediction_plan=prediction_plan,
                prediction_config=self.config,
            )
        authorization, _ = resolve_material_action_recovery_authorization(
            material_action,
            owner=DELIVERY_MATERIAL_OWNER,
            executor_id=DELIVERY_MATERIAL_EXECUTOR,
            action_type=DELIVERY_MATERIAL_ACTION_TYPE,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=self.database_dir / "producer_consumer_ledger.db",
        )
        if prediction_plan is not None:
            refs = tuple(authorization.permit.prediction_refs)
            if len(refs) != 1 or (
                refs[0].get("prediction_id") != prediction_plan.prediction_id
                or refs[0].get("prediction_plan_hash")
                != prediction_plan.prediction_plan_hash
            ):
                raise PermissionError(
                    "predictive delivery permit lacks its exact pre-effect prediction"
                )
        replay = self._recover_delivery_event(authorization)
        if replay is not None:
            return authorization, replay
        require_material_action(
            authorization,
            owner=DELIVERY_MATERIAL_OWNER,
            executor_id=DELIVERY_MATERIAL_EXECUTOR,
            action_type=DELIVERY_MATERIAL_ACTION_TYPE,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=self.database_dir / "producer_consumer_ledger.db",
        )
        return authorization, None

    def _recover_delivery_event(
        self,
        authorization: MaterialActionAuthorization,
    ) -> DeliveryDecision | None:
        existing = self._delivery_event_for_command(
            authorization.permit.command_id
        )
        if existing is None:
            return None
        oracle = DeliveryEventEffectOracle(self.db_path)
        recovered = authorization.recover(oracle)
        if recovered is None:
            raise RuntimeError(
                "delivery replay could not observe its existing effect"
            )
        if oracle.observe(authorization.permit) is None:
            raise RuntimeError(
                "terminal delivery receipt lacks its exact delivery event"
            )
        return _delivery_decision_from_row(existing)

    def _delivery_event_for_command(
        self,
        command_id: str,
    ) -> dict[str, Any] | None:
        if not self.db_path.is_file():
            return None
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delivery_events
                WHERE json_extract(
                    metadata_json,
                    '$.material_action.command_id'
                )=?
                """,
                (str(command_id),),
            ).fetchall()
        if len(rows) > 1:
            raise RuntimeError(
                "material command maps to multiple delivery events"
            )
        return _row_to_dict(rows[0]) if rows else None

    def _recover_pending_prediction_event(
        self,
        request_binding: Mapping[str, str],
    ) -> DeliveryDecision | None:
        state_db = self.database_dir / "producer_consumer_ledger.db"
        if not state_db.is_file():
            return None
        prediction_store = PredictionRecordStore(
            state_db,
            config=self.config,
        )
        pending = prediction_store.state_store.pending_commands()
        for command in pending:
            if command.get("command_type") != "execute_material_action":
                continue
            payload = command.get("payload")
            if not isinstance(payload, Mapping):
                raise RuntimeError("pending material prediction payload is malformed")
            projection = payload.get("prediction_delivery_projection")
            if projection is None:
                continue
            if not isinstance(projection, Mapping):
                raise RuntimeError("material prediction projection is malformed")
            route = projection.get("delivery_event_payload")
            if not isinstance(route, Mapping):
                raise RuntimeError("material prediction route payload is malformed")
            if dict(route.get("request_binding") or {}) != dict(request_binding):
                continue
            if (
                str(payload.get("target_ref") or "")
                != str(request_binding["target_ref"])
                or str(payload.get("input_hash") or "")
                != str(request_binding["input_hash"])
            ):
                raise RuntimeError("material prediction request binding drifted")
            route_hash = sha256_json(route)
            if route_hash != projection.get("delivery_event_payload_hash"):
                raise RuntimeError("material prediction route payload hash mismatch")
            prediction_refs = payload.get("prediction_refs")
            if (
                not isinstance(prediction_refs, list)
                or len(prediction_refs) != 1
                or not isinstance(prediction_refs[0], Mapping)
            ):
                raise RuntimeError("material prediction ref is malformed")
            prediction_ref = dict(prediction_refs[0])
            revision = prediction_store.state_store.revision(
                str(prediction_ref.get("prediction_revision_id") or "")
            )
            if (
                revision is None
                or revision.object_type != "prediction_record"
                or revision.object_id != projection.get("prediction_id")
                or revision.object_id != prediction_ref.get("prediction_id")
                or revision.payload_hash
                != prediction_ref.get("prediction_revision_hash")
                or revision.payload["prediction_plan_hash"]
                != projection.get("prediction_plan_hash")
                or revision.payload["prediction_plan_hash"]
                != prediction_ref.get("prediction_plan_hash")
                or revision.payload["route_disposition"] not in {"deliver", "silent"}
            ):
                raise RuntimeError("material prediction revision binding failed")
            authorization = MaterialActionCoordinator(
                prediction_store.state_store
            ).bind_for_recovery(
                str(command["command_id"]),
                executor_id=DELIVERY_MATERIAL_EXECUTOR,
            )
            metadata = dict(route.get("metadata") or {})
            metadata["material_action"] = _delivery_material_metadata(
                authorization.permit
            )
            metadata[DELIVERY_PREDICTION_METADATA_KEY] = {
                **prediction_ref,
                "delivery_event_payload_hash": route_hash,
            }
            decision = DeliveryDecision(
                event_id=str(route["event_id"]),
                source=str(route["source"]),
                subject=str(route["subject"]),
                channel=str(route["channel"]),
                target=str(route["target"]),
                decision=str(route["decision"]),
                reason=str(route["reason"]),
                requested_level=str(route["requested_level"]),
                delivered_level=str(route["delivered_level"]),
                profile=str(route["profile"]),
                cooldown_key=str(route["cooldown_key"]),
                trust_decision_id=str(route["trust_decision_id"]),
                trust_score=float(route["trust_score"]),
                task_fit_score=float(route["task_fit_score"]),
                interruption_cost=float(route["interruption_cost"]),
                metadata=metadata,
            )
            require_material_action(
                authorization,
                owner=DELIVERY_MATERIAL_OWNER,
                executor_id=DELIVERY_MATERIAL_EXECUTOR,
                action_type=DELIVERY_MATERIAL_ACTION_TYPE,
                target_ref=str(request_binding["target_ref"]),
                input_hash=str(request_binding["input_hash"]),
                expected_state_db=state_db,
            )
            before_hash, after_hash, committed_at = self._log_event(decision)
            permit = authorization.permit
            authorization.record_terminal(
                MaterialActionTerminal(
                    status="committed",
                    target_effect_id=permit.effect_id,
                    before_hash=before_hash,
                    after_hash=after_hash,
                    evidence_refs=(
                        f"material-command:{permit.command_id}",
                        f"decision-revision:{permit.decision_revision_id}",
                        f"material-effect:{permit.effect_id}",
                        f"delivery-event:{decision.event_id}",
                        f"delivery-event-hash:{after_hash}",
                        f"target-after:{after_hash}",
                        (
                            "target-oracle:delivery-event:"
                            f"{decision.event_id}:{after_hash}"
                        ),
                        "transport-status:routed-not-presented",
                    ),
                    outcome=(
                        "recovered exact pre-effect prediction delivery; "
                        "presentation is unclaimed"
                    ),
                    created_at=committed_at,
                )
            )
            return decision
        for command in prediction_store.state_store.pending_commands(
            "prediction_delivery_projection"
        ):
            if command.get("command_type") != "project_prediction_delivery":
                continue
            payload = command.get("payload")
            if not isinstance(payload, Mapping):
                raise RuntimeError("pending prediction delivery payload is malformed")
            route = payload.get("delivery_event_payload")
            if not isinstance(route, Mapping):
                raise RuntimeError("pending prediction route payload is malformed")
            if dict(route.get("request_binding") or {}) != dict(request_binding):
                continue
            if sha256_json(route) != payload.get("delivery_event_payload_hash"):
                raise RuntimeError("pending prediction route payload hash mismatch")
            revision = prediction_store.state_store.revision(
                str(payload.get("prediction_revision_id") or "")
            )
            if (
                revision is None
                or revision.object_type != "prediction_record"
                or revision.object_id != payload.get("prediction_id")
                or revision.payload_hash != payload.get("prediction_revision_hash")
                or revision.payload["route_disposition"] != "suppress"
            ):
                raise RuntimeError("pending prediction revision binding failed")
            metadata = dict(route.get("metadata") or {})
            metadata[DELIVERY_PREDICTION_METADATA_KEY] = {
                "prediction_id": revision.object_id,
                "prediction_revision_id": revision.revision_id,
                "prediction_revision_hash": revision.payload_hash,
                "prediction_plan_hash": revision.payload["prediction_plan_hash"],
                "delivery_event_payload_hash": payload[
                    "delivery_event_payload_hash"
                ],
            }
            decision = DeliveryDecision(
                event_id=str(route["event_id"]),
                source=str(route["source"]),
                subject=str(route["subject"]),
                channel=str(route["channel"]),
                target=str(route["target"]),
                decision=str(route["decision"]),
                reason=str(route["reason"]),
                requested_level=str(route["requested_level"]),
                delivered_level=str(route["delivered_level"]),
                profile=str(route["profile"]),
                cooldown_key=str(route["cooldown_key"]),
                trust_decision_id=str(route["trust_decision_id"]),
                trust_score=float(route["trust_score"]),
                task_fit_score=float(route["task_fit_score"]),
                interruption_cost=float(route["interruption_cost"]),
                metadata=metadata,
            )
            before_hash, _, committed_at = self._log_nonmaterial_event(decision)
            self._record_prediction_projection_receipt(
                prediction_store=prediction_store,
                command_id=str(command["command_id"]),
                projection_effect_id=str(payload["projection_effect_id"]),
                prediction_revision_id=revision.revision_id,
                delivery_event_id=decision.event_id,
                delivery_event_payload_hash=str(
                    payload["delivery_event_payload_hash"]
                ),
                before_hash=before_hash,
                committed_at=committed_at,
            )
            return decision
        return None

    @staticmethod
    def _record_prediction_projection_receipt(
        *,
        prediction_store: PredictionRecordStore,
        command_id: str,
        projection_effect_id: str,
        prediction_revision_id: str,
        delivery_event_id: str,
        delivery_event_payload_hash: str,
        before_hash: str,
        committed_at: str,
    ) -> None:
        prediction_store.state_store.record_effect_receipt(
            command_id,
            status="committed",
            target_effect_id=projection_effect_id,
            before_hash=before_hash,
            after_hash=delivery_event_payload_hash,
            evidence_refs=(
                f"prediction-delivery-command:{command_id}",
                f"prediction-revision:{prediction_revision_id}",
                f"delivery-event:{delivery_event_id}",
                f"delivery-event-payload:{delivery_event_payload_hash}",
            ),
            outcome="suppressed predictive delivery event projected exactly",
            created_at=committed_at,
        )

    def replay_candidates(self, candidates: list[Mapping[str, Any]]) -> dict[str, Any]:
        decisions = []
        counters = {"deliver": 0, "suppress": 0, "warn": 0, "force_open": 0}
        for item in candidates:
            decision = self.route_candidate(
                source=str(item.get("source") or "replay"),
                subject=str(item.get("subject") or ""),
                channel=str(item.get("channel") or "predictive_push"),
                target=str(item.get("target") or ""),
                evidence_refs=list(item.get("evidence_refs") or []),
                task_fit_score=_to_float(item.get("task_fit_score"), 0.5),
                requested_level=str(item.get("requested_level") or "hint"),
                task_key=str(item.get("task_key") or "replay"),
                cooldown_key=str(item.get("cooldown_key") or item.get("subject") or ""),
                active_risk=bool(item.get("active_risk")),
                scope_type=str(item.get("scope_type") or ""),
                scope_value=str(item.get("scope_value") or ""),
                metadata={"replay": True, **dict(item.get("metadata") or {})},
                source_access_control=item.get("source_access_control"),
                principal=item.get("principal"),
            )
            decisions.append(decision.to_dict())
            counters[decision.decision] = counters.get(decision.decision, 0) + 1
            if decision.delivered_level == "warn":
                counters["warn"] += 1
            if decision.delivered_level == "force_open":
                counters["force_open"] += 1
        return {"schema_version": SCHEMA_VERSION, "count": len(decisions), "counters": counters, "decisions": decisions}

    def _budget_decision(
        self,
        *,
        level: str,
        trust_decision: TrustDecision,
        cooldown_key: str,
        task_key: str,
        active_risk: bool,
    ) -> tuple[str, str, str]:
        if trust_decision.decision != "deliver":
            return "suppress", f"trust_gate:{trust_decision.reason}", "silent"
        if level == "silent":
            return "deliver", "silent_delivery_logged", "silent"
        if self._count_since("", timedelta(days=1), visible_only=True) >= self.policy.daily_total:
            return "suppress", "daily_total_budget_exhausted", "silent"
        if (
            self._count_since(
                task_key,
                timedelta(days=1),
                key_column="task_key",
                visible_only=True,
            )
            >= self.policy.per_task_total
        ):
            return "suppress", "per_task_total_budget_exhausted", "silent"
        delivered_level = level
        reason = "delivery_requirements_met"
        if self.policy.preference == "quiet" and delivered_level in {"warn", "force_open"} and not active_risk:
            delivered_level = "hint"
            reason = "quiet_profile_downgrade"
        if delivered_level == "force_open" and (not active_risk or self.policy.force_open_daily <= 0):
            delivered_level = "warn"
            reason = "downgraded_force_open_budget_or_risk"
        if (
            delivered_level == "force_open"
            and self._count_since("", timedelta(days=1), level="force_open", visible_only=True)
            >= self.policy.force_open_daily
        ):
            delivered_level = "warn"
            reason = "downgraded_force_open_daily_budget"
        if (
            delivered_level == "warn"
            and self._count_since(
                task_key,
                timedelta(days=1),
                key_column="task_key",
                level="warn",
                visible_only=True,
            )
            >= self.policy.per_task_warn
        ):
            delivered_level = "hint"
            reason = "downgraded_warn_budget"
        if (
            delivered_level == "hint"
            and self._count_since(
                task_key,
                timedelta(days=1),
                key_column="task_key",
                level="hint",
                visible_only=True,
            )
            >= self.policy.per_task_hint
        ):
            return "suppress", "per_task_hint_budget_exhausted", "silent"
        if self._cooldown_active(cooldown_key):
            return "suppress", "same_topic_cooldown", "silent"
        return "deliver", reason, delivered_level

    def _cooldown_active(self, cooldown_key: str) -> bool:
        hours = max(0, int(self.policy.same_topic_cooldown_hours))
        if hours <= 0:
            return False
        return (
            self._count_since(
                cooldown_key,
                timedelta(hours=hours),
                key_column="cooldown_key",
                visible_only=True,
            )
            > 0
        )

    def _count_since(
        self,
        key: str,
        window: timedelta,
        *,
        key_column: str = "",
        level: str = "",
        visible_only: bool = False,
    ) -> int:
        if not self.db_path.exists():
            return 0
        cutoff = (datetime.now(timezone.utc) - window).isoformat(timespec="seconds")
        clauses = ["created_at >= ?", "decision = 'deliver'"]
        params: list[Any] = [cutoff]
        if key_column:
            key_column = _delivery_event_key_column(key_column)
            clauses.append(f"{key_column} = ?")
            params.append(_norm(key))
        if level:
            clauses.append("delivered_level = ?")
            params.append(level)
        if visible_only:
            clauses.append("delivered_level IN ('hint', 'warn', 'force_open')")
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM delivery_events WHERE {' AND '.join(clauses)}",  # nosec B608
                params,
            ).fetchone()
        return int(row[0]) if row else 0

    def _delivery_event(self, event_id: str) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        if not event_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM delivery_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def _log_event(self, decision: DeliveryDecision) -> tuple[str, str, str]:
        self._ensure_schema()
        metadata = dict(decision.metadata or {})
        task_key = _norm(str(metadata.get("task_key") or decision.channel))
        before_hash = sha256_json(
            {"event_id": decision.event_id, "state": "absent"}
        )
        created_at = _now()
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO delivery_events (
                        event_id, created_at, source, subject, channel, target,
                        requested_level, delivered_level, decision, reason, profile,
                        cooldown_key, task_key, trust_decision_id, trust_score,
                        task_fit_score, interruption_cost, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision.event_id,
                        created_at,
                        decision.source,
                        decision.subject,
                        decision.channel,
                        decision.target,
                        decision.requested_level,
                        decision.delivered_level,
                        decision.decision,
                        decision.reason,
                        decision.profile,
                        decision.cooldown_key,
                        task_key,
                        decision.trust_decision_id,
                        decision.trust_score,
                        decision.task_fit_score,
                        decision.interruption_cost,
                        _json_dumps(metadata),
                    ),
                )
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    "SELECT * FROM delivery_events WHERE event_id=?",
                    (decision.event_id,),
                ).fetchone()
                if existing is None:
                    raise
                existing_data = _row_to_dict(existing)
                if _delivery_decision_from_row(existing_data) != decision:
                    raise ValueError(
                        "immutable delivery event conflict"
                    ) from None
                created_at = str(existing_data["created_at"])
            conn.commit()
            row = conn.execute(
                "SELECT * FROM delivery_events WHERE event_id=?",
                (decision.event_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("delivery event commit could not be verified")
        return before_hash, delivery_effect_hash(_row_to_dict(row)), created_at

    def _log_nonmaterial_event(
        self,
        decision: DeliveryDecision,
    ) -> tuple[str, str, str]:
        """Persist only a canonical suppress decision with no outward effect."""

        if decision.decision == "deliver":
            raise PermissionError(
                "outward delivery cannot use the non-material event path"
            )
        if decision.delivered_level != "silent":
            raise PermissionError(
                "non-material delivery classification requires a silent outcome"
            )
        clean_payload = _delivery_nonmaterial_payload(decision)
        classified = replace(
            decision,
            metadata={
                **decision.metadata,
                DELIVERY_NONMATERIAL_PROOF_KEY: {
                    "schema_version": DELIVERY_NONMATERIAL_SCHEMA_VERSION,
                    "decision": decision.decision,
                    "payload_hash": sha256_json(clean_payload),
                },
            },
        )
        return self._log_event(classified)

    def record_presentation(
        self,
        event_id: str,
        *,
        host_agent: str,
        rendered_content_hash: str,
    ) -> dict[str, Any]:
        """Append one host-bound presentation receipt for an already delivered item.

        Routing and presentation are separate effects: this method never turns a
        suppressed route into a delivery, and it records only a content hash so
        the adapter can acknowledge a real render without duplicating content.
        """

        normalized_event_id = str(event_id or "").strip()
        normalized_agent = _norm(host_agent)
        normalized_hash = str(rendered_content_hash or "").strip()
        if not normalized_event_id or not normalized_agent:
            raise ValueError("delivery presentation requires event_id and host_agent")
        if not normalized_hash.startswith("sha256:") or len(normalized_hash) != 71:
            raise ValueError("rendered_content_hash must be a sha256 digest")
        delivery = self._delivery_event(normalized_event_id)
        if delivery is None:
            raise ValueError("delivery event is unavailable")
        if delivery.get("decision") != "deliver":
            raise ValueError("only delivered events can be presented")
        metadata = dict(delivery.get("metadata") or {})
        delivery_principal = metadata.get("delivery_principal")
        delivery_agent = _norm(
            str(delivery_principal.get("agent") or "")
            if isinstance(delivery_principal, Mapping)
            else ""
        )
        if not delivery_agent:
            raise ValueError("delivery event has no bound host principal")
        if delivery_agent != normalized_agent:
            raise PermissionError("delivery presentation principal mismatch")
        delivery_hash = delivery_effect_hash(delivery)
        payload = {
            "schema_version": DELIVERY_PRESENTATION_RECEIPT_SCHEMA_VERSION,
            "event_id": normalized_event_id,
            "host_agent": normalized_agent,
            "rendered_content_hash": normalized_hash,
            "delivery_event_hash": delivery_hash,
        }
        receipt_hash = sha256_json(payload)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM delivery_presentation_receipts WHERE event_id=?",
                (normalized_event_id,),
            ).fetchone()
            if existing is None:
                recorded_at = _now()
                conn.execute(
                    """
                    INSERT INTO delivery_presentation_receipts (
                        event_id, recorded_at, host_agent, rendered_content_hash,
                        delivery_event_hash, receipt_hash
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_event_id,
                        recorded_at,
                        normalized_agent,
                        normalized_hash,
                        delivery_hash,
                        receipt_hash,
                    ),
                )
                conn.commit()
            else:
                existing_data = _row_to_dict(existing)
                if (
                    existing_data.get("host_agent") != normalized_agent
                    or existing_data.get("rendered_content_hash") != normalized_hash
                    or existing_data.get("delivery_event_hash") != delivery_hash
                    or existing_data.get("receipt_hash") != receipt_hash
                ):
                    raise ValueError("immutable delivery presentation receipt conflict")
                recorded_at = str(existing_data["recorded_at"])
        return {
            "success": True,
            "schema_version": DELIVERY_PRESENTATION_RECEIPT_SCHEMA_VERSION,
            "status": "recorded",
            "delivery_event_id": normalized_event_id,
            "host_agent": normalized_agent,
            "rendered_content_hash": normalized_hash,
            "delivery_event_hash": delivery_hash,
            "receipt_hash": receipt_hash,
            "recorded_at": recorded_at,
            "evidence_refs": [
                f"delivery-event:{normalized_event_id}",
                f"delivery-event-hash:{delivery_hash}",
                f"delivery-presentation:{receipt_hash}",
            ],
        }

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_events (
                    event_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    subject TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL DEFAULT '',
                    target TEXT NOT NULL DEFAULT '',
                    requested_level TEXT NOT NULL DEFAULT 'hint',
                    delivered_level TEXT NOT NULL DEFAULT 'silent',
                    decision TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    profile TEXT NOT NULL DEFAULT 'balanced',
                    cooldown_key TEXT NOT NULL DEFAULT '',
                    task_key TEXT NOT NULL DEFAULT '',
                    trust_decision_id TEXT NOT NULL DEFAULT '',
                    trust_score REAL NOT NULL DEFAULT 0,
                    task_fit_score REAL NOT NULL DEFAULT 0,
                    interruption_cost REAL NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_delivery_events_budget
                ON delivery_events(created_at, decision, cooldown_key, task_key, delivered_level)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_presentation_receipts (
                    event_id TEXT PRIMARY KEY,
                    recorded_at TEXT NOT NULL,
                    host_agent TEXT NOT NULL,
                    rendered_content_hash TEXT NOT NULL,
                    delivery_event_hash TEXT NOT NULL,
                    receipt_hash TEXT NOT NULL UNIQUE,
                    FOREIGN KEY(event_id) REFERENCES delivery_events(event_id)
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


def _delivery_material_metadata(
    permit: MaterialActionPermit,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
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
    if permit.prediction_refs:
        payload["prediction_refs"] = [dict(value) for value in permit.prediction_refs]
    return payload


def _prediction_route_facts(
    decision: DeliveryDecision,
    *,
    evidence_refs: list[str],
    scope_type: str,
    scope_id: str,
    created_at: str,
    request_binding: Mapping[str, str],
    source_access_control: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the exact pre-effect route facts admitted by PredictionLedger."""

    return {
        "event_id": decision.event_id,
        "source": decision.source,
        "subject": decision.subject,
        "channel": decision.channel,
        "target": decision.target,
        "decision": decision.decision,
        "reason": decision.reason,
        "requested_level": decision.requested_level,
        "delivered_level": decision.delivered_level,
        "profile": decision.profile,
        "cooldown_key": decision.cooldown_key,
        "trust_decision_id": decision.trust_decision_id,
        "trust_score": decision.trust_score,
        "task_fit_score": decision.task_fit_score,
        "interruption_cost": decision.interruption_cost,
        "evidence_refs": sorted(str(value) for value in evidence_refs if str(value)),
        "created_at": created_at,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "request_binding": dict(request_binding),
        "source_access_control": dict(source_access_control),
        "metadata": dict(decision.metadata),
    }


def _delivery_nonmaterial_payload(decision: DeliveryDecision) -> dict[str, Any]:
    payload = decision.to_dict()
    metadata = dict(payload.get("metadata") or {})
    metadata.pop(DELIVERY_NONMATERIAL_PROOF_KEY, None)
    payload["metadata"] = metadata
    return payload


def verify_delivery_nonmaterial_row(row: Mapping[str, Any]) -> bool:
    """Verify one system-classified suppress row that emitted no outward effect."""

    data = dict(row)
    if "metadata_json" in data:
        try:
            metadata = json.loads(str(data.pop("metadata_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        data["metadata"] = metadata
    metadata = data.get("metadata")
    if not isinstance(metadata, Mapping):
        return False
    clean_metadata = dict(metadata)
    proof = clean_metadata.pop(DELIVERY_NONMATERIAL_PROOF_KEY, None)
    if not isinstance(proof, Mapping) or "material_action" in clean_metadata:
        return False
    decision = _delivery_decision_from_row(
        {**data, "metadata": clean_metadata}
    )
    return (
        decision.decision != "deliver"
        and decision.delivered_level == "silent"
        and dict(proof)
        == {
            "schema_version": DELIVERY_NONMATERIAL_SCHEMA_VERSION,
            "decision": decision.decision,
            "payload_hash": sha256_json(_delivery_nonmaterial_payload(decision)),
        }
    )


def delivery_effect_hash(row: Mapping[str, Any]) -> str:
    """Return the canonical hash bound by delivery and presentation receipts."""

    metadata = row.get("metadata")
    material_action = (
        dict(metadata).get("material_action")
        if isinstance(metadata, Mapping)
        else None
    )
    return sha256_json(
        {
            "schema_version": "mnemos.delivery_material_effect.v1",
            "event_id": str(row.get("event_id") or ""),
            "created_at": str(row.get("created_at") or ""),
            "source": str(row.get("source") or ""),
            "subject": str(row.get("subject") or ""),
            "channel": str(row.get("channel") or ""),
            "target": str(row.get("target") or ""),
            "requested_level": str(row.get("requested_level") or ""),
            "delivered_level": str(row.get("delivered_level") or ""),
            "decision": str(row.get("decision") or ""),
            "reason": str(row.get("reason") or ""),
            "profile": str(row.get("profile") or ""),
            "cooldown_key": str(row.get("cooldown_key") or ""),
            "task_key": str(row.get("task_key") or ""),
            "trust_decision_id": str(row.get("trust_decision_id") or ""),
            "trust_score": float(row.get("trust_score") or 0.0),
            "task_fit_score": float(row.get("task_fit_score") or 0.0),
            "interruption_cost": float(row.get("interruption_cost") or 0.0),
            "material_action": material_action,
        }
    )


def verify_delivery_presentation(
    db_path: Path,
    *,
    delivery_event_id: str,
    principal: PrincipalEnvelope,
    expected_delivery_hash: str = "",
) -> dict[str, Any]:
    """Read-only proof that one exact visible delivery reached its bound host.

    A routing decision is intentionally insufficient: the proof needs the
    immutable presentation receipt, the canonical delivery-effect hash, and
    the principal that authorized the original delivery.
    """

    normalized_event_id = str(delivery_event_id or "").strip()
    if not db_path.is_file() or not normalized_event_id:
        return {"ok": False, "reason": "delivery_event_not_found"}
    try:
        with sqlite3.connect(
            f"file:{db_path.resolve(strict=True)}?mode=ro", uri=True
        ) as conn:
            conn.row_factory = sqlite3.Row
            delivery_row = conn.execute(
                "SELECT * FROM delivery_events WHERE event_id=?",
                (normalized_event_id,),
            ).fetchone()
            presentation_row = conn.execute(
                "SELECT * FROM delivery_presentation_receipts WHERE event_id=?",
                (normalized_event_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return {"ok": False, "reason": "delivery_presentation_not_acknowledged"}
    if delivery_row is None:
        return {"ok": False, "reason": "delivery_event_not_found"}
    delivery = _row_to_dict(delivery_row)
    if (
        delivery.get("decision") != "deliver"
        or delivery.get("delivered_level") == "silent"
    ):
        return {"ok": False, "reason": "delivery_event_not_visible"}
    delivery_principal = dict(delivery.get("metadata") or {}).get(
        "delivery_principal"
    )
    if not isinstance(delivery_principal, Mapping):
        return {"ok": False, "reason": "delivery_event_principal_missing"}
    if (
        str(delivery_principal.get("principal_id") or "") != principal.principal_id
        or _norm(str(delivery_principal.get("agent") or ""))
        != _norm(principal.agent)
        or str(delivery_principal.get("capability_id") or "")
        != principal.capability_id
    ):
        return {"ok": False, "reason": "delivery_event_principal_mismatch"}
    delivery_hash = delivery_effect_hash(delivery)
    if expected_delivery_hash:
        prediction_metadata = dict(delivery.get("metadata") or {}).get(
            DELIVERY_PREDICTION_METADATA_KEY
        )
        route_hash = (
            str(prediction_metadata.get("delivery_event_payload_hash") or "")
            if isinstance(prediction_metadata, Mapping)
            else ""
        )
        if expected_delivery_hash != route_hash:
            return {"ok": False, "reason": "delivery_event_hash_mismatch"}
    if presentation_row is None:
        return {"ok": False, "reason": "delivery_presentation_not_acknowledged"}
    presentation = dict(presentation_row)
    rendered_content_hash = str(presentation.get("rendered_content_hash") or "").strip()
    expected_receipt_hash = sha256_json(
        {
            "schema_version": DELIVERY_PRESENTATION_RECEIPT_SCHEMA_VERSION,
            "event_id": normalized_event_id,
            "host_agent": _norm(principal.agent),
            "rendered_content_hash": rendered_content_hash,
            "delivery_event_hash": delivery_hash,
        }
    )
    if (
        _norm(str(presentation.get("host_agent") or "")) != _norm(principal.agent)
        or str(presentation.get("delivery_event_hash") or "") != delivery_hash
        or str(presentation.get("receipt_hash") or "") != expected_receipt_hash
    ):
        return {"ok": False, "reason": "delivery_presentation_not_acknowledged"}
    return {
        "ok": True,
        "delivery": delivery,
        "delivery_effect_hash": delivery_hash,
        "presentation_receipt": presentation,
    }


def _delivery_decision_from_row(row: Mapping[str, Any]) -> DeliveryDecision:
    metadata = row.get("metadata")
    return DeliveryDecision(
        event_id=str(row.get("event_id") or ""),
        source=str(row.get("source") or ""),
        subject=str(row.get("subject") or ""),
        channel=str(row.get("channel") or ""),
        target=str(row.get("target") or ""),
        decision=str(row.get("decision") or ""),
        reason=str(row.get("reason") or ""),
        requested_level=str(row.get("requested_level") or ""),
        delivered_level=str(row.get("delivered_level") or ""),
        profile=str(row.get("profile") or ""),
        cooldown_key=str(row.get("cooldown_key") or ""),
        trust_decision_id=str(row.get("trust_decision_id") or ""),
        trust_score=float(row.get("trust_score") or 0.0),
        task_fit_score=float(row.get("task_fit_score") or 0.0),
        interruption_cost=float(row.get("interruption_cost") or 0.0),
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )


def _delivery_event_key_column(column: str) -> str:
    column = validate_sql_identifier(column)
    if column not in DELIVERY_EVENT_KEY_COLUMNS:
        raise ValueError(f"Unsupported delivery_events key column: {column!r}")
    return column


def _norm_level(level: str) -> str:
    normalized = _norm(level)
    return normalized if normalized in DELIVERY_LEVELS else "hint"


def _interruption_cost(level: str) -> float:
    return {"silent": 0.0, "hint": 0.25, "warn": 0.55, "force_open": 0.9}.get(level, 0.25)


def _event_id(source: str, subject: str, channel: str) -> str:
    digest = hashlib.sha256(
        f"{source}|{subject}|{channel}|{_now()}|{uuid4().hex}".encode("utf-8")
    ).hexdigest()[:16]
    return f"delivery-{digest}"


def build_delivery_feedback_proposal_owner(database_dir: Path):
    """Return the delivery-owned pending-review journal for feedback commands."""

    from core.cognitive.feedback_target_registry import (
        build_registered_feedback_proposal_owner,
    )

    return build_registered_feedback_proposal_owner(database_dir, "delivery_state")
