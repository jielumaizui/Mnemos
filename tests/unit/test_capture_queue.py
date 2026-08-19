# -*- coding: utf-8 -*-
"""
CaptureQueue 单元测试

覆盖项：
- 初始化（数据库、表、索引创建）
- enqueue 入队（成功、重复、背压）
- dequeue 出队（按来源过滤、状态变更 processing）
- dequeue_fair 公平出队（round-robin 配额分配）
- update_status 状态更新（ack / nack 语义）
- get_pending_count / is_duplicate / get_status 状态查询
- reset_processing_to_pending 崩溃恢复
- mark_session_end / get_session_end_markers / clear_session_end_marker
- 持久化：重启后队列状态可恢复
- 线程安全：并发 enqueue / dequeue
"""

from __future__ import annotations

import threading
import time
import sqlite3
from typing import Any, Dict

import pytest

import core.sync_framework.capture_queue as capture_queue_module
from core.sync_framework.capture_queue import (
    CaptureQueue,
    CaptureQueueOperationError,
)
from core.sync_framework.capture_duplicate_policy import CaptureDuplicatePolicy
from core.sync_framework.capture_worker import _payload_to_turn
from core.sync_framework.capture_schema import (
    CaptureQueueSchema,
    CaptureQueueSchemaMigrationRequired,
)


def _open_queue(db_path) -> CaptureQueue:
    """Bootstrap through the explicit schema owner before opening a queue."""
    CaptureQueueSchema.initialize(db_path)
    return CaptureQueue(db_path=str(db_path))


def test_payload_to_turn_prefers_single_top_level_structure_and_strips_legacy_copy():
    payload = {
        "user_content": "u",
        "assistant_content": "a",
        "tool_calls": [],
        "raw_event_refs": [{"event_type": "top-level"}],
        "metadata": {
            "owner": "test",
            "tool_calls": [{"name": "legacy-must-not-win"}],
            "raw_event_refs": [{"event_type": "legacy-must-not-win"}],
        },
    }

    turn = _payload_to_turn(payload, 0)

    assert turn.tool_calls == []
    assert turn.raw_event_refs == [{"event_type": "top-level"}]
    assert turn.metadata == {"owner": "test"}


def test_payload_to_turn_accepts_legacy_metadata_only_structure():
    payload = {
        "user_content": "u",
        "assistant_content": "a",
        "metadata": {
            "owner": "legacy",
            "tool_calls": [{"name": "legacy"}],
        },
    }

    turn = _payload_to_turn(payload, 0)

    assert turn.tool_calls == [{"name": "legacy"}]
    assert turn.metadata == {"owner": "legacy"}


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def queue(tmp_path, monkeypatch, fake_config):
    """提供一个基于临时 SQLite 的 CaptureQueue，测试结束后自动关闭。"""
    import core.config as _config_mod

    monkeypatch.setattr(_config_mod, "get_config", lambda: fake_config)
    db_path = tmp_path / "capture_queue.db"
    q = _open_queue(db_path)
    yield q
    q.close()


@pytest.fixture
def sample_payload() -> Dict[str, Any]:
    """标准测试 payload。"""
    return {"content": "hello", "turn_number": 0}


def _enqueue(
    q: CaptureQueue,
    dedupe_key: str,
    source_agent: str = "claude",
    session_id: str = "sess-1",
    turn_id: str = "turn-1",
    turn_number: int = 0,
    payload: Dict[str, Any] | None = None,
    content_hash: str = "hash-1",
) -> str:
    """辅助函数：封装 enqueue 调用。"""
    return q.enqueue(
        source_agent=source_agent,
        session_id=session_id,
        turn_id=turn_id,
        turn_number=turn_number,
        payload=payload or {"content": "test"},
        content_hash=content_hash,
        raw_revision_id=dedupe_key,
    )


# ---------------------------------------------------------------------------
# 1. 初始化
# ---------------------------------------------------------------------------


