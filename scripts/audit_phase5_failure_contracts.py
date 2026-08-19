#!/usr/bin/env python3
"""Audit Phase 5 negative contracts and their frozen baseline evidence."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "mnemos.phase5_failure_contract_audit.v1"
BASELINE_EVIDENCE_SCHEMA_VERSION = "mnemos.phase5_baseline_failure_evidence.v1"
DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE = DEFAULT_ROOT / "docs/acceptance/phase5_baseline_failure_evidence.json"


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_hash(payload: object) -> str:
    return _sha256_bytes(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _read(root: Path, relative_path: str) -> str:
    path = root / relative_path
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _contains_all(root: Path, relative_path: str, markers: tuple[str, ...]) -> bool:
    source = _read(root, relative_path)
    return bool(source) and all(marker in source for marker in markers)


def _called_method_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Resolve direct, getattr, and one-hop aliased method calls."""

    aliases: dict[str, tuple[str, int]] = {}
    calls: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        resolved: tuple[str, int] | None = None
        if isinstance(value, ast.Attribute):
            resolved = (value.attr, value.lineno)
        elif (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "getattr"
            and len(value.args) >= 2
            and isinstance(value.args[1], ast.Constant)
            and isinstance(value.args[1].value, str)
        ):
            resolved = (value.args[1].value, value.lineno)
        if resolved is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                aliases[target.id] = resolved

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            calls.append((node.func.attr, node.lineno))
        elif isinstance(node.func, ast.Name) and node.func.id in aliases:
            calls.append((aliases[node.func.id][0], node.lineno))
        elif (
            isinstance(node.func, ast.Call)
            and isinstance(node.func.func, ast.Name)
            and node.func.func.id == "getattr"
            and len(node.func.args) >= 2
            and isinstance(node.func.args[1], ast.Constant)
            and isinstance(node.func.args[1].value, str)
        ):
            calls.append((node.func.args[1].value, node.lineno))
    return calls


def _retired_runtime_residuals(root: Path) -> list[str]:
    forbidden_profile_methods = {
        "record_profile_signal",
        "upsert_profile_assertion",
    }
    residuals: list[str] = []
    production_files: list[Path] = []
    for directory in ("core", "integrations", "daemon"):
        base = root / directory
        if base.is_dir():
            production_files.extend(sorted(base.rglob("*.py")))
    daemon_entry = root / "mnemos_daemon.py"
    if daemon_entry.is_file():
        production_files.append(daemon_entry)

    for path in production_files:
        relative = str(path.relative_to(root))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            residuals.append(f"{relative}:parse_error:{type(exc).__name__}")
            continue
        for method, line in _called_method_names(tree):
            if method in forbidden_profile_methods:
                residuals.append(f"{relative}:{line}:call:{method}")

    apollon_path = root / "integrations/apollon.py"
    if apollon_path.is_file():
        try:
            tree = ast.parse(apollon_path.read_text(encoding="utf-8"), filename=str(apollon_path))
        except (OSError, SyntaxError) as exc:
            residuals.append(f"integrations/apollon.py:parse_error:{type(exc).__name__}")
        else:
            banned_cycle_symbols = {
                "BlindSpotProfileManager",
                "PersonaStore",
                "PreferenceAnalyzer",
                "get_signal_store",
                "save_persona",
                "save_persona_to_wiki",
            }
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name != "_run_persona_cycle":
                    continue
                for child in ast.walk(node):
                    if isinstance(child, ast.Name) and child.id in banned_cycle_symbols:
                        residuals.append(
                            f"integrations/apollon.py:{child.lineno}:persona_cycle:{child.id}"
                        )
                    elif (
                        isinstance(child, ast.Constant)
                        and isinstance(child.value, str)
                        and child.value in banned_cycle_symbols
                    ):
                        residuals.append(
                            f"integrations/apollon.py:{child.lineno}:dynamic:{child.value}"
                        )

    config_runtime_paths = (
        "core/config.py",
        "mnemos_daemon.py",
    )
    config_runtime_paths += tuple(
        str(path.relative_to(root))
        for path in sorted((root / "daemon").rglob("*.py"))
        if (root / "daemon").is_dir()
    )
    for relative in config_runtime_paths:
        source = _read(root, relative)
        if "daemon.services.persona_extensions" in source:
            residuals.append(f"{relative}:legacy_config:daemon.services.persona_extensions")
    return sorted(set(residuals))


