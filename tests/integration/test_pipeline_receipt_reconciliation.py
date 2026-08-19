from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace


def test_reconciliation_migrates_requeues_and_attaches_handoff(monkeypatch, tmp_path):
    cfg = SimpleNamespace(
        database_dir=tmp_path / "db",
        wiki_dir=tmp_path / "wiki",
        claude_data_dir=tmp_path / "claude",
        data_dir=tmp_path,
        get=lambda _key, default=None: default,
    )
    cfg.database_dir.mkdir(parents=True)
    cfg.wiki_dir.mkdir(parents=True)
    cfg.claude_data_dir.mkdir(parents=True)
    monkeypatch.setattr("core.config.get_config", lambda: cfg)
    monkeypatch.setattr("core.sync_framework.capture_queue.get_config", lambda: cfg)

    from core.sync_framework.capture_queue import CaptureQueue
    from core.sync_framework.capture_schema import CaptureQueueSchema

    capture_path = cfg.database_dir / "capture_queue.db"
    CaptureQueueSchema.initialize(capture_path)
    capture = CaptureQueue(db_path=str(capture_path))
    capture.enqueue(
        source_agent="codex",
        session_id="shared-session",
        turn_id=None,
        turn_number=0,
        payload={"user_content": "same", "assistant_content": "", "cwd": "."},
        content_hash="capture-hash",
        raw_revision_id="rawrev-capture-key",
    )
    with sqlite3.connect(str(capture.db_path)) as conn:
        conn.execute("UPDATE capture_events SET status='done'")
    capture.close()

    from core.kia import amphora

    monkeypatch.setattr(amphora, "_DB_PATH", cfg.database_dir / "distill_queue.db")
    messages_dir = cfg.database_dir / "distill_messages"
    messages_dir.mkdir()
    messages_path = messages_dir / "legacy-task.json"
    messages_path.write_text(json.dumps([{"role": "user", "content": "same"}]), encoding="utf-8")
    with sqlite3.connect(str(amphora._DB_PATH)) as conn:
        conn.execute("""
            CREATE TABLE distillation_tasks (
                task_id TEXT PRIMARY KEY,
                session_id TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                priority INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 3,
                messages_path TEXT,
                meta TEXT,
                progress_step TEXT,
                progress_detail TEXT,
                progress REAL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                output_path TEXT,
                error TEXT,
                next_retry_at TEXT,
                updated_at TEXT
            )
            """)
        conn.execute(
            """
            INSERT INTO distillation_tasks (
                task_id, session_id, status, messages_path, meta, created_at,
                completed_at, updated_at
            ) VALUES ('legacy-task', 'shared-session', 'done', ?, ?, datetime('now'),
                      datetime('now'), datetime('now'))
            """,
            (str(messages_path), json.dumps({"source": "codex"})),
        )

    from scripts import reconcile_pipeline_receipts as reconciliation

    monkeypatch.setattr(reconciliation, "get_config", lambda: cfg)
    before = reconciliation.audit()
    prepared = reconciliation.apply_repairs()
    assert prepared["applied"]["amphora_schema_migrated"] == 1
    assert prepared["applied"]["amphora_migration_blocked"] == 1
    inventory = amphora.build_historical_provenance_inventory()
    backup_dir = tmp_path / "backups"
    result = reconciliation.apply_repairs(
        amphora_manifest=inventory,
        backup_dir=backup_dir,
    )

    assert before["reconciliation_gap"] == 2
    assert result["reconciliation_gap"] == 0
    assert result["applied"]["amphora_requeued"] == 0
    assert result["applied"]["capture_handoffs_committed"] == 1
    with sqlite3.connect(str(cfg.database_dir / "capture_queue.db")) as conn:
        assert (
            conn.execute("SELECT status FROM capture_distillation_handoffs").fetchone()[0]
            == "committed"
        )
    with sqlite3.connect(str(cfg.database_dir / "distill_queue.db")) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM distillation_tasks WHERE session_id='shared-session'"
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT status FROM distillation_tasks WHERE task_id='legacy-task'"
            ).fetchone()[0]
            == "reconciliation_required"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM amphora_provenance_migrations"
        ).fetchone()[0] == 1
    assert (backup_dir / "legacy-task" / "backup_manifest.json").is_file()


def test_reconciliation_apply_without_reviewed_manifest_leaves_amphora_gap(
    monkeypatch, tmp_path
):
    cfg = SimpleNamespace(
        database_dir=tmp_path / "db",
        wiki_dir=tmp_path / "wiki",
        claude_data_dir=tmp_path / "claude",
        data_dir=tmp_path,
        get=lambda _key, default=None: default,
    )
    cfg.database_dir.mkdir(parents=True)
    cfg.wiki_dir.mkdir(parents=True)
    cfg.claude_data_dir.mkdir(parents=True)
    monkeypatch.setattr("core.config.get_config", lambda: cfg)
    from core.kia import amphora
    from scripts import reconcile_pipeline_receipts as reconciliation

    monkeypatch.setattr(amphora, "_DB_PATH", cfg.database_dir / "distill_queue.db")
    legacy = amphora.enqueue_with_receipt(
        "legacy-session",
        [{"role": "user", "content": "legacy"}],
        {"source": "codex", "input_revision": "legacy-revision"},
    )
    with sqlite3.connect(amphora._DB_PATH) as conn:
        conn.execute(
            "UPDATE distillation_tasks SET status='reconciliation_required', "
            "terminal_reason='legacy_done_without_typed_terminal_receipt' "
            "WHERE task_id=?",
            (legacy.task_id,),
        )
    monkeypatch.setattr(reconciliation, "get_config", lambda: cfg)

    result = reconciliation.apply_repairs()

    assert result["reconciliation_gap"] == 1
    assert result["applied"]["amphora_migration_blocked"] == 1
    with sqlite3.connect(amphora._DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM distillation_tasks").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM amphora_provenance_migrations"
        ).fetchone()[0] == 0


