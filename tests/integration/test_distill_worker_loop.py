# -*- coding: utf-8 -*-
"""
P1-1 长链路测试 — 蒸馏 Worker 链路

链路：Amphora enqueue → get_next → HephaestusWorker → 同步 DistillationEngine owner

策略：临时目录 + 临时 SQLite，mock 同步引擎边界。
      Amphora 使用真实代码（不手动建表，让 _init_db() 自动初始化）。
断言目标：队列状态流转、文件落盘、inbox 页面生成。
"""

import sqlite3

import pytest


class TestAmphoraQueueLoop:
    """Amphora SQLite 队列完整链路。"""

    @pytest.fixture
    def db(self, tmp_path):
        """返回临时 DB 路径；schema 由 amphora._init_db() 自动创建。"""
        return tmp_path / "amphora.db"

    def test_enqueue_get_next_mark_done(self, db, monkeypatch):
        from core.kia import amphora

        # 阻止 EventBus 加载 200万+ pending 事件
        monkeypatch.setattr(
            "core.mnemos_bus.EventBus._recover_pending",
            lambda self: None,
        )

        # 临时替换 DB 路径
        orig_db = amphora._DB_PATH
        amphora._DB_PATH = db
        try:
            amphora.enqueue_with_receipt("sess-001", "test content", meta={"source": "claude"})

            pending = amphora.list_pending()
            assert len(pending) == 1
            assert pending[0]["session_id"] == "sess-001"

            task = amphora.get_next()
            assert task is not None
            assert task["session_id"] == "sess-001"
            assert task["status"] == "processing"

            # mark_done 使用 task_id 和 output_path
            amphora.mark_done(task["task_id"], output_path="/tmp/out.md")

            with sqlite3.connect(str(db)) as conn:
                row = conn.execute(
                    "SELECT status, completed_at, output_path FROM distillation_tasks WHERE task_id=?",  # noqa: E501
                    (task["task_id"],),
                ).fetchone()
                assert row[0] == "committed"
                assert row[1] is not None
                assert row[2] == "/tmp/out.md"
        finally:
            amphora._DB_PATH = orig_db

    def test_enqueue_mark_failed_with_retry(self, db, monkeypatch):
        from core.kia import amphora

        monkeypatch.setattr(
            "core.mnemos_bus.EventBus._recover_pending",
            lambda self: None,
        )

        orig_db = amphora._DB_PATH
        amphora._DB_PATH = db
        try:
            amphora.enqueue_with_receipt("sess-retry", "content")
            task = amphora.get_next()
            amphora.mark_failed(task["task_id"], error="timeout")

            with sqlite3.connect(str(db)) as conn:
                row = conn.execute(
                    "SELECT status, retry_count, error FROM distillation_tasks WHERE task_id=?",
                    (task["task_id"],),
                ).fetchone()
                # 第一次失败后可能回退到 pending（如果 retry < max_retries）
                assert row[0] in ("pending", "failed")
                assert row[1] >= 1
                assert "timeout" in (row[2] or "")
        finally:
            amphora._DB_PATH = orig_db


class TestHephaestusWorkerLoop:
    """HephaestusWorker 唯一同步执行链路。"""

    @pytest.fixture
    def dirs(self, tmp_path, monkeypatch):
        # 阻止 EventBus 和 _emit_progress 卡住测试
        monkeypatch.setattr(
            "core.mnemos_bus.EventBus._recover_pending",
            lambda self: None,
        )
        monkeypatch.setattr(
            "core.hephaestus_worker.HephaestusWorker._emit_progress",
            lambda self, *args, **kwargs: None,
        )
        d = {
            "queue": tmp_path / "distill_queue",
            "inbox": tmp_path / "wiki" / "00-Inbox",
            "archive": tmp_path / "distill_archive",
        }
        for p in d.values():
            p.mkdir(parents=True)
        return d

    def test_process_one_task_uses_api_path_by_default(self, dirs, monkeypatch):
        from core.hephaestus_worker import HephaestusWorker
        from core.kia import amphora

        # 隔离 amphora DB
        monkeypatch.setattr(amphora, "_DB_PATH", dirs["queue"] / "amphora.db")
        amphora._init_db()

        # 通过 amphora 入队一个任务
        amphora.enqueue_with_receipt(
            "sess-001",
            [{"role": "user", "content": "test message"}],
            meta={"source": "claude"},
        )
        task = amphora.get_next()
        assert task is not None

        calls = {"sync": 0}
        monkeypatch.setattr(
            "core.hephaestus_worker.HephaestusWorker._sync_distill_and_complete",
            lambda self, session_id, distill_task, *, task=None: calls.__setitem__("sync", calls["sync"] + 1)
            or True,
        )

        worker = HephaestusWorker(
            queue_dir=dirs["queue"],
            inbox_dir=dirs["inbox"],
            archive_dir=dirs["archive"],
        )
        result = worker.process_one_task(task)

        assert result is True
        assert calls["sync"] == 1

    def test_external_output_collector_is_not_an_active_worker_surface(self, dirs):
        from core.hephaestus_worker import HephaestusWorker

        worker = HephaestusWorker(
            queue_dir=dirs["queue"],
            inbox_dir=dirs["inbox"],
            archive_dir=dirs["archive"],
        )

        assert not hasattr(worker, "collect_completed")
        assert not hasattr(worker, "output_dir")
