# -*- coding: utf-8 -*-
"""
HephaestusWorker 核心公共行为单元测试

覆盖项（按优先级排序）：
1. __init__() — 初始化与目录属性解析
2. process_all() — 空队列/无队列/文件扫描回退
3. process_one() — 指定 session_id 处理
4. process_one_task() — 重试耗尽与唯一同步 API 路径
5. _archive_failed_task_data() — 失败归档行为
6. get_stats() — 统计信息结构
7. stop() / watch_queue() — 停止事件与轮询生命周期

测试策略：
- 所有外部依赖（AgentDelegate、amphora、distillation_engine、EventBus）使用 monkeypatch / mock
- 所有 I/O 操作在 tmp_path 中进行
- time.sleep 被 mock 以加速测试
- 不使用真实 HTTP/API/SQLite 调用
"""

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.ops.durable_io import DurableIOError


@pytest.fixture(autouse=True)  # noqa
def _reset_distillation_pause_state(monkeypatch, tmp_path):
    """每个测试前重置蒸馏暂停状态，避免测试隔离问题。"""
    pause_db = tmp_path / "distillation_state.db"
    monkeypatch.setattr(
        "core.hephaestus.distillation_pause._get_pause_db",
        lambda: pause_db,
    )
    from core.hephaestus.distillation_pause import resume_distillation

    resume_distillation()


# ========== Fixtures ==========


@pytest.fixture
def dirs(tmp_path):
    """返回 HephaestusWorker 所需的四个临时目录。"""
    d = {
        "queue": tmp_path / "distill_queue",
        "output": tmp_path / "distill_output",
        "inbox": tmp_path / "wiki" / "00-Inbox",
        "archive": tmp_path / "distill_archive",
        "failed": tmp_path / "distill_failed",
    }
    for p in d.values():
        p.mkdir(parents=True)
    return d


@pytest.fixture
def worker(dirs, monkeypatch, tmp_path):
    """返回一个已配置临时目录且 _emit_progress 被静默的 Worker。"""
    monkeypatch.setattr(
        "core.hephaestus_worker.HephaestusWorker._emit_progress",
        lambda self, *args, **kwargs: None,
    )
    # 隔离 amphora 全局数据库，避免其他测试遗留任务污染本测试
    from core.kia import amphora

    monkeypatch.setattr(amphora, "_DB_PATH", tmp_path / "distill_queue.db")
    amphora._init_db()

    from core.hephaestus_worker import HephaestusWorker

    return HephaestusWorker(
        queue_dir=dirs["queue"],
        inbox_dir=dirs["inbox"],
        archive_dir=dirs["archive"],
    )


# ========== 1. 初始化 ==========


def test_init_with_custom_dirs(dirs):
    """自定义目录应被正确保存，属性返回传入值。"""
    from core.hephaestus_worker import HephaestusWorker

    w = HephaestusWorker(
        queue_dir=dirs["queue"],
        inbox_dir=dirs["inbox"],
        archive_dir=dirs["archive"],
    )
    assert w.queue_dir == dirs["queue"]
    assert w.inbox_dir == dirs["inbox"]
    assert w.archive_dir == dirs["archive"]
    assert w._stop_event.is_set() is False


def test_init_defaults_uses_config_paths(monkeypatch):
    """未传目录时应回退到配置默认路径。"""
    from core.hephaestus_worker import HephaestusWorker

    fake_config = SimpleNamespace(
        claude_data_dir=Path("/fake/claude"),
        database_dir=Path("/fake/db"),
        wiki_dir=Path("/fake/wiki"),
    )
    fake_config.get = lambda key, default=None: default
    monkeypatch.setattr("core.hephaestus_worker.get_config", lambda: fake_config)

    w = HephaestusWorker()
    assert w.queue_dir == Path("/fake/db") / "distill_queue"
    assert w.inbox_dir == Path("/fake/wiki") / "00-Inbox"


def test_backend_uses_worker_inbox_vault(dirs, monkeypatch):
    """L1 标记后端必须跟随 worker 的 wiki 目标，避免扫默认全局 vault。"""
    from core.hephaestus_worker import HephaestusWorker

    created = {}
    fake_backend = object()

    def fake_create_storage_backend(*args, **kwargs):
        created["args"] = args
        created["kwargs"] = kwargs
        return fake_backend

    monkeypatch.setattr(
        "core.sync_framework.storage_backend.create_storage_backend",
        fake_create_storage_backend,
    )

    w = HephaestusWorker(
        queue_dir=dirs["queue"],
        inbox_dir=dirs["inbox"],
        archive_dir=dirs["archive"],
    )

    assert w.backend is fake_backend
    assert created["args"] == ()
    assert created["kwargs"]["vault_path"] == dirs["inbox"].parent


# ========== 2. process_all ==========


def test_process_all_empty_queue_returns_zero(worker):
    """空队列应返回 0，不抛异常。"""
    result = worker.process_all()
    assert result == 0


def test_process_all_nonexistent_queue_returns_zero(worker):
    """legacy queue_dir 不存在时仍应正常返回 0，不跳过 SQLite pending 处理。"""
    worker._queue_dir = Path("/nonexistent/queue")
    result = worker.process_all()
    assert result == 0


def test_process_all_max_tasks_zero_returns_zero(worker, monkeypatch):
    """max_tasks <= 0 时应直接返回 0，不扫描文件。"""
    monkeypatch.setattr(
        "core.hephaestus_worker.get_config",
        lambda: SimpleNamespace(
            get=lambda key, default=None: 0 if key == "distill.max_tasks_per_cycle" else default,
            database_dir=Path("/fake"),
            wiki_dir=Path("/fake"),
        ),
    )
    result = worker.process_all()
    assert result == 0


def test_emit_progress_maps_judged_to_amphora_structuring(dirs, monkeypatch):
    """judged 阶段应同步为 Amphora 的 structuring 队列进度。"""
    from core.hephaestus_worker import HephaestusWorker
    from core.kia import amphora

    monkeypatch.setattr(
        "core.mnemos_bus.publish_event",
        lambda *args, **kwargs: kwargs.get("trace_id") or "worker-test-trace",
    )
    updates = []
    monkeypatch.setattr(
        amphora,
        "update_progress",
        lambda session_id, step, message: updates.append((session_id, step, message)) or True,
    )

    w = HephaestusWorker(
        queue_dir=dirs["queue"],
        inbox_dir=dirs["inbox"],
        archive_dir=dirs["archive"],
    )

    w._emit_progress("sess-1", "judged", "building structure")

    assert updates == [("sess-1", amphora.DistillProgress.STRUCTURING.value, "building structure")]


# ========== 3. process_one ==========


def test_process_one_missing_session_returns_false(worker):
    """指定 session_id 不在 amphora 队列时应返回 False。"""
    result = worker.process_one("nonexistent-session")
    assert result is False


def test_process_one_finds_task_in_amphora(worker, monkeypatch):
    """应从 amphora 队列中找到对应 session_id 并处理。"""
    from core.kia import amphora

    processed = {"called": False}

    def _capture(self, task):
        processed["called"] = True
        processed["session_id"] = task.get("session_id")
        processed["status"] = task.get("status")
        processed["started_at"] = task.get("started_at")
        return True

    monkeypatch.setattr(
        "core.hephaestus_worker.HephaestusWorker.process_one_task",
        _capture,
    )

    amphora.enqueue_with_receipt(
        "sess-find",
        [{"role": "user", "content": "hello"}],
        meta={"source": "test"},
    )
    result = worker.process_one("sess-find")
    assert result is True
    assert processed["called"] is True
    assert processed["session_id"] == "sess-find"
    assert processed["status"] == "processing"
    assert processed["started_at"]


def test_process_one_claims_requested_task_not_global_queue_head(worker, monkeypatch):
    from core.kia import amphora

    processed = []
    low = amphora.enqueue_with_receipt(
        "sess-requested-low",
        [{"role": "user", "content": "requested"}],
        priority=0,
    )
    high = amphora.enqueue_with_receipt(
        "sess-unrelated-high",
        [{"role": "user", "content": "unrelated"}],
        priority=2,
    )
    monkeypatch.setattr(
        worker,
        "process_one_task",
        lambda task: processed.append(task["task_id"]) or True,
    )

    assert worker.process_one(low.task_id) is True
    assert processed == [low.task_id]
    pending = amphora.list_pending()
    assert [task["task_id"] for task in pending] == [high.task_id]


# ========== 5. process_one_task ==========


def test_process_one_task_ignores_legacy_output_file_and_uses_sync_owner(
    worker, dirs, monkeypatch
):
    """A stray distill_output file must never become a second write entrypoint."""

    rogue_output = dirs["output"] / "sess-sync-only.md"
    rogue_output.write_text('{"judgment":"knowledge"}', encoding="utf-8")
    calls = []

    def _sync(self, session_id, distill_task, *, task=None):
        calls.append((session_id, distill_task, task))
        return True

    monkeypatch.setattr(
        "core.hephaestus_worker.HephaestusWorker._sync_distill_and_complete",
        _sync,
    )
    task = {
        "task_id": "task-sync-only",
        "session_id": "sess-sync-only",
        "messages": [{"role": "user", "content": "hello"}],
        "meta": {"source": "test"},
        "retry_count": 0,
    }

    assert worker.process_one_task(task) is True
    assert len(calls) == 1
    assert calls[0][0] == "sess-sync-only"
    assert rogue_output.exists()


def test_process_one_task_max_retries_returns_false(worker, monkeypatch):
    """A stale exhausted DTO without a durable queue row cannot authorize archival."""
    archived = {"called": False}

    def _capture_archive(self, sid, data, reason):
        archived["called"] = True
        archived["reason"] = reason

    monkeypatch.setattr(
        "core.hephaestus_worker.HephaestusWorker._archive_failed_task_data",
        _capture_archive,
    )

    task = {
        "session_id": "sess-max",
        "messages": [],
        "meta": {},
        "retry_count": 3,
    }
    from core.hephaestus_worker import DistillationWorkerCycleError

    with pytest.raises(
        DistillationWorkerCycleError,
        match="amphora_failure_transition_unmatched",
    ):
        worker.process_one_task(task)
    assert archived["called"] is False


