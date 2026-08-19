"""Stable DecisionTrace types, constants, and validation primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from core.cognitive.state_contract import (
    CognitiveStateRevision,
    canonical_json,
    sha256_json,
)
from core.cognitive.prediction_ledger import PredictionPlan

MATERIAL_ACTION_COMMAND_TYPE = "execute_material_action"
DECISION_RECEIPT_SCHEMA_VERSION = "mnemos.decision_receipt.v1"
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_VALUE_AUTHORITIES = frozenset({"system_policy", "explicit_user", "project_contract"})
_AUTHORITY_BY_CATEGORY = {
    "safety_permission_privacy": _VALUE_AUTHORITIES,
    "explicit_user_goal": frozenset({"explicit_user"}),
    "project_constraint": frozenset({"project_contract", "system_policy"}),
    "scoped_preference": frozenset({"explicit_user", "project_contract"}),
    "cost_convenience": _VALUE_AUTHORITIES,
}


@dataclass(frozen=True)
class DecisionSealReceipt:
    """Proof that decision state and local action commands committed together."""

    status: str
    event_id: str
    transaction_hash: str
    revision_ids: tuple[str, ...]
    command_ids: tuple[str, ...]
    value_context: CognitiveStateRevision
    snapshot: CognitiveStateRevision
    decision: CognitiveStateRevision
    predictions: tuple[CognitiveStateRevision, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical machine-readable decision-seal receipt."""

        return {
            "success": True,
            "schema_version": DECISION_RECEIPT_SCHEMA_VERSION,
            "status": self.status,
            "event_id": self.event_id,
            "transaction_hash": self.transaction_hash,
            "revision_ids": list(self.revision_ids),
            "outbox_ids": list(self.command_ids),
            "value_context": _revision_dict(self.value_context),
            "snapshot": _revision_dict(self.snapshot),
            "decision": _revision_dict(self.decision),
            "predictions": [_revision_dict(value) for value in self.predictions],
        }


@dataclass(frozen=True)
class DecisionVerification:
    """Independent readable proof for one committed decision bundle."""

    status: str
    decision_revision_id: str
    snapshot_revision_id: str
    value_context_revision_id: str
    action_ids: tuple[str, ...]
    effect_ids: tuple[str, ...]
    bundle_hash: str
    prediction_revision_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class MaterialActionPermit:
    """Immutable capability bound to one committed material-action command."""

    schema_version: str
    command_id: str
    decision_revision_id: str
    action_id: str
    effect_id: str
    action_type: str
    owner: str
    executor_id: str
    target_ref: str
    target_hash: str
    input_hash: str
    issued_at: str
    integrity_hash: str
    prediction_refs: tuple[Mapping[str, str], ...] = ()


@dataclass(frozen=True)
class MaterialActionTerminal:
    """Caller-observed terminal state for one permitted target effect."""

    status: str
    target_effect_id: str
    before_hash: str
    after_hash: str
    evidence_refs: tuple[str, ...]
    reason_code: str = ""
    retry_exhausted: bool = False
    outcome: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class MaterialActionReceipt:
    """Reciprocal proof linking one decision action to its target effect."""

    receipt_id: str
    command_id: str
    decision_revision_id: str
    action_id: str
    effect_id: str
    status: str
    before_hash: str
    after_hash: str
    evidence_refs: tuple[str, ...]
    reason_code: str
    retry_exhausted: bool
    created_at: str
    prediction_refs: tuple[Mapping[str, str], ...] = ()


@dataclass(frozen=True)
class MaterialActionObservation:
    """Read-only target observation used to close a crash-window command."""

    status: str
    before_hash: str
    after_hash: str
    evidence_refs: tuple[str, ...]
    reason_code: str = ""
    retry_exhausted: bool = False
    outcome: str = ""
    observed_at: str = ""


@runtime_checkable
class MaterialEffectOracle(Protocol):
    """Code-owned, read-only oracle for one exact material target family."""

    owner: str
    executor_id: str
    action_type: str

    def observe(
        self,
        permit: MaterialActionPermit,
    ) -> MaterialActionObservation | None:
        """Return the already committed effect, or ``None`` when absent."""


@dataclass(frozen=True)
class MaterialActionRequest:
    """Exact effect binding requested from an upstream authorization scope."""

    owner: str
    executor_id: str
    action_type: str
    target_ref: str
    input_hash: str
    expected_state_db: str = ""


