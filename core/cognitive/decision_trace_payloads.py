"""Decision input normalization and canonical payload construction."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from core.cognitive.access_control import cognitive_access_hash
from core.cognitive.decision_snapshot_access import AuthorizedDecisionSnapshotSource
from core.cognitive.state_contract import (
    COGNITIVE_OBJECT_SCHEMA_VERSIONS,
    FIXED_VALUE_PRECEDENCE,
    VALUE_PRECEDENCE_CONTRACT,
    CognitiveStateRevision,
    LocalConsumerCommand,
    sha256_json,
)
from core.cognitive.prediction_ledger import PredictionPlan
from core.evidence.source_authority import SourceAuthorityCatalog

from core.cognitive.decision_trace_contracts import (
    MATERIAL_ACTION_COMMAND_TYPE,
    _AUTHORITY_BY_CATEGORY,
    _digest,
    _evaluation_window,
    _mapping,
    _mapping_sequence,
    _required,
    _sha256,
    _spec_mapping,
    _strings,
    _timestamp,
    _tool_specs,
)


def _normalize_command(
    command: Mapping[str, Any],
    *,
    source_authority_catalog: SourceAuthorityCatalog,
) -> dict[str, Any]:
    if not isinstance(command, Mapping):
        raise ValueError("decision command must be an object")
    forbidden = {
        "decision_id",
        "snapshot_id",
        "snapshot_hash",
        "value_context_id",
        "access_control",
        "effect_refs",
        "action_refs",
    }
    supplied = forbidden.intersection(command)
    if supplied:
        raise ValueError(
            "decision, snapshot, value, action, and effect identities are server-owned: "
            + ", ".join(sorted(supplied))
        )
    source_raw = command.get("source")
    if not isinstance(source_raw, Mapping):
        raise ValueError("source must be an object")
    source = dict(source_raw)
    for key in (
        "source_id",
        "source_revision_id",
        "source_kind",
        "source_uri",
        "content_hash",
        "created_at",
    ):
        source[key] = _required(source.get(key), f"source.{key}")
    _sha256(source["content_hash"], "source.content_hash")
    source["evidence_refs"] = _strings(
        source.get("evidence_refs"),
        "source.evidence_refs",
        non_empty=True,
    )
    if not isinstance(source.get("access_control"), Mapping):
        raise ValueError("source.access_control must be an object")
    source["confidence"] = float(source.get("confidence", 1.0))
    if not 0.0 <= source["confidence"] <= 1.0:
        raise ValueError("source.confidence must be between 0 and 1")
    scope = command.get("scope")
    if not isinstance(scope, Mapping):
        raise ValueError("scope must be an object")
    values = _normalize_values(
        command.get("values"),
        source_authority_catalog=source_authority_catalog,
    )
    candidates = _normalize_candidates(command.get("candidates"), values=values)
    selection_key = _required(command.get("selection_key"), "selection_key")
    by_candidate_key = {value["key"]: value for value in candidates}
    selected = by_candidate_key.get(selection_key)
    if selected is None:
        raise ValueError("selection_key does not name a candidate")
    if selected["violated_value_keys"]:
        raise ValueError("selected candidate violates a hard value constraint")
    _enforce_value_precedence(
        values=values,
        candidates=candidates,
        selected=selected,
    )
    rejections = _normalize_rejections(
        command.get("rejections"),
        candidates=candidates,
        selection_key=selection_key,
    )
    approval = _mapping(command.get("approval"), "approval")
    approval_decision = _required(approval.get("decision"), "approval.decision")
    if approval_decision not in {"approved", "rejected"}:
        raise ValueError("approval.decision must be approved or rejected")
    normalized_approval = {
        "mode": _required(approval.get("mode"), "approval.mode"),
        "decision": approval_decision,
        "evidence_ref": _required(
            approval.get("evidence_ref"),
            "approval.evidence_ref",
        ),
        "created_at": _timestamp(approval.get("created_at"), "approval.created_at"),
    }
    actions = _normalize_actions(
        command.get("actions"),
        allow_empty=approval_decision == "rejected",
    )
    if approval_decision == "rejected" and actions:
        raise ValueError("a rejected decision cannot carry executable actions")
    return {
        "idempotency_key": _required(
            command.get("idempotency_key"),
            "idempotency_key",
        ),
        "source": source,
        "scope_type": _required(scope.get("type"), "scope.type"),
        "scope_id": _required(scope.get("id"), "scope.id"),
        "task": _required(command.get("task"), "task"),
        "goal": _required(command.get("goal"), "goal"),
        "constraints": _strings(
            command.get("constraints"),
            "constraints",
            non_empty=True,
        ),
        "values": values,
        "source_authority_catalog_hash": source_authority_catalog.catalog_hash,
        "source_authority_catalog": source_authority_catalog.canonical_payload(),
        "candidates": candidates,
        "selection_key": selection_key,
        "rejections": rejections,
        "model_spec": _spec_mapping(
            command.get("model_spec"),
            "model_spec",
            required=("provider", "model", "route", "version", "config_hash"),
            hash_fields=("config_hash",),
        ),
        "tool_specs": _tool_specs(command.get("tool_specs")),
        "prompt_spec": _spec_mapping(
            command.get("prompt_spec"),
            "prompt_spec",
            required=("prompt_id", "prompt_hash", "schema_hash"),
            hash_fields=("prompt_hash", "schema_hash"),
        ),
        "expected_outcomes": _mapping_sequence(
            command.get("expected_outcomes"),
            "expected_outcomes",
            non_empty=True,
        ),
        "evaluation_window": _evaluation_window(command.get("evaluation_window")),
        "approval": normalized_approval,
        "supersedes_decision_revision_ids": tuple(
            sorted(
                set(
                    _strings(
                        command.get("supersedes_decision_revision_ids", ()),
                        "supersedes_decision_revision_ids",
                    )
                )
            )
        ),
        "actions": actions,
        "created_at": _timestamp(source["created_at"], "source.created_at"),
    }


def _normalize_values(
    value: Any,
    *,
    source_authority_catalog: SourceAuthorityCatalog,
) -> tuple[dict[str, Any], ...]:
    rows = _mapping_sequence(value, "values", non_empty=True)
    normalized: list[dict[str, Any]] = []
    keys: set[str] = set()
    for row in rows:
        key = _required(row.get("key"), "values.key")
        if key in keys:
            raise ValueError("value keys must be unique")
        keys.add(key)
        category = _required(row.get("category"), "values.category")
        if category not in FIXED_VALUE_PRECEDENCE:
            raise ValueError("value category is outside the fixed precedence contract")
        authority_id = _required(
            row.get("source_authority_id"),
            "values.source_authority_id",
        )
        authority_entry = source_authority_catalog.get(authority_id)
        if authority_entry is None:
            raise ValueError("value source authority is absent from the immutable catalog")
        authority = authority_entry.authority.value
        if not authority_entry.allows_cognitive_update:
            raise ValueError("value source authority cannot authorize cognition")
        if authority not in _AUTHORITY_BY_CATEGORY[category]:
            raise ValueError("value source authority cannot authorize this category")
        source_hash = _sha256(
            row.get("source_content_hash"),
            "values.source_content_hash",
        )
        item: dict[str, Any] = {
            "key": key,
            "category": category,
            "constraint": _required(row.get("constraint"), "values.constraint"),
            "source_authority_id": authority_id,
            "source_authority": authority,
            "source_id": _required(row.get("source_id"), "values.source_id"),
            "source_revision_id": _required(
                row.get("source_revision_id"),
                "values.source_revision_id",
            ),
            "source_content_hash": source_hash,
            "evidence_refs": list(
                _strings(
                    row.get("evidence_refs"),
                    "values.evidence_refs",
                    non_empty=True,
                )
            ),
            "valid_from": _timestamp(row.get("valid_from"), "values.valid_from"),
            "valid_until": str(row.get("valid_until") or ""),
            "changed_decision": bool(row.get("changed_decision", False)),
            "conflicts_with_keys": list(
                sorted(
                    _strings(
                        row.get("conflicts_with_keys", ()),
                        "values.conflicts_with_keys",
                    )
                )
            ),
            "disposition": "active",
        }
        if (
            item["source_id"] != authority_entry.source_event_id
            or item["source_revision_id"] != authority_entry.source_event_id
        ):
            raise ValueError("value source revision does not match its authority catalog entry")
        if item["source_content_hash"] != authority_entry.content_sha256:
            raise ValueError("value content hash does not match its authority span")
        if authority_id not in set(item["evidence_refs"]):
            raise ValueError("value evidence does not bind its source authority entry")
        if item["valid_until"]:
            _timestamp(item["valid_until"], "values.valid_until")
        normalized.append(item)
    for item in normalized:
        conflicts = set(item["conflicts_with_keys"])
        if item["key"] in conflicts:
            raise ValueError("a value cannot conflict with itself")
        if conflicts - keys:
            raise ValueError("value conflict references an unknown value key")
        item["item_ref"] = "value-" + _digest(item)[:32]
    _value_conflict_pairs(normalized)
    return tuple(
        sorted(
            normalized,
            key=lambda item: (FIXED_VALUE_PRECEDENCE.index(item["category"]), item["key"]),
        )
    )


def _normalize_candidates(
    value: Any,
    *,
    values: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    rows = _mapping_sequence(value, "candidates", non_empty=True)
    if len(rows) < 2:
        raise ValueError("an approved material decision requires at least two candidates")
    value_by_key = {str(item["key"]): item for item in values}
    normalized: list[dict[str, Any]] = []
    keys: set[str] = set()
    for row in rows:
        key = _required(row.get("key"), "candidates.key")
        if key in keys:
            raise ValueError("candidate keys must be unique")
        keys.add(key)
        violated_keys = _strings(
            row.get("violated_value_keys", ()),
            "candidates.violated_value_keys",
        )
        unknown = set(violated_keys) - set(value_by_key)
        if unknown:
            raise ValueError("candidate references an unknown value key")
        if any(
            value_by_key[key]["category"] != "safety_permission_privacy" for key in violated_keys
        ):
            raise ValueError("only hard constraints may be declared as violations")
        satisfied_keys = tuple(
            sorted(
                _strings(
                    row.get("satisfies_value_keys", ()),
                    "candidates.satisfies_value_keys",
                )
            )
        )
        if set(satisfied_keys) - set(value_by_key):
            raise ValueError("candidate satisfies an unknown value key")
        item: dict[str, Any] = {
            "key": key,
            "summary": _required(row.get("summary"), "candidates.summary"),
            "supporting_evidence": list(
                _strings(row.get("supporting_evidence", ()), "supporting_evidence")
            ),
            "opposing_evidence": list(
                _strings(row.get("opposing_evidence", ()), "opposing_evidence")
            ),
            "violated_value_keys": list(violated_keys),
            "violated_value_refs": [value_by_key[key]["item_ref"] for key in violated_keys],
            "satisfies_value_keys": list(satisfied_keys),
            "satisfies_value_refs": [value_by_key[key]["item_ref"] for key in satisfied_keys],
        }
        if not item["supporting_evidence"] and not item["opposing_evidence"]:
            raise ValueError("each candidate requires supporting or opposing evidence")
        item["candidate_id"] = "candidate-" + _digest(item)[:32]
        normalized.append(item)
    return tuple(sorted(normalized, key=lambda item: item["key"]))


def _value_conflict_pairs(
    values: Sequence[Mapping[str, Any]],
) -> tuple[tuple[Mapping[str, Any], Mapping[str, Any]], ...]:
    by_key = {str(item["key"]): item for item in values}
    seen: set[tuple[str, str]] = set()
    result: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for item in values:
        item_key = str(item["key"])
        for other_key in item.get("conflicts_with_keys", ()):
            left_key, right_key = sorted((item_key, str(other_key)))
            pair_key = (left_key, right_key)
            if pair_key in seen:
                continue
            seen.add(pair_key)
            left = by_key[pair_key[0]]
            right = by_key[pair_key[1]]
            left_rank = FIXED_VALUE_PRECEDENCE.index(str(left["category"]))
            right_rank = FIXED_VALUE_PRECEDENCE.index(str(right["category"]))
            if left_rank == right_rank:
                raise ValueError("conflicting values at the same precedence are unresolved")
            higher, lower = (left, right) if left_rank < right_rank else (right, left)
            result.append((higher, lower))
    return tuple(
        sorted(
            result,
            key=lambda pair: (
                FIXED_VALUE_PRECEDENCE.index(str(pair[0]["category"])),
                str(pair[0]["key"]),
                str(pair[1]["key"]),
            ),
        )
    )


def _enforce_value_precedence(
    *,
    values: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    selected: Mapping[str, Any],
) -> None:
    selected_satisfied = set(selected["satisfies_value_keys"])
    eligible = tuple(candidate for candidate in candidates if not candidate["violated_value_keys"])
    for higher, lower in _value_conflict_pairs(values):
        higher_key = str(higher["key"])
        lower_key = str(lower["key"])
        if lower_key not in selected_satisfied or higher_key in selected_satisfied:
            continue
        higher_candidate_exists = any(
            higher_key in set(candidate["satisfies_value_keys"]) for candidate in eligible
        )
        if higher_candidate_exists:
            raise ValueError(
                "selected candidate lets a lower-precedence value override "
                "a higher-precedence value"
            )


def _normalize_rejections(
    value: Any,
    *,
    candidates: Sequence[Mapping[str, Any]],
    selection_key: str,
) -> tuple[dict[str, Any], ...]:
    rows = _mapping_sequence(value, "rejections")
    candidate_keys = {str(item["key"]) for item in candidates}
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = _required(row.get("candidate_key"), "rejections.candidate_key")
        if key not in candidate_keys or key == selection_key or key in seen:
            raise ValueError("rejection candidate binding is invalid")
        seen.add(key)
        normalized.append(
            {
                "candidate_key": key,
                "reason_code": _required(
                    row.get("reason_code"),
                    "rejections.reason_code",
                ),
                "evidence_refs": list(
                    _strings(
                        row.get("evidence_refs"),
                        "rejections.evidence_refs",
                        non_empty=True,
                    )
                ),
            }
        )
    if seen != candidate_keys - {selection_key}:
        raise ValueError("every non-selected candidate requires one rejection")
    return tuple(sorted(normalized, key=lambda item: item["candidate_key"]))


def _normalize_actions(
    value: Any,
    *,
    allow_empty: bool = False,
) -> tuple[dict[str, Any], ...]:
    rows = _mapping_sequence(value, "actions", non_empty=not allow_empty)
    normalized: list[dict[str, Any]] = []
    keys: set[str] = set()
    for row in rows:
        key = _required(row.get("key"), "actions.key")
        if key in keys:
            raise ValueError("action keys must be unique")
        keys.add(key)
        item: dict[str, Any] = {
            "key": key,
            "action_type": _required(
                row.get("action_type"),
                "actions.action_type",
            ),
            "owner": _required(row.get("owner"), "actions.owner"),
            "executor": _required(row.get("executor"), "actions.executor"),
            "target_ref": _required(
                row.get("target_ref"),
                "actions.target_ref",
            ),
            "input_hash": _sha256(
                row.get("input_hash"),
                "actions.input_hash",
            ),
            "rollback_contract": _required(
                row.get("rollback_contract"),
                "actions.rollback_contract",
            ),
            "expected_effect": _required(
                row.get("expected_effect"),
                "actions.expected_effect",
            ),
        }
        if row.get("source_object") is not None:
            source_object = _mapping(row.get("source_object"), "actions.source_object")
            item["source_object"] = {
                "domain": _required(
                    source_object.get("domain"),
                    "actions.source_object.domain",
                ),
                "table": _required(
                    source_object.get("table"),
                    "actions.source_object.table",
                ),
                "primary_key": _required(
                    source_object.get("primary_key"),
                    "actions.source_object.primary_key",
                ),
                "primary_key_value": _required(
                    source_object.get("primary_key_value"),
                    "actions.source_object.primary_key_value",
                ),
                "input_hash": _sha256(
                    source_object.get("input_hash"),
                    "actions.source_object.input_hash",
                ),
            }
        normalized.append(item)
    return tuple(sorted(normalized, key=lambda item: item["key"]))


def _value_context_payload(
    normalized: Mapping[str, Any],
    *,
    access_control: Mapping[str, Any],
    supersedes_revision_id: str,
) -> dict[str, Any]:
    values = list(normalized["values"])
    by_category = {
        category: [str(item["constraint"]) for item in values if item["category"] == category]
        for category in FIXED_VALUE_PRECEDENCE
    }
    explicit_goals = by_category["explicit_user_goal"]
    selected = next(
        candidate
        for candidate in normalized["candidates"]
        if candidate["key"] == normalized["selection_key"]
    )
    selected_value_keys = set(selected["satisfies_value_keys"])
    conflicts = [
        {
            "winner_item_ref": higher["item_ref"],
            "winner_key": higher["key"],
            "winner_category": higher["category"],
            "loser_item_ref": lower["item_ref"],
            "loser_key": lower["key"],
            "loser_category": lower["category"],
            "disposition_code": "higher_precedence_wins",
            "changed_decision": bool(
                higher["key"] in selected_value_keys and lower["key"] not in selected_value_keys
            ),
        }
        for higher, lower in _value_conflict_pairs(values)
    ]
    return {
        "schema_version": COGNITIVE_OBJECT_SCHEMA_VERSIONS["value_context"],
        "value_context_id": _value_context_id(
            str(normalized["scope_type"]),
            str(normalized["scope_id"]),
        ),
        "scope": {
            "type": normalized["scope_type"],
            "id": normalized["scope_id"],
        },
        "valid_from": min(str(item["valid_from"]) for item in values),
        "valid_until": "",
        "precedence_contract": VALUE_PRECEDENCE_CONTRACT,
        "precedence": list(FIXED_VALUE_PRECEDENCE),
        "source_authority_catalog_hash": normalized["source_authority_catalog_hash"],
        "source_authority_catalog": dict(normalized["source_authority_catalog"]),
        "items": values,
        "conflicts": conflicts,
        "disposition": "resolved",
        "consumed_refs": sorted(str(item["item_ref"]) for item in values),
        "consumed_refs_hash": sha256_json(sorted(str(item["item_ref"]) for item in values)),
        "supersedes_revision_id": supersedes_revision_id,
        "user_goal": explicit_goals[0] if explicit_goals else "",
        "project_constraints": by_category["project_constraint"],
        "safety_constraints": by_category["safety_permission_privacy"],
        "privacy_constraints": by_category["safety_permission_privacy"],
        "cost_constraints": by_category["cost_convenience"],
        "reversibility": (
            "; ".join(str(action["rollback_contract"]) for action in normalized["actions"])
            or "no executable action; decision rejected"
        ),
        "access_control": dict(access_control),
    }


def _snapshot_payload(
    normalized: Mapping[str, Any],
    *,
    value_revision: CognitiveStateRevision,
    consumed: Sequence[AuthorizedDecisionSnapshotSource],
    access_summary: Mapping[str, Any],
    access_control: Mapping[str, Any],
    evidence_refs: Sequence[str],
    profile_revision_refs: Sequence[str] = (),
) -> dict[str, Any]:
    consumed_state = [
        _snapshot_revision_entry(
            item.revision,
            source_read_purpose=item.source_read_purpose,
            source_purpose_contract_hash=item.source_purpose_contract_hash,
        )
        for item in consumed
    ]
    active_beliefs = [
        item.revision.revision_id
        for item in consumed
        if item.revision.object_type == "belief_revision"
    ]
    policy_refs = [
        item.revision.revision_id
        for item in consumed
        if item.revision.object_type == "belief_revision"
        and item.revision.payload.get("claim_kind") == "policy"
    ]
    core_payload = {
        "schema_version": COGNITIVE_OBJECT_SCHEMA_VERSIONS["cognitive_state_snapshot"],
        "task": normalized["task"],
        "goal": normalized["goal"],
        "constraints": list(normalized["constraints"]),
        "scope": {
            "type": normalized["scope_type"],
            "id": normalized["scope_id"],
        },
        "source_completeness": {
            "candidate_count": int(access_summary["candidate_count"]),
            "authorized_count": int(access_summary["authorized_count"]),
            "denied_by_reason": dict(access_summary["denied_by_reason"]),
            "contract": dict(access_summary["contract"]),
            "by_object_type": {
                str(object_type): dict(summary)
                for object_type, summary in access_summary["by_object_type"].items()
            },
        },
        "evidence_refs": list(evidence_refs),
        "active_belief_refs": active_beliefs,
        "profile_revision_refs": sorted(
            {str(value) for value in profile_revision_refs if str(value)}
        ),
        "policy_revision_refs": policy_refs,
        "value_context_revision_id": value_revision.revision_id,
        "value_context_hash": value_revision.payload_hash,
        "source_authority_catalog_hash": normalized["source_authority_catalog_hash"],
        "consumed_state": consumed_state,
        "head_preconditions": [
            {
                "object_type": item.revision.object_type,
                "object_id": item.revision.object_id,
                "revision_id": item.revision.revision_id,
            }
            for item in consumed
        ],
        "state_hash": sha256_json(consumed_state),
        "access_control": dict(access_control),
    }
    snapshot_id = "snapshot-" + _digest(core_payload)[:32]
    payload = {**core_payload, "snapshot_id": snapshot_id}
    payload["snapshot_hash"] = sha256_json(payload)
    return payload


def _decision_payload(
    normalized: Mapping[str, Any],
    *,
    decision_id: str,
    value_revision: CognitiveStateRevision,
    snapshot_revision: CognitiveStateRevision,
    action_specs: Sequence[Mapping[str, Any]],
    access_control: Mapping[str, Any],
    prediction_refs: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    selected = next(
        value for value in normalized["candidates"] if value["key"] == normalized["selection_key"]
    )
    snapshot_hash = str(snapshot_revision.payload["snapshot_hash"])
    return {
        "schema_version": COGNITIVE_OBJECT_SCHEMA_VERSIONS["decision_trace"],
        "decision_id": decision_id,
        "decision_state": normalized["approval"]["decision"],
        "supersedes_decision_revision_ids": list(normalized["supersedes_decision_revision_ids"]),
        "task": normalized["task"],
        "goal": normalized["goal"],
        "constraints": list(normalized["constraints"]),
        "snapshot_id": snapshot_revision.object_id,
        "snapshot_revision_id": snapshot_revision.revision_id,
        "snapshot_hash": snapshot_hash,
        "value_context_id": value_revision.object_id,
        "value_context_revision_id": value_revision.revision_id,
        "value_context_hash": value_revision.payload_hash,
        "source_authority_catalog_hash": normalized["source_authority_catalog_hash"],
        "belief_revision_refs": list(snapshot_revision.payload["active_belief_refs"]),
        "profile_revision_refs": list(snapshot_revision.payload["profile_revision_refs"]),
        "policy_revision_refs": list(snapshot_revision.payload["policy_revision_refs"]),
        "candidates": [dict(value) for value in normalized["candidates"]],
        "rejected_reasons": [dict(value) for value in normalized["rejections"]],
        "selection": {
            "candidate_key": selected["key"],
            "candidate_id": selected["candidate_id"],
        },
        "model_spec": dict(normalized["model_spec"]),
        "tool_specs": [dict(value) for value in normalized["tool_specs"]],
        "prompt_spec": dict(normalized["prompt_spec"]),
        "expected_outcomes": [dict(value) for value in normalized["expected_outcomes"]],
        "prediction_refs": [dict(value) for value in prediction_refs],
        "evaluation_window": dict(normalized["evaluation_window"]),
        "approval": dict(normalized["approval"]),
        "action_specs": [dict(value) for value in action_specs],
        "action_refs": [str(value["action_id"]) for value in action_specs],
        "effect_refs": [str(value["effect_id"]) for value in action_specs],
        "access_control": dict(access_control),
    }


def _material_action_specs(
    normalized: Mapping[str, Any],
    *,
    decision_id: str,
) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for raw in normalized["actions"]:
        action = dict(raw)
        action_identity = {
            "decision_id": decision_id,
            "action_type": action["action_type"],
            "owner": action["owner"],
            "executor": action["executor"],
            "target_ref": action["target_ref"],
            "input_hash": action["input_hash"],
            "key": action["key"],
        }
        if "source_object" in action:
            action_identity["source_object"] = action["source_object"]
        action_id = "material-action-" + _digest(action_identity)[:32]
        effect_id = (
            "material-effect-"
            + _digest({"action_id": action_id, "expected_effect": action["expected_effect"]})[:32]
        )
        result.append(
            {
                **action,
                "action_id": action_id,
                "effect_id": effect_id,
                "target_hash": sha256_json(action["target_ref"]),
            }
        )
    return tuple(result)


def _action_command(
    decision_revision: CognitiveStateRevision,
    *,
    value_revision: CognitiveStateRevision,
    snapshot_revision: CognitiveStateRevision,
    action: Mapping[str, Any],
    prediction_revisions: Sequence[CognitiveStateRevision],
    prediction_plan: PredictionPlan | None,
    created_at: str,
) -> LocalConsumerCommand:
    consumer_id = (
        "material-action:"
        + str(action["owner"])
        + ":"
        + str(action["action_id"]).removeprefix("material-action-")[:16]
    )
    return LocalConsumerCommand.create(
        revision_id=decision_revision.revision_id,
        consumer_id=consumer_id,
        command_type=MATERIAL_ACTION_COMMAND_TYPE,
        payload={
            "schema_version": "mnemos.material_action_command.v1",
            "decision_id": decision_revision.object_id,
            "decision_revision_id": decision_revision.revision_id,
            "decision_hash": decision_revision.payload_hash,
            "snapshot_revision_id": snapshot_revision.revision_id,
            "snapshot_hash": snapshot_revision.payload["snapshot_hash"],
            "value_context_revision_id": value_revision.revision_id,
            "value_context_hash": value_revision.payload_hash,
            "prediction_refs": [
                {
                    "prediction_id": revision.object_id,
                    "prediction_plan_hash": str(revision.payload["prediction_plan_hash"]),
                    "prediction_revision_id": revision.revision_id,
                    "prediction_revision_hash": revision.payload_hash,
                }
                for revision in prediction_revisions
            ],
            **(
                {
                    "prediction_delivery_projection": {
                        "schema_version": ("mnemos.material_prediction_delivery_projection.v1"),
                        "prediction_id": prediction_plan.prediction_id,
                        "prediction_plan_hash": (prediction_plan.prediction_plan_hash),
                        "delivery_event_id": prediction_plan.delivery_event_id,
                        "delivery_event_payload": dict(prediction_plan.route_payload),
                        "delivery_event_payload_hash": (
                            prediction_plan.delivery_event_payload_hash
                        ),
                    }
                }
                if prediction_plan is not None
                else {}
            ),
            **dict(action),
        },
        created_at=created_at,
    )


def _decision_evidence_refs(normalized: Mapping[str, Any]) -> tuple[str, ...]:
    refs = set(normalized["source"]["evidence_refs"])
    refs.add(normalized["source"]["source_id"])
    refs.add(normalized["approval"]["evidence_ref"])
    for value in normalized["values"]:
        refs.add(value["source_id"])
        refs.update(value["evidence_refs"])
    for candidate in normalized["candidates"]:
        refs.update(candidate["supporting_evidence"])
        refs.update(candidate["opposing_evidence"])
    for rejection in normalized["rejections"]:
        refs.update(rejection["evidence_refs"])
    return tuple(sorted(str(ref) for ref in refs if str(ref)))


def _value_context_id(scope_type: str, scope_id: str) -> str:
    return (
        "value-context-"
        + _digest(
            {
                "scope_type": scope_type,
                "scope_id": scope_id,
                "kind": VALUE_PRECEDENCE_CONTRACT,
            }
        )[:32]
    )


def _snapshot_revision_entry(
    revision: CognitiveStateRevision,
    *,
    source_read_purpose: str,
    source_purpose_contract_hash: str,
) -> dict[str, Any]:
    return {
        "object_type": revision.object_type,
        "object_id": revision.object_id,
        "revision_id": revision.revision_id,
        "schema_version": revision.schema_version,
        "payload": dict(revision.payload),
        "payload_hash": revision.payload_hash,
        "evidence_hash": revision.evidence_hash,
        "access_control_hash": cognitive_access_hash(revision.payload["access_control"]),
        "source_read_purpose": source_read_purpose,
        "source_purpose_contract_hash": source_purpose_contract_hash,
    }


def _historical_snapshot_revision_entry(
    revision: CognitiveStateRevision,
) -> dict[str, Any]:
    return {
        "object_type": revision.object_type,
        "object_id": revision.object_id,
        "revision_id": revision.revision_id,
        "schema_version": revision.schema_version,
        "payload": dict(revision.payload),
        "payload_hash": revision.payload_hash,
        "evidence_hash": revision.evidence_hash,
        "access_control_hash": cognitive_access_hash(revision.payload["access_control"]),
    }


def _verify_revision_payload_hash(revision: CognitiveStateRevision) -> None:
    if sha256_json(dict(revision.payload)) != revision.payload_hash:
        raise RuntimeError(f"{revision.object_type} payload hash mismatch")


def _verify_snapshot_hash(revision: CognitiveStateRevision) -> None:
    payload = dict(revision.payload)
    stored_hash = _sha256(payload.pop("snapshot_hash", None), "snapshot_hash")
    if sha256_json(payload) != stored_hash:
        raise RuntimeError("cognitive snapshot hash mismatch")
