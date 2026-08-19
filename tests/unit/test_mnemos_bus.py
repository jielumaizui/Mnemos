"""
EventBus / CaptureQueue 单元测试

覆盖项：
1. publish() / subscribe() — 事件发布与投递
2. stats() / _mark_done — 队列状态管理（通过公共接口验证）
3. CaptureQueue.enqueue() / dequeue() / update_status() — 采集队列操作
4. _recover_pending() — 启动时恢复 pending 事件（通过公共行为验证）
5. 错误处理 — 处理器抛异常时的重试与死信队列行为
"""

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

# ---- FakeConfig ----


class _FakeConfig:
    def __init__(
        self,
        tmpdir: Path,
        dispatch_workers: int = 1,
        handler_timeout_seconds: float = 0,
    ):
        self._tmpdir = tmpdir
        self.data_dir = tmpdir / "data"
        self.database_dir = self.data_dir
        # 每个测试使用唯一的 mnemos_dir，避免 SQLite WAL 文件冲突
        self.mnemos_dir = tmpdir / ".mnemos" / str(uuid.uuid4())[:8]
        self._values = {
            "event_bus.max_retries": 3,
            "event_bus.queue_depth_alert": 1000,
            "event_bus.max_queue_depth": 10000,
            "event_bus.max_recover_events": 1000,
            "event_bus.dead_letter_alert": 10,
            "event_bus.dead_letter_max": 1000,
            "event_bus.max_latency_ms": 10,
            "capture.max_queue_depth": 10000,
            "capture.per_source_max_queue_depth": 1000,
            "capture.max_workers": 2,
            "capture.per_source_concurrency": 1,
            "capture.max_batch_per_tick": 50,
            "capture.tick_interval_seconds": 1,
            "capture.max_payload_bytes": 200000,
            "event_bus.dispatch_workers": dispatch_workers,
            "event_bus.handler_timeout_seconds": handler_timeout_seconds,
            "event_bus.retry_base_seconds": 0,
            "event_bus.retry_max_seconds": 0,
        }

    def get(self, key, default=None):
        return self._values.get(key, default)


def _make_handler(name="handler", side_effect=None):
    """创建带 __name__ 的模拟处理器，兼容 subscribe 的日志输出。"""

    def handler(event):
        if side_effect:
            raise side_effect

    handler.__name__ = name
    return handler


@pytest.fixture
def fake_config(tmp_path):
    """提供隔离的 FakeConfig，测试结束后自动清理。"""
    cfg = _FakeConfig(tmp_path)
    yield cfg


@pytest.fixture
def patched_get_config(monkeypatch, fake_config):
    """将 core.config.get_config 和 core.mnemos_bus.get_config 替换为返回 fake_config 的 stub。

    注意：mnemos_bus.py 使用 `from core.config import get_config` 导入，
    因此必须同时 patch core.mnemos_bus.get_config 才能生效。
    """
    import core.config as _config_mod

    monkeypatch.setattr(_config_mod, "get_config", lambda: fake_config)
    monkeypatch.setattr("core.mnemos_bus.get_config", lambda: fake_config)
    monkeypatch.setattr("core.sync_framework.capture_queue.get_config", lambda: fake_config)
    return fake_config


@pytest.fixture
def event_bus(patched_get_config):
    """创建隔离的 EventBus 实例，使用临时目录。"""
    from core.mnemos_bus import EventBus

    bus = EventBus()
    yield bus
    bus.close()


@pytest.fixture
def capture_queue(patched_get_config):
    """创建隔离的 CaptureQueue 实例，使用临时数据库。"""
    from core.sync_framework.capture_queue import CaptureQueue
    from core.sync_framework.capture_schema import CaptureQueueSchema

    CaptureQueueSchema.initialize(patched_get_config.database_dir / "capture_queue.db")
    queue = CaptureQueue()
    yield queue
    queue.close()


def _enqueue_capture(queue, **kwargs):
    """Give legacy fixture labels an explicit canonical Raw revision identity."""
    raw_revision_id = kwargs.pop("dedupe_key")
    return queue.enqueue(raw_revision_id=f"rawrev-test-{raw_revision_id}", **kwargs)


def test_event_bus_accepts_explicit_runtime_config_for_all_durable_paths(tmp_path, monkeypatch):
    from core.mnemos_bus import EventBus

    config = _FakeConfig(tmp_path)
    monkeypatch.setattr(
        "core.mnemos_bus.resolve_wiki_projection_db_path",
        lambda runtime_config: runtime_config.database_dir / "wiki_projection.db",
    )
    bus = EventBus(config=config)
    try:
        assert bus._db_path == config.mnemos_dir / "events.db"
        assert bus._projection_db_path == config.database_dir / "wiki_projection.db"
    finally:
        bus.close()


def test_event_bus_accepts_explicit_projection_ledger_path(tmp_path, monkeypatch):
    from core.mnemos_bus import EventBus

    config = _FakeConfig(tmp_path)
    explicit = config.database_dir / "wiki_projection.db"
    monkeypatch.setattr(
        "core.mnemos_bus.resolve_wiki_projection_db_path",
        lambda runtime_config: tmp_path / "unexpected-global" / "wiki_projection.db",
    )

    bus = EventBus(
        config=config,
        projection_db_path=explicit,
        run_startup_maintenance=False,
        recover_pending=False,
    )
    try:
        assert bus.projection_db_path == explicit
        assert not (tmp_path / "unexpected-global").exists()
    finally:
        bus.close()


def test_event_bus_rejects_projection_ledger_outside_config_database(tmp_path):
    from core.mnemos_bus import EventBus

    config = _FakeConfig(tmp_path)

    with pytest.raises(ValueError, match="must match config.database_dir"):
        EventBus(
            config=config,
            projection_db_path=tmp_path / "other" / "wiki_projection.db",
            run_startup_maintenance=False,
            recover_pending=False,
        )


def test_event_bus_reconciliation_mode_skips_retention_and_pending_recovery(
    patched_get_config,
):
    from core.mnemos_bus import Event, EventBus

    first = EventBus(config=patched_get_config)
    try:
        pending_trace = first.publish(
            Event("wiki_page_updated", "test", {"mutation_id": "pending"})
        )
        done_trace = first.publish(Event("wiki_page_updated", "test", {"mutation_id": "done"}))
        with sqlite3.connect(first._db_path) as conn:
            conn.execute(
                "UPDATE events SET created_at='2000-01-01T00:00:00+00:00' "
                "WHERE trace_id IN (?, ?)",
                (pending_trace, done_trace),
            )
            conn.execute(
                "UPDATE events SET status='done' WHERE trace_id=?",
                (done_trace,),
            )
            conn.commit()
    finally:
        first.close()

    reopened = EventBus(
        config=patched_get_config,
        run_startup_maintenance=False,
        recover_pending=False,
        enqueue_published_events=False,
    )
    try:
        new_trace = reopened.publish(
            Event("wiki_page_updated", "test", {"mutation_id": "persist-only"})
        )
        with sqlite3.connect(reopened._db_path) as conn:
            rows = dict(
                conn.execute(
                    "SELECT trace_id, status FROM events " "WHERE trace_id IN (?, ?)",
                    (pending_trace, done_trace),
                )
            )
        assert rows == {pending_trace: "pending", done_trace: "done"}
        with sqlite3.connect(reopened._db_path) as conn:
            assert (
                conn.execute("SELECT status FROM events WHERE trace_id=?", (new_trace,)).fetchone()[
                    0
                ]
                == "pending"
            )
        assert reopened._queue.qsize() == 0
    finally:
        reopened.close()


def test_global_event_bus_rejects_a_conflicting_explicit_runtime_config(tmp_path):
    from core.mnemos_bus import get_event_bus, reset_event_bus

    first_config = _FakeConfig(tmp_path / "first")
    second_config = _FakeConfig(tmp_path / "second")
    reset_event_bus()
    try:
        first = get_event_bus(config=first_config)
        assert first._db_path == first_config.mnemos_dir / "events.db"
        with pytest.raises(RuntimeError, match="different durable event database"):
            get_event_bus(config=second_config)
    finally:
        reset_event_bus()


@pytest.fixture
def parallel_event_bus(tmp_path, monkeypatch):
    """创建启用并行分发的 EventBus 实例（4 workers），测试结束后自动停止。"""
    import core.config as _config_mod
    from core.mnemos_bus import EventBus

    cfg = _FakeConfig(tmp_path, dispatch_workers=4)
    monkeypatch.setattr(_config_mod, "get_config", lambda: cfg)
    monkeypatch.setattr("core.mnemos_bus.get_config", lambda: cfg)
    bus = EventBus()
    bus.start_dispatch()
    yield bus
    bus.stop_dispatch()
    bus.close()


