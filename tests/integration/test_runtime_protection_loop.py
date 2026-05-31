import json
import sqlite3


def test_hephaestus_process_all_respects_batch_limit(tmp_path, monkeypatch):
    from core.hephaestus_worker import HephaestusWorker
    from core.kia import amphora

    # 阻止 EventBus 加载 pending 事件
    monkeypatch.setattr(
        "core.mnemos_bus.EventBus._recover_pending",
        lambda self: None,
    )

    queue = tmp_path / "queue"
    output = tmp_path / "output"
    inbox = tmp_path / "wiki" / "00-Inbox"
    archive = tmp_path / "archive"
    for path in (queue, output, inbox, archive):
        path.mkdir(parents=True)

    # 临时替换 amphora DB
    orig_db = amphora._DB_PATH
    amphora._DB_PATH = tmp_path / "amphora.db"
    try:
        for i in range(5):
            amphora.enqueue(
                f"sess-{i}",
                "Redis 连接池排障",
                meta={"source": "test"},
            )

        class FakeDelegate:
            def delegate(self, task, output_path):
                output_path.write_text(
                    json.dumps({
                        "judgment": "knowledge",
                        "fragments": [{"title": "Redis 连接池", "form": "pitfall"}],
                    }),
                    encoding="utf-8",
                )
                return True

        monkeypatch.setattr(
            "core.hephaestus_worker.HephaestusWorker._emit_progress",
            lambda self, *args, **kwargs: None,
        )

        worker = HephaestusWorker(queue, output, inbox, archive)
        worker.delegate = FakeDelegate()
        worker.config.set("distill.min_task_interval_seconds", 0)

        processed = worker.process_all(max_tasks=2)

        assert processed == 2
        # amphora 中应有 3 个 pending（5-2=3）
        pending = amphora.list_pending()
        assert len(pending) == 3
    finally:
        amphora._DB_PATH = orig_db


def test_eventbus_recover_pending_is_capped(tmp_path, monkeypatch):
    import importlib
    import core.mnemos_bus
    importlib.reload(core.mnemos_bus)
    from core.mnemos_bus import EventBus

    recover_pending = EventBus._recover_pending
    monkeypatch.setattr(EventBus, "_recover_pending", lambda self: None)
    bus = EventBus(root_dir=tmp_path)
    bus.close()
    bus._db_path = tmp_path / "events.db"
    bus._init_db()
    bus._max_recover_events = 2

    with sqlite3.connect(str(bus._db_path)) as conn:
        for i in range(5):
            conn.execute(
                """INSERT INTO events
                   (timestamp, trace_id, event_type, source, payload_json, status, retry_count, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)""",
                (f"t{i}", f"trace-{i}", "test", "test", "{}", f"t{i}"),
            )
        conn.commit()

    recover_pending(bus)

    assert bus._queue.qsize() == 2
    bus.close()
