"""
forced_retrospective + obsidian_opener 单元测试

覆盖项：
- should_force_open 组合权重算法
- 用户预约复盘 schedule/cancel/reschedule
- 启动补偿 startup_compensation
- open_obsidian 跨平台逻辑（mock）
"""

import tempfile
import shutil
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

from core.app.forced_retrospective import ForcedRetrospective


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
        decision = self.fr.should_force_open(
            recap, {"current_file": "/path/to/docker-compose.yaml"}
        )
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


class TestStartupCompensation(unittest.TestCase):
    """启动补偿测试（蓝图 §9 关键边界）"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        from core.app.forced_retrospective import ForcedRetrospective

        self.fr = ForcedRetrospective(db_path=str(Path(self.temp_dir) / "recap.db"))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch(
        "core.app.forced_retrospective.ForcedRetrospective._is_user_active_time", return_value=True
    )
    @patch("core.app.forced_retrospective.open_obsidian")
    def test_expired_user_reminder_opened(self, mock_open, _mock_active):
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

    @patch(
        "core.app.forced_retrospective.ForcedRetrospective._is_user_active_time", return_value=True
    )
    @patch("core.app.forced_retrospective.open_obsidian")
    def test_expired_user_reminder_keeps_pending_when_open_fails(self, mock_open, _mock_active):
        """Obsidian 打开失败时不能吞掉用户预约提醒"""
        mock_open.return_value = False
        past_due = datetime.now() - timedelta(hours=2)
        self.fr.schedule_user_reminder(
            user_request="失败后仍需提醒",
            due_date=past_due,
            target_page="00-Dashboard.md",
        )

        expired = self.fr.startup_compensation()

        self.assertEqual(len(expired), 1)
        self.assertEqual(len(self.fr.list_user_reminders()), 1)

    @patch(
        "core.app.forced_retrospective.ForcedRetrospective._is_user_active_time", return_value=True
    )
    @patch("core.app.forced_retrospective.open_obsidian", return_value=True)
    def test_startup_compensation_respects_max_tasks(self, _mock_open, _mock_active):
        """启动补偿应受 max_tasks 限制，避免停机后一次性弹出过多窗口。"""
        for i in range(5):
            self.fr.schedule_user_reminder(
                user_request=f"复盘{i}",
                due_date=datetime.now() - timedelta(hours=1),
                target_page="00-Dashboard.md",
            )
        expired = self.fr.startup_compensation(max_tasks=2)
        self.assertLessEqual(len(expired), 2)


class TestDuplicateRecap(unittest.TestCase):
    """系统复盘去重测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        from core.app.forced_retrospective import ForcedRetrospective

        self.fr = ForcedRetrospective(db_path=str(Path(self.temp_dir) / "recap.db"))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch.object(
        ForcedRetrospective, "_generate_reminder_page", return_value="08-Reminders/复盘提醒-dup"
    )
    def test_create_system_recap_dedupes_same_topic(self, _mock_page):
        """同 topic 的 pending 系统复盘应合并，而非创建多个相同页面"""
        tid1 = self.fr.create_system_recap(
            topic="存在 1 个显著偏差",
            severity="medium",
            context="初始上下文",
            suggested_points="- 初始建议",
        )
        tid2 = self.fr.create_system_recap(
            topic="存在 1 个显著偏差",
            severity="high",
            context="更新后的上下文",
            suggested_points="- 更新后的建议",
        )

        assert tid1 == tid2
        pending = self.fr.get_pending_system_recaps()
        assert len(pending) == 1
        recap = pending[0]
        assert recap.severity == "high"
        assert recap.context == "更新后的上下文"
        assert recap.suggested_points == "- 更新后的建议"

    @patch.object(
        ForcedRetrospective,
        "_generate_reminder_page",
        return_value="08-Reminders/复盘提醒-session-skip",
    )
    def test_create_from_session_end_creates_low_quality_recap(self, _mock_page):
        """低质量蒸馏跳过应创建 medium 系统复盘任务。"""
        task_id = self.fr._create_from_session_end(
            "session-low-quality-1234567890",
            "skipped_low_quality",
        )

        assert task_id is not None
        pending = self.fr.get_pending_system_recaps()
        assert len(pending) == 1
        recap = pending[0]
        assert recap.severity == "medium"
        assert "skipped_low_quality" in recap.topic
        assert "低质量" in recap.suggested_points

    @patch.object(
        ForcedRetrospective,
        "_generate_reminder_page",
        return_value="08-Reminders/复盘提醒-pipeline-skip",
    )
    def test_create_from_session_end_creates_pipeline_recap(self, _mock_page):
        """蒸馏管道跳过应创建 high 系统复盘任务。"""
        task_id = self.fr._create_from_session_end(
            "session-pipeline-1234567890",
            "skipped_by_pipeline",
        )

        assert task_id is not None
        pending = self.fr.get_pending_system_recaps()
        assert len(pending) == 1
        recap = pending[0]
        assert recap.severity == "high"
        assert "skipped_by_pipeline" in recap.topic
        assert "API" in recap.suggested_points

    def test_create_from_session_end_ignores_untracked_reason(self):
        """非蒸馏跳过原因不应创建系统复盘任务。"""
        task_id = self.fr._create_from_session_end(
            "session-ignored-1234567890",
            "not_a_skip_reason",
        )

        assert task_id is None
        assert self.fr.get_pending_system_recaps() == []