def _force_worker_failure(worker, task, monkeypatch):
    def _raise_storage_error(self, session_id, distill_task):
        raise OSError(f"injected worker failure for {session_id}")

    monkeypatch.setattr(
        "core.hephaestus_worker.HephaestusWorker._run_distillation_engine",
        _raise_storage_error,
    )
    return worker.process_one_task(task)


def _make_runtime_bound_task(worker, tmp_path, *, session_id, max_retries):
    from core.kia import amphora
    from core.ops.cognitive_pipeline_receipts import record_capture_worker_handoff
    from core.ops.producer_consumer_ledger import DEFAULT_MATRIX, ProducerConsumerLedger

    worker.config = SimpleNamespace(
        database_dir=tmp_path,
        wiki_dir=tmp_path / "wiki",
        get=lambda key, default=None: default,
    )
    ledger = ProducerConsumerLedger(worker.config, initialize=True)
    ledger.register_adaptive_flows(DEFAULT_MATRIX)
    receipt = amphora.enqueue_with_receipt(
        session_id,
        [{"role": "user", "content": "retry contract"}],
        meta={"source": "test"},
        max_retries=max_retries,
    )
    record_capture_worker_handoff(worker.config, session_id, receipt)
    return ledger, receipt


def _cognitive_event(event_id: str, *, session_id: str):
    from core.ops.cognitive_data_contract import CognitiveDataEvent

    return CognitiveDataEvent(
        event_id=event_id,
        source_id=f"raw-{event_id}",
        asset_id=f"raw-{event_id}",
        source_kind="sync_engine",
        source_uri=f"sync://agent/{session_id}/turn/1",
        content_hash=f"content-{event_id}",
        canonical_subject=f"agent:{session_id}:turn:1",
        data_type="synced_turn",
        producer="sync_engine",
        intended_consumers=("amphora", "distill"),
        privacy_level="local",
        confidence=1.0,
        evidence_refs=(f"raw-{event_id}",),
        dedupe_key=f"{event_id}:turn:1",
        created_at="2026-07-13T00:00:00+00:00",
    )


def _release_retry_backoff(amphora, task_id):
    with sqlite3.connect(amphora._DB_PATH) as conn:
        conn.execute(
            "UPDATE distillation_tasks SET next_retry_at=NULL WHERE task_id=?",
            (task_id,),
        )


def test_real_worker_failure_records_terminal_on_task_retry_exhaustion(
    worker, tmp_path, monkeypatch
):
    """The same failure transaction that exhausts Amphora must sign dead-letter."""
    import sqlite3

    from core.kia import amphora

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-default-retries",
        max_retries=3,
    )
    for attempt in range(3):
        task = amphora.get_next()
        assert task is not None
        assert _force_worker_failure(worker, task, monkeypatch) is False
        if attempt < 2:
            _release_retry_backoff(amphora, receipt.task_id)

    with sqlite3.connect(amphora._DB_PATH) as conn:
        task_row = conn.execute(
            "SELECT status, retry_count, max_retries FROM distillation_tasks "
            "WHERE task_id=?",
            (receipt.task_id,),
        ).fetchone()
    with sqlite3.connect(ledger.db_path) as conn:
        terminal_rows = conn.execute(
            "SELECT status FROM runtime_flow_receipts "
            "WHERE status IN ('consumed', 'dead_letter', 'skipped')"
        ).fetchall()

    assert task_row == ("failed", 3, 3)
    assert terminal_rows == [("dead_letter",)]


def test_terminal_receipt_survives_failed_archive_side_effect(
    worker, tmp_path, monkeypatch
):
    """A secondary archive failure cannot erase the queue's committed terminal."""
    import sqlite3

    from core.kia import amphora

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-terminal-archive-failure",
        max_retries=1,
    )
    task = amphora.get_next()
    assert task is not None
    monkeypatch.setattr(
        worker,
        "_archive_failed_task_data",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("archive unavailable")),
    )

    assert _force_worker_failure(worker, task, monkeypatch) is False

    with sqlite3.connect(amphora._DB_PATH) as conn:
        task_row = conn.execute(
            "SELECT status, retry_count FROM distillation_tasks WHERE task_id=?",
            (receipt.task_id,),
        ).fetchone()
    with sqlite3.connect(ledger.db_path) as conn:
        terminal_rows = conn.execute(
            "SELECT status FROM runtime_flow_receipts "
            "WHERE status IN ('consumed', 'dead_letter', 'skipped')"
        ).fetchall()
    assert task_row == ("failed", 1)
    assert terminal_rows == [("dead_letter",)]


def test_failed_terminal_outbox_recovers_commit_receipt_crash_on_restart(
    worker,
    dirs,
    tmp_path,
    monkeypatch,
):
    """A crash after queue commit is repaired once, including duplicate replay."""
    from core.hephaestus_worker import HephaestusWorker
    from core.kia import amphora

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-terminal-crash-recovery",
        max_retries=1,
    )
    task = amphora.get_next()
    assert task is not None

    def _crash_before_receipt(**_kwargs):
        raise SystemExit("injected_after_amphora_commit")

    monkeypatch.setattr(
        worker,
        "reconcile_failed_terminal_receipts",
        _crash_before_receipt,
    )
    with pytest.raises(SystemExit, match="injected_after_amphora_commit"):
        worker._mark_amphora_failed(
            receipt.task_id,
            "persistent failure",
            task=task,
        )

    with sqlite3.connect(amphora._DB_PATH) as conn:
        status, meta_json = conn.execute(
            "SELECT status, meta FROM distillation_tasks WHERE task_id=?",
            (receipt.task_id,),
        ).fetchone()
    outbox = json.loads(meta_json)["failed_terminal_receipt_outbox"]
    assert status == "failed"
    assert outbox["status"] == "pending"
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts "
            "WHERE status='dead_letter'"
        ).fetchone()[0] == 0

    restarted = HephaestusWorker(
        queue_dir=dirs["queue"],
        inbox_dir=dirs["inbox"],
        archive_dir=dirs["archive"],
    )
    restarted.config = worker.config
    assert restarted.process_all(max_tasks=0) == 0

    with sqlite3.connect(amphora._DB_PATH) as conn:
        committed_meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        committed_outbox = committed_meta["failed_terminal_receipt_outbox"]
        committed_outbox["status"] = "pending"
        committed_outbox.pop("committed_at")
        committed_outbox.pop("runtime_receipt_id")
        committed_outbox.pop("production_event_id")
        committed_outbox.pop("generation_id")
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(committed_meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )

    assert restarted.reconcile_failed_terminal_receipts() == 1
    assert restarted.reconcile_failed_terminal_receipts() == 0
    with sqlite3.connect(ledger.db_path) as conn:
        terminal_rows = conn.execute(
            "SELECT status FROM runtime_flow_receipts "
            "WHERE status IN ('consumed', 'dead_letter', 'skipped')"
        ).fetchall()
    assert terminal_rows == [("dead_letter",)]


def test_success_terminal_outbox_recovers_commit_receipt_crash_on_restart(
    worker,
    dirs,
    tmp_path,
):
    """A restart replays a committed Amphora skip exactly once."""
    from core.hephaestus_worker import HephaestusWorker
    from core.kia import amphora
    from core.pipeline_receipts import DistillationWriteReceipt

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-success-terminal-crash-recovery",
        max_retries=1,
    )
    task = amphora.get_next()
    assert task is not None
    terminal = DistillationWriteReceipt(
        status="intentional_skip",
        terminal_reason="no durable knowledge",
    )
    assert amphora.mark_terminal(
        receipt.task_id,
        terminal,
        expected_started_at=task["started_at"],
    )
    pending = amphora.list_terminal_receipt_outbox(
        identifier=receipt.task_id,
    )
    assert len(pending) == 1
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts "
            "WHERE status='consumed'"
        ).fetchone()[0] == 0

    restarted = HephaestusWorker(
        queue_dir=dirs["queue"],
        inbox_dir=dirs["inbox"],
        archive_dir=dirs["archive"],
    )
    restarted.config = worker.config
    assert restarted.process_all(max_tasks=0) == 0
    assert amphora.list_terminal_receipt_outbox(
        identifier=receipt.task_id,
    ) == []
    assert restarted.reconcile_terminal_receipts() == 0
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts "
            "WHERE status='consumed'"
        ).fetchone()[0] == 1


def test_success_terminal_outbox_waits_for_missing_cognitive_event(
    worker,
    tmp_path,
):
    """An explicit missing event keeps success pending until both receipts exist."""
    from core.kia import amphora
    from core.ops.cognitive_data_contract import CognitiveDataEvent
    from core.pipeline_receipts import DistillationWriteReceipt

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-success-terminal-missing-event",
        max_retries=1,
    )
    event_id = "cde-success-terminal-late"
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        meta["cognitive_sync_event_ids"] = [event_id]
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )
    task = amphora.get_next()
    assert task is not None
    assert amphora.mark_terminal(
        receipt.task_id,
        DistillationWriteReceipt(
            status="intentional_skip",
            terminal_reason="no durable knowledge",
        ),
        expected_started_at=task["started_at"],
    )

    assert worker.reconcile_terminal_receipts() == 0
    assert len(
        amphora.list_terminal_receipt_outbox(
            identifier=receipt.task_id,
        )
    ) == 1
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts "
            "WHERE status='consumed'"
        ).fetchone()[0] == 0

    ledger.record_data_event(
        CognitiveDataEvent(
            event_id=event_id,
            source_id="raw-success-terminal-late",
            asset_id="raw-success-terminal-late",
            source_kind="sync_engine",
            source_uri="sync://agent/success-terminal-late/turn/1",
            content_hash="success-terminal-late-content",
            canonical_subject="agent:success-terminal-late:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=("raw-success-terminal-late",),
            dedupe_key="success-terminal-late:turn:1",
            created_at="2026-07-13T00:00:00+00:00",
        )
    )
    assert worker.reconcile_terminal_receipts() == 1
    with sqlite3.connect(ledger.db_path) as conn:
        rows = conn.execute(
            """
            SELECT consumer_id, status, outcome
            FROM cognitive_data_consumptions
            WHERE event_id=?
            ORDER BY consumer_id
            """,
            (event_id,),
        ).fetchall()
        runtime_count = conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts "
            "WHERE status='consumed'"
        ).fetchone()[0]
    assert rows == [
        ("amphora", "committed", "distill_task_handoff_verified"),
        ("distill", "committed", "distill_task_intentional_skip"),
    ]
    assert runtime_count == 1


