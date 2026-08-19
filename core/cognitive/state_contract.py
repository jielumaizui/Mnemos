"""Typed immutable contracts for canonical cognitive state revisions.

The generic cognitive-data event remains a transport envelope.  Domain content
is admitted here, validated against an object-specific contract, narrowly
redacted for local persistence, and content-addressed before any database work.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from core.cognitive.access_control import (
    cognitive_access_hash,
    validate_cognitive_access_envelope,
)
from core.cognition_episode_contract import (
    COGNITION_EPISODE_FIELDS,
    COGNITION_EPISODE_V2_FIELDS,
    LEGACY_COGNITION_EPISODE_SCHEMA_VERSION,
)
from core.privacy.content_redaction import REDACTION_POLICY, redact_persistence_value
from core.cognitive.feedback_contract import (
    validate_feedback_attribution_payload,
    validate_user_reaction_payload,
)
from core.cognitive.training_contract import (
    validate_training_admission_payload,
    validate_training_run_payload,
)
from core.cognitive.state_contract_schema import (  # noqa: F401
    COGNITIVE_OBJECT_SCHEMA_VERSIONS,
    COGNITIVE_OBJECT_TYPES,
    COGNITIVE_STATE_CONTRACT_VERSION,
    FIXED_VALUE_PRECEDENCE,
    VALUE_PRECEDENCE_CONTRACT,
    _OMISSION_ID_PATTERN,
    _REQUIRED_PAYLOAD_FIELDS,
    _SEQUENCE_FIELDS,
    _SHA256_PATTERN,
    _VALUE_AUTHORITY_BY_CATEGORY,
)
from core.cognitive.state_validation_primitives import (  # noqa: F401
    _contains_prohibited_reasoning,
    _exact_mapping,
    _finite_float,
    _is_non_negative_int,
    _json_value,
    _parse_timestamp,
    _positive_finite,
    _required_sha256,
    _required_text,
    _string_tuple,
    _validate_decision_execution_specs,
    _validate_source_span_id,
    canonical_json,
    now_utc,
    sha256_json,
)
from core.cognitive.state_extended_validation import (
    _validate_belief_revision_payload,
    _validate_calibration_record_payload,
    _validate_cognition_episode_payload,
)


def _validate_payload(object_type: str, payload: Mapping[str, Any]) -> None:
    if "access_control" not in payload:
        raise ValueError(f"{object_type} payload missing fields: access_control")
    validate_cognitive_access_envelope(payload["access_control"])
    required = _REQUIRED_PAYLOAD_FIELDS[object_type]
    if (
        object_type == "cognition_episode"
        and payload.get("schema_version") == LEGACY_COGNITION_EPISODE_SCHEMA_VERSION
    ):
        required = tuple(field for field in required if field not in COGNITION_EPISODE_V2_FIELDS)
    missing = [field for field in required if field not in payload]
    if missing:
        raise ValueError(f"{object_type} payload missing fields: {', '.join(missing)}")
    for field_name in required:
        value = payload[field_name]
        if object_type == "cognition_episode" and field_name in (
            *COGNITION_EPISODE_FIELDS,
            "source_event_ids",
            "source_spans",
        ):
            if not isinstance(value, (list, tuple)):
                raise ValueError(f"{object_type}.{field_name} must be a sequence")
        elif field_name in _SEQUENCE_FIELDS:
            if not isinstance(value, (list, tuple)):
                raise ValueError(f"{object_type}.{field_name} must be a sequence")
        elif (
            (
                object_type == "belief_revision"
                and field_name
                in {
                    "confidence",
                    "valid_until",
                    "supersedes_revision_id",
                    "correction_of_revision_id",
                }
            )
            or (
                object_type == "value_context"
                and field_name in {"valid_until", "supersedes_revision_id", "user_goal"}
            )
            or (
                object_type in {"prediction_record", "outcome_measurement"}
                and field_name in {"supersedes_revision_id", "correction_of_revision_id"}
            )
            or (
                object_type == "user_reaction_event"
                and field_name in {"supersedes_event_id", "correction_of_event_id"}
            )
            or (
                object_type == "feedback_attribution_record"
                and field_name in {"supersedes_revision_id", "correction_of_revision_id"}
            )
            or (
                object_type == "training_admission_record"
                and field_name in {"supersedes_revision_id", "correction_of_revision_id"}
            )
            or (
                object_type == "training_run_record"
                and field_name in {"supersedes_revision_id", "rebuild_of_revision_id"}
            )
        ):
            continue
        elif value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"{object_type}.{field_name} is required")
    if object_type == "belief_revision":
        _validate_belief_revision_payload(payload)
    if object_type == "cognition_episode":
        _validate_cognition_episode_payload(payload)
    if object_type == "calibration_record":
        _validate_calibration_record_payload(payload)
    if object_type == "value_context":
        _validate_value_context_payload(payload)
    if object_type == "cognitive_state_snapshot":
        _validate_cognitive_state_snapshot_payload(payload)
    if object_type == "decision_trace":
        _validate_decision_trace_payload(payload)
    if object_type == "prediction_record":
        _validate_prediction_record_payload(payload)
    if object_type == "user_reaction_event":
        validate_user_reaction_payload(payload)
    if object_type == "feedback_attribution_record":
        validate_feedback_attribution_payload(payload)
    if object_type == "outcome_measurement":
        _validate_outcome_measurement_payload(payload)
    if object_type == "training_admission_record":
        validate_training_admission_payload(payload)
    if object_type == "training_run_record":
        validate_training_run_payload(payload)


def _rebind_redacted_payload_identity(
    object_type: str,
    payload: dict[str, Any],
) -> None:
    """Recompute identities whose inputs may change during persistence redaction."""

    if object_type == "cognition_episode" and payload.get(
        "schema_version"
    ) == COGNITIVE_OBJECT_SCHEMA_VERSIONS[object_type]:
        payload["claim_catalog_hash"] = sha256_json(list(payload.get("claims") or ()))
        return
    if object_type != "cognitive_state_snapshot":
        return

    payload["state_hash"] = sha256_json(list(payload.get("consumed_state") or ()))
    identity_payload = dict(payload)
    identity_payload.pop("snapshot_hash", None)
    identity_payload.pop("snapshot_id", None)
    payload["snapshot_id"] = (
        "snapshot-" + sha256_json(identity_payload).split(":", 1)[1][:32]
    )
    hashed_payload = dict(payload)
    hashed_payload.pop("snapshot_hash", None)
    payload["snapshot_hash"] = sha256_json(hashed_payload)


def validate_cognitive_state_payload(
    object_type: str,
    payload: Mapping[str, Any],
) -> None:
    """Public read-only validator used by independent state auditors."""

    normalized_type = _required_text(object_type, "object_type")
    if normalized_type not in COGNITIVE_OBJECT_TYPES:
        raise ValueError(f"unsupported cognitive object type: {normalized_type}")
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be an object")
    _validate_payload(normalized_type, payload)


def _validate_value_context_payload(payload: Mapping[str, Any]) -> None:
    if payload["schema_version"] != COGNITIVE_OBJECT_SCHEMA_VERSIONS["value_context"]:
        raise ValueError("value_context payload schema_version mismatch")
    if not re.fullmatch(
        r"value-context-[0-9a-f]{32}",
        str(payload["value_context_id"]),
    ):
        raise ValueError("value_context.value_context_id is invalid")
    scope = payload["scope"]
    if not isinstance(scope, Mapping) or set(scope) != {"type", "id"}:
        raise ValueError("value_context.scope is invalid")
    _required_text(scope.get("type"), "value_context.scope.type")
    _required_text(scope.get("id"), "value_context.scope.id")
    _parse_timestamp(payload["valid_from"], "value_context.valid_from")
    if payload["valid_until"]:
        valid_until = _parse_timestamp(
            payload["valid_until"],
            "value_context.valid_until",
        )
        if valid_until <= _parse_timestamp(
            payload["valid_from"],
            "value_context.valid_from",
        ):
            raise ValueError("value_context validity window is invalid")
    if (
        payload["precedence_contract"] != VALUE_PRECEDENCE_CONTRACT
        or tuple(payload["precedence"]) != FIXED_VALUE_PRECEDENCE
    ):
        raise ValueError("value_context precedence contract mismatch")
    if payload["disposition"] != "resolved":
        raise ValueError("value_context disposition is unresolved")

    catalog = payload["source_authority_catalog"]
    if not isinstance(catalog, Mapping):
        raise ValueError("value_context source authority catalog is invalid")
    if payload["source_authority_catalog_hash"] != sha256_json(catalog):
        raise ValueError("value_context source authority catalog hash mismatch")
    raw_authorities = catalog.get("entries")
    if not isinstance(raw_authorities, list) or not raw_authorities:
        raise ValueError("value_context source authority catalog is empty")
    authorities: dict[str, Mapping[str, Any]] = {}
    for raw_authority in raw_authorities:
        if not isinstance(raw_authority, Mapping):
            raise ValueError("value_context source authority entry is invalid")
        authority_id = _required_text(
            raw_authority.get("source_authority_id"),
            "value_context source authority id",
        )
        if authority_id in authorities:
            raise ValueError("value_context source authority IDs must be unique")
        if not bool(raw_authority.get("allows_cognitive_update")):
            raise ValueError("value_context contains a low-authority value source")
        _required_sha256(
            raw_authority.get("content_sha256"),
            "value_context source authority content hash",
        )
        authorities[authority_id] = raw_authority

    items = payload["items"]
    if not items:
        raise ValueError("value_context.items must be non-empty")
    refs: list[str] = []
    by_ref: dict[str, Mapping[str, Any]] = {}
    keys: set[str] = set()
    for raw in items:
        if not isinstance(raw, Mapping):
            raise ValueError("value_context item must be an object")
        item = dict(raw)
        item_ref = str(item.pop("item_ref", ""))
        expected_ref = "value-" + sha256_json(item).split(":", 1)[1][:32]
        if item_ref != expected_ref or item_ref in by_ref:
            raise ValueError("value_context item_ref mismatch")
        key = _required_text(item.get("key"), "value_context item key")
        if key in keys:
            raise ValueError("value_context item keys must be unique")
        keys.add(key)
        if item.get("category") not in FIXED_VALUE_PRECEDENCE:
            raise ValueError("value_context item category is invalid")
        for field_name in (
            "constraint",
            "source_authority_id",
            "source_authority",
            "source_id",
            "source_revision_id",
        ):
            _required_text(item.get(field_name), f"value_context item {field_name}")
        _required_sha256(
            item.get("source_content_hash"),
            "value_context item source_content_hash",
        )
        evidence = _string_tuple(
            item.get("evidence_refs"),
            "value_context item evidence_refs",
        )
        if not evidence:
            raise ValueError("value_context item evidence_refs must be non-empty")
        authority = authorities.get(str(item["source_authority_id"]))
        if authority is None:
            raise ValueError("value_context item authority is not in its catalog")
        if (
            item["source_authority"] != authority.get("source_authority")
            or item["source_id"] != authority.get("source_event_id")
            or item["source_content_hash"] != authority.get("content_sha256")
        ):
            raise ValueError("value_context item authority binding mismatch")
        if item["source_authority"] not in _VALUE_AUTHORITY_BY_CATEGORY[str(item["category"])]:
            raise ValueError("value_context item authority cannot authorize category")
        if str(item["source_authority_id"]) not in evidence:
            raise ValueError("value_context item evidence lacks its authority ref")
        _parse_timestamp(item.get("valid_from"), "value_context item valid_from")
        if item.get("valid_until"):
            _parse_timestamp(item["valid_until"], "value_context item valid_until")
        conflicts = _string_tuple(
            item.get("conflicts_with_keys", ()),
            "value_context item conflicts_with_keys",
        )
        if tuple(conflicts) != tuple(sorted(set(conflicts))):
            raise ValueError("value_context item conflicts must be sorted and unique")
        refs.append(item_ref)
        by_ref[item_ref] = raw
    canonical_refs = sorted(refs)
    if list(payload["consumed_refs"]) != canonical_refs or payload[
        "consumed_refs_hash"
    ] != sha256_json(canonical_refs):
        raise ValueError("value_context consumed refs mismatch")
    for conflict in payload["conflicts"]:
        if not isinstance(conflict, Mapping):
            raise ValueError("value_context conflict must be an object")
        winner = by_ref.get(str(conflict.get("winner_item_ref") or ""))
        loser = by_ref.get(str(conflict.get("loser_item_ref") or ""))
        if winner is None or loser is None:
            raise ValueError("value_context conflict references a missing item")
        if (
            FIXED_VALUE_PRECEDENCE.index(str(winner["category"]))
            >= (FIXED_VALUE_PRECEDENCE.index(str(loser["category"])))
            or conflict.get("disposition_code") != "higher_precedence_wins"
        ):
            raise ValueError("value_context conflict disposition is invalid")
    if payload["supersedes_revision_id"] and not str(payload["supersedes_revision_id"]).startswith(
        "cogrev-"
    ):
        raise ValueError("value_context supersedes revision is invalid")


def _validate_cognitive_state_snapshot_payload(payload: Mapping[str, Any]) -> None:
    schema_version = str(payload["schema_version"])
    if schema_version not in {
        "mnemos.cognitive_state_snapshot.v1",
        COGNITIVE_OBJECT_SCHEMA_VERSIONS["cognitive_state_snapshot"],
    }:
        raise ValueError("cognitive_state_snapshot schema_version mismatch")
    typed_source_contract = (
        schema_version == COGNITIVE_OBJECT_SCHEMA_VERSIONS["cognitive_state_snapshot"]
    )
    if not re.fullmatch(r"snapshot-[0-9a-f]{32}", str(payload["snapshot_id"])):
        raise ValueError("cognitive_state_snapshot.snapshot_id is invalid")
    snapshot_hash = _required_sha256(
        payload["snapshot_hash"],
        "cognitive_state_snapshot.snapshot_hash",
    )
    without_hash = dict(payload)
    without_hash.pop("snapshot_hash")
    if sha256_json(without_hash) != snapshot_hash:
        raise ValueError("cognitive_state_snapshot snapshot_hash mismatch")
    identity_payload = dict(without_hash)
    identity_payload.pop("snapshot_id")
    expected_id = "snapshot-" + sha256_json(identity_payload).split(":", 1)[1][:32]
    if payload["snapshot_id"] != expected_id:
        raise ValueError("cognitive_state_snapshot snapshot_id mismatch")
    _required_sha256(
        payload["value_context_hash"],
        "cognitive_state_snapshot.value_context_hash",
    )
    _required_sha256(
        payload["source_authority_catalog_hash"],
        "cognitive_state_snapshot.source_authority_catalog_hash",
    )
    _required_sha256(payload["state_hash"], "cognitive_state_snapshot.state_hash")
    consumed = payload["consumed_state"]
    if payload["state_hash"] != sha256_json(list(consumed)):
        raise ValueError("cognitive_state_snapshot consumed state hash mismatch")
    triples: list[dict[str, str]] = []
    seen_revisions: set[str] = set()
    belief_refs: list[str] = []
    policy_refs: list[str] = []
    for raw in consumed:
        if not isinstance(raw, Mapping):
            raise ValueError("cognitive_state_snapshot consumed entry is invalid")
        revision_id = _required_text(
            raw.get("revision_id"),
            "cognitive_state_snapshot consumed revision_id",
        )
        if revision_id in seen_revisions:
            raise ValueError("cognitive_state_snapshot consumed revision is duplicated")
        seen_revisions.add(revision_id)
        for field_name in (
            "object_type",
            "object_id",
            "schema_version",
            "evidence_hash",
        ):
            _required_text(
                raw.get(field_name),
                f"cognitive_state_snapshot consumed {field_name}",
            )
        if not isinstance(raw.get("payload"), Mapping):
            raise ValueError("cognitive_state_snapshot consumed payload is invalid")
        if sha256_json(raw["payload"]) != _required_sha256(
            raw.get("payload_hash"),
            "cognitive_state_snapshot consumed payload_hash",
        ):
            raise ValueError("cognitive_state_snapshot consumed payload hash mismatch")
        if cognitive_access_hash(raw["payload"]["access_control"]) != _required_sha256(
            raw.get("access_control_hash"),
            "cognitive_state_snapshot consumed access_control_hash",
        ):
            raise ValueError("cognitive_state_snapshot consumed ACL hash mismatch")
        if typed_source_contract:
            _required_text(
                raw.get("source_read_purpose"),
                "cognitive_state_snapshot consumed source_read_purpose",
            )
            _required_sha256(
                raw.get("source_purpose_contract_hash"),
                "cognitive_state_snapshot consumed source_purpose_contract_hash",
            )
        triples.append(
            {
                "object_type": str(raw["object_type"]),
                "object_id": str(raw["object_id"]),
                "revision_id": revision_id,
            }
        )
        if raw["object_type"] == "belief_revision":
            belief_refs.append(revision_id)
            if raw["payload"].get("claim_kind") == "policy":
                policy_refs.append(revision_id)
    if list(payload["head_preconditions"]) != triples:
        raise ValueError("cognitive_state_snapshot head preconditions mismatch")
    if (
        list(payload["active_belief_refs"]) != belief_refs
        or list(payload["policy_revision_refs"]) != policy_refs
    ):
        raise ValueError("cognitive_state_snapshot typed refs mismatch")
    if typed_source_contract:
        completeness = payload["source_completeness"]
        if not isinstance(completeness, Mapping):
            raise ValueError("cognitive_state_snapshot source completeness is invalid")
        contract = completeness.get("contract")
        if not isinstance(contract, Mapping):
            raise ValueError("cognitive_state_snapshot source contract is invalid")
        _required_text(
            contract.get("schema_version"),
            "cognitive_state_snapshot source contract schema_version",
        )
        _required_sha256(
            contract.get("contract_hash"),
            "cognitive_state_snapshot source contract hash",
        )
        _required_text(
            contract.get("output_purpose"),
            "cognitive_state_snapshot source contract output_purpose",
        )
        if not isinstance(completeness.get("by_object_type"), Mapping):
            raise ValueError("cognitive_state_snapshot source denominator is invalid")


def _validate_decision_trace_payload(payload: Mapping[str, Any]) -> None:
    if payload["schema_version"] != COGNITIVE_OBJECT_SCHEMA_VERSIONS["decision_trace"]:
        raise ValueError("decision_trace schema_version mismatch")
    decision_id = str(payload["decision_id"])
    if not re.fullmatch(r"decision-[0-9a-f]{32}", decision_id):
        raise ValueError("decision_trace.decision_id is invalid")
    state = str(payload["decision_state"])
    if state not in {"approved", "rejected"}:
        raise ValueError("decision_trace.decision_state is invalid")
    superseded = list(payload["supersedes_decision_revision_ids"])
    if len(superseded) != len(set(superseded)) or superseded != sorted(superseded):
        raise ValueError("decision_trace superseded decision revisions must be sorted and unique")
    for revision_id in superseded:
        _required_text(
            revision_id,
            "decision_trace superseded decision revision",
        )
    for field_name in (
        "snapshot_hash",
        "value_context_hash",
        "source_authority_catalog_hash",
    ):
        _required_sha256(payload[field_name], f"decision_trace.{field_name}")
    candidates = payload["candidates"]
    if len(candidates) < 2:
        raise ValueError("decision_trace requires at least two candidates")
    candidate_by_key: dict[str, Mapping[str, Any]] = {}
    for raw in candidates:
        if not isinstance(raw, Mapping):
            raise ValueError("decision_trace candidate must be an object")
        candidate = dict(raw)
        candidate_id = str(candidate.pop("candidate_id", ""))
        expected_id = "candidate-" + sha256_json(candidate).split(":", 1)[1][:32]
        if candidate_id != expected_id:
            raise ValueError("decision_trace candidate ID mismatch")
        key = _required_text(candidate.get("key"), "decision_trace candidate key")
        if key in candidate_by_key:
            raise ValueError("decision_trace candidate keys must be unique")
        candidate_by_key[key] = raw
    selection = payload["selection"]
    if not isinstance(selection, Mapping):
        raise ValueError("decision_trace selection is invalid")
    selected = candidate_by_key.get(str(selection.get("candidate_key") or ""))
    if selected is None or selection.get("candidate_id") != selected.get("candidate_id"):
        raise ValueError("decision_trace selection does not resolve")
    if selected.get("violated_value_keys"):
        raise ValueError("decision_trace selected candidate violates a hard constraint")
    rejected = payload["rejected_reasons"]
    rejected_keys = {
        str(item.get("candidate_key") or "") for item in rejected if isinstance(item, Mapping)
    }
    if len(rejected_keys) != len(rejected) or rejected_keys != set(candidate_by_key) - {
        str(selection["candidate_key"])
    }:
        raise ValueError("decision_trace rejection coverage mismatch")
    action_specs = payload["action_specs"]
    action_ids: list[str] = []
    effect_ids: list[str] = []
    for raw in action_specs:
        if not isinstance(raw, Mapping):
            raise ValueError("decision_trace action spec must be an object")
        action_id = str(raw.get("action_id") or "")
        action_identity = {
            "decision_id": decision_id,
            "action_type": raw.get("action_type"),
            "owner": raw.get("owner"),
            "executor": raw.get("executor"),
            "target_ref": raw.get("target_ref"),
            "input_hash": raw.get("input_hash"),
            "key": raw.get("key"),
        }
        if "source_object" in raw:
            source_object = raw["source_object"]
            if not isinstance(source_object, Mapping) or set(source_object) != {
                "domain",
                "table",
                "primary_key",
                "primary_key_value",
                "input_hash",
            }:
                raise ValueError("decision_trace source object is invalid")
            for field_name in (
                "domain",
                "table",
                "primary_key",
                "primary_key_value",
            ):
                _required_text(
                    source_object.get(field_name),
                    f"decision_trace source_object.{field_name}",
                )
            _required_sha256(
                source_object.get("input_hash"),
                "decision_trace source_object.input_hash",
            )
            action_identity["source_object"] = source_object
        expected_action = "material-action-" + sha256_json(action_identity).split(":", 1)[1][:32]
        if action_id != expected_action:
            raise ValueError("decision_trace action ID mismatch")
        effect_id = str(raw.get("effect_id") or "")
        expected_effect = (
            "material-effect-"
            + sha256_json(
                {
                    "action_id": action_id,
                    "expected_effect": raw.get("expected_effect"),
                }
            ).split(":", 1)[1][:32]
        )
        if effect_id != expected_effect:
            raise ValueError("decision_trace effect ID mismatch")
        if raw.get("target_hash") != sha256_json(raw.get("target_ref")):
            raise ValueError("decision_trace target hash mismatch")
        _required_sha256(raw.get("input_hash"), "decision_trace action input_hash")
        action_ids.append(action_id)
        effect_ids.append(effect_id)
    if (state == "approved" and not action_ids) or (state == "rejected" and action_ids):
        raise ValueError("decision_trace decision/action state mismatch")
    if list(payload["action_refs"]) != action_ids or list(payload["effect_refs"]) != effect_ids:
        raise ValueError("decision_trace action/effect refs mismatch")
    prediction_refs = payload["prediction_refs"]
    seen_prediction_ids: set[str] = set()
    for raw in prediction_refs:
        if not isinstance(raw, Mapping) or set(raw) != {
            "prediction_id",
            "prediction_plan_hash",
        }:
            raise ValueError("decision_trace prediction ref is invalid")
        prediction_id = str(raw.get("prediction_id") or "")
        if (
            not re.fullmatch(r"prediction-[0-9a-f]{32}", prediction_id)
            or prediction_id in seen_prediction_ids
        ):
            raise ValueError("decision_trace prediction identity is invalid")
        seen_prediction_ids.add(prediction_id)
        _required_sha256(
            raw.get("prediction_plan_hash"),
            "decision_trace prediction plan hash",
        )
    _validate_decision_execution_specs(payload)
    if _contains_prohibited_reasoning(payload):
        raise ValueError("decision_trace contains prohibited private reasoning")


_PREDICTION_TERMINAL_STATES = frozenset({"measured", "unknown", "censored", "confounded"})


def prediction_input_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable pre-effect input whose hash owns a prediction."""

    return {
        "schema_version": payload.get("schema_version"),
        "prediction_id": payload.get("prediction_id"),
        "prediction_plan_hash": payload.get("prediction_plan_hash"),
        "subject": payload.get("subject"),
        "scope": payload.get("scope"),
        "source_snapshot": payload.get("source_snapshot"),
        "decision_ref": payload.get("decision_ref"),
        "action_ref": payload.get("action_ref"),
        "delivery_ref": payload.get("delivery_ref"),
        "route_disposition": payload.get("route_disposition"),
        "prediction_kind": payload.get("prediction_kind"),
        "metric": payload.get("metric"),
        "confidence": payload.get("confidence"),
        "evaluation_window": payload.get("evaluation_window"),
        "causal_assumptions": payload.get("causal_assumptions"),
    }