class TestInit:
    def test_db_file_created(self, tmp_path, monkeypatch, fake_config):
        """初始化后数据库文件应存在。"""
        import core.config as _config_mod

        monkeypatch.setattr(_config_mod, "get_config", lambda: fake_config)
        db_path = tmp_path / "cq.db"
        assert not db_path.exists()
        with pytest.raises(CaptureQueueSchemaMigrationRequired):
            CaptureQueue(db_path=str(db_path))
        assert not db_path.exists()
        q = _open_queue(db_path)
        assert db_path.exists()
        q.close()

    def test_tables_and_indexes_created(self, queue):
        """初始化后应创建 capture_events、source_backoff、session_end_events 表及索引。"""
        conn = queue._pool.get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {row[0] for row in cursor.fetchall()}
        assert "capture_events" in tables
        assert "source_backoff" in tables
        assert "session_end_events" in tables

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='capture_events'"
        )
        indexes = {row[0] for row in cursor.fetchall()}
        assert "idx_dedupe_key" in indexes
        assert "idx_source_status" in indexes
        assert "idx_session_turn" in indexes
        assert "idx_status" in indexes

    def test_wal_mode_enabled(self, queue):
        """数据库应启用 WAL 模式。"""
        conn = queue._pool.get_conn()
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode")
        assert cursor.fetchone()[0].upper() == "WAL"


# ---------------------------------------------------------------------------
# 2. enqueue 入队
# ---------------------------------------------------------------------------


