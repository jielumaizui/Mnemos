"""Read-only Capture status contract tests."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.sync_framework.capture_queue import CaptureQueue
from core.sync_framework.capture_schema import CaptureQueueSchema
from core.sync_framework.capture_status_reader import CaptureStatusReader


def _queue(db_path: Path) -> CaptureQueue:
    CaptureQueueSchema.initialize(db_path)
    return CaptureQueue(db_path=str(db_path))


def _filesystem_state(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        path.name: (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.iterdir())
        if path.is_file()
    }


def test_missing_capture_queue_is_reported_uninitialized_without_creating_files(tmp_path: Path):
    before = _filesystem_state(tmp_path)

    result = CaptureStatusReader(tmp_path / "capture_queue.db").read("codex", "sess-1")

    assert result["status"] == "uninitialized"
    assert result["pending_counts"] == {"total": 0, "by_source": {}}
    assert _filesystem_state(tmp_path) == before


def test_uninspectable_capture_queue_is_reported_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "capture_queue.db"
    original_lstat = Path.lstat

    def denied(path: Path, *args: object, **kwargs: object):
        if path == db_path:
            raise PermissionError("sentinel")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)

    result = CaptureStatusReader(db_path).read("codex", "sess-1")

    assert result["status"] == "unavailable"
    assert result["error"] == "capture queue state is unavailable"


def test_capture_status_rejects_leaf_symlink_database(tmp_path: Path) -> None:
    target = tmp_path / "capture_queue.real.db"
    queue = _queue(target)
    queue.close()
    link = tmp_path / "capture_queue.db"
    link.symlink_to(target)

    result = CaptureStatusReader(link).read("codex", "sess-1")

    assert result["status"] == "unavailable"
    assert result["error"] == "capture queue state is not a regular file"


def test_corrupt_capture_queue_is_unavailable_not_uninitialized(tmp_path: Path) -> None:
    db_path = tmp_path / "capture_queue.db"
    db_path.write_bytes(b"not-a-sqlite-database")

    result = CaptureStatusReader(db_path).read("codex", "sess-1")

    assert result["status"] == "unavailable"
    assert result["error"] == "capture queue state is unreadable"


def test_reader_is_read_only_for_existing_queue_and_returns_status(tmp_path: Path):
    db_path = tmp_path / "capture_queue.db"
    queue = _queue(db_path)
    try:
        assert queue.enqueue(
            source_agent="codex",
            session_id="sess-1",
            turn_id=None,
            turn_number=3,
            payload={},
            content_hash="hash-1",
            raw_revision_id="rawrev-status-1",
        ) == "queued"
    finally:
        queue.close()
    before = _filesystem_state(tmp_path)

    reader = CaptureStatusReader(db_path)
    for _ in range(100):
        result = reader.read("codex", "sess-1", 3)

    assert result["status"] == "pending"
    assert result["turn_number"] == 3
    assert result["pending_counts"] == {"total": 1, "by_source": {"codex": 1}}
    assert _filesystem_state(tmp_path) == before


def test_reader_reports_active_wal_without_mutating_existing_sidecars(tmp_path: Path):
    db_path = tmp_path / "capture_queue.db"
    queue = _queue(db_path)
    try:
        assert queue.enqueue(
            source_agent="claude",
            session_id="sess-live",
            turn_id=None,
            turn_number=4,
            payload={},
            content_hash="hash-live",
            raw_revision_id="rawrev-status-live",
        ) == "queued"
        before = _filesystem_state(tmp_path)

        result = CaptureStatusReader(db_path).read("claude", "sess-live", 4)

        assert result["status"] == "read_only_wal_pending"
        assert "uncheckpointed WAL" in result["error"]
        assert _filesystem_state(tmp_path) == before
    finally:
        queue.close()


def test_reader_tolerates_old_schema_without_mutating_it(tmp_path: Path):
    db_path = tmp_path / "capture_queue.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE capture_events (
                source_agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_number INTEGER,
                status TEXT,
                retry_count INTEGER,
                created_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO capture_events VALUES (?, ?, ?, ?, ?, ?)",
            ("claude", "legacy", 1, "pending", 0, "2026-07-12T00:00:00"),
        )
        conn.commit()
    before = _filesystem_state(tmp_path)

    result = CaptureStatusReader(db_path).read("claude", "legacy", 1)

    assert result["status"] == "pending"
    assert result["handoff_receipt_id"] == ""
    assert result["session_end_receipt_id"] == ""
    assert _filesystem_state(tmp_path) == before


def test_facade_capture_status_uses_reader_not_capture_service(monkeypatch, tmp_path: Path):
    from core.application.facade import DefaultMnemosServiceFacade

    db_path = tmp_path / "capture_queue.db"
    queue = _queue(db_path)
    try:
        assert queue.enqueue(
            source_agent="kimi",
            session_id="sess-1",
            turn_id=None,
            turn_number=0,
            payload={},
            content_hash="hash-1",
            raw_revision_id="rawrev-status-facade",
        ) == "queued"
    finally:
        queue.close()
    config = Mock(database_dir=tmp_path)
    monkeypatch.setattr("core.config.get_config", lambda: config)
    facade = DefaultMnemosServiceFacade.__new__(DefaultMnemosServiceFacade)
    facade._logger = Mock()

    result = facade.capture_status("kimi", "sess-1", 0)

    assert result["success"] is True
    assert result["status"] == "pending"
    assert result["pending_counts"]["total"] == 1
