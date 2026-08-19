#!/usr/bin/env python3
"""Validate that adaptive data written by Mnemos has real consumers."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = ROOT / "docs" / "acceptance" / "adaptive_data_flows.json"
REQUIRED_FIELDS = [
    "id",
    "data_type",
    "producer",
    "consumer",
    "adaptive_effect",
    "observable_state",
    "rollback_or_degradation",
    "validation_commands",
]
REQUIRED_REFS = {
    "producer": ["code"],
    "consumer": ["code"],
    "observable_state": ["code", "docs"],
}
REQUIRED_RUNTIME_AUDIT = {
    "schema_version": "mnemos.runtime_producer_consumer.v2",
    "data_event_schema": "mnemos.cognitive_data_event.v1",
    "data_interface_registry_schema": "mnemos.data_interface_registry.v1",
    "ledger": "producer_consumer_ledger.db",
    "default_topic": "flow_id",
    "strict_gate": "python3 scripts/audit_runtime_producer_consumer_closure.py --strict",
    "data_interface_strict_gate": "python3 scripts/audit_data_interface_registry.py --strict",
    "health_check": "checks.runtime_producer_consumer",
}
REQUIRED_SCORECARD_METRICS = {
    "producer_consumer.closed_flows",
    "producer_consumer.orphan_outputs",
    "producer_consumer.no_source_consumers",
    "producer_consumer.item_mismatches",
    "producer_consumer.dead_letters",
    "cognitive_data.events",
    "cognitive_data.consumed_events",
    "duplicates.reconciled",
}


def _path_exists(ref: str) -> bool:
    path = ref.split(":", 1)[0]
    if path.startswith(("cli:", "mcp:", "state:", "metric:", "manual:", "none")):
        return True
    return (ROOT / path).exists()


def _validate_refs(flow_id: str, field: str, refs: list[str]) -> list[str]:
    errors: list[str] = []
    if not refs:
        errors.append(f"{flow_id}: {field} has no refs")
    for ref in refs:
        if not _path_exists(ref):
            errors.append(f"{flow_id}: missing path in {field}: {ref}")
    return errors


def _validate_flow(flow: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    flow_id = str(flow.get("id", "<missing-id>"))
    for field in REQUIRED_FIELDS:
        if field not in flow:
            errors.append(f"{flow_id}: missing field {field}")
            continue
        if flow[field] in ("", None, [], {}):
            errors.append(f"{flow_id}: empty field {field}")

    for section, ref_fields in REQUIRED_REFS.items():
        value = flow.get(section, {})
        if not isinstance(value, dict):
            errors.append(f"{flow_id}: {section} must be object")
            continue
        for ref_field in ref_fields:
            refs = value.get(ref_field, [])
            if not isinstance(refs, list):
                errors.append(f"{flow_id}: {section}.{ref_field} must be list")
                continue
            errors.extend(_validate_refs(flow_id, f"{section}.{ref_field}", refs))

    tests = flow.get("validation_commands", [])
    if not all(str(cmd).startswith(("python3 -m pytest", "python3 scripts/")) for cmd in tests):
        errors.append(f"{flow_id}: validation_commands must be pytest/script commands")
    return errors


def _validate_runtime_audit(matrix: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    runtime = matrix.get("runtime_audit")
    if not isinstance(runtime, dict):
        return ["matrix runtime_audit must be object"]
    for field, expected in REQUIRED_RUNTIME_AUDIT.items():
        if runtime.get(field) != expected:
            errors.append(f"runtime_audit.{field} must be {expected}")
    for field in (
        "default_pending_budget",
        "default_dead_letter_budget",
        "default_max_lag_seconds",
    ):
        value = runtime.get(field)
        if not isinstance(value, int) or value < 0:
            errors.append(f"runtime_audit.{field} must be non-negative integer")
    metrics = runtime.get("scorecard_metrics", [])
    if not isinstance(metrics, list):
        errors.append("runtime_audit.scorecard_metrics must be list")
    else:
        missing = REQUIRED_SCORECARD_METRICS - {str(item) for item in metrics}
        if missing:
            errors.append(f"runtime_audit.scorecard_metrics missing {', '.join(sorted(missing))}")
    flow_contracts = runtime.get("flow_contracts")
    if not isinstance(flow_contracts, dict):
        errors.append("runtime_audit.flow_contracts must be object")
    return errors


def validate(matrix_path: Path = DEFAULT_MATRIX) -> list[str]:
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if matrix.get("schema_version") != "mnemos.adaptive_data_flows.v1":
        errors.append("matrix schema_version must be mnemos.adaptive_data_flows.v1")
    if not matrix.get("updated"):
        errors.append("matrix updated must be set")
    errors.extend(_validate_runtime_audit(matrix))

    flows = matrix.get("flows", [])
    if not isinstance(flows, list):
        return errors + ["matrix flows must be a list"]
    if not flows:
        errors.append("matrix has no flows")

    seen_ids: set[str] = set()
    runtime = matrix.get("runtime_audit", {})
    flow_contracts = runtime.get("flow_contracts", {}) if isinstance(runtime, dict) else {}
    instrumentation_source = (
        "\n".join(
            path.read_text(encoding="utf-8")
            for root in (ROOT / "core", ROOT / "daemon", ROOT / "integrations", ROOT / "scripts")
            for path in root.rglob("*.py")
        )
        + "\n"
        + (ROOT / "mnemos_daemon.py").read_text(encoding="utf-8")
    )
    for flow in flows:
        flow_id = str(flow.get("id", ""))
        if flow_id in seen_ids:
            errors.append(f"duplicate flow id {flow_id}")
        seen_ids.add(flow_id)
        errors.extend(_validate_flow(flow))
        contract = flow_contracts.get(flow_id) if isinstance(flow_contracts, dict) else None
        if not isinstance(contract, dict):
            errors.append(f"{flow_id}: missing runtime flow contract")
            continue
        mode = contract.get("observation_mode")
        if mode not in {"continuous", "on_event", "not_applicable"}:
            errors.append(f"{flow_id}: invalid observation_mode {mode}")
        if mode == "continuous" and contract.get("required") is not True:
            errors.append(f"{flow_id}: continuous flow must be required")
        if mode == "not_applicable" and not contract.get("not_applicable_reason"):
            errors.append(f"{flow_id}: not_applicable flow requires a reason")
        if mode != "not_applicable":
            produced = re.search(
                rf"record_runtime_produced\s*\(\s*['\"]{re.escape(flow_id)}['\"]",
                instrumentation_source,
            )
            terminal = re.search(
                rf"record_runtime_(?:consumed|dead_letter)\s*\(\s*['\"]{re.escape(flow_id)}['\"]",
                instrumentation_source,
            )
            if produced is None:
                errors.append(f"{flow_id}: missing real producer instrumentation")
            if terminal is None:
                errors.append(f"{flow_id}: missing terminal consumer instrumentation")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "matrix",
        nargs="?",
        default=str(DEFAULT_MATRIX),
        help="Path to adaptive_data_flows.json",
    )
    args = parser.parse_args(argv)
    errors = validate(Path(args.matrix))
    if errors:
        print("Adaptive data flow audit failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Adaptive data flow audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