class TestEnqueue:
    def test_enqueue_success(self, queue):
        """正常入队应返回 'queued'，pending 计数增加。"""
        result = _enqueue(queue, dedupe_key="dk-1")
        assert result == "queued"
        assert queue.get_pending_count() == 1

    def test_enqueue_duplicate_same_key(self, queue):
        """相同 dedupe_key 重复入队应返回 'duplicate'。"""
        _enqueue(queue, dedupe_key="dk-1")
        result = _enqueue(queue, dedupe_key="dk-1")
        assert result == "duplicate"
        assert queue.get_pending_count() == 1

    def test_enqueue_different_keys(self, queue):
        """不同 dedupe_key 应各自成功入队。"""
        assert _enqueue(queue, dedupe_key="dk-a") == "queued"
        assert _enqueue(queue, dedupe_key="dk-b") == "queued"
        assert queue.get_pending_count() == 2

    def test_enqueue_per_source_backpressure(self, tmp_path, monkeypatch, fake_config):
        """单来源 pending 超过阈值时应返回 'backpressure'。"""
        import core.sync_framework.capture_queue as _cq_mod

        fake_config._values["capture.per_source_max_queue_depth"] = 3
        monkeypatch.setattr(_cq_mod, "get_config", lambda: fake_config)

        q = _open_queue(tmp_path / "bp.db")
        for i in range(3):
            assert _enqueue(q, dedupe_key=f"dk-{i}") == "queued"
        assert _enqueue(q, dedupe_key="dk-overflow") == "backpressure"
        assert q.get_pending_count() == 3
        q.close()

    def test_enqueue_global_backpressure(self, tmp_path, monkeypatch, fake_config):
        """全局 pending 超过阈值时应返回 'backpressure'。"""
        import core.sync_framework.capture_queue as _cq_mod

        fake_config._values["capture.max_queue_depth"] = 2
        monkeypatch.setattr(_cq_mod, "get_config", lambda: fake_config)

        q = _open_queue(tmp_path / "bp2.db")
        assert _enqueue(q, dedupe_key="dk-1", source_agent="a") == "queued"
        assert _enqueue(q, dedupe_key="dk-2", source_agent="b") == "queued"
        assert _enqueue(q, dedupe_key="dk-3", source_agent="c") == "backpressure"
        assert q.get_pending_count() == 2
        q.close()

    def test_enqueue_capacity_does_not_trust_stale_process_local_counters(
        self,
        tmp_path,
        monkeypatch,
        fake_config,
    ):
        """A second process cannot exceed or falsely retain a global capacity limit."""
        import core.sync_framework.capture_queue as _cq_mod

        fake_config._values["capture.max_queue_depth"] = 1
        monkeypatch.setattr(_cq_mod, "get_config", lambda: fake_config)
        db_path = tmp_path / "cross-process-capacity.db"
        first = _open_queue(db_path)
        second = CaptureQueue(db_path=str(db_path))
        third = None
        try:
            assert _enqueue(first, dedupe_key="first") == "queued"
            assert second._pending_count == 0  # noqa: SLF001
            assert _enqueue(second, dedupe_key="overflow") == "backpressure"

            third = CaptureQueue(db_path=str(db_path))
            assert third._pending_count == 1  # noqa: SLF001
            assert len(first.dequeue(limit=1)) == 1
            assert third._pending_count == 1  # noqa: SLF001
            assert _enqueue(third, dedupe_key="after-drain") == "queued"
        finally:
            first.close()
            second.close()
            if third is not None:
                third.close()

    def test_counter_does_not_drift_after_dequeue(self, queue):
        """dequeue 后内存计数器应递减，避免排空后仍触发背压。"""
        for i in range(3):
            assert _enqueue(queue, dedupe_key=f"dk-{i}") == "queued"
        assert queue.get_pending_count() == 3

        items = queue.dequeue(limit=10)
        assert len(items) == 3
        assert queue.get_pending_count() == 0

        # 排空后再次入队应成功，而不是被错误背压
        assert _enqueue(queue, dedupe_key="dk-after-dequeue") == "queued"
        assert queue.get_pending_count() == 1

    def test_counter_decrements_on_done(self, queue):
        """update_status 为 done 后 pending 计数器应递减。"""
        assert _enqueue(queue, dedupe_key="dk-1") == "queued"
        items = queue.dequeue(limit=10)
        assert len(items) == 1
        assert queue.get_pending_count() == 0

        queue.update_status(items[0]["id"], "done")
        # done 不增加 pending，计数器保持 0
        assert queue.get_pending_count() == 0

    def test_counter_no_drift_under_mixed_enqueue_dequeue(self, queue):
        """交错 enqueue/dequeue 后内存计数器与实际 DB 一致。"""
        for i in range(5):
            assert _enqueue(queue, dedupe_key=f"dk-{i}") == "queued"

        items = queue.dequeue(limit=3)
        assert len(items) == 3
        assert queue.get_pending_count() == 2

        for i in range(2):
            assert _enqueue(queue, dedupe_key=f"dk-new-{i}") == "queued"
        assert queue.get_pending_count() == 4


# ---------------------------------------------------------------------------
# 3. dequeue 出队
# ---------------------------------------------------------------------------


class TestDequeue:
    def test_dequeue_returns_pending_items(self, queue):
        """dequeue 应返回 pending 项并将其状态改为 processing。"""
        _enqueue(queue, dedupe_key="dk-1", turn_number=1)
        _enqueue(queue, dedupe_key="dk-2", turn_number=2)

        items = queue.dequeue(limit=10)
        assert len(items) == 2
        assert items[0]["turn_number"] == 1
        assert items[1]["turn_number"] == 2
        # 状态已变为 processing
        assert queue.get_pending_count() == 0

    def test_dequeue_by_source_agent(self, queue):
        """按 source_agent 过滤 dequeue 只返回该来源的项。"""
        _enqueue(queue, dedupe_key="dk-a", source_agent="claude")
        _enqueue(queue, dedupe_key="dk-b", source_agent="hermes")

        items = queue.dequeue(source_agent="claude", limit=10)
        assert len(items) == 1
        assert items[0]["source_agent"] == "claude"
        assert queue.get_pending_count("claude") == 0
        assert queue.get_pending_count("hermes") == 1

    def test_dequeue_payload_parsed(self, queue, sample_payload):
        """dequeue 返回的 payload 应被正确反序列化为 dict。"""
        _enqueue(queue, dedupe_key="dk-1", payload=sample_payload)
        items = queue.dequeue(limit=10)
        assert items[0]["payload"] == sample_payload

    def test_dequeue_respects_limit(self, queue):
        """dequeue 应遵守 limit 参数。"""
        for i in range(5):
            _enqueue(queue, dedupe_key=f"dk-{i}")
        items = queue.dequeue(limit=2)
        assert len(items) == 2
        assert queue.get_pending_count() == 3

    def test_dequeue_empty(self, queue):
        """空队列 dequeue 应返回空列表。"""
        assert queue.dequeue(limit=10) == []