def _validate_prediction_record_payload(payload: Mapping[str, Any]) -> None:
    if payload["schema_version"] != COGNITIVE_OBJECT_SCHEMA_VERSIONS["prediction_record"]:
        raise ValueError("prediction_record schema_version mismatch")
    prediction_id = str(payload["prediction_id"])
    if not re.fullmatch(r"prediction-[0-9a-f]{32}", prediction_id):
        raise ValueError("prediction_record.prediction_id is invalid")
    _required_sha256(
        payload["prediction_plan_hash"],
        "prediction_record prediction plan hash",
    )
    revision_state = str(payload["revision_state"])
    if revision_state not in {"open", "terminal"}:
        raise ValueError("prediction_record.revision_state is invalid")
    if payload["prediction_input_hash"] != sha256_json(prediction_input_snapshot(payload)):
        raise ValueError("prediction_record prediction input hash mismatch")
    for field_name in ("supersedes_revision_id", "correction_of_revision_id"):
        value = str(payload[field_name] or "")
        if value and not value.startswith("cogrev-"):
            raise ValueError(f"prediction_record {field_name} is invalid")
    if revision_state == "open" and (
        payload["supersedes_revision_id"] or payload["correction_of_revision_id"]
    ):
        raise ValueError("open prediction cannot supersede a terminal revision")

    subject = _exact_mapping(payload["subject"], {"type", "id"}, "prediction subject")
    _required_text(subject["type"], "prediction subject type")
    _required_text(subject["id"], "prediction subject id")
    scope = _exact_mapping(payload["scope"], {"type", "id"}, "prediction scope")
    _required_text(scope["type"], "prediction scope type")
    _required_text(scope["id"], "prediction scope id")

    source = _exact_mapping(
        payload["source_snapshot"],
        {
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
            "snapshot_hash",
        },
        "prediction source snapshot",
    )
    for field_name in ("source", "subject", "channel", "decision", "delivered_level"):
        _required_text(source[field_name], f"prediction source {field_name}")
    if source["channel"] != "predictive_push":
        raise ValueError("prediction_record only admits predictive delivery")
    if source["subject"] != subject["id"]:
        raise ValueError("prediction_record subject binding mismatch")
    if source["decision"] not in {"deliver", "suppress"}:
        raise ValueError("prediction_record source decision is invalid")
    if not isinstance(source["evidence_refs"], (list, tuple)):
        raise ValueError("prediction_record source evidence refs must be a sequence")
    _required_sha256(source["snapshot_hash"], "prediction source snapshot hash")
    source_without_hash = dict(source)
    source_hash = source_without_hash.pop("snapshot_hash")
    if sha256_json(source_without_hash) != source_hash:
        raise ValueError("prediction_record source snapshot hash mismatch")
    for field_name in ("trust_score", "task_fit_score", "interruption_cost"):
        _finite_float(source[field_name], f"prediction source {field_name}")

    expected_disposition = (
        "suppress"
        if source["decision"] == "suppress"
        else "silent" if source["delivered_level"] == "silent" else "deliver"
    )
    if payload["route_disposition"] != expected_disposition:
        raise ValueError("prediction_record route disposition mismatch")
    if payload["prediction_kind"] != "predictive_delivery_usefulness":
        raise ValueError("prediction_record prediction kind is invalid")

    decision_ref = _exact_mapping(
        payload["decision_ref"],
        {"kind", "decision_id", "revision_id", "revision_hash"},
        "prediction decision ref",
    )
    if decision_ref["kind"] not in {"decision_trace", "trust_decision"}:
        raise ValueError("prediction_record decision ref kind is invalid")
    _required_text(decision_ref["decision_id"], "prediction decision id")
    if decision_ref["kind"] == "decision_trace":
        if not str(decision_ref["revision_id"]).startswith("cogrev-"):
            raise ValueError("prediction_record decision revision is invalid")
        _required_sha256(
            decision_ref["revision_hash"],
            "prediction decision revision hash",
        )
    elif decision_ref["revision_id"] or decision_ref["revision_hash"]:
        raise ValueError("trust decision cannot claim a DecisionTrace revision")

    action_ref = _exact_mapping(
        payload["action_ref"],
        {"action_id", "effect_id"},
        "prediction action ref",
    )
    delivery_ref = _exact_mapping(
        payload["delivery_ref"],
        {"event_id", "event_payload_hash"},
        "prediction delivery ref",
    )
    _required_text(delivery_ref["event_id"], "prediction delivery event id")
    _required_sha256(
        delivery_ref["event_payload_hash"],
        "prediction delivery event payload hash",
    )
    if expected_disposition == "suppress":
        if action_ref["action_id"] or action_ref["effect_id"]:
            raise ValueError("suppressed prediction cannot claim a material action")
    else:
        if not str(action_ref["action_id"]).startswith("material-action-"):
            raise ValueError("prediction_record material action is invalid")
        if not str(action_ref["effect_id"]).startswith("material-effect-"):
            raise ValueError("prediction_record material effect is invalid")

    metric = _exact_mapping(
        payload["metric"],
        {"metric_id", "unit", "predicted_value", "baseline", "measurement_spec"},
        "prediction metric",
    )
    if metric["metric_id"] != "predictive_delivery_usefulness" or metric["unit"] != "class_label":
        raise ValueError("prediction_record metric/unit is invalid")
    if metric["predicted_value"] not in {"useful", "not_useful"}:
        raise ValueError("prediction_record predicted class is invalid")
    if metric["baseline"] not in {"useful", "not_useful", "unknown"}:
        raise ValueError("prediction_record baseline class is invalid")
    measurement_spec = _exact_mapping(
        metric["measurement_spec"],
        {"schema_version", "allowed_values", "requires_independent_evidence"},
        "prediction measurement spec",
    )
    if measurement_spec["schema_version"] != "mnemos.predictive_delivery_measurement.v1":
        raise ValueError("prediction_record measurement spec version mismatch")
    if list(measurement_spec["allowed_values"]) != ["not_useful", "useful"]:
        raise ValueError("prediction_record measurement values are invalid")
    if measurement_spec["requires_independent_evidence"] is not True:
        raise ValueError("prediction_record measurement must require independent evidence")

    confidence = _exact_mapping(
        payload["confidence"],
        {
            "method",
            "method_version",
            "code_hash",
            "spec_hash",
            "is_probability",
            "score_band",
            "inputs",
        },
        "prediction confidence",
    )
    if (
        confidence["method"] != "delivery_policy_score_band.v1"
        or confidence["method_version"] != "v1"
        or confidence["is_probability"] is not False
        or confidence["score_band"] not in {"low", "medium", "high"}
    ):
        raise ValueError("prediction_record confidence method is invalid")
    _required_sha256(confidence["code_hash"], "prediction confidence code hash")
    _required_sha256(confidence["spec_hash"], "prediction confidence spec hash")
    inputs = _exact_mapping(
        confidence["inputs"],
        {"trust_score", "task_fit_score", "interruption_cost"},
        "prediction confidence inputs",
    )
    for field_name in inputs:
        _finite_float(inputs[field_name], f"prediction confidence {field_name}")
    if any(float(inputs[key]) != float(source[key]) for key in inputs):
        raise ValueError("prediction_record confidence/source input mismatch")

    window = _exact_mapping(
        payload["evaluation_window"],
        {"starts_at", "ends_at", "timezone", "maturity_policy", "config_hash"},
        "prediction evaluation window",
    )
    starts_at = _parse_timestamp(window["starts_at"], "prediction starts_at")
    ends_at = _parse_timestamp(window["ends_at"], "prediction ends_at")
    if ends_at <= starts_at or window["timezone"] != "UTC":
        raise ValueError("prediction_record evaluation window is invalid")
    if window["maturity_policy"] != "close_at_window_end.v1":
        raise ValueError("prediction_record maturity policy is invalid")
    _required_sha256(window["config_hash"], "prediction window config hash")
    causal = tuple(str(value) for value in payload["causal_assumptions"])
    if not causal or causal != tuple(sorted(set(causal))) or any(not value for value in causal):
        raise ValueError("prediction_record causal assumptions are invalid")

    exposure = _exact_mapping(
        payload["exposure"],
        {"status", "evidence_refs"},
        "prediction exposure",
    )
    if exposure["status"] not in {"unproven", "proven", "not_exposed"}:
        raise ValueError("prediction_record exposure status is invalid")
    if not isinstance(exposure["evidence_refs"], (list, tuple)):
        raise ValueError("prediction_record exposure refs must be a sequence")
    outcome_ref = _exact_mapping(
        payload["outcome_ref"],
        {"revision_id", "payload_hash"},
        "prediction outcome ref",
    )
    attribution = _exact_mapping(
        payload["attribution"],
        {"method", "competing_causes"},
        "prediction attribution",
    )
    if not isinstance(attribution["competing_causes"], (list, tuple)):
        raise ValueError("prediction_record competing causes must be a sequence")
    for item in attribution["competing_causes"]:
        cause = _exact_mapping(
            item,
            {"cause", "evidence_refs"},
            "prediction competing cause",
        )
        _required_text(cause["cause"], "prediction competing cause")
        if not isinstance(cause["evidence_refs"], (list, tuple)) or not cause["evidence_refs"]:
            raise ValueError("prediction competing cause lacks evidence")
        for ref in cause["evidence_refs"]:
            _required_text(ref, "prediction competing cause evidence")
    terminal = _exact_mapping(
        payload["terminal"],
        {"state", "reason", "evaluated_at"},
        "prediction terminal",
    )
    error = _exact_mapping(payload["error"], {"kind", "value"}, "prediction error")
    calibration = _exact_mapping(
        payload["calibration"],
        {"eligible", "exclusion_reason"},
        "prediction calibration",
    )
    if revision_state == "open":
        if terminal != {"state": "open", "reason": "", "evaluated_at": ""}:
            raise ValueError("open prediction has terminal evidence")
        if outcome_ref != {"revision_id": "", "payload_hash": ""}:
            raise ValueError("open prediction has an outcome")
        if error != {"kind": "none", "value": None}:
            raise ValueError("open prediction has an error")
        if calibration["eligible"] is not False or calibration["exclusion_reason"] != "open":
            raise ValueError("open prediction calibration state is invalid")
        return
    state = str(terminal["state"])
    if state not in _PREDICTION_TERMINAL_STATES:
        raise ValueError("prediction_record terminal state is invalid")
    _required_text(terminal["reason"], "prediction terminal reason")
    evaluated_at = _parse_timestamp(terminal["evaluated_at"], "prediction evaluated_at")
    if evaluated_at < ends_at and state not in {"measured", "confounded"}:
        raise ValueError("prediction_record closed before maturity")
    if not payload["supersedes_revision_id"]:
        raise ValueError("terminal prediction must supersede its open revision")
    if state in {"measured", "confounded"}:
        if not str(outcome_ref["revision_id"]).startswith("cogrev-"):
            raise ValueError("terminal prediction outcome revision is invalid")
        _required_sha256(outcome_ref["payload_hash"], "prediction outcome hash")
        if error["kind"] != "categorical_miss" or error["value"] not in {0, 1}:
            raise ValueError("prediction_record categorical error is invalid")
        if (state == "measured" and attribution["competing_causes"]) or (
            state == "confounded" and not attribution["competing_causes"]
        ):
            raise ValueError("prediction_record confounded evidence is inconsistent")
    elif outcome_ref != {"revision_id": "", "payload_hash": ""} or error != {
        "kind": "none",
        "value": None,
    }:
        raise ValueError("non-measured prediction cannot claim an outcome/error")
    expected_eligible = state == "measured" and not attribution["competing_causes"]
    if calibration["eligible"] is not expected_eligible:
        raise ValueError("prediction_record calibration eligibility mismatch")
    if expected_eligible and calibration["exclusion_reason"]:
        raise ValueError("eligible prediction cannot carry an exclusion reason")
    if not expected_eligible and not calibration["exclusion_reason"]:
        raise ValueError("excluded prediction requires a reason")


