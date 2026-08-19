import json

from scripts import audit_docs_freshness as audit
from scripts import run_local_gates


def test_audit_flags_bare_python_script_command(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text("```bash\npython scripts/run_local_gates.py\n```\n", encoding="utf-8")

    findings = audit.audit_paths([doc])

    assert len(findings) == 1
    assert findings[0].rule == "bare_python_scripts"


def test_audit_flags_bare_python_module_and_inline_commands(tmp_path):
    doc = tmp_path / "SECURITY.md"
    doc.write_text(
        "\n".join(
            [
                "python -m bandit -r core",
                "python -c \"print('secret')\"",
            ]
        ),
        encoding="utf-8",
    )

    findings = audit.audit_paths([doc])

    assert [finding.rule for finding in findings] == [
        "bare_python_module",
        "bare_python_inline",
    ]


def test_audit_allows_python3_and_repo_venv_commands(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text(
        "\n".join(
            [
                "python3 scripts/run_local_gates.py",
                ".venv/bin/python -m pytest -q",
                "python3 -c \"print('ok')\"",
            ]
        ),
        encoding="utf-8",
    )

    assert audit.audit_paths([doc]) == []


def test_audit_flags_machine_local_paths(tmp_path):
    doc = tmp_path / "report.md"
    doc.write_text(
        "\n".join(
            [
                "Generated on " + "/" + "Users/zhuwei/mnemos.",
                "Generated on " + "/" + "home/dev/mnemos.",
                "Generated on " + "C:" + r"\\Users\\dev\\mnemos.",
            ]
        ),
        encoding="utf-8",
    )

    findings = audit.audit_paths([doc])

    assert [finding.rule for finding in findings] == [
        "machine_local_path",
        "machine_local_path",
        "machine_local_path",
    ]


def test_audit_flags_retired_terms(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text(
        "\n".join(
            [
                "Memos is not required.",
                "Use daemon.services.event_bus for old config.",
                "The core.dark_knowledge module is gone.",
                "Do not run --mode entangle.",
            ]
        ),
        encoding="utf-8",
    )

    findings = audit.audit_paths([doc])

    assert [finding.rule for finding in findings] == [
        "legacy_memos_name",
        "legacy_daemon_service_key",
        "legacy_removed_module",
        "legacy_removed_mode",
    ]


def test_default_paths_include_formal_docs_and_desktop_system_map(tmp_path, monkeypatch):
    desktop_map = tmp_path / "Desktop" / "mnemos系统图谱"
    desktop_map.mkdir(parents=True)
    monkeypatch.setattr(audit, "discover_desktop_system_map", lambda: (desktop_map,))

    paths = audit.default_paths()

    assert audit.REPO_ROOT / "AGENTS.md" in paths
    assert audit.REPO_ROOT / "CLAUDE.md" in paths
    assert audit.REPO_ROOT / "CONTRIBUTING.md" in paths
    assert audit.REPO_ROOT / "docs" / "AGENT_GUIDE.md" in paths
    assert desktop_map in paths


def test_default_paths_use_canonical_tracked_markdown_discovery(tmp_path, monkeypatch):
    extra = audit.REPO_ROOT / "AGENT_BEHAVIOR_POLICY.md"
    monkeypatch.setattr(audit, "discover_reviewed_repo_markdown", lambda: [extra])
    monkeypatch.setattr(audit, "discover_desktop_system_map", lambda: ())

    assert audit.default_paths() == [extra]


def test_audit_flags_missing_repo_paths_in_shell_fences(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text(
        "```bash\npython3 scripts/not_a_real_script.py --help\n```\n",
        encoding="utf-8",
    )

    findings = audit.audit_paths([doc])

    assert len(findings) == 1
    assert findings[0].rule == "doc_command_missing_path"
    assert "scripts/not_a_real_script.py" in findings[0].text


def test_audit_accepts_existing_repo_paths_in_shell_fences(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text(
        "\n".join(
            [
                "```bash",
                "python3 scripts/run_local_gates.py",
                "python3 -m pytest tests/unit/test_docs_freshness_audit.py -q",
                "cp config/config.example.json ~/.mnemos/configs/main.json",
                "```",
            ]
        ),
        encoding="utf-8",
    )

    assert audit.audit_paths([doc]) == []


def test_audit_flags_config_set_keys_missing_from_example(tmp_path):
    doc = tmp_path / "SECURITY.md"
    doc.write_text(
        "```bash\n"
        "mnemos config set llm.providers.anthropic.api_key_source "
        '"env:MNEMOS_ANTHROPIC_API_KEY"\n'
        "```\n",
        encoding="utf-8",
    )

    findings = audit.audit_paths([doc])

    assert len(findings) == 1
    assert findings[0].rule == "config_key_not_in_example"


def test_audit_accepts_config_set_keys_from_example(tmp_path):
    doc = tmp_path / "SECURITY.md"
    doc.write_text(
        "```bash\n"
        "mnemos config set storage.disk_budget.raw_events_max_mb 4096\n"
        "```\n",
        encoding="utf-8",
    )

    assert audit.audit_paths([doc]) == []


def test_main_strict_json_reports_findings(tmp_path, capsys):
    doc = tmp_path / "README.md"
    doc.write_text("python scripts/run_tests.py quick\n", encoding="utf-8")

    code = audit.main(["--strict", "--json", str(doc)])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["ok"] is False
    assert payload["by_rule"] == {"bare_python_scripts": 1}


def test_main_paths_option_overrides_defaults(tmp_path, capsys, monkeypatch):
    clean_doc = tmp_path / "clean.md"
    clean_doc.write_text("python3 scripts/run_local_gates.py\n", encoding="utf-8")
    stale_doc = tmp_path / "stale.md"
    stale_doc.write_text("python scripts/run_tests.py quick\n", encoding="utf-8")
    monkeypatch.setattr(audit, "default_paths", lambda include_desktop_system_map=True: [stale_doc])

    code = audit.main(["--strict", "--json", "--paths", str(clean_doc)])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["scanned_files"] == 1


def test_local_gates_include_docs_freshness_audit():
    gate_commands = {name: cmd for name, cmd in run_local_gates.GATES}

    assert gate_commands["config examples"] == [
        "python",
        "scripts/verify_config_examples.py",
        "--strict",
    ]
    assert gate_commands["config registry closure"] == [
        "python",
        "scripts/audit_config_registry_closure.py",
        "--strict",
    ]
    assert gate_commands["docs freshness audit"] == [
        "python",
        "scripts/audit_docs_freshness.py",
        "--strict",
    ]


def test_current_markdown_docs_are_fresh():
    assert audit.audit_paths(audit.default_paths()) == []