# ---------------------------------------------------------------------------
# 4. dequeue_fair 公平出队
# ---------------------------------------------------------------------------


class TestDequeueFair:
    def test_fair_round_robin(self, queue):
        """公平出队应在多个来源间 round-robin 分配配额。"""
        for i in range(4):
            _enqueue(queue, dedupe_key=f"c-{i}", source_agent="claude", turn_number=i)
        for i in range(4):
            _enqueue(queue, dedupe_key=f"h-{i}", source_agent="hermes", turn_number=i)

        items = queue.dequeue_fair(limit=4)
        assert len(items) == 4
        # 2 个来源，limit=4，每个来源配额 = max(1, 4//2) = 2
        claude_items = [it for it in items if it["source_agent"] == "claude"]
        hermes_items = [it for it in items if it["source_agent"] == "hermes"]
        assert len(claude_items) == 2
        assert len(hermes_items) == 2

    def test_fair_fill_remaining(self, queue):
        """round-robin 后若总数不足 limit，应补充剩余项。"""
        _enqueue(queue, dedupe_key="c-1", source_agent="claude")
        _enqueue(queue, dedupe_key="c-2", source_agent="claude")
        _enqueue(queue, dedupe_key="h-1", source_agent="hermes")

        items = queue.dequeue_fair(limit=5)
        assert len(items) == 3

    def test_fair_empty(self, queue):
        """无 pending 项时公平出队返回空列表。"""
        assert queue.dequeue_fair(limit=10) == []


# ---------------------------------------------------------------------------
# 5. update_status（ack / nack 语义）
# ---------------------------------------------------------------------------


class TestUpdateStatus:
    def test_ack_done(self, queue):
        """update_status 为 done 后，事件不再出现在 pending 中。"""
        _enqueue(queue, dedupe_key="dk-1")
        items = queue.dequeue(limit=10)
        event_id = items[0]["id"]

        queue.update_status(event_id, "done")
        # 重新查询状态
        status = queue.get_status("claude", "sess-1")
        assert status is not None
        assert status["status"] == "done"

    def test_nack_failed_with_retry(self, queue):
        """update_status 为 failed 时应增加 retry_count 并记录 error。"""
        _enqueue(queue, dedupe_key="dk-1")
        items = queue.dequeue(limit=10)
        event_id = items[0]["id"]

        queue.update_status(event_id, "failed", error="timeout")
        status = queue.get_status("claude", "sess-1")
        assert status["status"] == "failed"
        assert status["retry_count"] == 1
        assert "timeout" in status["error"]

    def test_update_status_without_error(self, queue):
        """无 error 的 update_status 不应增加 retry_count。"""
        _enqueue(queue, dedupe_key="dk-1")
        items = queue.dequeue(limit=10)
        event_id = items[0]["id"]

        queue.update_status(event_id, "done")
        status = queue.get_status("claude", "sess-1")
        assert status["retry_count"] == 0


# ---------------------------------------------------------------------------
# 6. 状态查询
# ---------------------------------------------------------------------------


