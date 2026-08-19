"""Unit tests for core.utils."""

import os
import stat
from pathlib import Path

from core.utils import (
    secure_directory,
    secure_file,
    set_sensitive_umask,
)


def test_secure_directory_sets_700(tmp_path: Path):
    d = tmp_path / "secret"
    d.mkdir(mode=0o755)
    assert secure_directory(d) is True
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_secure_file_sets_600(tmp_path: Path):
    f = tmp_path / "secret.json"
    f.write_text("x")
    os.chmod(f, 0o644)
    assert secure_file(f) is True
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_set_sensitive_umask_affects_new_files(tmp_path: Path):
    old = set_sensitive_umask()
    try:
        f = tmp_path / "new_file"
        f.write_text("x")
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    finally:
        os.umask(old)


def test_set_sensitive_umask_returns_old_umask():
    original = os.umask(0o022)
    try:
        old = set_sensitive_umask()
        assert old == 0o022
        assert os.umask(0o022) == 0o077
    finally:
        os.umask(original)


def test_secure_missing_path_returns_false(tmp_path: Path):
    assert secure_directory(tmp_path / "nope") is False
    assert secure_file(tmp_path / "nope") is False
