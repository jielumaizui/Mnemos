"""Immutable semantic contracts for governed training objects.

This module owns value validation and deterministic derivation only. Runtime
admission, oracle revalidation, persistence, and model effects belong to
``TrainingGovernanceStore``; callers cannot use these helpers to bypass it.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from core.evidence.artifact_catalog import require_sha256_file


TRAINING_ADMISSION_SCHEMA_VERSION = "mnemos.training_admission_record.v2"
TRAINING_ADMISSION_INTAKE_SCHEMA_VERSION = (
    "mnemos.governed_training_admission_intake.v1"
)
TRAINING_ADMISSION_CONSUMER = "governed_training_admission"
TRAINING_ADMISSION_COMMAND = "admit_governed_training_evidence"
TRAINING_ADMISSION_SUPERSEDED_REASON = (
    "training_admission_intake_superseded_by_outcome_correction"
)
TRAINING_RUN_SCHEMA_VERSION = "mnemos.training_run_record.v1"
TRAINING_DIMENSION = "predictive_delivery"
TRAINING_METRIC = "predictive_delivery_usefulness"
TRAINING_UNIT = "class_label"
FEATURE_EXTRACTOR_VERSION = "mnemos.predictive_delivery_features.v1"
LABELER_VERSION = "mnemos.predictive_delivery_labeler.v1"
TRAINING_ALGORITHM_VERSION = "mnemos.governed_binary_centroid.v1"
BAYESIAN_PRIOR_VERSION = "mnemos.governed_bayesian_prior.v1"
RULE_OPTIMIZER_VERSION = "mnemos.governed_rule_optimizer.v1"
READINESS_POLICY_VERSION = "mnemos.training_readiness_policy.v1"
SPLIT_POLICY_VERSION = "mnemos.training_split_policy.v1"
SPLIT_POLICY_NAMESPACE = "mnemos:predictive_delivery:global:v1"
SPLIT_POLICY_SPEC = {
    "namespace": SPLIT_POLICY_NAMESPACE,
    "group_fields": ["metric_id", "scope", "subject"],
    "bucket_modulus": 100,
    "ranges": {"train": [0, 80], "validation": [80, 90], "holdout": [90, 100]},
}
FEATURE_NAMES = (
    "causal_assumption_count",
    "confidence_high",
    "confidence_low",
    "confidence_medium",
    "interruption_cost",
    "predicted_useful",
    "route_deliver",
    "source_snapshot_hash_bucket",
    "task_fit_score",
    "trust_score",
    "window_seconds",
)
FEATURE_EXTRACTOR_SPEC = {
    "schema_version": FEATURE_EXTRACTOR_VERSION,
    "dimension": TRAINING_DIMENSION,
    "source": "sealed_prediction_input_only",
    "feature_names": list(FEATURE_NAMES),
    "prohibited_sources": [
        "calibration",
        "outcome_measurement",
        "reaction",
        "terminal",
    ],
}
LABELER_SPEC = {
    "schema_version": LABELER_VERSION,
    "metric_id": TRAINING_METRIC,
    "unit": TRAINING_UNIT,
    "labels": {"not_useful": 0, "useful": 1},
    "source": "verified_outcome_measurement_only",
}
TRAINING_ALGORITHM_SPEC = {
    "schema_version": TRAINING_ALGORITHM_VERSION,
    "name": "governed_binary_centroid",
    "feature_names": list(FEATURE_NAMES),
    "fit_splits": ["train"],
    "selection_splits": ["train"],
    "validation_role": "report_only",
    "holdout_role": "post_seal_report_only",
    "auxiliary_train_effects": ["bayesian_prior", "rule_optimizer"],
}
READINESS_POLICY_SPEC = {
    "schema_version": READINESS_POLICY_VERSION,
    "minimum_train": 20,
    "minimum_train_per_class": 2,
    "required_train_classes": [0, 1],
    "minimum_validation": 2,
    "minimum_holdout": 2,
}
TRAINING_ADMISSION_INTAKE_SPEC = {
    "schema_version": TRAINING_ADMISSION_INTAKE_SCHEMA_VERSION,
    "admission_schema_version": TRAINING_ADMISSION_SCHEMA_VERSION,
    "consumer_id": TRAINING_ADMISSION_CONSUMER,
    "command_type": TRAINING_ADMISSION_COMMAND,
    "required_refs": [
        "attribution_ref",
        "outcome_ref",
        "training_target_ref",
        "required_feedback_commands",
        "source_identity",
        "source_access",
        "source_authority_refs",
        "correction_lineage",
    ],
    "replay_principal": "derive_from_committed_source_access",
}
_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


TRAINING_CONTRACT_CODE_HASH = "sha256:" + require_sha256_file(Path(__file__))
FEATURE_EXTRACTOR_CODE_HASH = TRAINING_CONTRACT_CODE_HASH
FEATURE_EXTRACTOR_SPEC_HASH = _sha256_json(FEATURE_EXTRACTOR_SPEC)
FEATURE_EXTRACTOR_CONFIG_HASH = _sha256_json(
    {"schema_version": FEATURE_EXTRACTOR_VERSION, "normalization": "fixed_v1"}
)
LABELER_CODE_HASH = TRAINING_CONTRACT_CODE_HASH
LABELER_SPEC_HASH = _sha256_json(LABELER_SPEC)
LABELER_CONFIG_HASH = _sha256_json({"schema_version": LABELER_VERSION, "unknown_labels": "reject"})
TRAINING_ALGORITHM_CODE_HASH = TRAINING_CONTRACT_CODE_HASH
TRAINING_ALGORITHM_SPEC_HASH = _sha256_json(TRAINING_ALGORITHM_SPEC)
TRAINING_ALGORITHM_CONFIG_HASH = _sha256_json(
    {"schema_version": TRAINING_ALGORITHM_VERSION, "distance": "squared_euclidean"}
)
READINESS_POLICY_HASH = _sha256_json(READINESS_POLICY_SPEC)
TRAINING_ADMISSION_INTAKE_CONTRACT_HASH = _sha256_json(
    TRAINING_ADMISSION_INTAKE_SPEC
)


def training_admission_intake_command_key(payload: Mapping[str, Any]) -> str:
    """Derive the immutable intake key without trusting a caller-supplied ID."""

    identity = {
        key: value
        for key, value in payload.items()
        if key != "command_key"
    }
    return (
        "training-admission-intake:"
        + _sha256_json(identity).split(":", 1)[1][:32]
    )


def validate_training_admission_intake_payload(
    payload: Mapping[str, Any],
) -> None:
    """Validate one durable objective-attribution admission obligation."""

    expected_fields = {
        "schema_version",
        "contract_hash",
        "command_key",
        "attribution_ref",
        "outcome_ref",
        "training_target_ref",
        "required_feedback_commands",
        "source_identity",
        "source_access",
        "source_authority_refs",
        "correction_lineage",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected_fields:
        raise ValueError("training admission intake fields are invalid")
    if (
        payload["schema_version"] != TRAINING_ADMISSION_INTAKE_SCHEMA_VERSION
        or payload["contract_hash"]
        != TRAINING_ADMISSION_INTAKE_CONTRACT_HASH
        or payload["command_key"]
        != training_admission_intake_command_key(payload)
    ):
        raise ValueError("training admission intake contract mismatch")

    attribution_ref = _exact_mapping(
        payload["attribution_ref"],
        {"object_id", "revision_id", "payload_hash"},
        "training admission attribution_ref",
    )
    outcome_ref = _exact_mapping(
        payload["outcome_ref"],
        {"object_id", "revision_id", "payload_hash"},
        "training admission outcome_ref",
    )
    for name, reference in (
        ("attribution_ref", attribution_ref),
        ("outcome_ref", outcome_ref),
    ):
        _required_text(reference["object_id"], f"training admission {name}.object_id")
        _required_text(reference["revision_id"], f"training admission {name}.revision_id")
        _required_sha256(reference["payload_hash"], f"training admission {name}.payload_hash")

    training_target = _exact_mapping(
        payload["training_target_ref"],
        {"command_id", "payload_hash"},
        "training admission training_target_ref",
    )
    _required_text(training_target["command_id"], "training admission target command_id")
    _required_sha256(
        training_target["payload_hash"],
        "training admission target payload_hash",
    )

    manifest = payload["required_feedback_commands"]
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("training admission feedback manifest is required")
    normalized_manifest: list[tuple[str, str]] = []
    training_matches = 0
    for raw in manifest:
        row = _exact_mapping(
            raw,
            {"command_id", "consumer_id", "command_type", "payload_hash"},
            "training admission feedback manifest row",
        )
        command_id = _required_text(
            row["command_id"],
            "training admission feedback command_id",
        )
        consumer_id = _required_text(
            row["consumer_id"],
            "training admission feedback consumer_id",
        )
        if row["command_type"] not in {
            "evaluate_feedback_target",
            "neutralize_feedback_effect",
        }:
            raise ValueError("training admission feedback command type is invalid")
        _required_sha256(
            row["payload_hash"],
            "training admission feedback payload_hash",
        )
        normalized_manifest.append((consumer_id, command_id))
        if (
            consumer_id == "training_evidence"
            and command_id == training_target["command_id"]
            and row["payload_hash"] == training_target["payload_hash"]
        ):
            training_matches += 1
    if (
        normalized_manifest != sorted(set(normalized_manifest))
        or training_matches != 1
    ):
        raise ValueError("training admission feedback manifest identity is invalid")

    source_identity = _exact_mapping(
        payload["source_identity"],
        {"principal_id", "agent"},
        "training admission source_identity",
    )
    _required_text(source_identity["principal_id"], "training admission principal_id")
    _required_text(source_identity["agent"], "training admission agent")
    source_access = _exact_mapping(
        payload["source_access"],
        {
            "access_control_hash",
            "scope_type",
            "scope_id",
            "project",
            "session_id",
            "visibility",
            "consent_status",
            "sensitivity",
            "retention_policy",
        },
        "training admission source_access",
    )
    _required_sha256(
        source_access["access_control_hash"],
        "training admission source access hash",
    )
    for field_name in (
        "scope_type",
        "scope_id",
        "visibility",
        "consent_status",
        "sensitivity",
        "retention_policy",
    ):
        _required_text(
            source_access[field_name],
            f"training admission source_access.{field_name}",
        )
    for field_name in ("project", "session_id"):
        if not isinstance(source_access[field_name], str):
            raise ValueError(
                f"training admission source_access.{field_name} must be text"
            )

    source_authority_refs = payload["source_authority_refs"]
    if (
        not isinstance(source_authority_refs, list)
        or not source_authority_refs
        or any(not isinstance(value, str) or not value for value in source_authority_refs)
        or source_authority_refs != sorted(set(source_authority_refs))
    ):
        raise ValueError("training admission source authority refs are invalid")
    correction = _exact_mapping(
        payload["correction_lineage"],
        {"supersedes_revision_id", "correction_of_revision_id"},
        "training admission correction_lineage",
    )
    if any(not isinstance(value, str) for value in correction.values()):
        raise ValueError("training admission correction lineage must be text")


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _required_sha256(value: Any, field_name: str) -> str:
    normalized = _required_text(value, field_name)
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} must be an exact SHA-256 identity")
    return normalized


def _exact_mapping(
    value: Any,
    fields: set[str],
    field_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{field_name} fields are invalid")
    return value


def _finite_float(value: Any, field_name: str) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be finite") from exc
    if not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be finite")
    return normalized


def _timestamp(value: Any, field_name: str) -> datetime:
    normalized = _required_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed


def _identity(value: Any, field_name: str) -> Mapping[str, Any]:
    identity = _exact_mapping(value, {"type", "id"}, field_name)
    _required_text(identity["type"], f"{field_name}.type")
    _required_text(identity["id"], f"{field_name}.id")
    return identity


def split_policy_hash() -> str:
    """Return the immutable public v1 split-policy identity."""

    return _sha256_json({"schema_version": SPLIT_POLICY_VERSION, "policy": SPLIT_POLICY_SPEC})


def derive_dataset_assignment(
    *,
    subject: Mapping[str, Any],
    scope: Mapping[str, Any],
) -> dict[str, Any]:
    """Assign one subject group without consulting labels or outcomes."""

    normalized_subject = dict(_identity(subject, "training subject"))
    normalized_scope = dict(_identity(scope, "training scope"))
    group_identity = {
        "namespace": SPLIT_POLICY_NAMESPACE,
        "metric_id": TRAINING_METRIC,
        "scope": normalized_scope,
        "subject": normalized_subject,
    }
    group_hash = _sha256_json(group_identity)
    bucket = int(group_hash.split(":", 1)[1][:16], 16) % 100
    split = "train" if bucket < 80 else "validation" if bucket < 90 else "holdout"
    assignment = {
        "group_id": "training-group-" + group_hash.split(":", 1)[1][:32],
        "group_hash": group_hash,
        "split": split,
        "bucket": bucket,
        "policy_version": SPLIT_POLICY_VERSION,
        "policy_hash": split_policy_hash(),
    }
    assignment["assignment_proof"] = _sha256_json(assignment)
    return assignment


def derive_feature_snapshot(prediction_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the v1 vector from pre-outcome PredictionRecord fields only."""

    if prediction_payload.get("prediction_kind") != TRAINING_METRIC:
        raise ValueError("training prediction metric is unsupported")
    metric = _exact_mapping(
        prediction_payload.get("metric"),
        {"metric_id", "unit", "predicted_value", "baseline", "measurement_spec"},
        "training prediction metric",
    )
    if metric["metric_id"] != TRAINING_METRIC or metric["unit"] != TRAINING_UNIT:
        raise ValueError("training prediction metric/unit mismatch")
    confidence = prediction_payload.get("confidence")
    if not isinstance(confidence, Mapping):
        raise ValueError("training prediction confidence is unavailable")
    inputs = confidence.get("inputs")
    if not isinstance(inputs, Mapping) or set(inputs) != {
        "trust_score",
        "task_fit_score",
        "interruption_cost",
    }:
        raise ValueError("training prediction confidence inputs are invalid")
    band = str(confidence.get("score_band") or "")
    if band not in {"low", "medium", "high"}:
        raise ValueError("training prediction confidence band is invalid")
    window = prediction_payload.get("evaluation_window")
    if not isinstance(window, Mapping):
        raise ValueError("training prediction evaluation window is unavailable")
    starts = _timestamp(window.get("starts_at"), "training prediction window start")
    ends = _timestamp(window.get("ends_at"), "training prediction window end")
    duration = (ends - starts).total_seconds()
    if duration <= 0:
        raise ValueError("training prediction evaluation window is invalid")
    causal = prediction_payload.get("causal_assumptions")
    if not isinstance(causal, (list, tuple)):
        raise ValueError("training prediction causal assumptions are invalid")
    source = prediction_payload.get("source_snapshot")
    if not isinstance(source, Mapping):
        raise ValueError("training prediction source snapshot is unavailable")
    source_hash = _required_sha256(
        source.get("snapshot_hash"),
        "training prediction source snapshot hash",
    )
    source_bucket = int(source_hash.split(":", 1)[1][:16], 16) / float(2**64 - 1)
    values = {
        "causal_assumption_count": float(len(causal)),
        "confidence_high": 1.0 if band == "high" else 0.0,
        "confidence_low": 1.0 if band == "low" else 0.0,
        "confidence_medium": 1.0 if band == "medium" else 0.0,
        "interruption_cost": _finite_float(
            inputs["interruption_cost"],
            "training interruption_cost",
        ),
        "predicted_useful": 1.0 if metric["predicted_value"] == "useful" else 0.0,
        "route_deliver": (1.0 if prediction_payload.get("route_disposition") == "deliver" else 0.0),
        "source_snapshot_hash_bucket": source_bucket,
        "task_fit_score": _finite_float(inputs["task_fit_score"], "training task_fit_score"),
        "trust_score": _finite_float(inputs["trust_score"], "training trust_score"),
        "window_seconds": duration,
    }
    snapshot = {
        "dimension": TRAINING_DIMENSION,
        "extractor_version": FEATURE_EXTRACTOR_VERSION,
        "extractor_code_hash": FEATURE_EXTRACTOR_CODE_HASH,
        "extractor_spec_hash": FEATURE_EXTRACTOR_SPEC_HASH,
        "config_hash": FEATURE_EXTRACTOR_CONFIG_HASH,
        "source_prediction_input_hash": _required_sha256(
            prediction_payload.get("prediction_input_hash"),
            "training prediction input hash",
        ),
        "values": values,
    }
    snapshot["snapshot_hash"] = _sha256_json(snapshot)
    return snapshot


