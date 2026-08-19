"""Capture schema ownership and permanent idempotency contract tests."""

from __future__ import annotations

import sqlite3
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.ops.exclusive_file_lock import (
    ExclusiveFileLockError,
    exclusive_file_lock,
)
from core.ops.durable_io import DurableIOError
from core.sync_framework.capture_duplicate_policy import CaptureDuplicatePolicy
from core.sync_framework.capture_maintenance import CaptureRetentionMaintenance
from core.sync_framework.capture_queue import CaptureQueue
from core.sync_framework.capture_schema import (
    CaptureQueueSchema,
    CaptureQueueSchemaMigrationRequired,
)


class _Config:
    def __init__(self, database_dir):
        self.database_dir = database_dir
        self.data_dir = database_dir

    def get(self, key, default=None):  # noqa: ARG002
        return default


def _open_queue(tmp_path):
    path = tmp_path / "capture_queue.db"
    CaptureQueueSchema.initialize(path)
    return CaptureQueue(db_path=str(path))


def test_constructor_requires_explicit_schema_and_does_not_create_database(tmp_path):
    path = tmp_path / "capture_queue.db"

    with pytest.raises(CaptureQueueSchemaMigrationRequired):
        CaptureQueue(db_path=str(path))

    assert not path.exists()
    assert not path.with_name("capture_queue.db-wal").exists()
    assert not path.with_name("capture_queue.db-shm").exists()


