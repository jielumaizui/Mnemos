from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.sync_framework.capture_queue import CaptureQueue
from core.sync_framework.capture_schema import CaptureQueueSchema
from core.sync_framework.capture_service import CaptureService
from core.sync_framework.capture_worker import CaptureWorkerPool


def _config(tmp_path: Path):
    values = {
        "capture.max_workers": 1,
        "capture.per_source_concurrency": 1,
        "capture.max_batch_per_tick": 50,
        "capture.tick_interval_seconds": 0.01,
        "distill.auto": True,
    }
    return SimpleNamespace(
        data_dir=tmp_path,
        database_dir=tmp_path,
        wiki_dir=tmp_path / "wiki",
        obsidian_vault_path=tmp_path / "wiki",
        get=lambda key, default=None: values.get(key, default),
    )


def _queue(tmp_path: Path) -> CaptureQueue:
    db_path = tmp_path / "capture_queue.db"
    CaptureQueueSchema.initialize(db_path)
    return CaptureQueue(db_path=str(db_path))


def _event(queue: CaptureQueue, *, key: str = "k1", source: str = "codex", session: str = "s1"):
    assert (
        queue.enqueue(
            source_agent=source,
            session_id=session,
            turn_id=None,
            turn_number=0,
            payload={"user_content": "hello", "assistant_content": "world", "cwd": "."},
            content_hash="hash-1",
            raw_revision_id=f"rawrev-{key}",
        )
        == "queued"
    )
    return queue.dequeue_by_session(source, session)[0]


def test_capture_handoff_failure_is_durable_and_restart_retry_commits(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    queue = _queue(tmp_path)
    event = _event(queue)
    engine = Mock()
    engine.sync_single_turn.return_value = SimpleNamespace(action="created", error="")
    monkeypatch.setattr("core.sync_framework.capture_worker.get_config", lambda: cfg)
    worker = CaptureWorkerPool(queue=queue, sync_engine=engine)

    with patch(
        "core.kia.amphora.enqueue_with_receipt", side_effect=sqlite3.OperationalError("locked")
    ):
        worker._process_registry_session("codex", "s1", [event], Mock())

    status = queue.get_status("codex", "s1", 0)
    handoff = queue.get_distillation_handoff("codex", "s1")
    assert status["status"] == "handoff_pending"
    assert handoff["status"] == "retryable_failed"
    assert handoff["event_ids"] == [event["id"]]

    receipt = SimpleNamespace(
        receipt_id="amphora-receipt-1",
        task_id="task-1",
        status="pending",
        input_revision=handoff["input_revision"],
    )
    restarted = CaptureWorkerPool(queue=queue, sync_engine=engine)
    with patch("core.kia.amphora.enqueue_with_receipt", return_value=receipt):
        assert restarted.dispatch_pending_handoffs() == 1

    status = queue.get_status("codex", "s1", 0)
    handoff = queue.get_distillation_handoff("codex", "s1")
    assert status["status"] == "done"
    assert handoff["status"] == "committed"
    assert handoff["downstream_receipt_id"] == "amphora-receipt-1"


def test_capture_session_reports_partial_and_preserves_item_receipts(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    queue = _queue(tmp_path)
    monkeypatch.setattr("core.sync_framework.capture_service.get_config", lambda: cfg)
    service = CaptureService(queue=queue, start_worker=False)
    results = iter(
        [
            {"status": "queued", "capture_dedupe_key": "a"},
            {"status": "error", "capture_dedupe_key": "b", "message": "disk locked"},
        ]
    )
    monkeypatch.setattr(service, "capture_turn", lambda **_kwargs: next(results))

    result = service.capture_session("codex", "s-partial", [{}, {}])

    assert result["status"] == "partial"
    assert result["queued_count"] == 1
    assert result["error_count"] == 1
    assert result["item_receipts"][1]["message"] == "disk locked"


def test_end_session_returns_durable_receipt_and_never_masks_db_failure(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    queue = _queue(tmp_path)
    monkeypatch.setattr("core.sync_framework.capture_service.get_config", lambda: cfg)
    service = CaptureService(queue=queue, start_worker=False)

    ok = service.end_session("codex", "s-end")
    assert ok["status"] == "handoff_pending"
    assert ok["receipt_id"]
    assert queue.get_session_end_receipt("codex", "s-end")["status"] == "handoff_pending"

    monkeypatch.setattr(
        queue, "mark_session_end", Mock(side_effect=sqlite3.OperationalError("locked"))
    )
    failed = service.end_session("codex", "s-failed")
    assert failed["status"] == "error"
    assert failed["receipt_id"] == ""


def test_worker_batch_reports_retryable_handoff_failure_not_false_success(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    queue = _queue(tmp_path)
    _event(queue)
    queue.reset_processing_to_pending()
    engine = Mock()
    engine.sync_turns.return_value = [SimpleNamespace(action="created", error="")]
    monkeypatch.setattr("core.sync_framework.capture_worker.get_config", lambda: cfg)
    worker = CaptureWorkerPool(queue=queue, sync_engine=engine)

    with patch(
        "core.kia.amphora.enqueue_with_receipt",
        side_effect=sqlite3.OperationalError("locked"),
    ):
        result = worker.process_batch()

    assert result["status"] == "retryable_failed"
    assert result["committed"] == 0
    assert result["errors"] == 1
    assert queue.get_status("codex", "s1", 0)["status"] == "handoff_pending"


def test_session_end_receipt_stays_retryable_when_capture_has_terminal_failure(
    tmp_path, monkeypatch
):
    cfg = _config(tmp_path)
    queue = _queue(tmp_path)
    assert queue.enqueue(
        source_agent="codex",
        session_id="s-failed",
        turn_id=None,
        turn_number=0,
        payload={},
        content_hash="h",
        raw_revision_id="rawrev-k",
    ) == "queued"
    event = queue.dequeue_by_session("codex", "s-failed")[0]
    queue.update_status(event["id"], "failed", error="permanent")
    queue.mark_session_end("codex", "s-failed")
    monkeypatch.setattr("core.sync_framework.capture_worker.get_config", lambda: cfg)
    worker = CaptureWorkerPool(queue=queue, sync_engine=Mock())

    assert worker._dequeue_session_end_markers() == []
    receipt = queue.get_session_end_receipt("codex", "s-failed")
    assert receipt["status"] == "retryable_failed"
    assert "1 capture event" in receipt["error"]
