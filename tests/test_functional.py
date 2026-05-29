"""功能测试 - 验证核心模块的读写操作"""

import sys
import os
import tempfile
import shutil
import unittest
import json
import gc
from unittest.mock import patch
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

    def test_same_day_tasks_do_not_overwrite(self):
        """同日同类型任务不会互相覆盖"""
        from core.kia.chronos import KnowledgeScheduler

        scheduler = KnowledgeScheduler(db_path=str(self.db_path))
        due = datetime.now() + timedelta(days=5)

        first = scheduler.schedule("review", "code", due, context="任务A")
        second = scheduler.schedule("review", "code", due, context="任务B")

        self.assertNotEqual(first, second)
        tasks = scheduler.list_all()
        self.assertEqual(len(tasks), 2)
        self.assertEqual({t.context for t in tasks}, {"任务A", "任务B"})

    def test_pending_reminders_order_by_priority(self):
        """到期提醒按优先级优先，再按时间排序"""
        from core.kia.chronos import KnowledgeScheduler

        scheduler = KnowledgeScheduler(db_path=str(self.db_path))
        due = datetime.now()
        low = scheduler.schedule("review", "low", due, context="low", priority=0)
        high = scheduler.schedule("review", "high", due, context="high", priority=5)

        reminders = scheduler.get_pending_reminders()
        self.assertEqual([t.task_id for t in reminders], [high, low])
        self.assertEqual(reminders[0].priority, 5)

    def test_periodic_task_regenerates_on_completion(self):
        """周期性任务完成后自动生成下一周期任务"""
        from core.kia.chronos import KnowledgeScheduler

        scheduler = KnowledgeScheduler(db_path=str(self.db_path))
        due = datetime.now() + timedelta(days=1)
        task_id = scheduler.schedule(
            "review",
            "weekly",
            due,
            context="每周复盘",
            is_periodic=True,
            period="weekly",
            priority=3,
        )

        scheduler.mark_completed(task_id)

        completed = scheduler.list_all(status="completed")
        pending = scheduler.list_all(status="pending")
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0].is_periodic)
        self.assertEqual(pending[0].period, "weekly")
        self.assertEqual(pending[0].priority, 3)
        next_due = datetime.fromisoformat(pending[0].due_date)
        self.assertEqual((next_due - due).days, 7)

    def test_session_start_event_uses_classifier_contract(self):
        """session.start 事件按 Dike 契约传入 messages 列表"""
        from core.kia.chronos import KnowledgeScheduler
        from core.kia.dike import ClassificationResult

        scheduler = KnowledgeScheduler(db_path=str(self.db_path))

        class FakeClassifier:
            def classify(self, messages):
                self.messages = messages
                return ClassificationResult(
                    task_type="coding",
                    subtype="python",
                    confidence=0.9,
                    suggested_confirmation="silent",
                )

        fake = FakeClassifier()
        with patch("core.kia.dike.TaskClassifier", return_value=fake):
            result = scheduler.trigger_event(
                "session.start",
                {"user_message": "帮我修一个 Python bug"},
            )

        self.assertEqual(fake.messages, [{"role": "user", "content": "帮我修一个 Python bug"}])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["event_type"], "session.start")
        self.assertEqual(result["task_type"], "coding")

    def test_message_exchanged_event_uses_guard_contract(self):
        """message.exchanged 事件按 Aegis InProcessGuard.check 契约执行"""
        from core.kia.chronos import KnowledgeScheduler

        scheduler = KnowledgeScheduler(db_path=str(self.db_path))

        class FakeGuard:
            def __init__(self):
                self.calls = []

            def check(self, user_message, ai_response=""):
                self.calls.append((user_message, ai_response))
                return None

        guard = FakeGuard()
        result = scheduler.trigger_event(
            "message.exchanged",
            {"guard": guard, "message": "继续", "ai_response": "好的"},
        )

        self.assertEqual(guard.calls, [("继续", "好的")])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["event_type"], "message.exchanged")
        self.assertIsNone(result["alert"])

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
        scheduler.cleanup_old_tasks(days=30)
        tasks = scheduler.list_all()
        self.assertEqual(len(tasks), 0)

    def test_issue_pipeline_step_scans_and_fixes(self):
        """issue_pipeline 步骤扫描并自动修复可自动修复的问题"""
        from core.kia.chronos import KnowledgeScheduler
        from core.kia.issue_pipeline import IssueRegistry, Issue

        # 准备独立的 issue db
        issue_db = Path(self.temp_dir) / "issues.db"
        registry = IssueRegistry(db_path=str(issue_db))
        issue = Issue(
            source_module="immune", issue_type="orphan",
            page_path="orphan.md", severity="medium",
        )
        registry.register(issue)

        scheduler = KnowledgeScheduler(db_path=str(self.db_path))
        result = scheduler._run_issue_pipeline(registry=registry)
        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(result["scanned"], 1)

    def test_dialog_reminder_cleanup_step(self):
        """dialog_reminder_cleanup 步骤清理过期记录"""
        from core.kia.chronos import KnowledgeScheduler
        from core.kia.dialog_reminder import DialogReminderQueue

        reminder_db = Path(self.temp_dir) / "reminders.db"
        queue = DialogReminderQueue(db_path=str(reminder_db))
        rid = queue.enqueue("issue-1", "page.md", "medium", "test", ["a"])
        queue.resolve(rid, "ok")
        # 将 resolved_at 改为过去
        import sqlite3
        old = (datetime.now() - timedelta(days=40)).isoformat()
        with sqlite3.connect(str(reminder_db), timeout=10) as conn:
            conn.execute(
                "UPDATE dialog_reminders SET resolved_at = ? WHERE reminder_id = ?",
                (old, rid),
            )
            conn.commit()

        scheduler = KnowledgeScheduler(db_path=str(self.db_path))
        result = scheduler._run_dialog_reminder_cleanup(queue=queue)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["deleted"], 1)


if __name__ == "__main__":
    unittest.main()
