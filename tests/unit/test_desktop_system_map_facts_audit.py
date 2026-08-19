import json

from scripts import audit_desktop_system_map_facts as audit
from scripts import run_local_gates


def _valid_payload(current_commit="abc123"):
    return {
        "generated_at": "2026-07-09T00:16:42+08:00",
        "git_commit": "old-historical-commit",
        "current_state": {
            "schema_version": audit.SCHEMA_VERSION,
            "generated_at": "2026-07-09T23:00:00+08:00",
            "repo_git_commit": current_commit,
            "historical_snapshot_notice": (
                "Historical scan entries are append-only snapshots; current-state evidence "
                "must be read from current_state."
            ),
            "commands": [
                {
                    "command": audit.QUICK_COMMAND,
                    "exit_code": 0,
                    "summary": "5367 passed, 15 subtests passed",
                    "tested_code_commit": current_commit,
                },
                {
                    "command": audit.LOCAL_GATES_COMMAND,
                    "exit_code": 0,
                    "summary": "All local gates PASSED.",
                },
            ],
        },
    }


def test_audit_accepts_current_state_even_when_historical_commit_is_old(tmp_path):
    findings = audit.audit_payload(
        _valid_payload(),
        current_commit="abc123",
        facts_path=tmp_path / "99-代码扫描-facts.json",
    )

    assert findings == []


def test_audit_flags_missing_current_state(tmp_path):
    findings = audit.audit_payload(
        {"git_commit": "old-historical-commit"},
        current_commit="abc123",
        facts_path=tmp_path / "99-代码扫描-facts.json",
    )

    assert [finding.rule for finding in findings] == ["missing_current_state"]


def test_audit_flags_stale_current_state_commit(tmp_path):
    findings = audit.audit_payload(
        _valid_payload(current_commit="old-commit"),
        current_commit="new-commit",
        facts_path=tmp_path / "99-代码扫描-facts.json",
    )

    assert [finding.rule for finding in findings] == ["stale_current_state_commit"]


def test_audit_requires_successful_quick(tmp_path):
    payload = _valid_payload()
    payload["current_state"]["commands"][0]["exit_code"] = 1
    payload["current_state"]["commands"][1]["summary"] = ""

    findings = audit.audit_payload(
        payload,
        current_commit="abc123",
        facts_path=tmp_path / "99-代码扫描-facts.json",
    )

    assert [finding.rule for finding in findings] == ["missing_successful_validation_command"]


def test_audit_rejects_quick_result_from_another_commit(tmp_path):
    payload = _valid_payload()
    payload["current_state"]["commands"][0]["tested_code_commit"] = "old-commit"

    findings = audit.audit_payload(
        payload,
        current_commit="abc123",
        facts_path=tmp_path / "99-代码扫描-facts.json",
    )

    assert [finding.rule for finding in findings] == ["stale_quick_validation_commit"]


def test_missing_facts_path_skips_unless_required(tmp_path):
    missing = tmp_path / "missing.json"

    skipped = audit.run_audit(facts_path=missing, repo_root=tmp_path, require_present=False)
    required = audit.run_audit(facts_path=missing, repo_root=tmp_path, require_present=True)

    assert skipped["ok"] is True
    assert skipped["skipped"] is True
    assert required["ok"] is False
    assert required["findings"][0]["rule"] == "missing_facts_path"


def test_main_json_reports_stale_commit(tmp_path, capsys, monkeypatch):
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps(_valid_payload(current_commit="old")), encoding="utf-8")
    monkeypatch.setattr(audit, "current_git_commit", lambda repo_root: "new")

    code = audit.main(["--facts-path", str(facts), "--repo-root", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["ok"] is False
    assert payload["by_rule"] == {"stale_current_state_commit": 1}


def test_local_gates_include_desktop_system_map_facts_audit():
    gate_commands = {name: cmd for name, cmd in run_local_gates.GATES}

    assert gate_commands["desktop system-map facts audit"] == [
        "python",
        "scripts/audit_desktop_system_map_facts.py",
    ]
