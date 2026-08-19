from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import run_full_score_gates as full_gates


def _args(**overrides):
    defaults = {
        "strict": True,
        "real_api": True,
        "json": False,
        "output_dir": None,
        "only": None,
        "skip": None,
        "skip_slow": False,
        "skip_tests": False,
        "skip_e2e": False,
        "skip_wiki": False,
        "skip_readiness": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _environment(tmp_path: Path):
    return full_gates.HermeticRunEnvironment.create(
        tmp_path / "environment", profile="isolated"
    ).environment


def test_strict_real_api_plan_includes_full_score_gates():
    plan = full_gates.build_gate_plan(_args())
    gate_ids = {gate.gate_id for gate in plan}

    assert len(plan) == 63
    assert "tests.quick" in gate_ids
    assert "tests.integration" in gate_ids
    assert "tests.heavy" in gate_ids
    assert "local_gates" in gate_ids
    assert "docs.asset_manifest.strict" in gate_ids
    assert "health.strict" in gate_ids
    assert "security.strict" in gate_ids
    assert "security.release_privacy" in gate_ids
    assert "e2e.wow_real_api" in gate_ids
    assert "config.examples.strict" in gate_ids
    assert "config.registry.closure" in gate_ids
    assert "model_call_ledger.static" in gate_ids
    assert "contracts.cognitive_projection_lifecycle" in gate_ids
    assert "cognitive_readiness.budget" in gate_ids
    assert "cognitive_readiness.reference" in gate_ids
    assert "wiki_lint.budget" in gate_ids
    assert "contracts.scorecard" in gate_ids
    assert "contracts.persona_profile" in gate_ids
    assert "contracts.belief_revision_lineage" in gate_ids
    assert "contracts.cognitive_search" in gate_ids
    assert "contracts.decision_trace_effects" in gate_ids
    assert "contracts.cognitive_event_dispatch" in gate_ids
    assert "contracts.evidence_graph_direction" in gate_ids
    assert "contracts.data_interface_registry" in gate_ids
    assert "contracts.test_suite_denominator" in gate_ids
    assert "behavior.cognitive_scenarios" in gate_ids
    assert "contracts.orphan_report" in gate_ids
    assert "contracts.operational_incident_pipeline" in gate_ids

    e2e_gate = next(
        gate for gate in full_gates.build_gate_plan(_args()) if gate.gate_id == "e2e.wow_real_api"
    )
    assert e2e_gate.command == ("python", "scripts/e2e_wow_probe.py", "--real-api")


def test_strict_release_plan_requires_phase_five_runtime_gates():
    gates_by_id = {gate.gate_id: gate for gate in full_gates.build_gate_plan(_args())}

    assert gates_by_id["contracts.persona_runtime_effectiveness"].command == (
        "python",
        "scripts/audit_persona_runtime_effectiveness.py",
        "--strict",
        "--json",
    )
    assert gates_by_id["contracts.blindspot_asset_boundaries"].command == (
        "python",
        "scripts/audit_blindspot_asset_boundaries.py",
        "--strict",
        "--json",
    )
    assert gates_by_id["contracts.phase5_failure_contracts"].command == (
        "python",
        "scripts/audit_phase5_failure_contracts.py",
        "--strict",
        "--json",
    )
    assert gates_by_id["contracts.operational_incident_pipeline"].command == (
        "python",
        "scripts/audit_operational_incident_pipeline.py",
        "--self-test",
        "--strict",
        "--json",
    )


def test_skip_slow_removes_test_and_release_probe_gates():
    gate_ids = {gate.gate_id for gate in full_gates.build_gate_plan(_args(skip_slow=True))}

    assert "tests.quick" not in gate_ids
    assert "tests.integration" not in gate_ids
    assert "tests.heavy" not in gate_ids
    assert "golden_benchmark.strict" not in gate_ids
    assert "install_probe" not in gate_ids
    assert "local_gates" in gate_ids


def test_non_real_api_plan_runs_wow_mock_llm_gate():
    gates = full_gates.build_gate_plan(_args(real_api=False))
    e2e_gate = next(gate for gate in gates if gate.gate_id == "e2e.wow_mock_llm")

    assert e2e_gate.command == ("python", "scripts/e2e_wow_probe.py", "--mock-llm")


def test_main_writes_reports_and_returns_nonzero_for_required_failure(tmp_path):
    child_environments = []

    def fake_runner(command, **_kwargs):
        child_environments.append(_kwargs["env"])
        command_text = " ".join(command)
        gate_name = command[-1] if command[-1].endswith(".py") else " ".join(command)
        if "audit_cognitive_readiness.py" in command_text:
            return subprocess.CompletedProcess(command, 1, '{"ok": false}', "budget failed")
        if "health" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "ok": True,
                        "usable": True,
                        "strict_ok": True,
                        "status": "ok",
                        "strict_failures": [],
                        "failed_checks": [],
                        "degraded_checks": [],
                        "warning_checks": [],
                    }
                ),
                "",
            )
        return subprocess.CompletedProcess(command, 0, f"{gate_name} ok", "")

    code = full_gates.main(
        [
            "--strict",
            "--output-dir",
            str(tmp_path),
            "--only",
            "local_gates,health.strict,cognitive_readiness.budget",
        ],
        runner=fake_runner,
    )

    assert code == 1
    payload = json.loads((tmp_path / "full_score_gates.json").read_text())
    assert payload["schema_version"] == "mnemos.full_score_gates.v2"
    assert payload["skip_arguments"] == []
    assert payload["summary"]["ok"] is False
    assert payload["summary"]["required_failed"] == ["cognitive_readiness.budget"]
    assert payload["certification"]["certifying"] is False
    assert payload["certification"]["release_eligible"] is False
    assert payload["certification"]["omitted_gate_ids"]
    assert payload["certification"]["selected_gate_ids"] == [
        "local_gates",
        "health.strict",
        "cognitive_readiness.budget",
    ]
    assert payload["run_environment"]["profile"] == "isolated"
    assert payload["run_environment"]["sandbox_root"] == str(tmp_path)
    assert payload["run_environment"]["environment_hash"]
    assert Path(payload["run_environment"]["manifest_path"]).is_file()
    assert child_environments
    assert {env["MNEMOS_RUN_ENVIRONMENT_HASH"] for env in child_environments} == {
        payload["run_environment"]["environment_hash"]
    }
    assert all(Path(env["MNEMOS_DIR"]).is_relative_to(tmp_path) for env in child_environments)
    assert all(
        Path(gate[key]).is_relative_to(tmp_path)
        for gate in payload["gates"]
        for key in ("stdout_path", "stderr_path")
    )
    assert Path(payload["gates"][0]["stdout_path"]).exists()
    markdown = (tmp_path / "full_score_gates.md").read_text()
    assert "stdout" in markdown
    assert payload["gates"][0]["stdout_path"] in markdown


