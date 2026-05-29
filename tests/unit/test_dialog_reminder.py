"""
Dialog Reminder 单元测试

覆盖项：
- DialogReminderQueue: 入队、触发推送、兜底推送、解决、忽略、推迟、冷却期、聚合
- PageBannerInjector: 注入、移除、替换横幅
"""

import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.kia.dialog_reminder import (
    DialogReminderQueue, PageBannerInjector, ReminderEntry,
)


class TestDialogReminderQueue(unittest.TestCase):
    """DialogReminderQueue 核心功能测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "reminders.db"
        self.queue = DialogReminderQueue(db_path=str(self.db_path))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_enqueue_new_reminder(self):
        """入队新提醒应生成 ID"""
        rid = self.queue.enqueue(
            issue_id="issue-abc",
            page_path="03-Tech/redis.md",
            severity="high",
            content="Redis 连接池踩坑",
            choices=["已更新", "仍有效", "忽略"],
        )
        self.assertTrue(rid.startswith("rem-"))
        pending = self.queue.get_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].severity, "high")

    def test_enqueue_duplicate_updates_content(self):
        """重复入队应更新内容而非重复创建"""
        rid1 = self.queue.enqueue(
            issue_id="issue-abc", page_path="redis.md",
            severity="medium", content="old", choices=["a"],
        )
        rid2 = self.queue.enqueue(
            issue_id="issue-abc", page_path="redis.md",
            severity="high", content="new", choices=["b"],
        )
        self.assertEqual(rid1, rid2)
        pending = self.queue.get_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].severity, "high")
        self.assertEqual(pending[0].content, "new")

    def test_enqueue_reopen_resolved(self):
        """对已解决的提醒重新入队应重新打开"""
        rid = self.queue.enqueue(
            issue_id="issue-abc", page_path="redis.md",
            severity="medium", content="test", choices=["a"],
        )
        self.queue.resolve(rid, "已更新")
        self.assertEqual(self.queue.get_pending(), [])

        rid2 = self.queue.enqueue(
            issue_id="issue-abc", page_path="redis.md",
            severity="high", content="regression", choices=["b"],
        )
        self.assertEqual(rid, rid2)
        pending = self.queue.get_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].status, "pending")

    def test_on_knowledge_triggered(self):
        """知识触发应返回关联页面的待推送提醒"""
        self.queue.enqueue(
            issue_id="i1", page_path="redis.md",
            severity="high", content="过期", choices=["a"],
        )
        self.queue.enqueue(
            issue_id="i2", page_path="docker.md",
            severity="medium", content="孤立", choices=["b"],
        )
        pushed = self.queue.on_knowledge_triggered("redis.md")
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0].issue_id, "i1")
        # 标记为 pushed 后不应再 pending
        self.assertEqual(self.queue.get_pending(page_path="redis.md"), [])

    def test_on_user_active_fallback(self):
        """兜底推送应返回 pending 提醒"""
        self.queue.enqueue(
            issue_id="i1", page_path="a.md",
            severity="critical", content="c1", choices=["a"],
        )
        self.queue.enqueue(
            issue_id="i2", page_path="b.md",
            severity="high", content="c2", choices=["b"],
        )
        pushed = self.queue.on_user_active(max_results=3)
        self.assertEqual(len(pushed), 2)
        # critical 优先
        self.assertEqual(pushed[0].severity, "critical")

    def test_on_user_active_defers_overflow(self):
        """超出 max_results 的应被 defer"""
        for i in range(5):
            self.queue.enqueue(
                issue_id=f"i{i}", page_path=f"p{i}.md",
                severity="medium", content=f"c{i}", choices=["a"],
            )
        pushed = self.queue.on_user_active(max_results=2)
        self.assertEqual(len(pushed), 2)
        # 剩余 3 个应被 deferred
        pending = self.queue.get_pending()
        self.assertEqual(len(pending), 0)
        # deferred 的 defer_until 应已设置
        with self.queue._conn() as conn:
            rows = conn.execute(
                "SELECT status, defer_until FROM dialog_reminders WHERE status = 'deferred'"
            ).fetchall()
            self.assertEqual(len(rows), 3)
            self.assertIsNotNone(rows[0]["defer_until"])

    def test_resolve(self):
        """resolve 应更新状态"""
        rid = self.queue.enqueue(
            issue_id="i1", page_path="a.md",
            severity="medium", content="c1", choices=["a"],
        )
        ok = self.queue.resolve(rid, "已更新")
        self.assertTrue(ok)
        self.assertEqual(self.queue.get_pending(), [])
        with self.queue._conn() as conn:
            row = conn.execute(
                "SELECT status, resolved_choice FROM dialog_reminders WHERE reminder_id = ?",
                (rid,),
            ).fetchone()
            self.assertEqual(row["status"], "resolved")
            self.assertEqual(row["resolved_choice"], "已更新")

    def test_ignore(self):
        """ignore 应更新状态为 ignored"""
        rid = self.queue.enqueue(
            issue_id="i1", page_path="a.md",
            severity="medium", content="c1", choices=["a"],
        )
        ok = self.queue.ignore(rid)
        self.assertTrue(ok)
        with self.queue._conn() as conn:
            row = conn.execute(
                "SELECT status FROM dialog_reminders WHERE reminder_id = ?",
                (rid,),
            ).fetchone()
            self.assertEqual(row["status"], "ignored")

    def test_defer(self):
        """defer 应设置 defer_until"""
        rid = self.queue.enqueue(
            issue_id="i1", page_path="a.md",
            severity="medium", content="c1", choices=["a"],
        )
        ok = self.queue.defer(rid, hours=48)
        self.assertTrue(ok)
        with self.queue._conn() as conn:
            row = conn.execute(
                "SELECT status, defer_until FROM dialog_reminders WHERE reminder_id = ?",
                (rid,),
            ).fetchone()
            self.assertEqual(row["status"], "deferred")
            self.assertIsNotNone(row["defer_until"])

    def test_cooldown(self):
        """冷却期内不应被推送"""
        rid = self.queue.enqueue(
            issue_id="i1", page_path="a.md",
            severity="medium", content="c1", choices=["a"],
        )
        self.queue.set_cooldown(rid, hours=24)
        self.assertTrue(self.queue.is_in_cooldown(rid))
        # 冷却期内 on_knowledge_triggered 不应返回
        triggered = self.queue.on_knowledge_triggered("a.md")
        self.assertEqual(len(triggered), 0)

    def test_aggregate_for_page_single(self):
        """单条提醒聚合应返回自身"""
        self.queue.enqueue(
            issue_id="i1", page_path="redis.md",
            severity="medium", content="孤立", choices=["a"],
        )
        agg = self.queue.aggregate_for_page("redis.md")
        self.assertIsNotNone(agg)
        self.assertEqual(agg.issue_id, "i1")

    def test_aggregate_for_page_multiple(self):
        """多条提醒聚合为一条"""
        self.queue.enqueue(
            issue_id="i1", page_path="redis.md",
            severity="medium", content="孤立页面", choices=["a"],
        )
        self.queue.enqueue(
            issue_id="i2", page_path="redis.md",
            severity="low", content="内容过短", choices=["b"],
        )
        agg = self.queue.aggregate_for_page("redis.md")
        self.assertIsNotNone(agg)
        self.assertIn("2 个优化建议", agg.content)
        self.assertEqual(agg.severity, "medium")  # 取最高严重度

    def test_cleanup_resolved(self):
        """清理已解决旧记录"""
        rid = self.queue.enqueue(
            issue_id="i1", page_path="a.md",
            severity="medium", content="c1", choices=["a"],
        )
        self.queue.resolve(rid, "ok")
        # 修改 resolved_at 为过去
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        with self.queue._conn() as conn:
            conn.execute(
                "UPDATE dialog_reminders SET resolved_at = ? WHERE reminder_id = ?",
                (old, rid),
            )
            conn.commit()
        deleted = self.queue.cleanup_resolved(retention_days=30)
        self.assertEqual(deleted, 1)


class TestPageBannerInjector(unittest.TestCase):
    """PageBannerInjector 测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_inject_banner_no_frontmatter(self):
        """无 frontmatter 的页面应插入到开头"""
        page = Path(self.temp_dir) / "test.md"
        page.write_text("# Hello\nworld\n", encoding="utf-8")

        injector = PageBannerInjector()
        ok = injector.inject_banner(page, ["> ⚠️ 提醒"], issue_id="i1")
        self.assertTrue(ok)
        text = page.read_text(encoding="utf-8")
        self.assertIn("<!-- mnemos-reminder -->", text)
        self.assertIn("<!-- /mnemos-reminder -->", text)
        self.assertIn("⚠️ 提醒", text)

    def test_inject_banner_with_frontmatter(self):
        """有 frontmatter 的页面应插入到 frontmatter 之后"""
        page = Path(self.temp_dir) / "test.md"
        page.write_text("---\ntype: tech\n---\n\n# Hello\n", encoding="utf-8")

        injector = PageBannerInjector()
        ok = injector.inject_banner(page, ["> ⚠️ 提醒"], issue_id="i1")
        self.assertTrue(ok)
        text = page.read_text(encoding="utf-8")
        # frontmatter 之后应有横幅
        self.assertRegex(text, r"---\n\n<!-- mnemos-reminder -->")

    def test_remove_banner(self):
        """移除横幅应清理 marker 间内容"""
        page = Path(self.temp_dir) / "test.md"
        original = "# Hello\nworld\n"
        page.write_text(original, encoding="utf-8")

        injector = PageBannerInjector()
        injector.inject_banner(page, ["> ⚠️ 提醒"])
        self.assertTrue(injector.has_banner(page))

        removed = injector.remove_banner(page)
        self.assertTrue(removed)
        self.assertFalse(injector.has_banner(page))
        text = page.read_text(encoding="utf-8")
        self.assertEqual(text, original)

    def test_replace_existing_banner(self):
        """已有横幅时应替换而非追加"""
        page = Path(self.temp_dir) / "test.md"
        page.write_text("# Hello\n", encoding="utf-8")

        injector = PageBannerInjector()
        injector.inject_banner(page, ["> 旧提醒"])
        injector.inject_banner(page, ["> 新提醒"])

        text = page.read_text(encoding="utf-8")
        self.assertIn("新提醒", text)
        self.assertNotIn("旧提醒", text)
        # 只应有一个横幅
        self.assertEqual(text.count("<!-- mnemos-reminder -->"), 1)

    def test_has_banner_nonexistent(self):
        """不存在的页面应返回 False"""
        injector = PageBannerInjector()
        self.assertFalse(injector.has_banner(Path(self.temp_dir) / "none.md"))


