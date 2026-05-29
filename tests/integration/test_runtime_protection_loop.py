import json
import sqlite3


def test_hephaestus_process_all_respects_batch_limit(tmp_path, monkeypatch):
    from core.hephaestus_worker import HephaestusWorker

    queue = tmp_path / "queue"
    output = tmp_path / "output"
    inbox = tmp_path / "wiki" / "00-Inbox"
    archive = tmp_path / "archive"
    for path in (queue, output, inbox, archive):
        path.mkdir(parents=True)

    for i in range(5):
        (queue / f"sess-{i}.json").write_text(
            json.dumps({
                "session_id": f"sess-{i}",
                "messages": [{"role": "user", "content": "Redis 连接池排障"}],
                "meta": {"source": "test"},
            }),
            encoding="utf-8",
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
    assert len(list(queue.glob("*.delegated"))) == 2
    assert len(list(queue.glob("*.json"))) == 3


def test_eventbus_recover_pending_is_capped(tmp_path, monkeypatch):
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