def test_real_api_is_the_only_mode_that_inherits_credentials(monkeypatch, tmp_path):
    created = []
    real_create = full_gates.HermeticRunEnvironment.create

    def capture_create(_cls, *args, **kwargs):
        created.append(kwargs.get("inherit_credentials"))
        return real_create(*args, **kwargs)

    monkeypatch.setattr(
        full_gates.HermeticRunEnvironment,
        "create",
        classmethod(capture_create),
    )
    monkeypatch.setattr(full_gates, "build_gate_plan", lambda _args: [])

    assert full_gates.main(["--output-dir", str(tmp_path / "mock")]) == 1
    assert full_gates.main(["--real-api", "--output-dir", str(tmp_path / "real")]) == 1
    assert created == [False, True]


def test_run_gate_requires_explicit_hermetic_environment(tmp_path):
    gate = full_gates.Gate("safe", "engineering", ("python", "-V"), "repair")

    with pytest.raises(ValueError, match="hermetic environment"):
        full_gates.run_gate(
            gate,
            output_dir=tmp_path,
            python_cmd="python3",
            strict=True,
            runner=lambda *_args, **_kwargs: None,
        )


def test_health_gate_requires_strict_ok_in_strict_mode(tmp_path):
    gate = full_gates.Gate(
        "health.strict",
        "runtime",
        ("python", "mnemos_cli.py", "health", "--json"),
        "repair health",
    )

    def fake_runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "usable": True,
                    "strict_ok": False,
                    "status": "degraded",
                    "strict_failures": ["amphora"],
                }
            ),
            "",
        )

    result = full_gates.run_gate(
        gate,
        output_dir=tmp_path,
        python_cmd="python3",
        strict=True,
        environment=_environment(tmp_path),
        runner=fake_runner,
    )

    assert result.status == "failed"
    assert "health strict_ok=false" in result.error
    assert result.evidence["strict_failures"] == ["amphora"]


