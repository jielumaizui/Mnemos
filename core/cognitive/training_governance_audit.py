"""Independent, read-only COG-048 governed-training audit."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from core.cognitive.state_contract import (
    LocalConsumerCommand,
    sha256_json,
    validate_cognitive_state_payload,
)
from core.cognitive.state_schema import inspect_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.prediction_outcome_support import (
    reissue_objective_measurement,
)
from core.cognitive.training_contract import (
    FEATURE_NAMES,
    TRAINING_ADMISSION_COMMAND,
    TRAINING_ADMISSION_CONSUMER,
    derive_bayesian_prior_artifact,
    derive_dataset_assignment,
    derive_feature_snapshot,
    derive_rule_optimizer_artifact,
    derive_training_label,
    governed_training_examples,
    training_admission_input_hash,
    training_dataset_manifest,
    training_fit_input_hash,
    training_run_input_hash,
    validate_training_admission_intake_payload,
)
from core.cognitive.training_governance_static_audit import (
    PERMANENT_FAIL_CLOSED_BOUNDARIES,
    audit_retired_training_surfaces,
)
from core.cognitive.training_governance_projection_audit import (
    audit_sample_projection_receipts,
)
from core.cognitive.training_history_migration import (
    build_training_history_inventory,
    inspect_training_history_coverage,
)
from core.cognitive.training_migration_barrier import (
    read_training_migration_barrier,
)
from core.scoring.training_schema import inspect_training_schema
from core.cognitive.training_governance_audit_support import (  # noqa: F401
    _barrier_guard_gap,
    _caller_selectable_correction_count,
    _command_index,
    _connect,
    _current_revision_for_object,
    _current_revisions,
    _expected_aux_projection,
    _expected_run_receipt,
    _historical_promotion_count,
    _prediction_outcome_identity_matches,
    _revision_descends_from,
    _revision_index,
    _state_effect_receipt_index,
    _terminal_prediction_matches,
    _terminal_projection_receipt_matches,
)


AUDIT_SCHEMA_VERSION = "mnemos.training_governance_audit.v1"
ZERO_BUDGET_METRICS = (
    "expected_equals_actual_from_same_reaction",
    "training_without_prior_prediction",
    "reaction_used_as_objective_ground_truth",
    "label_without_provenance",
    "immature_outcome_admitted",
    "confounded_outcome_admitted",
    "prediction_outcome_identity_mismatch",
    "training_terminal_prediction_gap",
    "training_admission_upstream_gap",
    "post_outcome_feature_leak",
    "holdout_leak",
    "split_assignment_mismatch",
    "duplicate_training_effect",
    "training_effect_without_receipt",
    "model_without_training_manifest",
    "model_manifest_hash_mismatch",
    "stale_corrected_sample_active",
    "stale_model_active",
    "bayesian_update_without_admission",
    "optimizer_update_without_admission",
    "training_producer_bypass",
    "legacy_training_active_reader",
    "historical_training_object_uncovered",
    "historical_quarantine_promoted_active",
    "training_schema_registry_mismatch",
    "training_migration_barrier_bypass",
    "caller_selectable_training_correction",
    "phase3_training_contract_gap",
)


def audit_training_governance_static(*, repo_root: Path) -> dict[str, Any]:
    """Audit only repository-owned COG-048 call, SQL, and barrier contracts."""

    repository = Path(repo_root).resolve()
    metrics = {name: 0 for name in ZERO_BUDGET_METRICS}
    static = audit_retired_training_surfaces(repository)
    metrics["training_producer_bypass"] = len(static["legacy_call_sites"])
    metrics["legacy_training_active_reader"] = len(static["legacy_sql_sites"])
    metrics["phase3_training_contract_gap"] = len(static["parse_errors"]) + len(
        static["fail_closed_boundary_gaps"]
    )
    metrics["training_migration_barrier_bypass"] = _barrier_guard_gap(repository)
    metrics["caller_selectable_training_correction"] = (
        _caller_selectable_correction_count(repository)
    )
    findings = [
        {
            "metric": metric,
            "code": metric,
            "count": int(count),
        }
        for metric, count in metrics.items()
        if count
    ]
    ok = all(int(value) == 0 for value in metrics.values())
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "audit_mode": "static_only",
        "ok": ok,
        "status": "pass" if ok else "fail",
        "metrics": metrics,
        "denominators": {
            "formal_producers": 0,
            "formal_producer_symbols": 1,
            "formal_schedulers": 1,
            "formal_model_readers": 1,
            "required_barrier_guards": 9,
            "permanent_fail_closed_boundaries": len(PERMANENT_FAIL_CLOSED_BOUNDARIES),
        },
        "findings": findings,
        "static_audit": static,
        "sensitive_bytes_in_report": 0,
    }


def audit_training_governance(
    *,
    database_dir: Path,
    repo_root: Path,
) -> dict[str, Any]:
    """Recompute governed-training truth without calling a production writer."""

    root = Path(database_dir).expanduser()
    repository = Path(repo_root).resolve()
    target_db = root / "producer_consumer_ledger.db"
    scoring_db = root / "mnemos.db"
    metrics = {name: 0 for name in ZERO_BUDGET_METRICS}
    findings: list[dict[str, Any]] = []
    denominators = {
        "formal_producers": 0,
        "formal_producer_symbols": 1,
        "formal_schedulers": 1,
        "formal_model_readers": 1,
        "historical_object_classes": 11,
        "historical_objects": 0,
        "historical_quarantines": 0,
        "admissions": 0,
        "admission_intake_commands": 0,
        "admission_intake_receipts": 0,
        "governed_projection_commands": 0,
        "governed_projection_receipts": 0,
        "terminal_prediction_expected": 0,
        "terminal_prediction_verified": 0,
        "admission_upstream_expected": 0,
        "admission_upstream_verified": 0,
        "admitted_samples": 0,
        "excluded_samples": 0,
        "train_examples": 0,
        "validation_examples": 0,
        "holdout_examples": 0,
        "training_runs": 0,
        "governed_models": 0,
        "active_model_heads": 0,
        "bayesian_effects": 0,
        "optimizer_effects": 0,
        "corrections": 0,
        "required_phase3_audits": 1,
        "permanent_fail_closed_boundaries": len(PERMANENT_FAIL_CLOSED_BOUNDARIES),
    }

    static = audit_retired_training_surfaces(repository)
    metrics["training_producer_bypass"] = len(static["legacy_call_sites"])
    metrics["legacy_training_active_reader"] = len(static["legacy_sql_sites"])
    metrics["caller_selectable_training_correction"] = (
        _caller_selectable_correction_count(repository)
    )
    if static["parse_errors"] or static["fail_closed_boundary_gaps"]:
        metrics["phase3_training_contract_gap"] += len(static["parse_errors"]) + len(
            static["fail_closed_boundary_gaps"]
        )
    relevant_paths = (
        target_db,
        scoring_db,
        root / "rule_weight_optimizer.db",
        root / "rule_weights.db",
    )
    if not any(path.is_file() for path in relevant_paths):
        metrics["phase3_training_contract_gap"] += 1
        barrier_gap = _barrier_guard_gap(repository)
        barrier_active = False
        try:
            barrier_active = read_training_migration_barrier(root) is not None
        except RuntimeError:
            barrier_active = True
            barrier_gap += 1
        if barrier_active:
            barrier_gap += 1
        metrics["training_migration_barrier_bypass"] = barrier_gap
        if any(
            metrics[name] for name in ZERO_BUDGET_METRICS if name != "phase3_training_contract_gap"
        ):
            metrics["phase3_training_contract_gap"] += 1
        for metric, count in metrics.items():
            if count:
                findings.append(
                    {
                        "metric": metric,
                        "code": metric,
                        "count": int(count),
                    }
                )
        ok = all(int(value) == 0 for value in metrics.values())
        return {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "audit_mode": "full",
            "ok": ok,
            "status": "pass" if ok else "fail",
            "metrics": metrics,
            "denominators": denominators,
            "findings": findings,
            "static_audit": static,
            "historical_inventory": {
                "status": "not_initialized",
                "object_count": 0,
                "counts_by_table": {},
            },
            "historical_coverage": {
                "status": "not_initialized",
                "covered": 0,
                "uncovered": 0,
                "unexpected": 0,
                "invalid": 0,
                "prior_feedback_links": 0,
                "uncovered_by_table": {},
                "activation_marker_present": False,
                "activation_marker_valid": False,
            },
            "state_schema": {
                "classification": "not_initialized",
                "ok": False,
            },
            "training_schema": {
                "classification": "not_initialized",
                "ok": False,
            },
            "sensitive_bytes_in_report": 0,
        }

    inventory: Mapping[str, Any] | None = None
    coverage: Mapping[str, Any] = {
        "covered": 0,
        "uncovered": 0,
        "unexpected": 0,
        "invalid": 0,
        "prior_feedback_links": 0,
        "uncovered_by_table": {},
        "activation_marker_present": False,
        "activation_marker_valid": False,
    }
    try:
        inventory = build_training_history_inventory(root)
        denominators["historical_objects"] = int(inventory["object_count"])
        coverage = inspect_training_history_coverage(target_db, inventory)
        denominators["historical_quarantines"] = int(coverage["covered"])
        metrics["historical_training_object_uncovered"] = (
            int(coverage["uncovered"]) + int(coverage["unexpected"]) + int(coverage["invalid"])
        )
        if coverage.get("activation_marker_present"):
            uncovered_by_table = coverage.get("uncovered_by_table") or {}
            metrics["bayesian_update_without_admission"] = sum(
                int(uncovered_by_table.get(table) or 0)
                for table in (
                    "scoring.bayesian_scorer_state",
                    "scoring.bayesian_feedback",
                )
            )
            metrics["optimizer_update_without_admission"] = sum(
                int(count)
                for table, count in uncovered_by_table.items()
                if str(table).startswith("rule_weight_optimizer.")
                or str(table).startswith("rule_weights.")
            )
        if inventory["missing_database_classes"]:
            metrics["phase3_training_contract_gap"] += len(inventory["missing_database_classes"])
        if int(inventory["active_legacy_model_count"]):
            metrics["legacy_training_active_reader"] += int(inventory["active_legacy_model_count"])
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        metrics["phase3_training_contract_gap"] += 1
        findings.append(
            {
                "metric": "phase3_training_contract_gap",
                "code": "training_history_inventory_failed",
                "evidence": type(exc).__name__,
            }
        )

    state_schema: Mapping[str, Any] = {
        "classification": "absent",
        "ok": False,
    }
    training_schema: Mapping[str, Any] = {
        "classification": "absent",
        "ok": False,
    }
    revisions: dict[str, dict[str, Any]] = {}
    commands: dict[str, dict[str, Any]] = {}
    state_effect_receipts: dict[str, dict[str, Any]] = {}
    current_admissions: list[dict[str, Any]] = []
    current_runs: list[dict[str, Any]] = []
    state_store: CognitiveStateStore | None = None
    if target_db.is_file():
        try:
            with _connect(target_db) as conn:
                inspected = inspect_cognitive_state_schema(conn)
                state_schema = inspected.as_dict()
                if inspected.ok:
                    state_store = CognitiveStateStore(target_db)
                    revisions = _revision_index(conn)
                    commands = _command_index(conn)
                    state_effect_receipts = _state_effect_receipt_index(conn)
                    admission_intakes = tuple(
                        command
                        for command in commands.values()
                        if command["consumer_id"] == TRAINING_ADMISSION_CONSUMER
                        and command["command_type"] == TRAINING_ADMISSION_COMMAND
                    )
                    governed_projections = tuple(
                        command
                        for command in commands.values()
                        if command["consumer_id"]
                        == "governed_training_projection"
                    )
                    denominators["formal_producers"] = len(admission_intakes)
                    denominators["admission_intake_commands"] = len(
                        admission_intakes
                    )
                    denominators["admission_intake_receipts"] = sum(
                        command["command_id"] in state_effect_receipts
                        for command in admission_intakes
                    )
                    denominators["governed_projection_commands"] = len(
                        governed_projections
                    )
                    denominators["governed_projection_receipts"] = sum(
                        command["command_id"] in state_effect_receipts
                        for command in governed_projections
                    )
                    current_admissions = _current_revisions(
                        conn,
                        object_type="training_admission_record",
                    )
                    current_runs = _current_revisions(
                        conn,
                        object_type="training_run_record",
                    )
                else:
                    metrics["phase3_training_contract_gap"] += 1
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
            metrics["phase3_training_contract_gap"] += 1
            findings.append(
                {
                    "metric": "phase3_training_contract_gap",
                    "code": "cognitive_state_audit_failed",
                    "evidence": type(exc).__name__,
                }
            )
    else:
        metrics["phase3_training_contract_gap"] += 1

    sample_state: dict[str, str] = {}
    if scoring_db.is_file():
        try:
            with _connect(scoring_db) as conn:
                training_inspection = inspect_training_schema(conn)
                training_schema = training_inspection.as_dict()
                if training_inspection.classification != "canonical" or not training_inspection.ok:
                    metrics["training_schema_registry_mismatch"] += 1
                else:
                    sample_state = _audit_projection(
                        conn,
                        current_admissions=current_admissions,
                        current_runs=current_runs,
                        revisions=revisions,
                        commands=commands,
                        state_effect_receipts=state_effect_receipts,
                        metrics=metrics,
                        denominators=denominators,
                    )
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
            metrics["training_schema_registry_mismatch"] += 1
            findings.append(
                {
                    "metric": "training_schema_registry_mismatch",
                    "code": "training_projection_audit_failed",
                    "evidence": type(exc).__name__,
                }
            )
    else:
        metrics["training_schema_registry_mismatch"] += 1

    _audit_admissions(
        current_admissions,
        revisions=revisions,
        state_store=state_store,
        sample_state=sample_state,
        metrics=metrics,
        denominators=denominators,
    )
    _audit_runs(
        current_runs,
        revisions=revisions,
        metrics=metrics,
        denominators=denominators,
    )
    if inventory is not None:
        metrics["historical_quarantine_promoted_active"] = _historical_promotion_count(
            current_admissions,
            tuple(inventory["objects"]),
        )

    barrier_gap = _barrier_guard_gap(repository)
    barrier_active = False
    try:
        barrier_active = read_training_migration_barrier(root) is not None
    except RuntimeError:
        barrier_active = True
        barrier_gap += 1
    if barrier_active:
        barrier_gap += 1
    metrics["training_migration_barrier_bypass"] = barrier_gap

    if not coverage.get("activation_marker_valid"):
        metrics["phase3_training_contract_gap"] += 1
    if not state_schema.get("ok"):
        metrics["phase3_training_contract_gap"] += 1
    if not training_schema.get("ok"):
        metrics["phase3_training_contract_gap"] += 1
    if any(metrics[name] for name in ZERO_BUDGET_METRICS if name != "phase3_training_contract_gap"):
        metrics["phase3_training_contract_gap"] += 1

    for metric, count in metrics.items():
        if count and not any(item["metric"] == metric for item in findings):
            findings.append(
                {
                    "metric": metric,
                    "code": metric,
                    "count": int(count),
                }
            )
    ok = all(int(value) == 0 for value in metrics.values())
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "audit_mode": "full",
        "ok": ok,
        "status": "pass" if ok else "fail",
        "metrics": metrics,
        "denominators": denominators,
        "findings": findings,
        "static_audit": static,
        "historical_inventory": (
            {
                "inventory_hash": inventory["inventory_hash"],
                "object_manifest_hash": inventory["object_manifest_hash"],
                "object_count": inventory["object_count"],
                "counts_by_table": inventory["counts_by_table"],
                "active_legacy_model_count": inventory["active_legacy_model_count"],
            }
            if inventory is not None
            else {}
        ),
        "historical_coverage": dict(coverage),
        "state_schema": dict(state_schema),
        "training_schema": dict(training_schema),
        "sensitive_bytes_in_report": 0,
    }


def _audit_admissions(
    admissions: Sequence[Mapping[str, Any]],
    *,
    revisions: Mapping[str, Mapping[str, Any]],
    state_store: CognitiveStateStore | None,
    sample_state: Mapping[str, str],
    metrics: dict[str, int],
    denominators: dict[str, int],
) -> None:
    denominators["admissions"] = len(admissions)
    group_splits: dict[str, set[str]] = {}
    for revision in admissions:
        payload = revision["payload"]
        try:
            validate_cognitive_state_payload(
                "training_admission_record",
                payload,
            )
        except (KeyError, TypeError, ValueError):
            metrics["phase3_training_contract_gap"] += 1
            continue
        split = str(payload["dataset_assignment"]["split"])
        denominators[f"{split}_examples"] += 1
        group_hash = str(payload["dataset_assignment"]["group_hash"])
        group_splits.setdefault(group_hash, set()).add(split)
        if payload["lifecycle_state"] == "admitted":
            denominators["admission_upstream_expected"] += 1
            if state_store is not None and _admission_upstream_matches(
                revision,
                state_store,
            ):
                denominators["admission_upstream_verified"] += 1
            else:
                metrics["training_admission_upstream_gap"] += 1
        prediction_ref = payload["prediction_ref"]
        terminal_ref = payload["prediction_terminal_ref"]
        outcome_ref = payload["outcome_ref"]
        prediction = revisions.get(str(prediction_ref["revision_id"]))
        terminal_prediction = revisions.get(
            str(terminal_ref["revision_id"])
        )
        outcome = revisions.get(str(outcome_ref["revision_id"]))
        attribution = revisions.get(
            str(payload["training_evidence_ref"]["attribution_revision_id"])
        )
        if (
            prediction is None
            or prediction.get("object_type") != "prediction_record"
            or prediction.get("payload_hash") != prediction_ref["payload_hash"]
        ):
            metrics["training_without_prior_prediction"] += 1
        denominators["terminal_prediction_expected"] += 1
        terminal_valid = _terminal_prediction_matches(
            prediction,
            terminal_prediction,
            outcome,
            payload,
            revisions,
        )
        if terminal_valid and state_store is not None:
            current_terminal = state_store.current_revision(
                "prediction_record",
                str(terminal_ref["object_id"]),
            )
            terminal_valid = bool(
                current_terminal is not None
                and current_terminal.revision_id == terminal_ref["revision_id"]
                and _terminal_projection_receipt_matches(
                    state_store,
                    current_terminal,
                )
            )
        if terminal_valid:
            denominators["terminal_prediction_verified"] += 1
        else:
            metrics["training_terminal_prediction_gap"] += 1
        if (
            attribution is None
            or attribution.get("object_type") != "feedback_attribution_record"
            or attribution["payload"].get("evidence_class") != "objective_outcome"
        ):
            metrics["reaction_used_as_objective_ground_truth"] += 1
            metrics["expected_equals_actual_from_same_reaction"] += 1
        if (
            outcome is None
            or outcome.get("object_type") != "outcome_measurement"
            or outcome.get("payload_hash") != outcome_ref["payload_hash"]
        ):
            metrics["label_without_provenance"] += 1
            continue
        temporal = payload["temporal_proof"]
        if temporal.get("maturity") != "mature":
            metrics["immature_outcome_admitted"] += 1
        quality = payload["evidence_quality"]
        if quality.get("competing_causes") or not quality.get("calibration_eligible"):
            metrics["confounded_outcome_admitted"] += 1
        if prediction is not None:
            if not _prediction_outcome_identity_matches(
                prediction["payload"],
                outcome["payload"],
                payload,
            ):
                metrics["prediction_outcome_identity_mismatch"] += 1
            try:
                expected_features = derive_feature_snapshot(prediction["payload"])
                if payload["feature_snapshot"] != expected_features or set(
                    payload["feature_snapshot"]["values"]
                ) != set(FEATURE_NAMES):
                    metrics["post_outcome_feature_leak"] += 1
            except (KeyError, TypeError, ValueError):
                metrics["post_outcome_feature_leak"] += 1
        try:
            expected_label = derive_training_label(
                outcome["payload"],
                outcome_revision_id=str(outcome["revision_id"]),
                outcome_payload_hash=str(outcome["payload_hash"]),
            )
            if payload["label"] != expected_label:
                metrics["label_without_provenance"] += 1
        except (KeyError, TypeError, ValueError):
            metrics["label_without_provenance"] += 1
        if state_store is None:
            metrics["label_without_provenance"] += 1
        else:
            try:
                prediction_revision = state_store.revision(str(prediction_ref["revision_id"]))
                outcome_revision = state_store.revision(str(outcome_ref["revision_id"]))
                if prediction_revision is None or outcome_revision is None:
                    raise ValueError("objective training source revision is missing")
                issuance = reissue_objective_measurement(
                    state_store=state_store,
                    prediction=prediction_revision,
                    outcome=outcome_revision,
                )
                if issuance.issuance_hash != outcome_ref["oracle_receipt_hash"]:
                    raise ValueError("objective oracle receipt hash mismatch")
            except (KeyError, OSError, RuntimeError, TypeError, ValueError):
                metrics["label_without_provenance"] += 1
        try:
            assignment = derive_dataset_assignment(
                subject=payload["subject"],
                scope=payload["scope"],
            )
            if assignment != payload["dataset_assignment"]:
                metrics["split_assignment_mismatch"] += 1
        except (KeyError, TypeError, ValueError):
            metrics["split_assignment_mismatch"] += 1
        if payload.get("input_set_hash") != training_admission_input_hash(payload):
            metrics["label_without_provenance"] += 1
        lifecycle = str(payload["lifecycle_state"])
        sample_revision_id = (
            str(revision["revision_id"])
            if lifecycle == "admitted"
            else str(payload.get("correction_of_revision_id") or revision["revision_id"])
        )
        projected_state = sample_state.get(sample_revision_id, "missing")
        if lifecycle == "admitted":
            denominators["admitted_samples"] += 1
            if projected_state != "admit":
                metrics["training_effect_without_receipt"] += 1
        else:
            denominators["excluded_samples"] += 1
            if projected_state == "admit":
                metrics["stale_corrected_sample_active"] += 1
        current_outcome = _current_revision_for_object(
            revisions,
            object_type="outcome_measurement",
            object_id=str(outcome["object_id"]),
        )
        if (
            lifecycle == "admitted"
            and current_outcome is not None
            and current_outcome["revision_id"] != outcome["revision_id"]
        ):
            metrics["stale_corrected_sample_active"] += 1
            denominators["corrections"] += 1
    metrics["holdout_leak"] += sum(1 for splits in group_splits.values() if len(splits) > 1)


def _admission_upstream_matches(
    admission: Mapping[str, Any],
    state_store: CognitiveStateStore,
) -> bool:
    """Read-only cross-check of one admission's complete current source chain."""

    try:
        payload = admission["payload"]
        evidence_ref = payload["training_evidence_ref"]
        target_command = state_store.command(str(evidence_ref["command_id"]))
        if target_command is None:
            return False
        recomputed_target = LocalConsumerCommand.create(
            revision_id=str(target_command["revision_id"]),
            consumer_id=str(target_command["consumer_id"]),
            command_type=str(target_command["command_type"]),
            payload=target_command["payload"],
            created_at=str(target_command["created_at"]),
        )
        if (
            target_command["consumer_id"] != "training_evidence"
            or target_command["command_type"] != "evaluate_feedback_target"
            or recomputed_target.command_id != target_command["command_id"]
            or recomputed_target.payload_hash != target_command["payload_hash"]
            or target_command["payload_hash"]
            != evidence_ref["command_payload_hash"]
        ):
            return False

        attribution = state_store.revision(str(target_command["revision_id"]))
        if (
            attribution is None
            or attribution.object_type != "feedback_attribution_record"
            or attribution.revision_id
            != evidence_ref["attribution_revision_id"]
            or attribution.payload_hash
            != evidence_ref["attribution_payload_hash"]
            or state_store.current_revision(
                "feedback_attribution_record",
                attribution.object_id,
            )
            != attribution
        ):
            return False
        state_store.validate_feedback_effect_receipt(
            str(target_command["command_id"])
        )
        target_receipt = state_store.effect_receipt(
            str(target_command["command_id"])
        )
        if target_receipt is None or target_receipt["status"] != "committed":
            return False

        outcome_ref = payload["outcome_ref"]
        outcome = state_store.revision(str(outcome_ref["revision_id"]))
        if (
            outcome is None
            or outcome.object_type != "outcome_measurement"
            or outcome.object_id != outcome_ref["object_id"]
            or outcome.payload_hash != outcome_ref["payload_hash"]
            or state_store.current_revision(
                "outcome_measurement",
                outcome.object_id,
            )
            != outcome
        ):
            return False
        target_outcome_ref = target_command["payload"].get(
            "objective_outcome_ref"
        )
        if not isinstance(target_outcome_ref, Mapping) or {
            "outcome_id": target_outcome_ref.get("outcome_id"),
            "revision_id": target_outcome_ref.get("revision_id"),
            "payload_hash": target_outcome_ref.get("payload_hash"),
        } != {
            "outcome_id": outcome.object_id,
            "revision_id": outcome.revision_id,
            "payload_hash": outcome.payload_hash,
        }:
            return False

        intake_commands = []
        for command in state_store.commands_for_revision(
            attribution.revision_id
        ):
            if (
                command["consumer_id"] != TRAINING_ADMISSION_CONSUMER
                or command["command_type"] != TRAINING_ADMISSION_COMMAND
            ):
                continue
            validate_training_admission_intake_payload(command["payload"])
            target_ref = command["payload"]["training_target_ref"]
            if (
                target_ref["command_id"] == target_command["command_id"]
                and target_ref["payload_hash"]
                == target_command["payload_hash"]
            ):
                intake_commands.append(command)
        if len(intake_commands) != 1:
            return False
        intake = intake_commands[0]
        recomputed_intake = LocalConsumerCommand.create(
            revision_id=str(intake["revision_id"]),
            consumer_id=str(intake["consumer_id"]),
            command_type=str(intake["command_type"]),
            payload=intake["payload"],
            created_at=str(intake["created_at"]),
        )
        if (
            recomputed_intake.command_id != intake["command_id"]
            or recomputed_intake.payload_hash != intake["payload_hash"]
            or intake["payload"]["attribution_ref"]
            != {
                "object_id": attribution.object_id,
                "revision_id": attribution.revision_id,
                "payload_hash": attribution.payload_hash,
            }
            or intake["payload"]["outcome_ref"]
            != {
                "object_id": outcome.object_id,
                "revision_id": outcome.revision_id,
                "payload_hash": outcome.payload_hash,
            }
        ):
            return False
        state_store.validate_training_admission_intake_receipt(
            str(intake["command_id"])
        )
        intake_receipt = state_store.effect_receipt(str(intake["command_id"]))
        if intake_receipt is None or intake_receipt["status"] != "committed":
            return False

        prediction = state_store.revision(
            str(payload["prediction_ref"]["revision_id"])
        )
        terminal = state_store.revision(
            str(payload["prediction_terminal_ref"]["revision_id"])
        )
        decision = state_store.revision(
            str(payload["decision_ref"]["revision_id"])
        )
        if (
            prediction is None
            or terminal is None
            or decision is None
            or prediction.payload_hash
            != payload["prediction_ref"]["payload_hash"]
            or decision.object_type != "decision_trace"
            or decision.object_id != payload["decision_ref"]["object_id"]
            or decision.payload_hash != payload["decision_ref"]["payload_hash"]
            or prediction.payload["decision_ref"]["revision_id"]
            != decision.revision_id
            or prediction.payload["decision_ref"]["revision_hash"]
            != decision.payload_hash
            or state_store.current_revision(
                "prediction_record",
                terminal.object_id,
            )
            != terminal
            or not _terminal_projection_receipt_matches(
                state_store,
                terminal,
            )
        ):
            return False
        return _prediction_material_receipt_matches(
            state_store,
            prediction=prediction,
            decision=decision,
            material_ref=payload["material_effect_ref"],
        )
    except (
        KeyError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ):
        return False