@pytest.fixture
def timeout_event_bus(tmp_path, monkeypatch):
    """创建启用 handler 超时的 EventBus 实例（timeout=0.1s）。"""
    import core.config as _config_mod
    from core.mnemos_bus import EventBus

    cfg = _FakeConfig(tmp_path, dispatch_workers=2, handler_timeout_seconds=0.1)
    monkeypatch.setattr(_config_mod, "get_config", lambda: cfg)
    monkeypatch.setattr("core.mnemos_bus.get_config", lambda: cfg)
    bus = EventBus()
    bus.start_dispatch()
    yield bus
    bus.stop_dispatch()
    bus.close()


def test_event_types_cover_runtime_routing_sets():
    from core.mnemos_bus import EVENT_TYPES, EventBus

    declared = set(EVENT_TYPES)

    assert len(EVENT_TYPES) == len(declared)
    assert EventBus._PERSISTENT_EVENT_TYPES <= declared
    assert EventBus._NO_PERSIST_EVENT_TYPES <= declared


# ============================================================
# EventBus 测试
# ============================================================


class TestEventBusPublishSubscribe:
    """测试事件发布与订阅。"""

    def test_eventbus_falls_back_from_magicmock_mnemos_dir_to_database_dir(
        self, tmp_path, monkeypatch
    ):
        """配置 mock 漏设 mnemos_dir 时不应在仓库根目录创建 MagicMock SQLite 文件。"""
        from core.mnemos_bus import EventBus

        fake_cfg = Mock()
        fake_cfg.mnemos_dir = MagicMock(name="mock.mnemos_dir")
        fake_cfg.database_dir = tmp_path / "db"
        fake_cfg.get = lambda key, default=None: default

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("core.mnemos_bus.get_config", lambda: fake_cfg)

        bus = EventBus()
        try:
            assert bus._db_path == tmp_path / "db" / "events.db"
            assert bus.root == tmp_path / "db" / "events"
            assert not list(tmp_path.glob("*MagicMock*"))
        finally:
            bus.close()

    def test_publish_persists_event_to_sqlite(self, event_bus):
        """发布事件后，SQLite 中应存在 pending 状态记录。"""
        trace_id = event_bus.publish("session.start", payload={"user": "test"})

        conn = event_bus._get_conn()
        row = conn.execute("SELECT * FROM events WHERE trace_id = ?", (trace_id,)).fetchone()

        assert row is not None
        assert row["event_type"] == "session.start"
        assert row["status"] == "pending"
        assert json.loads(row["payload_json"]) == {"user": "test"}

    def test_publish_returns_trace_id(self, event_bus):
        """publish() 应返回事件的 trace_id。"""
        trace_id = event_bus.publish("system_alert", payload={"msg": "hello"})
        assert isinstance(trace_id, str)
        assert len(trace_id) > 0

    def test_publish_puts_event_in_memory_queue(self, event_bus):
        """发布事件后，事件应进入内存队列。"""
        initial_qsize = event_bus._queue.qsize()
        event_bus.publish("session.start", payload={"key": "val"})
        assert event_bus._queue.qsize() == initial_qsize + 1

    def test_subscribe_registers_handler(self, event_bus):
        """subscribe() 应正确注册事件处理器。"""
        handler = _make_handler("my_handler")
        event_bus.subscribe("session.start", handler)

        assert "session.start" in event_bus._handlers
        assert handler in event_bus._handlers["session.start"]

    def test_subscribe_wildcard_handler(self, event_bus):
        """subscribe("*") 应注册通配符处理器。"""
        handler = _make_handler("wildcard_handler")
        event_bus.subscribe("*", handler)

        assert "*" in event_bus._handlers
        assert handler in event_bus._handlers["*"]

    def test_publish_with_force_persists_even_without_handler(self, event_bus):
        """force=True 时，即使无消费者也应持久化到 events 表。"""
        trace_id = event_bus.publish("custom_event", payload={}, force=True)

        conn = event_bus._get_conn()
        row = conn.execute("SELECT * FROM events WHERE trace_id = ?", (trace_id,)).fetchone()

        assert row is not None
        assert row["event_type"] == "custom_event"

    def test_publish_no_handler_non_persistent_goes_to_dead_letter(self, event_bus):
        """无消费者且非必须持久化的事件应进入死信队列。"""
        trace_id = event_bus.publish("unknown_event", payload={"x": 1})

        conn = event_bus._get_conn()
        row = conn.execute("SELECT * FROM dead_letters WHERE trace_id = ?", (trace_id,)).fetchone()

        assert row is not None
        assert row["event_type"] == "unknown_event"
        assert row["status"] == "no_consumer"

    def test_publish_no_handler_telemetry_dropped_not_dead_letter(self, event_bus):
        """[P005] 遥测/广播类无消费者事件应直接丢弃，不进入死信队列。"""
        trace_id = event_bus.publish("distillation_progress", payload={"step": 1})

        conn = event_bus._get_conn()
        row = conn.execute("SELECT * FROM dead_letters WHERE trace_id = ?", (trace_id,)).fetchone()

        assert row is None

    def test_publish_guard_alert_no_handler_dropped_not_dead_letter(self, event_bus):
        """[Iter6] guard_alert 为 telemetry 事件，无消费者时不应进入死信队列。"""
        trace_id = event_bus.publish(
            "guard_alert",
            payload={
                "level": "hint",
                "checklist_item": "test_item",
                "triggered_by": "user",
                "trigger_text": "test",
                "session_id": "coding",
            },
        )

        conn = event_bus._get_conn()
        row = conn.execute("SELECT * FROM dead_letters WHERE trace_id = ?", (trace_id,)).fetchone()

        assert row is None

    def test_publish_event_telemetry_skips_bus_init_without_global_consumers(self, monkeypatch):
        """便捷 publish_event 发布无消费者 telemetry 时不应为丢弃事件初始化 EventBus。"""
        import core.mnemos_bus as bus_mod

        class RaisingEventBus:
            _NO_PERSIST_EVENT_TYPES = {"guard_alert"}

            def __init__(self):
                raise AssertionError("EventBus should not be initialized for dropped telemetry")

        monkeypatch.setattr(bus_mod, "_global_bus", None)
        monkeypatch.setattr(bus_mod, "EventBus", RaisingEventBus)

        bus_mod.publish_event(
            "guard_alert",
            "aegis",
            {
                "level": "hint",
                "checklist_item": "test_item",
                "triggered_by": "user",
                "trigger_text": "test",
                "session_id": "coding",
            },
        )

    def test_publish_immune_auto_fix_no_handler_dropped_not_dead_letter(self, event_bus):
        """[P0] immune.auto_fix 为自动修复审计日志，无消费者时不应进入死信队列。"""
        trace_id = event_bus.publish(
            "immune.auto_fix",
            payload={
                "actions": ["为 'test.md' 自动发现 3 个关系"],
                "source": "KnowledgeImmuneSystem.auto_fix",
            },
        )

        conn = event_bus._get_conn()
        row = conn.execute("SELECT * FROM dead_letters WHERE trace_id = ?", (trace_id,)).fetchone()

        assert row is None

    def test_publish_polled_no_handler_persisted_not_dead_letter(self, event_bus):
        """[Audit] polled 为同步轮询审计事件，无消费者时应持久化到 events，不进死信。"""
        trace_id = event_bus.publish(
            "polled",
            payload={
                "file_path": "/tmp/test.json",
                "session_id": "sess-123",
            },
        )

        conn = event_bus._get_conn()
        dead_row = conn.execute(
            "SELECT * FROM dead_letters WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        event_row = conn.execute("SELECT * FROM events WHERE trace_id = ?", (trace_id,)).fetchone()

        assert dead_row is None
        assert event_row is not None
        assert event_row["event_type"] == "polled"
        assert event_row["status"] == "pending"

    def test_publish_polled_with_handler_delivered(self, event_bus):
        """[Audit] polled 事件有消费者时应正常投递。"""
        from core.mnemos_bus import Event

        received = []

        def handler(event):
            received.append(event)

        handler.__name__ = "polled_handler"

        event_bus.subscribe("polled", handler)
        event = Event(
            event_type="polled",
            source="sync_engine",
            payload={"file_path": "/tmp/test.json", "session_id": "sess-123"},
        )
        event_bus._dispatch_event(event)

        assert len(received) == 1
        assert received[0].event_type == "polled"
        assert received[0].payload["session_id"] == "sess-123"

    def test_publish_typed_event_preserves_source(self, event_bus):
        """Typed Event is the only source-bearing publication contract."""
        from core.mnemos_bus import Event

        trace_id = event_bus.publish(Event("session.start", "claude", {"key": "val"}))

        conn = event_bus._get_conn()
        row = conn.execute("SELECT * FROM events WHERE trace_id = ?", (trace_id,)).fetchone()

        assert row is not None
        assert row["event_type"] == "session.start"
        assert row["source"] == "claude"
        assert json.loads(row["payload_json"]) == {"key": "val"}

    def test_non_wiki_mutation_id_does_not_touch_projection_ledger(self, event_bus):
        """A generic business mutation_id must remain outside the Wiki adapter."""

        event_bus.subscribe("session.start", lambda _event: None)
        trace_id = event_bus.publish("session.start", payload={"mutation_id": "business-mutation"})
        event_bus._dispatch_event(event_bus._queue.get_nowait())

        row = (
            event_bus._get_conn()
            .execute("SELECT status FROM events WHERE trace_id=?", (trace_id,))
            .fetchone()
        )
        assert row["status"] == "done"
        from core.wiki_projection_lifecycle import WikiProjectionLedger

        assert WikiProjectionLedger(event_bus._projection_db_path).list_mutations() == []

    def test_wiki_dispatch_uses_authoritative_ledger_payload(self, event_bus, tmp_path):
        """Consumers must see the durable mutation row, not an incomplete event payload."""
        from core.wiki_projection_lifecycle import WikiProjectionLedger

        page = tmp_path / "page.md"
        page.write_text("# Authoritative\n", encoding="utf-8")
        ledger = WikiProjectionLedger(event_bus._projection_db_path)
        assert event_bus._projection_db_path.parent != event_bus._db_path.parent
        receipt = ledger.record_mutation(page, mutation_type="create")
        received = []

        def handler(event):
            received.append(event.payload)

        event_bus.subscribe("wiki_page_updated", handler)
        from core.mnemos_bus import Event

        trace_id = event_bus.publish(
            Event(
                "wiki_page_updated",
                "test",
                {"mutation_id": receipt.mutation_id},
                trace_id=receipt.mutation_id,
            )
        )
        event_bus._dispatch_event(event_bus._queue.get_nowait())

        assert received == [
            {
                "mutation_id": receipt.mutation_id,
                "page_id": receipt.page_id,
                "page_revision": receipt.page_revision,
                "page_path": receipt.page_path,
                "previous_path": "",
                "mutation_type": "create",
                "tombstone": False,
                "update_type": "create",
            }
        ]
        row = (
            event_bus._get_conn()
            .execute("SELECT status FROM events WHERE trace_id=?", (trace_id,))
            .fetchone()
        )
        assert row["status"] == "done"
        with sqlite3.connect(str(event_bus._projection_db_path)) as projection_conn:
            projection_receipt = projection_conn.execute(
                "SELECT consumer, outcome FROM projection_receipts WHERE mutation_id=?",
                (receipt.mutation_id,),
            ).fetchone()
        assert projection_receipt is not None
        assert projection_receipt[1] == "ack"
        assert (
            ledger.reconciliation_report(required_consumers=(projection_receipt[0],))["ok"] is True
        )

    @pytest.mark.parametrize("unknown", [False, True])
    def test_wiki_dispatch_rejects_untrusted_mutation_identity(self, event_bus, tmp_path, unknown):
        """Unknown or mismatched identity must die before any consumer side effect."""
        from core.wiki_projection_lifecycle import WikiProjectionLedger

        page = tmp_path / "page.md"
        page.write_text("# Trusted\n", encoding="utf-8")
        ledger = WikiProjectionLedger(event_bus._projection_db_path)
        receipt = ledger.record_mutation(page, mutation_type="create")
        calls = []

        def handler(event):
            calls.append(event)

        event_bus.subscribe("wiki_page_updated", handler)
        payload = {
            "mutation_id": "missing-mutation" if unknown else receipt.mutation_id,
            "page_path": str(tmp_path / "forged.md"),
        }
        from core.mnemos_bus import Event

        trace_id = event_bus.publish(
            Event(
                "wiki_page_updated",
                "test",
                payload,
                trace_id=("missing-mutation" if unknown else receipt.mutation_id),
            )
        )
        event_bus._dispatch_event(event_bus._queue.get_nowait())

        assert calls == []
        conn = event_bus._get_conn()
        dead = conn.execute(
            "SELECT failure_reason FROM dead_letters WHERE trace_id=?", (trace_id,)
        ).fetchone()
        assert dead is not None
        expected = "unknown Wiki mutation_id" if unknown else "payload mismatch"
        assert expected in dead["failure_reason"]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM handler_receipts WHERE trace_id=?", (trace_id,)
            ).fetchone()[0]
            == 0
        )

    def test_wiki_dispatch_rejects_forged_trace_for_valid_mutation(self, event_bus, tmp_path):
        from core.mnemos_bus import Event
        from core.wiki_projection_lifecycle import WikiProjectionLedger

        page = tmp_path / "page.md"
        page.write_text("# Trusted", encoding="utf-8")
        receipt = WikiProjectionLedger(event_bus._projection_db_path).record_mutation(
            page, mutation_type="create"
        )
        calls = []
        event_bus.subscribe("wiki_page_updated", lambda event: calls.append(event))
        trace_id = event_bus.publish(
            Event(
                "wiki_page_updated",
                "forged",
                {"mutation_id": receipt.mutation_id},
                trace_id="forged-trace",
            )
        )
        event_bus._dispatch_event(event_bus._queue.get_nowait())

        assert calls == []
        reason = (
            event_bus._get_conn()
            .execute("SELECT failure_reason FROM dead_letters WHERE trace_id=?", (trace_id,))
            .fetchone()[0]
        )
        assert "trace_id must equal mutation_id" in reason


