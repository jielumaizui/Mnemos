import json

from scripts import audit_docs_stale_service_keys as audit


def test_audit_flags_stale_daemon_service_key_in_json_block(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text(
        "\n".join(
            [
                "```json",
                "{",
                '  "daemon": {',
                '    "services": {',
                '      "event_bus": true',
                "    }",
                "  }",
                "}",
                "```",
            ]
        ),
        encoding="utf-8",
    )

    findings = audit.audit_paths([doc])

    assert len(findings) == 1
    assert findings[0].key == "event_bus"
    assert findings[0].context == "daemon.services block"


def test_audit_flags_dotted_stale_key_in_live_prose(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text(
        "Set `daemon.services.event_bus=true` to enable the daemon event bus.\n",
        encoding="utf-8",
    )

    findings = audit.audit_paths([doc])

    assert len(findings) == 1
    assert findings[0].key == "event_bus"
    assert findings[0].context == "prose"


def test_audit_allows_migration_prose(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text(
        "迁移会清理旧 key `daemon.services.event_bus`，请改用 `eventbus`。\n",
        encoding="utf-8",
    )

    assert audit.audit_paths([doc]) == []


def test_audit_allows_event_bus_runtime_domain_outside_daemon_services(tmp_path):
    doc = tmp_path / "README.md"
    doc.write_text(
        "\n".join(
            [
                "```json",
                "{",
                '  "event_bus": {',
                '    "max_chain_depth": 10',
                "  }",
                "}",
                "```",
            ]
        ),
        encoding="utf-8",
    )

    assert audit.audit_paths([doc]) == []


def test_main_json_reports_findings(tmp_path, capsys):
    doc = tmp_path / "README.md"
    doc.write_text(
        "```toml\n[daemon.services]\nevent_bus = true\n```\n",
        encoding="utf-8",
    )

    code = audit.main(["--json", str(doc)])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["ok"] is False
    assert payload["findings"][0]["key"] == "event_bus"


def test_current_public_docs_have_no_live_stale_daemon_service_examples():
    findings = audit.audit_paths(
        [
            audit.REPO_ROOT / "README.md",
            audit.REPO_ROOT / "README-en.md",
            audit.REPO_ROOT / "docs",
        ]
    )

    assert findings == []
