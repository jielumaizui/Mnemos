from __future__ import annotations

import json
from pathlib import Path

from scripts import audit_repo_sensitive_literals as audit
from scripts import run_local_gates


def _home_path() -> str:
    return "/" + "Users/alice/project"


def test_audit_flags_provider_shaped_tokens_and_home_paths(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text(
        "\n".join(
            [
                "openai = '" + ("sk" + "-") + ("a" * 20) + "'",
                "anthropic = '" + ("sk" + "-ant-") + ("b" * 20) + "'",
                "google = '" + ("AI" + "za") + ("c" * 20) + "'",
                "github = '" + ("ghp" + "_") + ("d" * 20) + "'",
                "pat = '" + ("github" + "_pat_") + ("e" * 20) + "'",
                "slack = '" + ("xoxb" + "-") + "12345678'",
                "hf = '" + ("hf" + "_") + ("f" * 20) + "'",
                "Generated from " + _home_path(),
            ]
        ),
        encoding="utf-8",
    )

    findings = audit.audit_paths([target])

    assert [finding.rule for finding in findings] == [
        "raw_openai_key",
        "raw_anthropic_key",
        "raw_google_key",
        "raw_github_token",
        "raw_github_pat",
        "raw_slack_token",
        "raw_huggingface_token",
        "local_home_path",
    ]


def test_audit_flags_plain_credential_assignment(tmp_path):
    target = tmp_path / "sample.md"
    target.write_text(
        "api_" + 'key: "super-secret-value"\n'
        + "pass" + "word=hunter2\n",
        encoding="utf-8",
    )

    findings = audit.audit_paths([target])

    assert [finding.rule for finding in findings] == [
        "credential_field_value",
        "credential_field_value",
    ]


def test_audit_allows_placeholders_and_words_containing_sk_dash(tmp_path):
    target = tmp_path / "safe.md"
    target.write_text(
        "\n".join(
            [
                "api_key: DUMMY_CREDENTIAL_VALUE_FOR_REDACTION_TEST",
                "token: <TOKEN>",
                "secret: ****",
                "key_source: env:MNEMOS_LLM_API_KEY",
                "task-specific context",
                "task-missing-raw",
                "<HOME>/project",
            ]
        ),
        encoding="utf-8",
    )

    assert audit.audit_paths([target]) == []


def test_main_strict_json_reports_findings(tmp_path, capsys):
    target = tmp_path / "sample.py"
    target.write_text("path = '" + _home_path() + "'\n", encoding="utf-8")

    code = audit.main(["--strict", "--json", str(target)])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["ok"] is False
    assert payload["by_rule"] == {"local_home_path": 1}


def test_iter_audit_files_uses_git_repo_files(monkeypatch):
    tracked = b"README.md\0sub/file.py\0"

    class Result:
        stdout = tracked

    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr(audit.subprocess, "run", fake_run)

    files = list(audit.iter_audit_files())

    assert files == [Path(audit.REPO_ROOT / "README.md"), Path(audit.REPO_ROOT / "sub/file.py")]
    assert calls[0][0][0] == [
        "git",
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    ]


def test_local_gates_include_repo_sensitive_literals_audit():
    gate_commands = {name: cmd for name, cmd in run_local_gates.GATES}

    assert gate_commands["repo sensitive literal audit"] == [
        "python",
        "scripts/audit_repo_sensitive_literals.py",
        "--strict",
    ]