def _prediction_material_receipt_matches(
    state_store: CognitiveStateStore,
    *,
    prediction: Any,
    decision: Any,
    material_ref: Mapping[str, Any],
) -> bool:
    action_ref = prediction.payload["action_ref"]
    action_specs = tuple(
        item
        for item in decision.payload["action_specs"]
        if item["action_id"] == action_ref["action_id"]
        and item["effect_id"] == action_ref["effect_id"]
    )
    if len(action_specs) != 1 or (
        material_ref["action_id"] != action_ref["action_id"]
        or material_ref["effect_id"] != action_ref["effect_id"]
    ):
        return False
    with _connect(state_store.db_path) as conn:
        rows = conn.execute(
            """
            SELECT receipt.receipt_id, receipt.command_id,
                   receipt.revision_id, receipt.status,
                   receipt.target_effect_id, receipt.before_hash,
                   receipt.after_hash, receipt.evidence_refs,
                   receipt.created_at AS receipt_created_at,
                   command.consumer_id, command.command_type,
                   command.payload_json, command.payload_hash,
                   command.created_at AS command_created_at
            FROM cognitive_state_effect_receipts AS receipt
            JOIN cognitive_state_outbox AS command
              ON command.command_id=receipt.command_id
            WHERE receipt.target_effect_id=?
            """,
            (str(action_ref["effect_id"]),),
        ).fetchall()
    if len(rows) != 1:
        return False
    row = rows[0]
    command_payload = json.loads(str(row["payload_json"]))
    command = LocalConsumerCommand.create(
        revision_id=str(row["revision_id"]),
        consumer_id=str(row["consumer_id"]),
        command_type=str(row["command_type"]),
        payload=command_payload,
        created_at=str(row["command_created_at"]),
    )
    receipt_hash = sha256_json(
        {
            "receipt_id": str(row["receipt_id"]),
            "command_id": str(row["command_id"]),
            "revision_id": str(row["revision_id"]),
            "status": str(row["status"]),
            "target_effect_id": str(row["target_effect_id"]),
            "before_hash": str(row["before_hash"]),
            "after_hash": str(row["after_hash"]),
            "evidence_refs": str(row["evidence_refs"]),
            "created_at": str(row["receipt_created_at"]),
        }
    )
    return bool(
        row["status"] == "committed"
        and row["command_type"] == "execute_material_action"
        and command.command_id == row["command_id"]
        and command.payload_hash == row["payload_hash"]
        and command_payload.get("decision_revision_id")
        == decision.revision_id
        and command_payload.get("action_id") == action_ref["action_id"]
        and command_payload.get("effect_id") == action_ref["effect_id"]
        and material_ref["effect_receipt_id"] == row["receipt_id"]
        and material_ref["effect_receipt_hash"] == receipt_hash
    )