def derive_training_label(
    outcome_payload: Mapping[str, Any],
    *,
    outcome_revision_id: str,
    outcome_payload_hash: str,
) -> dict[str, Any]:
    """Map one verified objective OutcomeMeasurement to the exact v1 label."""

    metric = outcome_payload.get("metric")
    if (
        not isinstance(metric, Mapping)
        or metric.get("metric_id") != TRAINING_METRIC
        or metric.get("unit") != TRAINING_UNIT
    ):
        raise ValueError("training outcome metric/unit mismatch")
    observed = str(outcome_payload.get("observed_value") or "")
    labels = {"not_useful": 0, "useful": 1}
    if observed not in labels:
        raise ValueError("training outcome label is unsupported")
    label = {
        "metric_id": TRAINING_METRIC,
        "unit": TRAINING_UNIT,
        "observed_value": observed,
        "numeric_value": labels[observed],
        "labeler_version": LABELER_VERSION,
        "labeler_code_hash": LABELER_CODE_HASH,
        "labeler_spec_hash": LABELER_SPEC_HASH,
        "config_hash": LABELER_CONFIG_HASH,
        "outcome_revision_id": _required_text(
            outcome_revision_id,
            "training outcome revision ID",
        ),
        "outcome_payload_hash": _required_sha256(
            outcome_payload_hash,
            "training outcome payload hash",
        ),
    }
    label["derivation_hash"] = _sha256_json(label)
    return label


