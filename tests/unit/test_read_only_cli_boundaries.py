from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.ops.hermetic_run import HermeticRunEnvironment


ROOT = Path(__file__).resolve().parents[2]


def _mutable_tree(root: Path) -> set[str]:
    return {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if not str(path.relative_to(root)).startswith("pycache/")
    }


@pytest.mark.parametrize(
    ("command", "expected_returncode"),
    (
        (("python3", "mnemos_cli.py", "health", "--json"), 1),
        (("python3", "scripts/verify_installation.py", "--json"), 1),
        (("python3", "mnemos_cli.py", "status"), 0),
        (("python3", "mnemos_cli.py", "distill", "status"), 0),
    ),
)
def test_default_diagnostic_entrypoints_do_not_provision_state(
    tmp_path, command, expected_returncode
):
    run = HermeticRunEnvironment.create(
        tmp_path / "run",
        profile="isolated",
        formal_targets=(),
    )
    before = _mutable_tree(run.root)

    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=dict(run.environment),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == expected_returncode, completed.stderr
    assert _mutable_tree(run.root) == before
    assert completed.stderr == ""