class TestReminderRenderer(unittest.TestCase):
    """ReminderRenderer 测试"""

    def test_render_dialog(self):
        """渲染对话内容应包含交互选项"""
        from core.kia.dialog_reminder import ReminderRenderer, ReminderEntry

        renderer = ReminderRenderer()
        entry = ReminderEntry(
            reminder_id="rem-1", issue_id="i1",
            page_path="redis.md", severity="high",
            content="Redis 连接池配置已过期",
            choices=["已更新", "仍有效", "忽略"],
        )
        text = renderer.render_dialog(entry)
        self.assertIn('<wiki-context type="reminder"', text)
        self.assertIn("Redis 连接池配置已过期", text)
        self.assertIn("[选择：已更新]", text)
        self.assertIn("[选择：仍有效]", text)
        self.assertIn("[选择：忽略]", text)
        self.assertIn("</wiki-context>", text)

    def test_render_banner(self):
        """渲染横幅内容"""
        from core.kia.dialog_reminder import ReminderRenderer, ReminderEntry

        renderer = ReminderRenderer()
        entry = ReminderEntry(
            reminder_id="rem-1", issue_id="i1",
            page_path="redis.md", severity="medium",
            content="孤立页面",
            choices=["发现相关页面", "忽略"],
        )
        lines = renderer.render_banner(entry)
        self.assertTrue(any("📋 **知识提醒**" in l for l in lines))
        self.assertTrue(any("孤立页面" in l for l in lines))
        self.assertTrue(any("[发现相关页面]" in l for l in lines))

    def test_render_aggregated_dialog(self):
        """渲染聚合提醒"""
        from core.kia.dialog_reminder import ReminderRenderer, ReminderEntry

        renderer = ReminderRenderer()
        entries = [
            ReminderEntry(reminder_id="r1", issue_id="i1", page_path="docker.md",
                          severity="medium", content="孤立页面"),
            ReminderEntry(reminder_id="r2", issue_id="i2", page_path="docker.md",
                          severity="low", content="内容过短"),
        ]
        text = renderer.render_aggregated_dialog("Docker Compose", entries)
        self.assertIn("Docker Compose", text)
        self.assertIn("存在 2 个优化建议", text)
        self.assertIn("孤立页面", text)
        self.assertIn("内容过短", text)
        self.assertIn("[选择：查看详情]", text)


if __name__ == "__main__":
    unittest.main()