def test_capture_schema_inspection_does_not_label_unavailable_uninitialized(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "capture_queue.db"
    original_lstat = Path.lstat

    def denied(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("sentinel")
        return original_lstat(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)

    result = CaptureQueueSchema.inspect(path)

    assert result["status"] == "unreadable"
    assert result["error"] == "capture_queue_path_unavailable"


def test_capture_schema_inspection_rejects_leaf_symlink_database(tmp_path):
    target = tmp_path / "capture_queue.real.db"
    CaptureQueueSchema.initialize(target)
    link = tmp_path / "capture_queue.db"
    link.symlink_to(target)

    result = CaptureQueueSchema.inspect(link)

    assert result["status"] == "unreadable"
    assert result["error"] == "capture_queue_path_not_regular"


def test_capture_schema_initialize_never_follows_a_leaf_symlink(tmp_path):
    target = tmp_path / "capture_queue.real.db"
    with sqlite3.connect(target) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES ('preserve')")
    before = target.read_bytes()
    link = tmp_path / "capture_queue.db"
    link.symlink_to(target)

    with pytest.raises(DurableIOError, match="anchored_sqlite_path_not_regular"):
        CaptureQueueSchema.initialize(link)

    assert target.read_bytes() == before
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone() == ("preserve",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='capture_schema_meta'"
        ).fetchone() is None


def test_capture_schema_initialize_rejects_path_replacement_before_connect(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "capture_queue.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE original(value TEXT)")
        connection.execute("INSERT INTO original VALUES ('reviewed')")
    replacement_template = tmp_path / "replacement.db"
    with sqlite3.connect(replacement_template) as connection:
        connection.execute("CREATE TABLE foreign_state(value TEXT)")
        connection.execute("INSERT INTO foreign_state VALUES ('preserve')")
    detached = tmp_path / "reviewed.detached.db"
    real_connect = sqlite3.connect
    injected = {"done": False}

    def replace_before_connect(database, *args, **kwargs):
        if str(database) == str(path) and not injected["done"]:
            injected["done"] = True
            path.replace(detached)
            shutil.copyfile(replacement_template, path)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", replace_before_connect)

    with pytest.raises(DurableIOError, match="anchored_sqlite_identity_changed"):
        CaptureQueueSchema.initialize(path)

    assert injected["done"] is True
    with real_connect(path) as connection:
        assert connection.execute("SELECT value FROM foreign_state").fetchone() == ("preserve",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='capture_schema_meta'"
        ).fetchone() is None
    with real_connect(detached) as connection:
        assert connection.execute("SELECT value FROM original").fetchone() == ("reviewed",)


def test_capture_schema_inspection_reads_a_verifiable_live_wal(tmp_path):
    path = tmp_path / "capture_queue.db"
    CaptureQueueSchema.initialize(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE live_sentinel(value INTEGER)")
        connection.commit()

        result = CaptureQueueSchema.inspect(path)

        assert result["status"] == "current"
    finally:
        connection.close()


def test_payload_cleanup_keeps_permanent_idempotency_and_explicit_replay(tmp_path):
    queue = _open_queue(tmp_path)
    try:
        assert queue.enqueue(
            source_agent="codex",
            session_id="session-1",
            turn_id="native-message-1",
            turn_number=0,
            payload={"metadata": {"raw_event_id": "rawrev-1"}},
            content_hash="capture-hash-1",
            raw_revision_id="rawrev-1",
        ) == "queued"
        event = queue.dequeue(limit=1)[0]
        queue.update_status(event["id"], "done")
        conn = queue._pool.get_conn()  # noqa: SLF001
        old = (datetime.now() - timedelta(days=91)).isoformat()
        conn.execute("UPDATE capture_events SET created_at=? WHERE id=?", (old, event["id"]))
        conn.commit()

        maintenance = CaptureRetentionMaintenance(config=_Config(tmp_path))
        result = maintenance.apply(
            maintenance.plan(
                payload_retention_days=30,
                artifact_retention_days=30,
                artifact_max_total_bytes=1024,
            )
        )
        assert result["deleted_payloads"] == 1
        assert queue.enqueue(
            source_agent="codex",
            session_id="session-1",
            turn_id="native-message-1",
            turn_number=0,
            payload={},
            content_hash="capture-hash-1",
            raw_revision_id="rawrev-1",
        ) == "duplicate"
        # A replay is a separate explicit generation, not a fake new capture.
        assert queue.enqueue(
            source_agent="codex",
            session_id="session-1",
            turn_id="native-message-1",
            turn_number=0,
            payload={},
            content_hash="capture-hash-1",
            raw_revision_id="rawrev-1",
            replay_generation=1,
        ) == "queued"
        key = CaptureDuplicatePolicy.build(
            source_agent="codex", raw_revision_id="rawrev-1", replay_generation=0
        ).value
        assert queue.is_duplicate(key) is True
    finally:
        queue.close()


def test_payload_cleanup_uses_exact_candidate_preimage(tmp_path):
    queue = _open_queue(tmp_path)
    try:
        assert queue.enqueue(
            source_agent="codex",
            session_id="session-preimage",
            turn_id="native-message",
            turn_number=0,
            payload={},
            content_hash="capture-hash",
            raw_revision_id="raw-preimage",
        ) == "queued"
        event = queue.dequeue(limit=1)[0]
        queue.update_status(event["id"], "done")
        conn = queue._pool.get_conn()  # noqa: SLF001
        old = (datetime.now() - timedelta(days=91)).isoformat()
        conn.execute(
            "UPDATE capture_events SET created_at=? WHERE id=?",
            (old, event["id"]),
        )
        conn.commit()
        maintenance = CaptureRetentionMaintenance(config=_Config(tmp_path))
        plan = maintenance.plan(
            payload_retention_days=30,
            artifact_retention_days=30,
            artifact_max_total_bytes=1024,
        )
        conn.execute(
            "UPDATE capture_events SET status='failed' WHERE id=?",
            (event["id"],),
        )
        conn.commit()

        result = maintenance.apply(plan)

        assert result["status"] == "partial"
        assert result["deleted_payloads"] == 0
        assert result["stale_payloads"] == 1
        assert conn.execute(
            "SELECT status FROM capture_events WHERE id=?",
            (event["id"],),
        ).fetchone() == ("failed",)
    finally:
        queue.close()


def test_payload_cleanup_rejects_duplicate_candidate_denominator(tmp_path):
    queue = _open_queue(tmp_path)
    try:
        assert queue.enqueue(
            source_agent="codex",
            session_id="session-duplicate",
            turn_id="native-message",
            turn_number=0,
            payload={},
            content_hash="capture-hash",
            raw_revision_id="raw-duplicate",
        ) == "queued"
        event = queue.dequeue(limit=1)[0]
        queue.update_status(event["id"], "done")
        conn = queue._pool.get_conn()  # noqa: SLF001
        old = (datetime.now() - timedelta(days=91)).isoformat()
        conn.execute(
            "UPDATE capture_events SET created_at=? WHERE id=?",
            (old, event["id"]),
        )
        conn.commit()
        maintenance = CaptureRetentionMaintenance(config=_Config(tmp_path))
        plan = maintenance.plan(
            payload_retention_days=30,
            artifact_retention_days=30,
            artifact_max_total_bytes=1024,
        )
        plan["payload_candidates"].append(dict(plan["payload_candidates"][0]))
        plan["plan_hash"] = maintenance._plan_hash(plan)  # noqa: SLF001

        with pytest.raises(ValueError, match="capture payload candidate id is invalid"):
            maintenance.apply(plan)
        assert conn.execute(
            "SELECT status FROM capture_events WHERE id=?",
            (event["id"],),
        ).fetchone() == ("done",)
    finally:
        queue.close()


def test_capture_retention_apply_has_one_cross_process_filesystem_owner(tmp_path):
    queue = _open_queue(tmp_path)
    try:
        maintenance = CaptureRetentionMaintenance(config=_Config(tmp_path))
        plan = maintenance.plan(
            payload_retention_days=30,
            artifact_retention_days=30,
            artifact_max_total_bytes=1024,
        )
        lock_path = tmp_path / ".capture_retention_apply.lock"

        with exclusive_file_lock(
            lock_path,
            unavailable_message="test already owns capture maintenance",
        ):
            with pytest.raises(
                ExclusiveFileLockError,
                match="capture_retention_apply_in_progress",
            ):
                maintenance.apply(plan)
    finally:
        queue.close()


def test_schema_migrates_legacy_queue_and_preserves_existing_receipt(tmp_path):
    path = tmp_path / "capture_queue.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE capture_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT UNIQUE,
                source_agent TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id TEXT,
                turn_number INTEGER,
                payload_json TEXT,
                content_hash TEXT,
                status TEXT,
                retry_count INTEGER,
                created_at TEXT,
                processed_at TEXT,
                error TEXT,
                working_dir TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO capture_events (
                dedupe_key, source_agent, session_id, turn_number, content_hash, status, created_at
            ) VALUES ('legacy-key', 'codex', 'legacy-session', 0, 'hash', 'done', '2020-01-01')
            """
        )

    CaptureQueueSchema.initialize(path)
    queue = CaptureQueue(db_path=str(path))
    try:
        conn = queue._pool.get_conn()  # noqa: SLF001
        row = conn.execute(
            "SELECT identity_kind FROM capture_idempotency_receipts WHERE idempotency_key='legacy-key'"
        ).fetchone()
        assert row == ("legacy_capture_key",)
        assert CaptureQueueSchema.inspect(path)["status"] == "current"
    finally:
        queue.close()