@dataclass(frozen=True)
class ProjectContractDecisionContext:
    """Code-owned source facts and evaluator for one material-action family.

    The context does not authorize an effect by itself.  It gives an upstream
    producer the exact immutable project contract, trigger identity, and
    domain evaluator needed to compare real candidates before a sink runs.
    """

    state_db_path: Path
    contract_id: str
    contract_revision_id: str
    contract_text: str
    contract_evidence_ref: str
    source_id: str
    source_revision_id: str
    source_content_hash: str
    source_uri: str
    evidence_refs: tuple[str, ...]
    task: str
    goal: str
    constraints: tuple[str, ...]
    created_at: str
    scope_prefix: str
    producer: str
    producer_version: str
    producer_code_hash: str
    evaluator_id: str
    evaluator: Callable[[MaterialActionRequest], "ProjectContractDecisionEvaluation"]
    prediction_plan: PredictionPlan | None = None
    prediction_config: Any | None = None


@dataclass(frozen=True)
class DecisionCandidateEvaluation:
    """One domain candidate that was actually evaluated before an effect."""

    key: str
    summary: str
    supporting_evidence: tuple[str, ...]
    opposing_evidence: tuple[str, ...]
    violated_value_keys: tuple[str, ...] = ()
    satisfies_value_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class DecisionRejectionEvaluation:
    """Structured rejection of one non-selected evaluated candidate."""

    candidate_key: str
    reason_code: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ProjectContractDecisionEvaluation:
    """Domain-owned result of comparing exact candidates for one request."""

    request_binding_hash: str
    source_facts_hash: str
    candidates: tuple[DecisionCandidateEvaluation, ...]
    selection_key: str
    rejections: tuple[DecisionRejectionEvaluation, ...]
    expected_outcomes: tuple[Mapping[str, Any], ...]
    approval_decision: str
    approval_evidence_ref: str


def _contains_private_reasoning(value: Any) -> bool:
    prohibited = {
        "chain_of_thought",
        "scratchpad",
        "private_reasoning",
        "hidden_reasoning",
        "reasoning_trace",
    }
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in prohibited or _contains_private_reasoning(child)
            for key, child in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_private_reasoning(child) for child in value)
    return False


