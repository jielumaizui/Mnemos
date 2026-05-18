"""功能测试 - 验证核心模块的读写操作"""

import sys
import os
import tempfile
import shutil
import unittest
import json
import gc
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))


def _cleanup_temp(path: str):
    """Windows-compatible temp cleanup: force GC to close DB handles first"""
    gc.collect()
    shutil.rmtree(path, ignore_errors=True)


class TestConfigFunctional(unittest.TestCase):
    """配置系统功能测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.temp_dir) / "test_config.yaml"

    def tearDown(self):
        _cleanup_temp(self.temp_dir)

    def test_config_save_and_load(self):
        """配置可保存并重新加载"""
        from core.config import Config

        config = Config(config_path=self.config_path)
        # 修改一个值
        config._data["wiki"]["vault_path"] = "/tmp/test_vault"
        config.save()

        # 读取保存的文件内容
        import yaml
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        self.assertEqual(data["wiki"]["vault_path"], "/tmp/test_vault")


class TestSignalStoreFunctional(unittest.TestCase):
    """SignalStore 数据库功能测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_signals.db"
        self._store = None

    def tearDown(self):
        _cleanup_temp(self.temp_dir)

    def test_database_crud(self):
        """信号数据库可读写"""
        from core.persona.psyche import SignalStore, SessionSignal

        self._store = store = SignalStore(db_path=self.db_path)

        # 写入 session 信号
        signal = SessionSignal(
            session_id="test-session-001",
            timestamp=datetime.now().isoformat(),
            task_type="coding",
            duration_seconds=120,
        )
        store.insert_session_signal(signal)

        # 读取
        signals = store.get_recent_session_signals(days=1)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["task_type"], "coding")
        self.assertEqual(signals[0]["duration_seconds"], 120)

    def test_source_validation(self):
        """非法数据源会被拒绝"""
        from core.persona.psyche import SignalStore

        self._store = store = SignalStore(db_path=self.db_path)
        with self.assertRaises(ValueError):
            store._validate_source("invalid_source")

        # 合法数据源不应抛出异常
        for source in store.ALLOWED_SOURCES:
            store._validate_source(source)


class TestKnowledgeSchedulerFunctional(unittest.TestCase):
    """KnowledgeScheduler 功能测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_scheduler.db"

    def tearDown(self):
        _cleanup_temp(self.temp_dir)

    def test_schedule_and_retrieve(self):
        """任务可调度并可检索"""
        from core.kia.chronos import KnowledgeScheduler

        scheduler = KnowledgeScheduler(db_path=str(self.db_path))

        due = datetime.now() + timedelta(days=10)
        task_id = scheduler.schedule(
            task_type="review",
            subtype="dark_knowledge",
            due_date=due,
            context="测试上下文",
        )

        self.assertTrue(task_id.startswith("review-dark_knowledge-"))

        # 列出所有任务
        tasks = scheduler.list_all()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].task_type, "review")
        self.assertEqual(tasks[0].context, "测试上下文")

    def test_mark_completed(self):
        """任务可标记完成"""
        from core.kia.chronos import KnowledgeScheduler

        scheduler = KnowledgeScheduler(db_path=str(self.db_path))

        due = datetime.now() + timedelta(days=5)
        task_id = scheduler.schedule(
            task_type="test",
            subtype="complete",
            due_date=due,
        )

        scheduler.mark_completed(task_id)
        tasks = scheduler.list_all(status="completed")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].status, "completed")

    def test_cleanup_old(self):
        """可清理旧任务"""
        from core.kia.chronos import KnowledgeScheduler

        scheduler = KnowledgeScheduler(db_path=str(self.db_path))

        # 创建一个已完成的旧任务（通过直接操作数据库，因为 schedule API 不允许设置过去日期）
        import sqlite3
        old_date = (datetime.now() - timedelta(days=60)).isoformat()
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("""
                INSERT INTO knowledge_scheduled_tasks
                (task_id, task_type, subtype, due_date, reminder_date,
                 is_periodic, period, status, context, created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "old-task-1", "test", "cleanup", old_date, old_date,
                0, None, "completed", "", old_date, old_date,
            ))

        # 清理 30 天前的
        scheduler.cleanup_old(days=30)
        tasks = scheduler.list_all()
        self.assertEqual(len(tasks), 0)


if __name__ == "__main__":
    unittest.main()
