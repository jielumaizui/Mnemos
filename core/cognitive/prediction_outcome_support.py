"""Objective outcome oracle contracts for canonical PredictionLedger measurements."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, TypeVar
from urllib.parse import urlparse

from core.cognitive.prediction_ledger_support import timestamp as _timestamp
from core.cognitive.state_contract import CognitiveStateRevision, sha256_json
from core.evidence.artifact_catalog import require_sha256_file
from core.evidence.source_authority import (
    SOURCE_AUTHORITY_SCHEMA_VERSION,
    SourceAuthority,
    SourceAuthorityEntry,
    load_source_authority_raw_snapshot,
)


TASK_RESULT_OBSERVATION_SCHEMA = "mnemos.task_result_observation.v1"
OUTCOME_MEASUREMENT_ISSUANCE_SCHEMA = "mnemos.objective_measurement_issuance.v1"
TASK_RESULT_ORACLE_ISSUER_ID = "mnemos.task_result_oracle"
TASK_RESULT_ORACLE_METHOD_ID = "task_result_oracle"
CalibrationReportT = TypeVar("CalibrationReportT")
_TASK_RESULT_ORACLE_CONTRACT = {
    "method": TASK_RESULT_ORACLE_METHOD_ID,
    "version": "v1",
    "source_kind": "objective_measurement",
    "source_uri_scheme": "oracle",
    "authority": "tool_observation",
    "observation_schema": TASK_RESULT_OBSERVATION_SCHEMA,
    "issuance_schema": OUTCOME_MEASUREMENT_ISSUANCE_SCHEMA,
    "issuer_id": TASK_RESULT_ORACLE_ISSUER_ID,
}


@dataclass(frozen=True)
class ObjectiveMeasurementIssuance:
    """System-issued measurement derived from one exact canonical Raw tool result."""

    measurement: Mapping[str, Any]
    measurement_method: Mapping[str, str]
    issuance_hash: str


class TaskResultOracle:
    """Production issuer for objective outcomes; callers cannot supply semantics."""

    @staticmethod
    def issue(
        *,
        source: Mapping[str, Any],
        prediction: CognitiveStateRevision,
        authority_entry: Any,
        raw_snapshot: Mapping[str, Any],
    ) -> ObjectiveMeasurementIssuance:
        spec = OUTCOME_MEASUREMENT_METHOD_REGISTRY[TASK_RESULT_ORACLE_METHOD_ID]
        source_id = str(source.get("source_id") or "")
        source_kind = str(source.get("source_kind") or "")
        source_uri = str(source.get("source_uri") or "")
        expected_source_uri = f"{spec['source_uri_scheme']}://{source_id}"
        authority = str(getattr(getattr(authority_entry, "authority", None), "value", ""))
        if (
            source_kind != spec["source_kind"]
            or authority != spec["authority"]
            or urlparse(source_uri).scheme != spec["source_uri_scheme"]
            or source_uri != expected_source_uri
        ):
            raise ValueError("objective measurement source contract mismatch")
        observation = TaskResultOracle._raw_observation(raw_snapshot)
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
            raise ValueError("task-result oracle observation contract mismatch")
        if (
            observation["schema_version"] != TASK_RESULT_OBSERVATION_SCHEMA
            or observation["issuer_id"] != TASK_RESULT_ORACLE_ISSUER_ID
            or observation["prediction_revision_id"] != prediction.revision_id
            or observation["prediction_input_hash"]
            != prediction.payload["prediction_input_hash"]
            or observation["source_id"] != source_id
        ):
            raise ValueError("task-result oracle observation binding mismatch")
        evidence_refs = observation["evidence_refs"]
        if (
            not isinstance(evidence_refs, (list, tuple))
            or not evidence_refs
            or any(not str(value).strip() for value in evidence_refs)
        ):
            raise ValueError("task-result oracle evidence refs are invalid")
        measurement = {
            "observed_value": observation["observed_value"],
            "observation_window": observation["observation_window"],
            "maturity": observation["maturity"],
            "raw_evidence": {
                "refs": [str(value) for value in evidence_refs],
                "content_hashes": [str(authority_entry.content_sha256)],
            },
            "uncertainty": observation["uncertainty"],
            "attribution": observation["attribution"],
        }
        issuance = {
            "schema_version": OUTCOME_MEASUREMENT_ISSUANCE_SCHEMA,
            "issuer_id": TASK_RESULT_ORACLE_ISSUER_ID,
            "method_id": TASK_RESULT_ORACLE_METHOD_ID,
            "prediction_revision_id": prediction.revision_id,
            "prediction_input_hash": prediction.payload["prediction_input_hash"],
            "subject_id": prediction.payload["subject"]["id"],
            "metric_id": prediction.payload["metric"]["metric_id"],
            "unit": prediction.payload["metric"]["unit"],
            "source_id": source_id,
            "source_revision_id": str(source.get("source_revision_id") or ""),
            "source_content_hash": str(source.get("content_hash") or ""),
            "source_authority_id": str(authority_entry.source_authority_id),
            "measurement_hash": sha256_json(measurement),
        }
        issuance_hash = sha256_json(issuance)
        method = {
            "method": str(spec["method"]),
            "version": str(spec["version"]),
            "code_hash": str(spec["code_hash"]),
            "registry_hash": OUTCOME_MEASUREMENT_REGISTRY_HASH,
            "source_kind": source_kind,
            "source_uri": source_uri,
            # Kept under the v1 field name; its value is now the server issuance hash.
            "attestation_hash": issuance_hash,
        }
        return ObjectiveMeasurementIssuance(
            measurement=MappingProxyType(measurement),
            measurement_method=MappingProxyType(method),
            issuance_hash=issuance_hash,
        )

    @staticmethod
    def _raw_observation(raw_snapshot: Mapping[str, Any]) -> dict[str, Any]:
        matches: list[dict[str, Any]] = []
        tool_results = raw_snapshot.get("tool_results")
        if not isinstance(tool_results, (list, tuple)):
            raise ValueError("task-result oracle requires canonical Raw tool results")
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
        if len(matches) != 1:
            raise ValueError("task-result oracle requires one exact Raw observation")
        return matches[0]


_ORACLE_CODE_PATH = Path(__file__).resolve()
TASK_RESULT_ORACLE_CODE_HASH = sha256_json(
    {
        "contract": _TASK_RESULT_ORACLE_CONTRACT,
        "implementation_file_hash": "sha256:"
        + require_sha256_file(_ORACLE_CODE_PATH),
    }
)
OUTCOME_MEASUREMENT_METHOD_REGISTRY = MappingProxyType(
    {
        TASK_RESULT_ORACLE_METHOD_ID: MappingProxyType(
            {
                **_TASK_RESULT_ORACLE_CONTRACT,
                "code_hash": TASK_RESULT_ORACLE_CODE_HASH,
            }
        )
    }
)
OUTCOME_MEASUREMENT_REGISTRY_HASH = sha256_json(
    {
        key: dict(value)
        for key, value in OUTCOME_MEASUREMENT_METHOD_REGISTRY.items()
    }
)


def reissue_objective_measurement(
    *,
    state_store: Any,
    prediction: CognitiveStateRevision,
    outcome: CognitiveStateRevision,
) -> ObjectiveMeasurementIssuance:
    payload = outcome.payload
    authority = payload.get("source_authority")
    if not isinstance(authority, Mapping):
        raise ValueError("outcome source authority is invalid")
    catalog = authority.get("source_authority_catalog")
    entry_payload = authority.get("source_authority_entry")
    if not isinstance(catalog, Mapping) or not isinstance(entry_payload, Mapping):
        raise ValueError("outcome source authority catalog is invalid")
    try:
        entry = SourceAuthorityEntry(
            source_authority_id=str(entry_payload["source_authority_id"]),
            authority=SourceAuthority(str(entry_payload["source_authority"])),
            source_event_id=str(entry_payload["source_event_id"]),
            role=str(entry_payload["role"]),
            purpose=str(entry_payload["purpose"]),
            content_sha256=str(entry_payload["content_sha256"]),
            span_start=int(entry_payload["span_start"]),
            span_end=int(entry_payload["span_end"]),
            span_status=str(entry_payload["span_status"]),
            source_revision_sha256=str(
                entry_payload["source_revision_sha256"]
            ),
            artifact_ref_id=str(entry_payload["artifact_ref_id"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("outcome source authority entry is invalid") from exc
    canonical_entry = entry.canonical_payload()
    expected_catalog = {
        "schema_version": SOURCE_AUTHORITY_SCHEMA_VERSION,
        "entries": [canonical_entry],
        "rejected_count": 0,
        "rejection_codes": [],
    }
    authority_identity = {
        "source_event_id": entry.source_event_id,
        "role": entry.role,
        "authority": entry.authority.value,
        "span_start": entry.span_start,
        "span_end": entry.span_end,
        "content_sha256": entry.content_sha256,
        "ordinal": 1,
        "segment_ordinal": 1,
    }
    expected_authority_id = (
        "source-authority:"
        + sha256_json(authority_identity).split(":", 1)[1][:32]
    )
    if (
        dict(entry_payload) != canonical_entry
        or dict(catalog) != expected_catalog
        or str(authority.get("source_authority_catalog_hash") or "")
        != sha256_json(expected_catalog)
        or str(authority.get("source_authority_id") or "")
        != expected_authority_id
        or entry.source_authority_id != expected_authority_id
        or entry.authority != SourceAuthority.TOOL_OBSERVATION
        or entry.span_status != "exact"
        or entry.role != "tool"
        or entry.artifact_ref_id
        or entry.source_event_id
        != str(authority.get("source_revision_id") or "")
        or entry.source_revision_sha256
        != str(authority.get("content_hash") or "")
    ):
        raise ValueError("outcome source authority binding mismatch")
    configured_raw_db = ""
    try:
        configured_raw_db = str(
            state_store.config.get("raw_event_store.db_path", "") or ""
        )
    except (AttributeError, TypeError):
        configured_raw_db = ""
    raw_db_path = (
        Path(configured_raw_db).expanduser()
        if configured_raw_db
        else state_store.db_path.parent / "raw_events.db"
    )
    raw_snapshot = load_source_authority_raw_snapshot(entry, raw_db_path)
    if raw_snapshot is None:
        raise ValueError("outcome source authority Raw span did not verify")
    method = payload.get("measurement_method")
    if not isinstance(method, Mapping):
        raise ValueError("outcome measurement method is invalid")
    source = {
        "source_id": str(authority.get("source_id") or ""),
        "source_kind": str(method.get("source_kind") or ""),
        "source_uri": str(method.get("source_uri") or ""),
        "source_revision_id": str(authority.get("source_revision_id") or ""),
        "content_hash": str(authority.get("content_hash") or ""),
    }
    issuance = TaskResultOracle.issue(
        source=source,
        prediction=prediction,
        authority_entry=entry,
        raw_snapshot=raw_snapshot,
    )
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
    if (
        dict(issuance.measurement) != expected_measurement
        or dict(issuance.measurement_method) != dict(method)
    ):
        raise ValueError("outcome objective measurement issuance mismatch")
    return issuance


def outcome_issuance_receipt_valid(
    *,
    state_store: Any,
    outcome: CognitiveStateRevision,
    issuance_hash: str,
    command_type: str,
    consumer_id: str,
) -> bool:
    with state_store._connect(read_only=True) as conn:  # noqa: SLF001
        rows = conn.execute(
            """
            SELECT o.command_id, o.consumer_id, o.payload_json,
                   r.revision_id AS receipt_revision_id,
                   r.status AS receipt_status,
                   r.target_effect_id AS receipt_target_effect_id,
                   r.after_hash AS receipt_after_hash,
                   r.evidence_refs AS receipt_evidence_refs
            FROM cognitive_state_outbox AS o
            LEFT JOIN cognitive_state_effect_receipts AS r
              ON r.command_id=o.command_id
            WHERE o.revision_id=? AND o.command_type=?
            """,
            (outcome.revision_id, command_type),
        ).fetchall()
    if len(rows) != 1:
        return False
    row = rows[0]
    try:
        command = json.loads(str(row["payload_json"]))
        refs = set(json.loads(str(row["receipt_evidence_refs"] or "[]")))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    authority = outcome.payload["source_authority"]
    source_revision_id = str(authority["source_revision_id"])
    source_content_hash = str(authority["content_hash"])
    return bool(
        row["consumer_id"] == consumer_id
        and command.get("outcome_revision_id") == outcome.revision_id
        and command.get("outcome_revision_hash") == outcome.payload_hash
        and command.get("oracle_issuance_hash") == issuance_hash
        and command.get("oracle_source_revision_id") == source_revision_id
        and command.get("oracle_source_content_hash") == source_content_hash
        and row["receipt_revision_id"] == outcome.revision_id
        and row["receipt_status"] == "committed"
        and row["receipt_target_effect_id"]
        == command.get("projection_effect_id")
        and row["receipt_after_hash"] == outcome.payload_hash
        and f"outcome-revision:{outcome.revision_id}" in refs
        and f"objective-oracle-issuance:{issuance_hash}" in refs
        and "objective-oracle-source:"
        f"{source_revision_id}:{source_content_hash}" in refs
    )


def build_calibration_report(
    *,
    state_store: Any,
    query: Mapping[str, Any] | None,
    verify_outcome_binding: Any,
    open_ancestor: Any,
    code_hash: str,
    spec_hash: str,
    report_factory: Callable[..., CalibrationReportT],
    terminal_states: frozenset[str],
    metric_id: str,
    method: str,
    method_version: str,
) -> CalibrationReportT:
    """Rebuild a deterministic categorical calibration/coverage report."""

    filters = dict(query or {})
    requested_statistics = filters.get("statistics", ())
    if isinstance(requested_statistics, str):
        requested_statistics = (requested_statistics,)
    if not isinstance(requested_statistics, (list, tuple, set)):
        raise ValueError("calibration statistics must be a sequence")
    probability_statistics = {
        str(value).strip().lower() for value in requested_statistics
    }.intersection({"brier", "brier_score", "ece", "expected_calibration_error"})
    if probability_statistics:
        raise ValueError(
            "Brier/ECE require an explicitly probabilistic prediction method"
        )
    starts_at = str(filters.get("starts_at") or "")
    ends_at = str(filters.get("ends_at") or "")
    if starts_at:
        _timestamp(starts_at)
    if ends_at:
        _timestamp(ends_at)
    revisions = state_store.current_revisions(object_type="prediction_record")
    rows: list[CognitiveStateRevision] = []
    for revision in revisions:
        payload = revision.payload
        evaluated = str(payload["terminal"].get("evaluated_at") or "")
        if payload["revision_state"] != "terminal":
            continue
        if starts_at and evaluated < _timestamp(starts_at).isoformat():
            continue
        if ends_at and evaluated > _timestamp(ends_at).isoformat():
            continue
        rows.append(revision)
    rows.sort(key=lambda value: value.object_id)
    counts = {state: 0 for state in terminal_states}
    exclusions: dict[str, int] = {}
    matrix = {
        "not_useful": {"not_useful": 0, "useful": 0},
        "useful": {"not_useful": 0, "useful": 0},
    }
    correct = 0
    incorrect = 0
    eligible = 0
    inputs: list[dict[str, str]] = []
    for revision in rows:
        payload = revision.payload
        state = str(payload["terminal"]["state"])
        counts[state] += 1
        if payload["calibration"]["eligible"]:
            outcome = state_store.revision(payload["outcome_ref"]["revision_id"])
            try:
                if (
                    outcome is None
                    or outcome.payload_hash
                    != payload["outcome_ref"]["payload_hash"]
                ):
                    raise RuntimeError("calibration input outcome is unavailable")
                verify_outcome_binding(open_ancestor(revision), outcome)
            except (RuntimeError, TypeError, ValueError):
                reason = "objective_outcome_revalidation_failed"
                exclusions[reason] = exclusions.get(reason, 0) + 1
            else:
                predicted = str(payload["metric"]["predicted_value"])
                observed = str(outcome.payload["observed_value"])
                matrix[predicted][observed] += 1
                eligible += 1
                if predicted == observed:
                    correct += 1
                else:
                    incorrect += 1
        else:
            reason = str(payload["calibration"]["exclusion_reason"])
            exclusions[reason] = exclusions.get(reason, 0) + 1
        inputs.append(
            {
                "prediction_id": revision.object_id,
                "terminal_revision_id": revision.revision_id,
                "terminal_revision_hash": revision.payload_hash,
            }
        )
    input_hash = sha256_json(inputs)
    accuracy: float | None = correct / eligible if eligible else None
    terminal_total = sum(counts.values())
    coverage_ratios = {
        state: (counts[state] / terminal_total if terminal_total else 0.0)
        for state in ("measured", "unknown", "censored", "confounded")
    }
    core = {
        "schema_version": "mnemos.prediction_calibration_report.v1",
        "metric_id": metric_id,
        "method": method,
        "method_version": method_version,
        "code_hash": code_hash,
        "spec_hash": spec_hash,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "counts": counts,
        "calibration_eligible": eligible,
        "correct": correct,
        "incorrect": incorrect,
        "accuracy": accuracy,
        "confusion_matrix": matrix,
        "exclusions": dict(sorted(exclusions.items())),
        "coverage_ratios": coverage_ratios,
        "input_hash": input_hash,
    }
    return report_factory(
        status="ok" if eligible else "insufficient_sample",
        metric_id=metric_id,
        method=method,
        method_version=method_version,
        code_hash=code_hash,
        spec_hash=spec_hash,
        starts_at=starts_at,
        ends_at=ends_at,
        measured=counts["measured"],
        unknown=counts["unknown"],
        censored=counts["censored"],
        confounded=counts["confounded"],
        calibration_eligible=eligible,
        correct=correct,
        incorrect=incorrect,
        accuracy=accuracy,
        confusion_matrix=MappingProxyType(
            {key: MappingProxyType(dict(value)) for key, value in matrix.items()}
        ),
        exclusions=MappingProxyType(dict(sorted(exclusions.items()))),
        coverage_ratios=MappingProxyType(coverage_ratios),
        input_hash=input_hash,
        report_hash=sha256_json(core),
    )