def _required_dead_letter_supersessions(
    state_db_path: Path,
    actions: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return latest exact action decisions whose dead letters block a retry."""

    if not actions:
        return ()
    database = Path(state_db_path).expanduser().resolve(strict=False)
    if not database.is_file():
        return ()
    with sqlite3.connect(
        f"file:{database}?mode=ro",
        uri=True,
    ) as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if not {
            "cognitive_state_outbox",
            "cognitive_state_effect_receipts",
        }.issubset(tables):
            return ()
        superseded: set[str] = set()
        for action in actions:
            row = conn.execute(
                """
                SELECT o.revision_id, r.status
                FROM cognitive_state_outbox AS o
                JOIN cognitive_state_effect_receipts AS r
                  ON r.command_id=o.command_id
                WHERE o.command_type=?
                  AND json_extract(o.payload_json, '$.owner')=?
                  AND json_extract(o.payload_json, '$.executor')=?
                  AND json_extract(o.payload_json, '$.action_type')=?
                  AND json_extract(o.payload_json, '$.target_ref')=?
                  AND json_extract(o.payload_json, '$.input_hash')=?
                ORDER BY r.created_at DESC, r.receipt_id DESC
                LIMIT 1
                """,
                (
                    MATERIAL_ACTION_COMMAND_TYPE,
                    str(action.get("owner") or ""),
                    str(action.get("executor") or ""),
                    str(action.get("action_type") or ""),
                    str(action.get("target_ref") or ""),
                    str(action.get("input_hash") or ""),
                ),
            ).fetchone()
            if row is not None and str(row[1]) == "dead_letter":
                superseded.add(str(row[0]))
    return tuple(sorted(superseded))


def _raise_unavailable_revision(label: str, reason: str) -> None:
    if reason in {"not_found", "revision_id_required"}:
        raise RuntimeError(f"{label} revision is unavailable")
    raise PermissionError(f"{label} revision access denied: {reason}")


def _revision_dict(revision: CognitiveStateRevision) -> dict[str, Any]:
    return {
        "revision_id": revision.revision_id,
        "object_type": revision.object_type,
        "object_id": revision.object_id,
        "schema_version": revision.schema_version,
        "payload": dict(revision.payload),
        "payload_hash": revision.payload_hash,
        "evidence_refs": list(revision.evidence_refs),
        "evidence_hash": revision.evidence_hash,
        "source_event_id": revision.source_event_id,
        "source_revision_id": revision.source_revision_id,
        "source_content_hash": revision.source_content_hash,
        "scope_type": revision.scope_type,
        "scope_id": revision.scope_id,
        "supersedes_revision_id": revision.supersedes_revision_id,
        "correction_of_revision_id": revision.correction_of_revision_id,
        "admission_state": revision.admission_state,
        "redaction_policy": revision.redaction_policy,
        "redaction_counts": dict(revision.redaction_counts),
        "created_at": revision.created_at,
        "canonical_payload_hash": sha256_json(dict(revision.payload)),
        "canonical_payload": canonical_json(revision.payload),
    }


def _revision_from_row(row: Any) -> CognitiveStateRevision:
    return CognitiveStateRevision(
        revision_id=str(row["revision_id"]),
        object_type=str(row["object_type"]),
        object_id=str(row["object_id"]),
        schema_version=str(row["schema_version"]),
        source_event_id=str(row["source_event_id"]),
        source_revision_id=str(row["source_revision_id"]),
        source_content_hash=str(row["source_content_hash"]),
        scope_type=str(row["scope_type"]),
        scope_id=str(row["scope_id"]),
        evidence_refs=tuple(json.loads(str(row["evidence_refs"]))),
        payload=json.loads(str(row["payload_json"])),
        payload_hash=str(row["payload_hash"]),
        evidence_hash=str(row["evidence_hash"]),
        supersedes_revision_id=str(row["supersedes_revision_id"] or ""),
        correction_of_revision_id=str(row["correction_of_revision_id"] or ""),
        created_at=str(row["created_at"]),
        admission_state=str(row["admission_state"]),
        redaction_policy=str(row["redaction_policy"]),
        redaction_counts=tuple(
            sorted(
                (str(key), int(value))
                for key, value in json.loads(str(row["redaction_counts"])).items()
            )
        ),
    )


def _evaluation_window(value: Any) -> dict[str, str]:
    row = _mapping(value, "evaluation_window")
    starts_at = _timestamp(row.get("starts_at"), "evaluation_window.starts_at")
    ends_at = _timestamp(row.get("ends_at"), "evaluation_window.ends_at")
    if datetime.fromisoformat(ends_at) <= datetime.fromisoformat(starts_at):
        raise ValueError("evaluation window must end after it starts")
    return {"starts_at": starts_at, "ends_at": ends_at}


def _tool_specs(value: Any) -> tuple[dict[str, Any], ...]:
    rows = _mapping_sequence(value, "tool_specs")
    result = [
        _spec_mapping(
            row,
            "tool_specs",
            required=("name", "version", "code_hash"),
            hash_fields=("code_hash",),
        )
        for row in rows
    ]
    names = [str(row["name"]) for row in result]
    if len(names) != len(set(names)):
        raise ValueError("tool specs must be unique by name")
    return tuple(sorted(result, key=lambda row: str(row["name"])))


def _spec_mapping(
    value: Any,
    field_name: str,
    *,
    required: Sequence[str],
    hash_fields: Sequence[str],
) -> dict[str, Any]:
    row = _mapping(value, field_name)
    result = {key: _required(row.get(key), f"{field_name}.{key}") for key in required}
    for key in hash_fields:
        _sha256(result[key], f"{field_name}.{key}")
    return result


def _mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return dict(value)


def _mapping_sequence(
    value: Any,
    field_name: str,
    *,
    non_empty: bool = False,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field_name} must be a sequence")
    result = tuple(_mapping(item, field_name) for item in value)
    if non_empty and not result:
        raise ValueError(f"{field_name} must be non-empty")
    return result


def _strings(
    value: Any,
    field_name: str,
    *,
    non_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field_name} must be a sequence")
    result = tuple(_required(item, field_name) for item in value)
    if non_empty and not result:
        raise ValueError(f"{field_name} must be non-empty")
    if len(result) != len(set(result)):
        raise ValueError(f"{field_name} must be unique")
    return result


def _required(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _text_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256(value: Any, field_name: str) -> str:
    normalized = _required(value, field_name)
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be an exact SHA-256 identity")
    return normalized


def _timestamp(value: Any, field_name: str) -> str:
    normalized = _required(value, field_name)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.isoformat()


def _digest(value: Any) -> str:
    return str(sha256_json(value)).split(":", 1)[1]
