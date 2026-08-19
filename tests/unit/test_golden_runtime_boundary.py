from __future__ import annotations

import pytest


def test_golden_requires_owned_output_root(monkeypatch):
    from core.benchmarks import golden

    monkeypatch.delenv("MNEMOS_RUN_ARTIFACTS_DIR", raising=False)

    with pytest.raises(ValueError, match="output_dir"):
        golden._prepare_paths(None)


def test_golden_uses_run_artifacts_without_shared_latest(monkeypatch, tmp_path):
    from core.benchmarks import golden

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setenv("MNEMOS_RUN_ARTIFACTS_DIR", str(artifacts))

    paths = golden._prepare_paths(None)

    assert paths.run_dir == artifacts / "golden"
    assert paths.run_dir.is_relative_to(tmp_path)
