"""
Issue Pipeline 单元测试

覆盖项：
- IssueRegistry: 注册、去重、回归、查询、状态更新、忽略规则
- AutoFixExecutor: 自动修复判断、备份、跳过非自动修复类型
"""

import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.kia.issue_pipeline import (
    Issue, IssueRegistry, AutoFixExecutor, IgnoreRule,
    AUTO_FIX_RULES,
)


class TestIssueRegistry(unittest.TestCase):
    """IssueRegistry 核心功能测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "issues.db"
        self.registry = IssueRegistry(db_path=str(self.db_path))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_issue(self, issue_type="conflict", severity="high", page="a.md") -> Issue:
        return Issue(
            source_module="immune",
            issue_type=issue_type,
            severity=severity,
            page_path=page,
            description="test issue",
            suggestion="fix it",
        )

    def test_register_new_issue(self):
        """注册新问题应生成 ID 并返回 is_new=True"""
        issue = self._make_issue()
        issue_id, is_new = self.registry.register(issue)
        self.assertTrue(is_new)
        self.assertTrue(issue_id.startswith("issue-"))
        self.assertEqual(issue.status, "detected")

    def test_register_duplicate_updates_detected_at(self):
        """重复注册同一问题应返回 is_new=False"""
        issue = self._make_issue()
        self.registry.register(issue)
        issue_id2, is_new2 = self.registry.register(issue)
        self.assertFalse(is_new2)
        self.assertEqual(issue.issue_id, issue_id2)

    def test_register_regression_reopens_resolved(self):
        """已解决问题再次检测到应重新打开"""
        issue = self._make_issue()
        self.registry.register(issue)
        self.registry.update_status(issue.issue_id, "resolved", resolved_by="user")

        issue_id2, is_new2 = self.registry.register(issue)
        self.assertTrue(is_new2)
        reloaded = self.registry.get_issue(issue_id2)
        self.assertEqual(reloaded.status, "detected")

    def test_list_issues_by_status(self):
        """按状态查询"""
        i1 = self._make_issue(page="a.md")
        i2 = self._make_issue(page="b.md")
        self.registry.register(i1)
        self.registry.register(i2)
        self.registry.update_status(i2.issue_id, "resolved")

        pending = self.registry.list_issues(status="detected")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].page_path, "a.md")

    def test_list_issues_by_severity(self):
        """按严重度查询并排序"""
        i1 = self._make_issue(severity="medium", page="a.md")
        i2 = self._make_issue(severity="critical", page="b.md")
        self.registry.register(i1)
        self.registry.register(i2)

        results = self.registry.list_issues()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].severity, "critical")
        self.assertEqual(results[1].severity, "medium")

    def test_list_issues_by_page(self):
        """按页面路径查询（含 related_pages）"""
        i1 = self._make_issue(page="a.md")
        i1.related_pages = ["b.md"]
        self.registry.register(i1)

        results = self.registry.list_issues(page_path="b.md")
        self.assertEqual(len(results), 1)

    def test_update_status(self):
        """更新状态后应正确反映"""
        issue = self._make_issue()
        self.registry.register(issue)
        ok = self.registry.update_status(
            issue.issue_id, "resolved", resolved_by="user",
            resolution_action="confirmed", resolution_notes="looks ok",
        )
        self.assertTrue(ok)
        reloaded = self.registry.get_issue(issue.issue_id)
        self.assertEqual(reloaded.status, "resolved")
        self.assertEqual(reloaded.resolved_by, "user")
        self.assertEqual(reloaded.resolution_action, "confirmed")

    def test_count_by_status(self):
        """统计按状态分布"""
        i1 = self._make_issue(page="a.md")
        i2 = self._make_issue(page="b.md")
        self.registry.register(i1)
        self.registry.register(i2)
        self.registry.update_status(i2.issue_id, "resolved")

        counts = self.registry.count_by_status()
        self.assertEqual(counts.get("detected"), 1)
        self.assertEqual(counts.get("resolved"), 1)

    def test_add_ignore_rule_exact(self):
        """精确忽略规则应命中"""
        rule = IgnoreRule(issue_type="conflict", page_pattern="a.md", reason="known")
        self.registry.add_ignore_rule(rule)

        issue = self._make_issue(issue_type="conflict", page="a.md")
        self.assertTrue(self.registry.is_ignored(issue))

    def test_add_ignore_rule_wildcard(self):
        """通配忽略规则应命中"""
        rule = IgnoreRule(issue_type="conflict", page_pattern="*", reason="all known")
        self.registry.add_ignore_rule(rule)

        issue = self._make_issue(issue_type="conflict", page="x.md")
        self.assertTrue(self.registry.is_ignored(issue))

    def test_add_ignore_rule_expires(self):
        """过期忽略规则不应命中"""
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        rule = IgnoreRule(issue_type="conflict", page_pattern="a.md",
                          reason="temp", expires_at=past)
        self.registry.add_ignore_rule(rule)

        issue = self._make_issue(issue_type="conflict", page="a.md")
        self.assertFalse(self.registry.is_ignored(issue))

    def test_cleanup_expired_ignores(self):
        """清理过期忽略规则"""
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        rule = IgnoreRule(issue_type="conflict", expires_at=past)
        self.registry.add_ignore_rule(rule)
        deleted = self.registry.cleanup_expired_ignores()
        self.assertEqual(deleted, 1)


class TestAutoFixExecutor(unittest.TestCase):
    """AutoFixExecutor 测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "issues.db"
        self.wiki_dir = Path(self.temp_dir) / "wiki"
        self.wiki_dir.mkdir()
        self.registry = IssueRegistry(db_path=str(self.db_path))
        self.executor = AutoFixExecutor(
            registry=self.registry,
            wiki_base=self.wiki_dir,
            backup_dir=Path(self.temp_dir) / "backups",
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_can_auto_fix_low_risk(self):
        """白名单内的问题应判定为可自动修复"""
        issue = Issue(
            source_module="immune", issue_type="orphan",
            page_path="a.md", severity="medium",
        )
        self.assertTrue(self.executor.can_auto_fix(issue))

    def test_can_auto_fix_high_risk(self):
        """白名单外的问题应判定为不可自动修复"""
        issue = Issue(
            source_module="immune", issue_type="conflict",
            page_path="a.md", severity="critical",
        )
        self.assertFalse(self.executor.can_auto_fix(issue))

    def test_execute_skips_non_auto_fixable(self):
        """不可自动修复的问题应被跳过"""
        issue = Issue(
            source_module="immune", issue_type="conflict",
            page_path="a.md", severity="critical",
        )
        result = self.executor.execute(issue)
        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, "not_auto_fixable")

    def test_execute_skips_ignored_issue(self):
        """被忽略的问题应被跳过"""
        issue = Issue(
            source_module="immune", issue_type="orphan",
            page_path="a.md", severity="medium",
        )
        self.registry.register(issue)
        self.registry.add_ignore_rule(
            IgnoreRule(issue_type="orphan", page_pattern="a.md", reason="test")
        )
        result = self.executor.execute(issue)
        self.assertTrue(result.skipped)
        self.assertEqual(result.reason, "issue_ignored")

    def test_backup_created_before_fix(self):
        """执行修复前应创建备份"""
        page = self.wiki_dir / "a.md"
        page.write_text("# Test\ncontent\n", encoding="utf-8")

        issue = Issue(
            source_module="entropy", issue_type="cross_reference",
            page_path=str(page), related_pages=[str(self.wiki_dir / "b.md")],
            severity="low",
        )
        result = self.executor.execute(issue)
        self.assertTrue(result.success or not result.skipped)
        self.assertNotEqual(result.backup_id, "")

    def test_discover_relations_fix(self):
        """discover_relations 修复动作应执行"""
        page = self.wiki_dir / "a.md"
        page.write_text("# Test\ncontent\n", encoding="utf-8")

        issue = Issue(
            source_module="immune", issue_type="orphan",
            page_path=str(page), severity="medium",
        )
        result = self.executor.execute(issue)
        # 由于 wiki 目录为空，discover_relations 可能返回 0，但不应抛异常
        self.assertTrue(result.success)

    def test_add_wiki_link_fix(self):
        """add_wiki_link 修复动作应在页面末尾添加链接"""
        page_a = self.wiki_dir / "a.md"
        page_b = self.wiki_dir / "b.md"
        page_a.write_text("# A\ncontent\n", encoding="utf-8")
        page_b.write_text("# B\ncontent\n", encoding="utf-8")

        issue = Issue(
            source_module="entropy", issue_type="cross_reference",
            page_path=str(page_a), related_pages=[str(page_b)],
            severity="low",
        )
        result = self.executor.execute(issue)
        self.assertTrue(result.success)
        self.assertIn("[[b]]", page_a.read_text(encoding="utf-8"))

    def test_add_wiki_link_idempotent(self):
        """重复执行 add_wiki_link 不应重复添加"""
        page_a = self.wiki_dir / "a.md"
        page_b = self.wiki_dir / "b.md"
        page_a.write_text("# A\ncontent\n", encoding="utf-8")
        page_b.write_text("# B\ncontent\n", encoding="utf-8")

        issue = Issue(
            source_module="entropy", issue_type="cross_reference",
            page_path=str(page_a), related_pages=[str(page_b)],
            severity="low",
        )
        r1 = self.executor.execute(issue)
        self.assertTrue(r1.success)
        r2 = self.executor.execute(issue)
        self.assertTrue(r2.success)
        self.assertEqual(r2.action, "add_wiki_link:already_exists")