def test_reconciliation_does_not_hide_failed_capture_handoff(monkeypatch, tmp_path):
    cfg = SimpleNamespace(
        database_dir=tmp_path / "db",
        wiki_dir=tmp_path / "wiki",
        claude_data_dir=tmp_path / "claude",
        data_dir=tmp_path,
        get=lambda _key, default=None: default,
    )
    cfg.database_dir.mkdir(parents=True)
    cfg.wiki_dir.mkdir(parents=True)
    monkeypatch.setattr("core.config.get_config", lambda: cfg)
    monkeypatch.setattr("core.sync_framework.capture_queue.get_config", lambda: cfg)
    from core.sync_framework.capture_queue import CaptureQueue
    from core.sync_framework.capture_schema import CaptureQueueSchema

    capture_path = cfg.database_dir / "capture_queue.db"
    CaptureQueueSchema.initialize(capture_path)
    capture = CaptureQueue(db_path=str(capture_path))
    capture.enqueue(
        source_agent="codex",
        session_id="session",
        turn_id=None,
        turn_number=0,
        payload={"user_content": "same", "assistant_content": "", "cwd": "."},
        content_hash="capture-hash",
        raw_revision_id="rawrev-capture-key",
    )
    event = capture.dequeue(limit=1)[0]
    handoff = capture.create_distillation_handoff("codex", "session", [event])
    capture.fail_distillation_handoff(handoff["receipt_id"], "database locked")
    capture.close()
    from scripts import reconcile_pipeline_receipts as reconciliation

    monkeypatch.setattr(reconciliation, "get_config", lambda: cfg)
    report = reconciliation.audit()

    assert report["capture"]["done_events_without_handoff"] == 0
    assert report["capture"]["nonterminal_handoffs"] == 1
    assert report["reconciliation_gap"] == 1


def test_document_worker_duplicate_is_removed_only_after_canonical_handoff_commits(
    monkeypatch, tmp_path
):
    cfg = SimpleNamespace(
        database_dir=tmp_path / "db",
        wiki_dir=tmp_path / "wiki",
        claude_data_dir=tmp_path / "claude",
        data_dir=tmp_path,
        get=lambda _key, default=None: default,
    )
    cfg.database_dir.mkdir(parents=True)
    monkeypatch.setattr("core.config.get_config", lambda: cfg)
    monkeypatch.setattr("core.sync_framework.capture_queue.get_config", lambda: cfg)

    from core.sync_framework.capture_queue import CaptureQueue
    from core.sync_framework.capture_schema import CaptureQueueSchema
    from core.sync_framework.raw_event_store import RawEventStore
    from scripts import reconcile_pipeline_receipts as reconciliation

    raw_db = cfg.database_dir / "raw_events.db"
    store = RawEventStore(db_path=raw_db, config=cfg)
    canonical_revision = store.upsert_turn(
        source_agent="file_ingestor:codex",
        session_id="document-session",
        turn_number=1,
        user_content="document body",
        assistant_content="",
        metadata={
            "asset_kind": "trusted_user_document",
            "asset_id": "document:abc",
            "distill_requested": True,
        },
        origin="capture_service",
    )
    duplicate_revision = store.upsert_turn(
        source_agent="file_ingestor:codex",
        session_id="document-session",
        turn_number=0,
        user_content="document body",
        assistant_content="",
        metadata={
            "asset_kind": "trusted_user_document",
            "asset_id": "document:abc",
            "raw_event_id": canonical_revision,
        },
        origin="sync_engine",
    )

    queue_path = cfg.database_dir / "capture_queue.db"
    CaptureQueueSchema.initialize(queue_path)
    queue = CaptureQueue(db_path=str(queue_path))
    queue.enqueue(
        source_agent="file_ingestor:codex",
        session_id="document-session",
        turn_id=None,
        turn_number=1,
        payload={"user_content": "document body"},
        content_hash="document-hash",
        raw_revision_id=canonical_revision,
    )
    event = queue.dequeue_by_session("file_ingestor:codex", "document-session")[0]
    handoff = queue.create_distillation_handoff(
        "file_ingestor:codex", "document-session", [event]
    )
    queue.commit_distillation_handoff(
        handoff["receipt_id"],
        downstream_receipt_id="amphora-document",
        downstream_task_id="task-document",
    )
    queue.close()

    rows = reconciliation._document_worker_duplicate_rows(
        raw_db, cfg.database_dir / "capture_queue.db"
    )
    assert len(rows) == 1
    assert rows[0]["safe_to_remove"] is True

    removed, remaining = reconciliation._remove_safe_document_worker_duplicates(
        raw_db, cfg.database_dir / "capture_queue.db"
    )

    assert removed == 1
    assert remaining == 0
    assert store.get_turn(canonical_revision) is not None
    assert store.get_turn(duplicate_revision) is None
    store.close()
