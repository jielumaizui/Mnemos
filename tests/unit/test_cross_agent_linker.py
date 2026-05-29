"""
cross_agent_linker 单元测试

覆盖项：
- CrossAgentLinker 基本流程
- _extract_agent_from_path 路径解析
- _link_exists 链接存在性检测
- _make_relative_link 相对链接生成
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest
from core.kia.cross_agent_linker import (
    CrossAgentDivergenceDetector,
    CrossAgentLinker,
    DivergencePushManager,
    LinkAction,
)


class FakeVectorIndex:
    def __init__(self, result_path):
        self.result_path = result_path
        self.calls = []

    def hybrid_search(self, **kwargs):
        self.calls.append(kwargs)
        return [{"path": str(self.result_path), "score": 0.91}]


class TestCrossAgentLinker(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.wiki_root = Path(self.tmpdir.name) / "wiki"
        self.wiki_root.mkdir()

        # 创建两个 agent 的 workspace
        (self.wiki_root / "claude" / "entities").mkdir(parents=True)
        (self.wiki_root / "hermes" / "entities").mkdir(parents=True)

        # 创建测试页面
        self.claude_page = self.wiki_root / "claude" / "entities" / "Redis.md"
        self.claude_page.write_text("# Redis\n\nRedis cluster config.\n", encoding="utf-8")

        self.hermes_page = self.wiki_root / "hermes" / "entities" / "Redis.md"
        self.hermes_page.write_text("# Redis\n\nRedis 集群配置讨论。\n", encoding="utf-8")

        self.linker = CrossAgentLinker(wiki_root=self.wiki_root)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_extract_agent_from_path(self):
        """测试从路径提取 agent"""
        self.assertEqual(
            self.linker._extract_agent_from_path(self.claude_page),
            "claude"
        )
        self.assertEqual(
            self.linker._extract_agent_from_path(self.hermes_page),
            "hermes"
        )

    def test_link_exists_false(self):
        """链接不存在时返回 False"""
        self.assertFalse(self.linker._link_exists(self.claude_page, self.hermes_page))

    def test_link_exists_true(self):
        """链接已存在时返回 True"""
        content = self.claude_page.read_text(encoding="utf-8")
        content += f"\n[[hermes/entities/Redis]]\n"
        self.claude_page.write_text(content, encoding="utf-8")
        self.assertTrue(self.linker._link_exists(self.claude_page, self.hermes_page))

    def test_make_relative_link(self):
        """测试相对链接生成"""
        link = self.linker._make_relative_link(self.claude_page, self.hermes_page)
        self.assertIn("hermes", link)
        self.assertIn("Redis", link)

    def test_append_link(self):
        """测试添加链接到页面"""
        action = LinkAction(
            from_page=self.claude_page,
            to_page=self.hermes_page,
            reason="test",
            similarity=0.8,
        )
        self.linker._append_link(action)
        content = self.claude_page.read_text(encoding="utf-8")
        self.assertIn("## 相关链接", content)
        self.assertIn("hermes", content)

    def test_link_after_distill_same_content(self):
        """两个 Agent 写了相似内容，建立关联"""
        actions = self.linker.link_after_distill(self.claude_page)
        # 至少应该找到 hermes 的 Redis 页面
        self.assertGreaterEqual(len(actions), 1)

    def test_vector_search_filters_other_workspaces(self):
        """向量检索按 workspace 过滤，并按 0.75 阈值建立双向链接"""
        vector_index = FakeVectorIndex(self.hermes_page)
        linker = CrossAgentLinker(wiki_root=self.wiki_root, vector_index=vector_index)

        actions = linker.link_after_distill(self.claude_page)

        self.assertEqual(len(actions), 2)
        self.assertEqual(vector_index.calls[0]["filters"]["workspace"], ["hermes", "kimi", "codex", "gpt"])
        self.assertGreaterEqual(actions[0].similarity, 0.75)

    def test_extract_agent_from_frontmatter(self):
        """路径无 workspace 时，可从 source_agent frontmatter 识别来源"""
        page = self.wiki_root / "mixed" / "Redis-Kimi.md"
        page.parent.mkdir()
        page.write_text("---\nsource_agent: kimi\n---\n# Redis\n", encoding="utf-8")

        self.assertEqual(self.linker._extract_agent_from_path(page), "kimi")

    def test_handle_event_distill_complete(self):
        """distill_complete 事件触发跨 Agent 关联"""
        actions = self.linker.handle_event("distill_complete", {"page_path": str(self.claude_page)})

        self.assertGreaterEqual(len(actions), 1)

    def test_divergence_detector_reports_conflicting_decisions(self):
        """不同 Agent 对同一主题给出方向对立结论时生成分歧报告"""
        self.claude_page.write_text(
            "---\n"
            "source_agent: claude\n"
            "topic: Redis 集群方案\n"
            "decision: 建议用 Sentinel 模式，主从自动故障转移\n"
            "distilled_at: 2026-05-20\n"
            "confidence: 0.8\n"
            "---\n"
            "# Redis 集群方案\n",
            encoding="utf-8",
        )
        self.hermes_page.write_text(
            "---\n"
            "source_agent: hermes\n"
            "topic: Redis 集群方案\n"
            "decision: 建议用 Cluster 模式，支持数据分片\n"
            "distilled_at: 2026-05-23\n"
            "confidence: 0.8\n"
            "---\n"
            "# Redis 集群方案\n",
            encoding="utf-8",
        )

        report = CrossAgentDivergenceDetector(wiki_root=self.wiki_root).detect("Redis 集群方案")

        self.assertIsNotNone(report)
        self.assertEqual(report.severity, "high")
        self.assertIn("不同 Agent", report.to_push_message())
        self.assertIn("Sentinel", report.to_push_message())
        self.assertIn("Cluster", report.to_push_message())

    def test_divergence_push_cooldown(self):
        """同一主题 24 小时内不重复推送"""
        report = CrossAgentDivergenceDetector(wiki_root=self.wiki_root).detect("not-exists")
        manager = DivergencePushManager()
        fake_report = report or type("Report", (), {"topic": "Redis 集群方案"})()

        self.assertTrue(manager.should_push(fake_report))
        manager.mark_pushed(fake_report.topic)
        self.assertFalse(manager.should_push(fake_report))


if __name__ == "__main__":
    unittest.main()