def training_admission_input_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact evidence/derivation set bound by an admission."""

    return {
        "schema_version": payload.get("schema_version"),
        "admission_id": payload.get("admission_id"),
        "revision_state": payload.get("revision_state"),
        "supersedes_revision_id": payload.get("supersedes_revision_id"),
        "correction_of_revision_id": payload.get("correction_of_revision_id"),
        "training_evidence_ref": payload.get("training_evidence_ref"),
        "prediction_ref": payload.get("prediction_ref"),
        "prediction_terminal_ref": payload.get("prediction_terminal_ref"),
        "outcome_ref": payload.get("outcome_ref"),
        "decision_ref": payload.get("decision_ref"),
        "material_effect_ref": payload.get("material_effect_ref"),
        "delivery_ref": payload.get("delivery_ref"),
        "subject": payload.get("subject"),
        "scope": payload.get("scope"),
        "principal_ref": payload.get("principal_ref"),
        "temporal_proof": payload.get("temporal_proof"),
        "authority_proof": payload.get("authority_proof"),
        "feature_snapshot": payload.get("feature_snapshot"),
        "label": payload.get("label"),
        "evidence_quality": payload.get("evidence_quality"),
        "dataset_assignment": payload.get("dataset_assignment"),
        "lifecycle_state": payload.get("lifecycle_state"),
        "target_effect_refs": payload.get("target_effect_refs"),
        "access_control": payload.get("access_control"),
    }


def training_admission_input_hash(payload: Mapping[str, Any]) -> str:
    """Hash every immutable input that authorizes one training admission."""

    return _sha256_json(training_admission_input_snapshot(payload))


def _validate_ref(
    value: Any,
    *,
    fields: set[str],
    hash_fields: set[str],
    name: str,
) -> Mapping[str, Any]:
    ref = _exact_mapping(value, fields, name)
    for field in fields - hash_fields:
        _required_text(ref[field], f"{name}.{field}")
    for field in hash_fields:
        _required_sha256(ref[field], f"{name}.{field}")
    return ref


def validate_training_admission_payload(payload: Mapping[str, Any]) -> None:
    """Validate a fully derived admission; no caller-owned eligibility remains."""

    expected_fields = {
        "access_control",
        "schema_version",
        "admission_id",
        "revision_state",
        "input_set_hash",
        "supersedes_revision_id",
        "correction_of_revision_id",
        "training_evidence_ref",
        "prediction_ref",
        "prediction_terminal_ref",
        "outcome_ref",
        "decision_ref",
        "material_effect_ref",
        "delivery_ref",
        "subject",
        "scope",
        "principal_ref",
        "temporal_proof",
        "authority_proof",
        "feature_snapshot",
        "label",
        "evidence_quality",
        "dataset_assignment",
        "lifecycle_state",
        "target_effect_refs",
    }
    _exact_mapping(payload, expected_fields, "training admission payload")
    if payload["schema_version"] != TRAINING_ADMISSION_SCHEMA_VERSION:
        raise ValueError("training admission schema_version mismatch")
    if not re.fullmatch(r"training-admission-[0-9a-f]{32}", str(payload["admission_id"])):
        raise ValueError("training admission ID is invalid")
    if payload["revision_state"] not in {"active", "superseded", "corrected"}:
        raise ValueError("training admission revision_state is invalid")
    for field in ("supersedes_revision_id", "correction_of_revision_id"):
        value = str(payload[field] or "")
        if value and not re.fullmatch(r"cogrev-[0-9a-f]{32}", value):
            raise ValueError(f"training admission {field} is invalid")

    _validate_ref(
        payload["training_evidence_ref"],
        fields={
            "command_id",
            "command_payload_hash",
            "attribution_revision_id",
            "attribution_payload_hash",
            "proposal_id",
            "proposal_hash",
            "decision_id",
            "action_id",
            "effect_id",
            "receipt_id",
            "receipt_hash",
        },
        hash_fields={
            "command_payload_hash",
            "attribution_payload_hash",
            "proposal_hash",
            "receipt_hash",
        },
        name="training evidence ref",
    )
    prediction = _validate_ref(
        payload["prediction_ref"],
        fields={"object_id", "revision_id", "payload_hash", "input_hash"},
        hash_fields={"payload_hash", "input_hash"},
        name="training prediction ref",
    )
    prediction_terminal = _validate_ref(
        payload["prediction_terminal_ref"],
        fields={
            "object_id",
            "revision_id",
            "payload_hash",
            "terminal_state",
            "outcome_revision_id",
            "outcome_payload_hash",
        },
        hash_fields={"payload_hash", "outcome_payload_hash"},
        name="training terminal prediction ref",
    )
    outcome = _validate_ref(
        payload["outcome_ref"],
        fields={"object_id", "revision_id", "payload_hash", "oracle_receipt_hash"},
        hash_fields={"payload_hash", "oracle_receipt_hash"},
        name="training outcome ref",
    )
    _validate_ref(
        payload["decision_ref"],
        fields={"object_id", "revision_id", "payload_hash"},
        hash_fields={"payload_hash"},
        name="training decision ref",
    )
    _validate_ref(
        payload["material_effect_ref"],
        fields={"action_id", "effect_id", "effect_receipt_id", "effect_receipt_hash"},
        hash_fields={"effect_receipt_hash"},
        name="training material effect ref",
    )
    _validate_ref(
        payload["delivery_ref"],
        fields={"event_id", "payload_hash"},
        hash_fields={"payload_hash"},
        name="training delivery ref",
    )
    subject = _identity(payload["subject"], "training subject")
    scope = _identity(payload["scope"], "training scope")
    principal = _exact_mapping(
        payload["principal_ref"],
        {"principal_id", "authorization_ref"},
        "training principal ref",
    )
    for field in principal:
        _required_text(principal[field], f"training principal ref.{field}")

    temporal = _exact_mapping(
        payload["temporal_proof"],
        {
            "prediction_sealed_at",
            "prediction_terminal_at",
            "effect_committed_at",
            "window_starts_at",
            "window_ends_at",
            "outcome_observed_at",
            "outcome_matured_at",
            "admission_effective_at",
            "maturity",
            "proof_hash",
        },
        "training temporal proof",
    )
    expected_temporal_hash = _sha256_json(
        {key: value for key, value in temporal.items() if key != "proof_hash"}
    )
    if temporal["proof_hash"] != expected_temporal_hash:
        raise ValueError("training temporal proof hash mismatch")
    sealed = _timestamp(temporal["prediction_sealed_at"], "prediction_sealed_at")
    terminal_at = _timestamp(
        temporal["prediction_terminal_at"],
        "prediction_terminal_at",
    )
    committed = _timestamp(temporal["effect_committed_at"], "effect_committed_at")
    starts = _timestamp(temporal["window_starts_at"], "window_starts_at")
    ends = _timestamp(temporal["window_ends_at"], "window_ends_at")
    observed = _timestamp(temporal["outcome_observed_at"], "outcome_observed_at")
    matured = _timestamp(temporal["outcome_matured_at"], "outcome_matured_at")
    admitted_at = _timestamp(
        temporal["admission_effective_at"],
        "admission_effective_at",
    )
    if (
        sealed > committed
        or sealed > starts
        or not starts < observed <= ends
        or matured < observed
        or terminal_at < matured
        or admitted_at < terminal_at
    ):
        raise ValueError("training temporal ordering is invalid")
    if temporal["maturity"] != "mature":
        raise ValueError("training outcome is not mature")
    if (
        prediction_terminal["object_id"] != prediction["object_id"]
        or prediction_terminal["revision_id"] == prediction["revision_id"]
        or prediction_terminal["terminal_state"] != "measured"
        or prediction_terminal["outcome_revision_id"] != outcome["revision_id"]
        or prediction_terminal["outcome_payload_hash"] != outcome["payload_hash"]
    ):
        raise ValueError("training terminal prediction binding mismatch")

    authority = _validate_ref(
        payload["authority_proof"],
        fields={
            "source_authority_catalog_hash",
            "raw_issuance_receipt_ref",
            "raw_issuance_receipt_hash",
        },
        hash_fields={"source_authority_catalog_hash", "raw_issuance_receipt_hash"},
        name="training authority proof",
    )
    if not authority["raw_issuance_receipt_ref"]:
        raise ValueError("training Raw issuance proof is missing")

    feature = _exact_mapping(
        payload["feature_snapshot"],
        {
            "dimension",
            "extractor_version",
            "extractor_code_hash",
            "extractor_spec_hash",
            "config_hash",
            "source_prediction_input_hash",
            "values",
            "snapshot_hash",
        },
        "training feature snapshot",
    )
    if (
        feature["dimension"] != TRAINING_DIMENSION
        or feature["extractor_version"] != FEATURE_EXTRACTOR_VERSION
    ):
        raise ValueError("training feature extractor identity mismatch")
    for field in (
        "extractor_code_hash",
        "extractor_spec_hash",
        "config_hash",
        "source_prediction_input_hash",
    ):
        _required_sha256(feature[field], f"training feature {field}")
    if (
        feature["extractor_code_hash"] != FEATURE_EXTRACTOR_CODE_HASH
        or feature["extractor_spec_hash"] != FEATURE_EXTRACTOR_SPEC_HASH
        or feature["config_hash"] != FEATURE_EXTRACTOR_CONFIG_HASH
    ):
        raise ValueError("training feature extractor registry mismatch")
    if feature["source_prediction_input_hash"] != prediction["input_hash"]:
        raise ValueError("training feature/prediction input binding mismatch")
    values = _exact_mapping(feature["values"], set(FEATURE_NAMES), "training feature values")
    for name, value in values.items():
        normalized = _finite_float(value, f"training feature {name}")
        if (
            name != "window_seconds"
            and name != "causal_assumption_count"
            and not 0 <= normalized <= 1
        ):
            raise ValueError(f"training feature {name} is outside its normalized range")
        if name in {"window_seconds", "causal_assumption_count"} and normalized < 0:
            raise ValueError(f"training feature {name} must be non-negative")
    if feature["snapshot_hash"] != _sha256_json(
        {key: value for key, value in feature.items() if key != "snapshot_hash"}
    ):
        raise ValueError("training feature snapshot hash mismatch")

    label = _exact_mapping(
        payload["label"],
        {
            "metric_id",
            "unit",
            "observed_value",
            "numeric_value",
            "labeler_version",
            "labeler_code_hash",
            "labeler_spec_hash",
            "config_hash",
            "outcome_revision_id",
            "outcome_payload_hash",
            "derivation_hash",
        },
        "training label",
    )
    expected_label = {"useful": 1, "not_useful": 0}
    if (
        label["metric_id"] != TRAINING_METRIC
        or label["unit"] != TRAINING_UNIT
        or label["observed_value"] not in expected_label
        or label["numeric_value"] != expected_label[label["observed_value"]]
        or label["labeler_version"] != LABELER_VERSION
    ):
        raise ValueError("training label registry mismatch")
    for field in ("labeler_code_hash", "labeler_spec_hash", "config_hash", "outcome_payload_hash"):
        _required_sha256(label[field], f"training label {field}")
    if (
        label["labeler_code_hash"] != LABELER_CODE_HASH
        or label["labeler_spec_hash"] != LABELER_SPEC_HASH
        or label["config_hash"] != LABELER_CONFIG_HASH
    ):
        raise ValueError("training labeler registry mismatch")
    if (
        label["outcome_revision_id"] != outcome["revision_id"]
        or label["outcome_payload_hash"] != outcome["payload_hash"]
    ):
        raise ValueError("training label/outcome binding mismatch")
    if label["derivation_hash"] != _sha256_json(
        {key: value for key, value in label.items() if key != "derivation_hash"}
    ):
        raise ValueError("training label derivation hash mismatch")

    quality = _exact_mapping(
        payload["evidence_quality"],
        {
            "uncertainty",
            "attribution",
            "competing_causes",
            "calibration_eligible",
            "exclusion_reason",
        },
        "training evidence quality",
    )
    uncertainty = _finite_float(quality["uncertainty"], "training uncertainty")
    if not 0 <= uncertainty <= 1:
        raise ValueError("training uncertainty is outside [0, 1]")
    if quality["attribution"] != "direct_objective_measurement":
        raise ValueError("training attribution is not objective")
    if not isinstance(quality["competing_causes"], (list, tuple)):
        raise ValueError("training competing causes must be a sequence")
    if quality["calibration_eligible"] is not True:
        raise ValueError("training admission is not calibration eligible")

    lifecycle = payload["lifecycle_state"]
    if lifecycle not in {
        "admitted",
        "excluded",
        "correction_pending",
        "historical_unverified",
    }:
        raise ValueError("training lifecycle state is invalid")
    exclusion_reason = str(quality["exclusion_reason"] or "")
    if lifecycle == "admitted" and exclusion_reason:
        raise ValueError("admitted training example cannot carry an exclusion reason")
    if lifecycle != "admitted" and not exclusion_reason:
        raise ValueError("non-admitted training example requires an exclusion reason")

    expected_assignment = derive_dataset_assignment(subject=subject, scope=scope)
    if dict(payload["dataset_assignment"]) != expected_assignment:
        raise ValueError("training dataset assignment mismatch")
    target_refs = _exact_mapping(
        payload["target_effect_refs"],
        {"projection_command_key", "projection_effect_id", "reciprocal_receipt_id"},
        "training target effect refs",
    )
    for field in target_refs:
        _required_text(target_refs[field], f"training target effect refs.{field}")

    _required_sha256(payload["input_set_hash"], "training input_set_hash")
    if payload["input_set_hash"] != training_admission_input_hash(payload):
        raise ValueError("training admission input-set hash mismatch")


def _normalized_admission_refs(admission_refs: Any) -> list[dict[str, Any]]:
    if not isinstance(admission_refs, (list, tuple)):
        raise ValueError("training admission refs must be a sequence")
    normalized: list[dict[str, Any]] = []
    group_splits: dict[str, str] = {}
    for raw in admission_refs:
        ref = _exact_mapping(
            raw,
            {
                "revision_id",
                "payload_hash",
                "feature_snapshot_hash",
                "label_numeric",
                "split",
                "group_hash",
            },
            "training run admission ref",
        )
        revision_id = _required_text(ref["revision_id"], "training admission revision_id")
        if not re.fullmatch(r"cogrev-[0-9a-f]{32}", revision_id):
            raise ValueError("training admission revision identity is invalid")
        for field in ("payload_hash", "feature_snapshot_hash", "group_hash"):
            _required_sha256(ref[field], f"training admission {field}")
        if ref["label_numeric"] not in {0, 1} or isinstance(ref["label_numeric"], bool):
            raise ValueError("training admission label is invalid")
        split = str(ref["split"])
        if split not in {"train", "validation", "holdout"}:
            raise ValueError("training admission split is invalid")
        group_hash = str(ref["group_hash"])
        previous_split = group_splits.setdefault(group_hash, split)
        if previous_split != split:
            raise ValueError("training subject group crosses dataset splits")
        normalized.append(dict(ref))
    revision_ids = [item["revision_id"] for item in normalized]
    if revision_ids != sorted(set(revision_ids)):
        raise ValueError("training admission refs must be ordered and unique")
    return normalized


def training_dataset_manifest(admission_refs: Any) -> dict[str, Any]:
    """Bind the exact ordered revision set and per-split denominators."""

    refs = _normalized_admission_refs(admission_refs)
    splits = ("train", "validation", "holdout")
    manifest = {
        "admission_revision_ids": [item["revision_id"] for item in refs],
        "counts": {split: sum(item["split"] == split for item in refs) for split in splits},
        "split_hashes": {
            split: _sha256_json([item for item in refs if item["split"] == split])
            for split in splits
        },
    }
    manifest["manifest_hash"] = _sha256_json(manifest)
    return manifest


def training_fit_input_hash(admission_refs: Any) -> str:
    """Hash train examples only; validation and holdout bytes are absent."""

    refs = _normalized_admission_refs(admission_refs)
    return _sha256_json(
        {
            "dimension": TRAINING_DIMENSION,
            "feature_names": list(FEATURE_NAMES),
            "examples": [item for item in refs if item["split"] == "train"],
        }
    )


def governed_training_examples(
    admissions: Any,
) -> list[dict[str, Any]]:
    """Normalize exact train-split revision inputs for auxiliary effects."""

    if not isinstance(admissions, (list, tuple)):
        raise ValueError("governed training admissions must be a sequence")
    examples: list[dict[str, Any]] = []
    for admission in admissions:
        if isinstance(admission, Mapping):
            revision_id_value = admission.get("revision_id", "")
            payload_hash_value = admission.get("payload_hash", "")
            payload = admission.get("payload")
        else:
            revision_id_value = getattr(admission, "revision_id", "")
            payload_hash_value = getattr(admission, "payload_hash", "")
            payload = getattr(admission, "payload", None)
        revision_id = _required_text(
            revision_id_value,
            "governed training admission revision_id",
        )
        payload_hash = _required_sha256(
            payload_hash_value,
            "governed training admission payload_hash",
        )
        if not isinstance(payload, Mapping):
            raise ValueError("governed training admission payload is required")
        assignment = payload.get("dataset_assignment")
        feature = payload.get("feature_snapshot")
        label = payload.get("label")
        if (
            not isinstance(assignment, Mapping)
            or not isinstance(feature, Mapping)
            or not isinstance(label, Mapping)
        ):
            raise ValueError("governed training admission inputs are incomplete")
        if assignment.get("split") != "train":
            continue
        values = feature.get("values")
        if not isinstance(values, Mapping) or set(values) != set(FEATURE_NAMES):
            raise ValueError("governed training feature values are invalid")
        numeric_values = {
            name: _finite_float(values[name], f"governed training feature {name}")
            for name in FEATURE_NAMES
        }
        numeric_label = label.get("numeric_value")
        if numeric_label not in {0, 1} or isinstance(numeric_label, bool):
            raise ValueError("governed training label is invalid")
        examples.append(
            {
                "revision_id": revision_id,
                "payload_hash": payload_hash,
                "feature_snapshot_hash": _required_sha256(
                    feature.get("snapshot_hash"),
                    "governed training feature snapshot_hash",
                ),
                "label_numeric": int(numeric_label),
                "feature_values": numeric_values,
            }
        )
    examples.sort(key=lambda item: item["revision_id"])
    if len({item["revision_id"] for item in examples}) != len(examples):
        raise ValueError("governed training examples are not unique")
    return examples


def training_aux_effect_input_hash(
    examples: Any,
    *,
    effect_kind: str,
) -> str:
    """Hash one auxiliary effect's exact train-only inputs."""

    if effect_kind not in {"bayesian_prior", "rule_optimizer"}:
        raise ValueError("unknown governed training auxiliary effect")
    if not isinstance(examples, (list, tuple)):
        raise ValueError("governed training examples must be a sequence")
    normalized = [dict(item) for item in examples]
    if [item.get("revision_id") for item in normalized] != sorted(
        {str(item.get("revision_id") or "") for item in normalized}
    ):
        raise ValueError("governed training examples must be ordered and unique")
    return _sha256_json(
        {
            "effect_kind": effect_kind,
            "effect_version": (
                BAYESIAN_PRIOR_VERSION
                if effect_kind == "bayesian_prior"
                else RULE_OPTIMIZER_VERSION
            ),
            "dimension": TRAINING_DIMENSION,
            "feature_names": list(FEATURE_NAMES),
            "examples": normalized,
        }
    )