def test_success_terminal_outbox_ignores_late_added_cognitive_event(
    worker,
    tmp_path,
):
    """Mutable task metadata cannot expand the success terminal denominator."""
    from core.kia import amphora
    from core.ops.cognitive_data_contract import CognitiveDataEvent
    from core.pipeline_receipts import DistillationWriteReceipt

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-success-terminal-frozen",
        max_retries=1,
    )

    def _event(event_id):
        return CognitiveDataEvent(
            event_id=event_id,
            source_id=f"raw-{event_id}",
            asset_id=f"raw-{event_id}",
            source_kind="sync_engine",
            source_uri=f"sync://agent/{event_id}/turn/1",
            content_hash=f"content-{event_id}",
            canonical_subject=f"agent:{event_id}:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=(f"raw-{event_id}",),
            dedupe_key=f"{event_id}:turn:1",
            created_at="2026-07-13T00:00:00+00:00",
        )

    event_a = "cde-success-frozen-a"
    event_b = "cde-success-frozen-b"
    ledger.record_data_event(_event(event_a))
    ledger.record_data_event(_event(event_b))
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        meta["cognitive_sync_event_ids"] = [event_a]
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )
    task = amphora.get_next()
    assert task is not None
    assert amphora.mark_terminal(
        receipt.task_id,
        DistillationWriteReceipt(
            status="intentional_skip",
            terminal_reason="no durable knowledge",
        ),
        expected_started_at=task["started_at"],
    )
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        assert meta["terminal_receipt_outbox"]["cognitive_event_ids"] == [
            event_a
        ]
        meta["cognitive_sync_event_ids"] = [event_a, event_b]
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )

    assert worker.reconcile_terminal_receipts() == 1
    with sqlite3.connect(ledger.db_path) as conn:
        counts = dict(
            conn.execute(
                """
                SELECT event_id, COUNT(*)
                FROM cognitive_data_consumptions
                WHERE event_id IN (?, ?)
                GROUP BY event_id
                """,
                (event_a, event_b),
            ).fetchall()
        )
    assert counts == {event_a: 2}


def test_success_terminal_outbox_commit_rejects_caller_declared_proof(
    worker,
    tmp_path,
):
    """A caller cannot self-sign a pending success terminal outbox."""
    from core.kia import amphora
    from core.pipeline_receipts import DistillationWriteReceipt

    _ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-success-terminal-forged-proof",
        max_retries=1,
    )
    task = amphora.get_next()
    assert task is not None
    assert amphora.mark_terminal(
        receipt.task_id,
        DistillationWriteReceipt(
            status="intentional_skip",
            terminal_reason="no durable knowledge",
        ),
        expected_started_at=task["started_at"],
    )
    pending = amphora.list_terminal_receipt_outbox(
        identifier=receipt.task_id,
    )
    assert len(pending) == 1

    with pytest.raises(
        RuntimeError,
        match="terminal_receipt_proof_verification_failed",
    ):
        amphora.mark_terminal_receipt_outbox_committed(
            receipt.task_id,
            expected_created_at=pending[0]["outbox"]["created_at"],
            runtime_receipt_id="forged-runtime",
            production_event_id="forged-production",
            generation_id="forged-generation",
            config=worker.config,
        )
    assert len(
        amphora.list_terminal_receipt_outbox(
            identifier=receipt.task_id,
        )
    ) == 1


def test_success_terminal_outbox_rejects_task_generation_identity_drift(
    worker,
    tmp_path,
):
    """A pending rev1 outbox cannot be replayed against a mutable rev2 row."""
    from core.kia import amphora
    from core.ops.cognitive_pipeline_receipts import record_capture_worker_handoff
    from core.pipeline_receipts import DistillationWriteReceipt

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-success-terminal-identity-drift",
        max_retries=1,
    )
    task = amphora.get_next()
    assert task is not None
    assert amphora.mark_terminal(
        receipt.task_id,
        DistillationWriteReceipt(
            status="intentional_skip",
            terminal_reason="no durable knowledge",
        ),
        expected_started_at=task["started_at"],
    )
    record_capture_worker_handoff(
        worker.config,
        task["session_id"],
        SimpleNamespace(
            task_id=receipt.task_id,
            input_revision="revision-two",
        ),
    )
    with sqlite3.connect(amphora._DB_PATH) as conn:
        conn.execute(
            "UPDATE distillation_tasks SET input_revision=? WHERE task_id=?",
            ("revision-two", receipt.task_id),
        )

    assert worker.reconcile_terminal_receipts() == 0
    stored = amphora.list_tasks(status="intentional_skip", limit=1)[0]
    assert stored["meta"]["terminal_receipt_outbox"]["status"] == "pending"
    assert stored["meta"]["terminal_receipt_outbox"]["input_revision"] == (
        receipt.input_revision
    )
    assert stored["progress_detail"].startswith(
        "terminal_outbox_quarantined:"
    )
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts WHERE status='consumed'"
        ).fetchone()[0] == 0


def test_success_terminal_outbox_rejects_receipt_payload_drift_after_runtime_receipt(
    worker,
    tmp_path,
):
    """A self-rehashed outbox cannot replace the row/runtime receipt payload."""
    from core.kia import amphora
    from core.pipeline_receipts import DistillationWriteReceipt

    _ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-success-terminal-payload-drift",
        max_retries=1,
    )
    task = amphora.get_next()
    assert task is not None
    terminal = DistillationWriteReceipt(
        status="intentional_skip",
        terminal_reason="original terminal reason",
    )
    assert amphora.mark_terminal(
        receipt.task_id,
        terminal,
        expected_started_at=task["started_at"],
    )
    pending = amphora.list_terminal_receipt_outbox(
        identifier=receipt.task_id,
    )
    assert len(pending) == 1
    assert worker._record_terminal_runtime_receipt(
        pending[0]["task"],
        pending[0]["receipt"],
    )
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        outbox = meta["terminal_receipt_outbox"]
        outbox["receipt"]["terminal_reason"] = "tampered terminal reason"
        outbox["receipt_sha256"] = amphora._terminal_receipt_payload_sha256(
            outbox["receipt"]
        )
        conn.execute(
            """
            UPDATE distillation_tasks
            SET meta=?, terminal_reason=?
            WHERE task_id=?
            """,
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                "tampered terminal reason",
                receipt.task_id,
            ),
        )

    assert worker.reconcile_terminal_receipts() == 0
    stored = amphora.list_tasks(status="intentional_skip", limit=1)[0]
    assert stored["terminal_reason"] == "tampered terminal reason"
    assert stored["meta"]["terminal_receipt_outbox"]["status"] == "pending"


def test_success_terminal_anchor_rejects_self_rehashed_count_drift_before_replay(
    worker,
    tmp_path,
):
    """The row anchor, not the outbox self-hash, owns all receipt counts."""
    from core.kia import amphora
    from core.pipeline_receipts import DistillationWriteReceipt

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-success-terminal-count-anchor",
        max_retries=1,
    )
    task = amphora.get_next()
    assert task is not None
    assert amphora.mark_terminal(
        receipt.task_id,
        DistillationWriteReceipt(
            status="intentional_skip",
            terminal_reason="no durable knowledge",
        ),
        expected_started_at=task["started_at"],
    )
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        outbox = meta["terminal_receipt_outbox"]
        outbox["receipt"]["expected_count"] = 999
        outbox["receipt"]["failed_count"] = 999
        outbox["receipt_sha256"] = amphora._terminal_receipt_payload_sha256(
            outbox["receipt"]
        )
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )

    assert worker.reconcile_terminal_receipts() == 0
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts WHERE status='consumed'"
        ).fetchone()[0] == 0


def test_success_terminal_anchor_rejects_self_rehashed_denominator_shrink(
    worker,
    tmp_path,
):
    """A self-consistent smaller outbox cannot omit a pre-terminal event."""
    from core.kia import amphora
    from core.pipeline_receipts import DistillationWriteReceipt

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-success-terminal-denominator-anchor",
        max_retries=1,
    )
    event_ids = ["cde-success-anchor-one", "cde-success-anchor-two"]
    for event_id in event_ids:
        ledger.record_data_event(
            _cognitive_event(
                event_id,
                session_id="sess-success-terminal-denominator-anchor",
            )
        )
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        meta["cognitive_sync_event_ids"] = event_ids
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )
    task = amphora.get_next()
    assert task is not None
    assert amphora.mark_terminal(
        receipt.task_id,
        DistillationWriteReceipt(
            status="intentional_skip",
            terminal_reason="no durable knowledge",
        ),
        expected_started_at=task["started_at"],
    )
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        outbox = meta["terminal_receipt_outbox"]
        outbox["cognitive_event_ids"] = [event_ids[0]]
        outbox["cognitive_event_count"] = 1
        outbox["cognitive_event_ids_sha256"] = (
            amphora._cognitive_event_ids_sha256([event_ids[0]])
        )
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )

    assert worker.reconcile_terminal_receipts() == 0
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts WHERE status='consumed'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_data_consumptions "
            "WHERE consumer_id='distill'"
        ).fetchone()[0] == 0


def test_failed_terminal_anchor_rejects_self_rehashed_payload_drift(
    worker,
    tmp_path,
    monkeypatch,
):
    """A recomputed failed payload hash cannot replace the queue transition."""
    from core.kia import amphora
    from core.pipeline_receipts import distillation_failed_terminal_sha256

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-failed-terminal-payload-anchor",
        max_retries=1,
    )
    task = amphora.get_next()
    assert task is not None
    real_record_failed_terminal = worker._record_failed_terminal_runtime_receipt
    monkeypatch.setattr(
        worker,
        "_record_failed_terminal_runtime_receipt",
        lambda *_args, **_kwargs: None,
    )
    assert _force_worker_failure(worker, task, monkeypatch) is False
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        outbox = meta["failed_terminal_receipt_outbox"]
        outbox["reason"] = "retry_exhausted:tampered"
        outbox["payload_sha256"] = distillation_failed_terminal_sha256(
            task_id=outbox["task_id"],
            session_id=outbox["session_id"],
            input_revision=outbox["input_revision"],
            reason=outbox["reason"],
            retry_count=outbox["retry_count"],
            max_retries=outbox["max_retries"],
            cognitive_event_ids=outbox["cognitive_event_ids"],
        )
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )

    monkeypatch.setattr(
        worker,
        "_record_failed_terminal_runtime_receipt",
        real_record_failed_terminal,
    )
    assert worker.reconcile_failed_terminal_receipts() == 0
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts "
            "WHERE status='dead_letter'"
        ).fetchone()[0] == 0


