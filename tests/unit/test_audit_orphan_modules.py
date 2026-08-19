"""Tests for scripts/audit_orphan_modules.py CLI write policy."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import audit_orphan_modules as audit_orphans  # noqa: E402


def _make_project(root: Path) -> None:
    (root / "core").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "core" / "__init__.py").write_text("", encoding="utf-8")
    (root / "core" / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    (root / "scripts" / "entry.py").write_text("from core import service\n", encoding="utf-8")


def test_default_prints_stdout_without_writing_repo_report(tmp_path, capsys):
    _make_project(tmp_path)

    assert audit_orphans.main(["--root", str(tmp_path)]) == 0

    captured = capsys.readouterr()
    assert "# Orphan / Unconnected Module Audit" in captured.out
    assert "on `<repo>`" in captured.out
    assert str(tmp_path) not in captured.out
    assert not (tmp_path / "docs" / "orphan-modules-report.md").exists()


def test_dead_module_paths_are_repo_relative(tmp_path, capsys):
    _make_project(tmp_path)
    (tmp_path / "core" / "dead.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert audit_orphans.main(["--root", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "`core.dead` → `core/dead.py`" in output
    assert str(tmp_path) not in output


def test_check_compares_default_report_without_writing(tmp_path, capsys):
    _make_project(tmp_path)
    report_path = tmp_path / "docs" / "orphan-modules-report.md"
    report_path.parent.mkdir()
    report_path.write_text("stale report\n", encoding="utf-8")

    assert audit_orphans.main(["--root", str(tmp_path), "--check"]) == 1

    captured = capsys.readouterr()
    assert "Report out of date" in captured.err
    assert report_path.read_text(encoding="utf-8") == "stale report\n"


def test_output_outside_repo_writes_without_apply(tmp_path):
    _make_project(tmp_path)
    output = tmp_path.parent / f"{tmp_path.name}-orphan-report.md"

    try:
        assert audit_orphans.main(["--root", str(tmp_path), "--output", str(output)]) == 0
        assert output.read_text(encoding="utf-8").startswith("# Orphan / Unconnected Module Audit")
    finally:
        output.unlink(missing_ok=True)


def test_repo_output_requires_apply(tmp_path, capsys):
    _make_project(tmp_path)
    output = tmp_path / "docs" / "orphan-modules-report.md"

    assert audit_orphans.main(["--root", str(tmp_path), "--output", str(output)]) == 2

    captured = capsys.readouterr()
    assert "Refusing to write inside the repository without --apply" in captured.err
    assert not output.exists()


def test_repo_output_with_apply_records_action_ledger(tmp_path):
    _make_project(tmp_path)
    output = tmp_path / "docs" / "orphan-modules-report.md"
    ledger = tmp_path / "action_ledger.db"

    assert audit_orphans.main(
        [
            "--root",
            str(tmp_path),
            "--output",
            str(output),
            "--apply",
            "--action-ledger",
            str(ledger),
        ]
    ) == 0

    assert output.exists()
    with sqlite3.connect(str(ledger)) as conn:
        row = conn.execute(
            "SELECT actor, action_type, target, status FROM action_ledger"
        ).fetchone()
    assert row == (
        "scripts.audit_orphan_modules",
        "quality_gate",
        "repo_report:docs/orphan-modules-report.md",
        "produced",
    )
