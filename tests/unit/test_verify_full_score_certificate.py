from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from scripts import run_full_score_gates as full_gates
from scripts import verify_full_score_certificate as verifier


@pytest.fixture(autouse=True)
def _clean_git_state(monkeypatch):
    monkeypatch.setattr(
        full_gates,
        "_git_state",
        lambda: {
            "commit": "deadbeef",
            "clean": True,
            "status_hash": hashlib.sha256(b"").hexdigest(),
        },
    )


def _payload(tmp_path):
    gates = full_gates._expected_gate_plan(argparse.Namespace(strict=True, real_api=True))
    results = []
    for gate in gates:
        stdout_path = tmp_path / f"{gate.gate_id}.out"
        stderr_path = tmp_path / f"{gate.gate_id}.err"
        stdout_path.write_text("ok", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        results.append(
            full_gates.GateResult(
                gate_id=gate.gate_id,
                category=gate.category,
                command=["python3", *gate.command[1:]],
                required=gate.required,
                status="passed",
                returncode=0,
                duration_seconds=0.1,
                stdout_path=str(stdout_path),
                stderr_path=str(stderr_path),
                repair_hint=gate.repair_hint,
                stdout_sha256=hashlib.sha256(b"ok").hexdigest(),
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
            )
        )
    return full_gates.build_certification_payload(
        expected_gates=gates,
        selected_gates=gates,
        results=results,
        strict=True,
        real_api=True,
        environment_hash="a" * 64,
        git_commit="deadbeef",
    )


def test_verifier_accepts_exact_hashed_certificate(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_payload(tmp_path)), encoding="utf-8")

    assert verifier.verify_report(report) == {"ok": True, "errors": []}
    assert verifier.main([str(report), "--json"]) == 0


def test_verifier_rejects_legacy_and_tampered_reports(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"schema_version": "mnemos.full_score_gates.v1"}))
    assert verifier.verify_report(legacy) == {
        "ok": False,
        "errors": ["legacy_scope_unverifiable"],
    }

    malformed = tmp_path / "malformed.json"
    malformed.write_text(
        json.dumps(
            {
                "schema_version": "mnemos.full_score_gates.v2",
                "certification": {},
            }
        )
    )
    assert verifier.verify_report(malformed) == {
        "ok": False,
        "errors": ["invalid_certificate_structure"],
    }

    payload = _payload(tmp_path)
    payload["certification"]["gate_receipts"][0]["status"] = "failed"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")
    result = verifier.verify_report(tampered)
    assert result["ok"] is False
    assert "certificate_hash_mismatch" in result["errors"]


def test_verifier_rejects_replaced_gate_artifact(tmp_path):
    payload = _payload(tmp_path)
    report = tmp_path / "report.json"
    report.write_text(json.dumps(payload), encoding="utf-8")
    first_stdout = payload["certification"]["gate_receipts"][0]["stdout_path"]
    Path(first_stdout).write_text("replaced", encoding="utf-8")

    result = verifier.verify_report(report)

    assert result["ok"] is False
    first_gate = payload["certification"]["gate_receipts"][0]["gate_id"]
    assert f"stdout_artifact_hash_mismatch:{first_gate}" in result["errors"]


def test_verifier_rejects_internally_consistent_shrunken_manifest(tmp_path):
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
    )
    report = tmp_path / "shrunken.json"
    report.write_text(json.dumps(payload), encoding="utf-8")

    verified = verifier.verify_report(report)

    assert verified["ok"] is False
    assert "authoritative_manifest_mismatch" in verified["errors"]


@pytest.mark.parametrize(
    "missing_gate_id",
    [
        "contracts.persona_runtime_effectiveness",
        "contracts.blindspot_asset_boundaries",
        "contracts.phase5_failure_contracts",
    ],
)
def test_verifier_rejects_release_report_when_runner_omits_phase_five_gate(
    tmp_path, monkeypatch, missing_gate_id
):
    original_contract_gates = full_gates.contract_gates

    def without_persona_runtime_effectiveness():
        return [gate for gate in original_contract_gates() if gate.gate_id != missing_gate_id]

    monkeypatch.setattr(full_gates, "contract_gates", without_persona_runtime_effectiveness)
    report = tmp_path / "shrunken-phase5-report.json"
    report.write_text(json.dumps(_payload(tmp_path)), encoding="utf-8")

    verified = verifier.verify_report(report)

    assert verified["ok"] is False
    assert f"required_phase5_gate_missing:{missing_gate_id}" in verified["errors"]


def test_verifier_rejects_phase_five_gate_contract_mutation(tmp_path, monkeypatch):
    original_contract_gates = full_gates.contract_gates

    def with_tampered_runtime_audit_contract():
        return [
            (
                replace(
                    gate,
                    command=(
                        "python",
                        "scripts/audit_persona_runtime_effectiveness.py",
                        "--strict",
                    ),
                )
                if gate.gate_id == "contracts.persona_runtime_effectiveness"
                else gate
            )
            for gate in original_contract_gates()
        ]

    monkeypatch.setattr(full_gates, "contract_gates", with_tampered_runtime_audit_contract)
    report = tmp_path / "tampered-phase5-contract.json"
    report.write_text(json.dumps(_payload(tmp_path)), encoding="utf-8")

    verified = verifier.verify_report(report)

    assert verified["ok"] is False
    assert (
        "required_phase5_gate_contract_mismatch:contracts.persona_runtime_effectiveness"
        in verified["errors"]
    )