def _audit_runs(
    runs: Sequence[Mapping[str, Any]],
    *,
    revisions: Mapping[str, Mapping[str, Any]],
    metrics: dict[str, int],
    denominators: dict[str, int],
) -> None:
    denominators["training_runs"] = len(runs)
    for revision in runs:
        payload = revision["payload"]
        try:
            validate_cognitive_state_payload("training_run_record", payload)
        except (KeyError, TypeError, ValueError):
            metrics["model_manifest_hash_mismatch"] += 1
            continue
        if payload["state"] == "model_sealed":
            metrics["phase3_training_contract_gap"] += 1
        if payload["state"] in {"sealed", "applied"} and not _durable_seal_lineage_valid(
            revision,
            revisions,
        ):
            metrics["model_manifest_hash_mismatch"] += 1
        refs = payload["admission_refs"]
        if payload["dataset_manifest"] != training_dataset_manifest(refs):
            metrics["model_manifest_hash_mismatch"] += 1
        fit_hash = training_fit_input_hash(refs)
        if (
            payload["fit_input_hash"] != fit_hash
            or payload["algorithm"]["selection_input_hash"] != fit_hash
        ):
            metrics["holdout_leak"] += 1
        if payload["run_input_hash"] != training_run_input_hash(payload):
            metrics["model_manifest_hash_mismatch"] += 1
        for ref in refs:
            admission = revisions.get(str(ref["revision_id"]))
            if (
                admission is None
                or admission.get("object_type") != "training_admission_record"
                or admission.get("payload_hash") != ref["payload_hash"]
            ):
                metrics["model_without_training_manifest"] += 1
        if payload["model_artifact"].get("model_id"):
            try:
                run_admissions = [revisions[str(ref["revision_id"])] for ref in refs]
                examples = governed_training_examples(run_admissions)
                expected_bayesian = derive_bayesian_prior_artifact(
                    run_id=str(payload["run_id"]),
                    examples=examples,
                )
                expected_optimizer = derive_rule_optimizer_artifact(
                    run_id=str(payload["run_id"]),
                    examples=examples,
                )
                if payload["bayesian_prior_artifact"] != expected_bayesian:
                    metrics["bayesian_update_without_admission"] += 1
                if payload["rule_optimizer_artifact"] != expected_optimizer:
                    metrics["optimizer_update_without_admission"] += 1
            except (KeyError, TypeError, ValueError):
                metrics["bayesian_update_without_admission"] += 1
                metrics["optimizer_update_without_admission"] += 1


