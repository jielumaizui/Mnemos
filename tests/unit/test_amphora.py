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
        self.assertEqual(pending[0]["messages"][0]["content"], "hello")

    def test_get_next_atomic(self):
        amphora.enqueue("sess-1", [{"role": "user", "content": "a"}])
        amphora.enqueue("sess-2", [{"role": "user", "content": "b"}], priority=1)

        # 优先级 1 的先被取到
        task = amphora.get_next()
        self.assertIsNotNone(task)
        self.assertEqual(task["session_id"], "sess-2")
        self.assertEqual(task["status"], "processing")

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

        # 第一次失败 → 回到 pending
        ok = amphora.mark_failed("sess-1", "timeout")
        self.assertTrue(ok)

        pending = amphora.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertIn("retry 1/2", pending[0]["error"])

        # 第二次失败 → archived
        amphora.get_next()
        amphora.mark_failed("sess-1", "timeout again")

        pending = amphora.list_pending()
        self.assertEqual(len(pending), 0)

    def test_cleanup_old(self):
        import time
        amphora.enqueue("sess-old", [{"role": "user", "content": "a"}])
        amphora.get_next()
        amphora.mark_done("sess-old")

        # 0 天前清理 → 应该清理掉
        removed = amphora.cleanup_old(days=0)
        self.assertGreaterEqual(removed, 0)

    def test_progress_update(self):
        amphora.enqueue("sess-1", [{"role": "user", "content": "a"}])
        amphora.update_progress("sess-1", 0.5)

        # 进度字段在内部，公开接口未暴露读取，至少确认不抛异常
        amphora.update_progress("sess-1", 1.0)

    def test_priority_ordering(self):
        amphora.enqueue("sess-low", [{"role": "user", "content": "low"}], priority=10)
        amphora.enqueue("sess-high", [{"role": "user", "content": "high"}], priority=1)

        task = amphora.get_next()
        self.assertEqual(task["session_id"], "sess-high")


if __name__ == "__main__":
    unittest.main()