def test_failed_terminal_scan_rejects_row_identity_drift_with_valid_anchor(
    worker,
    tmp_path,
    monkeypatch,
):
    """A valid outbox anchor cannot authorize a different queue-row identity."""
    from core.kia import amphora

    _ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-failed-terminal-row-binding",
        max_retries=1,
    )
    task = amphora.get_next()
    assert task is not None
    monkeypatch.setattr(
        worker,
        "_record_failed_terminal_runtime_receipt",
        lambda *_args, **_kwargs: None,
    )
    assert _force_worker_failure(worker, task, monkeypatch) is False

    with sqlite3.connect(amphora._DB_PATH) as conn:
        conn.execute(
            "UPDATE distillation_tasks SET terminal_reason=? WHERE task_id=?",
            ("tampered-row-reason", receipt.task_id),
        )

    assert amphora.list_failed_terminal_receipt_outbox(
        identifier=receipt.task_id,
    ) == []
    with sqlite3.connect(amphora._DB_PATH) as conn:
        progress_detail = conn.execute(
            "SELECT progress_detail FROM distillation_tasks WHERE task_id=?",
            (receipt.task_id,),
        ).fetchone()[0]
    assert "amphora_failed_terminal_outbox_payload_drift" in progress_detail


def test_failed_terminal_anchor_rejects_self_rehashed_denominator_shrink(
    worker,
    tmp_path,
    monkeypatch,
):
    """Failed replay cannot omit one event by recomputing every outbox hash."""
    from core.kia import amphora
    from core.pipeline_receipts import distillation_failed_terminal_sha256

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-failed-terminal-denominator-anchor",
        max_retries=1,
    )
    event_ids = ["cde-failed-anchor-one", "cde-failed-anchor-two"]
    for event_id in event_ids:
        ledger.record_data_event(
            _cognitive_event(
                event_id,
                session_id="sess-failed-terminal-denominator-anchor",
            )
        )
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        meta["cognitive_sync_event_ids"] = event_ids
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )
    task = amphora.get_next()
    assert task is not None
    real_record_failed_terminal = worker._record_failed_terminal_runtime_receipt
    monkeypatch.setattr(
        worker,
        "_record_failed_terminal_runtime_receipt",
        lambda *_args, **_kwargs: None,
    )
    assert _force_worker_failure(worker, task, monkeypatch) is False
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        outbox = meta["failed_terminal_receipt_outbox"]
        outbox["cognitive_event_ids"] = [event_ids[0]]
        outbox["cognitive_event_count"] = 1
        outbox["cognitive_event_ids_sha256"] = (
            amphora._cognitive_event_ids_sha256([event_ids[0]])
        )
        outbox["payload_sha256"] = distillation_failed_terminal_sha256(
            task_id=outbox["task_id"],
            session_id=outbox["session_id"],
            input_revision=outbox["input_revision"],
            reason=outbox["reason"],
            retry_count=outbox["retry_count"],
            max_retries=outbox["max_retries"],
            cognitive_event_ids=outbox["cognitive_event_ids"],
        )
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )

    monkeypatch.setattr(
        worker,
        "_record_failed_terminal_runtime_receipt",
        real_record_failed_terminal,
    )
    assert worker.reconcile_failed_terminal_receipts() == 0
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts "
            "WHERE status='dead_letter'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_data_consumptions "
            "WHERE consumer_id='distill'"
        ).fetchone()[0] == 0


def test_success_terminal_anchor_cannot_be_rewritten_with_forged_payload(
    worker,
    tmp_path,
):
    """The queue transition anchor is immutable, not another self-signed hash."""
    from core.kia import amphora
    from core.pipeline_receipts import DistillationWriteReceipt

    _ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-success-terminal-immutable-anchor",
        max_retries=1,
    )
    event_ids = ["cde-success-immutable-one", "cde-success-immutable-two"]
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        meta["cognitive_sync_event_ids"] = event_ids
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )
    task = amphora.get_next()
    assert task is not None
    assert amphora.mark_terminal(
        receipt.task_id,
        DistillationWriteReceipt(
            status="intentional_skip",
            terminal_reason="no durable knowledge",
        ),
        expected_started_at=task["started_at"],
    )

    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        outbox = meta["terminal_receipt_outbox"]
        outbox["cognitive_event_ids"] = [event_ids[0]]
        outbox["cognitive_event_count"] = 1
        outbox["cognitive_event_ids_sha256"] = (
            amphora._cognitive_event_ids_sha256([event_ids[0]])
        )
        outbox["receipt"]["expected_count"] = 999
        outbox["receipt"]["failed_count"] = 999
        outbox["receipt_sha256"] = amphora._terminal_receipt_payload_sha256(
            outbox["receipt"]
        )
        forged_anchor = amphora._terminal_outbox_anchor_sha256(outbox)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="terminal outbox anchor is immutable",
        ):
            conn.execute(
                "UPDATE distillation_tasks "
                "SET meta=?, terminal_outbox_anchor_sha256=? WHERE task_id=?",
                (
                    json.dumps(meta, ensure_ascii=False, sort_keys=True),
                    forged_anchor,
                    receipt.task_id,
                ),
            )


def test_failed_terminal_anchor_cannot_be_rewritten_with_forged_payload(
    worker,
    tmp_path,
    monkeypatch,
):
    """Failed-terminal reason, retry budget, and denominator share the guard."""
    from core.kia import amphora
    from core.pipeline_receipts import distillation_failed_terminal_sha256

    _ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-failed-terminal-immutable-anchor",
        max_retries=1,
    )
    event_ids = ["cde-failed-immutable-one", "cde-failed-immutable-two"]
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        meta["cognitive_sync_event_ids"] = event_ids
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )
    task = amphora.get_next()
    assert task is not None
    real_record_failed_terminal = worker._record_failed_terminal_runtime_receipt
    monkeypatch.setattr(
        worker,
        "_record_failed_terminal_runtime_receipt",
        lambda *_args, **_kwargs: None,
    )
    assert _force_worker_failure(worker, task, monkeypatch) is False
    monkeypatch.setattr(
        worker,
        "_record_failed_terminal_runtime_receipt",
        real_record_failed_terminal,
    )

    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        outbox = meta["failed_terminal_receipt_outbox"]
        outbox["reason"] = "retry_exhausted:forged"
        outbox["retry_count"] = outbox["max_retries"]
        outbox["cognitive_event_ids"] = [event_ids[0]]
        outbox["cognitive_event_count"] = 1
        outbox["cognitive_event_ids_sha256"] = (
            amphora._cognitive_event_ids_sha256([event_ids[0]])
        )
        outbox["payload_sha256"] = distillation_failed_terminal_sha256(
            task_id=outbox["task_id"],
            session_id=outbox["session_id"],
            input_revision=outbox["input_revision"],
            reason=outbox["reason"],
            retry_count=outbox["retry_count"],
            max_retries=outbox["max_retries"],
            cognitive_event_ids=outbox["cognitive_event_ids"],
        )
        forged_anchor = amphora._terminal_outbox_anchor_sha256(outbox)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="terminal outbox anchor is immutable",
        ):
            conn.execute(
                "UPDATE distillation_tasks "
                "SET meta=?, terminal_outbox_anchor_sha256=? WHERE task_id=?",
                (
                    json.dumps(meta, ensure_ascii=False, sort_keys=True),
                    forged_anchor,
                    receipt.task_id,
                ),
            )


def test_archive_failed_requires_exact_runtime_terminal_receipt(
    worker,
    tmp_path,
    monkeypatch,
):
    """Archival verifies the outbox proof against the real runtime ledger."""
    from core.kia import amphora

    _ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-terminal-archive-proof",
        max_retries=1,
    )
    task = amphora.get_next()
    assert task is not None
    assert _force_worker_failure(worker, task, monkeypatch) is False

    assert amphora.archive_failed(
        receipt.task_id,
        reason="reviewed terminal",
        config=worker.config,
    ) == 1
    archived = amphora.list_tasks(status="archived", limit=1)
    assert len(archived) == 1
    assert archived[0]["task_id"] == receipt.task_id


def test_archive_failed_rejects_outbox_reason_drift(
    worker,
    tmp_path,
    monkeypatch,
):
    """Archive proof binds the exact failure reason, not only receipt ids."""
    from core.kia import amphora

    _ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-terminal-reason-drift",
        max_retries=1,
    )
    task = amphora.get_next()
    assert task is not None
    assert _force_worker_failure(worker, task, monkeypatch) is False
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        meta["failed_terminal_receipt_outbox"]["reason"] = "tampered-reason"
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )

    with pytest.raises(
        RuntimeError,
        match="failed_terminal_archive_receipt_verification_failed",
    ):
        amphora.archive_failed(
            receipt.task_id,
            reason="reviewed terminal",
            config=worker.config,
        )


def test_failed_terminal_outbox_commit_rejects_caller_declared_proof(
    worker,
    tmp_path,
):
    """The outbox CAS independently verifies proof in the runtime ledger."""
    from core.kia import amphora

    _ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-terminal-forged-proof",
        max_retries=1,
    )
    task = amphora.get_next()
    assert task is not None
    transition = amphora.mark_failed_with_transition(
        receipt.task_id,
        "persistent failure",
    )
    assert transition is not None and transition.terminal
    pending = amphora.list_failed_terminal_receipt_outbox(
        identifier=receipt.task_id,
    )
    assert len(pending) == 1

    with pytest.raises(
        RuntimeError,
        match="failed_terminal_receipt_proof_verification_failed",
    ):
        amphora.mark_failed_terminal_receipt_outbox_committed(
            receipt.task_id,
            expected_created_at=pending[0]["outbox"]["created_at"],
            runtime_receipt_id="forged-runtime",
            production_event_id="forged-production",
            generation_id="forged-generation",
            config=worker.config,
        )
    assert len(
        amphora.list_failed_terminal_receipt_outbox(
            identifier=receipt.task_id,
        )
    ) == 1


