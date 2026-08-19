"""Independent runtime audit for PredictionRecord and OutcomeMeasurement lineage."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from core.cognitive.prediction_history_migration import (
    REASON_CODE,
    SOURCE_TABLE,
    build_prediction_history_inventory,
    inspect_prediction_history_coverage,
    inspect_prediction_target,
)
from core.cognitive.prediction_ledger import (
    OUTCOME_MEASUREMENT_METHOD_REGISTRY,
    OUTCOME_MEASUREMENT_REGISTRY_HASH,
    OUTCOME_MEASUREMENT_ISSUANCE_SCHEMA,
    PREDICTION_CODE_HASH,
    PREDICTION_CONFIDENCE_METHOD,
    PREDICTION_CONFIDENCE_VERSION,
    PREDICTION_CORRECTION_COMMAND,
    PREDICTION_CORRECTION_CONSUMER,
    PREDICTION_METRIC_ID,
    PREDICTION_SPEC_HASH,
    TASK_RESULT_OBSERVATION_SCHEMA,
    TASK_RESULT_ORACLE_ISSUER_ID,
    TASK_RESULT_ORACLE_METHOD_ID,
    PREDICTION_TERMINAL_CONSUMER,
    PredictionRecordStore,
)
from core.cognitive.state_contract import (
    LocalConsumerCommand,
    canonical_json,
    prediction_input_snapshot,
    sha256_json,
    validate_cognitive_state_payload,
)
from core.cognitive.state_store import CognitiveStateStore
from core.sync_framework.raw_event_reader import decode_raw_revision_snapshot


AUDIT_SCHEMA_VERSION = "mnemos.prediction_outcome_lineage_audit.v1"
ZERO_METRICS = (
    "outcome_without_prediction",
    "mature_prediction_without_terminal",
    "predictive_delivery_without_presealed_prediction",
    "prediction_after_delivery_effect",
    "prediction_decision_action_binding_mismatch",
    "prediction_payload_hash_mismatch",
    "prediction_terminal_conflict",
    "prediction_terminal_projection_receipt_mismatch",
    "prediction_terminal_correction_receipt_mismatch",
    "multiple_current_eligible_outcomes",
    "terminal_state_derivation_mismatch",
    "reaction_used_as_objective_outcome",
    "ineligible_measurement_used_for_error",
    "objective_measurement_issuance_receipt_gap",
    "score_band_used_as_probability",
    "calibration_input_or_report_hash_mismatch",
    "historical_prediction_inference_count",
    "historical_predictive_object_uncovered",
)


def audit_prediction_outcome_lineage(
    *,
    delivery_db: Path,
    target_db: Path,
    repo_root: Path,
    now: datetime | None = None,
    raw_db: Path | None = None,
) -> dict[str, Any]:
    """Recompute prediction lineage without trusting projection receipts."""

    timestamp = now or datetime.now(timezone.utc)
    delivery_path = Path(delivery_db).expanduser().resolve(strict=False)
    target_path = Path(target_db).expanduser().resolve(strict=False)
    raw_path = Path(raw_db or target_path.parent / "raw_events.db").expanduser().resolve(
        strict=False
    )
    if not delivery_path.is_file() and not target_path.is_file():
        daemon_registration_missing = _daemon_registration_missing(repo_root)
        metrics = {key: 0 for key in ZERO_METRICS}
        return {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "ok": daemon_registration_missing == 0,
            "status": (
                "not_initialized"
                if daemon_registration_missing == 0
                else "fail"
            ),
            "blocking_count": daemon_registration_missing,
            "metrics": metrics,
            "additional_metrics": {
                "schema_or_activation_error": 0,
                "prediction_projection_receipt_gap": 0,
                "prediction_daemon_registration_missing": (
                    daemon_registration_missing
                ),
            },
            "denominators": {
                "historical_predictive_objects": 0,
                "historical_quarantine": 0,
                "runtime_predictive_deliveries": 0,
                "prediction_revisions": 0,
                "open_not_mature": 0,
                "measured": 0,
                "unknown": 0,
                "censored": 0,
                "confounded": 0,
                "outcome_measurements": 0,
                "prediction_corrections": 0,
                "user_reactions": 0,
                "calibration_eligible": 0,
                "historical_inventory_snapshot_objects": 0,
            },
            "target": {"status": "not_initialized"},
            "history_coverage": {"status": "not_initialized", "ok": True},
            "findings": (
                []
                if daemon_registration_missing == 0
                else [
                    {
                        "code": "prediction_daemon_registration_missing",
                        "severity": "blocking",
                        "message": "prediction_daemon_registration_missing=1",
                    }
                ]
            ),
        }
    if not delivery_path.is_file() or not target_path.is_file():
        raise RuntimeError(
            "prediction audit requires both delivery and canonical state databases"
        )
    metrics = {key: 0 for key in ZERO_METRICS}
    findings: list[dict[str, str]] = []
    target = inspect_prediction_target(target_db)
    coverage = inspect_prediction_history_coverage(delivery_db, target_db)
    metrics["historical_predictive_object_uncovered"] = int(
        coverage["historical_predictive_object_uncovered"]
    ) + int(coverage["unexpected_historical_quarantine_count"])
    schema_marker_error = int(
        target.get("schema_classification") != "canonical"
        or target.get("integrity_check") != "ok"
        or not target.get("activation_marker")
    )
    inventory = build_prediction_history_inventory(delivery_db)
    historical_ids = _historical_ids(target_db)

    with _connect_read_only(target_db) as conn:
        raw_revisions = conn.execute(
            "SELECT revision_id, object_type, object_id, source_event_id, "
            "payload_json, payload_hash, evidence_refs, source_content_hash, "
            "created_at, supersedes_revision_id, correction_of_revision_id "
            "FROM cognitive_state_revisions "
            "WHERE object_type IN ('prediction_record','outcome_measurement',"
            "'user_reaction_event','decision_trace') ORDER BY created_at, revision_id"
        ).fetchall()
        heads = {
            (str(row[0]), str(row[1])): str(row[2])
            for row in conn.execute(
                "SELECT object_type, object_id, revision_id FROM cognitive_state_heads"
            )
        }
        commands: list[dict[str, Any]] = [
            {
                "command_id": str(row[0]),
                "revision_id": str(row[1]),
                "command_type": str(row[2]),
                "payload": _json_object(row[3]),
                "receipt": (
                    {
                        "receipt_id": str(row[4]),
                        "revision_id": str(row[5]),
                        "status": str(row[6]),
                        "target_effect_id": str(row[7]),
                        "before_hash": str(row[8]),
                        "after_hash": str(row[9]),
                        "evidence_refs": _json_string_tuple(row[10]),
                        "consumer_id": str(row[14]),
                        "outcome": str(row[15]),
                    }
                    if row[4]
                    else None
                ),
                "consumer_id": str(row[11]),
                "payload_hash": str(row[12]),
                "created_at": str(row[13]),
            }
            for row in conn.execute(
                "SELECT o.command_id, o.revision_id, o.command_type, o.payload_json, "
                "r.receipt_id, r.revision_id, r.status, r.target_effect_id, "
                "r.before_hash, r.after_hash, r.evidence_refs, "
                "o.consumer_id, o.payload_hash, o.created_at, "
                "r.consumer_id, COALESCE(c.outcome, '') "
                "FROM cognitive_state_outbox AS o "
                "LEFT JOIN cognitive_state_effect_receipts AS r "
                "ON r.command_id=o.command_id "
                "LEFT JOIN cognitive_data_consumptions AS c "
                "ON c.consumption_id=r.consumption_id"
            )
        ]

    revisions: dict[str, dict[str, Any]] = {}
    predictions: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    reactions: list[dict[str, Any]] = []
    decisions: dict[str, dict[str, Any]] = {}
    for row in raw_revisions:
        payload = _json_object(row[4])
        item: dict[str, Any] = {
            "revision_id": str(row[0]),
            "object_type": str(row[1]),
            "object_id": str(row[2]),
            "source_event_id": str(row[3]),
            "payload": payload,
            "payload_hash": str(row[5]),
            "evidence_refs": _json_string_tuple(row[6]),
            "source_content_hash": str(row[7]),
            "created_at": str(row[8]),
            "supersedes_revision_id": str(row[9] or ""),
            "correction_of_revision_id": str(row[10] or ""),
        }
        revisions[item["revision_id"]] = item
        if not payload or sha256_json(payload) != item["payload_hash"]:
            metrics["prediction_payload_hash_mismatch"] += int(
                item["object_type"] in {"prediction_record", "outcome_measurement"}
            )
        if item["object_type"] in {"prediction_record", "outcome_measurement"}:
            try:
                validate_cognitive_state_payload(item["object_type"], payload)
            except (KeyError, TypeError, ValueError):
                metrics["prediction_payload_hash_mismatch"] += 1
        if item["object_type"] == "prediction_record":
            predictions.append(item)
            try:
                expected_input = sha256_json(prediction_input_snapshot(payload))
            except (KeyError, TypeError, ValueError):
                expected_input = ""
            if expected_input != str(payload.get("prediction_input_hash") or ""):
                metrics["prediction_payload_hash_mismatch"] += 1
            confidence = payload.get("confidence")
            if (
                isinstance(confidence, Mapping)
                and confidence.get("method") == PREDICTION_CONFIDENCE_METHOD
                and confidence.get("is_probability") is not False
            ):
                metrics["score_band_used_as_probability"] += 1
            if not isinstance(confidence, Mapping) or (
                confidence.get("code_hash") != PREDICTION_CODE_HASH
                or confidence.get("spec_hash") != PREDICTION_SPEC_HASH
            ):
                metrics["prediction_payload_hash_mismatch"] += 1
        elif item["object_type"] == "outcome_measurement":
            outcomes.append(item)
        elif item["object_type"] == "user_reaction_event":
            reactions.append(item)
        elif item["object_type"] == "decision_trace":
            decisions[item["revision_id"]] = item

    prediction_by_revision = {item["revision_id"]: item for item in predictions}
    current_predictions = [
        item
        for item in predictions
        if heads.get(("prediction_record", item["object_id"])) == item["revision_id"]
    ]
    current_outcomes = [
        item
        for item in outcomes
        if heads.get(("outcome_measurement", item["object_id"])) == item["revision_id"]
    ]
    outcome_by_revision = {item["revision_id"]: item for item in outcomes}

    correction_commands = [
        command
        for command in commands
        if command["command_type"] == PREDICTION_CORRECTION_COMMAND
    ]
    correction_outcomes = [
        outcome
        for outcome in outcomes
        if outcome["correction_of_revision_id"]
    ]
    checked_correction_commands: set[str] = set()
    for outcome in correction_outcomes:
        matches = [
            command
            for command in correction_commands
            if command["revision_id"] == outcome["revision_id"]
        ]
        if len(matches) != 1:
            metrics["prediction_terminal_correction_receipt_mismatch"] += max(
                1,
                len(matches),
            )
            continue
        command = matches[0]
        checked_correction_commands.add(str(command["command_id"]))
        if command["receipt"] is not None and not _prediction_correction_receipt_valid(
            outcome,
            command,
            revisions=revisions,
            commands=commands,
            heads=heads,
        ):
            metrics["prediction_terminal_correction_receipt_mismatch"] += 1
    metrics["prediction_terminal_correction_receipt_mismatch"] += sum(
        str(command["command_id"]) not in checked_correction_commands
        for command in correction_commands
    )

    for outcome in current_outcomes:
        matches = [
            command
            for command in commands
            if command["command_type"] == "project_prediction_outcome"
            and command["revision_id"] == outcome["revision_id"]
        ]
        if len(matches) != 1 or not _outcome_issuance_receipt_valid(
            outcome,
            matches[0] if matches else {},
        ):
            metrics["objective_measurement_issuance_receipt_gap"] += 1

    for prediction in current_predictions:
        open_prediction = _open_prediction(prediction, revisions)
        eligible = [
            outcome
            for outcome in current_outcomes
            if _outcome_binding_valid(open_prediction, outcome, raw_path)
        ]
        if len(eligible) > 1:
            metrics["multiple_current_eligible_outcomes"] += len(eligible) - 1

    for outcome_row in outcomes:
        raw_refs = tuple(
            str(value)
            for value in dict(
                outcome_row["payload"].get("raw_evidence") or {}
            ).get("refs", ())
        )
        if any("reaction" in value.lower() for value in raw_refs):
            metrics["reaction_used_as_objective_outcome"] += 1
        ref = outcome_row["payload"].get("prediction_ref")
        outcome_prediction = (
            prediction_by_revision.get(str(ref.get("revision_id") or ""))
            if isinstance(ref, Mapping)
            else None
        )
        if (
            outcome_prediction is None
            or outcome_prediction["payload"].get("revision_state") != "open"
        ):
            metrics["outcome_without_prediction"] += 1
            continue
        if not _outcome_binding_valid(
            outcome_prediction,
            outcome_row,
            raw_path,
        ):
            # Invalid outcomes may remain stored for audit, but cannot close error.
            continue

    prediction_groups: dict[str, list[dict[str, Any]]] = {}
    for prediction in predictions:
        prediction_groups.setdefault(prediction["object_id"], []).append(prediction)
        delivery_ref = prediction["payload"].get("delivery_ref")
        if (
            isinstance(delivery_ref, Mapping)
            and str(delivery_ref.get("event_id") or "") in historical_ids
        ):
            metrics["historical_prediction_inference_count"] += 1
    for rows in prediction_groups.values():
        terminal_rows = [
            item
            for item in rows
            if item["payload"].get("revision_state") == "terminal"
        ]
        children: dict[str, int] = {}
        for item in terminal_rows:
            parent = str(item["supersedes_revision_id"])
            children[parent] = children.get(parent, 0) + 1
            parent_row = revisions.get(parent)
            if parent_row is None:
                metrics["prediction_terminal_conflict"] += 1
            elif parent_row["payload"].get("revision_state") == "terminal" and (
                item["correction_of_revision_id"] != parent
            ):
                metrics["prediction_terminal_conflict"] += 1
        metrics["prediction_terminal_conflict"] += sum(
            value - 1 for value in children.values() if value > 1
        )

    terminal_counts = {key: 0 for key in ("measured", "unknown", "censored", "confounded")}
    calibration_inputs: list[dict[str, str]] = []
    independent_eligible = 0
    independent_correct = 0
    independent_incorrect = 0
    independent_exclusions: dict[str, int] = {}
    independent_matrix = {
        "not_useful": {"not_useful": 0, "useful": 0},
        "useful": {"not_useful": 0, "useful": 0},
    }
    for prediction in sorted(current_predictions, key=lambda item: item["object_id"]):
        payload = prediction["payload"]
        state = str(dict(payload.get("terminal") or {}).get("state") or "")
        if state == "open":
            try:
                if _timestamp(payload["evaluation_window"]["ends_at"]) <= timestamp:
                    metrics["mature_prediction_without_terminal"] += 1
            except (KeyError, TypeError, ValueError):
                metrics["prediction_payload_hash_mismatch"] += 1
            continue
        if state not in terminal_counts:
            metrics["prediction_terminal_conflict"] += 1
            continue
        terminal_counts[state] += 1
        outcome_ref = payload.get("outcome_ref")
        selected_outcome = (
            outcome_by_revision.get(str(outcome_ref.get("revision_id") or ""))
            if isinstance(outcome_ref, Mapping)
            else None
        )
        if state in {"measured", "confounded"}:
            if selected_outcome is None or not _outcome_binding_valid(
                _open_prediction(prediction, revisions),
                selected_outcome,
                raw_path,
            ):
                metrics["ineligible_measurement_used_for_error"] += 1
            else:
                predicted = str(payload["metric"]["predicted_value"])
                observed = str(selected_outcome["payload"]["observed_value"])
                expected_error = 0 if predicted == observed else 1
                error = dict(payload.get("error") or {})
                causes = tuple(
                    str(value)
                    for value in dict(
                        selected_outcome["payload"].get("attribution") or {}
                    ).get("competing_causes", ())
                )
                evaluated_at = _safe_timestamp(
                    dict(payload.get("terminal") or {}).get("evaluated_at")
                )
                matured_at = _safe_timestamp(
                    dict(
                        selected_outcome["payload"].get("maturity") or {}
                    ).get("matured_at")
                )
                if (
                    error != {"kind": "categorical_miss", "value": expected_error}
                    or (state == "measured") == bool(causes)
                    or evaluated_at < matured_at
                ):
                    metrics["ineligible_measurement_used_for_error"] += 1
        elif not _nonmeasurement_terminal_valid(
            prediction,
            state=state,
            revisions=revisions,
            outcomes=current_outcomes,
            reactions=reactions,
            raw_db=raw_path,
        ):
            metrics["terminal_state_derivation_mismatch"] += 1
        calibration = dict(payload.get("calibration") or {})
        if calibration.get("eligible"):
            if state != "measured" or selected_outcome is None:
                metrics["ineligible_measurement_used_for_error"] += 1
            else:
                predicted = str(payload["metric"]["predicted_value"])
                observed = str(selected_outcome["payload"]["observed_value"])
                if predicted not in independent_matrix or observed not in independent_matrix[predicted]:
                    metrics["calibration_input_or_report_hash_mismatch"] += 1
                else:
                    independent_matrix[predicted][observed] += 1
                    independent_eligible += 1
                    independent_correct += int(predicted == observed)
                    independent_incorrect += int(predicted != observed)
        else:
            reason = str(calibration.get("exclusion_reason") or "")
            independent_exclusions[reason] = independent_exclusions.get(reason, 0) + 1
        calibration_inputs.append(
            {
                "prediction_id": prediction["object_id"],
                "terminal_revision_id": prediction["revision_id"],
                "terminal_revision_hash": prediction["payload_hash"],
            }
        )

    delivery_rows = _predictive_delivery_rows(delivery_db)
    for delivery in delivery_rows:
        event_id = str(delivery.get("event_id") or "")
        if event_id in historical_ids:
            continue
        metadata = _json_object(delivery.get("metadata_json"))
        ref = metadata.get("prediction_record") if metadata else None
        if not isinstance(ref, Mapping):
            metrics["predictive_delivery_without_presealed_prediction"] += 1
            continue
        delivery_prediction = prediction_by_revision.get(
            str(ref.get("prediction_revision_id") or "")
        )
        if delivery_prediction is None:
            metrics["predictive_delivery_without_presealed_prediction"] += 1
            continue
        if not _delivery_binding_valid(delivery, delivery_prediction, ref):
            metrics["prediction_decision_action_binding_mismatch"] += 1
        try:
            if _timestamp(delivery_prediction["created_at"]) > _timestamp(
                delivery["created_at"]
            ):
                metrics["prediction_after_delivery_effect"] += 1
        except ValueError:
            metrics["prediction_after_delivery_effect"] += 1
        if not _decision_action_binding_valid(
            delivery_prediction,
            decisions,
            commands,
        ):
            metrics["prediction_decision_action_binding_mismatch"] += 1

    projection_receipt_gap = sum(
        1
        for command in commands
        if command["command_type"]
        in {
            "project_prediction_delivery",
            "project_prediction_outcome",
            "project_prediction_terminal",
            PREDICTION_CORRECTION_COMMAND,
        }
        and command["receipt"] is None
    )
    metrics["prediction_terminal_projection_receipt_mismatch"] = sum(
        1
        for command in commands
        if command["command_type"] == "project_prediction_terminal"
        and command["receipt"] is not None
        and not _terminal_projection_receipt_valid(command, revisions)
    )
    daemon_registration_missing = _daemon_registration_missing(repo_root)
    if current_predictions and not metrics["prediction_payload_hash_mismatch"]:
        try:
            report = PredictionRecordStore(CognitiveStateStore(target_db)).calibration_report()
            input_hash = sha256_json(calibration_inputs)
            core = {
                "schema_version": "mnemos.prediction_calibration_report.v1",
                "metric_id": PREDICTION_METRIC_ID,
                "method": PREDICTION_CONFIDENCE_METHOD,
                "method_version": PREDICTION_CONFIDENCE_VERSION,
                "code_hash": PREDICTION_CODE_HASH,
                "spec_hash": PREDICTION_SPEC_HASH,
                "starts_at": "",
                "ends_at": "",
                "counts": terminal_counts,
                "calibration_eligible": independent_eligible,
                "correct": independent_correct,
                "incorrect": independent_incorrect,
                "accuracy": (
                    independent_correct / independent_eligible
                    if independent_eligible
                    else None
                ),
                "confusion_matrix": independent_matrix,
                "exclusions": dict(sorted(independent_exclusions.items())),
                "coverage_ratios": {
                    state: (
                        terminal_counts[state] / sum(terminal_counts.values())
                        if sum(terminal_counts.values())
                        else 0.0
                    )
                    for state in ("measured", "unknown", "censored", "confounded")
                },
                "input_hash": input_hash,
            }
            expected_report_hash = sha256_json(core)
            if (
                report.input_hash != input_hash
                or report.report_hash != expected_report_hash
                or report.calibration_eligible != independent_eligible
            ):
                metrics["calibration_input_or_report_hash_mismatch"] += 1
        except (KeyError, RuntimeError, TypeError, ValueError):
            metrics["calibration_input_or_report_hash_mismatch"] += 1

    for key, value in metrics.items():
        if value:
            findings.append(
                {
                    "code": key,
                    "severity": "blocking",
                    "message": f"{key}={value}",
                }
            )
    extra_blocking = {
        "schema_or_activation_error": schema_marker_error,
        "prediction_projection_receipt_gap": projection_receipt_gap,
        "prediction_daemon_registration_missing": daemon_registration_missing,
    }
    for key, value in extra_blocking.items():
        if value:
            findings.append(
                {
                    "code": key,
                    "severity": "blocking",
                    "message": f"{key}={value}",
                }
            )
    blocking_count = sum(metrics.values()) + sum(extra_blocking.values())
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "ok": blocking_count == 0,
        "status": "pass" if blocking_count == 0 else "fail",
        "blocking_count": blocking_count,
        "metrics": metrics,
        "additional_metrics": extra_blocking,
        "denominators": {
            "historical_predictive_objects": coverage[
                "historical_predictive_object_count"
            ],
            "historical_quarantine": coverage["historical_quarantine_count"],
            "runtime_predictive_deliveries": sum(
                str(row.get("event_id") or "") not in historical_ids
                for row in delivery_rows
            ),
            "prediction_revisions": len(predictions),
            "open_not_mature": sum(
                item["payload"].get("revision_state") == "open"
                and _safe_timestamp(
                    dict(item["payload"].get("evaluation_window") or {}).get("ends_at")
                )
                > timestamp
                for item in current_predictions
            ),
            **terminal_counts,
            "outcome_measurements": len(current_outcomes),
            "prediction_corrections": len(correction_outcomes),
            "user_reactions": len(reactions),
            "calibration_eligible": independent_eligible,
            "historical_inventory_snapshot_objects": len(inventory.objects),
        },
        "target": target,
        "history_coverage": coverage,
        "findings": findings,
    }


def _daemon_registration_missing(repo_root: Path) -> int:
    """Verify scheduler registration against imported canonical symbols."""

    try:
        import mnemos_daemon
        from daemon.service_registry import DIRECT_SERVICE_TARGETS
    except (ImportError, AttributeError, RuntimeError):
        return 1
    expected_root = Path(repo_root).expanduser().resolve(strict=False)
    daemon_file = Path(str(getattr(mnemos_daemon, "__file__", ""))).resolve(
        strict=False
    )
    if daemon_file.parent != expected_root:
        return 1
    target = DIRECT_SERVICE_TARGETS.get("prediction_maturity")
    return int(
        target != "service_prediction_maturity"
        or not callable(getattr(mnemos_daemon, str(target or ""), None))
    )


def _outcome_binding_valid(
    prediction: Mapping[str, Any],
    outcome: Mapping[str, Any],
    raw_db: Path,
) -> bool:
    try:
        prediction_payload = prediction["payload"]
        payload = outcome["payload"]
        validate_cognitive_state_payload("outcome_measurement", payload)
        ref = payload["prediction_ref"]
        observation = payload["observation_window"]
        window = prediction_payload["evaluation_window"]
        authority = payload["source_authority"]["authority"]
        return bool(
            ref["prediction_id"] == prediction["object_id"]
            and ref["revision_id"] == prediction["revision_id"]
            and ref["prediction_input_hash"]
            == prediction_payload["prediction_input_hash"]
            and payload["decision_ref"] == prediction_payload["decision_ref"]
            and payload["action_ref"] == prediction_payload["action_ref"]
            and payload["delivery_ref"] == prediction_payload["delivery_ref"]
            and payload["subject"] == prediction_payload["subject"]
            and payload["metric"]["metric_id"]
            == prediction_payload["metric"]["metric_id"]
            and payload["metric"]["unit"] == prediction_payload["metric"]["unit"]
            and payload["baseline"] == prediction_payload["metric"]["baseline"]
            and _timestamp(observation["starts_at"]) >= _timestamp(window["starts_at"])
            and _timestamp(observation["ends_at"]) <= _timestamp(window["ends_at"])
            and _timestamp(payload["maturity"]["matured_at"])
            >= _timestamp(observation["ends_at"])
            and authority == "tool_observation"
            and _authority_catalog_binding_valid(payload, raw_db)
            and payload["access_control"]["scope"]
            == prediction_payload["access_control"]["scope"]
            and payload["access_control"]["owner"]
            == prediction_payload["access_control"]["owner"]
        )
    except (KeyError, TypeError, ValueError):
        return False


def _authority_catalog_binding_valid(
    payload: Mapping[str, Any],
    raw_db: Path,
) -> bool:
    authority = payload["source_authority"]
    catalog = authority["source_authority_catalog"]
    entry = authority["source_authority_entry"]
    selected = [
        value
        for value in catalog["entries"]
        if isinstance(value, Mapping)
        and value.get("source_authority_id") == authority["source_authority_id"]
    ]
    expected_role = {
        "system_policy": "system",
        "project_contract": "system",
        "explicit_user": "user",
        "tool_observation": "tool",
    }.get(str(authority["authority"]))
    identity = {
        "source_event_id": entry["source_event_id"],
        "role": entry["role"],
        "authority": entry["source_authority"],
        "span_start": entry["span_start"],
        "span_end": entry["span_end"],
        "content_sha256": entry["content_sha256"],
        "ordinal": 1,
        "segment_ordinal": 1,
    }
    expected_id = "source-authority:" + sha256_json(identity).split(":", 1)[1][:32]
    if not raw_db.is_file():
        return False
    with _connect_read_only(raw_db) as conn:
        row = conn.execute(
            "SELECT content_hash, snapshot_blob FROM raw_turn_revisions "
            "WHERE revision_id=?",
            (entry["source_event_id"],),
        ).fetchone()
    if row is None:
        return False
    snapshot = decode_raw_revision_snapshot(row[1])
    role_text = _raw_role_text(snapshot, str(entry["role"]))
    try:
        span_start = int(entry["span_start"])
        span_end = int(entry["span_end"])
    except (TypeError, ValueError):
        return False
    if role_text is None or not 0 <= span_start < span_end <= len(role_text):
        return False
    span_hash = "sha256:" + hashlib.sha256(
        role_text[span_start:span_end].encode("utf-8")
    ).hexdigest()
    raw_revision_hash = _normalized_sha256(str(row[0]))
    return bool(
        sha256_json(catalog) == authority["source_authority_catalog_hash"]
        and len(catalog["entries"]) == 1
        and len(selected) == 1
        and selected[0] == entry
        and entry["source_authority_id"] == expected_id
        and entry["source_authority"] == authority["authority"]
        and entry["source_event_id"] == authority["source_revision_id"]
        and raw_revision_hash == authority["content_hash"]
        and entry["source_revision_sha256"] == raw_revision_hash
        and entry["content_sha256"] == span_hash
        and span_hash in payload["raw_evidence"]["content_hashes"]
        and entry["span_status"] == "exact"
        and entry["role"] == expected_role
        and not entry["artifact_ref_id"]
        and _objective_measurement_registry_binding_valid(payload, snapshot)
    )


def _objective_measurement_registry_binding_valid(
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> bool:
    method = payload.get("measurement_method")
    if not isinstance(method, Mapping):
        return False
    spec = OUTCOME_MEASUREMENT_METHOD_REGISTRY.get(str(method.get("method") or ""))
    if spec is None:
        return False
    observation = _independent_task_result_observation(snapshot)
    if observation is None:
        return False
    authority = payload["source_authority"]
    expected_keys = {
        "schema_version",
        "issuer_id",
        "prediction_revision_id",
        "prediction_input_hash",
        "source_id",
        "observed_value",
        "observation_window",
        "maturity",
        "evidence_refs",
        "uncertainty",
        "attribution",
    }
    if set(observation) != expected_keys:
        return False
    measurement = {
        "observed_value": observation["observed_value"],
        "observation_window": observation["observation_window"],
        "maturity": observation["maturity"],
        "raw_evidence": {
            "refs": [str(value) for value in observation["evidence_refs"]],
            "content_hashes": [
                str(authority["source_authority_entry"]["content_sha256"])
            ],
        },
        "uncertainty": observation["uncertainty"],
        "attribution": observation["attribution"],
    }
    expected_measurement = {
        key: payload[key]
        for key in (
            "observed_value",
            "observation_window",
            "maturity",
            "raw_evidence",
            "uncertainty",
            "attribution",
        )
    }
    issuance = {
        "schema_version": OUTCOME_MEASUREMENT_ISSUANCE_SCHEMA,
        "issuer_id": TASK_RESULT_ORACLE_ISSUER_ID,
        "method_id": TASK_RESULT_ORACLE_METHOD_ID,
        "prediction_revision_id": payload["prediction_ref"]["revision_id"],
        "prediction_input_hash": payload["prediction_ref"]["prediction_input_hash"],
        "subject_id": payload["subject"]["id"],
        "metric_id": payload["metric"]["metric_id"],
        "unit": payload["metric"]["unit"],
        "source_id": authority["source_id"],
        "source_revision_id": authority["source_revision_id"],
        "source_content_hash": authority["content_hash"],
        "source_authority_id": authority["source_authority_id"],
        "measurement_hash": sha256_json(measurement),
    }
    issuance_hash = sha256_json(issuance)
    return bool(
        observation["schema_version"] == TASK_RESULT_OBSERVATION_SCHEMA
        and observation["issuer_id"] == TASK_RESULT_ORACLE_ISSUER_ID
        and observation["prediction_revision_id"]
        == payload["prediction_ref"]["revision_id"]
        and observation["prediction_input_hash"]
        == payload["prediction_ref"]["prediction_input_hash"]
        and observation["source_id"] == authority["source_id"]
        and measurement == expected_measurement
        and dict(method)
        == {
            "method": str(spec["method"]),
            "version": str(spec["version"]),
            "code_hash": str(spec["code_hash"]),
            "registry_hash": OUTCOME_MEASUREMENT_REGISTRY_HASH,
            "source_kind": str(spec["source_kind"]),
            "source_uri": (
                f"{spec['source_uri_scheme']}://"
                f"{payload['source_authority']['source_id']}"
            ),
            "attestation_hash": issuance_hash,
        }
    )


def _independent_task_result_observation(
    snapshot: Mapping[str, Any],
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    tool_results = snapshot.get("tool_results")
    if not isinstance(tool_results, (list, tuple)):
        return None
    for result in tool_results:
        if not isinstance(result, Mapping) or result.get("tool_name") != (
            TASK_RESULT_ORACLE_ISSUER_ID
        ):
            continue
        try:
            value = json.loads(str(result.get("content") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            matches.append(value)
    return matches[0] if len(matches) == 1 else None


def _outcome_issuance_receipt_valid(
    outcome: Mapping[str, Any],
    command: Mapping[str, Any],
) -> bool:
    payload = command.get("payload")
    receipt = command.get("receipt")
    if not isinstance(payload, Mapping) or not isinstance(receipt, Mapping):
        return False
    outcome_payload = outcome["payload"]
    method = outcome_payload.get("measurement_method")
    authority = outcome_payload.get("source_authority")
    if not isinstance(method, Mapping) or not isinstance(authority, Mapping):
        return False
    issuance_hash = str(method.get("attestation_hash") or "")
    source_revision_id = str(authority.get("source_revision_id") or "")
    source_content_hash = str(authority.get("content_hash") or "")
    refs = set(receipt.get("evidence_refs") or ())
    return bool(
        payload.get("outcome_revision_id") == outcome["revision_id"]
        and payload.get("outcome_revision_hash") == outcome["payload_hash"]
        and payload.get("oracle_issuance_hash") == issuance_hash
        and payload.get("oracle_source_revision_id") == source_revision_id
        and payload.get("oracle_source_content_hash") == source_content_hash
        and receipt.get("status") == "committed"
        and receipt.get("revision_id") == outcome["revision_id"]
        and receipt.get("target_effect_id") == payload.get("projection_effect_id")
        and f"objective-oracle-issuance:{issuance_hash}" in refs
        and "objective-oracle-source:"
        f"{source_revision_id}:{source_content_hash}" in refs
    )


def _nonmeasurement_terminal_valid(
    prediction: Mapping[str, Any],
    *,
    state: str,
    revisions: Mapping[str, Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
    reactions: Sequence[Mapping[str, Any]],
    raw_db: Path,
) -> bool:
    open_prediction = _open_prediction(prediction, revisions)
    payload = prediction["payload"]
    open_payload = open_prediction["payload"]
    eligible_outcomes = [
        outcome
        for outcome in outcomes
        if _outcome_binding_valid(open_prediction, outcome, raw_db)
    ]
    matching_reaction_refs: list[str] = []
    for reaction in reactions:
        delivery_ref = reaction["payload"].get("delivery_ref")
        event_id = (
            str(delivery_ref.get("event_id") or "")
            if isinstance(delivery_ref, Mapping)
            else str(delivery_ref or "")
        )
        if event_id == str(open_payload["delivery_ref"]["event_id"]):
            matching_reaction_refs.append(
                f"reaction-exposure:{reaction['revision_id']}"
            )
    reaction_refs = tuple(sorted(matching_reaction_refs))
    exposure = dict(payload.get("exposure") or {})
    terminal = dict(payload.get("terminal") or {})
    attribution = dict(payload.get("attribution") or {})
    error = dict(payload.get("error") or {})
    calibration = dict(payload.get("calibration") or {})
    try:
        mature = _timestamp(terminal.get("evaluated_at")) >= _timestamp(
            open_payload["evaluation_window"]["ends_at"]
        )
    except (KeyError, TypeError, ValueError):
        return False
    if eligible_outcomes or not mature:
        return False
    if str(terminal.get("reason") or "").startswith(
        "maturity_permanent_failure:"
    ):
        reason_parts = str(terminal["reason"]).split(":", 2)
        if len(reason_parts) != 3:
            return False
        error_type, failure_digest = reason_parts[1:]
        failure_ref = f"maturity-failure:{error_type}:{failure_digest}"
        expected_source_hash = sha256_json(
            {
                "prediction_revision_id": prediction["supersedes_revision_id"],
                "terminal": payload["terminal"],
                "outcome_ref": payload["outcome_ref"],
            }
        )
        return bool(
            state == "censored"
            and len(failure_digest) == 64
            and failure_ref in set(prediction.get("evidence_refs") or ())
            and prediction.get("source_content_hash") == expected_source_hash
            and not reaction_refs
            and exposure
            == {
                "status": (
                    "not_exposed"
                    if open_payload.get("route_disposition") == "suppress"
                    else "unproven"
                ),
                "evidence_refs": [],
            }
            and attribution
            == {"method": "maturity_evaluation_failed", "competing_causes": []}
            and error == {"kind": "none", "value": None}
            and calibration
            == {
                "eligible": False,
                "exclusion_reason": "maturity_evaluation_failure",
            }
        )
    if state == "unknown":
        return bool(
            reaction_refs
            and exposure == {"status": "proven", "evidence_refs": list(reaction_refs)}
            and terminal.get("reason") == "exposure_proven_without_eligible_outcome"
            and attribution == {
                "method": "no_eligible_measurement",
                "competing_causes": [],
            }
            and error == {"kind": "none", "value": None}
            and calibration == {
                "eligible": False,
                "exclusion_reason": "unknown_outcome",
            }
        )
    if state != "censored" or reaction_refs:
        return False
    suppressed = open_payload.get("route_disposition") == "suppress"
    return bool(
        exposure
        == {
            "status": "not_exposed" if suppressed else "unproven",
            "evidence_refs": [],
        }
        and terminal.get("reason")
        == (
            "policy_suppressed_without_exposure"
            if suppressed
            else "presentation_or_follow_up_unproven"
        )
        and attribution == {"method": "not_identifiable", "competing_causes": []}
        and error == {"kind": "none", "value": None}
        and calibration
        == {"eligible": False, "exclusion_reason": "censored_observation"}
    )


def _raw_role_text(snapshot: Mapping[str, Any], role: str) -> str | None:
    if role == "user":
        return str(snapshot.get("user_content") or "")
    if role == "assistant":
        return str(snapshot.get("assistant_content") or "")
    if role == "tool":
        return str(canonical_json(snapshot.get("tool_results") or []))
    return None


def _normalized_sha256(value: str) -> str:
    return value if value.startswith("sha256:") else "sha256:" + value


def _delivery_binding_valid(
    delivery: Mapping[str, Any],
    prediction: Mapping[str, Any],
    ref: Mapping[str, Any],
) -> bool:
    payload = prediction["payload"]
    return bool(
        prediction["object_id"] == ref.get("prediction_id")
        and prediction["payload_hash"] == ref.get("prediction_revision_hash")
        and payload.get("prediction_plan_hash") == ref.get("prediction_plan_hash")
        and dict(payload.get("delivery_ref") or {}).get("event_id")
        == delivery.get("event_id")
        and dict(payload.get("delivery_ref") or {}).get("event_payload_hash")
        == ref.get("delivery_event_payload_hash")
    )


def _prediction_correction_receipt_valid(
    outcome: Mapping[str, Any],
    command: Mapping[str, Any],
    *,
    revisions: Mapping[str, Mapping[str, Any]],
    commands: Sequence[Mapping[str, Any]],
    heads: Mapping[tuple[str, str], str],
) -> bool:
    payload = command.get("payload")
    receipt = command.get("receipt")
    expected_fields = {
        "schema_version",
        "outcome_revision_id",
        "outcome_revision_hash",
        "correction_of_outcome_revision_id",
        "prediction_id",
        "prior_prediction_terminal_revision_id",
        "prior_prediction_terminal_hash",
        "correction_effect_id",
    }
    if (
        not isinstance(payload, Mapping)
        or set(payload) != expected_fields
        or not isinstance(receipt, Mapping)
    ):
        return False
    try:
        recomputed_command = LocalConsumerCommand.create(
            revision_id=str(command["revision_id"]),
            consumer_id=str(command["consumer_id"]),
            command_type=str(command["command_type"]),
            payload=payload,
            created_at=str(command["created_at"]),
        )
        outcome_revision_id = str(payload["outcome_revision_id"])
        outcome_revision_hash = str(payload["outcome_revision_hash"])
        prior_outcome_revision_id = str(
            payload["correction_of_outcome_revision_id"]
        )
        prediction_id = str(payload["prediction_id"])
        prior_terminal_revision_id = str(
            payload["prior_prediction_terminal_revision_id"]
        )
        prior_terminal_hash = str(
            payload["prior_prediction_terminal_hash"]
        )
        correction_effect_id = str(payload["correction_effect_id"])
        prior_outcome = revisions[prior_outcome_revision_id]
        prior_terminal = revisions[prior_terminal_revision_id]
    except (KeyError, TypeError, ValueError):
        return False
    expected_effect_id = (
        "prediction-terminal-correction-effect-"
        + sha256_json(
            {
                "outcome_revision_id": outcome_revision_id,
                "prior_prediction_terminal_revision_id": (
                    prior_terminal_revision_id
                ),
            }
        ).split(":", 1)[1][:32]
    )
    expected_prior_outcome_ref = {
        "revision_id": prior_outcome_revision_id,
        "payload_hash": prior_outcome.get("payload_hash"),
    }
    corrected_candidates = [
        revision
        for revision in revisions.values()
        if revision.get("object_type") == "prediction_record"
        and revision.get("object_id") == prediction_id
        and revision.get("supersedes_revision_id")
        == prior_terminal_revision_id
        and revision.get("correction_of_revision_id")
        == prior_terminal_revision_id
        and dict(revision.get("payload") or {}).get("outcome_ref")
        == {
            "revision_id": outcome_revision_id,
            "payload_hash": outcome_revision_hash,
        }
    ]
    if len(corrected_candidates) != 1:
        return False
    corrected = corrected_candidates[0]
    terminal_commands = [
        candidate
        for candidate in commands
        if candidate.get("command_type") == "project_prediction_terminal"
        and candidate.get("revision_id") == corrected["revision_id"]
    ]
    if (
        len(terminal_commands) != 1
        or terminal_commands[0].get("receipt") is None
        or not _terminal_projection_receipt_valid(
            terminal_commands[0],
            revisions,
        )
    ):
        return False
    terminal_receipt = terminal_commands[0]["receipt"]
    expected_refs = (
        f"prediction-terminal-correction-command:{command['command_id']}",
        f"outcome-revision:{outcome_revision_id}",
        "prior-prediction-terminal:"
        f"{prior_terminal_revision_id}:{prior_terminal_hash}",
        "corrected-prediction-terminal:"
        f"{corrected['revision_id']}:{corrected['payload_hash']}",
        "prediction-terminal-effect-receipt:"
        f"{terminal_receipt['receipt_id']}",
    )
    expected_receipt_id = "cogeffect-" + sha256_json(
        {
            "command_id": command["command_id"],
            "status": "committed",
            "target_effect_id": correction_effect_id,
            "before_hash": prior_terminal_hash,
            "after_hash": corrected["payload_hash"],
            "evidence_refs": list(expected_refs),
            "terminal_reason_code": "",
            "retry_exhausted": False,
        }
    ).split(":", 1)[1][:32]
    outcome_payload = dict(outcome.get("payload") or {})
    current_outcome = heads.get(
        ("outcome_measurement", str(outcome.get("object_id") or ""))
    ) == outcome_revision_id
    return bool(
        payload["schema_version"]
        == "mnemos.prediction_terminal_correction.v1"
        and command.get("command_type") == PREDICTION_CORRECTION_COMMAND
        and command.get("consumer_id") == PREDICTION_CORRECTION_CONSUMER
        and recomputed_command.command_id == command.get("command_id")
        and recomputed_command.payload_hash == command.get("payload_hash")
        and command.get("revision_id") == outcome_revision_id
        and outcome.get("revision_id") == outcome_revision_id
        and outcome.get("payload_hash") == outcome_revision_hash
        and outcome.get("correction_of_revision_id")
        == prior_outcome_revision_id
        and outcome.get("supersedes_revision_id")
        == prior_outcome_revision_id
        and outcome_payload.get("correction_of_revision_id")
        == prior_outcome_revision_id
        and outcome_payload.get("supersedes_revision_id")
        == prior_outcome_revision_id
        and prior_outcome.get("object_type") == "outcome_measurement"
        and prior_outcome.get("object_id") == outcome.get("object_id")
        and prior_terminal.get("object_type") == "prediction_record"
        and prior_terminal.get("object_id") == prediction_id
        and prior_terminal.get("payload_hash") == prior_terminal_hash
        and dict(prior_terminal.get("payload") or {}).get("outcome_ref")
        == expected_prior_outcome_ref
        and correction_effect_id == expected_effect_id
        and (
            not current_outcome
            or heads.get(("prediction_record", prediction_id))
            == corrected["revision_id"]
        )
        and receipt.get("receipt_id") == expected_receipt_id
        and receipt.get("revision_id") == outcome_revision_id
        and receipt.get("consumer_id") == PREDICTION_CORRECTION_CONSUMER
        and receipt.get("status") == "committed"
        and receipt.get("target_effect_id") == correction_effect_id
        and receipt.get("before_hash") == prior_terminal_hash
        and receipt.get("after_hash") == corrected["payload_hash"]
        and tuple(receipt.get("evidence_refs") or ()) == expected_refs
        and receipt.get("outcome")
        == "corrected prediction terminal available"
    )


def _terminal_projection_receipt_valid(
    command: Mapping[str, Any],
    revisions: Mapping[str, Mapping[str, Any]],
) -> bool:
    payload = command.get("payload")
    receipt = command.get("receipt")
    if not isinstance(payload, Mapping) or not isinstance(receipt, Mapping):
        return False
    try:
        if set(payload) != {
            "schema_version",
            "prediction_id",
            "terminal_revision_id",
            "terminal_revision_hash",
            "terminal_state",
            "projection_effect_id",
        } or payload["schema_version"] != (
            "mnemos.prediction_terminal_projection.v1"
        ):
            return False
        recomputed_command = LocalConsumerCommand.create(
            revision_id=str(command["revision_id"]),
            consumer_id=str(command["consumer_id"]),
            command_type=str(command["command_type"]),
            payload=payload,
            created_at=str(command["created_at"]),
        )
        prediction_id = str(payload["prediction_id"])
        revision_id = str(payload["terminal_revision_id"])
        revision_hash = str(payload["terminal_revision_hash"])
        terminal_state = str(payload["terminal_state"])
        target_effect_id = str(payload["projection_effect_id"])
        revision = revisions[revision_id]
        actual_terminal_state = str(revision["payload"]["terminal"]["state"])
    except (KeyError, TypeError, ValueError):
        return False
    expected_before = sha256_json(
        {"prediction_id": prediction_id, "state": "unprojected"}
    )
    expected_after = sha256_json(
        {
            "terminal_revision_id": revision_id,
            "terminal_revision_hash": revision_hash,
            "terminal_state": terminal_state,
        }
    )
    expected_refs = (
        f"prediction-terminal-command:{command['command_id']}",
        f"prediction-revision:{revision_id}",
        f"prediction-terminal-projection:{expected_after}",
    )
    expected_receipt_id = "cogeffect-" + sha256_json(
        {
            "command_id": command["command_id"],
            "status": "committed",
            "target_effect_id": target_effect_id,
            "before_hash": expected_before,
            "after_hash": expected_after,
            "evidence_refs": list(expected_refs),
            "terminal_reason_code": "",
            "retry_exhausted": False,
        }
    ).split(":", 1)[1][:32]
    return bool(
        command.get("command_type") == "project_prediction_terminal"
        and command.get("consumer_id") == PREDICTION_TERMINAL_CONSUMER
        and recomputed_command.command_id == command.get("command_id")
        and recomputed_command.payload_hash == command.get("payload_hash")
        and command.get("revision_id") == revision_id
        and revision.get("object_id") == prediction_id
        and revision.get("payload_hash") == revision_hash
        and actual_terminal_state == terminal_state
        and receipt.get("receipt_id") == expected_receipt_id
        and receipt.get("revision_id") == revision_id
        and receipt.get("consumer_id") == PREDICTION_TERMINAL_CONSUMER
        and receipt.get("status") == "committed"
        and receipt.get("target_effect_id") == target_effect_id
        and receipt.get("before_hash") == expected_before
        and receipt.get("after_hash") == expected_after
        and tuple(receipt.get("evidence_refs") or ()) == expected_refs
        and receipt.get("outcome")
        == "deterministic prediction terminal read model available"
    )


def _decision_action_binding_valid(
    prediction: Mapping[str, Any],
    decisions: Mapping[str, Mapping[str, Any]],
    commands: Sequence[Mapping[str, Any]],
) -> bool:
    payload = prediction["payload"]
    decision_ref = dict(payload.get("decision_ref") or {})
    action_ref = dict(payload.get("action_ref") or {})
    if decision_ref.get("kind") == "trust_decision":
        return not action_ref.get("action_id") and not action_ref.get("effect_id")
    decision = decisions.get(str(decision_ref.get("revision_id") or ""))
    if decision is None or decision["payload_hash"] != decision_ref.get("revision_hash"):
        return False
    matches = [
        command
        for command in commands
        if command["command_type"] == "execute_material_action"
        and command["revision_id"] == decision["revision_id"]
        and command["payload"].get("action_id") == action_ref.get("action_id")
        and command["payload"].get("effect_id") == action_ref.get("effect_id")
    ]
    if len(matches) != 1:
        return False
    refs = matches[0]["payload"].get("prediction_refs")
    receipt = matches[0].get("receipt")
    expected_receipt_ref = (
        f"prediction-revision:{prediction['revision_id']}:"
        f"{prediction['payload_hash']}"
    )
    return bool(
        isinstance(refs, list)
        and len(refs) == 1
        and refs[0].get("prediction_revision_id") == prediction["revision_id"]
        and refs[0].get("prediction_revision_hash") == prediction["payload_hash"]
        and isinstance(receipt, Mapping)
        and receipt.get("revision_id") == decision["revision_id"]
        and receipt.get("status") == "committed"
        and receipt.get("target_effect_id") == action_ref.get("effect_id")
        and expected_receipt_ref in set(receipt.get("evidence_refs") or ())
    )


def _open_prediction(
    current: Mapping[str, Any],
    revisions: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    item = current
    seen: set[str] = set()
    while item["payload"].get("revision_state") != "open":
        revision_id = str(item["revision_id"])
        if revision_id in seen:
            return current
        seen.add(revision_id)
        parent = revisions.get(str(item.get("supersedes_revision_id") or ""))
        if parent is None:
            return current
        item = parent
    return item


def _historical_ids(target_db: Path) -> set[str]:
    with _connect_read_only(target_db) as conn:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT source_key FROM cognitive_state_migration_quarantine "
                "WHERE source_table=? AND reason_code=?",
                (SOURCE_TABLE, REASON_CODE),
            )
        }


def _predictive_delivery_rows(delivery_db: Path) -> list[dict[str, Any]]:
    with _connect_read_only(delivery_db) as conn:
        cursor = conn.execute(
            "SELECT * FROM delivery_events WHERE channel='predictive_push' "
            "ORDER BY event_id"
        )
        columns = tuple(str(value[0]) for value in cursor.description or ())
        return [dict(zip(columns, row)) for row in cursor]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_string_tuple(value: Any) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(Path(path).resolve(strict=True)), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return parsed


def _safe_timestamp(value: Any) -> datetime:
    try:
        return _timestamp(value)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