def test_system_recap_reminder_page_is_self_explanatory_for_agent_handoff(
    tmp_path,
):
    """提醒页应先说明复盘对象，并给出可复制给 Agent 的 task_id 启动语句。"""
    from core.app.forced_retrospective import ForcedRetrospective

    db_path = tmp_path / "recap.db"
    reminders_dir = tmp_path / "08-Reminders"
    forced = ForcedRetrospective(db_path=str(db_path))

    with patch("core.app.forced_retrospective.REMINDERS_DIR", reminders_dir):
        task_id = forced.create_system_recap(
            topic="存在 1 个显著偏差",
            severity="high",
            context=(
                "**任务类型**：coding/bugfix\n"
                "**复盘摘要**：存在 1 个显著偏差\n\n"
                "**关键差异**：\n"
                "- [high] budget: 预期 100 / 实际 50 — 显著偏差"
            ),
            suggested_points="- 优先分析 1 个显著偏差（budget 等）的根因",
        )

    target_page = forced.get_recap_task(task_id).target_page
    content = (tmp_path / f"{target_page}.md").read_text(encoding="utf-8")

    assert "## 一、本次要复盘什么" in content
    assert content.index("## 一、本次要复盘什么") < content.index("## 四、如何和 Agent 继续")
    assert f"请复盘 task_id={task_id}" in content
    assert "如果工具返回 `not_found`" in content
    assert "偏差项：预算/资源消耗（budget）" in content
    assert "预期 100 / 实际 50" in content
    assert "先从下面这句话开始" in content
    assert "可以说\"复盘一下 {主题}\"" not in content


def test_system_recap_reminder_page_refresh_is_idempotent(
    tmp_path,
):
    """从旧提醒页内容再次生成时，不应重复追加证据摘录或三问模板。"""
    from core.app.forced_retrospective import ForcedRetrospective

    db_path = tmp_path / "recap.db"
    reminders_dir = tmp_path / "08-Reminders"
    forced = ForcedRetrospective(db_path=str(db_path))

    with patch("core.app.forced_retrospective.REMINDERS_DIR", reminders_dir):
        task_id = forced.create_system_recap(
            topic="存在 1 个显著偏差",
            severity="high",
            context=(
                "**任务类型**：coding/bugfix\n"
                "**关键差异**：\n"
                "- [high] budget: 预期 100 / 实际 50 — 显著偏差\n\n"
                "### 结构化证据摘录\n\n"
                "- 偏差项：budget\n"
                "  - 预期 100 / 实际 50"
            ),
            suggested_points=(
                "- 优先分析 1 个显著偏差（预算/资源消耗（预算/资源消耗（budget）） 等）的根因\n\n"
                "建议至少回答三件事：\n\n"
                "1. 当时想达成什么，实际发生了什么？"
            ),
        )

    target_page = forced.get_recap_task(task_id).target_page
    content = (tmp_path / f"{target_page}.md").read_text(encoding="utf-8")

    assert content.count("### 结构化证据摘录") == 1
    assert content.count("建议至少回答三件事：") == 1
    assert "偏差项：budget\n" not in content
    assert "预算/资源消耗（budget）" in content
    assert "预算/资源消耗（预算/资源消耗（budget））" not in content


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
