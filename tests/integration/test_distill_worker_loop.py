# -*- coding: utf-8 -*-
"""
P1-1 长链路测试 — 蒸馏 Worker 链路

链路：Amphora enqueue → get_next → mark_done/failed → HephaestusWorker
      (mock delegate) → collect_completed → inbox/page 写入

策略：临时目录 + 临时 SQLite，mock AgentDelegate（只验证文件流转）。
      Amphora 使用真实代码（不手动建表，让 _init_db() 自动初始化）。
断言目标：队列状态流转、文件落盘、inbox 页面生成。
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

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
            amphora.enqueue("sess-001", "test content", meta={"source": "claude"})

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
                    "SELECT status, completed_at, output_path FROM distillation_tasks WHERE task_id=?",
                    (task["task_id"],),
                ).fetchone()
                assert row[0] == "done"
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
            amphora.enqueue("sess-retry", "content")
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
    """HephaestusWorker 文件级链路（mock delegate）。"""

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
            "output": tmp_path / "distill_output",
            "inbox": tmp_path / "wiki" / "00-Inbox",
            "archive": tmp_path / "distill_archive",
            "failed_tmp": tmp_path / "distill_failed_tmp",
        }
        for p in d.values():
            p.mkdir(parents=True)
        return d

    def test_process_one_file_writes_output(self, dirs, monkeypatch):
        from core.hephaestus_worker import HephaestusWorker

        # 构造一个任务 JSON
        task = {
            "session_id": "sess-001",
            "messages": [{"role": "user", "content": "test message"}],
            "meta": {"source": "claude"},
        }
        task_file = dirs["queue"] / "task_001.json"
        task_file.write_text(json.dumps(task), encoding="utf-8")

        # mock delegate 让它立即写输出文件
        def mock_delegate(task_obj, output_path):
            output_path.write_text(
                'MNEMOS_DISTILL_TASK\n{"judgment": "knowledge", "fragments": [{"title": "Test"}]}\n\n# Test Output\n',
                encoding="utf-8",
            )
            return True

        mock_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.delegate = mock_delegate
        mock_cls.return_value = mock_instance
        monkeypatch.setattr("core.hephaestus_worker.AgentDelegate", mock_cls)

        worker = HephaestusWorker(
            queue_dir=dirs["queue"],
            output_dir=dirs["output"],
            inbox_dir=dirs["inbox"],
            archive_dir=dirs["archive"],
        )
        result = worker.process_one_file(task_file)

        assert result is True
        # 输出文件应被 delegate mock 写入
        assert len(list(dirs["output"].glob("*.md"))) >= 1

    def test_collect_completed_moves_to_inbox(self, dirs, monkeypatch):
        from core.hephaestus_worker import HephaestusWorker
        from core.kia import amphora

        # 阻止 EventBus 加载 pending 事件
        monkeypatch.setattr(
            "core.mnemos_bus.EventBus._recover_pending",
            lambda self: None,
        )

        # 临时替换 amphora DB 到临时目录
        orig_db = amphora._DB_PATH
        amphora._DB_PATH = dirs["queue"] / "amphora.db"
        try:
            # 在 amphora 中创建任务并标记为 processing
            amphora.enqueue("sess_001", "test content", meta={"source": "claude"})
            task = amphora.get_next()
            assert task is not None

            # 预置一个已完成的输出文件（长度必须 >200，否则被当作占位符跳过）
            output_file = dirs["output"] / "sess_001.md"
            output_file.write_text(
                'MNEMOS_DISTILL_TASK\n{"judgment": "knowledge", "fragments": [{"title": "Test Fragment", "form": "decision"}]}\n\n'
                '# Test Fragment\n\nThis is a detailed content that must exceed two hundred characters '
                'in total length so that the collect_completed method does not treat it as a placeholder.\n',
                encoding="utf-8",
            )

            worker = HephaestusWorker(
                queue_dir=dirs["queue"],
                output_dir=dirs["output"],
                inbox_dir=dirs["inbox"],
                archive_dir=dirs["archive"],
            )
            result = worker.collect_completed()

            assert result >= 1
            # inbox 中应有文件
            assert len(list(dirs["inbox"].glob("*.md"))) >= 1
            # 输出目录应被清理
            assert len(list(dirs["output"].glob("*.md"))) == 0
        finally:
            amphora._DB_PATH = orig_db

    def test_invalid_output_goes_to_failed(self, dirs, monkeypatch):
        from core.hephaestus_worker import HephaestusWorker
        from core.kia import amphora

        # 阻止 EventBus 加载 pending 事件
        monkeypatch.setattr(
            "core.mnemos_bus.EventBus._recover_pending",
            lambda self: None,
        )

        # 临时替换 amphora DB
        orig_db = amphora._DB_PATH
        amphora._DB_PATH = dirs["queue"] / "amphora.db"
        try:
            # 在 amphora 中创建任务
            amphora.enqueue("sess_bad", "test content", meta={"source": "claude"})
            task = amphora.get_next()
            assert task is not None

            # 预置一个无效输出（缺少 judgment，长度 >200 避免占位符跳过）
            output_file = dirs["output"] / "sess_bad.md"
            output_file.write_text(
                'MNEMOS_DISTILL_TASK\n{"fragments": []}\n\n'
                'No judgment field here. This text needs to be longer than two hundred characters '
                'so that collect_completed does not skip it as an unfinished placeholder output.\n',
                encoding="utf-8",
            )

            worker = HephaestusWorker(
                queue_dir=dirs["queue"],
                output_dir=dirs["output"],
                inbox_dir=dirs["inbox"],
                archive_dir=dirs["archive"],
            )
            # mock _move_to_failed 使其写入临时目录，避免污染 ~/.mnemos
            failed_tmp = dirs["queue"].parent / "distill_failed_tmp"
            failed_tmp.mkdir(exist_ok=True)
            def _mock_move_to_failed(self, output_path, session_id, task_data, reason):
                (failed_tmp / f"{session_id}.md").write_text(reason, encoding="utf-8")
                output_path.unlink()
            monkeypatch.setattr(
                "core.hephaestus_worker.HephaestusWorker._move_to_failed",
                _mock_move_to_failed,
            )

            result = worker.collect_completed()

            assert result == 0  # 无效输出不应被收集
            assert len(list(failed_tmp.glob("*.md"))) >= 1
        finally:
            amphora._DB_PATH = orig_db
