# -*- coding: utf-8 -*-
"""Config-backed policy patches for preflight and guard injection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from core.config import get_config
from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionPermit,
    MaterialActionRequest,
    authorize_exact_project_contract_action,
    require_material_action,
    resolve_material_action_recovery_authorization,
)
from core.cognitive.state_contract import sha256_json
from core.cognitive.material_effect_ledger import (
    SqliteTargetEffectOracle,
    record_target_effect,
    recover_pending_target_effects,
    recover_recorded_target_effect,
)
from core.cognitive.material_effect_schema import (
    initialize_material_effect_schema,
)
from core.cognitive.policy_patch_support import (  # noqa: F401
    _cfg_get,
    _clamp,
    _clean,
    _first_text,
    _first_trigger,
    _json_dumps,
    _matched_trigger_terms,
    _norm_severity,
    _now,
    _sanitize_trigger_terms,
    _serialize_trigger_terms,
    _stable_id,
    _string_list,
    _to_bool,
    _to_float,
    _to_int,
    policy_patch_id,
)


SCHEMA_VERSION = "mnemos.policy_patches.v1"
NEGATIVE_FEEDBACK_OUTCOMES = {
    "dismiss": "dismissed",
    "irrelevant": "dismissed",
    "contradicted": "review",
    "outdated": "review",
    "harmful": "blocked",
}
VALID_SEVERITIES = {"critical", "high", "medium", "low"}
MAX_TRIGGER_TERM_LENGTH = 64
MAX_TRIGGER_WORDS = 6
MAX_TRIGGER_CJK_CHARS = 12
SENTENCE_PUNCTUATION = frozenset("，,。！？!?；;：:\n\r")
POLICY_PATCH_PROPOSE_ACTION = "policy_patch_propose"
POLICY_PATCH_FEEDBACK_ACTION = "policy_patch_feedback"
POLICY_PATCH_RECONCILE_ACTION = "policy_patch_reconcile"
POLICY_PATCH_OWNER = "policy_patch"
POLICY_PATCH_EXECUTOR = "policy_patch_store"
POLICY_PATCH_DECISION_CONTRACT_ID = (
    "project-contract:policy-patch-material-actions"
)
POLICY_PATCH_DECISION_CONTRACT_REVISION = (
    "mnemos.policy_patch_material_actions.v1"
)
POLICY_PATCH_DECISION_CONTRACT_TEXT = (
    "The PolicyPatch domain may persist only an exact eligible bounded patch, "
    "an exact feedback correction, or an exact reviewed trigger reconciliation."
)
POLICY_PATCH_DECISION_PRODUCER_HASH = sha256_json(
    {
        "module": "core.cognitive.policy_patch",
        "producer": "policy-patch-domain-decision-producer",
        "version": POLICY_PATCH_DECISION_CONTRACT_REVISION,
    }
)


class PolicyPatchProposeEffectOracle(SqliteTargetEffectOracle):
    """Observe one committed policy-patch proposal effect."""

    owner = POLICY_PATCH_OWNER
    executor_id = POLICY_PATCH_EXECUTOR
    action_type = POLICY_PATCH_PROPOSE_ACTION


class PolicyPatchReconcileEffectOracle(SqliteTargetEffectOracle):
    """Observe one committed policy-patch reconciliation effect."""

    owner = POLICY_PATCH_OWNER
    executor_id = POLICY_PATCH_EXECUTOR
    action_type = POLICY_PATCH_RECONCILE_ACTION


class PolicyPatchFeedbackEffectOracle(SqliteTargetEffectOracle):
    """Observe one committed policy-patch feedback effect."""

    owner = POLICY_PATCH_OWNER
    executor_id = POLICY_PATCH_EXECUTOR
    action_type = POLICY_PATCH_FEEDBACK_ACTION


def authorize_exact_policy_patch_action(
    *,
    expected_request: MaterialActionRequest,
    state_db_path: Path,
    source_namespace: str,
    source_facts: Mapping[str, Any],
    evidence_refs: tuple[str, ...],
    task: str,
    goal: str,
    constraints: tuple[str, ...],
    created_at: str,
    producer: str,
    evaluator_id: str,
    approved_candidate_key: str,
    approved_candidate_summary: str,
    rejected_candidate_key: str,
    rejected_candidate_summary: str,
    committed_metric: str,
    rejected_metric: str,
) -> MaterialActionAuthorization:
    """Seal one exact PolicyPatch-domain decision before persistence."""

    return authorize_exact_project_contract_action(
        expected_request=expected_request,
        state_db_path=state_db_path,
        contract_id=POLICY_PATCH_DECISION_CONTRACT_ID,
        contract_revision_id=POLICY_PATCH_DECISION_CONTRACT_REVISION,
        contract_text=POLICY_PATCH_DECISION_CONTRACT_TEXT,
        source_namespace=source_namespace,
        source_facts=source_facts,
        decision_checks={
            "registered_policy_patch_action": expected_request.action_type
            in {
                POLICY_PATCH_PROPOSE_ACTION,
                POLICY_PATCH_RECONCILE_ACTION,
                POLICY_PATCH_FEEDBACK_ACTION,
            },
            "domain_facts_present": bool(source_facts),
            "evidence_refs_present": bool(evidence_refs),
        },
        evidence_refs=evidence_refs,
        task=task,
        goal=goal,
        constraints=constraints,
        created_at=created_at,
        producer=producer,
        producer_version=POLICY_PATCH_DECISION_CONTRACT_REVISION,
        producer_code_hash=POLICY_PATCH_DECISION_PRODUCER_HASH,
        evaluator_id=evaluator_id,
        approved_candidate_key=approved_candidate_key,
        approved_candidate_summary=approved_candidate_summary,
        rejected_candidate_key=rejected_candidate_key,
        rejected_candidate_summary=rejected_candidate_summary,
        approved_reason_code="policy_patch_exact_action_verified",
        rejected_reason_code="policy_patch_action_rejected",
        committed_metric=committed_metric,
        rejected_metric=rejected_metric,
    )


@dataclass(frozen=True)
class PolicyPatchOptions:
    """Runtime options for policy patch storage and retrieval."""

    database_dir: Path
    db_path: Path
    enabled: bool = True
    ttl_days: int = 30
    min_confidence: float = 0.7
    max_active: int = 5

    @classmethod
    def from_config(
        cls,
        cfg: Any | None = None,
        *,
        database_dir: Path | None = None,
    ) -> "PolicyPatchOptions":
        cfg = cfg or get_config()
        base_dir = Path(database_dir or getattr(cfg, "database_dir", "") or Path.home() / ".mnemos")
        configured_db = _cfg_get(cfg, "policy_patch.db_path", None)
        db_path = Path(configured_db).expanduser() if configured_db else base_dir / "policy_patches.db"
        return cls(
            database_dir=base_dir.expanduser(),
            db_path=db_path.expanduser(),
            enabled=_to_bool(_cfg_get(cfg, "policy_patch.enabled", True), True),
            ttl_days=max(1, _to_int(_cfg_get(cfg, "policy_patch.ttl_days", 30), 30)),
            min_confidence=_clamp(_to_float(_cfg_get(cfg, "policy_patch.min_confidence", 0.7), 0.7)),
            max_active=max(1, _to_int(_cfg_get(cfg, "policy_patch.max_active", 5), 5)),
        )


def policy_patch_proposal_binding(
    lesson: Mapping[str, Any],
    options: PolicyPatchOptions,
) -> dict[str, str] | None:
    """Return the stable target/input binding for one eligible patch proposal."""

    content = _first_text(
        lesson,
        "summary",
        "content",
        "lesson",
        "method",
        "recommendation",
        "action",
    )
    confidence = _clamp(_to_float(lesson.get("confidence"), 1.0))
    trigger = _first_trigger(lesson)
    if (
        not options.enabled
        or not content
        or confidence < options.min_confidence
        or not trigger
    ):
        return None
    source_type = _clean(lesson.get("source_type"), "lesson")
    source_id = _clean(
        lesson.get("source_id") or lesson.get("id"),
        _stable_id(source_type, content),
    )
    task_type = _clean(lesson.get("task_type"), "general")
    subtype = _clean(lesson.get("subtype"), "general")
    patch_id = _stable_id(
        source_type,
        source_id,
        task_type,
        subtype,
        content,
    )
    payload = {
        "schema_version": "mnemos.policy_patch_proposal_input.v1",
        "patch_id": patch_id,
        "source_type": source_type,
        "source_id": source_id,
        "task_type": task_type,
        "subtype": subtype,
        "scope": _clean(lesson.get("scope") or lesson.get("scope_type"), "global"),
        "severity": _norm_severity(str(lesson.get("severity") or "medium")),
        "content": content,
        "trigger": trigger,
        "confidence": confidence,
        "evidence_refs": _string_list(lesson.get("evidence_refs")),
        "expires_at": _clean(lesson.get("expires_at"), ""),
        "ttl_days": int(options.ttl_days),
        "metadata": dict(lesson.get("metadata") or {}),
    }
    return {
        "target_ref": f"policy-patch:{patch_id}",
        "input_hash": sha256_json(payload),
    }


def policy_patch_feedback_binding(
    *,
    patch_id: str,
    outcome: str,
    evidence: Mapping[str, Any] | None = None,
    source_event_id: str = "",
) -> dict[str, str]:
    """Bind one feedback mutation to its exact policy-patch input."""

    payload = {
        "schema_version": "mnemos.policy_patch_feedback_input.v1",
        "patch_id": str(patch_id),
        "outcome": _clean(outcome, "unknown"),
        "evidence": dict(evidence or {}),
        "source_event_id": str(source_event_id or ""),
    }
    return {
        "target_ref": f"policy-patch:{patch_id}",
        "input_hash": sha256_json(payload),
    }


def policy_patch_reconcile_binding(
    changes: list[dict[str, Any]],
) -> dict[str, str]:
    """Bind a deterministic reconciliation batch to one target identity."""

    input_hash = sha256_json(
        {
            "schema_version": "mnemos.policy_patch_reconcile_input.v1",
            "changes": changes,
        }
    )
    return {
        "target_ref": f"policy-patch-reconcile:{input_hash.split(':', 1)[1][:32]}",
        "input_hash": input_hash,
    }


@dataclass(frozen=True)
class PolicyPatch:
    """One bounded strategy patch derived from high-value lessons."""

    patch_id: str
    source_type: str
    source_id: str
    task_type: str
    subtype: str
    scope: str
    severity: str
    content: str
    trigger: str
    confidence: float
    evidence_refs: list[str] = field(default_factory=list)
    status: str = "active"
    created_at: str = ""
    expires_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    delivery_mode: str = "preflight_guard_only"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PolicyPatchStore:
    """Store and retrieve bounded policy patches without editing prompts."""

    def __init__(
        self,
        options: PolicyPatchOptions | None = None,
        *,
        config: Any | None = None,
        database_dir: Path | None = None,
        ensure_db: bool = True,
    ):
        self.options = options or PolicyPatchOptions.from_config(
            config,
            database_dir=database_dir,
        )
        if ensure_db:
            self._ensure_schema()

    def propose(
        self,
        lesson: Mapping[str, Any],
        *,
        material_action: MaterialActionAuthorization | None = None,
    ) -> PolicyPatch | None:
        """Persist a lesson as an active policy patch when it is trustworthy enough."""
        recover_pending_target_effects(
            state_db_path=self.options.database_dir / "producer_consumer_ledger.db",
            oracle=PolicyPatchProposeEffectOracle(self.options.db_path),
        )
        if not self.options.enabled:
            return None
        content = _first_text(
            lesson,
            "summary",
            "content",
            "lesson",
            "method",
            "recommendation",
            "action",
        )
        if not content:
            return None
        confidence = _clamp(_to_float(lesson.get("confidence"), 1.0))
        if confidence < self.options.min_confidence:
            return None

        now = _now()
        source_type = _clean(lesson.get("source_type"), "lesson")
        source_id = _clean(lesson.get("source_id") or lesson.get("id"), _stable_id(source_type, content))
        task_type = _clean(lesson.get("task_type"), "general")
        subtype = _clean(lesson.get("subtype"), "general")
        scope = _clean(lesson.get("scope") or lesson.get("scope_type"), "global")
        severity = _norm_severity(str(lesson.get("severity") or "medium"))
        trigger = _first_trigger(lesson)
        if not trigger:
            return None
        evidence_refs = _string_list(lesson.get("evidence_refs"))
        expires_at = _clean(lesson.get("expires_at"), "") or (
            datetime.now(timezone.utc) + timedelta(days=self.options.ttl_days)
        ).isoformat(timespec="seconds")
        patch_id = _stable_id(source_type, source_id, task_type, subtype, content)
        metadata = dict(lesson.get("metadata") or {})
        metadata.update(
            {
                "schema_version": SCHEMA_VERSION,
                "delivery_mode": "preflight_guard_only",
            }
        )

        patch = PolicyPatch(
            patch_id=patch_id,
            source_type=source_type,
            source_id=source_id,
            task_type=task_type,
            subtype=subtype,
            scope=scope,
            severity=severity,
            content=content,
            trigger=trigger,
            confidence=confidence,
            evidence_refs=evidence_refs,
            status="active",
            created_at=now,
            expires_at=expires_at,
            metadata=metadata,
        )
        binding = policy_patch_proposal_binding(lesson, self.options)
        if binding is None:
            raise RuntimeError("eligible policy patch lacks a material binding")
        material_action, permit = resolve_material_action_recovery_authorization(
            material_action,
            owner=POLICY_PATCH_OWNER,
            executor_id=POLICY_PATCH_EXECUTOR,
            action_type=POLICY_PATCH_PROPOSE_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=self.options.database_dir / "producer_consumer_ledger.db",
        )
        if recover_recorded_target_effect(
            material_action,
            PolicyPatchProposeEffectOracle(self.options.db_path),
        ):
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT * FROM policy_patches WHERE patch_id=?",
                    (patch.patch_id,),
                ).fetchone()
            if existing is None:
                raise RuntimeError("recovered policy patch effect has no target row")
            return _row_to_patch(existing)
        permit = require_material_action(
            material_action,
            owner=POLICY_PATCH_OWNER,
            executor_id=POLICY_PATCH_EXECUTOR,
            action_type=POLICY_PATCH_PROPOSE_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=self.options.database_dir / "producer_consumer_ledger.db",
        )
        required_refs = {
            f"material-command:{permit.command_id}",
            f"decision-revision:{permit.decision_revision_id}",
            f"material-effect:{permit.effect_id}",
        }
        patch = replace(
            patch,
            evidence_refs=sorted(set(patch.evidence_refs) | required_refs),
            metadata={
                **patch.metadata,
                "material_action": {
                    "command_id": permit.command_id,
                    "decision_revision_id": permit.decision_revision_id,
                    "action_id": permit.action_id,
                    "effect_id": permit.effect_id,
                    "input_hash": binding["input_hash"],
                },
            },
        )
        before_hash = self._patch_state_hash(patch.patch_id)
        values = (
            patch.patch_id,
            patch.source_type,
            patch.source_id,
            patch.task_type,
            patch.subtype,
            patch.scope,
            patch.severity,
            patch.content,
            patch.trigger,
            patch.confidence,
            _json_dumps(patch.evidence_refs),
            patch.status,
            patch.created_at,
            patch.expires_at,
            _json_dumps(patch.metadata),
            patch.delivery_mode,
        )
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO policy_patches (
                        patch_id, source_type, source_id, task_type, subtype, scope,
                        severity, content, trigger, confidence, evidence_refs_json,
                        status, created_at, expires_at, metadata_json, delivery_mode
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            except sqlite3.IntegrityError:
                existing = conn.execute(
                    "SELECT * FROM policy_patches WHERE patch_id=?",
                    (patch.patch_id,),
                ).fetchone()
                if existing is None or tuple(existing) != values:
                    raise ValueError("immutable policy patch proposal conflict") from None
            stored = conn.execute(
                "SELECT * FROM policy_patches WHERE patch_id=?",
                (patch.patch_id,),
            ).fetchone()
            after_hash = sha256_json(dict(stored) if stored is not None else None)
            recorded_at = _now()
            record_target_effect(
                conn,
                permit,
                status="committed",
                before_hash=before_hash,
                after_hash=after_hash,
                evidence_refs=(
                    f"target-after:{after_hash}",
                    f"target-journal:policy-patch:{patch.patch_id}:{after_hash}",
                ),
                outcome="policy patch proposal activated",
                observed_at=recorded_at,
            )
        if not recover_recorded_target_effect(
            material_action,
            PolicyPatchProposeEffectOracle(self.options.db_path),
        ):
            raise RuntimeError("policy patch effect journal was not recoverable")
        return patch

    def active_for(
        self,
        task_type: str = "",
        subtype: str = "",
        context: str = "",
        scope: str = "global",
    ) -> list[PolicyPatch]:
        """Return active, unexpired patches that fit the current task/context."""
        if not self.options.enabled:
            return []
        now = _now()
        task_key = _clean(task_type, "general")
        subtype_key = _clean(subtype, "general")
        scope_key = _clean(scope, "global")
        context_text = " ".join([task_key, subtype_key, str(context or "")]).lower()

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM policy_patches
                WHERE status = 'active'
                  AND (expires_at = '' OR expires_at >= ?)
                  AND task_type IN (?, 'general', '')
                  AND subtype IN (?, 'general', '')
                  AND (scope IN ('global', '') OR scope = ?)
                ORDER BY
                    CASE severity
                        WHEN 'critical' THEN 0
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        ELSE 3
                    END,
                    confidence DESC,
                    created_at DESC
                """,
                (
                    now,
                    task_key,
                    subtype_key,
                    scope_key,
                ),
            ).fetchall()

        candidates: list[tuple[int, float, PolicyPatch, list[str]]] = []
        for rank, patch in enumerate(_row_to_patch(row) for row in rows):
            matched_triggers = _matched_trigger_terms(patch.trigger, context_text)
            if matched_triggers:
                candidates.append(
                    (
                        rank,
                        _task_fit_score(
                            patch,
                            task_type=task_key,
                            subtype=subtype_key,
                        ),
                        patch,
                        matched_triggers,
                    )
                )
        candidates.sort(
            key=lambda item: (-item[1], -len(set(item[3])), item[0])
        )

        matched: list[PolicyPatch] = []
        seen_dedupe_keys: set[str] = set()
        selected_trigger_sets: dict[tuple[str, str, str], list[set[str]]] = {}
        for _rank, task_fit_score, patch, matched_triggers in candidates:
            selection_scope_key = (task_key, subtype_key, patch.scope)
            trigger_set = {item.lower() for item in matched_triggers}
            if any(
                trigger_set <= selected
                for selected in selected_trigger_sets.get(selection_scope_key, [])
            ):
                continue
            dedupe_key = _policy_patch_dedupe_key(patch, matched_triggers)
            if dedupe_key in seen_dedupe_keys:
                continue
            seen_dedupe_keys.add(dedupe_key)
            selected_trigger_sets.setdefault(selection_scope_key, []).append(trigger_set)
            metadata = dict(patch.metadata)
            metadata.update(
                {
                    "match_source": "current_context",
                    "matched_triggers": matched_triggers,
                    "task_fit_score": task_fit_score,
                    "dedupe_key": dedupe_key,
                    "interruption_budget": self.options.max_active,
                    "interruption_budget_ok": True,
                }
            )
            matched.append(replace(patch, metadata=metadata))
            if len(matched) >= self.options.max_active:
                break
        return matched

    def prepare_feedback_material_action(
        self,
        *,
        patch_id: str,
        outcome: str,
        evidence: Mapping[str, Any] | None,
        source_event_id: str,
        source_facts: Mapping[str, Any],
        evidence_refs: tuple[str, ...],
        created_at: str,
        producer: str,
    ) -> MaterialActionAuthorization | None:
        """Seal a new exact feedback mutation; return none for an exact replay."""

        normalized = _clean(outcome, "unknown")
        if normalized == "no_patch":
            return None
        if source_event_id:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT 1 FROM policy_patch_feedback WHERE source_event_id=?",
                    (source_event_id,),
                ).fetchone()
            if existing is not None:
                return None
        binding = policy_patch_feedback_binding(
            patch_id=patch_id,
            outcome=outcome,
            evidence=evidence,
            source_event_id=source_event_id,
        )
        state_db_path = (
            self.options.database_dir / "producer_consumer_ledger.db"
        ).resolve(strict=False)
        request = MaterialActionRequest(
            owner=POLICY_PATCH_OWNER,
            executor_id=POLICY_PATCH_EXECUTOR,
            action_type=POLICY_PATCH_FEEDBACK_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=str(state_db_path),
        )
        return authorize_exact_policy_patch_action(
            expected_request=request,
            state_db_path=state_db_path,
            source_namespace="policy-patch-feedback",
            source_facts={
                "schema_version": "mnemos.policy_patch_feedback_facts.v1",
                "patch_id": patch_id,
                "outcome": normalized,
                "evidence": dict(evidence or {}),
                "source_event_id": source_event_id,
                **dict(source_facts),
            },
            evidence_refs=evidence_refs,
            task=f"Record feedback for policy patch {patch_id}",
            goal="Apply only the exact feedback transition bound to this patch.",
            constraints=(
                "Patch identity, outcome, evidence, and source event must remain exact.",
                "An existing source event is a replay and cannot create a new decision.",
            ),
            created_at=created_at,
            producer=producer,
            evaluator_id="policy-patch-feedback-evaluator",
            approved_candidate_key="apply_exact_policy_feedback",
            approved_candidate_summary=(
                "Apply the exact feedback and resulting bounded patch status."
            ),
            rejected_candidate_key="retain_policy_patch_status",
            rejected_candidate_summary=(
                "Retain current patch status when feedback identity or evidence drifts."
            ),
            committed_metric="policy_patch_feedback_committed",
            rejected_metric="unbound_policy_patch_feedback_count",
        )

    def prepare_reconcile_material_action(
        self,
        changes: list[dict[str, Any]],
        *,
        source_facts: Mapping[str, Any],
        evidence_refs: tuple[str, ...],
        created_at: str,
        producer: str,
    ) -> MaterialActionAuthorization | None:
        """Seal an exact reviewed trigger reconciliation batch."""

        if not changes:
            return None
        binding = policy_patch_reconcile_binding(changes)
        state_db_path = (
            self.options.database_dir / "producer_consumer_ledger.db"
        ).resolve(strict=False)
        request = MaterialActionRequest(
            owner=POLICY_PATCH_OWNER,
            executor_id=POLICY_PATCH_EXECUTOR,
            action_type=POLICY_PATCH_RECONCILE_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=str(state_db_path),
        )
        return authorize_exact_policy_patch_action(
            expected_request=request,
            state_db_path=state_db_path,
            source_namespace="policy-patch-trigger-reconcile",
            source_facts={
                "schema_version": "mnemos.policy_patch_reconcile_facts.v1",
                "changes": changes,
                **dict(source_facts),
            },
            evidence_refs=evidence_refs,
            task="Reconcile stored PolicyPatch trigger terms",
            goal="Apply only the exact reviewed trigger sanitization batch.",
            constraints=(
                "The reviewed patch IDs, trigger removals, and statuses cannot drift.",
                "No replacement trigger may be invented during reconciliation.",
            ),
            created_at=created_at,
            producer=producer,
            evaluator_id="policy-patch-trigger-reconcile-evaluator",
            approved_candidate_key="apply_exact_trigger_reconciliation",
            approved_candidate_summary=(
                "Apply the exact reviewed trigger removals and status transitions."
            ),
            rejected_candidate_key="retain_existing_policy_triggers",
            rejected_candidate_summary=(
                "Retain current triggers if the reviewed batch or backup identity drifts."
            ),
            committed_metric="policy_patch_reconcile_committed",
            rejected_metric="unbound_policy_patch_reconcile_count",
        )

    def reconcile_trigger_terms(
        self,
        *,
        apply: bool = False,
        material_action: MaterialActionAuthorization | None = None,
    ) -> dict[str, Any]:
        """Sanitize stored triggers without inventing replacement activation terms."""

        recover_pending_target_effects(
            state_db_path=self.options.database_dir / "producer_consumer_ledger.db",
            oracle=PolicyPatchReconcileEffectOracle(self.options.db_path),
        )

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT patch_id, source_type, trigger, status, metadata_json
                FROM policy_patches
                """
            ).fetchall()
        changes: list[dict[str, Any]] = []
        operations: list[dict[str, Any]] = []
        for row in rows:
            original_terms = _string_list(row["trigger"])
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            generated_terms = (
                _string_list(metadata.get("key_points"))
                if row["source_type"] == "reflection"
                else []
            )
            generated_keys = {term.strip().lower() for term in generated_terms}
            sanitized_terms = _sanitize_trigger_terms(
                [
                    term
                    for term in original_terms
                    if term.strip().lower() not in generated_keys
                ]
            )
            target_status = row["status"] if sanitized_terms else "review"
            if sanitized_terms == original_terms and target_status == row["status"]:
                continue
            new_trigger = _serialize_trigger_terms(sanitized_terms)
            change = {
                "patch_id": row["patch_id"],
                "removed_term_count": len(original_terms) - len(sanitized_terms),
                "remaining_term_count": len(sanitized_terms),
                "target_status": target_status,
                "new_trigger": new_trigger,
            }
            changes.append(change)
            operations.append({**change, "metadata": metadata})

        if apply and operations:
            binding = policy_patch_reconcile_binding(changes)
            material_action, permit = resolve_material_action_recovery_authorization(
                material_action,
                owner=POLICY_PATCH_OWNER,
                executor_id=POLICY_PATCH_EXECUTOR,
                action_type=POLICY_PATCH_RECONCILE_ACTION,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
                expected_state_db=self.options.database_dir / "producer_consumer_ledger.db",
            )
            if recover_recorded_target_effect(
                material_action,
                PolicyPatchReconcileEffectOracle(self.options.db_path),
            ):
                operations = []
            if not operations:
                return {
                    "schema_version": "mnemos.policy_patch_trigger_reconciliation.v1",
                    "applied": True,
                    "scanned": len(rows),
                    "changed": len(changes),
                    "moved_to_review": sum(
                        1
                        for item in changes
                        if item["target_status"] == "review"
                    ),
                    "removed_term_count": sum(
                        int(item["removed_term_count"]) for item in changes
                    ),
                    "changes": changes,
                }
            permit = require_material_action(
                material_action,
                owner=POLICY_PATCH_OWNER,
                executor_id=POLICY_PATCH_EXECUTOR,
                action_type=POLICY_PATCH_RECONCILE_ACTION,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
                expected_state_db=(
                    self.options.database_dir / "producer_consumer_ledger.db"
                ),
            )
            patch_ids = [str(item["patch_id"]) for item in operations]
            before_hash = self._patch_batch_state_hash(patch_ids)
            reconciled_at = _now()
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                for item in operations:
                    metadata = dict(item["metadata"])
                    metadata["trigger_reconciliation"] = {
                        "schema_version": "mnemos.policy_patch_trigger_reconciliation.v1",
                        "removed_term_count": item["removed_term_count"],
                        "remaining_term_count": item["remaining_term_count"],
                        "reconciled_at": reconciled_at,
                        "decision_revision_id": permit.decision_revision_id,
                        "material_command_id": permit.command_id,
                    }
                    conn.execute(
                        """
                        UPDATE policy_patches
                        SET trigger=?, status=?, metadata_json=?
                        WHERE patch_id=?
                        """,
                        (
                            item["new_trigger"],
                            item["target_status"],
                            _json_dumps(metadata),
                            item["patch_id"],
                        ),
                    )
                after_hash = self._patch_batch_state_hash_conn(conn, patch_ids)
                record_target_effect(
                    conn,
                    permit,
                    status="committed",
                    before_hash=before_hash,
                    after_hash=after_hash,
                    evidence_refs=(
                        f"target-after:{after_hash}",
                        f"target-journal:policy-reconcile:{after_hash}",
                    ),
                    outcome="policy patch trigger reconciliation committed",
                    observed_at=reconciled_at,
                )
            if not recover_recorded_target_effect(
                material_action,
                PolicyPatchReconcileEffectOracle(self.options.db_path),
            ):
                raise RuntimeError("policy reconciliation journal was not recoverable")
        return {
            "schema_version": "mnemos.policy_patch_trigger_reconciliation.v1",
            "applied": apply,
            "scanned": len(rows),
            "changed": len(changes),
            "moved_to_review": sum(
                1 for item in changes if item["target_status"] == "review"
            ),
            "removed_term_count": sum(
                int(item["removed_term_count"]) for item in changes
            ),
            "changes": changes,
        }

    def record_feedback(
        self,
        patch_id: str,
        *,
        outcome: str,
        evidence: Mapping[str, Any] | None = None,
        source_event_id: str = "",
        material_action: MaterialActionAuthorization | None = None,
    ) -> dict[str, Any]:
        """Record user/agent feedback and suppress unsafe or irrelevant patches."""
        recover_pending_target_effects(
            state_db_path=self.options.database_dir / "producer_consumer_ledger.db",
            oracle=PolicyPatchFeedbackEffectOracle(self.options.db_path),
        )
        normalized = _clean(outcome, "unknown")
        status = NEGATIVE_FEEDBACK_OUTCOMES.get(normalized)
        created_at = _now()
        with self._connect() as conn:
            patch = conn.execute(
                "SELECT patch_id FROM policy_patches WHERE patch_id=?",
                (patch_id,),
            ).fetchone()
            if patch is None and normalized != "no_patch":
                raise ValueError("policy patch does not exist")
            if source_event_id:
                existing = conn.execute(
                    """
                    SELECT feedback_id, outcome, created_at
                    FROM policy_patch_feedback WHERE source_event_id=?
                    """,
                    (source_event_id,),
                ).fetchone()
                if existing:
                    return {
                        "feedback_id": existing["feedback_id"],
                        "patch_id": patch_id,
                        "outcome": existing["outcome"],
                        "status": self.get_patch(patch_id).get("status", ""),
                        "created_at": existing["created_at"],
                    }
        resolved_material = self._feedback_material_barrier(
            patch_id=patch_id,
            outcome=outcome,
            normalized_outcome=normalized,
            evidence=evidence,
            source_event_id=source_event_id,
            material_action=material_action,
        )
        if normalized == "no_patch":
            return self._record_no_patch_feedback(
                patch_id=patch_id,
                normalized_outcome=normalized,
                evidence=evidence,
                source_event_id=source_event_id,
                created_at=created_at,
            )
        if resolved_material is None:
            raise RuntimeError("material policy feedback was not authorized")
        material_action, permit, binding, recovered_material = resolved_material
        feedback_id = (
            f"policy-feedback-{hashlib.sha256(source_event_id.encode('utf-8')).hexdigest()[:24]}"
            if source_event_id
            else "policy-feedback-" + permit.action_id.removeprefix("material-action-")[:24]
        )
        if recovered_material:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT * FROM policy_patch_feedback WHERE feedback_id=?",
                    (feedback_id,),
                ).fetchone()
            if existing is None:
                raise RuntimeError("recovered policy feedback has no target row")
            return {
                "feedback_id": feedback_id,
                "patch_id": patch_id,
                "outcome": str(existing["outcome"]),
                "status": self.get_patch(patch_id).get("status", ""),
                "created_at": str(existing["created_at"]),
            }
        require_material_action(
            material_action,
            owner=POLICY_PATCH_OWNER,
            executor_id=POLICY_PATCH_EXECUTOR,
            action_type=POLICY_PATCH_FEEDBACK_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=(
                self.options.database_dir / "producer_consumer_ledger.db"
            ),
        )
        before_hash = self._feedback_state_hash(patch_id, feedback_id)
        evidence_payload = {
            **dict(evidence or {}),
            "material_action": {
                "command_id": permit.command_id,
                "decision_revision_id": permit.decision_revision_id,
                "action_id": permit.action_id,
                "effect_id": permit.effect_id,
                "input_hash": binding["input_hash"],
            },
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO policy_patch_feedback (
                    feedback_id, patch_id, outcome, evidence_json,
                    source_event_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_id,
                    patch_id,
                    normalized,
                    _json_dumps(evidence_payload),
                    source_event_id,
                    created_at,
                ),
            )
            if status:
                conn.execute(
                    "UPDATE policy_patches SET status = ? WHERE patch_id = ?",
                    (status, patch_id),
                )
            after_hash = self._feedback_state_hash_conn(
                conn,
                patch_id,
                feedback_id,
            )
            record_target_effect(
                conn,
                permit,
                status="committed",
                before_hash=before_hash,
                after_hash=after_hash,
                evidence_refs=(
                    f"target-after:{after_hash}",
                    f"target-journal:policy-feedback:{feedback_id}:{after_hash}",
                ),
                outcome="policy patch feedback committed",
                observed_at=created_at,
            )
        result = {
            "feedback_id": feedback_id,
            "patch_id": patch_id,
            "outcome": normalized,
            "status": status or self.get_patch(patch_id).get("status", ""),
            "created_at": created_at,
        }
        if not recover_recorded_target_effect(
            material_action,
            PolicyPatchFeedbackEffectOracle(self.options.db_path),
        ):
            raise RuntimeError("policy feedback journal was not recoverable")
        return result

    def _record_no_patch_feedback(
        self,
        *,
        patch_id: str,
        normalized_outcome: str,
        evidence: Mapping[str, Any] | None,
        source_event_id: str,
        created_at: str,
    ) -> dict[str, Any]:
        """Persist the exact no-patch observation that cannot change policy."""

        if normalized_outcome != "no_patch":
            raise PermissionError(
                "only no-patch observations are non-material policy feedback"
            )
        feedback_id = (
            "policy-feedback-"
            + hashlib.sha256(source_event_id.encode("utf-8")).hexdigest()[:24]
            if source_event_id
            else "policy-feedback-"
            + hashlib.sha256(
                _json_dumps(
                    {
                        "patch_id": patch_id,
                        "outcome": normalized_outcome,
                        "evidence": dict(evidence or {}),
                    }
                ).encode("utf-8")
            ).hexdigest()[:24]
        )
        evidence_payload = {
            **dict(evidence or {}),
            "classification": "non_material_no_patch_observation",
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO policy_patch_feedback (
                    feedback_id, patch_id, outcome, evidence_json,
                    source_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_id,
                    patch_id,
                    normalized_outcome,
                    _json_dumps(evidence_payload),
                    source_event_id,
                    created_at,
                ),
            )
        return {
            "feedback_id": feedback_id,
            "patch_id": patch_id,
            "outcome": normalized_outcome,
            "status": "not_applicable",
            "created_at": created_at,
        }

    def _feedback_material_barrier(
        self,
        *,
        patch_id: str,
        outcome: str,
        normalized_outcome: str,
        evidence: Mapping[str, Any] | None,
        source_event_id: str,
        material_action: MaterialActionAuthorization | None,
    ) -> tuple[
        MaterialActionAuthorization,
        MaterialActionPermit,
        dict[str, str],
        bool,
    ] | None:
        """Classify no-patch observations and authorize every patch mutation."""

        if normalized_outcome == "no_patch":
            if material_action is not None:
                raise PermissionError(
                    "no-patch observation cannot consume a material permit"
                )
            return None
        binding = policy_patch_feedback_binding(
            patch_id=patch_id,
            outcome=outcome,
            evidence=evidence,
            source_event_id=source_event_id,
        )
        authorization, permit = resolve_material_action_recovery_authorization(
            material_action,
            owner=POLICY_PATCH_OWNER,
            executor_id=POLICY_PATCH_EXECUTOR,
            action_type=POLICY_PATCH_FEEDBACK_ACTION,
            target_ref=binding["target_ref"],
            input_hash=binding["input_hash"],
            expected_state_db=(
                self.options.database_dir / "producer_consumer_ledger.db"
            ),
        )
        recovered = recover_recorded_target_effect(
            authorization,
            PolicyPatchFeedbackEffectOracle(self.options.db_path),
        )
        if not recovered:
            permit = require_material_action(
                authorization,
                owner=POLICY_PATCH_OWNER,
                executor_id=POLICY_PATCH_EXECUTOR,
                action_type=POLICY_PATCH_FEEDBACK_ACTION,
                target_ref=binding["target_ref"],
                input_hash=binding["input_hash"],
                expected_state_db=(
                    self.options.database_dir / "producer_consumer_ledger.db"
                ),
            )
        return authorization, permit, binding, recovered

    def get_patch(self, patch_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM policy_patches WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
        return _row_to_dict(row) if row else {}

    def _patch_state_hash(self, patch_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM policy_patches WHERE patch_id = ?",
                (patch_id,),
            ).fetchone()
        return sha256_json(dict(row) if row is not None else None)

    def _feedback_state_hash(self, patch_id: str, feedback_id: str) -> str:
        with self._connect() as conn:
            return self._feedback_state_hash_conn(conn, patch_id, feedback_id)

    @staticmethod
    def _feedback_state_hash_conn(
        conn: sqlite3.Connection,
        patch_id: str,
        feedback_id: str,
    ) -> str:
        patch = conn.execute(
            "SELECT * FROM policy_patches WHERE patch_id = ?",
            (patch_id,),
        ).fetchone()
        feedback = conn.execute(
            "SELECT * FROM policy_patch_feedback WHERE feedback_id = ?",
            (feedback_id,),
        ).fetchone()
        return sha256_json(
            {
                "patch": dict(patch) if patch is not None else None,
                "feedback": dict(feedback) if feedback is not None else None,
            }
        )

    def _patch_batch_state_hash(self, patch_ids: list[str]) -> str:
        with self._connect() as conn:
            return self._patch_batch_state_hash_conn(conn, patch_ids)

    @staticmethod
    def _patch_batch_state_hash_conn(
        conn: sqlite3.Connection,
        patch_ids: list[str],
    ) -> str:
        if not patch_ids:
            return sha256_json([])
        placeholders = ",".join("?" for _ in patch_ids)
        rows = conn.execute(
            f"SELECT * FROM policy_patches WHERE patch_id IN ({placeholders}) "
            "ORDER BY patch_id",  # nosec B608 - placeholders are generated, not input.
            tuple(patch_ids),
        ).fetchall()
        return sha256_json([dict(row) for row in rows])

    def _ensure_schema(self) -> None:
        self.options.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            initialize_material_effect_schema(conn)
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS policy_patches (
                    patch_id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    task_type TEXT NOT NULL DEFAULT 'general',
                    subtype TEXT NOT NULL DEFAULT 'general',
                    scope TEXT NOT NULL DEFAULT 'global',
                    severity TEXT NOT NULL DEFAULT 'medium',
                    content TEXT NOT NULL,
                    trigger TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    delivery_mode TEXT NOT NULL DEFAULT 'preflight_guard_only'
                );

                CREATE INDEX IF NOT EXISTS idx_policy_patches_lookup
                ON policy_patches(status, task_type, subtype, expires_at);

                CREATE INDEX IF NOT EXISTS idx_policy_patches_scope_lookup
                ON policy_patches(status, task_type, subtype, scope, expires_at);

                CREATE INDEX IF NOT EXISTS idx_policy_patches_source
                ON policy_patches(source_type, source_id);

                CREATE TABLE IF NOT EXISTS policy_patch_feedback (
                    feedback_id TEXT PRIMARY KEY,
                    patch_id TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    source_event_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_policy_patch_feedback_patch
                ON policy_patch_feedback(patch_id);
                """
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(policy_patch_feedback)")
            }
            if "source_event_id" not in columns:
                conn.execute(
                    "ALTER TABLE policy_patch_feedback ADD COLUMN source_event_id TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_policy_patch_feedback_source_event
                ON policy_patch_feedback(source_event_id) WHERE source_event_id <> ''
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.options.db_path)
        conn.row_factory = sqlite3.Row
        return conn


