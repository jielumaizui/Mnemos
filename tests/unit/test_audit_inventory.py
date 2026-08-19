from pathlib import Path


def test_audit_inventory_excludes_generated_directories(tmp_path):
    from scripts.audit_inventory import iter_project_files

    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "real.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "third_party.py").write_text("print('skip')\n", encoding="utf-8")
    (tmp_path / ".audit_venv").mkdir()
    (tmp_path / ".audit_venv" / "audit_dependency.py").write_text(
        "print('skip')\n", encoding="utf-8"
    )
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cached.pyc").write_bytes(b"skip")
    (tmp_path / "mnemos.egg-info").mkdir()
    (tmp_path / "mnemos.egg-info" / "SOURCES.txt").write_text("skip\n", encoding="utf-8")

    files = {p.relative_to(tmp_path).as_posix() for p in iter_project_files(tmp_path)}

    assert files == {"core/real.py"}


def test_audit_inventory_tracks_generated_artifact_names():
    from scripts.audit_inventory import GENERATED_PATH_NAMES

    assert ".venv" in GENERATED_PATH_NAMES
    assert ".audit_venv" in GENERATED_PATH_NAMES
    assert "__pycache__" in GENERATED_PATH_NAMES
    assert ".pytest_cache" in GENERATED_PATH_NAMES
    assert "mnemos.egg-info" in GENERATED_PATH_NAMES


def test_gitignore_names_common_generated_artifacts():
    text = Path(".gitignore").read_text(encoding="utf-8")

    for pattern in (".venv", "__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/"):
        assert pattern in text