def test_failed_terminal_outbox_rejects_foreign_runtime_ledger(
    worker,
    tmp_path,
):
    """A matching proof in another database cannot sign this Amphora task."""
    from core.kia import amphora
    from core.ops.cognitive_pipeline_receipts import (
        record_capture_worker_handoff,
        record_distillation_failed_terminal,
    )
    from core.ops.producer_consumer_ledger import (
        DEFAULT_MATRIX,
        ProducerConsumerLedger,
    )

    _real_ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-terminal-foreign-ledger",
        max_retries=1,
    )
    task = amphora.get_next()
    assert task is not None
    transition = amphora.mark_failed_with_transition(
        receipt.task_id,
        "persistent failure",
    )
    assert transition is not None and transition.terminal
    pending = amphora.list_failed_terminal_receipt_outbox(
        identifier=receipt.task_id,
    )
    assert len(pending) == 1

    foreign_dir = tmp_path / "foreign-ledger"
    foreign_config = SimpleNamespace(database_dir=foreign_dir)
    foreign_ledger = ProducerConsumerLedger(
        foreign_config,
        initialize=True,
    )
    foreign_ledger.register_adaptive_flows(DEFAULT_MATRIX)
    record_capture_worker_handoff(
        foreign_config,
        task["session_id"],
        receipt,
    )
    foreign_evidence = record_distillation_failed_terminal(
        foreign_config,
        task=task,
        reason=pending[0]["outbox"]["reason"],
    )
    assert foreign_evidence["matched"] is True

    with pytest.raises(
        RuntimeError,
        match="failed_terminal_runtime_ledger_identity_mismatch",
    ):
        amphora.mark_failed_terminal_receipt_outbox_committed(
            receipt.task_id,
            expected_created_at=pending[0]["outbox"]["created_at"],
            runtime_receipt_id=foreign_evidence["runtime_receipt_id"],
            production_event_id=foreign_evidence["production_event_id"],
            generation_id=foreign_evidence["generation_id"],
            config=foreign_config,
        )
    with pytest.raises(
        RuntimeError,
        match="failed_terminal_runtime_ledger_identity_mismatch",
    ):
        amphora.archive_failed(
            receipt.task_id,
            reason="foreign proof must not archive",
            config=foreign_config,
        )
    assert amphora.get_task_count("failed") == 1


def test_failed_terminal_outbox_rejects_task_generation_identity_drift(
    worker,
    tmp_path,
):
    """A failed rev1 outbox cannot dead-letter a mutable rev2 task row."""
    from core.kia import amphora
    from core.ops.cognitive_pipeline_receipts import record_capture_worker_handoff

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-failed-terminal-identity-drift",
        max_retries=1,
    )
    task = amphora.get_next()
    assert task is not None
    transition = amphora.mark_failed_with_transition(
        receipt.task_id,
        "persistent failure",
        expected_started_at=task["started_at"],
    )
    assert transition is not None and transition.terminal
    record_capture_worker_handoff(
        worker.config,
        task["session_id"],
        SimpleNamespace(
            task_id=receipt.task_id,
            input_revision="revision-two",
        ),
    )
    with sqlite3.connect(amphora._DB_PATH) as conn:
        conn.execute(
            "UPDATE distillation_tasks SET input_revision=? WHERE task_id=?",
            ("revision-two", receipt.task_id),
        )

    assert worker.reconcile_failed_terminal_receipts() == 0
    stored = amphora.list_tasks(status="failed", limit=1)[0]
    assert stored["meta"]["failed_terminal_receipt_outbox"]["status"] == (
        "pending"
    )
    assert stored["meta"]["failed_terminal_receipt_outbox"][
        "input_revision"
    ] == receipt.input_revision
    assert stored["progress_detail"].startswith(
        "failed_terminal_outbox_quarantined:"
    )
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts "
            "WHERE status='dead_letter'"
        ).fetchone()[0] == 0


def test_failed_terminal_outbox_waits_for_cognitive_receipt_replay(
    worker,
    tmp_path,
    monkeypatch,
):
    """Cognitive proof must become durable before runtime dead-letter closes."""
    from core.kia import amphora
    from core.ops import cognitive_pipeline_receipts as receipts
    from core.ops.cognitive_data_contract import CognitiveDataEvent

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-terminal-cognitive-deferred",
        max_retries=1,
    )
    event_id = "cde-worker-terminal-deferred"
    ledger.record_data_event(
        CognitiveDataEvent(
            event_id=event_id,
            source_id="raw-worker-deferred",
            asset_id="raw-worker-deferred",
            source_kind="sync_engine",
            source_uri="sync://agent/worker-deferred/turn/1",
            content_hash="worker-deferred-content",
            canonical_subject="agent:worker-deferred:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=("raw-worker-deferred",),
            dedupe_key="worker-deferred:turn:1",
            created_at="2026-07-13T00:00:00+00:00",
        )
    )
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        meta["cognitive_sync_event_ids"] = [event_id]
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False),
                receipt.task_id,
            ),
        )
    task = amphora.get_next()
    assert task is not None
    real_record = receipts.record_cognitive_data_consumed
    monkeypatch.setattr(
        receipts,
        "record_cognitive_data_consumed",
        lambda *_args, **_kwargs: None,
    )

    assert _force_worker_failure(worker, task, monkeypatch) is False

    pending = amphora.list_failed_terminal_receipt_outbox(
        identifier=receipt.task_id,
    )
    assert len(pending) == 1
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts "
            "WHERE status='dead_letter'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_data_consumptions "
            "WHERE event_id=? AND status='failed_terminal'",
            (event_id,),
        ).fetchone()[0] == 0
    monkeypatch.setattr(
        receipts,
        "record_cognitive_data_consumed",
        real_record,
    )

    assert worker.reconcile_failed_terminal_receipts() == 1
    assert amphora.list_failed_terminal_receipt_outbox(
        identifier=receipt.task_id,
    ) == []
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_data_consumptions "
            "WHERE event_id=? AND status='failed_terminal'",
            (event_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM cognitive_data_consumptions "
            "WHERE event_id=? AND consumer_id='amphora' AND status='committed'",
            (event_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts "
            "WHERE status='dead_letter'"
        ).fetchone()[0] == 1


def test_failed_terminal_outbox_freezes_cognitive_event_denominator(
    worker,
    tmp_path,
):
    """Pending task metadata cannot shrink the terminal cognitive denominator."""
    from core.kia import amphora
    from core.ops.cognitive_data_contract import CognitiveDataEvent

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-terminal-frozen-denominator",
        max_retries=1,
    )
    missing_event_id = "cde-frozen-but-missing"
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        meta["cognitive_sync_event_ids"] = [missing_event_id]
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )
    task = amphora.get_next()
    assert task is not None
    transition = amphora.mark_failed_with_transition(
        receipt.task_id,
        "persistent failure",
    )
    assert transition is not None and transition.terminal
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        outbox = meta["failed_terminal_receipt_outbox"]
        assert outbox["cognitive_event_ids"] == [missing_event_id]
        assert outbox["cognitive_event_count"] == 1
        meta["cognitive_sync_event_ids"] = []
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )

    assert worker.reconcile_failed_terminal_receipts() == 0
    pending = amphora.list_failed_terminal_receipt_outbox(
        identifier=receipt.task_id,
    )
    assert len(pending) == 1
    assert pending[0]["outbox"]["cognitive_event_ids"] == [missing_event_id]
    ledger.record_data_event(
        CognitiveDataEvent(
            event_id=missing_event_id,
            source_id="raw-frozen-denominator",
            asset_id="raw-frozen-denominator",
            source_kind="sync_engine",
            source_uri="sync://agent/frozen-denominator/turn/1",
            content_hash="frozen-denominator-content",
            canonical_subject="agent:frozen-denominator:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=("raw-frozen-denominator",),
            dedupe_key="frozen-denominator:turn:1",
            created_at="2026-07-13T00:00:00+00:00",
        )
    )

    assert worker.reconcile_failed_terminal_receipts() == 1
    assert amphora.list_failed_terminal_receipt_outbox(
        identifier=receipt.task_id,
    ) == []
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            """
            SELECT COUNT(*) FROM cognitive_data_consumptions
            WHERE event_id=? AND status='failed_terminal'
            """,
            (missing_event_id,),
        ).fetchone()[0] == 1


def test_real_worker_failure_defers_mixed_valid_and_missing_cognitive_events(
    worker,
    tmp_path,
    monkeypatch,
):
    """One valid event cannot hide another explicit missing event."""
    from core.kia import amphora
    from core.ops.cognitive_data_contract import CognitiveDataEvent

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-terminal-mixed-denominator",
        max_retries=1,
    )
    valid_event_id = "cde-worker-terminal-valid"
    missing_event_id = "cde-worker-terminal-missing"
    ledger.record_data_event(
        CognitiveDataEvent(
            event_id=valid_event_id,
            source_id="raw-worker-mixed",
            asset_id="raw-worker-mixed",
            source_kind="sync_engine",
            source_uri="sync://agent/worker-mixed/turn/1",
            content_hash="worker-mixed-content",
            canonical_subject="agent:worker-mixed:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=("raw-worker-mixed",),
            dedupe_key="worker-mixed:turn:1",
            created_at="2026-07-13T00:00:00+00:00",
        )
    )
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        meta["cognitive_sync_event_ids"] = [
            valid_event_id,
            missing_event_id,
        ]
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )
    task = amphora.get_next()
    assert task is not None
    assert _force_worker_failure(worker, task, monkeypatch) is False

    assert len(
        amphora.list_failed_terminal_receipt_outbox(
            identifier=receipt.task_id,
        )
    ) == 1
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts "
            "WHERE status='dead_letter'"
        ).fetchone()[0] == 0
        valid_rows = conn.execute(
            """
            SELECT consumer_id, status
            FROM cognitive_data_consumptions
            WHERE event_id=?
            ORDER BY consumer_id
            """,
            (valid_event_id,),
        ).fetchall()
        missing_count = conn.execute(
            "SELECT COUNT(*) FROM cognitive_data_consumptions WHERE event_id=?",
            (missing_event_id,),
        ).fetchone()[0]
    assert valid_rows == [
        ("amphora", "committed"),
        ("distill", "failed_terminal"),
    ]
    assert missing_count == 0


