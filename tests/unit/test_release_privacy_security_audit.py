from __future__ import annotations

import json
import subprocess
from typing import Sequence

from scripts import audit_release_privacy_security as audit
from scripts import run_local_gates


def _config_report() -> dict:
    return {
        "schema_version": "mnemos.config_audit.v1",
        "ok": True,
        "required_failed": [],
        "items": [
            {
                "item_id": "security.secret_inventory",
                "status": "ok",
                "evidence": {"plaintext_count": 0},
            },
        ],
    }


def _health_report() -> dict:
    return {
        "status": "degraded",
        "strict_ok": False,
        "strict_failures": ["heartbeat"],
        "checks": {
            "security": {
                "status": "ok",
                "permission_violations": [],
                "plaintext_api_key_risks": [],
                "keyring_status": "ok",
                "keyring_risk_level": "best",
                "secret_inventory": {"plaintext_count": 0},
            },
        },
    }


def _runner(
    command: Sequence[str],
    *,
    health_stdout_suffix: str = "",
    config_stdout_suffix: str = "",
    distill_stdout_suffix: str = "",
    e2e_stdout_suffix: str = "",
    docs_ok: bool = True,
    repo_ok: bool = True,
    **_kwargs,
):
    text = " ".join(command)
    if "security_audit.py" in text:
        payload = {
            "schema_version": "mnemos.security_audit.v2",
            "summary": {
                "ok": True,
                "status": "ok",
                "blocking_count": 0,
                "warning_count": 0,
            },
            "findings": [],
            "checks": {},
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
    if "doctor config --strict --json" in text:
        stdout = json.dumps(_config_report())
        return subprocess.CompletedProcess(command, 0, stdout + config_stdout_suffix, "")
    if "health --json" in text:
        stdout = json.dumps(_health_report())
        return subprocess.CompletedProcess(command, 1, stdout + health_stdout_suffix, "")
    if "distill status" in text:
        stdout = "蒸馏队列状态:\n  queue_dir: <HOME>/.mnemos/distill_queue\n"
        return subprocess.CompletedProcess(command, 0, stdout + distill_stdout_suffix, "")
    if "e2e_probe.py --dry-run --no-api" in text:
        stdout = "wiki_dir=<HOME>/Documents/MnemosVault; database_dir=<HOME>/.mnemos/db\n"
        return subprocess.CompletedProcess(command, 0, stdout + e2e_stdout_suffix, "")
    if "audit_docs_sensitive_info.py" in text:
        payload = {"ok": docs_ok, "findings": [{"rule": "real_api_url"}] if not docs_ok else []}
        return subprocess.CompletedProcess(command, 0 if docs_ok else 1, json.dumps(payload), "")
    if "audit_repo_sensitive_literals.py" in text:
        payload = {"ok": repo_ok, "findings": [{"rule": "raw_openai_key"}] if not repo_ok else []}
        return subprocess.CompletedProcess(command, 0 if repo_ok else 1, json.dumps(payload), "")
    raise AssertionError(f"unexpected command: {command}")


def test_release_privacy_security_audit_passes_with_security_slice_clean():
    payload = audit.run_audit(strict=True, runner=_runner)

    assert payload["schema_version"] == "mnemos.release_privacy_security.v1"
    assert payload["release_privacy_security"]["ok"] is True
    assert payload["blocking_findings"] == []
    health = next(check for check in payload["checks"] if check["id"] == "health.security_privacy")
    assert health["status"] == "passed"
    check_ids = {check["id"] for check in payload["checks"]}
    assert "distill.status_redaction" in check_ids
    assert "e2e_probe.dry_run_redaction" in check_ids
    assert str(audit.ROOT) not in json.dumps(payload, ensure_ascii=False)


def test_release_privacy_security_blocks_unredacted_diagnostics():
    def runner(command, **kwargs):
        return _runner(
            command,
            health_stdout_suffix='\n{"leak": "https://api.real-service.test/v1"}',
            config_stdout_suffix='\n{"source": "env:OPENAI_API_KEY"}',
            **kwargs,
        )

    payload = audit.run_audit(strict=True, runner=runner)

    codes = {finding["code"] for finding in payload["blocking_findings"]}
    assert "diagnostic_real_api_url" in codes
    assert "diagnostic_unredacted_key_source" in codes


def test_release_privacy_security_blocks_unredacted_distill_and_e2e_paths():
    local_path = "/" + "Users/dev/.mnemos/distill_queue"

    def runner(command, **kwargs):
        return _runner(
            command,
            distill_stdout_suffix=f"\nqueue_dir: {local_path}",
            e2e_stdout_suffix=f"\nwiki_dir={local_path}",
            **kwargs,
        )

    payload = audit.run_audit(strict=True, runner=runner)

    findings = payload["blocking_findings"]
    assert any(
        finding["source"] == "distill.status_redaction"
        and finding["code"] == "diagnostic_local_path"
        and local_path not in json.dumps(finding, ensure_ascii=False)
        for finding in findings
    )
    assert any(
        finding["source"] == "e2e_probe.dry_run_redaction"
        and finding["code"] == "diagnostic_local_path"
        and local_path not in json.dumps(finding, ensure_ascii=False)
        for finding in findings
    )


def test_release_privacy_security_redacts_failed_diagnostic_command_error():
    local_path = "/" + "Users/dev/.mnemos/e2e"

    def runner(command, **kwargs):
        text = " ".join(command)
        if "e2e_probe.py --dry-run --no-api" in text:
            return subprocess.CompletedProcess(
                command,
                2,
                f"failed while reading {local_path}",
                "",
            )
        return _runner(command, **kwargs)

    payload = audit.run_audit(strict=True, runner=runner)

    check = next(
        check for check in payload["checks"] if check["id"] == "e2e_probe.dry_run_redaction"
    )
    assert local_path not in check["error"]
    assert "<HOME>" in check["error"]
    assert local_path not in json.dumps(payload, ensure_ascii=False)


def test_release_privacy_security_reports_docs_and_repo_findings():
    def runner(command, **kwargs):
        return _runner(command, docs_ok=False, repo_ok=False, **kwargs)

    payload = audit.run_audit(strict=True, runner=runner)

    codes = {finding["code"] for finding in payload["blocking_findings"]}
    assert "docs_sensitive_findings" in codes
    assert "repo_sensitive_literal_findings" in codes


def test_release_aggregator_rejects_security_report_blocking_finding_with_rc_zero():
    def runner(command, **kwargs):
        if "security_audit.py" in " ".join(command):
            payload = {
                "schema_version": "mnemos.security_audit.v2",
                "summary": {
                    "ok": True,
                    "status": "ok",
                    "blocking_count": 1,
                    "warning_count": 0,
                },
                "findings": [
                    {
                        "source": "health_security",
                        "code": "legacy_encrypted_credentials",
                        "severity": "blocking",
                        "message": "redacted",
                        "repair_action": "repair",
                    }
                ],
                "checks": {},
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        return _runner(command, **kwargs)

    payload = audit.run_audit(strict=True, runner=runner)

    assert payload["release_privacy_security"]["ok"] is False
    codes = {finding["code"] for finding in payload["blocking_findings"]}
    assert "security_report_invariant_failed" in codes


def test_release_aggregator_consumes_valid_security_blocking_findings():
    def runner(command, **kwargs):
        if "security_audit.py" in " ".join(command):
            payload = {
                "schema_version": "mnemos.security_audit.v2",
                "summary": {
                    "ok": False,
                    "status": "failed",
                    "blocking_count": 1,
                    "warning_count": 0,
                },
                "findings": [
                    {
                        "source": "health_security",
                        "code": "plaintext_api_key_pattern",
                        "severity": "blocking",
                        "message": "redacted",
                        "repair_action": "repair",
                    }
                ],
                "checks": {},
            }
            return subprocess.CompletedProcess(command, 1, json.dumps(payload), "")
        return _runner(command, **kwargs)

    payload = audit.run_audit(strict=True, runner=runner)

    assert payload["release_privacy_security"]["ok"] is False
    codes = {finding["code"] for finding in payload["blocking_findings"]}
    assert "security_audit_blocking_findings" in codes


def test_release_aggregator_preserves_security_warnings_without_false_block():
    def runner(command, **kwargs):
        if "security_audit.py" in " ".join(command):
            payload = {
                "schema_version": "mnemos.security_audit.v2",
                "summary": {
                    "ok": True,
                    "status": "warning",
                    "blocking_count": 0,
                    "warning_count": 1,
                },
                "findings": [
                    {
                        "source": "health_security",
                        "code": "health_security_warning",
                        "severity": "warning",
                        "message": "redacted",
                        "repair_action": "review",
                    }
                ],
                "checks": {},
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        return _runner(command, **kwargs)

    payload = audit.run_audit(strict=True, runner=runner)

    assert payload["release_privacy_security"]["ok"] is True
    assert payload["release_privacy_security"]["warning_count"] == 1
    assert payload["warning_findings"][0]["code"] == "security_audit_warning_findings"


def test_local_gates_include_release_privacy_security_audit():
    gate_commands = {name: cmd for name, cmd in run_local_gates.GATES}

    assert gate_commands["release privacy security audit"] == [
        "python",
        "scripts/audit_release_privacy_security.py",
        "--strict",
    ]