class TestEventBusDispatch:
    """测试事件分发与状态管理。"""

    def test_dispatch_event_calls_handler(self, event_bus):
        """_dispatch_event 应调用匹配的处理器。"""
        called_with = []

        def handler(event):
            called_with.append(event)

        handler.__name__ = "test_handler"

        event_bus.subscribe("session.start", handler)

        from core.mnemos_bus import Event

        event = Event(event_type="session.start", source="test", payload={"x": 1})
        event_bus._dispatch_event(event)

        assert len(called_with) == 1
        assert called_with[0].event_type == "session.start"

    def test_dispatch_event_marks_done_on_success(self, event_bus):
        """处理器成功执行后，事件应标记为 done。"""

        def handler(event):
            pass

        handler.__name__ = "success_handler"

        event_bus.subscribe("session.start", handler)

        from core.mnemos_bus import Event

        event = Event(event_type="session.start", source="test", payload={})
        # 先持久化
        event_bus.publish(event)
        event_bus._dispatch_event(event)

        conn = event_bus._get_conn()
        row = conn.execute(
            "SELECT status FROM events WHERE trace_id = ?", (event.trace_id,)
        ).fetchone()
        assert row["status"] == "done"

    def test_dispatch_event_wildcard_matches_all_types(self, event_bus):
        """通配符处理器应匹配所有事件类型。"""
        called_with = []

        def handler(event):
            called_with.append(event)

        handler.__name__ = "wildcard_handler"

        event_bus.subscribe("*", handler)

        from core.mnemos_bus import Event

        event = Event(event_type="any_type", source="test", payload={})
        event_bus._dispatch_event(event)

        assert len(called_with) == 1

    def test_dispatch_event_to_module_registry_bridge(self, event_bus):
        """EventBus wildcard bridge should dispatch payloads to running modules."""
        from core.mnemos_bus import Event
        from core.pluggable import ModuleRegistry

        module_events = []
        registry = ModuleRegistry()

        class FakePluggableModule:
            def __init__(self, module_id, events):
                self.module_id = module_id
                self.events = events

            def enable(self):
                self.events.append(f"enable:{self.module_id}")

            def disable(self):
                self.events.append(f"disable:{self.module_id}")

            def configure(self, cfg):
                self.events.append(f"configure:{self.module_id}")

            def handle_event(self, event_type, data):
                self.events.append(f"event:{self.module_id}:{event_type}")

        registry.register("core", lambda: FakePluggableModule("core", module_events))
        registry.start_enabled()
        registry.subscribe_to_event_bus(event_bus)

        event = Event(
            event_type="knowledge.ingested",
            source="test",
            payload={"page_path": "p.md"},
        )
        event_bus._dispatch_event(event)

        assert "event:core:knowledge.ingested" in module_events

    def test_dispatch_event_no_handler_goes_to_dead_letter(self, event_bus):
        """无处理器的事件应进入死信队列。"""
        from core.mnemos_bus import Event

        event = Event(event_type="no_handler_event", source="test", payload={})
        event_bus._dispatch_event(event)

        conn = event_bus._get_conn()
        row = conn.execute(
            "SELECT * FROM dead_letters WHERE trace_id = ?", (event.trace_id,)
        ).fetchone()

        assert row is not None
        assert row["status"] == "no_consumer"

    def test_dispatch_event_no_handler_telemetry_archives_persisted_row(self, event_bus):
        """历史遗留的无消费者 telemetry 不应永久停留在 processing。"""
        from core.mnemos_bus import Event

        event = Event(
            event_type="profile_blindspot_detected",
            source="test",
            payload={"page": "a.md"},
        )
        event_bus.publish(event, force=True)

        event_bus._dispatch_event(event)

        conn = event_bus._get_conn()
        row = conn.execute(
            "SELECT status FROM events WHERE trace_id = ?", (event.trace_id,)
        ).fetchone()
        dl_row = conn.execute(
            "SELECT * FROM dead_letters WHERE trace_id = ?", (event.trace_id,)
        ).fetchone()

        assert row["status"] == "archived"
        assert dl_row is None