def derive_bayesian_prior_artifact(
    *,
    run_id: str,
    examples: Any,
) -> dict[str, Any]:
    """Rebuild one Beta prior solely from canonical train examples."""

    normalized = [dict(item) for item in examples]
    input_hash = training_aux_effect_input_hash(
        normalized,
        effect_kind="bayesian_prior",
    )
    positives = sum(int(item["label_numeric"]) == 1 for item in normalized)
    negatives = len(normalized) - positives
    effect_id = (
        "governed-bayesian-prior-"
        + _sha256_json({"run_id": run_id, "input_hash": input_hash}).split(":", 1)[1][:32]
    )
    artifact: dict[str, Any] = {
        "schema_version": BAYESIAN_PRIOR_VERSION,
        "effect_kind": "bayesian_prior",
        "effect_id": effect_id,
        "input_hash": input_hash,
        "admission_revision_ids": [item["revision_id"] for item in normalized],
        "dimension": TRAINING_DIMENSION,
        "alpha": float(1 + positives),
        "beta": float(1 + negatives),
        "total_samples": len(normalized),
    }
    artifact["artifact_hash"] = _sha256_json(artifact)
    return artifact


def derive_rule_optimizer_artifact(
    *,
    run_id: str,
    examples: Any,
) -> dict[str, Any]:
    """Derive deterministic feature-rule weights from train examples only."""

    normalized = [dict(item) for item in examples]
    input_hash = training_aux_effect_input_hash(
        normalized,
        effect_kind="rule_optimizer",
    )
    positive = [item for item in normalized if int(item["label_numeric"]) == 1]
    negative = [item for item in normalized if int(item["label_numeric"]) == 0]
    raw_weights: dict[str, float] = {}
    for name in FEATURE_NAMES:
        positive_mean = sum(float(item["feature_values"][name]) for item in positive) / max(
            len(positive), 1
        )
        negative_mean = sum(float(item["feature_values"][name]) for item in negative) / max(
            len(negative), 1
        )
        raw_weights[name] = positive_mean - negative_mean
    scale = sum(abs(value) for value in raw_weights.values()) or 1.0
    weights = {name: raw_weights[name] / scale for name in FEATURE_NAMES}
    effect_id = (
        "governed-rule-optimizer-"
        + _sha256_json({"run_id": run_id, "input_hash": input_hash}).split(":", 1)[1][:32]
    )
    artifact: dict[str, Any] = {
        "schema_version": RULE_OPTIMIZER_VERSION,
        "effect_kind": "rule_optimizer",
        "effect_id": effect_id,
        "input_hash": input_hash,
        "admission_revision_ids": [item["revision_id"] for item in normalized],
        "dimension": TRAINING_DIMENSION,
        "feature_names": list(FEATURE_NAMES),
        "weights": weights,
        "bias": sum(int(item["label_numeric"]) for item in normalized) / max(len(normalized), 1),
        "sample_count": len(normalized),
    }
    artifact["artifact_hash"] = _sha256_json(artifact)
    return artifact


