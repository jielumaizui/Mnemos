"""
Dialog Reminder 单元测试

覆盖项：
- DialogReminderQueue: 入队、触发推送、兜底推送、解决、忽略、推迟、冷却期、聚合
- PageBannerInjector: 注入、移除、替换横幅
"""

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.kia.dialog_reminder import (
    DialogReminderQueue,
    PageBannerInjector,
    ReminderEntry,
    ReminderRenderer,
)
from core.trust.proposal_queue import ProposalQueue


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

    def test_authenticated_response_records_canonical_reaction(self):
        """认证卡片响应应先进入统一反馈归因链。"""

        reminder_id = self.queue.enqueue(
            issue_id="issue-authenticated",
            page_path="redis.md",
            severity="medium",
            content="verify reminder",
            choices=["handled", "ignore"],
        )
        initialize_cognitive_state_schema(
            Path(self.temp_dir) / "producer_consumer_ledger.db"
        )
        principal = PrincipalEnvelope(
            principal_id="user:reminder-feedback",
            agent="codex",
            host_kind="test",
            capability_id="reminder-feedback",
            capabilities=frozenset({"memory_read", "memory_write"}),
            allowed_projects=frozenset({"mnemos"}),
        )

        result = self.queue.record_user_response(
            reminder_id,
            "ignore",
            principal=principal,
            narrowing=AccessNarrowing(project="mnemos"),
            reason="not relevant",
        )

        self.assertTrue(result["success"])
        self.assertEqual(
            result["canonical_feedback"]["disposition"],
            "record_only",
        )
        self.assertEqual(
            {
                item["disposition"]
                for item in result["canonical_feedback"]["terminal_receipts"]
            },
            {"intentional_skip"},
        )
        self.assertEqual(self.queue.get_pending(), [])

    def test_enqueue_duplicate_updates_content(self):
        """重复入队应更新内容而非重复创建"""
        rid1 = self.queue.enqueue(
            issue_id="issue-abc",
            page_path="redis.md",
            severity="medium",
            content="old",
            choices=["a"],
        )
        rid2 = self.queue.enqueue(
            issue_id="issue-abc",
            page_path="redis.md",
            severity="high",
            content="new",
            choices=["b"],
        )
        self.assertEqual(rid1, rid2)
        pending = self.queue.get_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].severity, "high")
        self.assertEqual(pending[0].content, "new")

    def test_enqueue_reopen_resolved(self):
        """对已解决的提醒重新入队应重新打开"""
        rid = self.queue.enqueue(
            issue_id="issue-abc",
            page_path="redis.md",
            severity="medium",
            content="test",
            choices=["a"],
        )
        self.queue.resolve(rid, "已更新")
        self.assertEqual(self.queue.get_pending(), [])

        rid2 = self.queue.enqueue(
            issue_id="issue-abc",
            page_path="redis.md",
            severity="high",
            content="regression",
            choices=["b"],
        )
        self.assertEqual(rid, rid2)
        pending = self.queue.get_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].status, "pending")

    def test_get_by_issue_returns_active_reminder(self):
        """get_by_issue 应按 issue_id 返回未关闭提醒"""
        rid = self.queue.enqueue(
            issue_id="issue-abc",
            page_path="redis.md",
            severity="medium",
            content="test",
            choices=["a"],
        )

        entry = self.queue.get_by_issue("issue-abc")

        self.assertIsNotNone(entry)
        self.assertEqual(entry.reminder_id, rid)
        self.assertEqual(entry.issue_id, "issue-abc")

    def test_get_by_issue_excludes_closed_reminders(self):
        """get_by_issue 不应返回已解决或已忽略提醒"""
        resolved = self.queue.enqueue(
            issue_id="issue-resolved",
            page_path="redis.md",
            severity="medium",
            content="resolved",
            choices=["a"],
        )
        ignored = self.queue.enqueue(
            issue_id="issue-ignored",
            page_path="docker.md",
            severity="medium",
            content="ignored",
            choices=["a"],
        )

        self.queue.resolve(resolved, "已处理")
        self.queue.ignore(ignored)

        self.assertIsNone(self.queue.get_by_issue("issue-resolved"))
        self.assertIsNone(self.queue.get_by_issue("issue-ignored"))

    def test_get_by_issue_prefers_highest_severity(self):
        """同一 issue 多条未关闭提醒时应返回最高严重度"""
        self.queue.enqueue(
            issue_id="issue-abc",
            page_path="low.md",
            severity="low",
            content="low",
            choices=["a"],
        )
        critical = self.queue.enqueue(
            issue_id="issue-abc",
            page_path="critical.md",
            severity="critical",
            content="critical",
            choices=["a"],
        )

        entry = self.queue.get_by_issue("issue-abc")

        self.assertIsNotNone(entry)
        self.assertEqual(entry.reminder_id, critical)

    def test_on_knowledge_triggered(self):
        """知识触发应返回关联页面的待推送提醒"""
        self.queue.enqueue(
            issue_id="i1",
            page_path="redis.md",
            severity="high",
            content="过期",
            choices=["a"],
        )
        self.queue.enqueue(
            issue_id="i2",
            page_path="docker.md",
            severity="medium",
            content="孤立",
            choices=["b"],
        )
        pushed = self.queue.on_knowledge_triggered("redis.md")
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0].issue_id, "i1")
        # 标记为 pushed 后不应再 pending
        self.assertEqual(self.queue.get_pending(page_path="redis.md"), [])

    def test_on_user_active_fallback(self):
        """兜底推送应返回 pending 提醒"""
        self.queue.enqueue(
            issue_id="i1",
            page_path="a.md",
            severity="critical",
            content="c1",
            choices=["a"],
        )
        self.queue.enqueue(
            issue_id="i2",
            page_path="b.md",
            severity="high",
            content="c2",
            choices=["b"],
        )
        pushed = self.queue.on_user_active(max_results=3)
        self.assertEqual(len(pushed), 2)
        # critical 优先
        self.assertEqual(pushed[0].severity, "critical")

    def test_on_user_active_defers_overflow(self):
        """超出 max_results 的应被 defer"""
        for i in range(5):
            self.queue.enqueue(
                issue_id=f"i{i}",
                page_path=f"p{i}.md",
                severity="medium",
                content=f"c{i}",
                choices=["a"],
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

    def test_on_user_active_uses_configured_overflow_defer_hours(self):
        """超出预算的提醒 defer 时长应来自 delivery profile。"""
        for i in range(2):
            self.queue.enqueue(
                issue_id=f"overflow-{i}",
                page_path=f"overflow-{i}.md",
                severity="medium",
                content=f"c{i}",
                choices=["a"],
            )

        class _FakePolicy:
            def get(self, key, default=None):
                values = {
                    "delivery.preference": "balanced",
                    "delivery.profiles.balanced": {},
                    "delivery.profiles.balanced.per_task_total": 1,
                    "delivery.profiles.balanced.per_task_hint": 1,
                    "delivery.profiles.balanced.overflow_defer_hours": 6,
                }
                return values.get(key, default)

        queue = DialogReminderQueue(db_path=str(self.db_path), policy=_FakePolicy())
        pushed = queue.on_user_active()

        self.assertEqual(len(pushed), 1)
        with queue._conn() as conn:
            defer_until = conn.execute(
                """
                SELECT defer_until
                FROM dialog_reminders
                WHERE status = 'deferred'
                """
            ).fetchone()["defer_until"]
        parsed = datetime.fromisoformat(defer_until)
        min_expected = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5)
        self.assertGreaterEqual(parsed, min_expected)

    def test_on_user_active_uses_delivery_profile_from_effective_policy(self):
        """兜底推送上限应优先从 delivery profile 读取。"""
        for i in range(5):
            self.queue.enqueue(
                issue_id=f"i{i}",
                page_path=f"p{i}.md",
                severity="medium",
                content=f"c{i}",
                choices=["a"],
            )

        class _FakePolicy:
            def __init__(self, values):
                self._values = values

            def get(self, key, default=None):
                return self._values.get(key, default)

        policy = _FakePolicy(
            {
                "delivery.preference": "balanced",
                "delivery.profiles.balanced": {},
                "delivery.profiles.balanced.per_task_total": 2,
                "app.push_max_items": 5,
            }
        )
        queue = DialogReminderQueue(db_path=str(self.db_path), policy=policy)
        pushed = queue.on_user_active()
        self.assertEqual(len(pushed), 2)

    def test_empty_delivery_event_id_is_deferred_not_marked_pushed(self):
        """A routing failure must never create a fake delivered reminder."""
        reminder_id = self.queue.enqueue(
            issue_id="empty-delivery-event",
            page_path="empty-event.md",
            severity="high",
            content="must not be marked delivered",
            choices=["ack"],
        )
        with patch.object(self.queue, "_route_delivery", return_value=""):
            pushed = self.queue.on_user_active()

        self.assertEqual(pushed, [])
        reminder = self.queue._get_by_id(reminder_id)
        self.assertIsNotNone(reminder)
        self.assertEqual(reminder.status, "deferred")
        self.assertEqual(reminder.delivery_event_id, "")

    def test_routed_delivery_is_not_marked_pushed_before_host_ack(self):
        """Routing creates a host command, not evidence that the user saw it."""
        reminder_id = self.queue.enqueue(
            issue_id="routed-without-presentation",
            page_path="routed.md",
            severity="high",
            content="await host presentation",
            choices=["ack"],
        )
        with patch.object(self.queue, "_route_delivery", return_value="delivery-route-1"):
            routed = self.queue.on_user_active()

        self.assertEqual(len(routed), 1)
        reminder = self.queue._get_by_id(reminder_id)
        self.assertIsNotNone(reminder)
        self.assertEqual(reminder.status, "routed")
        self.assertEqual(reminder.delivery_event_id, "delivery-route-1")

    def test_authenticated_host_ack_transitions_routed_reminder_to_pushed(self):
        """Only the host that routed the command may acknowledge its render."""
        principal = PrincipalEnvelope(
            principal_id="local-user:501:mnemos-test",
            agent="mnemos-cli",
            host_kind="local_cli",
            capability_id="local-cli-os-account:501",
            capabilities=frozenset({"memory_read", "memory_write"}),
            allowed_projects=frozenset({"mnemos"}),
        )
        reminder_id = self.queue.enqueue(
            issue_id="presentation-ack",
            page_path="presentation.md",
            severity="high",
            content="must be actually rendered",
            choices=["ack"],
        )

        routed = self.queue.on_user_active(principal=principal)
        self.assertEqual(len(routed), 1)
        self.assertEqual(self.queue._get_by_id(reminder_id).status, "routed")

        receipt = self.queue.record_presentation(
            reminder_id,
            principal=principal,
            rendered_content_hash="sha256:" + "c" * 64,
        )

        self.assertTrue(receipt["success"])
        reminder = self.queue._get_by_id(reminder_id)
        self.assertEqual(reminder.status, "pushed")
        self.assertEqual(receipt["delivery_event_id"], reminder.delivery_event_id)

    def test_routed_reminder_expires_as_typed_presentation_timeout(self):
        """No response ends waiting; it must not become positive or negative feedback."""
        principal = PrincipalEnvelope(
            principal_id="local-user:501:mnemos-test",
            agent="mnemos-cli",
            host_kind="local_cli",
            capability_id="local-cli-os-account:501",
            capabilities=frozenset({"memory_read", "memory_write"}),
            allowed_projects=frozenset({"mnemos"}),
        )
        reminder_id = self.queue.enqueue(
            issue_id="presentation-timeout",
            page_path="presentation-timeout.md",
            severity="medium",
            content="await host display",
            choices=["ack"],
        )
        self.queue.on_user_active(principal=principal)
        with self.queue._conn() as conn:
            conn.execute(
                "UPDATE dialog_reminders SET created_at='2000-01-01T00:00:00' WHERE reminder_id=?",
                (reminder_id,),
            )
            conn.commit()

        expired = self.queue.expire_stale_pending(days=1)

        self.assertEqual(expired, 1)
        reminder = self.queue._get_by_id(reminder_id)
        self.assertEqual(reminder.status, "expired")
        self.assertEqual(reminder.resolved_choice, "presentation_timeout")

    def test_on_user_active_keeps_app_push_max_items_as_legacy_fallback(self):
        """旧 app.push_max_items 只在 delivery profile 未配置时作为兜底。"""
        for i in range(5):
            self.queue.enqueue(
                issue_id=f"legacy-{i}",
                page_path=f"legacy-{i}.md",
                severity="medium",
                content=f"c{i}",
                choices=["a"],
            )

        class _FakePolicy:
            def __init__(self, values):
                self._values = values

            def get(self, key, default=None):
                return self._values.get(key, default)

        policy = _FakePolicy({"app.push_max_items": 2})
        queue = DialogReminderQueue(db_path=str(self.db_path), policy=policy)
        pushed = queue.on_user_active()
        self.assertEqual(len(pushed), 2)

    def test_on_user_active_uses_adaptive_config_effective_value(self):
        """未注入 policy 时应通过 AdaptiveConfig 读取 delivery profile。"""
        for i in range(5):
            self.queue.enqueue(
                issue_id=f"i{i}",
                page_path=f"p{i}.md",
                severity="medium",
                content=f"c{i}",
                choices=["a"],
            )

        calls = []

        class _FakeAdaptiveConfig:
            def get_effective(self, key, default=None):
                calls.append((key, default))
                values = {
                    "delivery.preference": "balanced",
                    "delivery.profiles.balanced": {},
                    "delivery.profiles.balanced.per_task_total": 2,
                }
                return values.get(key, default)

        with patch("core.kia.adaptive_config.AdaptiveConfig", _FakeAdaptiveConfig):
            pushed = self.queue.on_user_active()

        self.assertEqual(len(pushed), 2)
        self.assertIn(("delivery.preference", "balanced"), calls)
        self.assertIn(("delivery.profiles.balanced.per_task_total", None), calls)

    def test_resolve(self):
        """resolve 应更新状态"""
        rid = self.queue.enqueue(
            issue_id="i1",
            page_path="a.md",
            severity="medium",
            content="c1",
            choices=["a"],
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
            issue_id="i1",
            page_path="a.md",
            severity="medium",
            content="c1",
            choices=["a"],
        )
        ok = self.queue.ignore(rid)
        self.assertTrue(ok)
        with self.queue._conn() as conn:
            row = conn.execute(
                "SELECT status FROM dialog_reminders WHERE reminder_id = ?",
                (rid,),
            ).fetchone()
            self.assertEqual(row["status"], "ignored")

    def test_dismiss(self):
        """dismiss 应更新状态为 dismissed 并保留原因"""
        rid = self.queue.enqueue(
            issue_id="i1",
            page_path="a.md",
            severity="medium",
            content="c1",
            choices=["a"],
        )
        ok = self.queue.dismiss(rid, "not actionable")
        self.assertTrue(ok)
        with self.queue._conn() as conn:
            row = conn.execute(
                "SELECT status, resolved_choice FROM dialog_reminders WHERE reminder_id = ?",
                (rid,),
            ).fetchone()
            self.assertEqual(row["status"], "dismissed")
            self.assertEqual(row["resolved_choice"], "not actionable")

    def test_dismiss_source_event_is_idempotent(self):
        rid = self.queue.enqueue(
            issue_id="i1",
            page_path="a.md",
            severity="medium",
            content="c1",
            choices=["a"],
        )

        self.assertTrue(
            self.queue.dismiss(rid, "recap_inaccurate", source_event_id="correction-1")
        )
        self.assertTrue(
            self.queue.dismiss(rid, "recap_inaccurate", source_event_id="correction-1")
        )
        with self.queue._conn() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM dialog_reminder_corrections WHERE source_event_id=?",
                    ("correction-1",),
                ).fetchone()[0],
                1,
            )

    def test_defer(self):
        """defer 应设置 defer_until"""
        rid = self.queue.enqueue(
            issue_id="i1",
            page_path="a.md",
            severity="medium",
            content="c1",
            choices=["a"],
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

    def test_reminder_response_stays_in_reminder_state_without_learning_fanout(self):
        """resolve/ignore/defer 只更新提醒状态，不得伪装成认知反馈。"""
        import sqlite3

        class _FakePolicy:
            def get(self, key, default=None):
                values = {
                    "delivery.preference": "balanced",
                    "delivery.profiles.balanced": {},
                    "delivery.profiles.balanced.daily_total": 10,
                    "delivery.profiles.balanced.per_task_total": 3,
                    "delivery.profiles.balanced.per_task_hint": 3,
                    "delivery.profiles.balanced.per_task_warn": 1,
                }
                return values.get(key, default)

        queue = DialogReminderQueue(db_path=str(self.db_path), policy=_FakePolicy())
        for action in ("resolve", "ignore", "defer"):
            queue.enqueue(
                issue_id=f"issue-{action}",
                page_path=f"{action}.md",
                severity="medium",
                content=action,
                choices=["a"],
            )

        pushed = queue.on_user_active()
        self.assertEqual(len(pushed), 3)
        by_issue = {entry.issue_id: entry for entry in pushed}

        self.assertTrue(queue.resolve(by_issue["issue-resolve"].reminder_id, "已更新"))
        self.assertTrue(queue.ignore(by_issue["issue-ignore"].reminder_id))
        self.assertTrue(queue.defer(by_issue["issue-defer"].reminder_id, hours=2))

        with queue._conn() as conn:
            rows = conn.execute(
                """
                SELECT issue_id, status, resolved_choice, delivery_event_id
                FROM dialog_reminders
                ORDER BY issue_id
                """
            ).fetchall()
        self.assertEqual(
            [(row["issue_id"], row["status"]) for row in rows],
            [
                ("issue-defer", "deferred"),
                ("issue-ignore", "ignored"),
                ("issue-resolve", "resolved"),
            ],
        )
        self.assertTrue(all(row["delivery_event_id"] for row in rows))
        resolved = next(row for row in rows if row["issue_id"] == "issue-resolve")
        self.assertEqual(resolved["resolved_choice"], "已更新")

        with sqlite3.connect(Path(self.temp_dir) / "delivery_events.db") as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            delivery_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(delivery_events)")
            }
        self.assertNotIn("cognitive_outcomes", tables)
        self.assertNotIn("feedback", delivery_columns)

    def test_cooldown(self):
        """冷却期内不应被推送"""
        rid = self.queue.enqueue(
            issue_id="i1",
            page_path="a.md",
            severity="medium",
            content="c1",
            choices=["a"],
        )
        self.queue.set_cooldown(rid, hours=24)
        self.assertTrue(self.queue.is_in_cooldown(rid))
        # 冷却期内 on_knowledge_triggered 不应返回
        triggered = self.queue.on_knowledge_triggered("a.md")
        self.assertEqual(len(triggered), 0)

    def test_aggregate_for_page_single(self):
        """单条提醒聚合应返回自身"""
        self.queue.enqueue(
            issue_id="i1",
            page_path="redis.md",
            severity="medium",
            content="孤立",
            choices=["a"],
        )
        agg = self.queue.aggregate_for_page("redis.md")
        self.assertIsNotNone(agg)
        self.assertEqual(agg.issue_id, "i1")

    def test_aggregate_for_page_multiple(self):
        """多条提醒聚合为一条"""
        self.queue.enqueue(
            issue_id="i1",
            page_path="redis.md",
            severity="medium",
            content="孤立页面",
            choices=["a"],
        )
        self.queue.enqueue(
            issue_id="i2",
            page_path="redis.md",
            severity="low",
            content="内容过短",
            choices=["b"],
        )
        agg = self.queue.aggregate_for_page("redis.md")
        self.assertIsNotNone(agg)
        self.assertIn("2 个优化建议", agg.content)
        self.assertEqual(agg.severity, "medium")  # 取最高严重度

    def test_cleanup_resolved(self):
        """清理已解决旧记录"""
        rid = self.queue.enqueue(
            issue_id="i1",
            page_path="a.md",
            severity="medium",
            content="c1",
            choices=["a"],
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

    def test_expire_stale_pending(self):
        rid_old = self.queue.enqueue(
            issue_id="old",
            page_path="a.md",
            severity="medium",
            content="old",
            choices=["a"],
        )
        self.queue.enqueue(
            issue_id="new",
            page_path="b.md",
            severity="medium",
            content="new",
            choices=["a"],
        )
        old_created = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        with self.queue._conn() as conn:
            conn.execute(
                "UPDATE dialog_reminders SET created_at = ? WHERE reminder_id = ?",
                (old_created, rid_old),
            )

        expired = self.queue.expire_stale_pending(days=30)

        self.assertEqual(expired, 1)
        with self.queue._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM dialog_reminders GROUP BY status"
            ).fetchall()
        self.assertEqual({row[0]: row[1] for row in rows}, {"expired": 1, "pending": 1})

    def test_expire_stale_pending_limit_zero_is_noop(self):
        rid_old = self.queue.enqueue(
            issue_id="old-zero-limit",
            page_path="a.md",
            severity="medium",
            content="old",
            choices=["a"],
        )
        old_created = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
        with self.queue._conn() as conn:
            conn.execute(
                "UPDATE dialog_reminders SET created_at = ? WHERE reminder_id = ?",
                (old_created, rid_old),
            )

        expired = self.queue.expire_stale_pending(days=30, limit=0)

        self.assertEqual(expired, 0)
        with self.queue._conn() as conn:
            status = conn.execute(
                "SELECT status FROM dialog_reminders WHERE reminder_id = ?",
                (rid_old,),
            ).fetchone()[0]
        self.assertEqual(status, "pending")


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

    def test_inject_banner_enforce_submits_proposal_without_touching_page(self):
        """enforce 模式下横幅写入应提交 trusted proposal。"""
        wiki = Path(self.temp_dir)
        page = wiki / "test.md"
        original = "# Hello\n"
        page.write_text(original, encoding="utf-8")
        db = wiki / ".mnemos" / "trusted.db"
        fake_config = SimpleNamespace(
            wiki_dir=wiki,
            database_dir=wiki / ".mnemos",
            get=lambda key, default=None: {
                "trusted_push.mode": "enforce",
                "trusted_push.db_path": str(db),
            }.get(key, default),
        )

        with patch("core.kia.dialog_reminder.get_config", return_value=fake_config), patch(
            "core.trust.config.get_config", return_value=fake_config
        ):
            ok = PageBannerInjector().inject_banner(page, ["> ⚠️ 提醒"], issue_id="i1")

        self.assertTrue(ok)
        self.assertEqual(page.read_text(encoding="utf-8"), original)
        proposals = ProposalQueue(db, wiki_base=wiki).list()
        self.assertEqual(proposals[0].candidate.source, "dialog_reminder")

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

    def _create_banner_page(self, page_path, queue, checked=None):
        """辅助方法：创建带横幅的页面"""
        entry = ReminderEntry(
            reminder_id="rem-test",
            issue_id="issue-test",
            page_path=str(page_path),
            severity="high",
            content="此知识可能已过期，请确认。",
            choices=["已更新", "仍有效", "忽略"],
        )
        actual_reminder_id = queue.enqueue(
            entry.reminder_id, entry.page_path, entry.severity, entry.content, entry.choices
        )
        entry.reminder_id = actual_reminder_id
        page_path.write_text(
            "---\ntitle: Test\nstatus: 稳定\n---\n\n# Test\n\n正文\n", encoding="utf-8"
        )
        banner_lines = ReminderRenderer.render_banner(entry)
        injector = PageBannerInjector()
        injector.inject_banner(
            page_path,
            banner_lines,
            issue_id=entry.issue_id,
            reminder_id=entry.reminder_id,
        )
        if checked:
            text = page_path.read_text(encoding="utf-8")
            for choice in checked:
                text = text.replace(f"- [ ] {choice}", f"- [x] {choice}")
            page_path.write_text(text, encoding="utf-8")
        return injector, entry

    def test_process_banners_zero_checked(self):
        """0 个勾选时应跳过"""
        db_path = Path(self.temp_dir) / "reminders.db"
        queue = DialogReminderQueue(db_path=str(db_path))
        page = Path(self.temp_dir) / "test.md"
        injector, _ = self._create_banner_page(page, queue)

        stats = injector.process_banners(wiki_base=Path(self.temp_dir), queue=queue)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["resolved"], 0)
        self.assertEqual(stats["conflict"], 0)
        self.assertTrue(injector.has_banner(page))

    def test_process_banners_one_checked_ignore(self):
        """1 个勾选'忽略'时应 ignore 并移除横幅"""
        db_path = Path(self.temp_dir) / "reminders.db"
        queue = DialogReminderQueue(db_path=str(db_path))
        page = Path(self.temp_dir) / "test.md"
        injector, entry = self._create_banner_page(page, queue, checked=["忽略"])

        stats = injector.process_banners(wiki_base=Path(self.temp_dir), queue=queue)
        self.assertEqual(stats["resolved"], 1)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(stats["conflict"], 0)
        self.assertFalse(injector.has_banner(page))
        with queue._conn() as conn:
            row = conn.execute(
                "SELECT status FROM dialog_reminders WHERE reminder_id = ?",
                (entry.reminder_id,),
            ).fetchone()
        self.assertEqual(row["status"], "ignored")

    def test_process_banners_one_checked_validate(self):
        """1 个勾选'仍有效'时应 resolve 并更新 last_validated"""
        db_path = Path(self.temp_dir) / "reminders.db"
        queue = DialogReminderQueue(db_path=str(db_path))
        page = Path(self.temp_dir) / "test.md"
        injector, entry = self._create_banner_page(page, queue, checked=["仍有效"])

        stats = injector.process_banners(wiki_base=Path(self.temp_dir), queue=queue)
        self.assertEqual(stats["resolved"], 1)
        self.assertFalse(injector.has_banner(page))
        with queue._conn() as conn:
            row = conn.execute(
                "SELECT status, resolved_choice FROM dialog_reminders WHERE reminder_id = ?",
                (entry.reminder_id,),
            ).fetchone()
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["resolved_choice"], "仍有效")
        text = page.read_text(encoding="utf-8")
        self.assertIn("last_validated:", text)

    def test_process_banners_one_checked_update(self):
        """1 个勾选'已更新'时应 resolve 并更新 last_updated"""
        db_path = Path(self.temp_dir) / "reminders.db"
        queue = DialogReminderQueue(db_path=str(db_path))
        page = Path(self.temp_dir) / "test.md"
        injector, entry = self._create_banner_page(page, queue, checked=["已更新"])

        stats = injector.process_banners(wiki_base=Path(self.temp_dir), queue=queue)
        self.assertEqual(stats["resolved"], 1)
        self.assertFalse(injector.has_banner(page))
        with queue._conn() as conn:
            row = conn.execute(
                "SELECT status, resolved_choice FROM dialog_reminders WHERE reminder_id = ?",
                (entry.reminder_id,),
            ).fetchone()
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["resolved_choice"], "已更新")
        text = page.read_text(encoding="utf-8")
        self.assertIn("last_updated:", text)
        # 稳定状态应降级为待验证
        self.assertIn("status: 待验证", text)

    def test_process_banners_multiple_checked(self):
        """2 个及以上勾选时应提示冲突并保留横幅"""
        db_path = Path(self.temp_dir) / "reminders.db"
        queue = DialogReminderQueue(db_path=str(db_path))
        page = Path(self.temp_dir) / "test.md"
        injector, _ = self._create_banner_page(page, queue, checked=["已更新", "仍有效"])

        stats = injector.process_banners(wiki_base=Path(self.temp_dir), queue=queue)
        self.assertEqual(stats["conflict"], 1)
        self.assertEqual(stats["resolved"], 0)
        self.assertEqual(stats["skipped"], 0)
        self.assertTrue(injector.has_banner(page))
        text = page.read_text(encoding="utf-8")
        self.assertIn("请只选择一项", text)