def _validate_outcome_measurement_payload(payload: Mapping[str, Any]) -> None:
    if payload["schema_version"] != COGNITIVE_OBJECT_SCHEMA_VERSIONS["outcome_measurement"]:
        raise ValueError("outcome_measurement schema_version mismatch")
    if not re.fullmatch(r"outcome-[0-9a-f]{32}", str(payload["outcome_id"])):
        raise ValueError("outcome_measurement.outcome_id is invalid")
    prediction_ref = _exact_mapping(
        payload["prediction_ref"],
        {"prediction_id", "revision_id", "prediction_input_hash"},
        "outcome prediction ref",
    )
    if not re.fullmatch(r"prediction-[0-9a-f]{32}", str(prediction_ref["prediction_id"])):
        raise ValueError("outcome_measurement prediction identity is invalid")
    if not str(prediction_ref["revision_id"]).startswith("cogrev-"):
        raise ValueError("outcome_measurement prediction revision is invalid")
    _required_sha256(
        prediction_ref["prediction_input_hash"],
        "outcome prediction input hash",
    )
    decision_ref = _exact_mapping(
        payload["decision_ref"],
        {"kind", "decision_id", "revision_id", "revision_hash"},
        "outcome decision ref",
    )
    if decision_ref["kind"] not in {"decision_trace", "trust_decision"}:
        raise ValueError("outcome decision ref kind is invalid")
    _required_text(decision_ref["decision_id"], "outcome decision id")
    if decision_ref["revision_id"]:
        if not str(decision_ref["revision_id"]).startswith("cogrev-"):
            raise ValueError("outcome decision revision is invalid")
        _required_sha256(decision_ref["revision_hash"], "outcome decision hash")
    elif decision_ref["revision_hash"]:
        raise ValueError("outcome decision hash lacks its revision")
    _exact_mapping(
        payload["action_ref"],
        {"action_id", "effect_id"},
        "outcome action ref",
    )
    delivery_ref = _exact_mapping(
        payload["delivery_ref"],
        {"event_id", "event_payload_hash"},
        "outcome delivery ref",
    )
    _required_text(delivery_ref["event_id"], "outcome delivery event id")
    _required_sha256(delivery_ref["event_payload_hash"], "outcome delivery hash")
    presentation_ref = _exact_mapping(
        payload["presentation_ref"],
        {
            "state",
            "receipt_hash",
            "rendered_content_hash",
            "delivery_event_hash",
        },
        "outcome presentation ref",
    )
    if presentation_ref["state"] != "available":
        raise ValueError("outcome presentation must be acknowledged")
    _required_sha256(presentation_ref["receipt_hash"], "outcome presentation receipt")
    _required_sha256(
        presentation_ref["rendered_content_hash"],
        "outcome presentation content hash",
    )
    _required_sha256(
        presentation_ref["delivery_event_hash"],
        "outcome presentation delivery hash",
    )
    subject = _exact_mapping(payload["subject"], {"type", "id"}, "outcome subject")
    _required_text(subject["type"], "outcome subject type")
    _required_text(subject["id"], "outcome subject id")
    metric = _exact_mapping(payload["metric"], {"metric_id", "unit"}, "outcome metric")
    if metric != {
        "metric_id": "predictive_delivery_usefulness",
        "unit": "class_label",
    }:
        raise ValueError("outcome_measurement metric/unit is invalid")
    if payload["baseline"] not in {"useful", "not_useful", "unknown"}:
        raise ValueError("outcome_measurement baseline is invalid")
    if payload["observed_value"] not in {"useful", "not_useful"}:
        raise ValueError("outcome_measurement observed_value is invalid")
    observation = _exact_mapping(
        payload["observation_window"],
        {"starts_at", "ends_at"},
        "outcome observation window",
    )
    starts_at = _parse_timestamp(observation["starts_at"], "outcome starts_at")
    ends_at = _parse_timestamp(observation["ends_at"], "outcome ends_at")
    if ends_at < starts_at:
        raise ValueError("outcome_measurement observation window is invalid")
    maturity = _exact_mapping(
        payload["maturity"],
        {"matured_at", "is_mature"},
        "outcome maturity",
    )
    matured_at = _parse_timestamp(maturity["matured_at"], "outcome matured_at")
    if maturity["is_mature"] is not True or matured_at < ends_at:
        raise ValueError("outcome_measurement is not mature")
    raw_evidence = _exact_mapping(
        payload["raw_evidence"],
        {"refs", "content_hashes"},
        "outcome raw evidence",
    )
    if (
        not isinstance(raw_evidence["refs"], (list, tuple))
        or not raw_evidence["refs"]
        or not isinstance(raw_evidence["content_hashes"], (list, tuple))
        or len(raw_evidence["refs"]) != len(raw_evidence["content_hashes"])
    ):
        raise ValueError("outcome_measurement raw evidence is incomplete")
    for value in raw_evidence["content_hashes"]:
        _required_sha256(value, "outcome raw evidence hash")
    method = _exact_mapping(
        payload["measurement_method"],
        {
            "method",
            "version",
            "code_hash",
            "registry_hash",
            "source_kind",
            "source_uri",
            "attestation_hash",
        },
        "outcome measurement method",
    )
    if (
        method["method"] != "task_result_oracle"
        or method["version"] != "v1"
        or method["source_kind"] != "objective_measurement"
        or not str(method["source_uri"]).startswith("oracle://")
    ):
        raise ValueError("outcome measurement source registry binding is invalid")
    _required_sha256(method["code_hash"], "outcome measurement code hash")
    _required_sha256(method["registry_hash"], "outcome measurement registry hash")
    _required_sha256(method["attestation_hash"], "outcome measurement attestation hash")
    uncertainty = _exact_mapping(
        payload["uncertainty"],
        {"kind", "value"},
        "outcome uncertainty",
    )
    _required_text(uncertainty["kind"], "outcome uncertainty kind")
    if uncertainty["value"] is not None:
        _finite_float(uncertainty["value"], "outcome uncertainty value")
    attribution = _exact_mapping(
        payload["attribution"],
        {"method", "confidence", "competing_causes", "evidence_refs"},
        "outcome attribution",
    )
    _required_text(attribution["method"], "outcome attribution method")
    confidence = _finite_float(attribution["confidence"], "outcome attribution confidence")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("outcome attribution confidence is invalid")
    if not isinstance(attribution["competing_causes"], (list, tuple)) or not isinstance(
        attribution["evidence_refs"], (list, tuple)
    ):
        raise ValueError("outcome attribution evidence is invalid")
    attribution_refs = tuple(str(value) for value in attribution["evidence_refs"])
    if any(not value for value in attribution_refs):
        raise ValueError("outcome attribution evidence contains a blank value")
    if not set(attribution_refs).issubset(set(str(value) for value in raw_evidence["refs"])):
        raise ValueError("outcome attribution evidence is absent from raw evidence")
    seen_causes: set[str] = set()
    for item in attribution["competing_causes"]:
        cause = _exact_mapping(
            item,
            {"cause", "evidence_refs"},
            "outcome competing cause",
        )
        cause_text = _required_text(cause["cause"], "outcome competing cause")
        if cause_text in seen_causes:
            raise ValueError("outcome competing cause is duplicated")
        seen_causes.add(cause_text)
        if not isinstance(cause["evidence_refs"], (list, tuple)):
            raise ValueError("outcome competing-cause evidence must be a sequence")
        cause_refs = tuple(str(value) for value in cause["evidence_refs"])
        if not cause_refs or any(not value for value in cause_refs):
            raise ValueError("confounded outcome requires per-cause evidence")
        if not set(cause_refs).issubset(set(attribution_refs)):
            raise ValueError("competing-cause evidence is absent from attribution evidence")
    authority = _exact_mapping(
        payload["source_authority"],
        {
            "source_authority_id",
            "source_authority_catalog_hash",
            "source_authority_catalog",
            "source_authority_entry",
            "authority",
            "source_id",
            "source_revision_id",
            "content_hash",
        },
        "outcome source authority",
    )
    if not str(authority["source_authority_id"]).startswith("source-authority:"):
        raise ValueError("outcome source authority id is invalid")
    _required_sha256(
        authority["source_authority_catalog_hash"],
        "outcome source authority catalog hash",
    )
    catalog = _exact_mapping(
        authority["source_authority_catalog"],
        {"schema_version", "entries", "rejected_count", "rejection_codes"},
        "outcome source authority catalog",
    )
    if (
        catalog["schema_version"] != "mnemos.source_authority_catalog.v1"
        or catalog["rejected_count"] != 0
        or catalog["rejection_codes"] != []
        or not isinstance(catalog["entries"], (list, tuple))
        or len(catalog["entries"]) != 1
        or sha256_json(catalog) != authority["source_authority_catalog_hash"]
    ):
        raise ValueError("outcome source authority catalog is invalid")
    selected_entries = [
        entry
        for entry in catalog["entries"]
        if isinstance(entry, Mapping)
        and entry.get("source_authority_id") == authority["source_authority_id"]
    ]
    if len(selected_entries) != 1 or selected_entries[0] != authority["source_authority_entry"]:
        raise ValueError("outcome source authority selection is invalid")
    selected = _exact_mapping(
        authority["source_authority_entry"],
        {
            "source_authority_id",
            "source_authority",
            "source_event_id",
            "role",
            "purpose",
            "content_sha256",
            "span_start",
            "span_end",
            "span_status",
            "source_revision_sha256",
            "artifact_ref_id",
            "allows_cognitive_update",
        },
        "outcome source authority entry",
    )
    expected_role = {
        "system_policy": "system",
        "project_contract": "system",
        "explicit_user": "user",
        "tool_observation": "tool",
    }.get(str(authority["authority"]))
    entry_identity = {
        "source_event_id": selected["source_event_id"],
        "role": selected["role"],
        "authority": selected["source_authority"],
        "span_start": selected["span_start"],
        "span_end": selected["span_end"],
        "content_sha256": selected["content_sha256"],
        "ordinal": 1,
        "segment_ordinal": 1,
    }
    expected_authority_id = "source-authority:" + sha256_json(entry_identity).split(":", 1)[1][:32]
    if (
        selected["source_authority_id"] != expected_authority_id
        or selected["source_authority"] != authority["authority"]
        or selected["source_event_id"] != authority["source_revision_id"]
        or selected["source_revision_sha256"] != authority["content_hash"]
        or selected["span_status"] != "exact"
        or int(selected["span_start"]) != 0
        or int(selected["span_end"]) <= 0
        or selected["role"] != expected_role
        or selected["artifact_ref_id"]
    ):
        raise ValueError("outcome source authority raw-span binding is invalid")
    if authority["authority"] != "tool_observation":
        raise ValueError("outcome source authority is ineligible")
    for field_name in ("source_id", "source_revision_id"):
        _required_text(authority[field_name], f"outcome authority {field_name}")
    _required_sha256(authority["content_hash"], "outcome authority content hash")
    for field_name in ("supersedes_revision_id", "correction_of_revision_id"):
        value = str(payload[field_name] or "")
        if value and not value.startswith("cogrev-"):
            raise ValueError(f"outcome_measurement {field_name} is invalid")
    if (
        bool(payload["supersedes_revision_id"]) != bool(payload["correction_of_revision_id"])
        or payload["supersedes_revision_id"] != payload["correction_of_revision_id"]
    ):
        raise ValueError("outcome_measurement correction lineage is invalid")


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    # A canonical JSON roundtrip both proves serializability and detaches the
    # immutable contract from caller-owned mutable dictionaries.
    parsed = json.loads(canonical_json(value))
    if not isinstance(parsed, dict):
        raise ValueError("canonical payload must be an object")
    return MappingProxyType(parsed)


