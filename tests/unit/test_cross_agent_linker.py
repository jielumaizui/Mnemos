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
from core.kia.cross_agent_linker import CrossAgentLinker, LinkAction


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


if __name__ == "__main__":
    unittest.main()