def test_failed_terminal_runtime_outage_replay_keeps_singleton_payload(
    worker,
    tmp_path,
    monkeypatch,
):
    """Repeated real Worker replay cannot duplicate one dead-letter payload."""
    from core.kia import amphora
    from core.ops.producer_consumer_ledger import ProducerConsumerLedger
    from core.ops.runtime_flow_telemetry import RuntimeFlowTelemetry

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-terminal-runtime-outage",
        max_retries=1,
    )
    task = amphora.get_next()
    assert task is not None
    real_record_dead_letter = ProducerConsumerLedger.record_dead_letter
    attempts = {"count": 0}

    def _outage(self, *args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise sqlite3.OperationalError("injected runtime telemetry outage")
        return real_record_dead_letter(self, *args, **kwargs)

    monkeypatch.setattr(
        ProducerConsumerLedger,
        "record_dead_letter",
        _outage,
    )
    assert _force_worker_failure(worker, task, monkeypatch) is False
    outbox = RuntimeFlowTelemetry(worker.config)
    assert outbox.outbox_path.is_file()
    assert len(outbox.outbox_path.read_text(encoding="utf-8").splitlines()) == 1

    assert worker.reconcile_failed_terminal_receipts() == 0
    assert len(outbox.outbox_path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(
        amphora.list_failed_terminal_receipt_outbox(
            identifier=receipt.task_id,
        )
    ) == 1

    monkeypatch.setattr(
        ProducerConsumerLedger,
        "record_dead_letter",
        real_record_dead_letter,
    )
    assert worker.reconcile_failed_terminal_receipts() == 1
    assert worker.reconcile_failed_terminal_receipts() == 0
    assert not outbox.outbox_path.exists()
    with sqlite3.connect(ledger.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts "
            "WHERE status='dead_letter'"
        ).fetchone()[0] == 1


def test_failed_terminal_outbox_frozen_denominator_ignores_late_added_cognitive_event(
    worker,
    tmp_path,
):
    """Replay records only the event set frozen by the failure transaction."""
    from core.kia import amphora
    from core.ops.cognitive_data_contract import CognitiveDataEvent

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-terminal-frozen-expand",
        max_retries=1,
    )

    def _event(event_id):
        return CognitiveDataEvent(
            event_id=event_id,
            source_id=f"raw-{event_id}",
            asset_id=f"raw-{event_id}",
            source_kind="sync_engine",
            source_uri=f"sync://agent/{event_id}/turn/1",
            content_hash=f"content-{event_id}",
            canonical_subject=f"agent:{event_id}:turn:1",
            data_type="synced_turn",
            producer="sync_engine",
            intended_consumers=("amphora", "distill"),
            privacy_level="local",
            confidence=1.0,
            evidence_refs=(f"raw-{event_id}",),
            dedupe_key=f"{event_id}:turn:1",
            created_at="2026-07-13T00:00:00+00:00",
        )

    event_a = "cde-failed-frozen-a"
    event_b = "cde-failed-frozen-b"
    ledger.record_data_event(_event(event_a))
    ledger.record_data_event(_event(event_b))
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        meta["cognitive_sync_event_ids"] = [event_a]
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )
    task = amphora.get_next()
    assert task is not None
    transition = amphora.mark_failed_with_transition(
        receipt.task_id,
        "persistent failure",
        expected_started_at=task["started_at"],
    )
    assert transition is not None and transition.terminal
    with sqlite3.connect(amphora._DB_PATH) as conn:
        meta = json.loads(
            conn.execute(
                "SELECT meta FROM distillation_tasks WHERE task_id=?",
                (receipt.task_id,),
            ).fetchone()[0]
        )
        assert meta["failed_terminal_receipt_outbox"][
            "cognitive_event_ids"
        ] == [event_a]
        meta["cognitive_sync_event_ids"] = [event_a, event_b]
        conn.execute(
            "UPDATE distillation_tasks SET meta=? WHERE task_id=?",
            (
                json.dumps(meta, ensure_ascii=False, sort_keys=True),
                receipt.task_id,
            ),
        )

    assert worker.reconcile_failed_terminal_receipts() == 1
    with sqlite3.connect(ledger.db_path) as conn:
        counts = dict(
            conn.execute(
                """
                SELECT event_id, COUNT(*)
                FROM cognitive_data_consumptions
                WHERE event_id IN (?, ?)
                GROUP BY event_id
                """,
                (event_a, event_b),
            ).fetchall()
        )
    assert counts == {event_a: 2}


def test_real_worker_respects_task_level_retry_budget_before_dead_letter(
    worker, tmp_path, monkeypatch
):
    """A max_retries=5 task remains retryable after its fourth real failure."""
    import sqlite3

    from core.kia import amphora

    ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id="sess-five-retries",
        max_retries=5,
    )
    for _attempt in range(4):
        task = amphora.get_next()
        assert task is not None
        assert task["max_retries"] == 5
        assert _force_worker_failure(worker, task, monkeypatch) is False
        _release_retry_backoff(amphora, receipt.task_id)

    with sqlite3.connect(amphora._DB_PATH) as conn:
        task_row = conn.execute(
            "SELECT status, retry_count, max_retries FROM distillation_tasks "
            "WHERE task_id=?",
            (receipt.task_id,),
        ).fetchone()
    with sqlite3.connect(ledger.db_path) as conn:
        terminal_count = conn.execute(
            "SELECT COUNT(*) FROM runtime_flow_receipts "
            "WHERE status IN ('consumed', 'dead_letter', 'skipped')"
        ).fetchone()[0]

    assert task_row == ("pending", 4, 5)
    assert terminal_count == 0

    task = amphora.get_next()
    assert task is not None
    assert _force_worker_failure(worker, task, monkeypatch) is False
    with sqlite3.connect(amphora._DB_PATH) as conn:
        final_task_row = conn.execute(
            "SELECT status, retry_count, max_retries FROM distillation_tasks "
            "WHERE task_id=?",
            (receipt.task_id,),
        ).fetchone()
    with sqlite3.connect(ledger.db_path) as conn:
        final_terminal_rows = conn.execute(
            "SELECT status FROM runtime_flow_receipts "
            "WHERE status IN ('consumed', 'dead_letter', 'skipped')"
        ).fetchall()
    assert final_task_row == ("failed", 5, 5)
    assert final_terminal_rows == [("dead_letter",)]


@pytest.mark.parametrize("failure_mode", ["unmatched", "exception"])
def test_worker_never_signs_failed_terminal_when_queue_failure_was_not_committed(
    worker, tmp_path, monkeypatch, failure_mode
):
    """A false/exceptional Amphora update cannot authorize terminal evidence."""
    from core.kia import amphora

    _ledger, receipt = _make_runtime_bound_task(
        worker,
        tmp_path,
        session_id=f"sess-uncommitted-failure-{failure_mode}",
        max_retries=3,
    )
    task = amphora.get_next()
    assert task is not None
    task["retry_count"] = task["max_retries"]
    recorded = []
    if failure_mode == "unmatched":
        monkeypatch.setattr(
            amphora,
            "mark_failed_with_transition",
            lambda *_args, **_kwargs: None,
        )
    else:
        def _raise_failure(*_args, **_kwargs):
            raise OSError("injected queue failure")

        monkeypatch.setattr(
            amphora,
            "mark_failed_with_transition",
            _raise_failure,
        )
    monkeypatch.setattr(
        worker,
        "_record_failed_terminal_runtime_receipt",
        lambda task, reason: recorded.append((task, reason)),
    )

    from core.hephaestus_worker import DistillationWorkerCycleError

    expected = (
        "amphora_failure_transition_unmatched"
        if failure_mode == "unmatched"
        else "amphora_failure_transition_failed"
    )
    with pytest.raises(DistillationWorkerCycleError, match=expected):
        worker.process_one_task(task)
    assert recorded == []
    assert receipt.task_id


def test_process_one_task_api_mode_skips_delegate(worker, dirs, monkeypatch):
    """默认 API 模式下不应调用 AgentDelegate，而是走 _sync_distill_and_complete。"""
    monkeypatch.setattr(
        "core.hephaestus_worker.get_config",
        lambda: SimpleNamespace(
            get=lambda key, default=None: default,
            database_dir=dirs["queue"].parent,
            wiki_dir=dirs["inbox"].parent,
        ),
    )

    sync_called = {"count": 0}

    def _capture_sync(self, sid, dt, *, task=None):
        sync_called["count"] += 1
        sync_called["session_id"] = sid
        sync_called["task"] = task
        sync_called["meta"] = dict(dt.meta)
        return True

    monkeypatch.setattr(
        "core.hephaestus_worker.HephaestusWorker._sync_distill_and_complete",
        _capture_sync,
    )

    task = {
        "task_id": "task-api",
        "session_id": "sess-api",
        "input_revision": "revision-api",
        "messages": [{"role": "user", "content": "hello"}],
        "meta": {"source": "test"},
    }
    result = worker.process_one_task(task)
    assert result is True
    assert sync_called["count"] == 1
    assert sync_called["session_id"] == "sess-api"
    assert sync_called["task"] == task
    assert sync_called["meta"]["_amphora_task_id"] == "task-api"
    assert sync_called["meta"]["input_revision"] == "revision-api"
    # AgentDelegate 已废弃移除，直接验证 _sync_distill_and_complete 被调用即可


