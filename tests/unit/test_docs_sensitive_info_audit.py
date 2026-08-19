import json

from scripts import audit_docs_sensitive_info as audit
from scripts import run_local_gates


def test_audit_flags_high_confidence_tokens(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text(
        "\n".join(
            [
                "OpenAI key: sk-" + "a" * 20,
                "Anthropic key: " + "sk" + "-ant-" + "b" * 20,
                "GitHub to" + "ken: " + "ghp" + "_" + "c" * 20,
                "JWT: eyJ" + "d" * 10 + "." + "e" * 12 + "." + "f" * 12,
            ]
        ),
        encoding="utf-8",
    )

    findings = audit.audit_paths([doc])

    assert [finding.rule for finding in findings] == [
        "raw_openai_key",
        "raw_anthropic_key",
        "raw_github_token",
        "jwt_like",
    ]


def test_audit_flags_local_paths_and_personal_values(tmp_path):
    doc = tmp_path / "runbook.md"
    doc.write_text(
        "\n".join(
            [
                "Generated from " + "/" + "Users/alice/mnemos.",
                "owner email: alice@corp.test",
                "手机号: 13800138000",
                "身份证: 110105199001011234",
                "姓名: 张三",
            ]
        ),
        encoding="utf-8",
    )

    findings = audit.audit_paths([doc])

    assert [finding.rule for finding in findings] == [
        "local_home_path",
        "email_address",
        "phone_cn",
        "id_cn",
        "pii_assignment",
    ]


def test_audit_flags_real_api_urls_in_endpoint_context(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text(
        "\n".join(
            [
                '"base_url": "https://api.openai.com/v1"',
                "Docs link: https://docs.python.org/3/library/json.html",
            ]
        ),
        encoding="utf-8",
    )

    findings = audit.audit_paths([doc])

    assert len(findings) == 1
    assert findings[0].rule == "real_api_url"


def test_audit_flags_plain_credential_assignments(tmp_path):
    doc = tmp_path / "SECURITY.md"
    doc.write_text(
        "\n".join(
            [
                "api_" + 'key: "super-secret-value"',
                "Authorization: Bearer abcdefghijk",
                "PASSWORD" + "=actual-password",
            ]
        ),
        encoding="utf-8",
    )

    findings = audit.audit_paths([doc])

    assert [finding.rule for finding in findings] == [
        "credential_field_value",
        "credential_field_value",
        "credential_field_value",
    ]


def test_audit_allows_safe_placeholders_and_non_api_urls(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text(
        "\n".join(
            [
                'api_key: ""',
                'api_key_source: "env:MNEMOS_LLM_API_KEY"',
                'key_source: "keyring:mnemos/db"',
                "export MNEMOS_LLM_API_KEY=your_llm_key",
                '"base_url": "https://api.example.invalid/v1"',
                "See https://docs.python.org/3/library/json.html",
                "Contact security@example.invalid",
                "姓名: 某用户",
            ]
        ),
        encoding="utf-8",
    )

    assert audit.audit_paths([doc]) == []


def test_main_strict_json_reports_findings(tmp_path, capsys):
    doc = tmp_path / "README.md"
    doc.write_text('"base_url": "https://api.openai.com/v1"\n', encoding="utf-8")

    code = audit.main(["--strict", "--json", str(doc)])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["ok"] is False
    assert payload["by_rule"] == {"real_api_url": 1}


def test_local_gates_include_docs_sensitive_info_audit():
    gate_commands = {name: cmd for name, cmd in run_local_gates.GATES}

    assert gate_commands["docs sensitive info audit"] == [
        "python",
        "scripts/audit_docs_sensitive_info.py",
        "--strict",
    ]


def test_current_markdown_docs_have_no_sensitive_values():
    assert audit.audit_paths(audit.default_paths()) == []


def test_default_paths_use_canonical_tracked_markdown_discovery(monkeypatch):
    extra = audit.REPO_ROOT / "AGENT_BEHAVIOR_POLICY.md"
    monkeypatch.setattr(audit, "discover_reviewed_repo_markdown", lambda: [extra])
    monkeypatch.setattr(audit, "discover_desktop_system_map", lambda: [])

    assert audit.default_paths() == [extra]
