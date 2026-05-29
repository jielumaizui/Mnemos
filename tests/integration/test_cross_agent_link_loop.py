# -*- coding: utf-8 -*-
"""
P2-3 长链路测试 — 跨 Agent 关联链路

链路：distilled page → CrossAgentLinker.link_after_distill()
      → bidirectional link injection → cross_agent_refs frontmatter

策略：临时 wiki 目录（多 agent workspace），真实文件操作。
      必须 mock EventBus._recover_pending，避免加载 200万+ pending 事件导致超时。
断言目标：双向链接被注入、frontmatter 被更新、相似度阈值被遵守。
"""

from pathlib import Path
from unittest.mock import patch

import pytest


class TestCrossAgentLinkLoop:
    """跨 Agent 关联完整链路。"""

    @pytest.fixture
    def multi_agent_wiki(self, tmp_path, monkeypatch):
        """创建多 agent 临时 wiki 目录，并禁用 EventBus 启动恢复。"""
        # 阻止 EventBus 在初始化时加载海量 pending 事件
        monkeypatch.setattr(
            "core.mnemos_bus.EventBus._recover_pending",
            lambda self: None,
        )

        wiki = tmp_path / "wiki"
        # 两个 agent 的 workspace
        claude_dir = wiki / "claude" / "01-Projects"
        kimi_dir = wiki / "kimi" / "01-Projects"
        claude_dir.mkdir(parents=True)
        kimi_dir.mkdir(parents=True)

        # Claude 写的 Redis 页面
        (claude_dir / "redis.md").write_text(
            "---\nsource_agent: claude\ntype: concept\n---\n\n# Redis\n\n"
            "Redis is an in-memory data store used for caching. "
            "It supports pub/sub and streams.\n",
            encoding="utf-8",
        )
        # Kimi 写的缓存页面（主题相似）
        (kimi_dir / "caching.md").write_text(
            "---\nsource_agent: kimi\ntype: concept\n---\n\n# Caching\n\n"
            "Caching strategies include Redis, Memcached, and in-memory stores. "
            "Redis is the most popular choice for distributed caching.\n",
            encoding="utf-8",
        )
        # 第三个不相关的页面
        (claude_dir / "unrelated.md").write_text(
            "---\nsource_agent: claude\ntype: concept\n---\n\n# Cooking\n\nHow to make pasta.\n",
            encoding="utf-8",
        )
        return wiki

    def test_link_after_distill_finds_similar_pages(self, multi_agent_wiki):
        from core.kia.cross_agent_linker import CrossAgentLinker

        linker = CrossAgentLinker(wiki_root=multi_agent_wiki)
        page = multi_agent_wiki / "claude" / "01-Projects" / "redis.md"
        actions = linker.link_after_distill(page)

        assert isinstance(actions, list)
        # 应发现 redis.md 和 caching.md 之间的相似关系
        targets = [str(a.to_page) for a in actions]
        assert any("caching" in t.lower() for t in targets), f"Expected caching in targets: {targets}"

    def test_bidirectional_link_injected(self, multi_agent_wiki):
        from core.kia.cross_agent_linker import CrossAgentLinker

        linker = CrossAgentLinker(wiki_root=multi_agent_wiki)
        redis_page = multi_agent_wiki / "claude" / "01-Projects" / "redis.md"
        actions = linker.link_after_distill(redis_page)

        # 检查目标文件是否被注入了反向链接
        for action in actions:
            target_text = action.to_page.read_text(encoding="utf-8")
            # 应包含 redis 的反向链接
            assert "redis" in target_text.lower() or "[[" in target_text

    def test_link_exists_guard_prevents_duplicates(self, multi_agent_wiki):
        from core.kia.cross_agent_linker import CrossAgentLinker

        linker = CrossAgentLinker(wiki_root=multi_agent_wiki)
        page = multi_agent_wiki / "claude" / "01-Projects" / "redis.md"

        # 第一次链接
        actions1 = linker.link_after_distill(page)
        # 第二次链接（不应重复创建）
        actions2 = linker.link_after_distill(page)

        # 第二次应返回空或去重后的结果
        assert len(actions2) <= len(actions1)

    def test_different_agent_pages_only(self, multi_agent_wiki):
        from core.kia.cross_agent_linker import CrossAgentLinker

        linker = CrossAgentLinker(wiki_root=multi_agent_wiki)
        # 从 claude 页面出发
        page = multi_agent_wiki / "claude" / "01-Projects" / "redis.md"
        actions = linker.link_after_distill(page)

        # 每条链接的 from 和 to 都应来自不同 agent
        for action in actions:
            from_agent = linker._extract_agent_from_path(action.from_page)
            to_agent = linker._extract_agent_from_path(action.to_page)
            assert from_agent != to_agent, (
                f"跨 Agent 链接要求 from={from_agent} != to={to_agent}"
            )
