"""Tests for the local security audit gate."""

from types import SimpleNamespace

from scripts import run_local_gates
from scripts import security_audit


def test_pip_audit_parses_current_dependencies_json_shape(monkeypatch):
    payload = {
        "dependencies": [
            {
                "name": "pypdf",
                "version": "5.9.0",
                "vulns": [
                    {
                        "id": "CVE-2025-55197",
                        "fix_versions": ["6.0.0"],
                        "aliases": ["GHSA-7hfw-26vp-jp8m"],
                        "description": "crafted PDF can exhaust RAM",
                    }
                ],
            },
            {"name": "requests", "version": "2.34.2", "vulns": []},
        ],
        "fixes": [],
    }

    monkeypatch.setattr(
        security_audit.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=security_audit.json.dumps(payload)),
    )

    result = security_audit._run_pip_audit()

    assert result["ok"] is False
    assert result["vulnerabilities"] == [
        {
            "package": "pypdf",
            "installed_version": "5.9.0",
            "vulnerability_id": "CVE-2025-55197",
            "description": "crafted PDF can exhaust RAM",
            "fix_versions": ["6.0.0"],
            "aliases": ["GHSA-7hfw-26vp-jp8m"],
        }
    ]
    assert result["errors"] == ["pip-audit pypdf 5.9.0 CVE-2025-55197 fix=6.0.0"]


def test_pip_audit_keeps_legacy_mapping_json_compatibility(monkeypatch):
    payload = {
        "pypdf": {
            "version": "5.9.0",
            "vulns": [
                {
                    "vulnerability_id": "PYSEC-legacy",
                    "fix_versions": ["6.0.0"],
                    "description": "legacy shape",
                }
            ],
        }
    }

    monkeypatch.setattr(
        security_audit.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=security_audit.json.dumps(payload)),
    )

    result = security_audit._run_pip_audit()

    assert result["ok"] is False
    assert result["vulnerabilities"][0]["package"] == "pypdf"
    assert result["vulnerabilities"][0]["vulnerability_id"] == "PYSEC-legacy"


def test_local_gates_run_security_audit_instead_of_bandit_only():
    gate_commands = {name: cmd for name, cmd in run_local_gates.GATES}

    assert gate_commands["security audit"] == ["python", "scripts/security_audit.py"]
    assert "bandit" not in gate_commands


def test_security_audit_prefers_repo_venv_for_subtools(monkeypatch, tmp_path):
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(stdout="{}")

    monkeypatch.setattr(security_audit, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(security_audit, "_module_available", lambda *args: True)
    monkeypatch.setattr(security_audit.subprocess, "run", fake_run)

    security_audit._run_bandit()
    security_audit._run_pip_audit()

    assert commands[0][0] == str(venv_python)
    assert commands[1][0] == str(venv_python)


def test_security_audit_strict_env_uses_current_python(monkeypatch, tmp_path):
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    monkeypatch.setattr(security_audit, "PROJECT_ROOT", tmp_path)

    assert security_audit._python_cmd(autodetect_venv=False) == security_audit.sys.executable


def test_security_audit_missing_dependency_returns_repair_hint(monkeypatch):
    monkeypatch.setattr(security_audit, "_module_available", lambda *args: False)

    result = security_audit._run_bandit("/tmp/missing-python")

    assert result["ok"] is False
    assert result["dependency_missing"] is True
    assert result["errors"] == [
        "bandit dependency missing for Python /tmp/missing-python "
        "(module bandit). Install dev dependencies with: "
        "uv pip install -r requirements-dev.txt; or run: "
        "/tmp/missing-python -m pip install -r requirements-dev.txt"
    ]


def test_security_audit_strict_bandit_fails_on_medium(monkeypatch):
    payload = {
        "results": [
            {
                "issue_severity": "MEDIUM",
                "issue_confidence": "HIGH",
                "test_id": "B608",
                "filename": "core/example.py",
                "line_number": 10,
                "issue_text": "Possible SQL injection vector.",
            }
        ]
    }
    monkeypatch.setattr(security_audit, "_module_available", lambda *args: True)
    monkeypatch.setattr(
        security_audit.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=security_audit.json.dumps(payload),
            returncode=1,
            stderr="",
        ),
    )

    permissive = security_audit._run_bandit("/tmp/python", fail_on_medium=False)
    strict = security_audit._run_bandit("/tmp/python", fail_on_medium=True)

    assert permissive["ok"] is True
    assert permissive["medium"] == 1
    assert strict["ok"] is False
    assert strict["errors"] == ["bandit strict medium issues: 1"]


def test_health_security_uses_selected_python(monkeypatch):
    commands: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        commands.append(cmd)
        payload = {"status": "ok", "legacy_key_rows": {"enc_rows": 0}}
        return SimpleNamespace(stdout=security_audit.json.dumps(payload), returncode=0)

    monkeypatch.setattr(security_audit.subprocess, "run", fake_run)

    result = security_audit._run_health_security("/tmp/venv-python")

    assert result["ok"] is True
    assert commands[0][0] == "/tmp/venv-python"


