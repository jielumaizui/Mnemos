from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path

import pytest

from core.ops.gate_execution import (
    ExecutableMutationSpec,
    GateExecutionEnvironment,
    GateRunnerSelector,
    execute_mutation,
)

requires_macos_sandbox = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="strict per-gate OS write denial requires macOS sandbox-exec",
)


def _write_candidate(root: Path, name: str, source: str) -> tuple[str, str]:
    relative = f"tests/.gate-candidates/{name}.py"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return relative, hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(root: Path, name: str, payload: object) -> tuple[str, str]:
    relative = f"tests/.gate-candidates/{name}.json"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return relative, hashlib.sha256(path.read_bytes()).hexdigest()


def _mutation_spec(
    root: Path,
    *,
    operator: str,
    candidate_sha256: str | None = None,
) -> ExecutableMutationSpec:
    baseline, baseline_sha256 = _write_json(
        root,
        "baseline",
        {"gates": [{"gate_id": "required"}, {"gate_id": "other"}]},
    )
    candidate, actual_candidate_sha256 = _write_json(
        root,
        "candidate",
        {"gates": [{"gate_id": "other"}]},
    )
    oracle, oracle_sha256 = _write_candidate(
        root,
        "gate_oracle",
        "import json, sys\n"
        "payload=json.load(open(sys.argv[1], encoding='utf-8'))\n"
        "raise SystemExit(0 if any(g.get('gate_id') == 'required' "
        "for g in payload['gates']) else 1)\n",
    )
    return ExecutableMutationSpec(
        mutation_id=operator,
        operator=operator,
        baseline_path=baseline,
        baseline_sha256=baseline_sha256,
        candidate_path=candidate,
        candidate_sha256=candidate_sha256 or actual_candidate_sha256,
        collection_path=("gates",),
        identity_field="gate_id",
        removed_identity="required",
        selector=GateRunnerSelector("python", oracle),
        selector_sha256=oracle_sha256,
        killed_returncodes=(1,),
    )