class TestReminderRenderer(unittest.TestCase):
    """ReminderRenderer 测试"""

    def test_render_dialog(self):
        """渲染对话内容应包含交互选项"""
        from core.kia.dialog_reminder import ReminderRenderer, ReminderEntry

        renderer = ReminderRenderer()
        entry = ReminderEntry(
            reminder_id="rem-1",
            issue_id="i1",
            page_path="redis.md",
            severity="high",
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
        """渲染横幅内容（任务列表格式）"""
        from core.kia.dialog_reminder import ReminderRenderer, ReminderEntry

        renderer = ReminderRenderer()
        entry = ReminderEntry(
            reminder_id="rem-1",
            issue_id="i1",
            page_path="redis.md",
            severity="medium",
            content="孤立页面",
            choices=["发现相关页面", "忽略"],
        )
        lines = renderer.render_banner(entry)
        self.assertTrue(any("📋 **知识提醒**" in line for line in lines))
        self.assertTrue(any("孤立页面" in line for line in lines))
        self.assertTrue(any("- [ ] 发现相关页面" in line for line in lines))
        self.assertTrue(any("- [ ] 忽略" in line for line in lines))
        self.assertTrue(any("请选择一项" in line for line in lines))

    def test_render_aggregated_dialog(self):
        """渲染聚合提醒"""
        from core.kia.dialog_reminder import ReminderRenderer, ReminderEntry

        renderer = ReminderRenderer()
        entries = [
            ReminderEntry(
                reminder_id="r1",
                issue_id="i1",
                page_path="docker.md",
                severity="medium",
                content="孤立页面",
            ),
            ReminderEntry(
                reminder_id="r2",
                issue_id="i2",
                page_path="docker.md",
                severity="low",
                content="内容过短",
            ),
        ]
        text = renderer.render_aggregated_dialog("Docker Compose", entries)
        self.assertIn("Docker Compose", text)
        self.assertIn("存在 2 个优化建议", text)
        self.assertIn("孤立页面", text)
        self.assertIn("内容过短", text)
        self.assertIn("[选择：查看详情]", text)


if __name__ == "__main__":
    unittest.main()