class TestDisputePageGenerator(unittest.TestCase):
    """争议页面生成器测试"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.wiki_dir = Path(self.temp_dir) / "wiki"
        self.wiki_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_generate_dispute_page(self):
        """生成争议页面"""
        from core.kia.issue_pipeline import DisputePageGenerator, Issue

        gen = DisputePageGenerator(wiki_base=self.wiki_dir)
        issue = Issue(
            source_module="immune", issue_type="conflict",
            page_path="03-Tech/redis.md", severity="critical",
            description="Redis 冲突", suggestion="添加版本边界",
            related_pages=["03-Tech/redis-cluster.md"],
            detected_at="2026-05-19",
        )
        page_path = gen.generate(issue)
        self.assertTrue(page_path.exists())
        content = page_path.read_text(encoding="utf-8")
        self.assertIn("争议：redis", content)
        self.assertIn("Redis 冲突", content)
        self.assertIn("- [ ] 已解决", content)
        self.assertIn("- [[redis-cluster]]", content)

    def test_parse_resolution_resolved(self):
        """解析已解决裁决"""
        from core.kia.issue_pipeline import DisputePageGenerator

        gen = DisputePageGenerator(wiki_base=self.wiki_dir)
        page = self.wiki_dir / "99-Reports" / "test.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("- [x] 已解决\n## 备注\n已处理", encoding="utf-8")

        result = gen.parse_resolution(page)
        self.assertIsNotNone(result)
        self.assertEqual(result["choice"], "resolved")
        self.assertIn("已处理", result["notes"])

    def test_parse_resolution_ignored(self):
        """解析忽略裁决"""
        from core.kia.issue_pipeline import DisputePageGenerator

        gen = DisputePageGenerator(wiki_base=self.wiki_dir)
        page = self.wiki_dir / "99-Reports" / "test.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("- [x] 忽略此问题\n", encoding="utf-8")

        result = gen.parse_resolution(page)
        self.assertIsNotNone(result)
        self.assertEqual(result["choice"], "ignored")

    def test_parse_resolution_none(self):
        """未勾选时返回 None"""
        from core.kia.issue_pipeline import DisputePageGenerator

        gen = DisputePageGenerator(wiki_base=self.wiki_dir)
        page = self.wiki_dir / "99-Reports" / "test.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("- [ ] 已解决\n", encoding="utf-8")

        result = gen.parse_resolution(page)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