@requires_macos_sandbox
def test_each_gate_gets_a_unique_empty_hermetic_environment(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    relative, _ = _write_candidate(
        root,
        "unique_environment",
        "from pathlib import Path\n"
        "import os\n"
        "p=Path(os.environ['MNEMOS_DIR'])/'seed.db'\n"
        "assert not p.exists()\n"
        "p.write_text('owned')\n",
    )
    selector = GateRunnerSelector("python", relative)

    first = GateExecutionEnvironment(
        base_root=tmp_path / "runs",
        repo_root=root,
        gate_id="first",
    ).execute(selector)
    second = GateExecutionEnvironment(
        base_root=tmp_path / "runs",
        repo_root=root,
        gate_id="second",
    ).execute(selector)

    assert first.ok is True
    assert second.ok is True
    assert first.sandbox_root != second.sandbox_root
    assert first.environment_hash != second.environment_hash


@requires_macos_sandbox
def test_desktop_and_unknown_database_write_is_killed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "formal-desktop" / "unknown.db-wal"
    outside.parent.mkdir()
    relative, _ = _write_candidate(
        root,
        "outside_write",
        f"from pathlib import Path\nPath({str(outside)!r}).write_text('mutation')\n",
    )

    result = GateExecutionEnvironment(
        base_root=tmp_path / "runs",
        repo_root=root,
        gate_id="outside-write",
    ).execute(GateRunnerSelector("python", relative))

    assert result.ok is False
    assert result.returncode != 0
    assert outside.exists() is False


@pytest.mark.parametrize("operator", ["name_only", "empty_body", "reverse_assertion"])
def test_non_behavioral_mutation_is_not_credited_as_killed(
    tmp_path: Path,
    operator: str,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    spec = _mutation_spec(root, operator=operator)

    report = execute_mutation(
        spec,
        base_root=tmp_path / "runs",
        repo_root=root,
    )

    assert report["executed"] is False
    assert report["killed"] is False
    assert report["ok"] is False
    assert "non-semantic mutation operator cannot receive kill credit" in report["errors"]


def test_outside_write_is_an_execution_guard_not_a_structural_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    spec = _mutation_spec(root, operator="outside_write")

    report = execute_mutation(
        spec,
        base_root=tmp_path / "runs",
        repo_root=root,
    )

    assert report["executed"] is False
    assert report["killed"] is False
    assert report["ok"] is False
    assert "unsupported semantic mutation operator" in report["errors"]


@requires_macos_sandbox
def test_mutation_executes_exact_hash_bound_candidate(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    spec = _mutation_spec(root, operator="delete_required_gate")

    report = execute_mutation(
        spec,
        base_root=tmp_path / "runs",
        repo_root=root,
    )

    assert report["executed"] is True
    assert report["killed"] is True
    assert report["ok"] is True


def test_candidate_hash_drift_fails_before_execution(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    spec = _mutation_spec(
        root,
        operator="delete_required_gate",
        candidate_sha256="0" * 64,
    )

    report = execute_mutation(
        spec,
        base_root=tmp_path / "runs",
        repo_root=root,
    )

    assert report["executed"] is False
    assert "candidate artifact hash mismatch" in report["errors"]


def test_exit_one_relabelled_as_semantic_mutation_is_not_credited(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    baseline, baseline_sha256 = _write_json(
        root,
        "baseline",
        {"gates": [{"gate_id": "required"}]},
    )
    candidate, candidate_sha256 = _write_candidate(root, "fake_candidate", "raise SystemExit(1)\n")
    oracle, oracle_sha256 = _write_candidate(root, "fake_oracle", "raise SystemExit(1)\n")
    spec = ExecutableMutationSpec(
        mutation_id="relabelled",
        operator="delete_required_gate",
        baseline_path=baseline,
        baseline_sha256=baseline_sha256,
        candidate_path=candidate,
        candidate_sha256=candidate_sha256,
        collection_path=("gates",),
        identity_field="gate_id",
        removed_identity="required",
        selector=GateRunnerSelector("python", oracle),
        selector_sha256=oracle_sha256,
        killed_returncodes=(1,),
    )

    report = execute_mutation(spec, base_root=tmp_path / "runs", repo_root=root)

    assert report["executed"] is False
    assert report["killed"] is False
    assert report["ok"] is False
    assert any("invalid JSON" in error for error in report["errors"])


@requires_macos_sandbox
def test_filename_only_oracle_cannot_receive_semantic_kill_credit(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    baseline, baseline_sha256 = _write_json(
        root,
        "baseline",
        {"gates": [{"gate_id": "required"}, {"gate_id": "other"}]},
    )
    candidate, candidate_sha256 = _write_json(
        root,
        "candidate",
        {"gates": [{"gate_id": "other"}]},
    )
    oracle, oracle_sha256 = _write_candidate(
        root,
        "filename_oracle",
        "import sys\nraise SystemExit(0 if 'baseline' in sys.argv[1] else 1)\n",
    )
    spec = ExecutableMutationSpec(
        mutation_id="filename-only",
        operator="delete_required_gate",
        baseline_path=baseline,
        baseline_sha256=baseline_sha256,
        candidate_path=candidate,
        candidate_sha256=candidate_sha256,
        collection_path=("gates",),
        identity_field="gate_id",
        removed_identity="required",
        selector=GateRunnerSelector("python", oracle),
        selector_sha256=oracle_sha256,
        killed_returncodes=(1,),
    )

    report = execute_mutation(spec, base_root=tmp_path / "runs", repo_root=root)

    assert report["executed"] is True
    assert report["baseline_execution"]["returncode"] == 1
    assert report["killed"] is False
    assert report["ok"] is False


def test_pytest_selector_requires_an_existing_exact_node(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    test_file = root / "tests" / "test_exact.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_exact():\n    assert True\n", encoding="utf-8")

    assert GateRunnerSelector("pytest", "", node_ids=("tests/test_exact.py",)).validate(
        repo_root=root
    )
    assert GateRunnerSelector(
        "pytest",
        "",
        node_ids=("tests/test_exact.py::test_missing",),
    ).validate(repo_root=root)
    assert GateRunnerSelector(
        "pytest",
        "",
        argv=("--collect-only",),
        node_ids=("tests/test_exact.py::test_exact",),
    ).validate(repo_root=root)


@requires_macos_sandbox
def test_pytest_selector_executes_one_exact_node(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    test_file = root / "tests" / "test_exact.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "def test_exact():\n    assert True\n\n" "def test_must_not_run():\n    assert False\n",
        encoding="utf-8",
    )

    result = GateExecutionEnvironment(
        base_root=tmp_path / "runs",
        repo_root=root,
        gate_id="pytest-exact",
    ).execute(
        GateRunnerSelector(
            "pytest",
            "",
            node_ids=("tests/test_exact.py::test_exact",),
        )
    )

    assert result.ok is True, Path(result.stderr_path).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "decorator",
    [
        "@__import__('pytest').mark.skip(reason='no evidence')",
        "@__import__('pytest').mark.xfail(reason='no evidence')",
    ],
)
@requires_macos_sandbox
def test_pytest_skip_or_xfail_is_not_a_green_gate(
    tmp_path: Path,
    decorator: str,
) -> None:
    root = tmp_path / "repo"
    test_file = root / "tests" / "test_not_evidence.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        f"{decorator}\ndef test_not_evidence():\n    assert False\n",
        encoding="utf-8",
    )

    result = GateExecutionEnvironment(
        base_root=tmp_path / "runs",
        repo_root=root,
        gate_id="pytest-not-evidence",
    ).execute(
        GateRunnerSelector(
            "pytest",
            "",
            node_ids=("tests/test_not_evidence.py::test_not_evidence",),
        )
    )

    assert result.ok is False
    assert result.semantic_failures


@requires_macos_sandbox
def test_pytest_xpass_is_not_a_green_gate(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    test_file = root / "tests" / "test_xpass.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "@__import__('pytest').mark.xfail(reason='expected failure')\n"
        "def test_xpass():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    result = GateExecutionEnvironment(
        base_root=tmp_path / "runs",
        repo_root=root,
        gate_id="pytest-xpass",
    ).execute(
        GateRunnerSelector(
            "pytest",
            "",
            node_ids=("tests/test_xpass.py::test_xpass",),
        )
    )

    assert result.ok is False


@requires_macos_sandbox
def test_pytest_marker_cannot_override_xpass_failure(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    test_file = root / "tests" / "test_non_strict_xpass.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "@__import__('pytest').mark.xfail(reason='expected failure', strict=False)\n"
        "def test_non_strict_xpass():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    result = GateExecutionEnvironment(
        base_root=tmp_path / "runs",
        repo_root=root,
        gate_id="pytest-non-strict-xpass",
    ).execute(
        GateRunnerSelector(
            "pytest",
            "",
            node_ids=("tests/test_non_strict_xpass.py::test_non_strict_xpass",),
        )
    )

    assert result.ok is False
    assert "pytest selector produced XPASS" in result.semantic_failures


@requires_macos_sandbox
def test_repo_pytest_config_cannot_turn_xfail_into_a_pass(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    test_file = root / "tests" / "test_runxfail.py"
    test_file.parent.mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\naddopts = --runxfail\n", encoding="utf-8")
    test_file.write_text(
        "@__import__('pytest').mark.xfail(reason='expected failure')\n"
        "def test_runxfail():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    result = GateExecutionEnvironment(
        base_root=tmp_path / "runs",
        repo_root=root,
        gate_id="pytest-config-runxfail",
    ).execute(
        GateRunnerSelector(
            "pytest",
            "",
            node_ids=("tests/test_runxfail.py::test_runxfail",),
        )
    )

    assert result.ok is False


@requires_macos_sandbox
def test_repo_conftest_cannot_remove_xfail_evidence(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    test_file = root / "tests" / "test_conftest_xfail.py"
    test_file.parent.mkdir(parents=True)
    (root / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    for item in items:\n"
        "        item.own_markers[:] = [m for m in item.own_markers if m.name != 'xfail']\n",
        encoding="utf-8",
    )
    test_file.write_text(
        "@__import__('pytest').mark.xfail(reason='expected failure')\n"
        "def test_conftest_xfail():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    result = GateExecutionEnvironment(
        base_root=tmp_path / "runs",
        repo_root=root,
        gate_id="pytest-conftest-xfail",
    ).execute(
        GateRunnerSelector(
            "pytest",
            "",
            node_ids=("tests/test_conftest_xfail.py::test_conftest_xfail",),
        )
    )

    assert result.ok is False


def test_pytest_selector_rejects_explicit_plugins(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    test_file = root / "tests" / "test_explicit_plugin.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "pytest_plugins = ('tests.marker_rewriter',)\n"
        "def test_explicit_plugin():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    selector = GateRunnerSelector(
        "pytest",
        "",
        node_ids=("tests/test_explicit_plugin.py::test_explicit_plugin",),
    )

    assert "pytest node file must not declare pytest_plugins" in selector.validate(repo_root=root)


@requires_macos_sandbox
def test_pytest_runtime_guardian_rejects_dynamic_explicit_plugins(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    test_file = root / "tests" / "test_dynamic_plugin.py"
    test_file.parent.mkdir(parents=True)
    (root / "marker_rewriter.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    for item in items:\n"
        "        item.own_markers[:] = [m for m in item.own_markers if m.name != 'xfail']\n",
        encoding="utf-8",
    )
    test_file.write_text(
        "globals()['pytest_' + 'plugins'] = ['marker_rewriter']\n"
        "@__import__('pytest').mark.xfail(reason='expected failure')\n"
        "def test_dynamic_plugin():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    result = GateExecutionEnvironment(
        base_root=tmp_path / "runs",
        repo_root=root,
        gate_id="pytest-dynamic-plugin",
    ).execute(
        GateRunnerSelector(
            "pytest",
            "",
            node_ids=("tests/test_dynamic_plugin.py::test_dynamic_plugin",),
        )
    )

    assert result.ok is False
    assert result.returncode != 0