def test_sync_distill_and_complete_times_out(worker, dirs, monkeypatch):
    """A timed-out live generation stays owned until the worker thread exits."""

    pause_db = dirs["queue"].parent / "distillation_state.db"
    monkeypatch.setattr(
        "core.hephaestus.distillation_pause._get_pause_db",
        lambda: pause_db,
    )

    monkeypatch.setattr(
        worker.config,
        "get",
        lambda key, default=None: 0.01 if key == "distill.task_timeout_seconds" else default,
    )

    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    write_calls = []

    def slow_run(self, sid, dt):
        entered.set()
        release.wait(timeout=2)
        completed.set()
        return SimpleNamespace(
            write_pages_with_receipt=lambda _result: write_calls.append(True)
        ), SimpleNamespace(judgment="skip", fragments=[])

    monkeypatch.setattr(
        "core.hephaestus_worker.HephaestusWorker._run_distillation_engine",
        slow_run,
    )

    from core.kia import amphora

    amphora.enqueue_with_receipt(
        "sess-timeout", [{"role": "user", "content": "x"}], meta={"source": "test"}
    )
    task = amphora.get_next()
    started = time.monotonic()
    result = worker.process_one_task(task)
    elapsed = time.monotonic() - started

    assert result is False
    assert entered.is_set()
    assert elapsed < 0.5
    assert amphora.get_task_count(status="processing") == 1
    assert amphora.get_task_count(status="pending") == 0
    current = amphora.list_tasks(limit=10)[0]
    assert current["retry_count"] == 0
    assert write_calls == []

    from core.hephaestus.distillation_pause import get_pause_status

    pause = get_pause_status()
    assert pause["paused"] is True
    assert pause["reason"] == "蒸馏任务超时: sess-timeout"
    assert "同步蒸馏任务超时" in pause["last_error"]

    release.set()
    assert completed.wait(timeout=1)
    deadline = time.monotonic() + 1
    while (
        amphora.get_task_count(status="processing") != 0
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert amphora.get_task_count(status="processing") == 0
    assert amphora.get_task_count(status="pending") == 1
    current = amphora.list_tasks(limit=10)[0]
    assert current["retry_count"] == 1
    assert write_calls == []


def test_process_all_fails_closed_when_pause_state_is_unreadable(
    worker, monkeypatch
):
    from core.hephaestus_worker import DistillationWorkerCycleError

    monkeypatch.setattr(worker, "reconcile_proposal_tasks", lambda: 0)
    monkeypatch.setattr(worker, "reconcile_terminal_receipts", lambda: 0)
    monkeypatch.setattr(worker, "reconcile_failed_terminal_receipts", lambda: 0)
    monkeypatch.setattr(
        "core.hephaestus.distillation_engine.is_distillation_paused",
        lambda: (_ for _ in ()).throw(OSError("pause db unavailable")),
    )

    with pytest.raises(
        DistillationWorkerCycleError,
        match="distillation_pause_state_unavailable",
    ):
        worker.process_all()


def test_process_all_fails_closed_when_pending_queue_scan_is_unavailable(
    worker, monkeypatch
):
    from core.hephaestus_worker import DistillationWorkerCycleError
    from core.kia import amphora

    monkeypatch.setattr(worker, "reconcile_proposal_tasks", lambda: 0)
    monkeypatch.setattr(worker, "reconcile_terminal_receipts", lambda: 0)
    monkeypatch.setattr(worker, "reconcile_failed_terminal_receipts", lambda: 0)
    monkeypatch.setattr(worker, "_recover_expired_delegations", lambda: None)
    monkeypatch.setattr(
        "core.hephaestus.distillation_engine.is_distillation_paused",
        lambda: False,
    )
    monkeypatch.setattr(
        amphora,
        "list_pending",
        lambda **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("unavailable")),
    )

    with pytest.raises(
        DistillationWorkerCycleError,
        match="amphora_pending_scan_failed",
    ):
        worker.process_all()


def test_process_all_runs_every_maintenance_owner_before_failing_closed(
    worker,
    monkeypatch,
):
    from core.hephaestus_worker import DistillationWorkerCycleError

    calls = []

    def fail_proposal():
        calls.append("proposal")
        raise DistillationWorkerCycleError("trusted_proposal_store_unavailable")

    monkeypatch.setattr(worker, "reconcile_proposal_tasks", fail_proposal)
    monkeypatch.setattr(
        worker,
        "reconcile_terminal_receipts",
        lambda: calls.append("success_terminal") or 0,
    )
    monkeypatch.setattr(
        worker,
        "reconcile_failed_terminal_receipts",
        lambda: calls.append("failed_terminal") or 0,
    )
    monkeypatch.setattr(
        worker,
        "_recover_expired_delegations",
        lambda: calls.append("timeout"),
    )

    with pytest.raises(
        DistillationWorkerCycleError,
        match=(
            "distillation_maintenance_unavailable:"
            "proposal:trusted_proposal_store_unavailable"
        ),
    ):
        worker.process_all()

    assert calls == [
        "proposal",
        "success_terminal",
        "failed_terminal",
        "timeout",
    ]


def test_proposal_store_outage_does_not_consume_task_retry_budget(
    worker, dirs, monkeypatch
):
    from core.hephaestus_worker import DistillationWorkerCycleError
    from core.kia import amphora
    from core.pipeline_receipts import DistillationWriteReceipt

    amphora.enqueue_with_receipt(
        "sess-proposal-outage",
        [{"role": "user", "content": "x"}],
        meta={"source": "test"},
        max_retries=2,
    )
    task = amphora.get_next()
    assert task is not None
    assert amphora.mark_terminal(
        task["task_id"],
        DistillationWriteReceipt(
            status="proposal_pending",
            terminal_reason="awaiting proposal",
            proposal_ids=("proposal-1",),
            expected_count=1,
        ),
        expected_started_at=task["started_at"],
    )
    before = amphora.list_tasks(status="proposal_pending", limit=10)[0]
    monkeypatch.setattr(
        "core.trust.config.load_trusted_push_config",
        lambda **_kwargs: SimpleNamespace(
            db_path=dirs["queue"].parent / "proposal.db"
        ),
    )

    class _UnavailableQueue:
        def __init__(self, *_args, **_kwargs):
            pass

        def get(self, _proposal_id):
            raise sqlite3.OperationalError("proposal db unavailable")

    monkeypatch.setattr(
        "core.trust.proposal_queue.ProposalQueue",
        _UnavailableQueue,
    )

    with pytest.raises(
        DistillationWorkerCycleError,
        match="trusted_proposal_store_unavailable",
    ):
        worker.reconcile_proposal_tasks()

    after = amphora.list_tasks(status="proposal_pending", limit=10)[0]
    assert after["status"] == before["status"] == "proposal_pending"
    assert after["retry_count"] == before["retry_count"] == 0
    assert amphora.list_failed_terminal_receipt_outbox(
        identifier=task["task_id"]
    ) == []


def test_committed_success_terminal_is_not_reinterpreted_when_outbox_replay_fails(
    worker, dirs, monkeypatch
):
    from core.hephaestus_worker import DistillationWorkerCycleError
    from core.kia import amphora
    from core.pipeline_receipts import DistillationWriteReceipt

    pause_db = dirs["queue"].parent / "distillation_state.db"
    monkeypatch.setattr(
        "core.hephaestus.distillation_pause._get_pause_db",
        lambda: pause_db,
    )
    receipt = amphora.enqueue_with_receipt(
        "sess-success-outbox-outage",
        [{"role": "user", "content": "x"}],
        max_retries=1,
    )
    task = amphora.claim_task(receipt.task_id)
    assert task is not None
    engine = SimpleNamespace(
        write_pages_with_receipt=lambda _result: DistillationWriteReceipt(
            status="intentional_skip",
            terminal_reason="reviewed no effect",
        )
    )
    result = SimpleNamespace(
        judgment="skip",
        judgment_reason="reviewed",
        fragments=[],
        cognition_asset_receipt=None,
    )
    monkeypatch.setattr(
        worker,
        "_run_distillation_engine",
        lambda _session_id, _task: (engine, result),
    )
    monkeypatch.setattr(
        worker,
        "reconcile_terminal_receipts",
        lambda **_kwargs: (_ for _ in ()).throw(
            DistillationWorkerCycleError("amphora_terminal_outbox_scan_failed")
        ),
    )
    failure_calls = []
    monkeypatch.setattr(
        worker,
        "_mark_amphora_failed",
        lambda *_args, **_kwargs: failure_calls.append((_args, _kwargs)),
    )

    assert worker.process_one_task(task) is True

    stored = amphora.list_tasks(status="intentional_skip", limit=10)
    assert [item["task_id"] for item in stored] == [receipt.task_id]
    assert stored[0]["retry_count"] == 0
    assert failure_calls == []
    pending = amphora.list_terminal_receipt_outbox(identifier=receipt.task_id)
    assert len(pending) == 1
    assert pending[0]["outbox"]["status"] == "pending"


def test_committed_failed_terminal_survives_secondary_outbox_scan_failure(
    worker, monkeypatch
):
    from core.hephaestus_worker import DistillationWorkerCycleError
    from core.kia import amphora

    receipt = amphora.enqueue_with_receipt(
        "sess-failed-outbox-outage",
        [{"role": "user", "content": "x"}],
        max_retries=1,
    )
    task = amphora.claim_task(receipt.task_id)
    assert task is not None
    monkeypatch.setattr(
        worker,
        "reconcile_failed_terminal_receipts",
        lambda **_kwargs: (_ for _ in ()).throw(
            DistillationWorkerCycleError(
                "amphora_failed_terminal_outbox_scan_failed"
            )
        ),
    )

    transition = worker._mark_amphora_failed(
        receipt.task_id,
        "semantic failure",
        task=task,
    )

    assert transition is not None
    assert transition.terminal is True
    stored = amphora.list_tasks(status="failed", limit=10)
    assert [item["task_id"] for item in stored] == [receipt.task_id]
    pending = amphora.list_failed_terminal_receipt_outbox(
        identifier=receipt.task_id
    )
    assert len(pending) == 1
    assert pending[0]["outbox"]["status"] == "pending"