def _durable_seal_lineage_valid(
    run: Mapping[str, Any],
    revisions: Mapping[str, Mapping[str, Any]],
) -> bool:
    if run["payload"]["state"] == "applied":
        sealed = revisions.get(str(run["supersedes_revision_id"]))
        if (
            sealed is None
            or sealed.get("object_type") != "training_run_record"
            or sealed.get("object_id") != run.get("object_id")
            or sealed["payload"].get("state") != "sealed"
            or run.get("source_revision_id") != sealed.get("revision_id")
            or run.get("source_content_hash") != sealed.get("payload_hash")
        ):
            return False
    else:
        sealed = run
    model_seal = revisions.get(str(sealed["supersedes_revision_id"]))
    if (
        model_seal is None
        or model_seal.get("object_type") != "training_run_record"
        or model_seal.get("object_id") != sealed.get("object_id")
        or model_seal["payload"].get("state") != "model_sealed"
        or sealed.get("source_revision_id") != model_seal.get("revision_id")
        or sealed.get("source_content_hash") != model_seal.get("payload_hash")
    ):
        return False
    stable_fields = (
        "access_control",
        "run_id",
        "dimension",
        "algorithm",
        "admission_refs",
        "dataset_manifest",
        "fit_input_hash",
        "validation_report",
        "parent_model_ref",
        "model_artifact",
        "bayesian_prior_artifact",
        "rule_optimizer_artifact",
        "rebuild_of_revision_id",
    )
    pairs = [(sealed, model_seal)]
    if run["payload"]["state"] == "applied":
        pairs.append((run, sealed))
    return bool(
        not model_seal["payload"]["holdout_report"]["evaluated_after_model_sealed_at"]
        and sealed["payload"]["holdout_report"]["evaluated_after_model_sealed_at"]
        and all(
            successor["payload"][field] == predecessor["payload"][field]
            for successor, predecessor in pairs
            for field in stable_fields
        )
    )