def _policy_patch_dedupe_key(
    patch: PolicyPatch, matched_triggers: list[str]
) -> str:
    """Group paraphrases by the trigger set matched in this task context."""

    terms = sorted(
        {term.strip().lower() for term in matched_triggers if term.strip()}
    )
    payload = _json_dumps([patch.task_type, patch.subtype, patch.scope, terms])
    return "policy-dedupe-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _task_fit_score(
    patch: PolicyPatch,
    *,
    task_type: str,
    subtype: str,
) -> float:
    """Explain task fit from explicit scope plus a proven context trigger."""

    score = 0.7
    if patch.task_type == task_type and patch.task_type not in {"", "general"}:
        score += 0.2
    if patch.subtype == subtype and patch.subtype not in {"", "general"}:
        score += 0.1
    return round(min(1.0, score), 3)


def _row_to_patch(row: sqlite3.Row) -> PolicyPatch:
    data = _row_to_dict(row)
    return PolicyPatch(
        patch_id=data["patch_id"],
        source_type=data["source_type"],
        source_id=data["source_id"],
        task_type=data["task_type"],
        subtype=data["subtype"],
        scope=data["scope"],
        severity=data["severity"],
        content=data["content"],
        trigger=data["trigger"],
        confidence=float(data["confidence"]),
        evidence_refs=list(data.get("evidence_refs") or []),
        status=data["status"],
        created_at=data["created_at"],
        expires_at=data["expires_at"],
        metadata=dict(data.get("metadata") or {}),
        delivery_mode=data.get("delivery_mode") or "preflight_guard_only",
    )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("evidence_refs_json", "metadata_json", "evidence_json"):
        if key not in data:
            continue
        out_key = key.removesuffix("_json")
        try:
            data[out_key] = json.loads(data.pop(key) or "null")
        except json.JSONDecodeError:
            data[out_key] = [] if out_key == "evidence_refs" else {}
    return data


def build_policy_feedback_proposal_owner(database_dir: Path):
    """Return the policy-owned pending-review journal for feedback commands."""

    from core.cognitive.feedback_target_registry import (
        build_registered_feedback_proposal_owner,
    )

    return build_registered_feedback_proposal_owner(database_dir, "policy_proposal")