class TestStatusQueries:
    def test_get_pending_count_by_source(self, queue):
        """get_pending_count 应按 source_agent 正确过滤。"""
        _enqueue(queue, dedupe_key="dk-1", source_agent="claude")
        _enqueue(queue, dedupe_key="dk-2", source_agent="claude")
        _enqueue(queue, dedupe_key="dk-3", source_agent="hermes")

        assert queue.get_pending_count("claude") == 2
        assert queue.get_pending_count("hermes") == 1
        assert queue.get_pending_count() == 3

    def test_get_status_with_turn_number(self, queue):
        """get_status 支持按 turn_number 精确查询。"""
        _enqueue(queue, dedupe_key="dk-1", turn_number=1)
        _enqueue(queue, dedupe_key="dk-2", turn_number=2)

        status = queue.get_status("claude", "sess-1", turn_number=2)
        assert status is not None
        assert status["turn_number"] == 2

    def test_get_status_not_found(self, queue):
        """查询不存在的记录应返回 None。"""
        assert queue.get_status("claude", "nonexistent") is None

    def test_is_duplicate(self, queue):
        """is_duplicate is derived from the permanent policy receipt."""
        _enqueue(queue, dedupe_key="dk-1")
        present = CaptureDuplicatePolicy.build(
            source_agent="claude", raw_revision_id="dk-1"
        ).value
        missing = CaptureDuplicatePolicy.build(
            source_agent="claude", raw_revision_id="dk-2"
        ).value
        assert queue.is_duplicate(present) is True
        assert queue.is_duplicate(missing) is False

    def test_is_duplicate_is_permanent_after_payload_ages(self, queue):
        """Aged queue payloads do not expire canonical idempotency receipts."""
        _enqueue(queue, dedupe_key="dk-old")
        # 将 created_at 改到 40 天前
        conn = queue._pool.get_conn()
        from datetime import datetime, timedelta

        old_time = (datetime.now() - timedelta(days=40)).isoformat()
        conn.execute(
            "UPDATE capture_events SET created_at = ? WHERE dedupe_key = ?",
            (old_time, "dk-old"),
        )
        conn.commit()
        key = CaptureDuplicatePolicy.build(
            source_agent="claude", raw_revision_id="dk-old"
        ).value
        assert queue.is_duplicate(key) is True


# ---------------------------------------------------------------------------
# 7. 崩溃恢复
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    def test_reset_processing_to_pending(self, queue):
        """reset_processing_to_pending 应将 processing 状态回退为 pending。"""
        _enqueue(queue, dedupe_key="dk-1")
        _enqueue(queue, dedupe_key="dk-2")
        queue.dequeue(limit=10)  # 状态变为 processing
        assert queue.get_pending_count() == 0

        reset = queue.reset_processing_to_pending()
        assert reset == 2
        assert queue.get_pending_count() == 2

        # 再次 dequeue 应能取到
        items = queue.dequeue(limit=10)
        assert len(items) == 2

    def test_reset_zero_when_no_processing(self, queue):
        """无 processing 项时重置应返回 0。"""
        _enqueue(queue, dedupe_key="dk-1")
        assert queue.reset_processing_to_pending() == 0


# ---------------------------------------------------------------------------
# 8. session end 标记
# ---------------------------------------------------------------------------


class TestSessionEnd:
    def test_mark_and_get_session_end(self, queue):
        """mark_session_end 后应能通过 get_session_end_markers 读取。"""
        queue.mark_session_end("claude", "sess-1")
        markers = queue.get_session_end_markers()
        assert len(markers) == 1
        assert markers[0]["source_agent"] == "claude"
        assert markers[0]["session_id"] == "sess-1"

    def test_clear_session_end_marker(self, queue):
        """clear_session_end_marker 后标记应被删除。"""
        queue.mark_session_end("claude", "sess-1")
        queue.clear_session_end_marker("claude", "sess-1")
        assert queue.get_session_end_markers() == []

    def test_mark_session_end_idempotent(self, queue):
        """同一 session 多次 mark 不应产生重复记录。"""
        queue.mark_session_end("claude", "sess-1")
        queue.mark_session_end("claude", "sess-1")
        assert len(queue.get_session_end_markers()) == 1