def _contract_checks(root: Path) -> dict[str, bool]:
    return {
        "cooldown_100_instances_and_multiprocess": _contains_all(
            root,
            "tests/unit/test_blindspot_discovery.py",
            (
                "test_cooldown_survives_one_hundred_fresh_service_instances",
                "test_cooldown_survives_multiprocess_service_restarts",
                "[(False, 0)] * 8",
            ),
        ),
        "duplicate_persona_revision_rejected": _contains_all(
            root,
            "tests/unit/test_delphi.py",
            (
                "test_persona_replay_rejects_conflicting_reused_version",
                "pytest.raises(ValueError",
                "already belongs to a different Persona",
            ),
        ),
        "apollon_direct_write_negative_contract": _contains_all(
            root,
            "tests/unit/test_apollon.py",
            (
                "test_apollon_persona_cycle_is_observation_only",
                "must not construct a Persona writer",
                "deferred to daemon canonical revision command",
            ),
        ),
        "real_challenge_positive_contract": _contains_all(
            root,
            "tests/unit/test_persona_challenge_queue_p5.py",
            (
                "test_real_canonical_challenge_command_is_presented_and_committed",
                '"awaiting_presentation"',
                "record_presentation(",
                'effect["status"] == "committed"',
            ),
        ),
        "producer_assertion_consumer_target_e2e": _contains_all(
            root,
            "tests/integration/test_profile_signal_assertion_usage_loop.py",
            (
                "record_explicit_profile_evidence(",
                "build_persona_section(",
                "FROM profile_usage_log",
                'target_delta["target_id"] == "preflight_persona_section"',
            ),
        ),
        "enabled_disabled_counterfactual": _contains_all(
            root,
            "tests/integration/test_profile_signal_assertion_usage_loop.py",
            (
                "persona_enabled = False",
                "persona_enabled = True",
                "disabled_usage_count == 0",
                "usage[1] != usage[2]",
            ),
        ),
        "migration_crash_restore_contracts": all(
            (
                _contains_all(
                    root,
                    "tests/unit/test_reconcile_profile_assertion_revisions.py",
                    ("failpoint", "restore", "rollback"),
                ),
                _contains_all(
                    root,
                    "tests/unit/test_reconcile_user_model_asset_stores.py",
                    (
                        "test_prepared_generation_recovers_after_process_death_window",
                        "test_second_store_failure_restores_both_pre_states",
                    ),
                ),
                _contains_all(
                    root,
                    "tests/unit/test_reconcile_wiki_knowledge_forms.py",
                    (
                        "test_apply_failure_restores_all_wiki_preimages",
                        "test_restart_recovery_restores_source_materialized_generation",
                    ),
                ),
            )
        ),
        "full_score_required_gate_mutation": _contains_all(
            root,
            "tests/unit/test_verify_full_score_certificate.py",
            (
                "test_verifier_rejects_release_report_when_runner_omits_phase_five_gate",
                "test_verifier_rejects_phase_five_gate_contract_mutation",
                "required_phase5_gate_missing:",
                "required_phase5_gate_contract_mismatch:",
            ),
        ),
        "production_zero_denominator_blocks": _contains_all(
            root,
            "tests/unit/test_audit_persona_runtime_effectiveness.py",
            (
                "production_signal_denominator_zero",
                "production_active_assertion_denominator_zero",
                "production_usage_denominator_zero",
            ),
        ),
        "old_alias_config_dynamic_scan": _contains_all(
            root,
            "tests/unit/test_audit_phase5_failure_contracts.py",
            (
                "test_dynamic_legacy_entrypoint_and_old_config_are_detected",
                'getattr(store, "record_profile_signal")',
                "daemon.services.persona_extensions",
            ),
        ),
    }


def _false_green_contracts(root: Path) -> dict[str, bool]:
    manifest_source = _read(root, "docs/acceptance/phase5_required_full_score_gates.json")
    runtime_source = _read(root, "scripts/audit_persona_runtime_effectiveness.py")
    seeded_source = _read(root, "scripts/audit_persona_profile_contract.py")
    blindspot_source = _read(root, "scripts/audit_blindspot_asset_boundaries.py")
    return {
        "persona_runtime_gate_required": (
            '"contracts.persona_runtime_effectiveness"' in manifest_source
        ),
        "blindspot_runtime_gate_required": (
            '"contracts.blindspot_asset_boundaries"' in manifest_source
        ),
        "phase5_failure_contract_gate_required": (
            '"contracts.phase5_failure_contracts"' in manifest_source
        ),
        "zero_denominators_are_errors": all(
            marker in runtime_source
            for marker in (
                "production_signal_denominator_zero",
                "production_active_assertion_denominator_zero",
                "production_usage_denominator_zero",
            )
        ),
        "seeded_audit_non_certifying": (
            '"certifying": False' in seeded_source and '"seeded_by_audit": True' in seeded_source
        ),
        "wiki_zero_coverage_blocks": (
            'observation_status = "OBSERVED" if denominator else "UNOBSERVED"' in blindspot_source
            and 'failures.append("production_knowledge_form_coverage")' in blindspot_source
        ),
    }


