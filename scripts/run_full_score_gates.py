#!/usr/bin/env python3
"""Run the Mnemos full-score strict gate bundle.

The default output directory is under /tmp so this audit does not dirty the
repository. Use --strict --real-api for the full release-style check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ops.hermetic_run import HermeticRunEnvironment  # noqa: E402

SCHEMA_VERSION = "mnemos.full_score_gates.v2"
MANIFEST_SCHEMA_VERSION = "mnemos.full_score_gate_manifest.v1"
CERTIFICATE_SCHEMA_VERSION = "mnemos.full_score_certificate.v1"
PHASE5_REQUIRED_GATE_MANIFEST_SCHEMA_VERSION = "mnemos.phase5_required_full_score_gates.v1"
PHASE5_REQUIRED_GATE_MANIFEST_PATH = (
    ROOT / "docs" / "acceptance" / "phase5_required_full_score_gates.json"
)


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class Gate:
    gate_id: str
    category: str
    command: tuple[str, ...]
    repair_hint: str
    required: bool = True
    strict_only: bool = False
    slow: bool = False
    real_api: bool = False
    timeout_seconds: int = 1800
    notes: str = ""


@dataclass
class GateResult:
    gate_id: str
    category: str
    command: list[str]
    required: bool
    status: str
    returncode: int | None
    duration_seconds: float
    stdout_path: str
    stderr_path: str
    repair_hint: str
    notes: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    stdout_sha256: str = ""
    stderr_sha256: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    @property
    def failed(self) -> bool:
        return self.status == "failed"


@dataclass(frozen=True)
class GateManifest:
    schema_version: str
    manifest_id: str
    manifest_hash: str
    expected_gate_ids: tuple[str, ...]
    gate_contracts: tuple[dict[str, Any], ...]


def _python_cmd() -> str:
    for candidate in (
        ROOT / ".venv" / "bin" / "python",
        ROOT / ".venv" / "Scripts" / "python.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _now_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _git_state() -> dict[str, Any]:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    commit = (proc.stdout or "unknown").strip() or "unknown"
    status_proc = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    status = status_proc.stdout if status_proc.returncode == 0 else "git_status_failed"
    return {
        "commit": commit,
        "clean": (
            proc.returncode == 0
            and commit != "unknown"
            and status_proc.returncode == 0
            and not status
        ),
        "status_hash": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def _replace_python(command: Sequence[str], python_cmd: str) -> list[str]:
    return [python_cmd if item == "python" else item for item in command]


def _split_csv(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _gate_contract(gate: Gate) -> dict[str, Any]:
    return {
        "gate_id": gate.gate_id,
        "category": gate.category,
        "command": list(gate.command),
        "required": gate.required,
        "strict_only": gate.strict_only,
        "slow": gate.slow,
        "real_api": gate.real_api,
        "timeout_seconds": gate.timeout_seconds,
    }


def _load_required_phase5_gate_contracts() -> tuple[list[dict[str, Any]], list[str]]:
    """Load the Phase 5 release denominator independently from the runner plan."""

    try:
        payload = json.loads(PHASE5_REQUIRED_GATE_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"required_phase5_manifest_unreadable:{exc}"]
    if not isinstance(payload, Mapping):
        return [], ["required_phase5_manifest_not_object"]
    if payload.get("schema_version") != PHASE5_REQUIRED_GATE_MANIFEST_SCHEMA_VERSION:
        return [], ["required_phase5_manifest_schema_invalid"]
    contracts = payload.get("required_gate_contracts")
    if not isinstance(contracts, list) or not contracts:
        return [], ["required_phase5_manifest_contracts_invalid"]
    required_keys = {
        "gate_id",
        "category",
        "command",
        "required",
        "strict_only",
        "slow",
        "real_api",
        "timeout_seconds",
    }
    normalized: list[dict[str, Any]] = []
    gate_ids: set[str] = set()
    for contract in contracts:
        if not isinstance(contract, Mapping) or set(contract) != required_keys:
            return [], ["required_phase5_manifest_contract_shape_invalid"]
        gate_id = contract.get("gate_id")
        command = contract.get("command")
        if (
            not isinstance(gate_id, str)
            or not gate_id
            or gate_id in gate_ids
            or not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
            or contract.get("required") is not True
            or contract.get("strict_only") is not True
            or not isinstance(contract.get("timeout_seconds"), int)
        ):
            return [], ["required_phase5_manifest_contract_value_invalid"]
        gate_ids.add(gate_id)
        normalized.append(dict(contract))
    return normalized, []


def _list_field(payload: Mapping[str, Any], key: str) -> list[Any]:
    value = payload.get(key, [])
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)


def _write_text(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _base_gate_plan(*, real_api: bool) -> list[Gate]:
    e2e_command = (
        ("python", "scripts/e2e_wow_probe.py", "--real-api")
        if real_api
        else ("python", "scripts/e2e_wow_probe.py", "--mock-llm")
    )
    e2e_id = "e2e.wow_real_api" if real_api else "e2e.wow_mock_llm"

    return [
        Gate(
            "tests.quick",
            "engineering",
            ("python", "scripts/run_tests.py", "quick"),
            "Fix failing quick/unit regressions before scoring.",
            strict_only=True,
            slow=True,
            timeout_seconds=2400,
        ),
        Gate(
            "tests.integration",
            "engineering",
            ("python", "scripts/run_tests.py", "integration"),
            "Fix cross-module or acceptance regressions before scoring.",
            strict_only=True,
            slow=True,
            timeout_seconds=1800,
        ),
        Gate(
            "tests.heavy",
            "engineering",
            ("python", "scripts/run_tests.py", "heavy"),
            "Fix packaging, benchmark, or E2E test regressions before release scoring.",
            strict_only=True,
            slow=True,
            timeout_seconds=1800,
        ),
        Gate(
            "local_gates",
            "engineering",
            ("python", "scripts/run_local_gates.py"),
            "Run the failing local gate directly and fix the named sub-gate.",
            timeout_seconds=2400,
        ),
        Gate(
            "docs.asset_manifest.strict",
            "engineering",
            (
                "python",
                "scripts/audit_document_asset_manifest.py",
                "--strict",
                "--desktop-mode",
                "required",
                "--json",
            ),
            "Classify and verify every tracked repo doc, prompt/schema, and Desktop system-map asset.",
            strict_only=True,
            timeout_seconds=120,
        ),
        Gate(
            "quality.maintainability_zero_closure",
            "engineering",
            (
                "python",
                "scripts/check_maintainability_budget.py",
                "--closure",
                "--strict",
                "--json",
            ),
            "Remove residual large-file/broad-catch debt; accepted debt is visible but cannot certify full score.",
            strict_only=True,
            timeout_seconds=120,
        ),
        Gate(
            "quality.zombie_zero_closure",
            "engineering",
            (
                "python",
                "scripts/check_zombie_code_policy.py",
                "--closure",
                "--strict",
                "--json",
            ),
            "Remove residual compatibility candidates; time-bounded acceptances do not certify full score.",
            strict_only=True,
            timeout_seconds=120,
        ),
        Gate(
            "quality.vulture_zero_closure",
            "engineering",
            (
                "python",
                "scripts/ci_ratchet.py",
                "--closure",
                "--strict",
                "--json",
            ),
            "Keep the exact vulture whitelist and its committed baseline at zero.",
            strict_only=True,
            timeout_seconds=120,
        ),
        Gate(
            "security.strict",
            "security",
            ("python", "scripts/security_audit.py", "--strict"),
            "Resolve Bandit high/medium, dependency, or health security failures.",
            timeout_seconds=300,
        ),
        Gate(
            "security.release_privacy",
            "security",
            ("python", "scripts/audit_release_privacy_security.py", "--strict"),
            "Resolve release privacy/security findings across diagnostics, docs, repo text and runtime encryption.",
            timeout_seconds=600,
        ),
        Gate(
            "config.examples.strict",
            "engineering",
            ("python", "scripts/verify_config_examples.py", "--strict"),
            "Regenerate config examples and restore 100% public coverage.",
            timeout_seconds=120,
        ),
        Gate(
            "config.registry.closure",
            "engineering",
            ("python", "scripts/audit_config_registry_closure.py", "--strict"),
            "Reconcile canonical keys, runtime readers, examples, migrations, and live config.",
            timeout_seconds=120,
        ),
        Gate(
            "schema.relation_evidence.strict",
            "engineering",
            ("python", "scripts/audit_schema_registry.py", "--strict", "--json"),
            "Run the explicit relation_evidence migration, then restore the registered canonical schema hash.",
            timeout_seconds=120,
        ),
        Gate(
            "model_call_ledger.static",
            "engineering",
            ("python", "scripts/audit_model_call_ledger.py", "--json"),
            "Route every direct billable model provider request through ModelCallLedger reservation and settlement.",
            timeout_seconds=120,
        ),
        Gate(
            "health.strict",
            "runtime",
            ("python", "mnemos_cli.py", "health", "--json"),
            "Inspect strict_failures and repair degraded health checks.",
            timeout_seconds=120,
        ),
        Gate(
            e2e_id,
            "runtime",
            e2e_command,
            "Fix the user-value wow path named in the probe failure; use --real-api for release scoring.",
            real_api=real_api,
            timeout_seconds=1200,
            notes="Default strict scoring uses mock LLM; release scoring uses real API when --real-api is passed.",
        ),
        Gate(
            "cognitive_readiness.budget",
            "data",
            ("python", "scripts/audit_cognitive_readiness.py", "--json", "--budget"),
            "Close or explicitly record readiness gaps with the owning repair workflow.",
            timeout_seconds=300,
        ),
        Gate(
            "cognitive_readiness.reference",
            "data",
            ("python", "scripts/audit_cognitive_readiness_reference.py", "--json"),
            "Repair immutable Raw-to-Wiki consolidation proof; producer receipts alone are not evidence.",
            timeout_seconds=300,
        ),
        Gate(
            "wiki_lint.budget",
            "wow",
            ("python", "scripts/wiki_lint.py", "--summary", "--json", "--budget"),
            "Rebuild or repair Wiki/Vault quality until budget passes.",
            timeout_seconds=600,
        ),
        Gate(
            "golden_benchmark.strict",
            "wow",
            ("python", "scripts/run_golden_benchmark.py", "--strict", "--mock-llm"),
            "Fix benchmark regressions or update the baseline only with explicit review.",
            strict_only=True,
            slow=True,
            timeout_seconds=900,
        ),
        Gate(
            "install_probe",
            "runtime",
            ("python", "scripts/e2e_install_probe.py", "--tmp-home"),
            "Fix setup lifecycle regressions in a temporary HOME.",
            strict_only=True,
            slow=True,
            timeout_seconds=900,
        ),
        Gate(
            "upgrade_probe",
            "runtime",
            ("python", "scripts/e2e_upgrade_probe.py", "--tmp-home", "--preserve-existing"),
            "Fix upgrade lifecycle regressions in a temporary HOME.",
            strict_only=True,
            slow=True,
            timeout_seconds=900,
        ),
        *contract_gates(),
    ]


def contract_gates() -> list[Gate]:
    commands = [
        (
            "contracts.cognitive_asset_schema",
            ("python", "scripts/audit_cognitive_asset_schema.py", "--strict"),
        ),
        (
            "contracts.cognitive_action_effects",
            ("python", "scripts/audit_cognitive_action_effects.py", "--strict", "--json"),
        ),
        (
            "contracts.quality_decision",
            ("python", "scripts/audit_quality_decision_contract.py", "--strict"),
        ),
        (
            "contracts.capability_registry",
            ("python", "scripts/audit_capability_registry.py", "--strict"),
        ),
        (
            "contracts.privacy_retention",
            ("python", "scripts/audit_privacy_retention_policy.py", "--strict"),
        ),
        (
            "contracts.lifecycle_status",
            ("python", "scripts/audit_lifecycle_status_contract.py", "--strict"),
        ),
        ("contracts.action_ledger", ("python", "scripts/audit_action_ledger.py", "--strict")),
        (
            "contracts.cognitive_state_store",
            ("python", "scripts/audit_cognitive_state_store.py", "--strict", "--json"),
        ),
        (
            "contracts.cognitive_search",
            (
                "python",
                "scripts/audit_cognitive_search.py",
                "--strict",
                "--json",
            ),
        ),
        (
            "contracts.cognitive_projection_lifecycle",
            (
                "python",
                "scripts/audit_cognitive_projection_lifecycle.py",
                "--strict",
                "--json",
            ),
        ),
        (
            "contracts.belief_revision_lineage",
            (
                "python",
                "scripts/audit_belief_revision_lineage.py",
                "--strict",
                "--json",
            ),
        ),
        (
            "contracts.decision_trace_effects",
            (
                "python",
                "scripts/audit_decision_trace_effects.py",
                "--strict",
                "--json",
            ),
        ),
        (
            "contracts.prediction_outcome_lineage",
            (
                "python",
                "scripts/audit_prediction_outcome_lineage.py",
                "--strict",
                "--json",
            ),
        ),
        (
            "contracts.feedback_attribution",
            (
                "python",
                "scripts/audit_feedback_attribution.py",
                "--strict",
                "--json",
            ),
        ),
        (
            "contracts.training_governance",
            (
                "python",
                "scripts/audit_training_governance.py",
                "--static-only",
                "--strict",
                "--json",
            ),
        ),
        (
            "contracts.phase3_cognitive_chain",
            (
                "python",
                "scripts/audit_phase3_cognitive_chain.py",
                "--static-only",
                "--strict",
                "--json",
            ),
        ),
        (
            "contracts.cognitive_calibration_lineage",
            (
                "python",
                "scripts/audit_cognitive_calibration_lineage.py",
                "--strict",
                "--json",
            ),
        ),
        (
            "contracts.cognitive_event_dispatch",
            (
                "python",
                "scripts/audit_cognitive_event_dispatch.py",
                "--strict",
                "--json",
            ),
        ),
        (
            "contracts.evidence_graph_direction",
            (
                "python",
                "scripts/audit_evidence_graph_direction.py",
                "--strict",
                "--json",
            ),
        ),
        ("contracts.domain_glossary", ("python", "scripts/audit_domain_glossary.py", "--strict")),
        ("contracts.scorecard", ("python", "scripts/audit_mnemos_scorecard.py", "--strict")),
        (
            "contracts.persona_profile",
            ("python", "scripts/audit_persona_profile_contract.py", "--strict"),
        ),
        (
            "contracts.persona_runtime_effectiveness",
            (
                "python",
                "scripts/audit_persona_runtime_effectiveness.py",
                "--strict",
                "--json",
            ),
        ),
        (
            "contracts.blindspot_asset_boundaries",
            (
                "python",
                "scripts/audit_blindspot_asset_boundaries.py",
                "--strict",
                "--json",
            ),
        ),
        (
            "contracts.phase5_failure_contracts",
            (
                "python",
                "scripts/audit_phase5_failure_contracts.py",
                "--strict",
                "--json",
            ),
        ),
        (
            "contracts.operational_incident_pipeline",
            (
                "python",
                "scripts/audit_operational_incident_pipeline.py",
                "--self-test",
                "--strict",
                "--json",
            ),
        ),
        (
            "contracts.data_interface_registry",
            ("python", "scripts/audit_data_interface_registry.py", "--strict"),
        ),
        (
            "contracts.test_suite_denominator",
            ("python", "scripts/audit_test_suite_denominator.py", "--strict", "--json"),
        ),
        (
            "behavior.cognitive_scenarios",
            ("python", "scripts/run_cognitive_behavior_scenarios.py", "--json"),
        ),
        (
            "contracts.module_toggle",
            ("python", "scripts/audit_module_toggle_registry.py", "--strict"),
        ),
        (
            "contracts.cold_start_toggle",
            ("python", "scripts/audit_cold_start_toggle_matrix.py", "--strict"),
        ),
        (
            "contracts.toggle_auto_disable",
            ("python", "scripts/audit_toggle_auto_disable_policy.py", "--strict"),
        ),
        (
            "contracts.toggle_output_consumers",
            ("python", "scripts/audit_toggle_output_consumers.py", "--strict"),
        ),
        (
            "contracts.runtime_producer_consumer",
            ("python", "scripts/audit_runtime_producer_consumer_closure.py", "--strict"),
        ),
        (
            "contracts.golden_benchmark",
            ("python", "scripts/audit_golden_benchmark_contract.py", "--strict"),
        ),
        (
            "contracts.install_upgrade",
            ("python", "scripts/audit_install_upgrade_contract.py", "--strict"),
        ),
        ("contracts.function_matrix", ("python", "scripts/audit_function_matrix.py")),
        ("contracts.adaptive_data_flows", ("python", "scripts/audit_adaptive_data_flows.py")),
        ("contracts.ops_resilience", ("python", "scripts/audit_ops_resilience_matrix.py")),
        (
            "contracts.cognitive_behavior",
            ("python", "scripts/audit_cognitive_behavior_scenarios.py"),
        ),
        ("contracts.orphan_report", ("python", "scripts/audit_orphan_modules.py", "--check")),
    ]
    return [
        Gate(
            gate_id,
            "contracts",
            command,
            "Run the failing contract audit directly and update code/docs/tests together.",
            strict_only=True,
            timeout_seconds=240,
        )
        for gate_id, command in commands
    ]


def _expected_gate_plan(args: argparse.Namespace) -> list[Gate]:
    return [
        gate
        for gate in _base_gate_plan(real_api=args.real_api)
        if not gate.strict_only or args.strict
    ]


def _manifest_id(*, strict: bool, real_api: bool) -> str:
    if strict and real_api:
        return "mnemos.full-score.strict-real-api.v1"
    mode = "strict" if strict else "standard"
    api = "real-api" if real_api else "mock-api"
    return f"mnemos.full-score.diagnostic-{mode}-{api}.v1"


def build_gate_manifest(
    args: argparse.Namespace,
    *,
    expected_gates: Sequence[Gate] | None = None,
) -> GateManifest:
    gates = list(expected_gates) if expected_gates is not None else _expected_gate_plan(args)
    contracts = tuple(_gate_contract(gate) for gate in gates)
    expected_ids = tuple(gate.gate_id for gate in gates)
    manifest_id = _manifest_id(strict=args.strict, real_api=args.real_api)
    body = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest_id": manifest_id,
        "expected_gate_ids": list(expected_ids),
        "gate_contracts": list(contracts),
    }
    return GateManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        manifest_id=manifest_id,
        manifest_hash=_canonical_hash(body),
        expected_gate_ids=expected_ids,
        gate_contracts=contracts,
    )


def build_gate_plan(args: argparse.Namespace) -> list[Gate]:
    only = _split_csv(args.only)
    skipped = _split_csv(args.skip)
    expected = _expected_gate_plan(args)
    known_ids = {gate.gate_id for gate in expected}
    if args.only is not None:
        if not only:
            raise ValueError("--only must select at least one known gate id")
        unknown_only = sorted(only - known_ids)
        if unknown_only:
            raise ValueError(f"--only contains unknown gate ids: {unknown_only}")
    unknown_skipped = sorted(skipped - known_ids)
    if unknown_skipped:
        raise ValueError(f"--skip contains unknown gate ids: {unknown_skipped}")
    gates = []
    for gate in expected:
        if args.skip_slow and gate.slow:
            continue
        if args.skip_tests and gate.gate_id.startswith("tests."):
            continue
        if args.skip_e2e and gate.gate_id.startswith("e2e."):
            continue
        if args.skip_wiki and gate.gate_id == "wiki_lint.budget":
            continue
        if args.skip_readiness and gate.gate_id in {
            "cognitive_readiness.budget",
            "cognitive_readiness.reference",
        }:
            continue
        if only and gate.gate_id not in only:
            continue
        if gate.gate_id in skipped:
            continue
        gates.append(gate)
    return gates


def _evaluate_health_stdout(stdout: str, *, strict: bool) -> tuple[bool, dict[str, Any], str]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return False, {}, f"health output is not valid JSON: {exc}"
    strict_failures = _list_field(payload, "strict_failures")
    failed_checks = _list_field(payload, "failed_checks")
    degraded_checks = _list_field(payload, "degraded_checks")
    warning_checks = _list_field(payload, "warning_checks")
    skipped_critical_checks = _list_field(payload, "skipped_critical_checks")
    skipped_critical_checks.extend(_list_field(payload, "critical_skipped_checks"))
    evidence = {
        "status": payload.get("status"),
        "ok": payload.get("ok"),
        "usable": payload.get("usable"),
        "strict_ok": payload.get("strict_ok"),
        "strict_failures": strict_failures,
        "failed_checks": failed_checks,
        "degraded_checks": degraded_checks,
        "warning_checks": warning_checks,
        "skipped_critical_checks": skipped_critical_checks,
    }
    errors: list[str] = []
    if payload.get("status") != "ok":
        errors.append(f"health status={payload.get('status')}")
    if payload.get("ok") is not True:
        errors.append("health ok=false")
    if payload.get("usable") is not True:
        errors.append("health usable=false")
    if payload.get("strict_ok") is not True:
        errors.append("health strict_ok=false")
    if strict and strict_failures:
        errors.append(f"health strict_failures={strict_failures}")
    if failed_checks:
        errors.append(f"health failed_checks={failed_checks}")
    if degraded_checks:
        errors.append(f"health degraded_checks={degraded_checks}")
    if warning_checks:
        errors.append(f"health warning_checks={warning_checks}")
    if skipped_critical_checks:
        errors.append(f"health skipped_critical_checks={skipped_critical_checks}")
    return not errors, evidence, "; ".join(errors)


def _forbidden_release_skip_args(args: argparse.Namespace) -> list[str]:
    if not (args.strict and args.real_api):
        return []
    forbidden: list[str] = []
    if args.only is not None:
        forbidden.append("--only")
    if args.skip:
        forbidden.append("--skip")
    for name, option in (
        ("skip_slow", "--skip-slow"),
        ("skip_tests", "--skip-tests"),
        ("skip_e2e", "--skip-e2e"),
        ("skip_wiki", "--skip-wiki"),
        ("skip_readiness", "--skip-readiness"),
    ):
        if getattr(args, name, False):
            forbidden.append(option)
    return forbidden


def run_gate(
    gate: Gate,
    *,
    output_dir: Path,
    python_cmd: str,
    strict: bool,
    environment: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
) -> GateResult:
    if environment is None or not environment.get("MNEMOS_RUN_ENVIRONMENT_HASH"):
        raise ValueError("run_gate requires an explicit hermetic environment")
    command = _replace_python(gate.command, python_cmd)
    log_stem = _slug(gate.gate_id)
    stdout_path = output_dir / "logs" / f"{log_stem}.stdout.txt"
    stderr_path = output_dir / "logs" / f"{log_stem}.stderr.txt"
    start = time.monotonic()
    returncode: int | None = None
    stdout = ""
    stderr = ""
    error = ""

    try:
        proc = runner(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=gate.timeout_seconds,
            env=dict(environment),
        )
        returncode = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        returncode = None
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        error = f"timed out after {gate.timeout_seconds}s"
    except OSError as exc:
        returncode = None
        error = str(exc)

    duration = time.monotonic() - start
    _write_text(stdout_path, stdout)
    _write_text(stderr_path, stderr)

    evidence: dict[str, Any] = {}
    status = "passed" if returncode == 0 and not error else "failed"
    if gate.gate_id == "health.strict" and stdout:
        health_ok, evidence, health_error = _evaluate_health_stdout(stdout, strict=strict)
        if not health_ok:
            status = "failed"
            error = health_error

    if returncode not in (0, None):
        error = error or f"command exited {returncode}"

    return GateResult(
        gate_id=gate.gate_id,
        category=gate.category,
        command=command,
        required=gate.required,
        status=status,
        returncode=returncode,
        duration_seconds=round(duration, 3),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        repair_hint=gate.repair_hint,
        notes=gate.notes,
        evidence=evidence,
        error=error,
        stdout_sha256=hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        stderr_sha256=hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
    )


def summarize(results: Sequence[GateResult]) -> dict[str, Any]:
    categories: dict[str, dict[str, int | float]] = {}
    for result in results:
        bucket = categories.setdefault(
            result.category,
            {"total": 0, "passed": 0, "failed": 0, "score": 0.0},
        )
        bucket["total"] = int(bucket["total"]) + 1
        if result.passed:
            bucket["passed"] = int(bucket["passed"]) + 1
        elif result.failed:
            bucket["failed"] = int(bucket["failed"]) + 1
    for bucket in categories.values():
        total = int(bucket["total"])
        passed = int(bucket["passed"])
        bucket["score"] = round((passed / total) * 100, 1) if total else 100.0

    required_failed = [result.gate_id for result in results if result.required and result.failed]
    errors = [] if results else ["no gates executed"]
    return {
        "total": len(results),
        "passed": sum(1 for result in results if result.passed),
        "failed": sum(1 for result in results if result.failed),
        "required_failed": required_failed,
        "ok": not required_failed and bool(results),
        "errors": errors,
        "categories": categories,
    }


def _certificate_hash_input(
    *,
    certification: Mapping[str, Any],
    summary: Mapping[str, Any],
    environment_hash: str,
    git_commit: str,
    git_clean: bool,
    git_status_hash: str,
) -> dict[str, Any]:
    return {
        "schema_version": certification["schema_version"],
        "manifest_id": certification["manifest_id"],
        "manifest_hash": certification["manifest_hash"],
        "expected_gate_ids": certification["expected_gate_ids"],
        "selected_gate_ids": certification["selected_gate_ids"],
        "executed_gate_ids": certification["executed_gate_ids"],
        "omitted_gate_ids": certification["omitted_gate_ids"],
        "gate_receipts": certification["gate_receipts"],
        "certifying": certification["certifying"],
        "release_eligible": certification["release_eligible"],
        "summary": {
            "total": summary.get("total"),
            "passed": summary.get("passed"),
            "failed": summary.get("failed"),
            "required_failed": summary.get("required_failed"),
            "ok": summary.get("ok"),
        },
        "environment_hash": environment_hash,
        "git_commit": git_commit,
        "git_clean": git_clean,
        "git_status_hash": git_status_hash,
    }


def _build_certification(
    *,
    manifest: GateManifest,
    selected_gates: Sequence[Gate],
    results: Sequence[GateResult],
    strict: bool,
    real_api: bool,
    summary: Mapping[str, Any],
    environment_hash: str,
    git_commit: str,
    git_clean: bool,
    git_status_hash: str,
) -> dict[str, Any]:
    expected_ids = list(manifest.expected_gate_ids)
    selected_ids = [gate.gate_id for gate in selected_gates]
    executed_ids = [result.gate_id for result in results]
    selected_set = set(selected_ids)
    omitted_ids = [gate_id for gate_id in expected_ids if gate_id not in selected_set]
    exact_denominator = bool(expected_ids) and expected_ids == selected_ids == executed_ids
    certifying = bool(strict and real_api and exact_denominator and git_clean)
    release_eligible = bool(certifying and summary.get("ok") is True)
    certification: dict[str, Any] = {
        "schema_version": CERTIFICATE_SCHEMA_VERSION,
        "manifest_id": manifest.manifest_id,
        "manifest_hash": manifest.manifest_hash,
        "expected_gate_ids": expected_ids,
        "selected_gate_ids": selected_ids,
        "executed_gate_ids": executed_ids,
        "omitted_gate_ids": omitted_ids,
        "gate_contracts": list(manifest.gate_contracts),
        "gate_receipts": [
            {
                "gate_id": result.gate_id,
                "required": result.required,
                "status": result.status,
                "returncode": result.returncode,
                "error": result.error,
                "stdout_path": result.stdout_path,
                "stderr_path": result.stderr_path,
                "stdout_sha256": result.stdout_sha256,
                "stderr_sha256": result.stderr_sha256,
            }
            for result in results
        ],
        "certifying": certifying,
        "release_eligible": release_eligible,
        "non_certifying_reasons": [],
    }
    reasons = certification["non_certifying_reasons"]
    if not strict:
        reasons.append("strict_mode_required")
    if not real_api:
        reasons.append("real_api_authorization_required")
    if not git_clean:
        reasons.append("clean_git_worktree_required")
    if not expected_ids:
        reasons.append("empty_expected_gate_set")
    if expected_ids != selected_ids:
        reasons.append("selected_gate_set_differs_from_manifest")
    if selected_ids != executed_ids:
        reasons.append("executed_gate_set_differs_from_selection")
    if summary.get("ok") is not True:
        reasons.append("required_gate_failure")
    certification["certificate_hash"] = _canonical_hash(
        _certificate_hash_input(
            certification=certification,
            summary=summary,
            environment_hash=environment_hash,
            git_commit=git_commit,
            git_clean=git_clean,
            git_status_hash=git_status_hash,
        )
    )
    return certification


def build_certification_payload(
    *,
    expected_gates: Sequence[Gate],
    selected_gates: Sequence[Gate],
    results: Sequence[GateResult],
    strict: bool,
    real_api: bool,
    environment_hash: str,
    git_commit: str,
    git_clean: bool = True,
    git_status_hash: str | None = None,
) -> dict[str, Any]:
    if git_status_hash is None:
        git_status_hash = hashlib.sha256(b"").hexdigest()
    args = argparse.Namespace(strict=strict, real_api=real_api)
    manifest = build_gate_manifest(args, expected_gates=expected_gates)
    summary = summarize(results)
    certification = _build_certification(
        manifest=manifest,
        selected_gates=selected_gates,
        results=results,
        strict=strict,
        real_api=real_api,
        summary=summary,
        environment_hash=environment_hash,
        git_commit=git_commit,
        git_clean=git_clean,
        git_status_hash=git_status_hash,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "git_commit": git_commit,
        "git_state": {
            "commit": git_commit,
            "clean": git_clean,
            "status_hash": git_status_hash,
        },
        "strict": strict,
        "real_api": real_api,
        "run_environment": {"environment_hash": environment_hash},
        "summary": summary,
        "certification": certification,
        "gates": [result.__dict__ for result in results],
    }


def verify_certificate_payload(
    payload: Mapping[str, Any],
    *,
    expected_manifest: GateManifest | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        return {"ok": False, "errors": ["legacy_scope_unverifiable"]}
    certification = payload.get("certification")
    if not isinstance(certification, Mapping):
        return {"ok": False, "errors": ["missing_certification"]}
    required_certificate_fields = {
        "schema_version",
        "manifest_id",
        "manifest_hash",
        "expected_gate_ids",
        "selected_gate_ids",
        "executed_gate_ids",
        "omitted_gate_ids",
        "gate_contracts",
        "gate_receipts",
        "certifying",
        "release_eligible",
        "certificate_hash",
    }
    if required_certificate_fields - set(certification):
        return {"ok": False, "errors": ["invalid_certificate_structure"]}
    if certification.get("schema_version") != CERTIFICATE_SCHEMA_VERSION:
        errors.append("invalid_certificate_schema")
    expected_ids = certification.get("expected_gate_ids")
    contracts = certification.get("gate_contracts")
    if not isinstance(expected_ids, list) or not isinstance(contracts, list):
        return {"ok": False, "errors": ["invalid_manifest"]}
    contract_ids = [item.get("gate_id") for item in contracts if isinstance(item, Mapping)]
    manifest_body = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest_id": certification.get("manifest_id"),
        "expected_gate_ids": expected_ids,
        "gate_contracts": contracts,
    }
    if contract_ids != expected_ids or _canonical_hash(manifest_body) != certification.get(
        "manifest_hash"
    ):
        errors.append("manifest_hash_mismatch")
    authoritative_manifest = expected_manifest or build_gate_manifest(
        argparse.Namespace(strict=True, real_api=True)
    )
    if (
        certification.get("manifest_id") != authoritative_manifest.manifest_id
        or certification.get("manifest_hash") != authoritative_manifest.manifest_hash
        or expected_ids != list(authoritative_manifest.expected_gate_ids)
        or contracts != list(authoritative_manifest.gate_contracts)
    ):
        errors.append("authoritative_manifest_mismatch")
    if expected_manifest is None:
        required_phase5_contracts, required_phase5_manifest_errors = (
            _load_required_phase5_gate_contracts()
        )
        errors.extend(required_phase5_manifest_errors)
        reported_contracts = {
            contract.get("gate_id"): contract
            for contract in contracts
            if isinstance(contract, Mapping) and isinstance(contract.get("gate_id"), str)
        }
        for required_contract in required_phase5_contracts:
            gate_id = str(required_contract["gate_id"])
            reported_contract = reported_contracts.get(gate_id)
            if reported_contract is None:
                errors.append(f"required_phase5_gate_missing:{gate_id}")
            elif dict(reported_contract) != required_contract:
                errors.append(f"required_phase5_gate_contract_mismatch:{gate_id}")
    selected_ids = certification.get("selected_gate_ids")
    executed_ids = certification.get("executed_gate_ids")
    omitted_ids = certification.get("omitted_gate_ids")
    if not expected_ids:
        errors.append("empty_expected_gate_set")
    if expected_ids != selected_ids or selected_ids != executed_ids or omitted_ids != []:
        errors.append("gate_denominator_mismatch")
    receipts = certification.get("gate_receipts")
    receipt_ids = (
        [item.get("gate_id") for item in receipts if isinstance(item, Mapping)]
        if isinstance(receipts, list)
        else []
    )
    if (
        not isinstance(receipts, list)
        or len(receipt_ids) != len(receipts)
        or receipt_ids != executed_ids
    ):
        errors.append("invalid_gate_receipts")
    else:
        for receipt in receipts:
            assert isinstance(receipt, Mapping)
            for stream in ("stdout", "stderr"):
                path_value = receipt.get(f"{stream}_path")
                expected_hash = receipt.get(f"{stream}_sha256")
                if not isinstance(path_value, str) or not isinstance(expected_hash, str):
                    errors.append(f"invalid_{stream}_artifact:{receipt.get('gate_id')}")
                    continue
                path = Path(path_value)
                if not path.is_file():
                    errors.append(f"missing_{stream}_artifact:{receipt.get('gate_id')}")
                    continue
                actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual_hash != expected_hash:
                    errors.append(f"{stream}_artifact_hash_mismatch:{receipt.get('gate_id')}")
        required_receipt_failures = [
            receipt.get("gate_id")
            for receipt in receipts
            if receipt.get("required") is True
            and (receipt.get("status") != "passed" or receipt.get("returncode") != 0)
        ]
        if required_receipt_failures:
            errors.append("required_gate_receipt_failure")
    summary = payload.get("summary")
    environment = payload.get("run_environment")
    git_state = payload.get("git_state")
    if (
        not isinstance(summary, Mapping)
        or not isinstance(environment, Mapping)
        or not isinstance(git_state, Mapping)
    ):
        errors.append("invalid_certificate_context")
    else:
        receipt_summary = {
            "total": len(receipts) if isinstance(receipts, list) else 0,
            "passed": (
                sum(1 for receipt in receipts if receipt.get("status") == "passed")
                if isinstance(receipts, list)
                else 0
            ),
            "failed": (
                sum(1 for receipt in receipts if receipt.get("status") == "failed")
                if isinstance(receipts, list)
                else 0
            ),
            "required_failed": (
                [
                    receipt.get("gate_id")
                    for receipt in receipts
                    if receipt.get("required") is True and receipt.get("status") == "failed"
                ]
                if isinstance(receipts, list)
                else []
            ),
        }
        if any(summary.get(key) != value for key, value in receipt_summary.items()):
            errors.append("summary_receipt_mismatch")
        expected_certificate_hash = _canonical_hash(
            _certificate_hash_input(
                certification=certification,
                summary=summary,
                environment_hash=str(environment.get("environment_hash", "")),
                git_commit=str(payload.get("git_commit", "")),
                git_clean=git_state.get("clean") is True,
                git_status_hash=str(git_state.get("status_hash", "")),
            )
        )
        if expected_certificate_hash != certification.get("certificate_hash"):
            errors.append("certificate_hash_mismatch")
        if summary.get("ok") is not True:
            errors.append("required_gate_failure")
        current_git_state = _git_state()
        if (
            git_state.get("clean") is not True
            or payload.get("git_commit") != current_git_state["commit"]
            or git_state.get("commit") != current_git_state["commit"]
            or git_state.get("status_hash") != current_git_state["status_hash"]
            or current_git_state["clean"] is not True
        ):
            errors.append("git_state_mismatch")
    if certification.get("certifying") is not True:
        errors.append("not_certifying")
    if certification.get("release_eligible") is not True:
        errors.append("not_release_eligible")
    return {"ok": not errors, "errors": errors}


def build_report_payload(
    *,
    args: argparse.Namespace,
    output_dir: Path,
    gates: Sequence[Gate],
    expected_gates: Sequence[Gate],
    results: Sequence[GateResult],
    run_environment: HermeticRunEnvironment,
) -> dict[str, Any]:
    summary = summarize(results)
    git_state = _git_state()
    git_commit = str(git_state["commit"])
    manifest = build_gate_manifest(args, expected_gates=expected_gates)
    certification = _build_certification(
        manifest=manifest,
        selected_gates=gates,
        results=results,
        strict=args.strict,
        real_api=args.real_api,
        summary=summary,
        environment_hash=run_environment.environment_hash,
        git_commit=git_commit,
        git_clean=bool(git_state["clean"]),
        git_status_hash=str(git_state["status_hash"]),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(ROOT),
        "git_commit": git_commit,
        "git_state": git_state,
        "strict": args.strict,
        "real_api": args.real_api,
        "skip_arguments": _forbidden_release_skip_args(args),
        "output_dir": str(output_dir),
        "planned_gate_count": len(gates),
        "expected_gate_count": len(expected_gates),
        "run_environment": run_environment.report(),
        "summary": summary,
        "certification": certification,
        "gates": [result.__dict__ for result in results],
    }


def render_markdown(payload: Mapping[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Mnemos Full Score Gate Report",
        "",
        f"- schema: `{payload['schema_version']}`",
        f"- generated_at: `{payload['generated_at']}`",
        f"- git_commit: `{payload['git_commit']}`",
        f"- strict: `{payload['strict']}`",
        f"- real_api: `{payload['real_api']}`",
        f"- ok: `{summary['ok']}`",
        f"- certifying: `{payload['certification']['certifying']}`",
        f"- release_eligible: `{payload['certification']['release_eligible']}`",
        f"- certificate_verified: `{payload.get('certificate_verification', {}).get('ok')}`",
        f"- manifest_id: `{payload['certification']['manifest_id']}`",
        f"- manifest_hash: `{payload['certification']['manifest_hash']}`",
        f"- certificate_hash: `{payload['certification']['certificate_hash']}`",
        f"- expected_gate_count: `{len(payload['certification']['expected_gate_ids'])}`",
        f"- selected_gate_count: `{len(payload['certification']['selected_gate_ids'])}`",
        f"- executed_gate_count: `{len(payload['certification']['executed_gate_ids'])}`",
        f"- omitted_gate_ids: `{payload['certification']['omitted_gate_ids']}`",
        f"- output_dir: `{payload['output_dir']}`",
        f"- sandbox_root: `{payload['run_environment']['sandbox_root']}`",
        f"- environment_hash: `{payload['run_environment']['environment_hash']}`",
        f"- outside_write_count: `{payload['run_environment']['outside_write_count']}`",
        "",
        "## Category Scores",
        "",
        "| category | passed | failed | total | score |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for category, bucket in sorted(summary["categories"].items()):
        lines.append(
            f"| {category} | {bucket['passed']} | {bucket['failed']} | "
            f"{bucket['total']} | {bucket['score']} |"
        )
    lines.extend(
        [
            "",
            "## Gate Results",
            "",
            "| gate | category | status | stdout | stderr | repair hint |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for gate in payload["gates"]:
        status = gate["status"]
        error = f" ({gate['error']})" if gate.get("error") else ""
        lines.append(
            f"| `{gate['gate_id']}` | {gate['category']} | {status}{error} | "
            f"`{gate['stdout_path']}` | `{gate['stderr_path']}` | "
            f"{gate['repair_hint']} |"
        )
    return "\n".join(lines) + "\n"


def write_reports(payload: Mapping[str, Any], output_dir: Path) -> tuple[Path, Path]:
    json_path = output_dir / "full_score_gates.json"
    markdown_path = output_dir / "full_score_gates.md"
    _write_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    _write_text(markdown_path, render_markdown(payload))
    return json_path, markdown_path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="include slow/release gates")
    parser.add_argument(
        "--real-api", action="store_true", help="run wow-path E2E with real API instead of mock LLM"
    )
    parser.add_argument("--json", action="store_true", help="print JSON report to stdout")
    parser.add_argument("--output-dir", help="directory for JSON, Markdown, and per-gate logs")
    parser.add_argument("--only", help="comma-separated gate ids to run")
    parser.add_argument("--skip", help="comma-separated gate ids to skip")
    parser.add_argument("--skip-slow", action="store_true", help="skip gates marked as slow")
    parser.add_argument(
        "--skip-tests", action="store_true", help="skip tests.quick/integration/heavy"
    )
    parser.add_argument("--skip-e2e", action="store_true", help="skip E2E probe gate")
    parser.add_argument("--skip-wiki", action="store_true", help="skip wiki_lint.budget")
    parser.add_argument(
        "--skip-readiness", action="store_true", help="skip cognitive_readiness.budget"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, runner: Runner = subprocess.run) -> int:
    args = _parse_args(argv)
    forbidden_skip_args = _forbidden_release_skip_args(args)
    if forbidden_skip_args:
        print(
            "--strict --real-api full-score runs do not allow skip arguments: "
            + ", ".join(forbidden_skip_args),
            file=sys.stderr,
        )
        return 2
    try:
        expected_gates = _expected_gate_plan(args)
        gates = build_gate_plan(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else Path(tempfile.gettempdir()) / "mnemos-full-score-gates" / _now_slug()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    run_environment = HermeticRunEnvironment.create(
        output_dir,
        profile="isolated",
        inherit_credentials=bool(args.real_api),
    )
    python_cmd = _python_cmd()
    results = [
        run_gate(
            gate,
            output_dir=output_dir,
            python_cmd=python_cmd,
            strict=args.strict,
            environment=run_environment.environment,
            runner=runner,
        )
        for gate in gates
    ]
    formal_state_diff = run_environment.finalize()
    if formal_state_diff:
        results.append(
            GateResult(
                gate_id="hermeticity.formal_state",
                category="engineering",
                command=[],
                required=True,
                status="failed",
                returncode=None,
                duration_seconds=0.0,
                stdout_path="",
                stderr_path="",
                repair_hint="Remove the sandbox escape and rerun against unchanged formal state.",
                evidence={"formal_state_diff": formal_state_diff},
                error="formal state changed outside the run sandbox",
            )
        )
    payload = build_report_payload(
        args=args,
        output_dir=output_dir,
        gates=gates,
        expected_gates=expected_gates,
        results=results,
        run_environment=run_environment,
    )
    payload["certificate_verification"] = verify_certificate_payload(payload)
    json_path, markdown_path = write_reports(payload, output_dir)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        summary = payload["summary"]
        print(
            f"Full score gates: ok={summary['ok']} "
            f"passed={summary['passed']} failed={summary['failed']} total={summary['total']}"
        )
        if summary["required_failed"]:
            print("Failed required gates:")
            for gate_id in summary["required_failed"]:
                print(f"  - {gate_id}")
        print(f"JSON report: {json_path}")
        print(f"Markdown report: {markdown_path}")
    if args.strict and args.real_api:
        return (
            0
            if (
                payload["certification"]["release_eligible"]
                and payload["certificate_verification"]["ok"]
            )
            else 1
        )
    return 0 if payload["summary"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