class TestEventBusParallelDispatch:
    """测试事件总线并行分发与 handler 超时。"""

    def test_slow_handler_does_not_block_other_events(self, parallel_event_bus):
        """单个慢 handler 不应阻塞其他事件的投递。"""
        from core.mnemos_bus import Event

        bus = parallel_event_bus
        slow_called = threading.Event()
        fast_received = []

        def slow_handler(event):
            slow_called.set()
            time.sleep(2)

        slow_handler.__name__ = "slow_handler"

        def fast_handler(event):
            fast_received.append(event.payload["id"])

        fast_handler.__name__ = "fast_handler"

        bus.subscribe("slow.event", slow_handler)
        bus.subscribe("fast.event", fast_handler)

        bus.publish(Event(event_type="slow.event", source="test", payload={}))
        # 等待慢 handler 开始执行
        slow_called.wait(timeout=1)
        # 发布多个快事件，应能被并行处理
        for i in range(3):
            bus.publish(Event(event_type="fast.event", source="test", payload={"id": i}))

        # 给分发线程足够时间
        deadline = time.time() + 3
        while len(fast_received) < 3 and time.time() < deadline:
            time.sleep(0.05)

        assert len(fast_received) == 3, f"快事件应全部处理，实际 {fast_received}"

    def test_parallel_dispatch_uses_multiple_workers(self, parallel_event_bus):
        """并行分发应能同时处理多个事件（通过耗时缩短验证）。"""
        from core.mnemos_bus import Event

        bus = parallel_event_bus
        received = []
        lock = threading.Lock()

        def handler(event):
            time.sleep(0.2)
            with lock:
                received.append(event.payload["id"])

        handler.__name__ = "parallel_handler"
        bus.subscribe("parallel.event", handler)

        for i in range(4):
            bus.publish(Event(event_type="parallel.event", source="test", payload={"id": i}))

        deadline = time.time() + 2
        while len(received) < 4 and time.time() < deadline:
            time.sleep(0.05)

        assert len(received) == 4
        assert set(received) == {0, 1, 2, 3}

    def test_handler_timeout_marks_failed(self, timeout_event_bus):
        """handler 超时应被记录为失败并重试。"""
        from core.mnemos_bus import Event

        bus = timeout_event_bus

        def slow_handler(event):
            time.sleep(2)

        slow_handler.__name__ = "timeout_slow_handler"
        bus.subscribe("timeout.event", slow_handler)

        event = Event(event_type="timeout.event", source="test", payload={})
        bus.publish(event)

        deadline = time.time() + 3
        while time.time() < deadline:
            conn = bus._get_conn()
            row = conn.execute(
                "SELECT status, retry_count FROM events WHERE trace_id = ?",
                (event.trace_id,),
            ).fetchone()
            if row and row["status"] in ("pending", "done") and row["retry_count"] > 0:
                break
            time.sleep(0.05)

        conn = bus._get_conn()
        row = conn.execute(
            "SELECT status, retry_count FROM events WHERE trace_id = ?",
            (event.trace_id,),
        ).fetchone()
        assert row is not None
        assert row["retry_count"] > 0, "超时后 retry_count 应增加"

    def test_stop_dispatch_waits_for_in_flight_events(self, parallel_event_bus):
        """stop_dispatch 应等待进行中的事件处理完成。"""
        from core.mnemos_bus import Event

        bus = parallel_event_bus
        completed = threading.Event()

        def handler(event):
            time.sleep(0.3)
            completed.set()

        handler.__name__ = "in_flight_handler"
        bus.subscribe("inflight.event", handler)

        bus.publish(Event(event_type="inflight.event", source="test", payload={}))
        # 确保 handler 已经开始执行
        time.sleep(0.05)
        bus.stop_dispatch()

        assert completed.is_set(), "stop_dispatch 应等待进行中的 handler 完成"

    def test_same_trace_id_deferred_until_previous_done(self, parallel_event_bus):
        """同一 trace_id 的事件在前一个处理完成前不会并发处理。"""
        from core.mnemos_bus import Event

        bus = parallel_event_bus
        active_count = 0
        max_active = 0
        lock = threading.Lock()

        def handler(event):
            nonlocal active_count, max_active
            with lock:
                active_count += 1
                max_active = max(max_active, active_count)
            time.sleep(0.2)
            with lock:
                active_count -= 1

        handler.__name__ = "same_trace_handler"
        bus.subscribe("same_trace.event", handler)

        # 发布两个相同 trace_id 的事件
        event = Event(event_type="same_trace.event", source="test", payload={})
        bus.publish(event)
        bus._queue.put(event)  # 直接放入队列，模拟重试/重复投递

        time.sleep(0.6)
        assert max_active == 1, f"同一 trace_id 不应并发处理，max_active={max_active}"