def _baseline_evidence_status(
    evidence_path: Path,
    *,
    audit_sha256: str,
) -> tuple[bool, list[str], dict[str, Any]]:
    errors: list[str] = []
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, [f"baseline_evidence_unreadable:{type(exc).__name__}"], {}
    if payload.get("schema_version") != BASELINE_EVIDENCE_SCHEMA_VERSION:
        errors.append("baseline_evidence_schema_mismatch")
    if payload.get("frozen_audit_sha256") != audit_sha256:
        errors.append("baseline_evidence_audit_hash_mismatch")
    commit = str(payload.get("baseline_commit") or "")
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        errors.append("baseline_evidence_commit_invalid")
    failed = payload.get("baseline_failed_contracts")
    if not isinstance(failed, list) or not failed:
        errors.append("baseline_evidence_has_no_failures")
    if int(payload.get("baseline_wrong_legacy_behavior_expected_by_runtime_test") or 0) <= 0:
        errors.append("baseline_evidence_wrong_behavior_count_zero")
    report_hash = str(payload.get("baseline_contract_snapshot_hash") or "")
    if not (
        report_hash.startswith("sha256:")
        and len(report_hash) == 71
        and all(char in "0123456789abcdef" for char in report_hash[7:])
    ):
        errors.append("baseline_evidence_report_hash_invalid")
    command = payload.get("reproduction_command")
    if not isinstance(command, list) or not command:
        errors.append("baseline_evidence_reproduction_command_missing")
    return not errors, errors, payload


def audit_phase5_failure_contracts(
    root: Path,
    *,
    evidence_path: Path,
    skip_baseline_evidence: bool = False,
) -> dict[str, Any]:
    root = root.expanduser().resolve(strict=True)
    audit_sha256 = _sha256_bytes(Path(__file__).read_bytes())
    checks = _contract_checks(root)
    residuals = _retired_runtime_residuals(root)
    false_green_checks = _false_green_contracts(root)
    failed_contracts = sorted(name for name, passed in checks.items() if not passed)
    wrong_behavior_count = len(failed_contracts)
    static_green_production_red = sum(not passed for passed in false_green_checks.values())
    baseline_present = False
    baseline_errors: list[str] = []
    baseline_payload: dict[str, Any] = {}
    if not skip_baseline_evidence:
        baseline_present, baseline_errors, baseline_payload = _baseline_evidence_status(
            evidence_path,
            audit_sha256=audit_sha256,
        )
    snapshot = {
        "checks": checks,
        "legacy_runtime_residuals": residuals,
        "false_green_checks": false_green_checks,
        "wrong_legacy_behavior_expected_by_runtime_test": wrong_behavior_count,
        "old_production_caller_residual": len(residuals),
        "static_green_production_red": static_green_production_red,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "audit_scope": "phase5_negative_contract_static_and_baseline_evidence",
        "root": str(root),
        "frozen_audit_sha256": audit_sha256,
        "contract_snapshot_hash": _canonical_hash(snapshot),
        "baseline_failure_evidence_present": int(baseline_present),
        "wrong_legacy_behavior_expected_by_runtime_test": wrong_behavior_count,
        "old_production_caller_residual": len(residuals),
        "static_green_production_red": static_green_production_red,
        "checks": checks,
        "failed_contracts": failed_contracts,
        "legacy_runtime_residuals": residuals,
        "false_green_checks": false_green_checks,
        "baseline_evidence_path": str(evidence_path),
        "baseline_evidence_errors": baseline_errors,
        "baseline_commit": str(baseline_payload.get("baseline_commit") or ""),
        "ok": bool(
            baseline_present
            and not failed_contracts
            and not residuals
            and not static_green_production_red
        ),
    }


def _git_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--baseline-evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--skip-baseline-evidence", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = audit_phase5_failure_contracts(
            args.root,
            evidence_path=args.baseline_evidence,
            skip_baseline_evidence=args.skip_baseline_evidence,
        )
        report["root_commit"] = _git_commit(args.root)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": str(exc),
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") or not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
