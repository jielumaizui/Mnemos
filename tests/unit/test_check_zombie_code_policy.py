"""Unit tests for scripts/check_zombie_code_policy.py."""

from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path

from scripts import check_zombie_code_policy as zcp


def _future_date() -> str:
    return (date.today() + timedelta(days=30)).isoformat()


def _baseline(entries):
    return {
        "schema_version": zcp.SCHEMA_VERSION,
        "entries": entries,
    }


def _entry(finding: zcp.ZombieFinding):
    return finding.to_baseline_entry(
        {
            "owner": "core",
            "reason": "Compatibility path is intentionally retained.",
            "callers": ["test caller"],
            "remove_when": "Remove after the caller migrates.",
            "expires_at": _future_date(),
            "telemetry": "python3 scripts/check_zombie_code_policy.py --closure --json",
        }
    )


def test_documented_legacy_symbol_passes(tmp_path: Path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def old_entry():\n"
        "    \"\"\"legacy compatibility wrapper.\"\"\"\n"
        "    return 1\n",
        encoding="utf-8",
    )

    findings = zcp.scan_project([Path("sample.py")], project_root=tmp_path)
    errors = zcp.check_baseline(findings, _baseline([_entry(findings[0])]))

    assert errors == []


def test_undocumented_legacy_symbol_fails(tmp_path: Path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def old_entry():\n"
        "    # 兼容旧调用路径\n"
        "    return 1\n",
        encoding="utf-8",
    )

    findings = zcp.scan_file(sample, project_root=tmp_path)
    errors = zcp.check_baseline(findings, _baseline([]))

    assert len(findings) == 1
    assert any("Undocumented zombie-code candidate" in error for error in errors)


def test_baseline_entry_requires_policy_metadata(tmp_path: Path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def old_entry():\n"
        "    \"\"\"已废弃，保留兼容。\"\"\"\n"
        "    return 1\n",
        encoding="utf-8",
    )

    finding = zcp.scan_file(sample, project_root=tmp_path)[0]
    entry = finding.to_baseline_entry(
        {
            "owner": "",
            "reason": "",
            "callers": [],
            "remove_when": "",
            "expires_at": "",
            "telemetry": "",
        }
    )
    errors = zcp.check_baseline([finding], _baseline([entry]))

    assert any("needs non-empty owner" in error for error in errors)
    assert any("needs non-empty reason" in error for error in errors)
    assert any("needs non-empty callers list" in error for error in errors)
    assert any("needs non-empty remove_when" in error for error in errors)
    assert any("needs non-empty expires_at" in error for error in errors)
    assert any("needs non-empty telemetry" in error for error in errors)


def test_baseline_entry_rejects_expired_plan(tmp_path: Path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def old_entry():\n"
        "    \"\"\"legacy compatibility wrapper.\"\"\"\n"
        "    return 1\n",
        encoding="utf-8",
    )

    finding = zcp.scan_file(sample, project_root=tmp_path)[0]
    entry = finding.to_baseline_entry(
        {
            "owner": "core",
            "reason": "Compatibility path is intentionally retained.",
            "callers": ["test caller"],
            "remove_when": "Remove after the caller migrates.",
            "expires_at": "2000-01-01",
            "telemetry": "python3 scripts/check_zombie_code_policy.py --closure --json",
        }
    )
    errors = zcp.check_baseline([finding], _baseline([entry]))

    assert any("expired on 2000-01-01" in error for error in errors)


def test_stale_baseline_entry_fails(tmp_path: Path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def current_entry():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    stale = zcp.ZombieFinding(
        path="sample.py",
        qualified_name="removed_entry",
        kind="function",
        line=1,
        markers=("legacy",),
    )

    findings = zcp.scan_file(sample, project_root=tmp_path)
    errors = zcp.check_baseline(findings, _baseline([_entry(stale)]))

    assert findings == []
    assert any("Stale zombie-code baseline entry" in error for error in errors)


def test_update_refuses_new_acceptance_without_explicit_flag(tmp_path: Path, monkeypatch):
    finding = zcp.ZombieFinding(
        path="core/new.py",
        qualified_name="legacy_entry",
        kind="function",
        line=1,
        markers=("legacy",),
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        '{"schema_version":"mnemos.zombie_code_baseline.v2","entries":[]}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(zcp, "scan_project", lambda _paths: [finding])

    assert zcp.main(["--update", "--baseline", str(baseline)]) == 1


def test_strict_release_failure_preserves_ratchet_and_acceptance_status(
    tmp_path: Path, monkeypatch, capsys
):
    finding = zcp.ZombieFinding(
        path="core/old.py",
        qualified_name="legacy_entry",
        kind="function",
        line=1,
        markers=("legacy",),
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(_baseline([_entry(finding)])),
        encoding="utf-8",
    )
    monkeypatch.setattr(zcp, "scan_project", lambda _paths: [finding])

    assert zcp.main(["--closure", "--strict", "--json", "--baseline", str(baseline)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ratchet_status"] == "passed"
    assert report["closure"]["status"] == "accepted_debt"
    assert report["closure"]["accepted_count"] == 1
    assert report["closure"]["unaccepted_count"] == 0