@dataclass(frozen=True)
class CognitiveStateRevision:
    """One immutable, typed semantic revision with exact source lineage."""

    revision_id: str
    object_type: str
    object_id: str
    schema_version: str
    source_event_id: str
    source_revision_id: str
    source_content_hash: str
    scope_type: str
    scope_id: str
    evidence_refs: tuple[str, ...]
    payload: Mapping[str, Any]
    payload_hash: str
    evidence_hash: str
    supersedes_revision_id: str = ""
    correction_of_revision_id: str = ""
    created_at: str = ""
    admission_state: str = "active"
    redaction_policy: str = REDACTION_POLICY
    redaction_counts: tuple[tuple[str, int], ...] = ()
    contract_version: str = COGNITIVE_STATE_CONTRACT_VERSION

    @classmethod
    def create(
        cls,
        *,
        object_type: str,
        object_id: str,
        source_event_id: str,
        source_revision_id: str,
        source_content_hash: str,
        scope_type: str,
        scope_id: str,
        evidence_refs: Sequence[str],
        payload: Mapping[str, Any],
        supersedes_revision_id: str = "",
        correction_of_revision_id: str = "",
        created_at: str = "",
    ) -> "CognitiveStateRevision":
        normalized_type = _required_text(object_type, "object_type")
        if normalized_type not in COGNITIVE_OBJECT_TYPES:
            raise ValueError(f"unsupported cognitive object type: {normalized_type}")
        normalized_object_id = _required_text(object_id, "object_id")
        normalized_event_id = _required_text(source_event_id, "source_event_id")
        normalized_source_revision = _required_text(
            source_revision_id,
            "source_revision_id",
        )
        normalized_source_hash = _required_text(
            source_content_hash,
            "source_content_hash",
        )
        normalized_scope_type = _required_text(scope_type, "scope_type")
        normalized_scope_id = _required_text(scope_id, "scope_id")
        normalized_evidence = _string_tuple(evidence_refs, "evidence_refs")
        if not normalized_evidence:
            raise ValueError("evidence_refs must be non-empty")
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be an object")
        if (
            normalized_type == "cognition_episode"
            and payload.get("schema_version") == COGNITIVE_OBJECT_SCHEMA_VERSIONS[normalized_type]
            and payload.get("claim_catalog_hash") != sha256_json(list(payload.get("claims") or ()))
        ):
            raise ValueError("cognition_episode claim catalog hash mismatch")
        redacted = redact_persistence_value(dict(payload))
        if not isinstance(redacted.value, Mapping):
            raise ValueError("redacted payload must be an object")
        redacted_payload = dict(redacted.value)
        _rebind_redacted_payload_identity(normalized_type, redacted_payload)
        frozen_payload = _frozen_mapping(redacted_payload)
        _validate_payload(normalized_type, frozen_payload)
        if (
            normalized_type == "cognition_episode"
            and frozen_payload["schema_version"]
            != COGNITIVE_OBJECT_SCHEMA_VERSIONS[normalized_type]
        ):
            raise ValueError(f"{normalized_type} new revisions must use the current schema_version")
        validate_cognitive_access_envelope(
            frozen_payload["access_control"],
            expected_scope_type=normalized_scope_type,
            expected_scope_id=normalized_scope_id,
        )
        payload_hash = sha256_json(frozen_payload)
        evidence_hash = sha256_json(list(normalized_evidence))
        identity = {
            "object_type": normalized_type,
            "object_id": normalized_object_id,
            "schema_version": COGNITIVE_OBJECT_SCHEMA_VERSIONS[normalized_type],
            "source_event_id": normalized_event_id,
            "source_revision_id": normalized_source_revision,
            "source_content_hash": normalized_source_hash,
            "scope_type": normalized_scope_type,
            "scope_id": normalized_scope_id,
            "evidence_hash": evidence_hash,
            "payload_hash": payload_hash,
            "supersedes_revision_id": str(supersedes_revision_id or ""),
            "correction_of_revision_id": str(correction_of_revision_id or ""),
        }
        revision_id = "cogrev-" + sha256_json(identity).split(":", 1)[1][:32]
        return cls(
            revision_id=revision_id,
            object_type=normalized_type,
            object_id=normalized_object_id,
            schema_version=COGNITIVE_OBJECT_SCHEMA_VERSIONS[normalized_type],
            source_event_id=normalized_event_id,
            source_revision_id=normalized_source_revision,
            source_content_hash=normalized_source_hash,
            scope_type=normalized_scope_type,
            scope_id=normalized_scope_id,
            evidence_refs=normalized_evidence,
            payload=frozen_payload,
            payload_hash=payload_hash,
            evidence_hash=evidence_hash,
            supersedes_revision_id=str(supersedes_revision_id or ""),
            correction_of_revision_id=str(correction_of_revision_id or ""),
            created_at=created_at or now_utc(),
            admission_state="active",
            redaction_counts=redacted.counts,
        )

    def canonical_payload(self) -> dict[str, Any]:
        return dict(self.payload)


