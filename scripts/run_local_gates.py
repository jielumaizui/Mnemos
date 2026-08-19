#!/usr/bin/env python3
"""Run the same local gates as .pre-commit-config.yaml without installing pre-commit.

Usage:
    python3 scripts/run_local_gates.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

GATES: List[Tuple[str, List[str]]] = [
    ("flake8", ["python", "-m", "flake8", "--count"]),
    ("mypy budget", ["python", "scripts/mypy_budget.py"]),
    (
        "compileall",
        [
            "python",
            "-m",
            "compileall",
            "-q",
            "core/",
            "integrations/",
            "daemon/",
            "scripts/",
            "mnemos_cli.py",
            "mnemos_daemon.py",
        ],
    ),
    ("bare except check", ["python", "scripts/check_bare_except.py"]),
    ("maintainability closure", ["python", "scripts/check_maintainability_budget.py", "--closure"]),
    ("config examples", ["python", "scripts/verify_config_examples.py", "--strict"]),
    (
        "config registry closure",
        ["python", "scripts/audit_config_registry_closure.py", "--strict"],
    ),
    (
        "relation evidence schema registry",
        ["python", "scripts/audit_schema_registry.py", "--strict", "--json"],
    ),
    (
        "cognitive state store contract",
        ["python", "scripts/audit_cognitive_state_store.py", "--strict", "--json"],
    ),
    (
        "cognitive search contract",
        [
            "python",
            "scripts/audit_cognitive_search.py",
            "--strict",
            "--json",
        ],
    ),
    (
        "cognitive projection lifecycle",
        [
            "python",
            "scripts/audit_cognitive_projection_lifecycle.py",
            "--strict",
            "--json",
        ],
    ),
    (
        "belief revision lineage",
        ["python", "scripts/audit_belief_revision_lineage.py", "--strict", "--json"],
    ),
    (
        "decision trace effects",
        ["python", "scripts/audit_decision_trace_effects.py", "--strict", "--json"],
    ),
    (
        "prediction outcome lineage",
        [
            "python",
            "scripts/audit_prediction_outcome_lineage.py",
            "--strict",
            "--json",
        ],
    ),
    (
        "feedback attribution closure",
        ["python", "scripts/audit_feedback_attribution.py", "--strict", "--json"],
    ),
    (
        "training governance static contract",
        [
            "python",
            "scripts/audit_training_governance.py",
            "--static-only",
            "--strict",
            "--json",
        ],
    ),
    (
        "phase3 cognitive chain",
        [
            "python",
            "scripts/audit_phase3_cognitive_chain.py",
            "--strict",
            "--json",
        ],
    ),
    (
        "cognitive calibration lineage",
        ["python", "scripts/audit_cognitive_calibration_lineage.py", "--strict", "--json"],
    ),
    (
        "cognition episode event dispatch",
        ["python", "scripts/audit_cognitive_event_dispatch.py", "--strict", "--json"],
    ),
    (
        "evidence graph direction",
        ["python", "scripts/audit_evidence_graph_direction.py", "--strict", "--json"],
    ),
    (
        "tech debt annotations",
        [
            "python",
            "scripts/check_tech_debt_annotations.py",
            "core/",
            "integrations/",
            "daemon/",
            "scripts/",
            "mnemos_cli.py",
            "mnemos_daemon.py",
        ],
    ),
    ("hardcoded path audit", ["python", "scripts/audit_hardcoded_paths.py", "--strict"]),
    (
        "document asset manifest audit",
        ["python", "scripts/audit_document_asset_manifest.py", "--strict"],
    ),
    ("docs freshness audit", ["python", "scripts/audit_docs_freshness.py", "--strict"]),
    ("desktop system-map facts audit", ["python", "scripts/audit_desktop_system_map_facts.py"]),
    ("event bus map", ["python", "scripts/generate_event_bus_map.py", "--check"]),
    ("delayed import audit", ["python", "scripts/audit_delayed_imports.py"]),
    ("docs sensitive info audit", ["python", "scripts/audit_docs_sensitive_info.py", "--strict"]),
    (
        "repo sensitive literal audit",
        ["python", "scripts/audit_repo_sensitive_literals.py", "--strict"],
    ),
    (
        "release privacy security audit",
        ["python", "scripts/audit_release_privacy_security.py", "--strict"],
    ),
    ("docs stale service key audit", ["python", "scripts/audit_docs_stale_service_keys.py"]),
    ("adaptive policy coverage", ["python", "scripts/audit_adaptive_policy_matrix.py", "--strict"]),
    (
        "runtime producer/consumer closure",
        ["python", "scripts/audit_runtime_producer_consumer_closure.py", "--strict"],
    ),
    ("KG relation contract", ["python", "scripts/audit_kg_relation_contract.py"]),
    (
        "distill output contract",
        ["python", "scripts/audit_distill_output_contract.py", "--strict"],
    ),
    (
        "cognitive source authority",
        ["python", "scripts/audit_cognitive_source_authority.py", "--strict", "--json"],
    ),
    (
        "cognitive action effect closure",
        ["python", "scripts/audit_cognitive_action_effects.py", "--strict", "--json"],
    ),
    ("distill response budget", ["python", "scripts/audit_distill_response_budget.py"]),
    (
        "model call ledger boundary audit",
        ["python", "scripts/audit_model_call_ledger.py", "--json"],
    ),
    ("trusted push static scan", ["python", "-m", "core.trust.static_scan"]),
    ("No Zombie Code closure", ["python", "scripts/check_zombie_code_policy.py", "--closure"]),
    ("arch dependency graph", ["python", "scripts/arch_dependency_graph.py", "--check"]),
    ("CI ratchet closure", ["python", "scripts/ci_ratchet.py", "--closure", "--strict"]),
    ("vulture", ["python", "-m", "vulture", "--min-confidence", "80", "."]),
    ("security audit", ["python", "scripts/security_audit.py"]),
]


def _python_cmd() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    candidates = [
        repo_root / ".venv" / "bin" / "python",
        repo_root / ".venv" / "Scripts" / "python.exe",
    ]
    current = Path(sys.executable)
    for candidate in candidates:
        if candidate.exists() and candidate != current:
            return str(candidate)
    return sys.executable


def main() -> int:
    failed = []
    print(f"Using Python: {_python_cmd()}")
    for name, cmd in GATES:
        cmd = [_python_cmd() if c == "python" else c for c in cmd]
        print(f"\n==> Running: {name}")
        result = subprocess.run(cmd, cwd=".")
        if result.returncode != 0:
            failed.append(name)
            print(f"FAILED: {name}")
        else:
            print(f"PASSED: {name}")

    print("\n" + "=" * 60)
    if failed:
        print("Local gates FAILED:")
        for name in failed:
            print(f"  - {name}")
        return 1

    print(
        "All local development gates PASSED. This ratchet/accepted-debt profile "
        "is not a full-score release certificate."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