def _audit_projection(
    conn: sqlite3.Connection,
    *,
    current_admissions: Sequence[Mapping[str, Any]],
    current_runs: Sequence[Mapping[str, Any]],
    revisions: Mapping[str, Mapping[str, Any]],
    commands: Mapping[str, Mapping[str, Any]],
    state_effect_receipts: Mapping[str, Mapping[str, Any]],
    metrics: dict[str, int],
    denominators: dict[str, int],
) -> dict[str, str]:
    audit_sample_projection_receipts(
        conn,
        revisions=revisions,
        commands=commands,
        state_effect_receipts=state_effect_receipts,
        metrics=metrics,
        denominators=denominators,
    )
    sample_state: dict[str, str] = {}
    rows = conn.execute(
        """
        SELECT sample.admission_revision_id, sample.sample_id,
               (
                   SELECT action.action_type
                   FROM governed_training_sample_actions AS action
                   WHERE action.sample_id=sample.sample_id
                   ORDER BY action.created_at DESC, action.action_id DESC
                   LIMIT 1
               ) AS action_type,
               (
                   SELECT receipt.status
                   FROM governed_training_sample_receipts AS receipt
                   WHERE receipt.sample_id=sample.sample_id
                   ORDER BY receipt.created_at DESC, receipt.receipt_id DESC
                   LIMIT 1
               ) AS receipt_status
        FROM governed_training_samples AS sample
        """
    ).fetchall()
    for row in rows:
        action = str(row["action_type"] or "missing")
        receipt = str(row["receipt_status"] or "missing")
        sample_state[str(row["admission_revision_id"])] = (
            action if receipt in {"committed", "revoked"} else "missing"
        )
        if action == "missing" or receipt == "missing":
            metrics["training_effect_without_receipt"] += 1
    duplicates = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT sample_id FROM governed_training_sample_actions
            WHERE action_type='admit'
            GROUP BY sample_id HAVING COUNT(*)>1
        )
        """
    ).fetchone()
    metrics["duplicate_training_effect"] += int(duplicates[0] if duplicates else 0)

    models = conn.execute("SELECT * FROM governed_scorer_models ORDER BY model_id").fetchall()
    heads = conn.execute("SELECT * FROM governed_scorer_model_heads ORDER BY dimension").fetchall()
    run_receipts = conn.execute(
        "SELECT * FROM governed_training_run_receipts ORDER BY run_revision_id"
    ).fetchall()
    aux_effects = conn.execute(
        "SELECT * FROM governed_training_aux_effects ORDER BY run_revision_id, effect_kind"
    ).fetchall()
    aux_receipts = conn.execute(
        "SELECT * FROM governed_training_aux_receipts ORDER BY run_revision_id, effect_kind"
    ).fetchall()
    denominators["governed_models"] = len(models)
    denominators["active_model_heads"] = len(heads)
    denominators["bayesian_effects"] = sum(
        str(row["effect_kind"]) == "bayesian_prior" for row in aux_effects
    )
    denominators["optimizer_effects"] = sum(
        str(row["effect_kind"]) == "rule_optimizer" for row in aux_effects
    )
    current_run_index = {str(item["revision_id"]): item for item in current_runs}
    all_runs = {
        str(item["revision_id"]): item
        for item in revisions.values()
        if item.get("object_type") == "training_run_record"
    }
    models_by_id = {str(row["model_id"]): row for row in models}
    for row in models:
        run = all_runs.get(str(row["run_revision_id"]))
        if run is None:
            metrics["model_without_training_manifest"] += 1
            continue
        payload = run["payload"]
        try:
            refs_json = json.loads(str(row["admission_revision_ids_json"]))
            stored_blob = json.loads(str(row["model_blob_json"]))
            expected_refs = [str(item["revision_id"]) for item in payload["admission_refs"]]
            expected_blob = _recompute_model_blob(payload["admission_refs"], revisions)
            expected_model = payload["model_artifact"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            metrics["model_manifest_hash_mismatch"] += 1
            continue
        if (
            refs_json != expected_refs
            or str(row["run_payload_hash"]) != run["payload_hash"]
            or str(row["dataset_manifest_hash"]) != payload["dataset_manifest"]["manifest_hash"]
            or str(row["fit_input_hash"]) != payload["fit_input_hash"]
            or stored_blob != expected_blob
            or stored_blob != expected_model["blob"]
            or str(row["model_blob_hash"]) != expected_model["blob_hash"]
            or sha256_json(stored_blob) != expected_model["blob_hash"]
            or str(row["validation_report_hash"]) != payload["validation_report"]["report_hash"]
            or str(row["holdout_report_hash"]) != payload["holdout_report"]["report_hash"]
        ):
            metrics["model_manifest_hash_mismatch"] += 1
    for head in heads:
        model = models_by_id.get(str(head["model_id"]))
        run = current_run_index.get(str(head["run_revision_id"]))
        if model is None or run is None or run["payload"]["state"] != "applied":
            metrics["stale_model_active"] += 1

    expected_run_receipts: list[tuple[Any, ...]] = []
    expected_aux_effects: list[tuple[Any, ...]] = []
    expected_aux_receipts: list[tuple[Any, ...]] = []
    run_commands = sorted(
        (
            command
            for command in commands.values()
            if command["command_type"] == "project_governed_training_run"
        ),
        key=lambda item: str(item["command_id"]),
    )
    denominators["run_projection_commands"] = len(run_commands)
    denominators["run_projection_receipts"] = len(run_receipts)
    for command in run_commands:
        run = all_runs.get(str(command["revision_id"]))
        command_payload = command["payload"]
        if (
            run is None
            or command_payload.get("run_revision_id") != command["revision_id"]
            or command_payload.get("run_id") != run["object_id"]
            or command_payload.get("run_payload_hash") != run["payload_hash"]
            or command_payload.get("state") != run["payload"]["state"]
        ):
            metrics["training_effect_without_receipt"] += 1
            continue
        try:
            expected_receipt, receipt_context = _expected_run_receipt(
                run,
                command,
                revisions=revisions,
            )
            aux_effect_rows, aux_receipt_rows, aux_refs = _expected_aux_projection(
                run,
                command,
                run_before_hash=str(receipt_context["before_hash"]),
            )
        except (KeyError, TypeError, ValueError):
            metrics["training_effect_without_receipt"] += 1
            continue
        expected_run_receipts.append(expected_receipt)
        expected_aux_effects.extend(aux_effect_rows)
        expected_aux_receipts.extend(aux_receipt_rows)
        state_receipt = state_effect_receipts.get(str(command["command_id"]))
        expected_state_evidence = (
            f"training-run:{run['revision_id']}",
            "governed-training-run-receipt:"
            + str(expected_receipt[0])
            + ":"
            + str(expected_receipt[11]),
            *((f"governed-scorer-model:{expected_receipt[4]}",) if expected_receipt[4] else ()),
            *aux_refs,
        )
        if (
            state_receipt is None
            or state_receipt.get("command_id") != command["command_id"]
            or state_receipt.get("status") != "committed"
            or state_receipt.get("revision_id") != run["revision_id"]
            or state_receipt.get("event_id") != command["event_id"]
            or state_receipt.get("consumer_id") != command["consumer_id"]
            or state_receipt.get("target_effect_id")
            != run["payload"]["material_effect_refs"]["effect_id"]
            or state_receipt.get("before_hash") != receipt_context["before_hash"]
            or state_receipt.get("after_hash") != receipt_context["after_hash"]
            or tuple(state_receipt.get("evidence_refs") or ()) != expected_state_evidence
        ):
            metrics["training_effect_without_receipt"] += 1

    actual_run_receipts = [tuple(row) for row in run_receipts]
    actual_aux_effects = [tuple(row) for row in aux_effects]
    actual_aux_receipts = [tuple(row) for row in aux_receipts]
    expected_run_receipts.sort(key=lambda row: row[2])
    expected_aux_effects.sort(key=lambda row: (row[2], row[1]))
    expected_aux_receipts.sort(key=lambda row: (row[2], row[3]))
    if actual_run_receipts != expected_run_receipts:
        metrics["training_effect_without_receipt"] += 1
    if actual_aux_effects != expected_aux_effects:
        kinds = {str(row[1]) for row in actual_aux_effects + expected_aux_effects}
        if "bayesian_prior" in kinds:
            metrics["bayesian_update_without_admission"] += 1
        if "rule_optimizer" in kinds:
            metrics["optimizer_update_without_admission"] += 1
    if actual_aux_receipts != expected_aux_receipts:
        metrics["training_effect_without_receipt"] += 1
    for row in aux_effects:
        run = all_runs.get(str(row["run_revision_id"]))
        if run is None:
            if row["effect_kind"] == "bayesian_prior":
                metrics["bayesian_update_without_admission"] += 1
            else:
                metrics["optimizer_update_without_admission"] += 1
            continue
        train_ids = [
            str(item["revision_id"])
            for item in run["payload"]["admission_refs"]
            if item["split"] == "train"
        ]
        try:
            stored_ids = json.loads(str(row["admission_revision_ids_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            stored_ids = []
        if stored_ids != train_ids:
            metrics["holdout_leak"] += 1
    receipts_without_samples = conn.execute(
        """
        SELECT COUNT(*)
        FROM governed_training_sample_receipts AS receipt
        LEFT JOIN governed_training_samples AS sample
          ON sample.sample_id=receipt.sample_id
        WHERE sample.sample_id IS NULL
        """
    ).fetchone()
    metrics["training_effect_without_receipt"] += int(
        receipts_without_samples[0] if receipts_without_samples else 0
    )
    return sample_state


def _recompute_model_blob(
    admission_refs: Sequence[Mapping[str, Any]],
    revisions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    grouped: dict[int, list[tuple[float, ...]]] = {0: [], 1: []}
    for ref in admission_refs:
        if ref["split"] != "train":
            continue
        revision = revisions[str(ref["revision_id"])]
        payload = revision["payload"]
        label = int(payload["label"]["numeric_value"])
        grouped[label].append(
            tuple(float(payload["feature_snapshot"]["values"][name]) for name in FEATURE_NAMES)
        )
    if not grouped[0] or not grouped[1]:
        raise ValueError("governed model lacks both train classes")

    def centroid(rows: Sequence[tuple[float, ...]]) -> list[float]:
        return [sum(row[index] for row in rows) / len(rows) for index in range(len(FEATURE_NAMES))]

    return {
        "feature_names": list(FEATURE_NAMES),
        "negative_centroid": centroid(grouped[0]),
        "positive_centroid": centroid(grouped[1]),
    }
