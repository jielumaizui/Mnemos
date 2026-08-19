"""Unit tests for scripts/check_tech_debt_annotations.py."""

from pathlib import Path

import pytest

from scripts.check_tech_debt_annotations import main


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    return tmp_path / "sample.py"


def test_valid_annotations_pass(sample_file: Path):
    sample_file.write_text(
        "# TODO(2026-06-25): do something\n"
        "# FIXME(2026-06-25,owner): fix bug\n"
        "# DEBT(S25): sensitive data encryption\n",
        encoding="utf-8",
    )
    assert main([str(sample_file)]) == 0


def test_missing_marker_fails(sample_file: Path):
    sample_file.write_text(
        "# TODO: do something\n",
        encoding="utf-8",
    )
    assert main([str(sample_file)]) == 1


def test_invalid_marker_fails(sample_file: Path):
    sample_file.write_text(
        "# FIXME(nobody): bad marker\n",
        encoding="utf-8",
    )
    assert main([str(sample_file)]) == 1
