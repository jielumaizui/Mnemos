from __future__ import annotations

from copy import deepcopy

import pytest

from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.state_contract import CognitiveStateRevision, sha256_json
from core.cognitive.training_contract import (
    BAYESIAN_PRIOR_VERSION,
    FEATURE_EXTRACTOR_CODE_HASH,
    FEATURE_EXTRACTOR_CONFIG_HASH,
    FEATURE_EXTRACTOR_SPEC_HASH,
    LABELER_CODE_HASH,
    LABELER_CONFIG_HASH,
    LABELER_SPEC_HASH,
    READINESS_POLICY_HASH,
    RULE_OPTIMIZER_VERSION,
    TRAINING_ALGORITHM_CODE_HASH,
    TRAINING_ALGORITHM_CONFIG_HASH,
    TRAINING_ALGORITHM_SPEC_HASH,
    TRAINING_ADMISSION_SCHEMA_VERSION,
    TRAINING_RUN_SCHEMA_VERSION,
    derive_dataset_assignment,
    training_admission_input_hash,
    training_dataset_manifest,
    training_fit_input_hash,
    training_run_input_hash,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _access_control() -> dict:
    return make_cognitive_access_envelope(
        owner_principal_id="mcp:codex:training-test",
        owner_agent="codex",
        scope_type="project",
        scope_id="mnemos",
        project="mnemos",
        purposes=("cognitive_state_read", "cognitive_state_write", "model_training"),
        consent_provenance_refs=("raw-event:training-test",),
        sensitivity="sensitive",
        retention_policy="inherit_source",
        source_acl_lineage=(HASH_A,),
    )


def admission_payload() -> dict:
    subject = {"type": "predictive_delivery_subject", "id": "subject-001"}
    scope = {"type": "project", "id": "mnemos"}
    feature_snapshot = {
        "dimension": "predictive_delivery",
        "extractor_version": "mnemos.predictive_delivery_features.v1",
        "extractor_code_hash": FEATURE_EXTRACTOR_CODE_HASH,
        "extractor_spec_hash": FEATURE_EXTRACTOR_SPEC_HASH,
        "config_hash": FEATURE_EXTRACTOR_CONFIG_HASH,
        "source_prediction_input_hash": HASH_A,
        "values": {
            "causal_assumption_count": 1.0,
            "confidence_high": 1.0,
            "confidence_low": 0.0,
            "confidence_medium": 0.0,
            "interruption_cost": 0.1,
            "predicted_useful": 1.0,
            "route_deliver": 1.0,
            "source_snapshot_hash_bucket": 0.25,
            "task_fit_score": 0.8,
            "trust_score": 0.9,
            "window_seconds": 3600.0,
        },
    }
    feature_snapshot["snapshot_hash"] = sha256_json(feature_snapshot)
    label = {
        "metric_id": "predictive_delivery_usefulness",
        "unit": "class_label",
        "observed_value": "useful",
        "numeric_value": 1,
        "labeler_version": "mnemos.predictive_delivery_labeler.v1",
        "labeler_code_hash": LABELER_CODE_HASH,
        "labeler_spec_hash": LABELER_SPEC_HASH,
        "config_hash": LABELER_CONFIG_HASH,
        "outcome_revision_id": "cogrev-" + "2" * 32,
        "outcome_payload_hash": HASH_B,
    }
    label["derivation_hash"] = sha256_json(label)
    temporal_proof = {
        "prediction_sealed_at": "2026-07-19T00:00:00+00:00",
        "prediction_terminal_at": "2026-07-19T01:00:01+00:00",
        "effect_committed_at": "2026-07-19T00:00:01+00:00",
        "window_starts_at": "2026-07-19T00:00:01+00:00",
        "window_ends_at": "2026-07-19T01:00:01+00:00",
        "outcome_observed_at": "2026-07-19T01:00:01+00:00",
        "outcome_matured_at": "2026-07-19T01:00:01+00:00",
        "admission_effective_at": "2026-07-19T01:00:01+00:00",
        "maturity": "mature",
    }
    temporal_proof["proof_hash"] = sha256_json(temporal_proof)
    payload = {
        "access_control": _access_control(),
        "schema_version": TRAINING_ADMISSION_SCHEMA_VERSION,
        "admission_id": "training-admission-" + "1" * 32,
        "revision_state": "active",
        "input_set_hash": "",
        "supersedes_revision_id": "",
        "correction_of_revision_id": "",
        "training_evidence_ref": {
            "command_id": "feedback-command-001",
            "command_payload_hash": HASH_A,
            "attribution_revision_id": "cogrev-" + "1" * 32,
            "attribution_payload_hash": HASH_B,
            "proposal_id": "feedback-target-proposal-001",
            "proposal_hash": HASH_C,
            "decision_id": "push-decision-001",
            "action_id": "material-action-" + "1" * 32,
            "effect_id": "material-effect-" + "1" * 32,
            "receipt_id": "effect-receipt-001",
            "receipt_hash": HASH_A,
        },
        "prediction_ref": {
            "object_id": "prediction-" + "1" * 32,
            "revision_id": "cogrev-" + "3" * 32,
            "payload_hash": HASH_A,
            "input_hash": HASH_A,
        },
        "prediction_terminal_ref": {
            "object_id": "prediction-" + "1" * 32,
            "revision_id": "cogrev-" + "5" * 32,
            "payload_hash": HASH_C,
            "terminal_state": "measured",
            "outcome_revision_id": "cogrev-" + "2" * 32,
            "outcome_payload_hash": HASH_B,
        },
        "outcome_ref": {
            "object_id": "outcome-" + "1" * 32,
            "revision_id": "cogrev-" + "2" * 32,
            "payload_hash": HASH_B,
            "oracle_receipt_hash": HASH_C,
        },
        "decision_ref": {
            "object_id": "decision-" + "1" * 32,
            "revision_id": "cogrev-" + "4" * 32,
            "payload_hash": HASH_C,
        },
        "material_effect_ref": {
            "action_id": "material-action-" + "1" * 32,
            "effect_id": "material-effect-" + "1" * 32,
            "effect_receipt_id": "effect-receipt-001",
            "effect_receipt_hash": HASH_A,
        },
        "delivery_ref": {"event_id": "delivery-001", "payload_hash": HASH_B},
        "subject": subject,
        "scope": scope,
        "principal_ref": {
            "principal_id": "mcp:codex:training-test",
            "authorization_ref": "authz:training-test",
        },
        "temporal_proof": temporal_proof,
        "authority_proof": {
            "source_authority_catalog_hash": HASH_A,
            "raw_issuance_receipt_ref": "raw-receipt-001",
            "raw_issuance_receipt_hash": HASH_B,
        },
        "feature_snapshot": feature_snapshot,
        "label": label,
        "evidence_quality": {
            "uncertainty": 0.0,
            "attribution": "direct_objective_measurement",
            "competing_causes": [],
            "calibration_eligible": True,
            "exclusion_reason": "",
        },
        "dataset_assignment": derive_dataset_assignment(subject=subject, scope=scope),
        "lifecycle_state": "admitted",
        "target_effect_refs": {
            "projection_command_key": "training-projection-command-001",
            "projection_effect_id": "training-sample-001",
            "reciprocal_receipt_id": "training-sample-receipt-001",
        },
    }
    payload["input_set_hash"] = training_admission_input_hash(payload)
    return payload


def _revision(payload: dict | None = None) -> CognitiveStateRevision:
    return CognitiveStateRevision.create(
        object_type="training_admission_record",
        object_id="training-admission-" + "1" * 32,
        source_event_id="training-evidence-source-event",
        source_revision_id="feedback-command-001",
        source_content_hash=HASH_A,
        scope_type="project",
        scope_id="mnemos",
        evidence_refs=("feedback-command-001", "cogrev-" + "2" * 32),
        payload=payload or admission_payload(),
        created_at="2026-07-19T01:00:02+00:00",
    )


def test_canonical_state_accepts_complete_training_admission_record() -> None:
    revision = _revision()

    assert revision.schema_version == TRAINING_ADMISSION_SCHEMA_VERSION
    assert revision.object_type == "training_admission_record"


def test_training_features_reject_post_outcome_or_reaction_bytes() -> None:
    payload = deepcopy(admission_payload())
    payload["feature_snapshot"]["outcome_observed_value"] = "useful"
    payload["feature_snapshot"]["reaction_kind"] = "accept"
    payload["feature_snapshot"]["snapshot_hash"] = sha256_json(
        {key: value for key, value in payload["feature_snapshot"].items() if key != "snapshot_hash"}
    )
    payload["input_set_hash"] = training_admission_input_hash(payload)

    with pytest.raises(ValueError, match="feature snapshot fields are invalid"):
        _revision(payload)


def test_dataset_split_is_code_owned_and_cannot_be_overridden() -> None:
    payload = deepcopy(admission_payload())
    current = payload["dataset_assignment"]["split"]
    payload["dataset_assignment"]["split"] = "holdout" if current != "holdout" else "train"
    payload["input_set_hash"] = training_admission_input_hash(payload)

    with pytest.raises(ValueError, match="dataset assignment mismatch"):
        _revision(payload)


def run_payload() -> dict:
    admissions = []
    for index in range(24):
        split = "train" if index < 20 else "validation" if index < 22 else "holdout"
        admissions.append(
            {
                "revision_id": f"cogrev-{index + 1:032x}",
                "payload_hash": "sha256:" + f"{index + 1:064x}",
                "feature_snapshot_hash": "sha256:" + f"{index + 101:064x}",
                "label_numeric": index % 2,
                "split": split,
                "group_hash": "sha256:" + f"{index + 201:064x}",
            }
        )
    fit_input_hash = training_fit_input_hash(admissions)
    model_blob = {
        "feature_names": [
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
        ],
        "negative_centroid": [0.0] * 11,
        "positive_centroid": [1.0] * 11,
    }
    train_revision_ids = [item["revision_id"] for item in admissions if item["split"] == "train"]
    bayesian_prior_artifact = {
        "schema_version": BAYESIAN_PRIOR_VERSION,
        "effect_kind": "bayesian_prior",
        "effect_id": "governed-bayesian-prior-" + "6" * 32,
        "input_hash": HASH_A,
        "admission_revision_ids": train_revision_ids,
        "dimension": "predictive_delivery",
        "alpha": 11.0,
        "beta": 11.0,
        "total_samples": 20,
    }
    bayesian_prior_artifact["artifact_hash"] = sha256_json(bayesian_prior_artifact)
    rule_optimizer_artifact = {
        "schema_version": RULE_OPTIMIZER_VERSION,
        "effect_kind": "rule_optimizer",
        "effect_id": "governed-rule-optimizer-" + "7" * 32,
        "input_hash": HASH_B,
        "admission_revision_ids": train_revision_ids,
        "dimension": "predictive_delivery",
        "feature_names": list(model_blob["feature_names"]),
        "weights": {name: 0.0 for name in model_blob["feature_names"]},
        "bias": 0.5,
        "sample_count": 20,
    }
    rule_optimizer_artifact["artifact_hash"] = sha256_json(rule_optimizer_artifact)
    payload = {
        "access_control": _access_control(),
        "schema_version": TRAINING_RUN_SCHEMA_VERSION,
        "run_id": "training-run-" + "4" * 32,
        "run_input_hash": "",
        "dimension": "predictive_delivery",
        "algorithm": {
            "name": "governed_binary_centroid",
            "version": "mnemos.governed_binary_centroid.v1",
            "code_hash": TRAINING_ALGORITHM_CODE_HASH,
            "spec_hash": TRAINING_ALGORITHM_SPEC_HASH,
            "config_hash": TRAINING_ALGORITHM_CONFIG_HASH,
            "readiness_policy_hash": READINESS_POLICY_HASH,
            "selection_input_hash": fit_input_hash,
        },
        "admission_refs": admissions,
        "dataset_manifest": training_dataset_manifest(admissions),
        "fit_input_hash": fit_input_hash,
        "validation_report": {"example_count": 2, "report_hash": HASH_A},
        "holdout_report": {
            "example_count": 2,
            "report_hash": HASH_B,
            "evaluated_after_model_sealed_at": "2026-07-19T02:00:01+00:00",
        },
        "parent_model_ref": {"model_id": "", "model_hash": ""},
        "model_artifact": {
            "model_id": "governed-model-" + "5" * 32,
            "model_type": "binary_feature_centroid",
            "blob": model_blob,
            "blob_hash": sha256_json(model_blob),
            "serialization": "canonical_json_v1",
            "sealed_at": "2026-07-19T02:00:00+00:00",
        },
        "bayesian_prior_artifact": bayesian_prior_artifact,
        "rule_optimizer_artifact": rule_optimizer_artifact,
        "state": "sealed",
        "material_effect_refs": {
            "action_id": "material-action-" + "5" * 32,
            "effect_id": "material-effect-" + "5" * 32,
        },
        "projection_receipt_ref": {
            "receipt_id": "training-run-receipt-001",
            "receipt_hash": HASH_C,
        },
        "supersedes_revision_id": "",
        "rebuild_of_revision_id": "",
    }
    payload["run_input_hash"] = training_run_input_hash(payload)
    return payload


def _run_revision(payload: dict | None = None) -> CognitiveStateRevision:
    return CognitiveStateRevision.create(
        object_type="training_run_record",
        object_id="training-run-" + "4" * 32,
        source_event_id="training-run-source-event",
        source_revision_id="training-manifest-001",
        source_content_hash=HASH_B,
        scope_type="project",
        scope_id="mnemos",
        evidence_refs=("training-manifest-001",),
        payload=payload or run_payload(),
        created_at="2026-07-19T02:00:02+00:00",
    )


def test_canonical_state_accepts_complete_training_run_record() -> None:
    revision = _run_revision()

    assert revision.schema_version == TRAINING_RUN_SCHEMA_VERSION
    assert revision.object_type == "training_run_record"


def test_training_run_fit_hash_excludes_validation_and_holdout() -> None:
    payload = deepcopy(run_payload())
    payload["fit_input_hash"] = sha256_json(payload["admission_refs"])
    payload["algorithm"]["selection_input_hash"] = payload["fit_input_hash"]
    payload["run_input_hash"] = training_run_input_hash(payload)

    with pytest.raises(ValueError, match="fit input hash mismatch"):
        _run_revision(payload)


def test_holdout_cannot_influence_algorithm_selection() -> None:
    payload = deepcopy(run_payload())
    payload["algorithm"]["selection_input_hash"] = payload["dataset_manifest"]["manifest_hash"]
    payload["run_input_hash"] = training_run_input_hash(payload)

    with pytest.raises(ValueError, match="algorithm selection input"):
        _run_revision(payload)
