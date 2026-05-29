"""
amphora 单元测试

覆盖项：
- enqueue / list_pending / get_next / mark_done / mark_failed / cleanup_old
- 原子性：get_next 后状态变为 processing
- 重试机制：mark_failed 后回到 pending，超过 max_retries 后 archived
- 优先级排序
- 进度更新
"""

import os
import sys
import unittest
import tempfile
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.kia import amphora


class TestAmphoraQueue(unittest.TestCase):
    def setUp(self):
        # 使用临时目录隔离测试
        self.tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_path = amphora._DB_PATH
        amphora._DB_PATH = Path(self.tmpdir.name) / "test_distill_queue.db"

    def tearDown(self):
        amphora._DB_PATH = self._orig_db_path
        self.tmpdir.cleanup()

    def test_enqueue_and_list_pending(self):
        sid = amphora.enqueue("sess-1", [{"role": "user", "content": "hello"}], {"source": "test"})
        self.assertEqual(sid, "sess-1")

        pending = amphora.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["session_id"], "sess-1")
        self.assertIn("task_id", pending[0])
        self.assertTrue(Path(pending[0]["messages_path"]).exists())
        self.assertEqual(pending[0]["messages"][0]["content"], "hello")

    def test_get_next_atomic(self):
        amphora.enqueue("sess-1", [{"role": "user", "content": "a"}])
        amphora.enqueue("sess-2", [{"role": "user", "content": "b"}], priority=1)

        # 兼容旧调用：priority=1 高于 priority=10
        task = amphora.get_next()
        self.assertIsNotNone(task)
        self.assertEqual(task["session_id"], "sess-2")
        self.assertEqual(task["status"], "processing")
        self.assertEqual(task["progress_step"], amphora.DistillProgress.EXTRACTING.value)

        # 再次 get_next 不应取到同一个
        task2 = amphora.get_next()
        self.assertIsNotNone(task2)
        self.assertEqual(task2["session_id"], "sess-1")

    def test_mark_done(self):
        amphora.enqueue("sess-1", [{"role": "user", "content": "a"}])
        amphora.get_next()

        ok = amphora.mark_done("sess-1", "/path/to/output.md")
        self.assertTrue(ok)

        # 再次 list_pending 应为空
        self.assertEqual(len(amphora.list_pending()), 0)

    def test_mark_failed_with_retry(self):
        amphora.enqueue("sess-1", [{"role": "user", "content": "a"}], max_retries=2)
        amphora.get_next()

        # 第一次失败 → 回到 pending，但 next_retry_at 未到时不会被消费
        ok = amphora.mark_failed("sess-1", "timeout")
        self.assertTrue(ok)

        pending = amphora.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertIn("retry 1/2", pending[0]["error"])
        self.assertIsNotNone(pending[0]["next_retry_at"])
        self.assertIsNone(amphora.get_next())

        # 手动把 retry 时间拨到过去，模拟 chronos 到点
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET next_retry_at = ? WHERE session_id = ?",
                ((datetime.now() - timedelta(minutes=1)).isoformat(), "sess-1"),
            )

        # 第二次失败 → archived
        amphora.get_next()
        amphora.mark_failed("sess-1", "timeout again")

        pending = amphora.list_pending()
        self.assertEqual(len(pending), 0)
        self.assertEqual(amphora.get_task_count("failed"), 1)

    def test_cleanup_old(self):
        import time
        amphora.enqueue("sess-old", [{"role": "user", "content": "a"}])
        amphora.get_next()
        amphora.mark_done("sess-old")

        pending_before = amphora.get_task_count()
        archived = amphora.cleanup_old(days=0)
        self.assertGreaterEqual(archived, 1)
        self.assertEqual(amphora.get_task_count(), pending_before)
        self.assertEqual(amphora.get_task_count("archived"), 1)

    def test_progress_update(self):
        amphora.enqueue("sess-1", [{"role": "user", "content": "a"}])
        amphora.update_progress("sess-1", 0.5)

        # 进度字段在内部，公开接口未暴露读取，至少确认不抛异常
        amphora.update_progress("sess-1", 1.0)
        self.assertTrue(amphora.update_progress("sess-1", amphora.DistillProgress.VERIFYING.value, "quality check"))

        pending = amphora.list_pending()
        self.assertEqual(pending[0]["progress_step"], amphora.DistillProgress.VERIFYING.value)
        self.assertEqual(pending[0]["progress_detail"], "quality check")

    def test_priority_ordering(self):
        amphora.enqueue("sess-low", [{"role": "user", "content": "low"}], priority=10)
        amphora.enqueue("sess-high", [{"role": "user", "content": "high"}], priority=1)

        task = amphora.get_next()
        self.assertEqual(task["session_id"], "sess-high")

    def test_meta_infers_priority(self):
        amphora.enqueue("sess-normal", [{"role": "user", "content": "normal"}])
        amphora.enqueue("sess-urgent", [{"role": "user", "content": "urgent"}], {"urgent": True})

        task = amphora.get_next()
        self.assertEqual(task["session_id"], "sess-urgent")

    def test_mark_done_accepts_task_id(self):
        amphora.enqueue("sess-1", [{"role": "user", "content": "a"}])
        task = amphora.get_next()

        self.assertTrue(amphora.mark_done(task["task_id"], "/path/to/output.md"))
        self.assertEqual(amphora.get_task_count("done"), 1)

    def test_duplicate_enqueue_does_not_overwrite_message_file(self):
        amphora.enqueue("sess-1", [{"role": "user", "content": "original"}])
        first = amphora.list_pending()[0]

        amphora.enqueue("sess-1", [{"role": "user", "content": "replacement"}])
        second = amphora.list_pending()[0]

        self.assertEqual(first["messages_path"], second["messages_path"])
        self.assertEqual(second["messages"][0]["content"], "original")
        self.assertEqual(amphora.get_task_count(), 1)

    def test_reset_timeouts_returns_stuck_processing_to_pending(self):
        amphora.enqueue("sess-1", [{"role": "user", "content": "a"}])
        amphora.get_next()

        stale_started_at = (datetime.now() - timedelta(minutes=31)).isoformat()
        with sqlite3.connect(str(amphora._DB_PATH)) as conn:
            conn.execute(
                "UPDATE distillation_tasks SET started_at = ? WHERE session_id = ?",
                (stale_started_at, "sess-1"),
            )

        reset_count = amphora.reset_timeouts(timeout_minutes=30)

        self.assertEqual(reset_count, 1)
        pending = amphora.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertIn("processing timeout", pending[0]["error"])
        self.assertEqual(pending[0]["progress_detail"], "reset by timeout watchdog")

    def test_missing_message_file_degrades_to_empty_messages(self):
        amphora.enqueue("sess-1", [{"role": "user", "content": "a"}])
        pending = amphora.list_pending()[0]
        Path(pending["messages_path"]).unlink()

        task = amphora.get_next()

        self.assertEqual(task["messages"], [])
        self.assertEqual(task["session_id"], "sess-1")


if __name__ == "__main__":
    unittest.main()
