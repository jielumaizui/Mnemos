"""Structural runtime-evidence validation for the AgentSource manifest audit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from core.ops.durable_io import read_native_bytes

RUNTIME_REPORT_SCHEMA_VERSION = "mnemos.agent_source_runtime_report.v2"
NATIVE_SOURCE_SNAPSHOT_SCHEMA_VERSION = "mnemos.native_source_snapshot.v2"
RUNTIME_REPORT_PRODUCERS = {
    "scripts.backfill_raw_event_store",
    "daemon.raw_sync",
}
CONTINUOUS_CAPTURE_CURSOR_KIND = "continuous_tail_reconcile_v1"
CONTINUOUS_CAPTURE_CURSOR_HASH_FIELDS = {
    "capture_roster_hash",
    "capture_denominator_session_set_hash",
    "capture_expected_turn_fingerprint_set_hash",
    "capture_receipt_binding_set_hash",
}
CONTINUOUS_CAPTURE_CURSOR_COUNT_FIELDS = {
    "capture_expected_turn_count",
    "capture_receipt_count",
    "capture_exact_receipt_count",
    "capture_pending_turn_count",
    "capture_orphan_receipt_count",
}


def _finding(code: str, message: str, *, path: str = "") -> dict[str, str]:
    return {
        "code": code,
        "severity": "blocking",
        "message": message,
        "path": path,
        "repair_action": "restore the canonical AgentSource support contract",
    }


def canonical_hash(value: Any) -> str:
    """Compute the manifest-compatible canonical JSON digest."""
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _valid_sha256(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


def load_runtime_evidence(
    evidence: Path | Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any] | None, list[dict[str, str]]]:
    """Load an immutable daemon/backfill report without opening production state."""
    if evidence is None:
        return None, []
    if isinstance(evidence, Mapping):
        return evidence, []
    path = Path(evidence)
    try:
        loaded = json.loads(read_native_bytes(path).decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, [
            _finding("runtime_evidence_unreadable", str(exc), path=str(path))
        ]
    if not isinstance(loaded, Mapping):
        return None, [
            _finding(
                "runtime_evidence_not_object",
                "runtime evidence must be a JSON object",
                path=str(path),
            )
        ]
    return loaded, []


def validate_runtime_evidence_envelope(
    evidence: Mapping[str, Any] | None,
    *,
    manifest_hash: str,
    specs: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Validate structural report provenance without granting attestation credit."""
    del specs
    if evidence is None:
        return []
    findings: list[dict[str, str]] = []
    if evidence.get("schema_version") != RUNTIME_REPORT_SCHEMA_VERSION:
        findings.append(
            _finding(
                "runtime_report_schema_invalid",
                "runtime report does not declare the canonical structural schema version",
                path="runtime_evidence",
            )
        )
    producer = evidence.get("producer")
    if producer not in RUNTIME_REPORT_PRODUCERS:
        findings.append(
            _finding(
                "runtime_report_producer_invalid",
                "runtime report producer is not a canonical daemon/backfill owner",
                path="runtime_evidence",
            )
        )
    if evidence.get("report_kind") != "structural_source_observation":
        findings.append(
            _finding(
                "runtime_report_kind_invalid",
                "runtime report must be labeled as a structural source observation",
                path="runtime_evidence",
            )
        )
    if evidence.get("support_manifest_hash") != manifest_hash:
        findings.append(
            _finding(
                "runtime_report_manifest_mismatch",
                "runtime report was produced for a different support manifest",
                path="runtime_evidence",
            )
        )
    unsigned = dict(evidence)
    supplied_hash = str(unsigned.pop("report_hash", "") or "")
    if not supplied_hash or supplied_hash != canonical_hash(unsigned):
        findings.append(
            _finding(
                "runtime_report_checksum_invalid",
                "runtime report checksum does not bind the supplied payload",
                path="runtime_evidence",
            )
        )
    forbidden = {
        "runtime_receipts",
        "runtime_receipt_scope",
        "evidence_hash",
        "runtime_attestation",
        "certifying",
        "release_eligible",
        "runtime_full_power_ok",
        "full_power_agents",
    }
    present_forbidden = sorted(key for key in forbidden if key in evidence)
    if present_forbidden:
        findings.append(
            _finding(
                "runtime_report_contains_forbidden_attestation",
                "structural report cannot carry runtime receipts or attestations: "
                + ", ".join(present_forbidden),
                path="runtime_evidence",
            )
        )

    unmanifested = evidence.get("unmanifested_sources")
    if not isinstance(unmanifested, list) or not all(
        isinstance(source, str) and source for source in unmanifested
    ):
        findings.append(
            _finding(
                "runtime_report_unmanifested_sources_invalid",
                "runtime report must declare unmanifested_sources as a string list",
                path="runtime_evidence",
            )
        )
    elif unmanifested:
        findings.append(
            _finding(
                "runtime_report_unmanifested_sources_present",
                "runtime report observed undeclared sources: "
                + ", ".join(sorted(unmanifested)),
                path="runtime_evidence",
            )
        )

    if producer == "daemon.raw_sync":
        errors = evidence.get("errors")
        if isinstance(errors, bool) or not isinstance(errors, int) or errors < 0:
            findings.append(
                _finding(
                    "runtime_report_error_count_invalid",
                    "daemon report errors must be a non-negative integer",
                    path="runtime_evidence",
                )
            )
        elif errors:
            findings.append(
                _finding(
                    "runtime_report_errors_present",
                    f"daemon report recorded {errors} source errors",
                    path="runtime_evidence",
                )
            )
        if not isinstance(evidence.get("source_snapshots"), Mapping):
            findings.append(
                _finding(
                    "runtime_report_snapshots_missing",
                    "daemon report must expose source_snapshots",
                    path="runtime_evidence",
                )
            )
    if producer == "scripts.backfill_raw_event_store":
        agents = evidence.get("agents")
        if not isinstance(agents, Mapping):
            findings.append(
                _finding(
                    "runtime_report_agents_missing",
                    "backfill report must expose per-source agents results",
                    path="runtime_evidence",
                )
            )
        else:
            for source_name, details in agents.items():
                if not isinstance(details, Mapping):
                    findings.append(
                        _finding(
                            "runtime_report_agent_result_invalid",
                            f"{source_name}: backfill result is not an object",
                            path="runtime_evidence",
                        )
                    )
                    continue
                failed = details.get("failed")
                if (
                    isinstance(failed, bool)
                    or not isinstance(failed, int)
                    or failed < 0
                ):
                    findings.append(
                        _finding(
                            "runtime_report_agent_failure_count_invalid",
                            f"{source_name}: failed must be a non-negative integer",
                            path="runtime_evidence",
                        )
                    )
                elif failed:
                    findings.append(
                        _finding(
                            "runtime_report_agent_failures_present",
                            f"{source_name}: backfill recorded {failed} failed sessions",
                            path="runtime_evidence",
                        )
                    )
    return findings