class TestEventBusErrorHandling:
    """测试错误处理：处理器异常、重试、死信队列。"""

    def test_handler_exception_marks_failed_and_retries(self, event_bus):
        """处理器抛异常后，事件应进入重试状态。"""
        handler = _make_handler("failing_handler", side_effect=RuntimeError("boom"))
        event_bus.subscribe("session.start", handler)

        from core.mnemos_bus import Event

        event = Event(event_type="session.start", source="test", payload={})
        event_bus.publish(event)
        event_bus._dispatch_event(event)

        conn = event_bus._get_conn()
        row = conn.execute(
            "SELECT status, retry_count FROM events WHERE trace_id = ?",
            (event.trace_id,),
        ).fetchone()

        assert row is not None
        assert row["status"] == "pending"
        assert row["retry_count"] == 1

    def test_handler_exception_stays_durable_without_duplicate_queue_entry(self, event_bus):
        """失败事件由持久化 pending 重填，不在内存队列制造重复副本。"""
        handler = _make_handler("failing_handler", side_effect=RuntimeError("boom"))
        event_bus.subscribe("session.start", handler)

        from core.mnemos_bus import Event

        event = Event(event_type="session.start", source="test", payload={})
        event_bus.publish(event)
        initial_qsize = event_bus._queue.qsize()
        event_bus._dispatch_event(event)

        assert event_bus._queue.qsize() == initial_qsize
        row = (
            event_bus._get_conn()
            .execute("SELECT status, retry_count FROM events WHERE trace_id=?", (event.trace_id,))
            .fetchone()
        )
        assert tuple(row) == ("pending", 1)

    def test_max_retries_moves_to_dead_letter(self, event_bus):
        """重试超限后，事件应移入死信队列。"""
        handler = _make_handler("failing_handler", side_effect=RuntimeError("boom"))
        event_bus.subscribe("session.start", handler)

        from core.mnemos_bus import Event

        event = Event(event_type="session.start", source="test", payload={})
        event_bus.publish(event)

        # 模拟持久化调度器多次重新 claim（max_retries=3）。
        for _ in range(4):
            event_bus._dispatch_event(event)

        conn = event_bus._get_conn()
        row = conn.execute(
            "SELECT * FROM dead_letters WHERE trace_id = ?", (event.trace_id,)
        ).fetchone()

        assert row is not None
        assert row["status"] == "dead"

    def test_partial_failure_marks_failed(self, event_bus):
        """多个处理器中部分失败，事件应标记为 failed。"""
        good_handler = _make_handler("good_handler")
        bad_handler = _make_handler("bad_handler", side_effect=RuntimeError("boom"))
        event_bus.subscribe("session.start", good_handler)
        event_bus.subscribe("session.start", bad_handler)

        from core.mnemos_bus import Event

        event = Event(event_type="session.start", source="test", payload={})
        event_bus.publish(event)
        event_bus._dispatch_event(event)

        conn = event_bus._get_conn()
        row = conn.execute(
            "SELECT status, retry_count FROM events WHERE trace_id = ?",
            (event.trace_id,),
        ).fetchone()

        assert row is not None
        assert row["status"] == "pending"
        assert row["retry_count"] == 1

    def test_cognition_episode_terminal_receipt_is_unique_across_crash_replay(self, event_bus):
        """Ack 后 checkpoint 前崩溃的重放不得生成第二张终态回执。"""
        from core.mnemos_bus import Event, HandlerOutcome

        event_bus.subscribe(
            "cognition_episode_committed",
            lambda _event: HandlerOutcome.ack(
                "wiki",
                effect_id="effect-1",
                before_hash="sha256:before",
                after_hash="sha256:after",
            ),
            consumer_id="wiki",
        )
        event = Event("cognition_episode_committed", "test", {})
        event_bus.publish(event)
        event_bus._dispatch_event(event)

        conn = event_bus._get_conn()
        conn.execute(
            """UPDATE events
               SET status='pending', processed_handlers='[]',
                   lease_owner='', lease_expires_at=''
               WHERE trace_id=?""",
            (event.trace_id,),
        )
        conn.commit()
        event_bus._dispatch_event(event)

        terminal_count = conn.execute(
            """SELECT COUNT(*) FROM handler_receipts
               WHERE trace_id=? AND consumer='wiki'
                 AND disposition IN ('ack','noop')""",
            (event.trace_id,),
        ).fetchone()[0]
        assert terminal_count == 1

    def test_retry_checkpoint_survives_subscription_reordering(self, event_bus):
        """A stable consumer id prevents a successful side effect from replaying."""
        from core.mnemos_bus import Event, HandlerOutcome

        calls = {"original": 0, "retrying": 0, "inserted": 0}
        retry = {"enabled": True}

        def original(_event):
            calls["original"] += 1
            return HandlerOutcome.ack()

        def retrying(_event):
            calls["retrying"] += 1
            return (
                HandlerOutcome.retry(reason="again") if retry["enabled"] else HandlerOutcome.ack()
            )

        event_bus.subscribe("session.start", original, consumer_id="original")
        event_bus.subscribe("session.start", retrying, consumer_id="retrying")
        event = Event("session.start", "test", {})
        event_bus.publish(event)
        event_bus._dispatch_event(event)
        assert calls == {"original": 1, "retrying": 1, "inserted": 0}

        event_bus._handlers.clear()
        event_bus._handler_consumer_ids.clear()

        def inserted(_event):
            calls["inserted"] += 1
            return HandlerOutcome.ack()

        retry["enabled"] = False
        event_bus.subscribe("session.start", inserted, consumer_id="inserted")
        event_bus.subscribe("session.start", original, consumer_id="original")
        event_bus.subscribe("session.start", retrying, consumer_id="retrying")
        event_bus._dispatch_event(event)

        assert calls == {"original": 1, "retrying": 2, "inserted": 1}
        row = (
            event_bus._get_conn()
            .execute("SELECT status FROM events WHERE trace_id=?", (event.trace_id,))
            .fetchone()
        )
        assert row["status"] == "done"

    def test_retry_uses_durable_backoff_then_recovers(self, event_bus):
        """Transient failures stay pending without hot-looping through the retry budget."""
        from core.mnemos_bus import Event, HandlerOutcome

        available = {"value": False}

        def handler(_event):
            return (
                HandlerOutcome.ack()
                if available["value"]
                else HandlerOutcome.retry(reason="temporary outage")
            )

        event_bus._retry_base_seconds = 30
        event_bus._retry_max_seconds = 30
        event_bus.subscribe("session.start", handler)
        event = Event("session.start", "test", {})
        event_bus.publish(event)
        event_bus._queue.get_nowait()
        event_bus._dispatch_event(event)

        row = (
            event_bus._get_conn()
            .execute(
                "SELECT status, retry_count, next_attempt_at FROM events WHERE trace_id=?",
                (event.trace_id,),
            )
            .fetchone()
        )
        assert row["status"] == "pending"
        assert row["retry_count"] == 1
        assert row["next_attempt_at"] > datetime.now(timezone.utc).isoformat()
        assert event_bus._refill_pending_queue() == 0

        available["value"] = True
        event_bus._get_conn().execute(
            "UPDATE events SET next_attempt_at='' WHERE trace_id=?", (event.trace_id,)
        )
        event_bus._get_conn().commit()
        assert event_bus._refill_pending_queue() == 1
        event_bus._dispatch_event(event_bus._queue.get_nowait())
        done = (
            event_bus._get_conn()
            .execute("SELECT status FROM events WHERE trace_id=?", (event.trace_id,))
            .fetchone()
        )
        assert done["status"] == "done"

    def test_deferred_event_resumes_only_after_all_decisions(self, event_bus):
        """Human decisions park events without consuming retries or busy-looping."""
        from core.mnemos_bus import Event, HandlerOutcome

        decided = {"value": False}

        def handler(_event):
            if decided["value"]:
                return HandlerOutcome.ack()
            return HandlerOutcome.defer(
                reason="awaiting proposals", deferred_keys=["proposal-1", "proposal-2"]
            )

        event_bus.subscribe("session.start", handler)
        event = Event("session.start", "test", {})
        event_bus.publish(event)
        event_bus._queue.get_nowait()
        event_bus._dispatch_event(event)
        row = (
            event_bus._get_conn()
            .execute("SELECT status, retry_count FROM events WHERE trace_id=?", (event.trace_id,))
            .fetchone()
        )
        assert tuple(row) == ("awaiting_decision", 0)
        assert event_bus._refill_pending_queue() == 0
        assert event_bus.resume_deferred("proposal-1") == 0
        assert event_bus.resume_deferred("proposal-2") == 1

        decided["value"] = True
        assert event_bus._refill_pending_queue() == 1
        event_bus._dispatch_event(event_bus._queue.get_nowait())
        final = (
            event_bus._get_conn()
            .execute("SELECT status, retry_count FROM events WHERE trace_id=?", (event.trace_id,))
            .fetchone()
        )
        assert tuple(final) == ("done", 0)

    def test_decision_before_defer_registration_is_not_lost(self, event_bus):
        """A committed proposal is a durable wake signal even if it wins the race."""
        from core.mnemos_bus import Event, HandlerOutcome

        event_bus.resume_deferred("already-committed")
        event_bus.subscribe(
            "session.start",
            lambda _event: HandlerOutcome.defer(
                reason="late registration", deferred_keys=["already-committed"]
            ),
        )
        event = Event("session.start", "test", {})
        event_bus.publish(event)
        event_bus._queue.get_nowait()
        event_bus._dispatch_event(event)

        row = (
            event_bus._get_conn()
            .execute("SELECT status, retry_count FROM events WHERE trace_id=?", (event.trace_id,))
            .fetchone()
        )
        assert tuple(row) == ("pending", 0)
        assert event_bus.stats()["orphan_awaiting_decision"] == 0


class TestEventBusStats:
    """测试统计信息接口。"""

    def test_stats_returns_all_status_counts(self, event_bus):
        """stats() 应返回各状态计数。"""
        stats = event_bus.stats()

        assert "pending" in stats
        assert "processing" in stats
        assert "done" in stats
        assert "dead_letters" in stats
        assert "queue_depth" in stats
        assert "total_recorded" in stats

    def test_stats_exposes_configured_latency_threshold(self, event_bus):
        """stats() includes the configured latency alert threshold."""
        stats = event_bus.stats()

        assert stats["max_latency_ms"] == 10

    def test_stats_counts_pending_events(self, event_bus):
        """stats() 应正确计数 pending 事件。"""
        event_bus.publish("session.start", payload={"x": 1})
        event_bus.publish("session.end", payload={"x": 2})

        stats = event_bus.stats()
        assert stats["pending"] == 2
        assert stats["queue_depth"] == 2

    def test_stats_by_type_groups_correctly(self, event_bus):
        """stats_by_type() 应按事件类型分组计数。"""
        event_bus.publish("session.start", payload={})
        event_bus.publish("session.start", payload={})
        event_bus.publish("session.end", payload={})

        stats = event_bus.stats_by_type()
        assert stats["pending"].get("session.start", 0) == 2
        assert stats["pending"].get("session.end", 0) == 1


