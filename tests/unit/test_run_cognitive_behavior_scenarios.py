from __future__ import annotations

import json
import subprocess

from scripts import run_cognitive_behavior_scenarios as runner
from scripts.audit_cognitive_behavior_scenarios import REQUIRED_SCENARIOS


def _matrix(tmp_path, *, tests):
    path = tmp_path / "matrix.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "mnemos.cognitive_behavior_scenarios.v1",
                "updated": "2026-07-12",
                "scenarios": [
                    {
                        "id": scenario_id,
                        "behavior_goal": "goal",
                        "user_scenario": "scenario",
                        "primary_tools": ["health_check"],
                        "behavior_delta": "delta",
                        "evidence_fields": ["status"],
                        "user_explanation": "explanation",
                        "feedback_or_correction_tools": ["health_check"],
                        "code_refs": ["scripts/run_cognitive_behavior_scenarios.py"],
                        "tests": tests,
                        "docs": ["docs/OPS_MANUAL.md"],
                    }
                    for scenario_id in sorted(REQUIRED_SCENARIOS)
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_build_plan_deduplicates_promised_behavior_test_files(tmp_path, monkeypatch):
    matrix = _matrix(tmp_path, tests=["tests/unit/test_run_full_score_gates.py"])
    monkeypatch.setattr(runner, "validate", lambda _path: [])

    plan = runner.build_plan(matrix)

    assert len(plan["scenario_ids"]) == 10
    assert plan["test_files"] == ["tests/unit/test_run_full_score_gates.py"]
    assert plan["errors"] == []


def test_execute_returns_real_pytest_receipt(tmp_path, monkeypatch):
    matrix = _matrix(tmp_path, tests=["tests/unit/test_run_full_score_gates.py"])
    monkeypatch.setattr(runner, "validate", lambda _path: [])
    calls = []

    def fake_runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    report = runner.execute(matrix, runner=fake_runner)

    assert report["ok"] is True
    assert report["returncode"] == 0
    assert calls[0][0][2:4] == ["pytest", "tests/unit/test_run_full_score_gates.py"]
