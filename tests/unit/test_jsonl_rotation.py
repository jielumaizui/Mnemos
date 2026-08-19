"""Unit tests for core.jsonl_rotation."""

import json

import pytest

from core.jsonl_rotation import (
    cleanup_jsonl_archives,
    iter_jsonl_lines,
    jsonl_total_size,
    rotate_jsonl,
)


@pytest.fixture
def jsonl_path(tmp_path):
    return tmp_path / "test.jsonl"


def test_rotate_jsonl_creates_archive_when_over_size(jsonl_path):
    jsonl_path.write_text("\n".join(json.dumps({"i": i}) for i in range(100)) + "\n")
    # Force rotation regardless of actual size by setting tiny threshold
    assert rotate_jsonl(jsonl_path, max_size_bytes=1, max_archives=3) is True
    assert not jsonl_path.exists()
    archive = jsonl_path.parent / "test.1.jsonl"
    assert archive.exists()


def test_iter_jsonl_lines_reads_archives_chronologically(jsonl_path):
    # Current file
    jsonl_path.write_text(json.dumps({"src": "current"}) + "\n")
    # Archive 1 (newer)
    (jsonl_path.parent / "test.1.jsonl").write_text(
        json.dumps({"src": "archive1"}) + "\n"
    )
    # Archive 2 (older)
    (jsonl_path.parent / "test.2.jsonl").write_text(
        json.dumps({"src": "archive2"}) + "\n"
    )

    lines = list(iter_jsonl_lines(jsonl_path))
    records = [json.loads(line) for line in lines]
    assert [r["src"] for r in records] == ["archive2", "archive1", "current"]


def test_rotate_jsonl_drops_oldest_archive(jsonl_path):
    # Pre-fill two archives so the next rotation must drop the oldest
    (jsonl_path.parent / "test.1.jsonl").write_text(
        json.dumps({"batch": 1}) + "\n"
    )
    (jsonl_path.parent / "test.2.jsonl").write_text(
        json.dumps({"batch": 0}) + "\n"
    )
    jsonl_path.write_text(json.dumps({"batch": 2}) + "\n")
    rotate_jsonl(jsonl_path, max_size_bytes=1, max_archives=2)

    # After rotation: current -> .1, old .1 -> .2, old .2 dropped
    assert (jsonl_path.parent / "test.1.jsonl").exists()
    assert (jsonl_path.parent / "test.2.jsonl").exists()
    assert not (jsonl_path.parent / "test.3.jsonl").exists()
    # The dropped archive contained batch 0
    assert json.dumps({"batch": 0}) not in (jsonl_path.parent / "test.2.jsonl").read_text()
    # The newest archive should contain batch 2
    assert json.dumps({"batch": 2}) in (jsonl_path.parent / "test.1.jsonl").read_text()


def test_cleanup_jsonl_archives_by_total_size(jsonl_path):
    jsonl_path.write_text(json.dumps({"current": True}) + "\n")
    for i in range(1, 4):
        (jsonl_path.parent / f"test.{i}.jsonl").write_text(
            json.dumps({"i": i}) + "\n" * 100
        )
    removed = cleanup_jsonl_archives(
        jsonl_path, max_total_size_bytes=10, max_age_days=None
    )
    assert removed >= 1
    # Current file always kept
    assert jsonl_path.exists()


def test_jsonl_total_size_includes_archives(jsonl_path):
    jsonl_path.write_text(json.dumps({"i": 1}) + "\n")
    (jsonl_path.parent / "test.1.jsonl").write_text(json.dumps({"i": 2}) + "\n")
    total = jsonl_total_size(jsonl_path)
    assert total == jsonl_path.stat().st_size + (
        jsonl_path.parent / "test.1.jsonl"
    ).stat().st_size