class TestEventBusRecoverPending:
    """测试启动时恢复 pending 事件。"""

    def test_recover_pending_restores_events_to_queue(self, patched_get_config, tmp_path):
        """新建 EventBus 时应自动恢复 pending 事件到内存队列。"""
        from core.mnemos_bus import EventBus, Event

        # 使用唯一的子目录确保 DB 完全隔离
        unique_dir = tmp_path / "recover_test" / str(uuid.uuid4())[:8]
        fake_cfg = _FakeConfig(tmp_path)
        fake_cfg.mnemos_dir = unique_dir

        import core.config as _config_mod

        _config_mod.get_config = lambda: fake_cfg

        # 先创建一个 bus，插入 pending 事件
        bus1 = EventBus()
        bus1.publish(Event(event_type="session.start", source="test", payload={"k": "v"}))
        bus1.close()

        # 再创建一个新 bus，应自动恢复
        bus2 = EventBus()
        assert bus2._queue.qsize() == 1

        # 验证队列中的事件内容
        event = bus2._queue.get_nowait()
        bus2._queue.task_done()
        assert event.event_type == "session.start"
        assert event.payload == {"k": "v"}

        bus2.close()

    def test_recover_pending_marks_recovered_as_processing(self, patched_get_config, tmp_path):
        """恢复时先将 processing 重置为 pending，加载到队列后再标记为 processing，避免重复恢复。"""
        from core.mnemos_bus import EventBus, Event

        unique_dir = tmp_path / "recover_test2" / str(uuid.uuid4())[:8]
        fake_cfg = _FakeConfig(tmp_path)
        fake_cfg.mnemos_dir = unique_dir

        import core.config as _config_mod

        _config_mod.get_config = lambda: fake_cfg

        bus1 = EventBus()
        event = Event(event_type="session.start", source="test", payload={})
        trace_id = bus1.publish(event)
        # 手动改为 processing
        conn = bus1._get_conn()
        conn.execute(
            "UPDATE events SET status = 'processing' WHERE trace_id = ?",
            (trace_id,),
        )
        conn.commit()
        bus1.close()

        # 新 bus 恢复后，已加载事件应被标记为 processing
        bus2 = EventBus()
        conn2 = bus2._get_conn()
        row = conn2.execute("SELECT status FROM events WHERE trace_id = ?", (trace_id,)).fetchone()
        assert row["status"] == "processing"
        # 事件应已进入内存队列
        recovered = bus2._queue.get_nowait()
        assert recovered.trace_id == trace_id
        bus2._queue.task_done()
        bus2.close()

    def test_refill_pending_queue_loads_more_pending(self, patched_get_config, tmp_path):
        """P106: 启动恢复达到上限后，_refill_pending_queue 应继续补充 pending 事件。"""
        from core.mnemos_bus import EventBus, Event

        unique_dir = tmp_path / "refill_test" / str(uuid.uuid4())[:8]
        fake_cfg = patched_get_config
        fake_cfg.mnemos_dir = unique_dir
        # 限制启动恢复 1 条，模拟 backlog 场景
        fake_cfg._values["event_bus.max_recover_events"] = 1

        bus1 = EventBus()
        # 发布 3 个事件，然后全部重置为 pending
        trace_ids = []
        for i in range(3):
            event = Event(event_type="session.start", source="test", payload={"i": i})
            tid = bus1.publish(event)
            trace_ids.append(tid)
        conn = bus1._get_conn()
        conn.execute("UPDATE events SET status = 'pending'")
        conn.commit()
        bus1.close()

        # 新 bus 启动时只恢复 1 条
        bus2 = EventBus()
        assert bus2._queue.qsize() == 1

        # 调用补货，应再加载剩余 pending 事件
        loaded = bus2._refill_pending_queue(batch=10)
        assert loaded == 2
        assert bus2._queue.qsize() == 3

        # 所有事件状态都应为 processing
        conn2 = bus2._get_conn()
        statuses = conn2.execute(
            "SELECT status FROM events WHERE trace_id IN (?, ?, ?)", tuple(trace_ids)
        ).fetchall()
        assert all(r["status"] == "processing" for r in statuses)
        bus2.close()

    def test_refill_pending_queue_returns_zero_when_empty(self, patched_get_config, tmp_path):
        """P106: 无 pending 事件时 _refill_pending_queue 返回 0。"""
        from core.mnemos_bus import EventBus

        unique_dir = tmp_path / "refill_empty" / str(uuid.uuid4())[:8]
        fake_cfg = patched_get_config
        fake_cfg.mnemos_dir = unique_dir

        bus = EventBus()
        assert bus._refill_pending_queue(batch=10) == 0
        bus.close()


class TestEventBusDeadLetter:
    """测试死信队列接口。"""

    def test_get_dead_letters_returns_records(self, event_bus):
        """get_dead_letters() 应返回死信记录。"""
        from core.mnemos_bus import Event

        event = Event(event_type="test_event", source="test", payload={"x": 1})
        event_bus._dead_letter_no_consumer(event)

        dead_letters = event_bus.get_dead_letters()
        assert len(dead_letters) == 1
        assert dead_letters[0]["event_type"] == "test_event"

    def test_replay_dead_letter_moves_back_to_events(self, event_bus):
        """replay_dead_letter() 应将死信移回 events 表。"""
        from core.mnemos_bus import Event

        event = Event(event_type="test_event", source="test", payload={"x": 1})
        event_bus._dead_letter_no_consumer(event)

        result = event_bus.replay_dead_letter(event.trace_id)
        assert result is True

        conn = event_bus._get_conn()
        row = conn.execute("SELECT * FROM events WHERE trace_id = ?", (event.trace_id,)).fetchone()
        assert row is not None
        assert row["status"] == "pending"

        # 死信队列中应已删除
        dl_row = conn.execute(
            "SELECT * FROM dead_letters WHERE trace_id = ?", (event.trace_id,)
        ).fetchone()
        assert dl_row is None

    def test_replay_dead_letter_returns_false_for_missing(self, event_bus):
        """replay_dead_letter() 对不存在的 trace_id 应返回 False。"""
        result = event_bus.replay_dead_letter("nonexistent")
        assert result is False

    def test_replay_no_consumer_dead_letters_requires_registered_handler(self, event_bus):
        """仅重放已有消费者的 no_consumer 死信，避免再次制造无消费者死信。"""
        from core.mnemos_bus import Event

        handled = Event(event_type="handled_event", source="test", payload={"x": 1})
        unhandled = Event(event_type="unhandled_event", source="test", payload={"x": 2})
        event_bus._dead_letter_no_consumer(handled)
        event_bus._dead_letter_no_consumer(unhandled)

        assert event_bus.replay_no_consumer_dead_letters() == 0

        calls = []

        def handler(event):
            calls.append(event.payload)

        event_bus.subscribe("handled_event", handler)
        assert event_bus.replay_no_consumer_dead_letters() == 1

        conn = event_bus._get_conn()
        replayed = conn.execute(
            "SELECT * FROM events WHERE trace_id = ?", (handled.trace_id,)
        ).fetchone()
        still_dead = conn.execute(
            "SELECT * FROM dead_letters WHERE trace_id = ?", (unhandled.trace_id,)
        ).fetchone()

        assert replayed is not None
        assert replayed["status"] == "pending"
        assert still_dead is not None

    def test_replay_no_consumer_dead_letters_respects_age_and_per_type_limit(self, event_bus):
        """启动重放应受时间窗口和单类型上限约束，避免历史死信洪峰。"""
        from core.mnemos_bus import Event

        old_ts = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        recent_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        def make(event_type, ts):
            return Event(
                event_type=event_type,
                source="test",
                payload={"x": event_type},
                timestamp=ts,
            )

        event_bus.subscribe("type_a", lambda e: None)
        event_bus.subscribe("type_b", lambda e: None)

        # type_a: 1 old + 2 recent -> only 2 recent replayed
        event_bus._dead_letter_no_consumer(make("type_a", old_ts))
        for _ in range(2):
            event_bus._dead_letter_no_consumer(make("type_a", recent_ts))
        # type_b: 3 recent -> per_type_limit=2 -> only 2 replayed
        for _ in range(3):
            event_bus._dead_letter_no_consumer(make("type_b", recent_ts))

        replayed = event_bus.replay_no_consumer_dead_letters(max_age_hours=24, per_type_limit=2)
        assert replayed == 4

        conn = event_bus._get_conn()
        pending_a = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'type_a' AND status = 'pending'"
        ).fetchone()[0]
        pending_b = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'type_b' AND status = 'pending'"
        ).fetchone()[0]
        assert pending_a == 2
        assert pending_b == 2


class TestEventBusCleanup:
    """测试清理接口。"""

    def test_cleanup_stale_archives_old_events(self, event_bus):
        """cleanup_stale() 应将超龄 pending 事件标记为 archived。"""
        # 使用 force=True 确保事件进入 events 表（old_event 无消费者且非持久化类型）
        trace_id = event_bus.publish("old_event", payload={}, force=True)

        # 手动修改时间为 48 小时前
        conn = event_bus._get_conn()
        old_time = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "UPDATE events SET timestamp = ? WHERE trace_id = ?",
            (old_time, trace_id),
        )
        conn.commit()

        count = event_bus.cleanup_stale(max_age_hours=1)
        assert count == 1

        row = conn.execute("SELECT status FROM events WHERE trace_id = ?", (trace_id,)).fetchone()
        assert row["status"] == "archived"

    def test_archive_no_consumer_events_moves_to_dead_letter(self, event_bus):
        """archive_no_consumer_events() 应将无消费者 pending 事件移入死信。"""
        event_bus.publish("unhandled_event", payload={}, force=True)

        count = event_bus.archive_no_consumer_events()
        assert count == 1

        conn = event_bus._get_conn()
        row = conn.execute(
            "SELECT * FROM events WHERE event_type = ?", ("unhandled_event",)
        ).fetchone()
        assert row is None

        dl_row = conn.execute(
            "SELECT * FROM dead_letters WHERE event_type = ?",
            ("unhandled_event",),
        ).fetchone()
        assert dl_row is not None

    def test_archive_no_consumer_events_archives_processing_telemetry(self, event_bus):
        """无消费者 processing telemetry 应归档，不进入死信也不继续占用运行态。"""
        from core.mnemos_bus import Event

        event = Event(
            event_type="knowledge_needs_reinforcement",
            source="test",
            payload={"page": "a.md"},
        )
        event_bus.publish(event, force=True)
        conn = event_bus._get_conn()
        conn.execute(
            "UPDATE events SET status = 'processing' WHERE trace_id = ?",
            (event.trace_id,),
        )
        conn.commit()

        count = event_bus.archive_no_consumer_events()

        assert count == 1
        row = conn.execute(
            "SELECT status FROM events WHERE trace_id = ?", (event.trace_id,)
        ).fetchone()
        dl_row = conn.execute(
            "SELECT * FROM dead_letters WHERE trace_id = ?", (event.trace_id,)
        ).fetchone()
        assert row["status"] == "archived"
        assert dl_row is None

    def test_startup_retains_durable_cognition_episode_event(self, tmp_path):
        """COG-030 的版本化事件不得被 7 天通用保留策略删除。"""
        from core.mnemos_bus import Event, EventBus, HandlerOutcome

        config = _FakeConfig(tmp_path)
        first = EventBus(config=config)
        first.subscribe(
            "cognition_episode_committed",
            lambda _event: HandlerOutcome.ack("wiki", effect_id="effect-1"),
            consumer_id="wiki",
        )
        event = Event("cognition_episode_committed", "test", {})
        first.publish(event)
        first._dispatch_event(event)
        conn = first._get_conn()
        conn.execute(
            "UPDATE events SET created_at='2020-01-01T00:00:00+00:00' WHERE trace_id=?",
            (event.trace_id,),
        )
        conn.commit()
        first.close()

        restarted = EventBus(config=config)
        try:
            retained = (
                restarted._get_conn()
                .execute("SELECT status FROM events WHERE trace_id=?", (event.trace_id,))
                .fetchone()
            )
            assert retained is not None
            assert retained["status"] == "done"
        finally:
            restarted.close()