# ---------------------------------------------------------------------------
# 9. 持久化：重启后状态恢复
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_queue_survives_reopen(self, tmp_path, monkeypatch, fake_config):
        """关闭队列后重新打开，pending 项应仍然存在。"""
        import core.config as _config_mod

        monkeypatch.setattr(_config_mod, "get_config", lambda: fake_config)
        db_path = tmp_path / "persist.db"

        q1 = _open_queue(db_path)
        _enqueue(q1, dedupe_key="dk-1", payload={"k": "v"})
        q1.close()

        q2 = CaptureQueue(db_path=str(db_path))
        assert q2.get_pending_count() == 1
        items = q2.dequeue(limit=10)
        assert items[0]["payload"] == {"k": "v"}
        q2.close()

    def test_processing_state_survives_reopen(self, tmp_path, monkeypatch, fake_config):
        """processing 状态在重新打开后可通过 reset_processing_to_pending 恢复。"""
        import core.config as _config_mod

        monkeypatch.setattr(_config_mod, "get_config", lambda: fake_config)
        db_path = tmp_path / "persist2.db"

        q1 = _open_queue(db_path)
        _enqueue(q1, dedupe_key="dk-1")
        q1.dequeue(limit=10)
        q1.close()

        q2 = CaptureQueue(db_path=str(db_path))
        assert q2.get_pending_count() == 0
        assert q2.reset_processing_to_pending() == 1
        assert q2.get_pending_count() == 1
        q2.close()