@dataclass(frozen=True)
class LocalConsumerCommand:
    """Immutable local-outbox command emitted by the semantic transaction."""

    command_id: str
    revision_id: str
    consumer_id: str
    command_type: str
    payload: Mapping[str, Any]
    payload_hash: str
    created_at: str

    @classmethod
    def create(
        cls,
        *,
        revision_id: str,
        consumer_id: str,
        command_type: str,
        payload: Mapping[str, Any],
        created_at: str = "",
    ) -> "LocalConsumerCommand":
        normalized_revision = _required_text(revision_id, "revision_id")
        normalized_consumer = _required_text(consumer_id, "consumer_id")
        normalized_type = _required_text(command_type, "command_type")
        if not isinstance(payload, Mapping):
            raise ValueError("command payload must be an object")
        redacted = redact_persistence_value(dict(payload))
        if not isinstance(redacted.value, Mapping):
            raise ValueError("redacted command payload must be an object")
        frozen_payload = _frozen_mapping(redacted.value)
        payload_hash = sha256_json(frozen_payload)
        identity = {
            "revision_id": normalized_revision,
            "consumer_id": normalized_consumer,
            "command_type": normalized_type,
            "payload_hash": payload_hash,
        }
        command_id = "cogcmd-" + sha256_json(identity).split(":", 1)[1][:32]
        return cls(
            command_id=command_id,
            revision_id=normalized_revision,
            consumer_id=normalized_consumer,
            command_type=normalized_type,
            payload=frozen_payload,
            payload_hash=payload_hash,
            created_at=created_at or now_utc(),
        )


@dataclass(frozen=True)
class CognitiveStateCommitReceipt:
    """Receipt proving the semantic/envelope/outbox transaction boundary."""

    status: str
    event_id: str
    revision_ids: tuple[str, ...]
    outbox_ids: tuple[str, ...]
    transaction_hash: str


@dataclass(frozen=True)
class CognitiveHeadPrecondition:
    """Exact current-head identity observed before a semantic transaction."""

    object_type: str
    object_id: str
    revision_id: str

    @classmethod
    def create(
        cls,
        *,
        object_type: str,
        object_id: str,
        revision_id: str,
    ) -> "CognitiveHeadPrecondition":
        """Validate and construct an exact current-head precondition."""

        normalized_type = _required_text(object_type, "object_type")
        if normalized_type not in COGNITIVE_OBJECT_TYPES:
            raise ValueError(f"unsupported cognitive object type: {normalized_type}")
        normalized_revision = _required_text(revision_id, "revision_id")
        if not normalized_revision.startswith("cogrev-"):
            raise ValueError("revision_id is not a cognitive revision identity")
        return cls(
            object_type=normalized_type,
            object_id=_required_text(object_id, "object_id"),
            revision_id=normalized_revision,
        )
