"""Tests for scripts.run_tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def test_quick_layer_excludes_packaging_and_heavy_paths():
    from scripts.run_tests import build_pytest_command

    cmd = build_pytest_command("quick", [])

    assert "tests/unit" in cmd
    assert "--ignore=tests/unit/test_packaging_contract.py" in cmd
    assert "tests/integration" not in cmd
    assert "tests/benchmark" not in cmd
    assert "tests/e2e" not in cmd


def test_heavy_layer_collects_packaging_benchmark_and_e2e():
    from scripts.run_tests import build_pytest_command

    cmd = build_pytest_command("heavy", [])

    assert "tests/unit/test_packaging_contract.py" in cmd
    assert "tests/benchmark" in cmd
    assert "tests/e2e" in cmd


def test_extra_pytest_args_are_appended_after_layer_selection():
    from scripts.run_tests import build_pytest_command

    cmd = build_pytest_command("integration", ["-x", "--tb=short"])

    assert cmd[-2:] == ["-x", "--tb=short"]


def test_runner_rejects_pytest_options_that_override_hermetic_harness():
    from scripts.run_tests import build_pytest_command

    for arguments in (
        ["--basetemp", "/tmp/escape"],
        ["--basetemp=/tmp/escape"],
        ["--baset", "/tmp/escape"],
        ["--noconftest"],
        ["--noconf"],
        ["--confcutdir=/tmp"],
        ["--confcutd=/tmp"],
        ["--rootdir", "/tmp"],
        ["--rootd", "/tmp"],
        ["-c", "/tmp/pytest.ini"],
        ["-c/tmp/pytest.ini"],
    ):
        try:
            build_pytest_command("quick", arguments)
        except ValueError as exc:
            assert "cannot override the hermetic harness" in str(exc)
        else:
            raise AssertionError(f"unsafe pytest option was accepted: {arguments}")


def test_system_layer_runs_only_system_tests_through_hermetic_runner():
    from scripts.run_tests import build_pytest_command

    cmd = build_pytest_command("system", [])

    assert cmd[2:] == ["pytest", "tests/test_system.py", "-v"]


def test_ci_system_tests_use_cross_platform_hermetic_runner():
    workflow = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "python scripts/run_tests.py system" in workflow
    assert "MNEMOS_DIR=$(" not in workflow
    assert "mktemp" not in workflow


def test_system_layer_executes_without_a_shell_and_keeps_hermetic_environment(
    monkeypatch,
):
    from scripts import run_tests as layered_runner

    calls = []
    monkeypatch.delenv("MNEMOS_TEST_RUN", raising=False)

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(layered_runner.subprocess, "run", fake_run)

    assert layered_runner.main(["system"]) == 0
    command, kwargs = calls[0]
    assert command[2:] == ["pytest", "tests/test_system.py", "-v"]
    assert "shell" not in kwargs
    assert kwargs["env"]["MNEMOS_TEST_RUN"] == "1"
    assert kwargs["env"]["PYTEST_ADDOPTS"] == ""
    assert kwargs["env"]["MNEMOS_RUN_ENVIRONMENT_HASH"]
    assert kwargs["env"]["MNEMOS_DIR"].startswith(kwargs["env"]["MNEMOS_RUN_ROOT"])


def test_root_runner_uses_layered_quick_entrypoint(monkeypatch):
    import run_tests
    from scripts import run_tests as layered_runner

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(layered_runner.subprocess, "run", fake_run)

    assert run_tests.main(["quick", "--", "-x"]) == 0
    assert calls
    command, kwargs = calls[0]
    assert "tests/unit" in command
    assert "--ignore=tests/unit/test_packaging_contract.py" in command
    assert command[-1] == "-x"
    assert kwargs["env"]["MNEMOS_RUN_ENVIRONMENT_HASH"]
    assert kwargs["env"]["MNEMOS_DIR"].startswith(kwargs["env"]["MNEMOS_RUN_ROOT"])
