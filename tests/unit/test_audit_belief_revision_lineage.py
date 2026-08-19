from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from scripts import run_local_gates


def test_strict_belief_lineage_audit_has_zero_metrics(tmp_path):
    from scripts.audit_belief_revision_lineage import audit_belief_revision_lineage

    report = audit_belief_revision_lineage(
        live_state_db=tmp_path / "missing-state.db",
        live_graph_db=tmp_path / "missing-graph.db",
        strict=True,
    )

    assert report["ok"] is True
    assert report["matrix"]["passed_count"] == report["matrix"]["contract_count"]
    assert report["matrix"]["contract_count"] >= 15
    assert all(value == 0 for value in report["metrics"].values())
    assert report["live"]["classification"] == "not_initialized"


def test_strict_belief_lineage_audit_cli_is_machine_readable(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "audit_belief_revision_lineage.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--strict",
            "--json",
            "--state-db",
            str(tmp_path / "missing-state.db"),
            "--graph-db",
            str(tmp_path / "missing-graph.db"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == "mnemos.belief_revision_lineage_audit.v1"
    assert payload["ok"] is True
    assert payload["strict"] is True


def test_belief_lineage_audit_is_required_by_local_precommit_and_ci_gates():
    repo_root = Path(__file__).resolve().parents[2]
    gate_commands = {name: command for name, command in run_local_gates.GATES}

    assert gate_commands["belief revision lineage"] == [
        "python",
        "scripts/audit_belief_revision_lineage.py",
        "--strict",
        "--json",
    ]
    expected_command = "python3 scripts/audit_belief_revision_lineage.py --strict --json"
    assert expected_command in (repo_root / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "python scripts/audit_belief_revision_lineage.py --strict --json" in (
        repo_root / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