def test_health_security_fails_on_plaintext_secret_inventory(monkeypatch):
    payload = {
        "status": "warning",
        "legacy_key_rows": {"enc_rows": 0},
        "secret_inventory": {
            "plaintext_count": 1,
            "findings": [{"path": "memos.token", "status": "plaintext-risk"}],
        },
    }
    monkeypatch.setattr(
        security_audit.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=security_audit.json.dumps(payload),
            returncode=0,
        ),
    )

    result = security_audit._run_health_security("/tmp/venv-python")

    assert result["ok"] is False
    assert result["errors"] == ["plaintext secret-like config values found: 1"]


def _health_security_result(monkeypatch, payload):
    monkeypatch.setattr(
        security_audit.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=security_audit.json.dumps(payload),
            returncode=0,
            stderr="",
        ),
    )
    return security_audit._run_health_security("/tmp/venv-python")


def test_health_security_legacy_encrypted_rows_are_blocking(monkeypatch):
    result = _health_security_result(
        monkeypatch,
        {"status": "ok", "legacy_key_rows": {"enc_rows": 1}},
    )

    assert result["ok"] is False
    assert result["blocking_count"] == 1
    assert result["errors"] == ["legacy XOR-encrypted credential rows found"]
    assert result["findings"][0]["code"] == "legacy_encrypted_credentials"


def test_health_security_plaintext_api_key_patterns_are_blocking(monkeypatch):
    result = _health_security_result(
        monkeypatch,
        {"status": "ok", "plaintext_api_key_risks": [{"path": "redacted"}]},
    )

    assert result["ok"] is False
    assert result["blocking_count"] == 1
    assert result["findings"][0]["code"] == "plaintext_api_key_pattern"


def test_health_security_degraded_or_unknown_status_is_blocking(monkeypatch):
    degraded = _health_security_result(monkeypatch, {"status": "degraded"})
    unknown = _health_security_result(monkeypatch, {})

    assert degraded["ok"] is False
    assert unknown["ok"] is False
    assert degraded["findings"][0]["code"] == "health_security_status"
    assert unknown["findings"][0]["code"] == "health_security_status"


def test_health_security_warning_is_visible_but_not_blocking(monkeypatch):
    result = _health_security_result(monkeypatch, {"status": "warning"})

    assert result["ok"] is True
    assert result["blocking_count"] == 0
    assert result["warning_count"] == 1
    assert result["status"] == "warning"


def test_security_report_derives_ok_from_findings_not_component_boole():
    report = security_audit.build_security_report(
        {"ok": True, "errors": ["bandit contradiction"]},
        {"ok": True, "errors": []},
        {"ok": True, "errors": [], "findings": []},
    )

    assert report["schema_version"] == "mnemos.security_audit.v2"
    assert report["summary"]["ok"] is False
    assert report["summary"]["blocking_count"] == 1
    assert report["findings"][0]["message"] == "bandit contradiction"
    assert security_audit.validate_security_report(report, returncode=1) == []

    report["summary"]["ok"] = True
    assert "ok_invariant" in security_audit.validate_security_report(
        report, returncode=0
    )


def test_security_report_validator_rejects_malformed_finding():
    report = {
        "schema_version": "mnemos.security_audit.v2",
        "summary": {
            "ok": True,
            "status": "ok",
            "blocking_count": 0,
            "warning_count": 0,
        },
        "findings": [{"severity": "unknown"}],
        "checks": {},
    }

    errors = security_audit.validate_security_report(report, returncode=0)

    assert "finding_structure" in errors


def test_security_audit_json_exit_is_derived_from_blocking_findings(
    monkeypatch, capsys
):
    monkeypatch.setattr(security_audit, "_python_cmd", lambda **_kwargs: "/tmp/python")
    monkeypatch.setattr(
        security_audit,
        "_run_bandit",
        lambda *_args, **_kwargs: {"ok": True, "errors": [], "high": 0, "medium": 0, "low": 0},
    )
    monkeypatch.setattr(
        security_audit,
        "_run_pip_audit",
        lambda *_args, **_kwargs: {"ok": True, "errors": [], "vulnerabilities": []},
    )
    monkeypatch.setattr(
        security_audit,
        "_run_health_security",
        lambda *_args, **_kwargs: {
            "ok": False,
            "errors": ["legacy XOR-encrypted credential rows found"],
            "findings": [
                {
                    "source": "health_security",
                    "code": "legacy_encrypted_credentials",
                    "severity": "blocking",
                    "message": "legacy XOR-encrypted credential rows found",
                    "repair_action": "repair",
                }
            ],
        },
    )

    assert security_audit.main(["--strict", "--json"]) == 1
    payload = security_audit.json.loads(capsys.readouterr().out)
    assert payload["summary"]["ok"] is False
    assert payload["summary"]["blocking_count"] == 1