# ============================================================
# CaptureQueue 测试
# ============================================================


class TestCaptureQueueEnqueue:
    """测试 CaptureQueue 入队操作。"""

    def test_enqueue_returns_queued(self, capture_queue):
        """正常入队应返回 'queued'。"""
        result = _enqueue_capture(
            capture_queue,
            dedupe_key="dk1",
            source_agent="claude",
            session_id="sess-1",
            turn_id="t1",
            turn_number=0,
            payload={"content": "hello"},
            content_hash="hash1",
        )
        assert result == "queued"

    def test_enqueue_duplicate_returns_duplicate(self, capture_queue):
        """重复 dedupe_key 应返回 'duplicate'。"""
        _enqueue_capture(
            capture_queue,
            dedupe_key="dk1",
            source_agent="claude",
            session_id="sess-1",
            turn_id="t1",
            turn_number=0,
            payload={"content": "hello"},
            content_hash="hash1",
        )
        result = _enqueue_capture(
            capture_queue,
            dedupe_key="dk1",
            source_agent="claude",
            session_id="sess-1",
            turn_id="t2",
            turn_number=1,
            payload={"content": "world"},
            content_hash="hash2",
        )
        assert result == "duplicate"

    def test_enqueue_increases_pending_count(self, capture_queue):
        """入队后 pending 计数应增加。"""
        _enqueue_capture(
            capture_queue,
            dedupe_key="dk1",
            source_agent="claude",
            session_id="sess-1",
            turn_id="t1",
            turn_number=0,
            payload={},
            content_hash="h1",
        )
        assert capture_queue.get_pending_count() == 1
        assert capture_queue.get_pending_count("claude") == 1


class TestCaptureQueueDequeue:
    """测试 CaptureQueue 出队操作。"""

    def test_dequeue_returns_records_and_changes_status(self, capture_queue):
        """出队应返回记录并将状态改为 processing。"""
        _enqueue_capture(
            capture_queue,
            dedupe_key="dk1",
            source_agent="claude",
            session_id="sess-1",
            turn_id="t1",
            turn_number=0,
            payload={"content": "hello"},
            content_hash="h1",
        )

        records = capture_queue.dequeue(source_agent="claude", limit=10)
        assert len(records) == 1
        assert records[0]["payload"]["content"] == "hello"

        # 状态应变为 processing
        status = capture_queue.get_status("claude", "sess-1", turn_number=0)
        assert status["status"] == "processing"

    def test_dequeue_respects_source_filter(self, capture_queue):
        """出队应按 source_agent 过滤。"""
        _enqueue_capture(
            capture_queue,
            dedupe_key="dk1",
            source_agent="claude",
            session_id="s1",
            turn_id="t1",
            turn_number=0,
            payload={},
            content_hash="h1",
        )
        _enqueue_capture(
            capture_queue,
            dedupe_key="dk2",
            source_agent="codex",
            session_id="s2",
            turn_id="t1",
            turn_number=0,
            payload={},
            content_hash="h2",
        )

        records = capture_queue.dequeue(source_agent="claude", limit=10)
        assert len(records) == 1
        assert records[0]["source_agent"] == "claude"

    def test_dequeue_respects_limit(self, capture_queue):
        """出队应尊重 limit 参数。"""
        for i in range(5):
            _enqueue_capture(
                capture_queue,
                dedupe_key=f"dk{i}",
                source_agent="claude",
                session_id="s1",
                turn_id=f"t{i}",
                turn_number=i,
                payload={},
                content_hash=f"h{i}",
            )

        records = capture_queue.dequeue(limit=3)
        assert len(records) == 3


class TestCaptureQueueUpdateStatus:
    """测试 CaptureQueue 状态更新。"""

    def test_update_status_to_done(self, capture_queue):
        """update_status 为 done 后，pending 计数应减少。"""
        _enqueue_capture(
            capture_queue,
            dedupe_key="dk1",
            source_agent="claude",
            session_id="s1",
            turn_id="t1",
            turn_number=0,
            payload={},
            content_hash="h1",
        )
        records = capture_queue.dequeue(limit=1)
        event_id = records[0]["id"]

        capture_queue.update_status(event_id, "done")
        assert capture_queue.get_pending_count() == 0

    def test_update_status_with_error_increments_retry(self, capture_queue):
        """带 error 的 update_status 应递增 retry_count。"""
        _enqueue_capture(
            capture_queue,
            dedupe_key="dk1",
            source_agent="claude",
            session_id="s1",
            turn_id="t1",
            turn_number=0,
            payload={},
            content_hash="h1",
        )
        records = capture_queue.dequeue(limit=1)
        event_id = records[0]["id"]

        capture_queue.update_status(event_id, "failed", error="timeout")
        status = capture_queue.get_status("claude", "s1", turn_number=0)
        assert status["status"] == "failed"
        assert status["retry_count"] == 1
        assert status["error"] == "timeout"


class TestCaptureQueueRecovery:
    """测试 CaptureQueue 崩溃恢复。"""

    def test_reset_processing_to_pending(self, capture_queue):
        """reset_processing_to_pending 应将 processing 恢复为 pending。"""
        _enqueue_capture(
            capture_queue,
            dedupe_key="dk1",
            source_agent="claude",
            session_id="s1",
            turn_id="t1",
            turn_number=0,
            payload={},
            content_hash="h1",
        )
        capture_queue.dequeue(limit=1)
        assert capture_queue.get_pending_count() == 0

        count = capture_queue.reset_processing_to_pending()
        assert count == 1
        assert capture_queue.get_pending_count() == 1


class TestCaptureQueueBackoff:
    """测试 CaptureQueue 退避状态。"""

    def test_set_and_get_backoff_state(self, capture_queue):
        """set_backoff_state / get_backoff_state 应正确读写。"""
        capture_queue.set_backoff_state(
            "claude", error_count=3, last_retry_at="2026-01-01T00:00:00"
        )

        state = capture_queue.get_backoff_state("claude")
        assert state["error_count"] == 3
        assert state["last_retry_at"] == "2026-01-01T00:00:00"

    def test_clear_backoff_state(self, capture_queue):
        """clear_backoff_state 应清除退避状态。"""
        capture_queue.set_backoff_state(
            "claude", error_count=1, last_retry_at="2026-01-01T00:00:00"
        )
        capture_queue.clear_backoff_state("claude")

        state = capture_queue.get_backoff_state("claude")
        assert state["error_count"] == 0
        assert state["last_retry_at"] is None


class TestCaptureQueueSessionEnd:
    """测试 CaptureQueue session end 标记。"""

    def test_mark_and_get_session_end(self, capture_queue):
        """mark_session_end / get_session_end_markers 应正确工作。"""
        capture_queue.mark_session_end("claude", "sess-1")
        markers = capture_queue.get_session_end_markers()

        assert len(markers) == 1
        assert markers[0]["source_agent"] == "claude"
        assert markers[0]["session_id"] == "sess-1"

    def test_clear_session_end_marker(self, capture_queue):
        """clear_session_end_marker 应清除标记。"""
        capture_queue.mark_session_end("claude", "sess-1")
        capture_queue.clear_session_end_marker("claude", "sess-1")

        markers = capture_queue.get_session_end_markers()
        assert len(markers) == 0


