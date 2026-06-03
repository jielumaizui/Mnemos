# -*- coding: utf-8 -*-
"""
DualIndexRetriever 单元测试
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.embeddings.dual_index import DualIndexRetriever


class FakeIndexManager:
    def __init__(self, wiki_base, results=None):
        self.wiki_base = Path(wiki_base)
        self.client = MagicMock()
        self._results = results or []

    def search(self, query, top_k=10, similarity_threshold=None, use_rerank=False):
        return self._results[:top_k]

    def get_stats(self):
        return {"indexed": len(self._results)}


class FakeRelationManager:
    def __init__(self, db_path, results=None, page_map=None):
        self.db_path = Path(db_path)
        self.client = MagicMock()
        self._results = results or []
        self._page_map = page_map or {}

    def search(self, query, top_k=10):
        return self._results[:top_k]

    def get_stats(self):
        return {"indexed": len(self._results)}


def _make_fake_db(tmp_path, relations):
    db = tmp_path / "test_relations.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS relations (id INTEGER PRIMARY KEY, source TEXT, target TEXT)"
    )
    for rel_id, source, target in relations:
        conn.execute(
            "INSERT INTO relations (id, source, target) VALUES (?, ?, ?)",
            (rel_id, source, target),
        )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def tmp_wiki(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "page_a.md").write_text("# Page A\nContent A")
    (wiki / "page_b.md").write_text("# Page B\nContent B")
    (wiki / "page_c.md").write_text("# Page C\nContent C")
    return wiki


class TestDualIndexSearch:
    """双索引融合检索测试"""

    def test_page_only_no_relation_index(self, tmp_path, tmp_wiki):
        """只有页面向量索引时，直接返回页面结果"""
        page_results = [("page_a.md", 0.95), ("page_b.md", 0.80)]
        page_idx = FakeIndexManager(tmp_wiki, page_results)

        retriever = DualIndexRetriever(
            page_index=page_idx,
            relation_manager=None,
            wiki_base=tmp_wiki,
        )
        results = retriever.search("test query", top_k=5)
        assert len(results) == 2
        assert results[0] == ("page_a.md", pytest.approx(0.95 * 0.7))
        assert results[1] == ("page_b.md", pytest.approx(0.80 * 0.7))

        # search_detailed 返回分解分数
        detailed = retriever.search_detailed("test query", top_k=5)
        assert detailed[0][0] == "page_a.md"
        assert detailed[0][1] == pytest.approx(0.95 * 0.7)
        assert detailed[0][2] == pytest.approx(0.95)  # page_embedding_score
        assert detailed[0][3] == pytest.approx(0.0)   # relation_score

    def test_fusion_with_relation_boost(self, tmp_path, tmp_wiki):
        """双索引融合：页面得分 + 关联 boost"""
        page_results = [("page_a.md", 0.90), ("page_b.md", 0.70)]
        page_idx = FakeIndexManager(tmp_wiki, page_results)

        # relation 命中 page_a（id=1）和 page_c（id=2，不在页面结果中）
        db = _make_fake_db(
            tmp_path,
            [(1, "page_a.md", "page_c.md"), (2, "page_b.md", "page_d.md")],
        )
        rel_results = [(1, 0.80), (2, 0.60)]
        rel_mgr = FakeRelationManager(db, rel_results)

        retriever = DualIndexRetriever(
            page_index=page_idx,
            relation_manager=rel_mgr,
            wiki_base=tmp_wiki,
            content_weight=0.7,
            relation_weight=0.3,
        )
        results = retriever.search("test query", top_k=5, use_rerank=False)

        # page_a: 0.7*0.90 + 0.3*0.80 = 0.87
        # page_b: 0.7*0.70 + 0.3*0.60 = 0.67
        # page_c: 0 + 0.3*0.80 = 0.24
        detailed = retriever.search_detailed("test query", top_k=5, use_rerank=False)
        scores = {path: score for path, score, _, _ in detailed}
        page_scores = {path: ps for path, _, ps, _ in detailed}
        rel_scores = {path: rs for path, _, _, rs in detailed}
        assert scores["page_a.md"] == pytest.approx(0.87, abs=0.01)
        assert scores["page_b.md"] == pytest.approx(0.67, abs=0.01)
        assert scores["page_c.md"] == pytest.approx(0.24, abs=0.01)
        assert page_scores["page_a.md"] == pytest.approx(0.90)
        assert rel_scores["page_a.md"] == pytest.approx(0.24)  # 0.3*0.80 capped to 0.25, actually 0.24

    def test_relation_manager_no_client(self, tmp_wiki):
        """relation_manager 存在但 client 为 None 时，忽略关联索引"""
        page_results = [("page_a.md", 0.95)]
        page_idx = FakeIndexManager(tmp_wiki, page_results)
        rel_mgr = FakeRelationManager(tmp_wiki / "dummy.db", [])
        rel_mgr.client = None

        retriever = DualIndexRetriever(
            page_index=page_idx,
            relation_manager=rel_mgr,
            wiki_base=tmp_wiki,
        )
        results = retriever.search("test", top_k=5)
        assert len(results) == 1
        assert results[0][0] == "page_a.md"

    def test_empty_result(self, tmp_wiki):
        """两个索引都无结果时返回空列表"""
        page_idx = FakeIndexManager(tmp_wiki, [])
        rel_mgr = FakeRelationManager(tmp_wiki / "dummy.db", [])

        retriever = DualIndexRetriever(
            page_index=page_idx,
            relation_manager=rel_mgr,
            wiki_base=tmp_wiki,
        )
        results = retriever.search("test", top_k=5)
        assert results == []

    def test_rerank_path(self, tmp_path, tmp_wiki):
        """rerank 路径：融合后调用 rerank API"""
        page_results = [("page_a.md", 0.95), ("page_b.md", 0.85), ("page_c.md", 0.75)]
        page_idx = FakeIndexManager(tmp_wiki, page_results)
        page_idx.client.rerank.return_value = [(1, 0.99), (0, 0.88)]

        retriever = DualIndexRetriever(
            page_index=page_idx,
            relation_manager=None,
            wiki_base=tmp_wiki,
        )
        results = retriever.search("test", top_k=2, use_rerank=True)
        # rerank 返回 [(1, 0.99), (0, 0.88)] → valid_paths[1]=page_b, valid_paths[0]=page_a
        detailed = retriever.search_detailed("test", top_k=2, use_rerank=True)
        assert detailed[0][0] == "page_b.md"
        assert detailed[0][1] == pytest.approx(0.99)
        assert detailed[0][2] == pytest.approx(0.85)  # page_embedding_score
        assert detailed[1][0] == "page_a.md"
        assert detailed[1][1] == pytest.approx(0.88)
        assert detailed[1][2] == pytest.approx(0.95)  # page_embedding_score

    def test_rerank_failure_fallback(self, tmp_wiki):
        """rerank 失败时回退到融合排序"""
        page_results = [("page_a.md", 0.95), ("page_b.md", 0.85)]
        page_idx = FakeIndexManager(tmp_wiki, page_results)
        page_idx.client.rerank.side_effect = RuntimeError("API error")

        retriever = DualIndexRetriever(
            page_index=page_idx,
            relation_manager=None,
            wiki_base=tmp_wiki,
        )
        results = retriever.search("test", top_k=2, use_rerank=True)
        assert len(results) == 2
        assert results[0][0] == "page_a.md"

    def test_relation_query_db_failure(self, tmp_wiki):
        """关联查询数据库失败时不影响页面检索"""
        page_results = [("page_a.md", 0.90)]
        page_idx = FakeIndexManager(tmp_wiki, page_results)

        # 伪造一个不存在的关系表，触发查询异常
        db = tmp_wiki.parent / "broken.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.close()
        rel_mgr = FakeRelationManager(db, [(1, 0.80)])

        retriever = DualIndexRetriever(
            page_index=page_idx,
            relation_manager=rel_mgr,
            wiki_base=tmp_wiki,
        )
        results = retriever.search("test", top_k=5, use_rerank=False)
        assert len(results) == 1
        assert results[0][0] == "page_a.md"


class TestDualIndexStats:
    """统计接口测试"""

    def test_get_stats(self, tmp_wiki):
        page_idx = FakeIndexManager(tmp_wiki, [("a.md", 0.9)])
        rel_mgr = FakeRelationManager(tmp_wiki / "dummy.db", [(1, 0.8)])

        retriever = DualIndexRetriever(
            page_index=page_idx,
            relation_manager=rel_mgr,
        )
        stats = retriever.get_stats()
        assert stats["content_weight"] == 0.7
        assert stats["relation_weight"] == 0.3
        assert stats["page_index"]["indexed"] == 1
        assert stats["relation_index"]["indexed"] == 1