def test_timeout_watchdog_never_releases_a_live_quarantined_generation(
    worker, monkeypatch
):
    from concurrent.futures import Future
    from core.kia import amphora

    reset_calls = []
    monkeypatch.setattr(
        amphora,
        "reset_timeouts",
        lambda **kwargs: reset_calls.append(kwargs) or 1,
    )
    future = Future()
    runner = threading.Thread(target=lambda: None, daemon=True)
    with worker._late_futures_lock:
        worker._late_futures["task-live"] = (runner, future)
        worker._late_claims["task-live"] = ("task-live", "started-live")

    worker._recover_expired_delegations()

    assert reset_calls == [
        {
            "timeout_minutes": 24 * 60,
            "excluded_claims": (("task-live", "started-live"),),
        }
    ]


def test_timeout_transition_failure_retains_claim_until_retry_commits(
    worker, monkeypatch
):
    from concurrent.futures import Future
    from core.hephaestus_worker import DistillationWorkerCycleError
    from core.kia import amphora

    calls = []

    def transition(identifier, error, *, task=None):
        calls.append((identifier, error, task))
        if len(calls) == 1:
            raise DistillationWorkerCycleError("amphora_failure_transition_failed")
        return SimpleNamespace(terminal=False, task_id=identifier)

    reset_calls = []
    monkeypatch.setattr(worker, "_mark_amphora_failed", transition)
    monkeypatch.setattr(
        amphora,
        "reset_timeouts",
        lambda **kwargs: reset_calls.append(kwargs) or 0,
    )
    future = Future()
    task = {
        "task_id": "task-late-transition",
        "session_id": "sess-late-transition",
        "started_at": "started-late-transition",
    }
    worker._quarantine_timed_out_future(
        runner=threading.Thread(target=lambda: None, daemon=True),
        future=future,
        session_id=task["session_id"],
        identifier=task["task_id"],
        task=task,
        timeout=0.01,
    )

    future.set_result((None, None))
    with worker._late_futures_lock:
        assert "task-late-transition" not in worker._late_futures
        assert worker._late_claims["task-late-transition"] == (
            "task-late-transition",
            "started-late-transition",
        )
        assert "task-late-transition" in worker._late_transition_failures

    worker._recover_expired_delegations()

    assert len(calls) == 2
    with worker._late_futures_lock:
        assert "task-late-transition" not in worker._late_claims
        assert "task-late-transition" not in worker._late_transition_failures
    assert reset_calls == [
        {"timeout_minutes": 24 * 60, "excluded_claims": ()}
    ]


def test_sync_distill_long_task_gets_enough_time_to_complete(worker, monkeypatch):
    """长输入应使用动态任务超时，不能被固定 300s 等价的小 timeout 提前丢弃结果。"""
    import time

    config_values = {
        "distill.task_timeout_seconds": 0.01,
        "distill.task_timeout_medium_seconds": 0.2,
        "distill.task_timeout_long_seconds": 0.3,
        "distill.task_timeout_chunked_seconds": 0.4,
        "distill.response_tokens_short_input_threshold": 10,
        "distill.response_tokens_medium_input_threshold": 1000,
        "distill.token_budget_total": 16000,
        "distill.chunk_std_factor": 3,
    }
    monkeypatch.setattr(
        worker.config,
        "get",
        lambda key, default=None: config_values.get(key, default),
    )

    def slow_success(self, sid, dt):
        time.sleep(0.05)
        from core.pipeline_receipts import DistillationWriteReceipt

        return SimpleNamespace(
            write_pages_with_receipt=lambda _result: DistillationWriteReceipt(
                status="intentional_skip",
                terminal_reason="test completed",
            )
        ), SimpleNamespace(judgment="skip", fragments=[], judgment_reason="test completed")

    monkeypatch.setattr(
        "core.hephaestus_worker.HephaestusWorker._run_distillation_engine",
        slow_success,
    )

    from core.hephaestus.distillation_pause import get_pause_status
    from core.kia import amphora

    amphora.enqueue_with_receipt(
        "sess-long-success",
        [{"role": "user", "content": "important long input " * 50}],
        meta={"source": "test"},
    )
    task = amphora.get_next()

    result = worker.process_one_task(task)

    assert result is True
    assert amphora.get_task_count(status="done") == 1
    assert amphora.get_task_count(status="pending") == 0
    assert amphora.get_task_count(status="processing") == 0
    assert get_pause_status()["paused"] is False


def test_process_all_stops_after_distillation_pause(worker, monkeypatch):
    """单个任务触发暂停后，本轮不应继续拉取后续 pending 任务。"""
    from core.hephaestus.distillation_pause import pause_distillation
    from core.kia import amphora

    pause_db = worker.queue_dir.parent / "distillation_state.db"
    monkeypatch.setattr(
        "core.hephaestus.distillation_pause._get_pause_db",
        lambda: pause_db,
    )

    amphora.enqueue_with_receipt(
        "sess-pause-1", [{"role": "user", "content": "x"}], meta={"source": "test"}
    )
    amphora.enqueue_with_receipt(
        "sess-pause-2", [{"role": "user", "content": "y"}], meta={"source": "test"}
    )

    calls = []

    def pause_after_first(self, task):
        calls.append(task["session_id"])
        pause_distillation(
            reason="api unavailable",
            resume_after=60,
            api_chain_desc="test-chain",
            last_error="boom",
        )
        amphora.mark_failed(task["session_id"], "api unavailable")
        return False

    monkeypatch.setattr(
        "core.hephaestus_worker.HephaestusWorker.process_one_task",
        pause_after_first,
    )

    processed = worker.process_all(max_tasks=2)

    assert processed == 0
    assert calls == ["sess-pause-1"]
    assert amphora.get_task_count(status="processing") == 0
    assert amphora.get_task_count(status="pending") == 2


# ========== 5. 归档 ==========


def test_archive_failed_task_data_writes_json(worker, dirs):
    """_archive_failed_task_data 应在 archive/failed/ 下写入 JSON。"""
    task_data = {"session_id": "sess-fail", "meta": {"source": "test"}}
    original = json.loads(json.dumps(task_data))
    worker._archive_failed_task_data("sess-fail", task_data, "测试失败原因")

    failed_files = list((dirs["archive"] / "failed").glob("task-*.json"))
    assert len(failed_files) == 1
    failed_file = failed_files[0]
    assert failed_file.exists()
    data = json.loads(failed_file.read_text(encoding="utf-8"))
    assert data["session_id"] == "sess-fail"
    assert data["archive_task_id"] == "sess-fail"
    assert data["fail_reason"] == "测试失败原因"
    assert "failed_at" in data
    assert task_data == original


def test_archive_failed_task_never_overwrites_existing_task_evidence(worker, dirs):
    worker._archive_failed_task_data(
        "same-task",
        {"session_id": "same-task"},
        "first failure",
    )
    failed_file = next((dirs["archive"] / "failed").glob("task-*.json"))
    first_bytes = failed_file.read_bytes()

    with pytest.raises(DurableIOError, match="durable_immutable_collision"):
        worker._archive_failed_task_data(
            "same-task",
            {"session_id": "same-task"},
            "second failure",
        )

    assert failed_file.read_bytes() == first_bytes


def test_archive_failed_task_never_follows_existing_symlink(worker, dirs):
    failed_dir = dirs["archive"] / "failed"
    failed_dir.mkdir()
    sentinel = dirs["archive"] / "foreign.json"
    sentinel.write_text('{"sentinel": true}', encoding="utf-8")
    task_id = "../../hostile-task"
    component = hashlib.sha256(
        f"mnemos.failed_task_archive.v1\0{task_id}".encode("utf-8")
    ).hexdigest()
    (failed_dir / f"task-{component}.json").symlink_to(sentinel)

    with pytest.raises(DurableIOError, match="durable_target_unsafe"):
        worker._archive_failed_task_data(task_id, {"session_id": "s"}, "failed")

    assert sentinel.read_text(encoding="utf-8") == '{"sentinel": true}'


# ========== 11. get_stats ==========


def test_get_stats_structure(worker, monkeypatch):
    """get_stats 应返回当前 Worker 统计键。"""
    monkeypatch.setattr(
        "core.hephaestus_worker.HephaestusWorker.get_pending_count",
        lambda self: 5,
    )

    stats = worker.get_stats()
    assert stats["pending"] == 5
    assert "queue_dir" in stats
    assert "output_dir" not in stats
    assert "inbox_dir" in stats
    assert "archive_dir" in stats


# ========== 12. stop ==========


def test_stop_sets_stop_event(worker):
    """stop() 应设置 _stop_event，使 is_set() 返回 True。"""
    assert worker._stop_event.is_set() is False
    worker.stop()
    assert worker._stop_event.is_set() is True


def test_watch_queue_invokes_callback_and_stops(worker, monkeypatch):
    """watch_queue 是运维轮询入口；处理到任务后应调用 callback，并支持优雅停止。"""
    monkeypatch.setattr(
        "core.hephaestus_worker.HephaestusWorker.process_all",
        lambda self: 1,
    )

    processed = []

    def _capture(count):
        processed.append(count)
        worker.stop()

    worker.watch_queue(interval=0, callback=_capture)

    assert processed == [1]
    assert worker._stop_event.is_set() is True


# ========== 13. L1 distilled 标记 ==========


def test_mark_l1_distilled_updates_matching_records(worker, monkeypatch):
    """[P102] _mark_l1_distilled 应调用 backend.update_tags 标记匹配 session 的记录。"""
    updated = []

    class FakeBackend:
        def list_by_tags(self, tags, limit=None):
            assert "session=sess-distilled" in tags
            return [
                MagicMock(uid="raw/2026-06-18/sess-distilled.md"),
                MagicMock(uid="raw/2026-06-18/sess-distilled-2.md"),
            ]

        def update_tags(self, uid, add_tags=None, remove_tags=None):
            updated.append((uid, add_tags))

    worker.__dict__["_backend"] = FakeBackend()
    worker._mark_l1_distilled("sess-distilled")

    assert len(updated) == 2
    assert all("status=distilled" in add for _, add in updated)


def test_mark_l1_distilled_propagates_programming_errors(worker):
    class BuggyBackend:
        def list_by_tags(self, tags, limit=None):
            return [MagicMock(uid="raw/bug.md")]

        def update_tags(self, uid, add_tags=None, remove_tags=None):
            raise AssertionError("backend contract bug")

    worker.__dict__["_backend"] = BuggyBackend()
    with pytest.raises(AssertionError, match="backend contract bug"):
        worker._mark_l1_distilled("sess-bug")
