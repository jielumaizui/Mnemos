"""Tests for release artifact cleanup."""

from __future__ import annotations

from scripts.clean_release_artifacts import collect_artifacts, main, remove_artifacts


def test_collect_artifacts_finds_release_noise(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "dist").mkdir()
    (tmp_path / "EOF").write_text("", encoding="utf-8")
    cache = tmp_path / "core" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "stale.cpython-312.pyc").write_bytes(b"pyc")

    paths = {artifact.path.relative_to(tmp_path).as_posix() for artifact in collect_artifacts(tmp_path)}

    assert paths == {"build", "dist", "EOF", "core/__pycache__"}


def test_collect_artifacts_skips_virtualenv_caches(tmp_path):
    cache = tmp_path / ".audit_venv" / "lib" / "python3.12" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "pip.cpython-312.pyc").write_bytes(b"pyc")

    assert collect_artifacts(tmp_path) == []


def test_remove_artifacts_deletes_release_noise(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "dist").mkdir()
    (tmp_path / "EOF").write_text("", encoding="utf-8")
    cache = tmp_path / "core" / "__pycache__"
    cache.mkdir(parents=True)

    artifacts = collect_artifacts(tmp_path)
    assert remove_artifacts(artifacts) == 4

    assert not (tmp_path / "build").exists()
    assert not (tmp_path / "dist").exists()
    assert not (tmp_path / "EOF").exists()
    assert not cache.exists()


def test_main_check_returns_nonzero_when_artifacts_exist(tmp_path, capsys):
    (tmp_path / "EOF").write_text("", encoding="utf-8")

    assert main(["--repo-root", str(tmp_path), "--check"]) == 1

    captured = capsys.readouterr()
    assert "Run with --apply" in captured.out


def test_main_apply_cleans_artifacts(tmp_path):
    (tmp_path / "EOF").write_text("", encoding="utf-8")

    assert main(["--repo-root", str(tmp_path), "--apply"]) == 0

    assert collect_artifacts(tmp_path) == []