class TestCaptureQueueDedupe:
    """测试 CaptureQueue 去重检查。"""

    def test_is_duplicate_returns_true_for_existing(self, capture_queue):
        """已存在的 dedupe_key 应返回 True。"""
        _enqueue_capture(
            capture_queue,
            dedupe_key="dk1",
            source_agent="claude",
            session_id="s1",
            turn_id="t1",
            turn_number=0,
            payload={},
            content_hash="h1",
        )
        from core.sync_framework.capture_duplicate_policy import CaptureDuplicatePolicy

        assert (
            capture_queue.is_duplicate(
                CaptureDuplicatePolicy.build(
                    source_agent="claude", raw_revision_id="rawrev-test-dk1"
                ).value
            )
            is True
        )

    def test_is_duplicate_returns_false_for_new(self, capture_queue):
        """不存在的 dedupe_key 应返回 False。"""
        from core.sync_framework.capture_duplicate_policy import CaptureDuplicatePolicy

        assert (
            capture_queue.is_duplicate(
                CaptureDuplicatePolicy.build(
                    source_agent="claude", raw_revision_id="rawrev-test-new_key"
                ).value
            )
            is False
        )


class TestCaptureQueueDequeueFair:
    """测试 CaptureQueue 公平出队。"""

    def test_dequeue_fair_round_robin(self, capture_queue):
        """公平出队应在多个来源间 round-robin。"""
        for i in range(4):
            _enqueue_capture(
                capture_queue,
                dedupe_key=f"c{i}",
                source_agent="claude",
                session_id="s1",
                turn_id=f"t{i}",
                turn_number=i,
                payload={},
                content_hash=f"h{i}",
            )
        for i in range(4):
            _enqueue_capture(
                capture_queue,
                dedupe_key=f"x{i}",
                source_agent="codex",
                session_id="s2",
                turn_id=f"t{i}",
                turn_number=i,
                payload={},
                content_hash=f"h{i}",
            )

        records = capture_queue.dequeue_fair(limit=4)
        # 4 个来源各取 1 个，共 4 个
        assert len(records) == 4
        sources = [r["source_agent"] for r in records]
        assert "claude" in sources
        assert "codex" in sources


# ============================================================
# Event 数据类测试
# ============================================================


class TestEventDataclass:
    """测试 Event 数据类。"""

    def test_event_auto_generates_trace_id(self):
        """未提供 trace_id 时应自动生成。"""
        from core.mnemos_bus import Event

        event = Event(event_type="test", source="src", payload={})
        assert len(event.trace_id) > 0

    def test_event_auto_generates_timestamp(self):
        """未提供 timestamp 时应自动生成 UTC 时间戳。"""
        from core.mnemos_bus import Event

        event = Event(event_type="test", source="src", payload={})
        assert event.timestamp.startswith("20")
        assert "+" in event.timestamp or "Z" in event.timestamp

    def test_event_to_dict_roundtrip(self):
        """to_dict / from_row 应正确序列化反序列化。"""
        from core.mnemos_bus import Event

        event = Event(
            event_type="test",
            source="src",
            payload={"k": "v"},
            trace_id="abc",
            timestamp="2026-01-01T00:00:00+00:00",
        )
        d = event.to_dict()
        assert d["event_type"] == "test"
        assert d["payload"] == {"k": "v"}

    def test_event_to_json_is_valid_json(self):
        """to_json 应返回合法 JSON 字符串。"""
        from core.mnemos_bus import Event

        event = Event(event_type="test", source="src", payload={"k": "v"})
        s = event.to_json()
        parsed = json.loads(s)
        assert parsed["event_type"] == "test"

    def test_event_from_row_deserializes_correctly(self):
        """from_row 应正确从 sqlite3.Row 反序列化。"""
        from core.mnemos_bus import Event

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row  # noqa
        conn.execute("""
            CREATE TABLE t (
                event_type TEXT, source TEXT, payload_json TEXT,
                trace_id TEXT, timestamp TEXT
            )
        """)
        conn.execute(
            "INSERT INTO t VALUES (?, ?, ?, ?, ?)",
            ("test", "src", '{"k": "v"}', "abc", "2026-01-01T00:00:00+00:00"),
        )
        row = conn.execute("SELECT * FROM t").fetchone()

        event = Event.from_row(row)
        assert event is not None
        assert event.event_type == "test"
        assert event.payload == {"k": "v"}
        conn.close()

    def test_event_from_row_returns_none_on_bad_json(self):
        """from_row 对非法 JSON 应返回 None。"""
        from core.mnemos_bus import Event

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row  # noqa
        conn.execute(
            "CREATE TABLE t (event_type TEXT, source TEXT, payload_json TEXT, trace_id TEXT, timestamp TEXT)"  # noqa: E501
        )
        conn.execute(
            "INSERT INTO t VALUES (?, ?, ?, ?, ?)",
            ("test", "src", "not-json", "abc", "2026-01-01T00:00:00+00:00"),
        )
        row = conn.execute("SELECT * FROM t").fetchone()

        event = Event.from_row(row)
        assert event is None
        conn.close()


# ============================================================
# 便捷函数测试
# ============================================================


class TestUtilityFunctions:
    """测试模块级便捷函数。"""

    @staticmethod
    def _restore_isolated_event_bus_config(monkeypatch, patched_get_config):
        monkeypatch.setattr("core.config.get_config", lambda: patched_get_config)
        import core.mnemos_bus as bus_mod

        bus_mod.reset_event_bus()
        monkeypatch.setattr(bus_mod, "get_config", lambda: patched_get_config)
        return bus_mod

    def test_publish_event_returns_trace_id(self, patched_get_config, monkeypatch):
        """publish_event 应返回 trace_id。"""
        # 解除 conftest 中对 publish_event 的全局 patch

        monkeypatch.undo()
        # 重新 patch get_config 为隔离配置
        self._restore_isolated_event_bus_config(monkeypatch, patched_get_config)

        from core.mnemos_bus import publish_event

        trace_id = publish_event("session.start", "claude", {"key": "val"})
        assert isinstance(trace_id, str)
        assert len(trace_id) > 0

    def test_get_event_stats_returns_dict(self, patched_get_config, monkeypatch):
        """get_event_stats 应返回统计字典。"""

        monkeypatch.undo()
        self._restore_isolated_event_bus_config(monkeypatch, patched_get_config)

        from core.mnemos_bus import get_event_stats

        stats = get_event_stats()
        assert isinstance(stats, dict)
        assert "pending" in stats


class TestEventBusChainDepth:
    """事件链深度守护测试。"""

    def test_chain_depth_guard_stops_infinite_cascade(self, tmp_path, monkeypatch):
        """handler 中递归 publish 同类型事件时，链深度达到上限后应停止。"""
        import core.config as _config_mod
        from core.mnemos_bus import EventBus

        cfg = _FakeConfig(tmp_path)
        cfg._values["event_bus.max_chain_depth"] = 3
        monkeypatch.setattr(_config_mod, "get_config", lambda: cfg)
        monkeypatch.setattr("core.mnemos_bus.get_config", lambda: cfg)

        bus = EventBus()
        bus.start_dispatch()
        try:
            calls = []

            def handler(event):
                calls.append(event.chain_depth)
                bus.publish("chain_test", payload={"n": len(calls)})

            bus.subscribe("chain_test", handler)
            bus.publish("chain_test", payload={"start": True})
            time.sleep(0.5)

            # depth 0, 1, 2 被dispatch；depth 3 在 publish 时被丢弃
            assert len(calls) == 3
            assert calls == [0, 1, 2]
        finally:
            bus.stop_dispatch()
            bus.close()


def test_transient_eventbus_connections_release_only_current_thread():
    """Transient EventBus SQLite connections must be closed by their creator thread."""
    from core.mnemos_bus import EventBus

    class FakeConn:
        def __init__(self):
            self.owner = threading.get_ident()
            self.closed = False

        def close(self):
            assert threading.get_ident() == self.owner
            self.closed = True

    bus = object.__new__(EventBus)
    bus._transient_sqlite = True
    bus._local = threading.local()
    bus._conns_lock = threading.Lock()
    bus._all_conns = set()

    main_conn = FakeConn()
    bus._local.transient_conns = [main_conn]
    worker_result = {}

    def worker():
        worker_conn = FakeConn()
        bus._local.transient_conns = [worker_conn]
        bus._release_transient_connections()
        worker_result["closed"] = worker_conn.closed

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert worker_result["closed"] is True
    assert main_conn.closed is False
    bus._release_transient_connections()
    assert main_conn.closed is True


def test_transient_dispatch_survives_handler_publish(patched_get_config):
    """Handler 内部 publish 不应关闭 _dispatch_event 后续写回所需的连接。"""
    from core.mnemos_bus import Event, EventBus

    bus = EventBus()
    bus._transient_sqlite = True
    calls = []

    def handler(event):
        calls.append(event.trace_id)
        bus.publish("nested.event", payload={"from": event.trace_id})

    bus.subscribe("primary.event", handler)
    trace_id = bus.publish("primary.event", payload={"ok": True})
    row = bus._get_conn().execute("SELECT * FROM events WHERE trace_id = ?", (trace_id,)).fetchone()
    event = Event.from_row(row)
    bus._release_transient_connections()

    assert event is not None
    bus._dispatch_event(event)

    assert calls == [trace_id]
    row = (
        bus._get_conn()
        .execute("SELECT status FROM events WHERE trace_id = ?", (trace_id,))
        .fetchone()
    )
    assert row["status"] == "done"
    bus.close()
