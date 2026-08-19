#!/usr/bin/env python3
"""Fail closed when cognitive interface declarations lack independent runtime proof.

Phase 0 intentionally leaves this audit red on the current implementation. It
freezes the 13-interface denominator outside the implementation registry and
rejects declaration-only receipts, unscoped static callsites, and evidence that
does not prove a target-store change. A later Root must supply an independent
target-store reader before this audit can certify a real effect.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ops.cognitive_data_contract import data_interface_registry_payload  # noqa: E402

SCHEMA_VERSION = "mnemos.runtime_cognitive_interface_audit.v1"
MANIFEST_SCHEMA_VERSION = "mnemos.cognitive_runtime_interface_manifest.v1"
DEFAULT_MANIFEST = ROOT / "docs" / "acceptance" / "cognitive_runtime_interface_manifest.json"

# This is verifier-owned baseline evidence, not a second runtime registry.
# Changing it requires a versioned manifest migration and an independent review.
BASELINE_REQUIRED_INTERFACE_IDS = frozenset(
    {
        "capture_service_turn",
        "capture_queue_event",
        "sync_engine_turn",
        "file_ingestor_document",
        "document_processor_document",
        "amphora_distill_task",
        "event_bus_event",
        "reflection_record",
        "adaptive_scoring_sample",
        "distill_action",
        "cognition_asset_commit",
        "cognitive_state_revision",
        "persona_profile_signal",
    }
)

EffectEvidenceReader = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]


class _ScopedCallVisitor(ast.NodeVisitor):
    """Collect calls together with the lexical symbol that owns them."""

    def __init__(self) -> None:
        self.calls: set[tuple[str, str]] = set()
        self.symbols: set[str] = set()
        self._scope: list[str] = []

    @property
    def scope(self) -> str:
        return ".".join(self._scope) if self._scope else "<module>"

    def _visit_scoped(self, node: ast.AST, name: str) -> None:
        self._scope.append(name)
        self.symbols.add(self.scope)
        self.generic_visit(node)
        self._scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scoped(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scoped(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scoped(node, node.name)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            self.calls.add((node.func.id, self.scope))
        elif isinstance(node.func, ast.Attribute):
            self.calls.add((node.func.attr, self.scope))
        self.generic_visit(node)


def _parse_path(path: Path) -> tuple[_ScopedCallVisitor | None, str | None]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return None, str(exc)
    visitor = _ScopedCallVisitor()
    visitor.visit(tree)
    return visitor, None


def _validate_anchor(
    anchor: object,
    *,
    repo_root: Path,
) -> tuple[bool, dict[str, str]]:
    if not isinstance(anchor, Mapping):
        return False, {"path": "", "call": "", "symbol": "", "error": "anchor must be an object"}
    relative_path = anchor.get("path")
    call_name = anchor.get("call")
    symbol = anchor.get("symbol")
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or not isinstance(call_name, str)
        or not call_name
        or not isinstance(symbol, str)
        or not symbol
    ):
        return False, {
            "path": str(relative_path or ""),
            "call": str(call_name or ""),
            "symbol": str(symbol or ""),
            "error": "anchor requires non-empty string path, call, and symbol",
        }
    path = repo_root / relative_path
    if not path.is_file():
        return False, {
            "path": relative_path,
            "call": call_name,
            "symbol": symbol,
            "error": "path missing",
        }
    visitor, error = _parse_path(path)
    if error:
        return False, {"path": relative_path, "call": call_name, "symbol": symbol, "error": error}
    assert visitor is not None
    if (call_name, symbol) not in visitor.calls:
        return False, {
            "path": relative_path,
            "call": call_name,
            "symbol": symbol,
            "error": "scoped callsite missing",
        }
    return True, {"path": relative_path, "call": call_name, "symbol": symbol, "error": ""}


def _validate_anchor_group(
    anchors: object,
    *,
    repo_root: Path,
) -> tuple[bool, list[dict[str, str]]]:
    if not isinstance(anchors, list) or not anchors:
        return False, [
            {
                "path": "",
                "call": "",
                "symbol": "",
                "error": "anchor group must be non-empty",
            }
        ]
    results = [_validate_anchor(anchor, repo_root=repo_root) for anchor in anchors]
    return all(ok for ok, _ in results), [result for _, result in results]


def _load_manifest(path: Path) -> tuple[list[dict[str, Any]], object, list[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], None, [f"cannot load manifest: {exc}"]
    if not isinstance(payload, Mapping):
        return [], None, ["manifest must be a JSON object"]

    errors: list[str] = []
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append(f"manifest schema_version must be {MANIFEST_SCHEMA_VERSION}")
    interfaces = payload.get("interfaces")
    if not isinstance(interfaces, list):
        return (
            [],
            payload.get("required_release_gate"),
            [*errors, "manifest interfaces must be a list"],
        )

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for interface in interfaces:
        if not isinstance(interface, dict):
            errors.append("manifest interface must be an object")
            continue
        interface_id = interface.get("interface_id")
        if not isinstance(interface_id, str) or not interface_id:
            errors.append("manifest interface_id must be a non-empty string")
            continue
        if interface_id in seen:
            errors.append(f"duplicate manifest interface id: {interface_id}")
            continue
        seen.add(interface_id)
        normalized.append(interface)

    missing = sorted(BASELINE_REQUIRED_INTERFACE_IDS - seen)
    unknown = sorted(seen - BASELINE_REQUIRED_INTERFACE_IDS)
    if missing:
        errors.append(f"missing baseline interface ids: {', '.join(missing)}")
    if unknown:
        errors.append(f"unknown baseline interface ids: {', '.join(unknown)}")
    return normalized, payload.get("required_release_gate"), errors


def _validate_required_release_gate(gate: object, *, repo_root: Path) -> list[str]:
    if not isinstance(gate, Mapping):
        return ["required_release_gate_missing"]

    failures: list[str] = []
    gate_id = gate.get("gate_id")
    runner_path = gate.get("runner_path")
    runner_symbol = gate.get("runner_symbol")
    if not isinstance(gate_id, str) or not gate_id:
        failures.append("required_release_gate_id_missing")
    if gate.get("activation_root") != "COG-042":
        failures.append("required_release_gate_activation_root_invalid")
    if gate.get("phase_0_status") != "contract_locked_deferred":
        failures.append("required_release_gate_phase_0_status_invalid")
    if not isinstance(runner_path, str) or not runner_path:
        failures.append("required_release_gate_runner_path_missing")
        return failures
    if not isinstance(runner_symbol, str) or not runner_symbol:
        failures.append("required_release_gate_runner_symbol_missing")
        return failures

    path = repo_root / runner_path
    if not path.is_file():
        failures.append("required_release_gate_runner_missing")
        return failures
    visitor, error = _parse_path(path)
    if error or visitor is None or runner_symbol not in visitor.symbols:
        failures.append("required_release_gate_runner_symbol_missing")
    return failures


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _read_effect_evidence(
    interface: Mapping[str, Any],
    *,
    reader: EffectEvidenceReader | None,
) -> tuple[dict[str, Any], list[str]]:
    if reader is None:
        return {}, ["independent_effect_evidence_missing"]
    evidence = reader(interface)
    if not isinstance(evidence, Mapping):
        return {}, ["independent_effect_evidence_missing"]

    required_counts = (
        "eligible_event_count",
        "produced_event_count",
        "consumed_event_count",
        "effect_count",
        "unknown_eligible_count",
    )
    summary = {key: evidence.get(key) for key in required_counts}
    summary.update(
        {
            "target_before_hash": evidence.get("target_before_hash"),
            "target_after_hash": evidence.get("target_after_hash"),
            "reciprocal_receipt": evidence.get("reciprocal_receipt"),
        }
    )
    failures = [
        f"effect_evidence_{key}_invalid"
        for key in required_counts
        if not _is_nonnegative_int(evidence.get(key))
    ]
    if failures:
        return summary, failures

    eligible = int(evidence["eligible_event_count"])
    produced = int(evidence["produced_event_count"])
    consumed = int(evidence["consumed_event_count"])
    effects = int(evidence["effect_count"])
    unknown = int(evidence["unknown_eligible_count"])
    if unknown:
        failures.append("eligible_denominator_unknown")
    if eligible and not produced:
        failures.append("eligible_event_without_produced_event")
    if produced and not consumed:
        failures.append("produced_event_without_consumption")
    if consumed and not effects:
        failures.append("receipt_without_target_effect")
    if effects:
        before_hash = evidence.get("target_before_hash")
        after_hash = evidence.get("target_after_hash")
        receipt = evidence.get("reciprocal_receipt")
        if not isinstance(before_hash, str) or not before_hash:
            failures.append("target_before_hash_missing")
        if not isinstance(after_hash, str) or not after_hash:
            failures.append("target_after_hash_missing")
        if isinstance(before_hash, str) and before_hash == after_hash:
            failures.append("target_effect_unchanged")
        if not isinstance(receipt, str) or not receipt:
            failures.append("target_reciprocal_receipt_missing")
    return summary, failures


def _validate_eligibility_contract(interface: Mapping[str, Any]) -> list[str]:
    eligibility = interface.get("eligible_event_denominator")
    if not isinstance(eligibility, Mapping):
        return ["eligible_event_denominator_missing"]
    failures: list[str] = []
    if not isinstance(eligibility.get("owner"), str) or not eligibility.get("owner"):
        failures.append("eligible_event_denominator_owner_missing")
    if eligibility.get("positive_event_required") is not True:
        failures.append("eligible_positive_event_contract_missing")
    if eligibility.get("unknown_allowed") is not False:
        failures.append("eligible_denominator_unknown_allowed")
    return failures


def audit_runtime_cognitive_interfaces(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    repo_root: Path = ROOT,
    registry_payload: Mapping[str, Any] | None = None,
    effect_evidence_reader: EffectEvidenceReader | None = None,
) -> dict[str, Any]:
    """Audit independent interface coverage without trusting a receipt alone."""
    interfaces, required_release_gate, manifest_errors = _load_manifest(manifest_path)
    registry = dict(registry_payload or data_interface_registry_payload())
    registry_interfaces = registry.get("interfaces", [])
    registry_ids: set[str] = set()
    for item in registry_interfaces:
        if not isinstance(item, Mapping):
            continue
        interface_id = item.get("interface_id")
        if isinstance(interface_id, str):
            registry_ids.add(interface_id)
    declared_runtime_ids = {
        interface_id
        for interface_id in registry.get("runtime_instrumented_interface_ids", [])
        if isinstance(interface_id, str)
    }
    manifest_ids = {str(interface["interface_id"]) for interface in interfaces}

    contract_failures = _validate_required_release_gate(required_release_gate, repo_root=repo_root)
    registry_missing_manifest = sorted(registry_ids - manifest_ids)
    declared_runtime_unknown = sorted(declared_runtime_ids - manifest_ids)
    contract_failures.extend(
        f"registry_interface_missing_manifest:{interface_id}"
        for interface_id in registry_missing_manifest
    )
    contract_failures.extend(
        f"declared_runtime_interface_missing_manifest:{interface_id}"
        for interface_id in declared_runtime_unknown
    )

    results: list[dict[str, Any]] = []
    for interface in interfaces:
        interface_id = str(interface["interface_id"])
        runtime_required = interface.get("runtime_required") is True
        producer_ok, producer_anchors = _validate_anchor_group(
            interface.get("producer_anchors"), repo_root=repo_root
        )
        consumer_ok, consumer_anchors = _validate_anchor_group(
            interface.get("consumer_anchors"), repo_root=repo_root
        )
        oracle = interface.get("target_effect_oracle")
        effect_evidence, effect_failures = _read_effect_evidence(
            interface,
            reader=effect_evidence_reader,
        )

        failures: list[str] = []
        if interface_id not in registry_ids:
            failures.append("registry_interface_missing")
        if interface_id in BASELINE_REQUIRED_INTERFACE_IDS and not runtime_required:
            failures.append("baseline_interface_made_optional")
        if (
            interface_id in BASELINE_REQUIRED_INTERFACE_IDS
            and interface.get("evidence_mode") != "runtime_receipt"
        ):
            failures.append("baseline_interface_not_runtime_receipt")
        if runtime_required and interface_id not in declared_runtime_ids:
            failures.append("runtime_receipt_declaration_missing")
        if not producer_ok:
            failures.append("producer_anchor_missing")
        if not consumer_ok:
            failures.append("consumer_anchor_missing")
        failures.extend(_validate_eligibility_contract(interface))
        if not isinstance(oracle, Mapping) or not oracle.get("owner") or not oracle.get("contract"):
            failures.append("target_effect_oracle_missing")
        failures.extend(effect_failures)

        results.append(
            {
                "interface_id": interface_id,
                "runtime_required": runtime_required,
                "evidence_mode": interface.get("evidence_mode"),
                "registered": interface_id in registry_ids,
                "declared_runtime_receipt": interface_id in declared_runtime_ids,
                "producer_anchors_ok": producer_ok,
                "producer_anchors": producer_anchors,
                "consumer_anchors_ok": consumer_ok,
                "consumer_anchors": consumer_anchors,
                "eligible_event_denominator": interface.get("eligible_event_denominator", {}),
                "target_effect_oracle": oracle if isinstance(oracle, Mapping) else {},
                "independent_effect_evidence_ok": not effect_failures,
                "effect_evidence": effect_evidence,
                "failures": failures,
            }
        )

    blocking_count = (
        len(manifest_errors)
        + len(contract_failures)
        + sum(bool(item["failures"]) for item in results)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_phase": "phase_0_locked_baseline",
        "manifest_path": str(manifest_path),
        "manifest_errors": manifest_errors,
        "contract_failures": contract_failures,
        "required_release_gate": (
            required_release_gate if isinstance(required_release_gate, Mapping) else {}
        ),
        "denominator": {
            "required_interface_count": len(manifest_ids),
            "declared_runtime_receipt_count": len(declared_runtime_ids & manifest_ids),
            "runtime_receipt_gap": len(manifest_ids - declared_runtime_ids),
            "registry_missing_manifest": registry_missing_manifest,
            "declared_runtime_unknown": declared_runtime_unknown,
        },
        "interfaces": results,
        "blocking_count": blocking_count,
        "ok": blocking_count == 0,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the phase-0 interface denominator audit from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--strict", action="store_true", help="accepted for release-gate compatibility"
    )
    parser.add_argument("--json", action="store_true", help="emit versioned JSON")
    args = parser.parse_args(argv)
    report = audit_runtime_cognitive_interfaces(manifest_path=args.manifest, repo_root=ROOT)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "Runtime cognitive interfaces: "
            f"ok={report['ok']} required={report['denominator']['required_interface_count']} "
            f"declared={report['denominator']['declared_runtime_receipt_count']}"
        )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