def report_is_structural_only(evidence: Mapping[str, Any] | None) -> bool:
    """External daemon/backfill JSON may validate shape, never host full-power."""
    return evidence is not None


def _runtime_snapshot_records(
    evidence: Mapping[str, Any],
) -> list[tuple[str, Any]]:
    records: list[tuple[str, Any]] = []
    source_snapshots = evidence.get("source_snapshots")
    if isinstance(source_snapshots, Mapping):
        records.extend(
            (str(name), snapshot)
            for name, snapshot in source_snapshots.items()
        )
    agents = evidence.get("agents")
    if isinstance(agents, Mapping):
        for name, details in agents.items():
            if (
                isinstance(details, Mapping)
                and "native_source_snapshot" in details
            ):
                records.append(
                    (str(name), details.get("native_source_snapshot"))
                )
    snapshots = evidence.get("snapshots")
    if isinstance(snapshots, list):
        records.extend(("", snapshot) for snapshot in snapshots)
    return records


def audit_runtime_evidence(
    evidence: Mapping[str, Any] | None,
    specs: Mapping[str, Mapping[str, Any]],
    manifest_hash: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Validate manifest-bound snapshots without granting runtime capability."""
    result: dict[str, Any] = {
        "runtime_evidence_collected": evidence is not None,
        "runtime_evidence_state": (
            "structural_observation" if evidence is not None else "not_collected"
        ),
        "runtime_evidence_certifying": False,
        "runtime_evidence_trust": (
            "untrusted_external_report" if evidence is not None else "not_collected"
        ),
        "snapshot_record_count": 0,
        "runtime_receipt_count": None,
        "runtime_full_power_ok": None,
        "support_snapshot_manifest_mismatch": None,
        "receipt_without_support_manifest_hash": None,
        "receipt_support_manifest_mismatch": None,
    }
    if evidence is None:
        return result, []

    findings: list[dict[str, str]] = []
    snapshot_errors = 0
    for expected_name, snapshot in _runtime_snapshot_records(evidence):
        result["snapshot_record_count"] += 1
        errors: list[str] = []
        if not isinstance(snapshot, Mapping):
            errors.append("snapshot_not_object")
            source_name = expected_name
        else:
            source_name = str(snapshot.get("source_name") or expected_name)
            if expected_name and source_name != expected_name:
                errors.append("snapshot_report_key_mismatch")
            if (
                snapshot.get("schema_version")
                != NATIVE_SOURCE_SNAPSHOT_SCHEMA_VERSION
            ):
                errors.append("snapshot_schema_version_mismatch")
            spec = specs.get(source_name)
            if spec is None:
                errors.append("snapshot_source_not_in_manifest")
            else:
                parser = spec.get("parser")
                capability = spec.get("capability")
                parser_map = parser if isinstance(parser, Mapping) else {}
                capability_map = (
                    capability if isinstance(capability, Mapping) else {}
                )
                if snapshot.get("support_manifest_hash") != manifest_hash:
                    errors.append("support_manifest_hash_mismatch")
                if snapshot.get("source_role") != spec.get("role"):
                    errors.append("snapshot_role_mismatch")
                if snapshot.get("parser_module") != parser_map.get("module"):
                    errors.append("snapshot_parser_mismatch")
                if snapshot.get("parser_class") != parser_map.get("class"):
                    errors.append("snapshot_parser_mismatch")
                if snapshot.get("capability_contract_hash") != canonical_hash(
                    capability_map
                ):
                    errors.append("snapshot_capability_mismatch")
                if spec.get("role") == "retired":
                    errors.append("retired_source_snapshot")
            roots = snapshot.get("resolved_roots")
            if not isinstance(roots, list) or not all(
                isinstance(root, str) for root in roots
            ):
                errors.append("snapshot_roots_malformed")
            cursor = snapshot.get("cursor")
            if not isinstance(cursor, Mapping) or not str(
                cursor.get("kind") or ""
            ):
                errors.append("snapshot_cursor_malformed")
            denominator = snapshot.get("native_denominator")
            if not isinstance(denominator, Mapping):
                errors.append("snapshot_native_denominator_malformed")
            else:
                for key in ("sessions", "turns"):
                    value = denominator.get(key)
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 0
                    ):
                        errors.append(
                            "snapshot_native_denominator_malformed"
                        )
                        break
            if (
                isinstance(cursor, Mapping)
                and cursor.get("kind") == CONTINUOUS_CAPTURE_CURSOR_KIND
            ):
                required_capture_fields = (
                    CONTINUOUS_CAPTURE_CURSOR_HASH_FIELDS
                    | CONTINUOUS_CAPTURE_CURSOR_COUNT_FIELDS
                    | {
                        "capture_generation_id",
                        "capture_generation_eligible",
                        "denominator_complete",
                        "denominator_observed_sessions",
                        "discovered_sessions",
                        "denominator_turns",
                    }
                )
                if not required_capture_fields.issubset(cursor):
                    errors.append("snapshot_capture_cursor_incomplete")
                else:
                    count_fields = (
                        CONTINUOUS_CAPTURE_CURSOR_COUNT_FIELDS
                        | {
                            "denominator_observed_sessions",
                            "discovered_sessions",
                            "denominator_turns",
                        }
                    )
                    counts_valid = all(
                        isinstance(cursor.get(key), int)
                        and not isinstance(cursor.get(key), bool)
                        and int(cursor[key]) >= 0
                        for key in count_fields
                    )
                    malformed = (
                        not str(cursor.get("capture_generation_id") or "")
                        or not isinstance(
                            cursor.get("capture_generation_eligible"),
                            bool,
                        )
                        or not isinstance(
                            cursor.get("denominator_complete"),
                            bool,
                        )
                        or not counts_valid
                        or any(
                            not _valid_sha256(cursor.get(key))
                            for key in CONTINUOUS_CAPTURE_CURSOR_HASH_FIELDS
                        )
                    )
                    if malformed:
                        errors.append("snapshot_capture_cursor_malformed")
                    elif (
                        int(cursor["capture_exact_receipt_count"])
                        + int(cursor["capture_pending_turn_count"])
                        != int(cursor["capture_expected_turn_count"])
                        or int(cursor["capture_exact_receipt_count"])
                        > int(cursor["capture_receipt_count"])
                        or int(cursor["capture_orphan_receipt_count"])
                        > int(cursor["capture_receipt_count"])
                        or int(cursor["capture_expected_turn_count"])
                        != int(cursor["denominator_turns"])
                        or not isinstance(denominator, Mapping)
                        or int(denominator.get("sessions", -1))
                        != int(cursor["discovered_sessions"])
                        or (
                            cursor["denominator_complete"] is True
                            and int(denominator.get("turns", -1))
                            != int(cursor["denominator_turns"])
                        )
                        or (
                            cursor["denominator_complete"] is False
                            and int(denominator.get("turns", -1)) != 0
                        )
                    ):
                        errors.append("snapshot_capture_cursor_inconsistent")
        if errors:
            snapshot_errors += 1
            findings.append(
                _finding(
                    "support_snapshot_manifest_mismatch",
                    f"{source_name or expected_name or 'unknown'}: "
                    + ", ".join(sorted(set(errors))),
                    path="runtime_evidence",
                )
            )

    result["support_snapshot_manifest_mismatch"] = snapshot_errors
    return result, findings
