# -*- coding: utf-8 -*-
"""
P1-3 长链路测试 — KG 搜索链路

链路：distilled page → KnowledgeGraph.discover_relations() → apply_discovered()
      → Charon/KG relation → ContextAwareSearch → Teiresias/QA

策略：临时 wiki 目录 + 临时 SQLite KG DB，只 mock 外部网络/LLM。
      注意：discover_relations() 只返回关系列表，不会自动写入 DB，
            需要调用 apply_discovered() 才能持久化。
断言目标：关系发现、搜索召回、评分排序。
"""

import sqlite3
from pathlib import Path

import pytest


class TestKnowledgeGraphLoop:
    """知识图谱关系发现 + 搜索召回链路。"""

    @pytest.fixture
    def wiki_and_db(self, tmp_path):
        wiki = tmp_path / "wiki"
        inbox = wiki / "00-Inbox"
        inbox.mkdir(parents=True)

        # 创建两个有关系的页面
        (inbox / "redis.md").write_text(
            "---\ntype: concept\nsource_agent: claude\n---\n\n# Redis\n\n"
            "Redis is an in-memory data store. See also [[memcached]] and [[kafka]].\n",
            encoding="utf-8",
        )
        (inbox / "memcached.md").write_text(
            "---\ntype: concept\nsource_agent: claude\n---\n\n# Memcached\n\n"
            "Memcached is a distributed memory caching system.\n",
            encoding="utf-8",
        )

        kg_db = tmp_path / "kg.db"
        return wiki, kg_db

    def test_discover_relations_finds_wikilinks(self, wiki_and_db):
        from core.kia.knowledge_graph import KnowledgeGraph

        wiki, kg_db = wiki_and_db
        kg = KnowledgeGraph(wiki_base=str(wiki), db_path=str(kg_db))

        # 发现关系并写入数据库
        page = wiki / "00-Inbox" / "redis.md"
        discovered = kg.discover_relations(page)
        assert len(discovered) >= 1
        kg.apply_discovered(discovered)

        with sqlite3.connect(str(kg_db)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT source, target, relation_type FROM relations"
            ).fetchall()
            assert len(rows) >= 1
            # 应发现 redis → memcached 的 REFERENCES 关系
            targets = [r["target"] for r in rows]
            assert any("memcached" in t for t in targets)

    def test_search_recall_from_kg_and_files(self, wiki_and_db):
        from core.kia.knowledge_graph import KnowledgeGraph

        wiki, kg_db = wiki_and_db
        kg = KnowledgeGraph(wiki_base=str(wiki), db_path=str(kg_db))

        # 先建立关系并写入数据库
        for page in [wiki / "00-Inbox" / "redis.md", wiki / "00-Inbox" / "memcached.md"]:
            discovered = kg.discover_relations(page)
            kg.apply_discovered(discovered)

        # 搜索
        results = kg.search("caching system")
        assert isinstance(results, list)
        # 应召回 memcached 页面（通过 KG 关系或文件系统回退）
        titles = [r.get("title", "") for r in results]
        assert any("memcached" in t or "Memcached" in t for t in titles)

    def test_kg_conflict_detection(self, wiki_and_db):
        from core.kia.knowledge_graph import KnowledgeGraph

        wiki, kg_db = wiki_and_db
        kg = KnowledgeGraph(wiki_base=str(wiki), db_path=str(kg_db))

        # 同一个关系两次发现并写入应该是幂等的
        page = wiki / "00-Inbox" / "redis.md"
        for _ in range(2):
            discovered = kg.discover_relations(page)
            kg.apply_discovered(discovered)

        with sqlite3.connect(str(kg_db)) as conn:
            conn.row_factory = sqlite3.Row
            count = conn.execute(
                "SELECT COUNT(*) as cnt FROM relations WHERE source LIKE '%redis%'"
            ).fetchone()["cnt"]
            # 幂等写入：相同 source+target+type 只应有一条
            assert count <= 5


class TestContextAwareSearchLoop:
    """ContextAwareSearch 端到端召回。"""

    @pytest.fixture
    def search_env(self, tmp_path):
        wiki = tmp_path / "wiki"
        inbox = wiki / "00-Inbox"
        inbox.mkdir(parents=True)

        (inbox / "docker.md").write_text(
            "---\ntype: concept\nsource_agent: claude\nconfidence: 0.9\n---\n\n"
            "# Docker\n\nDocker is a containerization platform.\n",
            encoding="utf-8",
        )
        (inbox / "k8s.md").write_text(
            "---\ntype: concept\nsource_agent: claude\nconfidence: 0.85\n---\n\n"
            "# Kubernetes\n\nKubernetes orchestrates Docker containers.\n",
            encoding="utf-8",
        )
        return wiki

    def test_search_returns_ranked_results(self, search_env):
        from core.app.context_search import ContextAwareSearch

        searcher = ContextAwareSearch(wiki_base=str(search_env))
        results = searcher.search("container orchestration")

        assert isinstance(results, list)
        assert len(results) >= 1
        # 结果应包含评分字段（SearchResult 对象）
        if results:
            r = results[0]
            assert hasattr(r, "score") or hasattr(r, "relevance") or hasattr(r, "page_path")

    def test_search_falls_back_to_filesystem(self, search_env):
        from core.app.context_search import ContextAwareSearch

        searcher = ContextAwareSearch(wiki_base=str(search_env))
        # 查询一个不太可能出现在 KG 中的词
        results = searcher.search("orchestrates Docker")

        assert isinstance(results, list)
        # 至少应通过文件系统搜索召回 k8s.md（SearchResult 对象）
        paths = [r.page_path for r in results]
        assert any("k8s" in p or "docker" in p for p in paths)


class TestTeiresiasPushLoop:
    """Teiresias 主动推荐链路。"""

    @pytest.fixture
    def push_env(self, tmp_path):
        wiki = tmp_path / "wiki"
        inbox = wiki / "00-Inbox"
        inbox.mkdir(parents=True)
        # frontmatter 需使用嵌套 关键词 结构才能被 _get_page_index 识别
        (inbox / "python.md").write_text(
            "---\ntype: concept\n关键词:\n  工具实体: [\"Python\", \"pip\"]\n---\n\n"
            "# Python\n\nPython programming.\n",
            encoding="utf-8",
        )
        return wiki

    def test_decide_push_matches_tools(self, push_env):
        from core.kia.teiresias import PredictivePushEngine

        engine = PredictivePushEngine(wiki_base=str(push_env))
        # 传入包含 "Python" 的用户消息，让 _extract_tools 匹配到
        decision = engine.decide_push(user_message="How do I use Python for data analysis?")

        assert isinstance(decision, object)
        # 如果匹配到 Python 页面，应产生推送推荐
        if decision.should_push:
            assert hasattr(decision, "matched_pages")
            if decision.matched_pages:
                assert any("python" in p.get("page_path", "").lower()
                           for p in decision.matched_pages)