def training_run_input_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the holdout-free immutable input identity for one run."""

    return {
        "schema_version": payload.get("schema_version"),
        "run_id": payload.get("run_id"),
        "dimension": payload.get("dimension"),
        "algorithm": payload.get("algorithm"),
        "admission_refs": payload.get("admission_refs"),
        "dataset_manifest": payload.get("dataset_manifest"),
        "fit_input_hash": payload.get("fit_input_hash"),
        "bayesian_prior_artifact": payload.get("bayesian_prior_artifact"),
        "rule_optimizer_artifact": payload.get("rule_optimizer_artifact"),
        "parent_model_ref": payload.get("parent_model_ref"),
        "supersedes_revision_id": payload.get("supersedes_revision_id"),
        "rebuild_of_revision_id": payload.get("rebuild_of_revision_id"),
        "access_control": payload.get("access_control"),
    }


def training_run_input_hash(payload: Mapping[str, Any]) -> str:
    """Hash the canonical run input snapshot without evaluation outputs."""

    return _sha256_json(training_run_input_snapshot(payload))


def validate_training_run_payload(payload: Mapping[str, Any]) -> None:
    """Validate a sealed run while proving holdout absence from training inputs."""

    expected_fields = {
        "access_control",
        "schema_version",
        "run_id",
        "run_input_hash",
        "dimension",
        "algorithm",
        "admission_refs",
        "dataset_manifest",
        "fit_input_hash",
        "validation_report",
        "holdout_report",
        "parent_model_ref",
        "model_artifact",
        "bayesian_prior_artifact",
        "rule_optimizer_artifact",
        "state",
        "material_effect_refs",
        "projection_receipt_ref",
        "supersedes_revision_id",
        "rebuild_of_revision_id",
    }
    _exact_mapping(payload, expected_fields, "training run payload")
    if payload["schema_version"] != TRAINING_RUN_SCHEMA_VERSION:
        raise ValueError("training run schema_version mismatch")
    if not re.fullmatch(r"training-run-[0-9a-f]{32}", str(payload["run_id"])):
        raise ValueError("training run ID is invalid")
    if payload["dimension"] != TRAINING_DIMENSION:
        raise ValueError("training run dimension is unsupported")
    state = str(payload["state"])
    if state not in {
        "model_sealed",
        "sealed",
        "applied",
        "stale",
        "failed",
        "insufficient_sample",
    }:
        raise ValueError("training run state is invalid")
    for field in ("supersedes_revision_id", "rebuild_of_revision_id"):
        value = str(payload[field] or "")
        if value and not re.fullmatch(r"cogrev-[0-9a-f]{32}", value):
            raise ValueError(f"training run {field} is invalid")

    refs = _normalized_admission_refs(payload["admission_refs"])
    expected_manifest = training_dataset_manifest(refs)
    if dict(payload["dataset_manifest"]) != expected_manifest:
        raise ValueError("training run dataset manifest mismatch")
    expected_fit_hash = training_fit_input_hash(refs)
    if payload["fit_input_hash"] != expected_fit_hash:
        raise ValueError("training run fit input hash mismatch")

    algorithm = _exact_mapping(
        payload["algorithm"],
        {
            "name",
            "version",
            "code_hash",
            "spec_hash",
            "config_hash",
            "readiness_policy_hash",
            "selection_input_hash",
        },
        "training algorithm",
    )
    if (
        algorithm["name"] != "governed_binary_centroid"
        or algorithm["version"] != TRAINING_ALGORITHM_VERSION
    ):
        raise ValueError("training algorithm registry mismatch")
    for field in (
        "code_hash",
        "spec_hash",
        "config_hash",
        "readiness_policy_hash",
        "selection_input_hash",
    ):
        _required_sha256(algorithm[field], f"training algorithm {field}")
    if (
        algorithm["code_hash"] != TRAINING_ALGORITHM_CODE_HASH
        or algorithm["spec_hash"] != TRAINING_ALGORITHM_SPEC_HASH
        or algorithm["config_hash"] != TRAINING_ALGORITHM_CONFIG_HASH
        or algorithm["readiness_policy_hash"] != READINESS_POLICY_HASH
    ):
        raise ValueError("training algorithm registry mismatch")
    if algorithm["selection_input_hash"] != expected_fit_hash:
        raise ValueError("training algorithm selection input includes non-train evidence")

    counts = expected_manifest["counts"]
    train_labels = [item["label_numeric"] for item in refs if item["split"] == "train"]
    ready = (
        counts["train"] >= 20
        and train_labels.count(0) >= 2
        and train_labels.count(1) >= 2
        and counts["validation"] >= 2
        and counts["holdout"] >= 2
    )
    if state == "insufficient_sample" and ready:
        raise ValueError("training run falsely claims insufficient samples")
    if state in {"model_sealed", "sealed", "applied", "failed"} and not ready:
        raise ValueError("training run readiness policy is not satisfied")

    validation = _exact_mapping(
        payload["validation_report"],
        {"example_count", "report_hash"},
        "training validation report",
    )
    holdout = _exact_mapping(
        payload["holdout_report"],
        {"example_count", "report_hash", "evaluated_after_model_sealed_at"},
        "training holdout report",
    )
    if validation["example_count"] != counts["validation"]:
        raise ValueError("training validation denominator mismatch")
    if holdout["example_count"] != counts["holdout"]:
        raise ValueError("training holdout denominator mismatch")
    _required_sha256(validation["report_hash"], "training validation report hash")
    _required_sha256(holdout["report_hash"], "training holdout report hash")

    parent = _exact_mapping(
        payload["parent_model_ref"],
        {"model_id", "model_hash"},
        "training parent model ref",
    )
    if bool(parent["model_id"]) != bool(parent["model_hash"]):
        raise ValueError("training parent model ref is incomplete")
    if parent["model_id"]:
        _required_text(parent["model_id"], "training parent model ID")
        _required_sha256(parent["model_hash"], "training parent model hash")

    artifact = _exact_mapping(
        payload["model_artifact"],
        {"model_id", "model_type", "blob", "blob_hash", "serialization", "sealed_at"},
        "training model artifact",
    )
    artifact_must_be_empty = state == "insufficient_sample" or (state == "stale" and not ready)
    if artifact_must_be_empty:
        if any(artifact[field] for field in artifact):
            raise ValueError("non-model training run cannot claim a model artifact")
    else:
        if not re.fullmatch(r"governed-model-[0-9a-f]{32}", str(artifact["model_id"])):
            raise ValueError("governed model ID is invalid")
        if artifact["model_type"] != "binary_feature_centroid":
            raise ValueError("governed model type is invalid")
        if not isinstance(artifact["blob"], Mapping):
            raise ValueError("governed model blob must be an object")
        if artifact["blob_hash"] != _sha256_json(artifact["blob"]):
            raise ValueError("governed model blob hash mismatch")
        if artifact["serialization"] != "canonical_json_v1":
            raise ValueError("governed model serialization is invalid")
        sealed_at = _timestamp(artifact["sealed_at"], "governed model sealed_at")
        evaluated_value = str(holdout["evaluated_after_model_sealed_at"] or "")
        evaluation_pending = state == "model_sealed" or (state == "stale" and not evaluated_value)
        if evaluation_pending:
            expected_pending_hash = _sha256_json(
                {
                    "status": "pending_after_durable_model_seal",
                    "split": "holdout",
                    "count": counts["holdout"],
                    "model_blob_hash": artifact["blob_hash"],
                }
            )
            if holdout["report_hash"] != expected_pending_hash:
                raise ValueError("training holdout pending report hash mismatch")
        else:
            evaluated_at = _timestamp(
                evaluated_value,
                "training holdout evaluated_at",
            )
            if evaluated_at < sealed_at:
                raise ValueError("training holdout was evaluated before model sealing")

    _validate_aux_artifacts(
        payload,
        train_revision_ids=[item["revision_id"] for item in refs if item["split"] == "train"],
        empty=artifact_must_be_empty,
    )

    effects = _exact_mapping(
        payload["material_effect_refs"],
        {"action_id", "effect_id"},
        "training run material effects",
    )
    receipt = _exact_mapping(
        payload["projection_receipt_ref"],
        {"receipt_id", "receipt_hash"},
        "training run projection receipt",
    )
    for field in effects:
        _required_text(effects[field], f"training run material effects.{field}")
    _required_text(receipt["receipt_id"], "training run receipt ID")
    _required_sha256(receipt["receipt_hash"], "training run receipt hash")

    _required_sha256(payload["run_input_hash"], "training run input hash")
    if payload["run_input_hash"] != training_run_input_hash(payload):
        raise ValueError("training run input hash mismatch")


def _validate_aux_artifacts(
    payload: Mapping[str, Any],
    *,
    train_revision_ids: list[str],
    empty: bool,
) -> None:
    artifacts = (
        ("bayesian_prior", payload["bayesian_prior_artifact"]),
        ("rule_optimizer", payload["rule_optimizer_artifact"]),
    )
    if empty:
        if any(value != {} for _kind, value in artifacts):
            raise ValueError("non-model training run cannot claim auxiliary effects")
        return
    for effect_kind, raw in artifacts:
        expected = {
            "schema_version",
            "effect_kind",
            "effect_id",
            "input_hash",
            "admission_revision_ids",
            "dimension",
            "artifact_hash",
        }
        if effect_kind == "bayesian_prior":
            expected |= {"alpha", "beta", "total_samples"}
        else:
            expected |= {"feature_names", "weights", "bias", "sample_count"}
        artifact_value = _exact_mapping(raw, expected, f"training {effect_kind} artifact")
        if artifact_value["effect_kind"] != effect_kind:
            raise ValueError("governed training auxiliary effect kind mismatch")
        version = (
            BAYESIAN_PRIOR_VERSION if effect_kind == "bayesian_prior" else RULE_OPTIMIZER_VERSION
        )
        if artifact_value["schema_version"] != version:
            raise ValueError("governed training auxiliary effect version mismatch")
        if artifact_value["dimension"] != TRAINING_DIMENSION:
            raise ValueError("governed training auxiliary dimension mismatch")
        if list(artifact_value["admission_revision_ids"]) != train_revision_ids:
            raise ValueError("governed training auxiliary effect includes non-train evidence")
        _required_sha256(artifact_value["input_hash"], "auxiliary effect input hash")
        supplied_hash = _required_sha256(
            artifact_value["artifact_hash"],
            "auxiliary effect artifact hash",
        )
        if supplied_hash != _sha256_json(
            {key: artifact_value[key] for key in artifact_value if key != "artifact_hash"}
        ):
            raise ValueError("governed training auxiliary artifact hash mismatch")
        if effect_kind == "bayesian_prior":
            if (
                _finite_float(artifact_value["alpha"], "bayesian alpha") <= 0
                or _finite_float(artifact_value["beta"], "bayesian beta") <= 0
                or artifact_value["total_samples"] != len(train_revision_ids)
            ):
                raise ValueError("governed Bayesian prior artifact is invalid")
        else:
            if list(artifact_value["feature_names"]) != list(FEATURE_NAMES):
                raise ValueError("governed rule optimizer feature registry mismatch")
            weights = _exact_mapping(
                artifact_value["weights"],
                set(FEATURE_NAMES),
                "governed rule optimizer weights",
            )
            for name in FEATURE_NAMES:
                _finite_float(weights[name], f"governed rule optimizer weight {name}")
            bias = _finite_float(artifact_value["bias"], "governed rule optimizer bias")
            if not 0 <= bias <= 1 or artifact_value["sample_count"] != len(train_revision_ids):
                raise ValueError("governed rule optimizer artifact is invalid")
