from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

import pytest


def test_pytest_tempfile_default_directory_is_run_owned():
    root = Path(os.environ["MNEMOS_RUN_ROOT"]).resolve()
    active_temp = Path(tempfile.gettempdir()).resolve()

    assert active_temp == root or root in active_temp.parents


def test_pytest_basetemp_is_run_owned_and_external_override_is_rejected(tmp_path):
    from core.ops.hermetic_run import HermeticRunEnvironment

    current_root = Path(os.environ["MNEMOS_RUN_ROOT"]).resolve()
    assert tmp_path == current_root or current_root in tmp_path.parents
    nested = HermeticRunEnvironment.create(
        current_root / "tmp" / f"nested-pytest-{uuid.uuid4().hex}",
        profile="isolated",
        base_environment=dict(os.environ),
    )
    environment = dict(nested.environment)
    environment["MNEMOS_TEST_RUN"] = "1"
    forbidden_paths = (
        current_root / "tmp" / f"outside-nested-{uuid.uuid4().hex}",
        nested.root,
        nested.root / "home",
        nested.root / "artifacts",
        nested.root / "tmp" / "other",
    )
    for forbidden in forbidden_paths:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                (
                    "tests/unit/test_hermetic_run_environment.py::"
                    "test_pytest_tempfile_default_directory_is_run_owned"
                ),
                "--basetemp",
                str(forbidden),
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert completed.returncode == pytest.ExitCode.USAGE_ERROR
        assert "pytest basetemp must equal the dedicated hermetic path" in (
            completed.stdout + completed.stderr
        )
    assert not forbidden_paths[0].exists()

    symlink_target = current_root / "tmp" / f"symlink-target-{uuid.uuid4().hex}"
    symlink_target.mkdir()
    sentinel = symlink_target / "sentinel"
    sentinel.write_text("preserve", encoding="utf-8")
    dedicated = nested.root / "tmp" / "pytest"
    dedicated.symlink_to(symlink_target, target_is_directory=True)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            (
                "tests/unit/test_hermetic_run_environment.py::"
                "test_pytest_tempfile_default_directory_is_run_owned"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == pytest.ExitCode.USAGE_ERROR
    assert "pytest dedicated basetemp cannot be a symlink" in (completed.stdout + completed.stderr)
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_run_environment_owns_every_mutable_path_and_writes_manifest(tmp_path):
    from core.ops.hermetic_run import HermeticRunEnvironment

    run = HermeticRunEnvironment.create(
        tmp_path / "run",
        profile="isolated",
        base_environment={
            "PATH": "/bin",
            "OPENAI_API_KEY": "must-not-leak-by-default",
            "UNRELATED_SECRET": "must-not-leak",
        },
    )

    assert run.profile == "isolated"
    assert run.environment_hash
    assert run.manifest_path.is_file()
    assert "UNRELATED_SECRET" not in run.environment
    assert "OPENAI_API_KEY" not in run.environment
    assert run.environment["PATH"] == "/bin"
    assert run.environment["MNEMOS_RUN_ENVIRONMENT_HASH"] == run.environment_hash

    for key in (
        "HOME",
        "USERPROFILE",
        "MNEMOS_DIR",
        "MNEMOS_DATABASE_DIR",
        "MNEMOS_WIKI_DIR",
        "MNEMOS_OBSIDIAN_CONFIG_PATH",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "PYTHONPYCACHEPREFIX",
        "MNEMOS_RUN_ARTIFACTS_DIR",
    ):
        path = Path(run.environment[key]).resolve()
        assert path == run.root or run.root in path.parents, (key, path)

    payload = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "mnemos.hermetic_run_environment.v1"
    assert payload["environment_hash"] == run.environment_hash
    assert payload["outside_write_count"] == 0
    assert payload["formal_state_diff"] == []
    assert payload["integrity"]["scheme"] == "sha256-v1"

    from core.ops.hermetic_run import verify_environment_manifest

    assert verify_environment_manifest(payload, run.environment) is True


def test_run_environment_manifest_integrity_rejects_tampering(tmp_path):
    from core.ops.hermetic_run import (
        HermeticRunEnvironment,
        verify_environment_manifest,
    )

    run = HermeticRunEnvironment.create(
        tmp_path / "run",
        profile="isolated",
        base_environment={"PATH": "/bin"},
    )
    payload = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    payload["outside_write_count"] = 1

    assert verify_environment_manifest(payload, run.environment) is False


def test_run_environment_inherits_credentials_only_when_explicit(tmp_path):
    from core.ops.hermetic_run import HermeticRunEnvironment

    run = HermeticRunEnvironment.create(
        tmp_path / "run",
        profile="isolated",
        base_environment={"PATH": "/bin", "OPENAI_API_KEY": "explicit-test-key"},
        inherit_credentials=True,
    )

    assert run.environment["OPENAI_API_KEY"] == "explicit-test-key"


def test_run_environment_refuses_reusing_nonempty_root(tmp_path):
    from core.ops.hermetic_run import HermeticRunEnvironment

    root = tmp_path / "existing"
    root.mkdir()
    (root / "foreign.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        HermeticRunEnvironment.create(root, profile="isolated")

    assert (root / "foreign.txt").read_text(encoding="utf-8") == "keep"


def test_run_environment_rejects_unimplemented_profile(tmp_path):
    from core.ops.hermetic_run import HermeticRunEnvironment

    with pytest.raises(ValueError, match="unknown hermetic run profile"):
        HermeticRunEnvironment.create(tmp_path / "run", profile="read_only")


def test_run_environment_detects_formal_state_change_and_updates_manifest(tmp_path):
    from core.ops.hermetic_run import HermeticRunEnvironment

    formal = tmp_path / "formal.db"
    formal.write_bytes(b"before")
    run = HermeticRunEnvironment.create(
        tmp_path / "run",
        profile="isolated",
        formal_targets=(formal,),
    )

    formal.write_bytes(b"after")

    assert run.finalize() == [str(formal)]
    payload = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    assert payload["outside_write_count"] == 1
    assert payload["formal_state_diff"] == [str(formal)]
