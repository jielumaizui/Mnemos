# -*- coding: utf-8 -*-
"""
P0-2 长链路测试 — 真实 EventBus 链路

链路：publish_event → SQLite events 表 → handler.handle_event → downstream state

策略：临时 events.db，真实 EventBus 实例，只 mock 外部 LLM/网络。
断言目标：DB 记录、事件状态流转、handler 被调用、异常不阻塞后续事件。
"""

import sqlite3
import threading
import time
from pathlib import Path

import pytest


class TestEventBusRealLoop:
    """EventBus 真实链路测试 — 不使用 mock_event。"""

    @pytest.fixture
    def bus(self, tmp_path, monkeypatch):
        """每个测试独立 EventBus 实例（隔离 events.db）。"""
        from core.mnemos_bus import EventBus

        # 避免加载全局 200万+ pending 事件导致超时
        monkeypatch.setattr(EventBus, "_recover_pending", lambda self: None)
        bus = EventBus(root_dir=tmp_path)
        # 关闭 __init__ 中已建立的全局 DB 连接，重新指向临时 DB
        bus.close()
        bus._db_path = tmp_path / "events.db"
        bus._init_db()
        bus.start_dispatch()
        # 让 publish_event 等全局函数也指向这个 bus
        monkeypatch.setattr("core.mnemos_bus._global_bus", bus)
        yield bus
        bus.stop_dispatch()
        bus.close()

    def test_publish_persists_to_sqlite(self, bus):
        """bus.publish 应将事件写入 SQLite events 表。"""
        from core.mnemos_bus import Event

        event = Event(event_type="memory_synced", source="sync", payload={
            "session_id": "sess-001",
            "source": "memos",
        })
        event_id = bus.publish(event, force=True)
        assert event_id

        conn = bus._get_conn()
        row = conn.execute(
            "SELECT event_type, source, payload_json, status FROM events WHERE trace_id=?",
            (event_id,),
        ).fetchone()
        assert row is not None
        assert row["event_type"] == "memory_synced"
        assert row["source"] == "sync"
        assert row["status"] == "pending"
        assert "sess-001" in row["payload_json"]

    def test_handler_receives_event(self, bus):
        """订阅 handler 应在事件发布后收到回调。"""
        from core.mnemos_bus import Event
        received = []

        def handler(event):
            received.append({
                "type": event.event_type,
                "source": event.source,
                "payload": event.payload,
            })

        bus.subscribe("content_scored", handler)

        event = Event(event_type="content_scored", source="scorer", payload={"score": 0.85})
        event_id = bus.publish(event)
        assert event_id

        # 给分发线程一点时间
        time.sleep(0.3)

        assert len(received) == 1
        assert received[0]["type"] == "content_scored"
        assert received[0]["payload"]["score"] == 0.85

    def test_multiple_handlers_same_event(self, bus):
        """同一事件类型多个 handler 都应被调用。"""
        from core.mnemos_bus import Event
        calls = []

        bus.subscribe("knowledge_distilled", lambda e: calls.append("h1"))
        bus.subscribe("knowledge_distilled", lambda e: calls.append("h2"))

        bus.publish(Event(event_type="knowledge_distilled", source="distill", payload={"title": "x"}))
        time.sleep(0.3)

        assert "h1" in calls
        assert "h2" in calls

    def test_handler_exception_does_not_block_others(self, bus):
        """某个 handler 抛异常不应阻塞同事件的其他 handler。"""
        from core.mnemos_bus import Event
        calls = []

        def bad_handler(event):
            raise RuntimeError("boom")

        def good_handler(event):
            calls.append("ok")

        bus.subscribe("distill_complete", bad_handler)
        bus.subscribe("distill_complete", good_handler)

        bus.publish(Event(event_type="distill_complete", source="distill", payload={}))
        time.sleep(0.3)

        assert "ok" in calls, "good_handler 应被调用"

    def test_event_status_lifecycle(self, bus):
        """事件状态应在 DB 中记录为 pending，handler 完成后更新为 done。"""
        from core.mnemos_bus import Event
        processed = []

        def sync_handler(event):
            processed.append(event.event_type)

        bus.subscribe("test_lifecycle", sync_handler)

        event = Event(event_type="test_lifecycle", source="test", payload={"data": 1})
        bus.publish(event)
        time.sleep(0.3)

        conn = bus._get_conn()
        row = conn.execute(
            "SELECT status FROM events WHERE trace_id=?", (event.trace_id,),
        ).fetchone()
        assert row is not None
        # handler 成功执行后状态应为 done
        assert row["status"] == "done"
        assert "test_lifecycle" in processed

    def test_bus_stats_returns_counts(self, bus):
        """stats() 应返回事件统计。"""
        from core.mnemos_bus import Event
        bus.publish(Event(event_type="type_a", source="s", payload={}), force=True)
        bus.publish(Event(event_type="type_a", source="s", payload={}), force=True)
        bus.publish(Event(event_type="type_b", source="s", payload={}), force=True)
        time.sleep(0.3)

        stats = bus.stats()
        assert stats.get("pending", 0) + stats.get("done", 0) >= 3
        assert "dead_letters" in stats

    def test_cross_thread_publish_and_consume(self, bus):
        """跨线程 publish → consume 应正常工作。"""
        from core.mnemos_bus import Event
        consumed = []

        def handler(event):
            consumed.append(event.payload["thread"])

        bus.subscribe("thread_test", handler)

        def producer(thread_id):
            bus.publish(
                Event(event_type="thread_test", source="test", payload={"thread": thread_id}),
            )

        threads = [threading.Thread(target=producer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        time.sleep(0.4)
        assert len(consumed) == 5
        assert set(consumed) == {0, 1, 2, 3, 4}
