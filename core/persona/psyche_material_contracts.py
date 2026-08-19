"""Exact Persona material-action contracts and recovery oracles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from core.cognitive.decision_trace import (
    MaterialActionAuthorization,
    MaterialActionRequest,
    authorize_exact_project_contract_action,
)
from core.cognitive.material_effect_ledger import SqliteTargetEffectOracle
from core.cognitive.state_contract import sha256_json

PERSONA_VERSION_ACTION = "save_persona_version"
PERSONA_VERSION_OWNER = "persona"
PERSONA_VERSION_EXECUTOR = "signal_store"
PERSONA_BLINDSPOT_ACTION = "update_persona_blindspot"
PERSONA_CALIBRATION_ACTION = "calibrate_persona"
PERSONA_BLINDSPOT_REVOKE_ACTION = "revoke_persona_blindspot"
PERSONA_CALIBRATION_REVOKE_ACTION = "revoke_persona_calibration"
PERSONA_DECISION_CONTRACT_ID = "project-contract:persona-material-actions"
PERSONA_DECISION_CONTRACT_REVISION = "mnemos.persona_material_actions.v2"
PERSONA_DECISION_CONTRACT_TEXT = (
    "The Persona domain may persist only the exact profile version, blindspot "
    "state, or explicit calibration accepted by its current validated workflow."
)
PERSONA_DECISION_PRODUCER_HASH = sha256_json(
    {
        "module": "core.persona.psyche",
        "producer": "persona-domain-decision-producer",
        "version": PERSONA_DECISION_CONTRACT_REVISION,
    }
)

_PERSONA_CURSOR_SOURCES = frozenset(
    {"session", "git", "knowledge", "wechat", "file_system"}
)


def canonical_persona_signal_cursor(
    source_signal_ids: Mapping[str, Sequence[int]] | None,
) -> dict[str, list[int]]:
    """Return the exact, stable signal cursor bound to one Persona command.

    A candidate cannot claim success for an arbitrary batch and consume a
    different batch at commit time.  The command payload therefore contains a
    sorted, duplicate-free cursor limited to the sources Pythia can derive.
    """

    if not source_signal_ids:
        return {}
    cursor: dict[str, list[int]] = {}
    for source_type, raw_ids in source_signal_ids.items():
        if source_type not in _PERSONA_CURSOR_SOURCES:
            raise ValueError(f"unsupported Persona signal cursor source: {source_type}")
        if isinstance(raw_ids, (str, bytes)):
            raise ValueError("Persona signal cursor ids must be integer sequences")
        try:
            normalized = sorted({int(signal_id) for signal_id in raw_ids})
        except (TypeError, ValueError) as exc:
            raise ValueError("Persona signal cursor ids must be integers") from exc
        if any(signal_id <= 0 for signal_id in normalized):
            raise ValueError("Persona signal cursor ids must be positive")
        if normalized:
            cursor[str(source_type)] = normalized
    return {source_type: cursor[source_type] for source_type in sorted(cursor)}


class PersonaVersionEffectOracle(SqliteTargetEffectOracle):
    """Observe one committed persona-version mutation."""

    owner = PERSONA_VERSION_OWNER
    executor_id = PERSONA_VERSION_EXECUTOR
    action_type = PERSONA_VERSION_ACTION


class PersonaBlindspotEffectOracle(SqliteTargetEffectOracle):
    """Observe one committed persona-blindspot mutation."""

    owner = PERSONA_VERSION_OWNER
    executor_id = PERSONA_VERSION_EXECUTOR
    action_type = PERSONA_BLINDSPOT_ACTION


class PersonaCalibrationEffectOracle(SqliteTargetEffectOracle):
    """Observe one committed persona-calibration mutation."""

    owner = PERSONA_VERSION_OWNER
    executor_id = PERSONA_VERSION_EXECUTOR
    action_type = PERSONA_CALIBRATION_ACTION


class PersonaBlindspotRevokeEffectOracle(SqliteTargetEffectOracle):
    """Observe one committed append-only Persona blindspot revocation."""

    owner = PERSONA_VERSION_OWNER
    executor_id = PERSONA_VERSION_EXECUTOR
    action_type = PERSONA_BLINDSPOT_REVOKE_ACTION


class PersonaCalibrationRevokeEffectOracle(SqliteTargetEffectOracle):
    """Observe one committed append-only Persona calibration revocation."""

    owner = PERSONA_VERSION_OWNER
    executor_id = PERSONA_VERSION_EXECUTOR
    action_type = PERSONA_CALIBRATION_REVOKE_ACTION


def authorize_exact_persona_material_action(
    *,
    expected_request: MaterialActionRequest,
    state_db_path: Path,
    source_namespace: str,
    source_facts: Dict[str, Any],
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
    """Seal one exact Persona-domain decision before its material effect."""

    return authorize_exact_project_contract_action(
        expected_request=expected_request,
        state_db_path=state_db_path,
        contract_id=PERSONA_DECISION_CONTRACT_ID,
        contract_revision_id=PERSONA_DECISION_CONTRACT_REVISION,
        contract_text=PERSONA_DECISION_CONTRACT_TEXT,
        source_namespace=source_namespace,
        source_facts=source_facts,
        decision_checks={
            "registered_persona_action": expected_request.action_type
            in {
                PERSONA_VERSION_ACTION,
                PERSONA_BLINDSPOT_ACTION,
                PERSONA_CALIBRATION_ACTION,
                PERSONA_BLINDSPOT_REVOKE_ACTION,
                PERSONA_CALIBRATION_REVOKE_ACTION,
            },
            "persona_source_facts_present": bool(source_facts),
            "persona_evidence_present": bool(evidence_refs),
        },
        evidence_refs=evidence_refs,
        task=task,
        goal=goal,
        constraints=constraints,
        created_at=created_at,
        producer=producer,
        producer_version=PERSONA_DECISION_CONTRACT_REVISION,
        producer_code_hash=PERSONA_DECISION_PRODUCER_HASH,
        evaluator_id=evaluator_id,
        approved_candidate_key=approved_candidate_key,
        approved_candidate_summary=approved_candidate_summary,
        rejected_candidate_key=rejected_candidate_key,
        rejected_candidate_summary=rejected_candidate_summary,
        approved_reason_code="persona_exact_material_action_verified",
        rejected_reason_code="persona_material_action_rejected",
        committed_metric=committed_metric,
        rejected_metric=rejected_metric,
    )


def persona_version_material_action_binding(
    *,
    version: int,
    generated_at: str,
    period_start: str,
    period_end: str,
    energy: Dict,
    cognitive: Dict,
    value: Dict,
    blindspot: Dict,
    signal_count: int,
    user_confirmed: bool = False,
    confirmed_at: str = "",
    calibration_score: float | None = None,
    supersedes_revision_id: str = "",
    source_signal_ids: Mapping[str, Sequence[int]] | None = None,
    revision_metadata: Mapping[str, Any] | None = None,
    action_type: str = PERSONA_VERSION_ACTION,
    actor: str = "system",
    reason: str = "signal_store.save_persona_version",
) -> dict[str, Any]:
    """Bind a persona version to its complete derived profile payload."""

    canonical_cursor = canonical_persona_signal_cursor(source_signal_ids)
    try:
        canonical_metadata = json.loads(
            json.dumps(dict(revision_metadata or {}), ensure_ascii=False, sort_keys=True)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Persona revision metadata must be JSON-serializable") from exc
    payload = {
        "schema_version": "mnemos.persona_version_input.v3",
        "version": int(version),
        "generated_at": str(generated_at),
        "period_start": str(period_start),
        "period_end": str(period_end),
        "energy": dict(energy),
        "cognitive": dict(cognitive),
        "value": dict(value),
        "blindspot": dict(blindspot),
        "signal_count": int(signal_count),
        "user_confirmed": bool(user_confirmed),
        "confirmed_at": str(confirmed_at),
        "calibration_score": calibration_score,
        "supersedes_revision_id": str(supersedes_revision_id),
        "source_signal_ids": canonical_cursor,
        "revision_metadata": canonical_metadata,
    }
    payload_hash = sha256_json(payload)
    content_hash = sha256_json(
        {
            "schema_version": "mnemos.persona_revision_content.v3",
            "energy": dict(energy),
            "cognitive": dict(cognitive),
            "value": dict(value),
            "blindspot": dict(blindspot),
            "user_confirmed": bool(user_confirmed),
            "confirmed_at": str(confirmed_at),
            "calibration_score": calibration_score,
            "semantic_action": (
                action_type if action_type != PERSONA_VERSION_ACTION else ""
            ),
            "revision_metadata": (
                canonical_metadata if action_type != PERSONA_VERSION_ACTION else {}
            ),
        }
    )
    target_ref = f"persona-version:{int(version)}:" f"{payload_hash.split(':', 1)[1][:24]}"
    from core.trust.formal_cognitive_mutation import (
        formal_cognitive_mutation_input_hash,
    )

    input_hash = formal_cognitive_mutation_input_hash(
        asset_kind="persona_profile",
        action=action_type,
        target_ref=target_ref,
        actor=actor,
        reason=reason,
        metadata=payload,
    )
    return {
        "target_ref": target_ref,
        "input_hash": input_hash,
        "payload": payload,
        "payload_hash": payload_hash,
        "content_hash": content_hash,
    }