def test_health_gate_rejects_warning_health_even_when_usable(tmp_path):
    gate = full_gates.Gate(
        "health.strict",
        "runtime",
        ("python", "mnemos_cli.py", "health", "--json"),
        "repair health",
    )

    def fake_runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "status": "warning",
                    "ok": False,
                    "usable": True,
                    "strict_ok": True,
                    "strict_failures": [],
                    "failed_checks": [],
                    "degraded_checks": [],
                    "warning_checks": ["multimodal"],
                }
            ),
            "",
        )

    result = full_gates.run_gate(
        gate,
        output_dir=tmp_path,
        python_cmd="python3",
        strict=True,
        environment=_environment(tmp_path),
        runner=fake_runner,
    )

    assert result.status == "failed"
    assert "health status=warning" in result.error
    assert "health ok=false" in result.error
    assert "warning_checks" in result.error
    assert result.evidence["warning_checks"] == ["multimodal"]


def test_strict_real_api_rejects_skip_arguments(tmp_path):
    code = full_gates.main(
        [
            "--strict",
            "--real-api",
            "--skip-tests",
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert code == 2
    assert not (tmp_path / "full_score_gates.json").exists()


def test_strict_real_api_rejects_only_selector(tmp_path):
    code = full_gates.main(
        [
            "--strict",
            "--real-api",
            "--only",
            "health.strict",
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert code == 2
    assert not (tmp_path / "full_score_gates.json").exists()


def test_only_selection_limits_gate_plan():
    gates = full_gates.build_gate_plan(
        _args(only="local_gates,health.strict", strict=True, real_api=False)
    )

    assert [gate.gate_id for gate in gates] == ["local_gates", "health.strict"]


@pytest.mark.parametrize("only", ["does-not-exist", ","])
def test_unknown_or_empty_only_selection_is_invalid(only):
    with pytest.raises(ValueError, match="--only"):
        full_gates.build_gate_plan(_args(only=only, strict=True, real_api=False))


def test_empty_summary_is_not_ok():
    summary = full_gates.summarize([])

    assert summary["ok"] is False
    assert summary["errors"] == ["no gates executed"]


def test_gate_manifest_is_versioned_hashed_and_has_fixed_denominator():
    args = _args(strict=True, real_api=True)
    manifest = full_gates.build_gate_manifest(args)
    selected = full_gates.build_gate_plan(args)

    assert manifest.schema_version == "mnemos.full_score_gate_manifest.v1"
    assert manifest.manifest_id == "mnemos.full-score.strict-real-api.v1"
    assert len(manifest.manifest_hash) == 64
    assert manifest.expected_gate_ids == tuple(gate.gate_id for gate in selected)
    assert manifest.expected_gate_ids


def test_dirty_worktree_cannot_issue_release_certificate(tmp_path):
    stdout_path = tmp_path / "one.out"
    stderr_path = tmp_path / "one.err"
    stdout_path.write_text("ok", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    gate = full_gates.Gate("one", "engineering", ("python", "-V"), "repair")
    result = full_gates.GateResult(
        gate_id="one",
        category="engineering",
        command=["python3", "-V"],
        required=True,
        status="passed",
        returncode=0,
        duration_seconds=0.1,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        repair_hint="repair",
        stdout_sha256=hashlib.sha256(b"ok").hexdigest(),
        stderr_sha256=hashlib.sha256(b"").hexdigest(),
    )

    payload = full_gates.build_certification_payload(
        expected_gates=[gate],
        selected_gates=[gate],
        results=[result],
        strict=True,
        real_api=True,
        environment_hash="a" * 64,
        git_commit="deadbeef",
        git_clean=False,
        git_status_hash=hashlib.sha256(b" M file.py\n").hexdigest(),
    )

    assert payload["certification"]["certifying"] is False
    assert payload["certification"]["release_eligible"] is False
    assert "clean_git_worktree_required" in payload["certification"]["non_certifying_reasons"]


def test_certificate_verifier_rejects_tampered_denominator(tmp_path, monkeypatch):
    monkeypatch.setattr(
        full_gates,
        "_git_state",
        lambda: {
            "commit": "deadbeef",
            "clean": True,
            "status_hash": hashlib.sha256(b"").hexdigest(),
        },
    )
    stdout_path = tmp_path / "one.out"
    stderr_path = tmp_path / "one.err"
    stdout_path.write_text("ok", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    gates = [
        full_gates.Gate("one", "engineering", ("python", "-V"), "repair"),
    ]
    results = [
        full_gates.GateResult(
            gate_id="one",
            category="engineering",
            command=["python3", "-V"],
            required=True,
            status="passed",
            returncode=0,
            duration_seconds=0.1,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            repair_hint="repair",
            stdout_sha256=hashlib.sha256(b"ok").hexdigest(),
            stderr_sha256=hashlib.sha256(b"").hexdigest(),
        )
    ]
    payload = full_gates.build_certification_payload(
        expected_gates=gates,
        selected_gates=gates,
        results=results,
        strict=True,
        real_api=True,
        environment_hash="a" * 64,
        git_commit="deadbeef",
    )
    expected_manifest = full_gates.build_gate_manifest(
        argparse.Namespace(strict=True, real_api=True), expected_gates=gates
    )
    assert (
        full_gates.verify_certificate_payload(payload, expected_manifest=expected_manifest)["ok"]
        is True
    )

    payload["certification"]["expected_gate_ids"] = []
    verified = full_gates.verify_certificate_payload(payload, expected_manifest=expected_manifest)

    assert verified["ok"] is False
    assert "manifest_hash_mismatch" in verified["errors"]


def test_full_strict_real_api_run_is_release_eligible_only_for_exact_manifest(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        full_gates,
        "_git_state",
        lambda: {
            "commit": "deadbeef",
            "clean": True,
            "status_hash": hashlib.sha256(b"").hexdigest(),
        },
    )

    def fake_runner(command, **_kwargs):
        if "health" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "status": "ok",
                        "ok": True,
                        "usable": True,
                        "strict_ok": True,
                        "strict_failures": [],
                        "failed_checks": [],
                        "degraded_checks": [],
                        "warning_checks": [],
                    }
                ),
                "",
            )
        return subprocess.CompletedProcess(command, 0, "ok", "")

    assert (
        full_gates.main(
            ["--strict", "--real-api", "--output-dir", str(tmp_path)],
            runner=fake_runner,
        )
        == 0
    )
    payload = json.loads((tmp_path / "full_score_gates.json").read_text())
    certificate = payload["certification"]

    assert certificate["certifying"] is True
    assert certificate["release_eligible"] is True
    assert certificate["omitted_gate_ids"] == []
    assert certificate["expected_gate_ids"] == certificate["selected_gate_ids"]
    assert certificate["selected_gate_ids"] == certificate["executed_gate_ids"]
    assert full_gates.verify_certificate_payload(payload) == {"ok": True, "errors": []}