# ---------------------------------------------------------------------------
# 10. 线程安全
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_enqueue(self, queue):
        """多线程并发 enqueue 应全部成功且无重复冲突。"""
        results = []
        lock = threading.Lock()

        def worker(idx: int):
            r = _enqueue(queue, dedupe_key=f"dk-{idx}")
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r == "queued" for r in results)
        assert queue.get_pending_count() == 50

    def test_concurrent_dequeue(self, queue):
        """多线程并发 dequeue 不应出现重复消费（race condition）。"""
        for i in range(20):
            _enqueue(queue, dedupe_key=f"dk-{i}")

        all_items = []
        lock = threading.Lock()

        def worker():
            items = queue.dequeue(limit=5)
            with lock:
                all_items.extend(items)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 所有被取出的 id 应唯一（无重复消费）
        ids = [it["id"] for it in all_items]
        assert len(ids) == len(set(ids))
        # 总共取出的不应超过 20
        assert len(all_items) <= 20
        # pending 应为 20 - 取出的数量
        assert queue.get_pending_count() == 20 - len(all_items)

    @pytest.mark.parametrize(
        "claim",
        (
            lambda queue: queue.dequeue(limit=1),
            lambda queue: queue.dequeue_fair(limit=1),
            lambda queue: queue.dequeue_by_session(
                "claude",
                "cross-process",
                limit=1,
            ),
        ),
    )
    def test_two_queue_instances_never_claim_the_same_event(
        self,
        tmp_path,
        monkeypatch,
        fake_config,
        claim,
    ):
        """The SQLite claim transaction, not one Python lock, owns exclusivity."""
        import core.config as _config_mod

        monkeypatch.setattr(_config_mod, "get_config", lambda: fake_config)
        db_path = tmp_path / "cross-process-claim.db"
        first_queue = _open_queue(db_path)
        second_queue = CaptureQueue(db_path=str(db_path))
        try:
            assert (
                _enqueue(
                    first_queue,
                    dedupe_key="cross-process-revision",
                    session_id="cross-process",
                )
                == "queued"
            )
            first_selected = threading.Event()
            release_first = threading.Event()
            load_lock = threading.Lock()
            load_count = 0
            original_loads = capture_queue_module.json.loads

            def pause_first_claim(value):
                nonlocal load_count
                with load_lock:
                    load_count += 1
                    current = load_count
                if current == 1:
                    first_selected.set()
                    assert release_first.wait(timeout=5)
                return original_loads(value)

            monkeypatch.setattr(capture_queue_module.json, "loads", pause_first_claim)
            claimed: list[int] = []
            errors: list[BaseException] = []

            def worker(queue_instance):
                try:
                    claimed.extend(
                        int(item["id"]) for item in claim(queue_instance)
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            first = threading.Thread(target=worker, args=(first_queue,))
            second = threading.Thread(target=worker, args=(second_queue,))
            first.start()
            assert first_selected.wait(timeout=5)
            second.start()
            time.sleep(0.1)
            release_first.set()
            first.join(timeout=5)
            second.join(timeout=5)

            assert not first.is_alive()
            assert not second.is_alive()
            assert errors == []
            assert len(claimed) == 1
            assert len(set(claimed)) == 1
        finally:
            first_queue.close()
            second_queue.close()

    def test_enqueue_during_dequeue(self, queue):
        """一个线程 enqueue 同时另一个线程 dequeue，状态应保持一致。"""
        _enqueue(queue, dedupe_key="dk-0")
        dequeue_results = []
        enqueue_results = []

        def enqueue_worker():
            for i in range(20):
                r = _enqueue(queue, dedupe_key=f"dk-{i+1}")
                enqueue_results.append(r)
                time.sleep(0.001)

        def dequeue_worker():
            for _ in range(10):
                items = queue.dequeue(limit=2)
                dequeue_results.extend(items)
                time.sleep(0.002)

        t1 = threading.Thread(target=enqueue_worker)
        t2 = threading.Thread(target=dequeue_worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # enqueue 全部成功
        assert all(r == "queued" for r in enqueue_results)
        # dequeue 取出的 id 唯一
        ids = [it["id"] for it in dequeue_results]
        assert len(ids) == len(set(ids))
        # 最终 pending 数量 = 初始 1 + enqueue 20 - dequeue 取出数
        assert queue.get_pending_count() == 21 - len(dequeue_results)


# ---------------------------------------------------------------------------
# 11. dequeue_by_session
# ---------------------------------------------------------------------------


class TestDequeueBySession:
    def test_dequeue_by_session_filters_correctly(self, queue):
        """dequeue_by_session 应只返回指定 source_agent + session_id 的项。"""
        _enqueue(queue, dedupe_key="dk-1", source_agent="claude", session_id="sess-a")
        _enqueue(queue, dedupe_key="dk-2", source_agent="claude", session_id="sess-b")
        _enqueue(queue, dedupe_key="dk-3", source_agent="hermes", session_id="sess-a")

        items = queue.dequeue_by_session("claude", "sess-a", limit=10)
        assert len(items) == 1
        assert items[0]["session_id"] == "sess-a"
        assert items[0]["source_agent"] == "claude"
        assert queue.get_pending_count("claude") == 1  # sess-b 仍在 pending

    def test_dequeue_by_session_changes_status(self, queue):
        """dequeue_by_session 应将取出的项状态改为 processing。"""
        _enqueue(queue, dedupe_key="dk-1", session_id="sess-x")
        queue.dequeue_by_session("claude", "sess-x", limit=10)
        assert queue.get_pending_count() == 0


class TestDequeueFailures:
    @pytest.mark.parametrize(
        "operation",
        (
            lambda queue: queue.dequeue(limit=1),
            lambda queue: queue.dequeue_fair(limit=1),
            lambda queue: queue.dequeue_by_session("claude", "session", limit=1),
        ),
    )
    def test_corrupt_payload_fails_closed_without_claiming_the_event(
        self,
        queue,
        operation,
    ):
        assert _enqueue(queue, dedupe_key="corrupt", session_id="session") == "queued"
        conn = queue._pool.get_conn()  # noqa: SLF001
        update = conn.execute(
            "UPDATE capture_events SET payload_json='[not-a-mapping]' "
            "WHERE raw_revision_id='corrupt'"
        )
        assert update.rowcount == 1
        conn.commit()

        with pytest.raises(
            CaptureQueueOperationError,
            match="capture_queue_payload_",
        ):
            operation(queue)

        assert conn.in_transaction is False
        status = conn.execute(
            "SELECT status FROM capture_events WHERE raw_revision_id='corrupt'"
        ).fetchone()
        assert status is not None
        assert status["status"] == "pending"

    @pytest.mark.parametrize(
        "operation",
        (
            lambda queue: queue.dequeue(limit=1),
            lambda queue: queue.dequeue_fair(limit=1),
            lambda queue: queue.dequeue_by_session("claude", "session", limit=1),
        ),
    )
    def test_database_failure_is_not_reported_as_an_empty_queue(
        self,
        queue,
        monkeypatch,
        operation,
    ):
        monkeypatch.setattr(
            queue._pool,  # noqa: SLF001
            "get_conn",
            lambda: (_ for _ in ()).throw(
                sqlite3.OperationalError("synthetic queue outage")
            ),
        )

        with pytest.raises(
            CaptureQueueOperationError,
            match="capture_queue_dequeue_failed",
        ):
            operation(queue)

    @pytest.mark.parametrize(
        ("operation", "error_code"),
        (
            (
                lambda queue: queue.get_pending_count(),
                "capture_queue_pending_count_failed",
            ),
            (
                lambda queue: queue.get_pending_counts_by_source(),
                "capture_queue_pending_counts_failed",
            ),
            (
                lambda queue: queue.get_status("claude", "session"),
                "capture_queue_status_read_failed",
            ),
            (
                lambda queue: queue.is_duplicate("idempotency"),
                "capture_queue_idempotency_read_failed",
            ),
            (
                lambda queue: queue.reset_processing_to_pending(),
                "capture_queue_recovery_failed",
            ),
            (
                lambda queue: queue.get_backoff_state("claude"),
                "capture_queue_backoff_read_failed",
            ),
        ),
    )
    def test_read_failure_is_not_converted_to_a_false_default(
        self,
        queue,
        monkeypatch,
        operation,
        error_code,
    ):
        monkeypatch.setattr(
            queue._pool,  # noqa: SLF001
            "get_conn",
            lambda: (_ for _ in ()).throw(
                sqlite3.OperationalError("synthetic queue outage")
            ),
        )

        with pytest.raises(CaptureQueueOperationError, match=error_code):
            operation(queue)


# ---------------------------------------------------------------------------
# 12. backoff state
# ---------------------------------------------------------------------------


class TestBackoffState:
    def test_set_and_get_backoff(self, queue):
        """set_backoff_state / get_backoff_state 应正确读写。"""
        queue.set_backoff_state("claude", error_count=3, last_retry_at="2024-01-01T00:00:00")
        state = queue.get_backoff_state("claude")
        assert state["error_count"] == 3
        assert state["last_retry_at"] == "2024-01-01T00:00:00"

    def test_get_backoff_default(self, queue):
        """未设置过 backoff 的来源应返回默认值。"""
        state = queue.get_backoff_state("unknown")
        assert state["error_count"] == 0
        assert state["last_retry_at"] is None

    def test_clear_backoff(self, queue):
        """clear_backoff_state 后应恢复默认值。"""
        queue.set_backoff_state("claude", error_count=5, last_retry_at="2024-01-01T00:00:00")
        queue.clear_backoff_state("claude")
        state = queue.get_backoff_state("claude")
        assert state["error_count"] == 0
        assert state["last_retry_at"] is None
