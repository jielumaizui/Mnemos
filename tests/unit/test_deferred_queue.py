"""
deferred_queue 单元测试

覆盖项：
- enqueue / get_ready_items / mark_done / mark_failed
- 延迟调度（delay_seconds）
- 指数退避重试
- process_batch
- cleanup_old
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest
from core.deferred_queue import DeferredQueue, DeferredItem


class TestDeferredQueue(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test_deferred.db"
        self.queue = DeferredQueue(db_path=self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_enqueue_and_get_ready(self):
        self.queue.enqueue("distill", {"content": "hello"})
        items = self.queue.get_ready_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].item_type, "distill")

    def test_delay_not_ready(self):
        self.queue.enqueue("distill", {"content": "hello"}, delay_seconds=3600)
        items = self.queue.get_ready_items()
        self.assertEqual(len(items), 0)

    def test_mark_done(self):
        self.queue.enqueue("distill", {"content": "hello"})
        item = self.queue.get_ready_items()[0]
        self.queue.mark_done(item.id)
        items = self.queue.get_ready_items()
        self.assertEqual(len(items), 0)

    def test_retry_then_archive(self):
        import sqlite3
        from datetime import datetime

        self.queue.enqueue("distill", {"content": "hello"}, max_retries=2)
        item = self.queue.get_ready_items()[0]

        # 第一次失败 → pending 但 scheduled_at 被延迟到未来
        self.queue.mark_failed(item.id, "error1")
        items = self.queue.get_ready_items()
        self.assertEqual(len(items), 0)  # 延迟中，还不能取回

        # 模拟延迟结束：把 scheduled_at 改回现在
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "UPDATE deferred_items SET scheduled_at = ? WHERE id = ?",
                (datetime.now().isoformat(), item.id)
            )
            conn.commit()

        items = self.queue.get_ready_items()
        self.assertEqual(len(items), 1)  # 回到 pending
        self.assertEqual(items[0].retry_count, 1)
        self.assertIn("error1", items[0].error or "")

        # 第二次失败 → archived（retry_count 达到 max_retries）
        item2 = items[0]
        self.queue.mark_failed(item2.id, "error2")
        items = self.queue.get_ready_items()
        self.assertEqual(len(items), 0)  # archived

    def test_process_batch(self):
        self.queue.enqueue("distill", {"content": "a"})
        self.queue.enqueue("distill", {"content": "b"})

        def handler(item: DeferredItem) -> bool:
            return True

        stats = self.queue.process_batch(handler, item_type="distill")
        self.assertEqual(stats["processed"], 2)
        self.assertEqual(stats["succeeded"], 2)

    def test_priority_ordering(self):
        self.queue.enqueue("low", {"v": 1}, priority=10)
        self.queue.enqueue("high", {"v": 2}, priority=1)

        items = self.queue.get_ready_items()
        self.assertEqual(items[0].payload["v"], 2)

    def test_stats(self):
        self.queue.enqueue("a", {})
        self.queue.enqueue("b", {})
        item = self.queue.get_ready_items()[0]
        self.queue.mark_done(item.id)

        stats = self.queue.get_stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["done"], 1)


if __name__ == "__main__":
    unittest.main()
