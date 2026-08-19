from pathlib import Path
from types import SimpleNamespace

import json

from scripts import audit_gate_hermeticity as audit


def test_strict_suite_denominator_covers_all_release_test_layers():
    assert audit.STRICT_SUITES == ("quick", "integration", "heavy", "full-score")


def test_diagnostics_plan_includes_all_read_only_entrypoints(tmp_path: Path):
    plan = audit.build_command_plan(("diagnostics",), output_dir=tmp_path)

    assert [item.command_id for item in plan] == [
        "health",
        "verify",
        "status",
        "distill-status",
        "golden",
    ]
    golden = plan[-1]
    assert "--output-dir" not in golden.argv


def test_audit_owns_reports_and_logs_under_sandbox_root(monkeypatch, tmp_path):
    output_dir = tmp_path / "audit"
    monkeypatch.setattr(audit, "build_command_plan", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(audit, "_git_status", lambda: "")

    assert audit.main(["--suite", "diagnostics", "--output-dir", str(output_dir)]) == 0

    report = json.loads((output_dir / "gate_hermeticity.json").read_text(encoding="utf-8"))
    assert report["sandbox_root"] == str(output_dir)
    assert report["per_gate_environment_count"] == 0
    assert report["unique_sandbox_count"] == 0
    assert report["outside_write_count"] == 0


def test_each_planned_gate_uses_a_distinct_execution_environment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "audit"
    commands = [
        audit.AuditCommand("one", ("python3", "one.py")),
        audit.AuditCommand("two", ("python3", "two.py")),
    ]
    created: list[str] = []

    class _FakeGateEnvironment:
        def __init__(self, *, gate_id: str, **_kwargs):
            created.append(gate_id)
            self.gate_id = gate_id
            self.run = SimpleNamespace(
                environment={"MNEMOS_RUN_ARTIFACTS_DIR": str(tmp_path / gate_id)}
            )

        def execute(self, _selector):
            root = tmp_path / f"sandbox-{self.gate_id}"
            root.mkdir()
            stdout = root / "stdout"
            stderr = root / "stderr"
            stdout.write_text("", encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            return SimpleNamespace(
                sandbox_root=str(root),
                environment_hash=f"hash-{self.gate_id}",
                os_write_guard="sandbox-exec-v1",
                formal_state_diff=(),
                returncode=0,
                stdout_path=str(stdout),
                stderr_path=str(stderr),
            )

    monkeypatch.setattr(audit, "build_command_plan", lambda *_args, **_kwargs: commands)
    monkeypatch.setattr(audit, "GateExecutionEnvironment", _FakeGateEnvironment)
    monkeypatch.setattr(audit, "_git_status", lambda: "")

    assert audit.main(["--suite", "diagnostics", "--output-dir", str(output_dir)]) == 0

    report = json.loads((output_dir / "gate_hermeticity.json").read_text(encoding="utf-8"))
    assert created == ["one", "two"]
    assert report["per_gate_environment_count"] == 2
    assert report["unique_sandbox_count"] == 2
