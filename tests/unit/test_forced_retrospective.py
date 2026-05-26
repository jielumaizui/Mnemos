"""
forced_retrospective + obsidian_opener 单元测试

覆盖项：
- should_force_open 组合权重算法
- 用户预约复盘 schedule/cancel/reschedule
- 启动补偿 startup_compensation
- open_obsidian 跨平台逻辑（mock）
"""

import os
import sys
import tempfile
import shutil
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class TestShouldForceOpen(unittest.TestCase):
    """组合权重决策算法测试（蓝图 §8）"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        from core.app.forced_retrospective import ForcedRetrospective, RecapTask
        self.fr = ForcedRetrospective(db_path=str(Path(self.temp_dir) / "recap.db"))
        self.RecapTask = RecapTask

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_critical_high_age_forces_open(self):
        """severity=critical + age>=7d → score=5 → 强制打开"""
        recap = self.RecapTask(
            task_id="test-1",
            severity="critical",
            topic="架构决策复盘",
            source="system",
            created_at=datetime.now().isoformat(),
            age_days=7,
        )
        decision = self.fr.should_force_open(recap)
        self.assertTrue(decision.should_force_open)
        self.assertGreaterEqual(decision.score, 4)

    def test_medium_no_time_no_force(self):
        """severity=medium + age=0d → score=0 → 对话轻提醒"""
        recap = self.RecapTask(
            task_id="test-2",
            severity="medium",
            topic="普通复盘",
            source="system",
            created_at=datetime.now().isoformat(),
            age_days=0,
        )
        decision = self.fr.should_force_open(recap)
        self.assertFalse(decision.should_force_open)
        self.assertEqual(decision.channel, "dialog_reminder")

    def test_high_age3_same_type_forces_open(self):
        """severity=high + age>=3d + same_type>=2 → score=5 → 强制打开"""
        recap = self.RecapTask(
            task_id="test-3",
            severity="high",
            topic="同类Bug",
            source="system",
            created_at=datetime.now().isoformat(),
            age_days=4,
            same_type_count=2,
        )
        decision = self.fr.should_force_open(recap)
        self.assertTrue(decision.should_force_open)

    def test_critical_age2_not_enough(self):
        """severity=critical + age=2d → score=3 → 不强制"""
        recap = self.RecapTask(
            task_id="test-4",
            severity="critical",
            topic="数据迁移复盘",
            source="system",
            created_at=datetime.now().isoformat(),
            age_days=2,
        )
        decision = self.fr.should_force_open(recap)
        self.assertFalse(decision.should_force_open)

    def test_related_file_adds_score(self):
        """上下文关联文件 +2"""
        recap = self.RecapTask(
            task_id="test-5",
            severity="high",
            topic="Docker配置",
            source="system",
            created_at=datetime.now().isoformat(),
            age_days=0,
            current_file="/path/to/docker-compose.yaml",
        )
        decision = self.fr.should_force_open(recap, {"current_file": "/path/to/docker-compose.yaml"})
        # severity=high(2) + related_file(2) = 4 → 强制
        self.assertTrue(decision.should_force_open)

    def test_promise_broken_adds_score(self):
        """承诺违约 +1"""
        recap = self.RecapTask(
            task_id="test-6",
            severity="critical",
            topic="架构复盘",
            source="system",
            created_at=datetime.now().isoformat(),
            age_days=2,
            user_promised=True,
        )
        # critical(3) + promise(1) = 4 → 强制
        decision = self.fr.should_force_open(recap)
        self.assertTrue(decision.should_force_open)


class TestUserScheduling(unittest.TestCase):
    """用户主动预约测试（蓝图 §9）"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        from core.app.forced_retrospective import ForcedRetrospective
        self.fr = ForcedRetrospective(db_path=str(Path(self.temp_dir) / "recap.db"))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_schedule_user_reminder(self):
        """用户预约可创建"""
        due = datetime.now() + timedelta(days=1)
        task_id = self.fr.schedule_user_reminder(
            user_request="1天后提醒我复盘",
            due_date=due,
        )
        self.assertTrue(task_id.startswith("user_reminder-recap-"))

        reminders = self.fr.list_user_reminders()
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0].user_request, "1天后提醒我复盘")
        self.assertEqual(reminders[0].source, "user")

    def test_cancel_user_reminder(self):
        """用户预约可取消"""
        due = datetime.now() + timedelta(days=1)
        task_id = self.fr.schedule_user_reminder(
            user_request="测试取消",
            due_date=due,
        )
        result = self.fr.cancel_user_reminder(task_id)
        self.assertTrue(result)

        reminders = self.fr.list_user_reminders()
        self.assertEqual(len(reminders), 0)

    def test_reschedule_user_reminder(self):
        """用户预约可改期"""
        due = datetime.now() + timedelta(days=1)
        task_id = self.fr.schedule_user_reminder(
            user_request="改期测试",
            due_date=due,
        )
        new_due = datetime.now() + timedelta(days=3)
        new_task_id = self.fr.reschedule_user_reminder(task_id, new_due)
        self.assertIsNotNone(new_task_id)

        # 旧任务应已取消
        reminders = self.fr.list_user_reminders()
        self.assertEqual(len(reminders), 1)
        self.assertTrue(reminders[0].task_id.startswith("user_reminder-recap-"))


class TestStartupCompensation(unittest.TestCase):
    """启动补偿测试（蓝图 §9 关键边界）"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        from core.app.forced_retrospective import ForcedRetrospective
        self.fr = ForcedRetrospective(db_path=str(Path(self.temp_dir) / "recap.db"))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("core.app.obsidian_opener.open_obsidian")
    def test_expired_user_reminder_opened(self, mock_open):
        """过期的用户预约 → 直接打开 Obsidian"""
        mock_open.return_value = True
        # 创建已过期的用户预约
        past_due = datetime.now() - timedelta(hours=2)
        self.fr.schedule_user_reminder(
            user_request="昨天提醒我复盘",
            due_date=past_due,
            target_page="00-Dashboard.md",
        )
        # 手动把 due_date 改到过去
        import sqlite3
        with sqlite3.connect(str(self.fr._db_path), timeout=10) as conn:
            conn.execute(
                "UPDATE recap_tasks SET due_date = ? WHERE source = 'user'",
                (past_due.isoformat(),),
            )

        expired = self.fr.startup_compensation()
        self.assertEqual(len(expired), 1)
        mock_open.assert_called()


class TestObsidianOpener(unittest.TestCase):
    """obsidian_opener 单元测试"""

    def test_build_uri(self):
        """URI 构建正确"""
        from core.app.obsidian_opener import _build_uri
        uri = _build_uri("MyVault", "00-Dashboard.md")
        self.assertIn("obsidian://open", uri)
        self.assertIn("vault=MyVault", uri)
        self.assertIn("file=00-Dashboard", uri)
        # .md 后缀应被移除
        self.assertNotIn("00-Dashboard.md", uri)

    @patch("core.app.obsidian_opener.subprocess.run")
    def test_open_file_macos(self, mock_run):
        """macOS 上 open -a Obsidian"""
        mock_run.return_value = MagicMock(returncode=0)
        with patch("core.app.obsidian_opener.platform.system", return_value="Darwin"):
            from core.app.obsidian_opener import _open_file
            # 需要真实文件
            result = _open_file("nonexistent-page-for-test")
            # 文件不存在应该返回 False
            self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
