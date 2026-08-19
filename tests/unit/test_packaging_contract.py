"""Packaging contract tests for source-detached Mnemos installs."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path


def test_wheel_contains_runtime_packages_and_resources(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    wheel_dir = tmp_path / "wheelhouse"
    wheel_dir.mkdir()

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(repo),
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )

    wheels = sorted(wheel_dir.glob("mnemos-*.whl"))
    assert wheels, "mnemos wheel was not built"
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())

    assert "daemon/runtime.py" in names
    assert "core/scoring/adaptive_scorer_v2.py" in names
    assert "core/vaults/vault_sync.py" in names
    assert "integrations/sources/codex_source.py" in names
    assert "prompts/distill/extract/base.md" in names
    assert "prompts/distill/_output_schemas/extract.json" in names
    assert "config/config.example.json" in names
    assert "config/config.example.yaml" in names
