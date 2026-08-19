# -*- coding: utf-8 -*-
"""
DualIndexRetriever 单元测试
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.embeddings.dual_index import DualIndexRetriever


class FakeIndexManager:
    def __init__(self, wiki_base, results=None):
        self.wiki_base = Path(wiki_base)
        self.client = MagicMock()
        self._results = results or []

    def search(
        self,
        query,
        top_k=10,
        similarity_threshold=None,
        use_rerank=False,
        *,
        allowed_page_paths=None,
    ):
        results = self._results
        if allowed_page_paths is not None:
            allowed = set(allowed_page_paths)
            results = [item for item in results if item[0] in allowed]
        return results[:top_k]

    def get_stats(self):
        return {"indexed": len(self._results)}


class FakeRelationManager:
    def __init__(self, db_path, results=None, page_map=None):
        self.db_path = Path(db_path)
        self.client = MagicMock()
        self._results = results or []
        self._page_map = page_map or {}  # noqa
        self.allowed_relation_ids = None

    def search(self, query, top_k=10, *, allowed_relation_ids=None):
        self.allowed_relation_ids = allowed_relation_ids
        results = self._results
        if allowed_relation_ids is not None:
            allowed = set(allowed_relation_ids)
            results = [item for item in results if item[0] in allowed]
        return results[:top_k]

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
        assert detailed[0][3] == pytest.approx(0.0)  # relation_score
        trace = retriever.get_last_trace()
        assert trace["page_search_attempted"] is True
        assert trace["page_search_ok"] is True
        assert trace["page_result_count"] == 2
        assert trace["relation_index_available"] is False
        assert trace["returned_count"] == 2

    def test_acl_allowlist_is_applied_before_semantic_top_k(self, tmp_wiki):
        page_results = [(f"denied-{index:02d}.md", 1.0 - index / 1000) for index in range(25)] + [
            ("page_c.md", 0.70)
        ]
        page_idx = FakeIndexManager(tmp_wiki, page_results)
        retriever = DualIndexRetriever(
            page_index=page_idx,
            relation_manager=None,
            wiki_base=tmp_wiki,
        )

        results = retriever.search_detailed(
            "authorized tail",
            top_k=1,
            use_rerank=False,
            allowed_page_paths={"page_c.md"},
        )

        assert [item[0] for item in results] == ["page_c.md"]

    def test_relation_acl_allowlist_is_applied_before_relation_top_k_and_refills(
        self,
        tmp_path,
        tmp_wiki,
    ):
        page_idx = FakeIndexManager(tmp_wiki, [("page_c.md", 0.70)])
        relations = [
            (index, f"denied-{index:02d}.md", f"hidden-{index:02d}.md") for index in range(1, 26)
        ] + [(26, "page_c.md", "page_c.md")]
        db = _make_fake_db(tmp_path, relations)
        relation_results = [(index, 1.0 - index / 1000) for index in range(1, 27)]
        rel_mgr = FakeRelationManager(db, relation_results)
        retriever = DualIndexRetriever(
            page_index=page_idx,
            relation_manager=rel_mgr,
            wiki_base=tmp_wiki,
        )

        results = retriever.search_detailed(
            "authorized relation tail",
            top_k=1,
            use_rerank=False,
            allowed_page_paths={"page_c.md"},
        )

        assert rel_mgr.allowed_relation_ids == {26}
        assert results[0][0] == "page_c.md"
        assert results[0][3] == pytest.approx(0.25)
        trace = retriever.get_last_trace()
        assert trace["relation_acl_candidate_count"] == 26
        assert trace["relation_acl_allowed_count"] == 1

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
        _ = retriever.search("test query", top_k=5, use_rerank=False)

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
        assert rel_scores["page_a.md"] == pytest.approx(
            0.24
        )  # 0.3*0.80 capped to 0.25, actually 0.24

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
        trace = retriever.get_last_trace()
        assert trace["page_search_attempted"] is True
        assert trace["page_search_ok"] is True
        assert trace["page_result_count"] == 0
        assert trace["returned_count"] == 0
        assert trace["degraded"] is False

    def test_page_embedding_client_unavailable_trace(self, tmp_wiki):
        """页面 embedding client 不可用时返回空结果并暴露降级原因"""
        page_idx = FakeIndexManager(tmp_wiki, [("page_a.md", 0.95)])
        page_idx.client = None

        retriever = DualIndexRetriever(
            page_index=page_idx,
            relation_manager=None,
            wiki_base=tmp_wiki,
        )
        results = retriever.search("test", top_k=5)
        trace = retriever.get_last_trace()

        assert results == []
        assert trace["page_index_available"] is False
        assert trace["page_search_attempted"] is False
        assert trace["degraded"] is True
        assert "page_embedding_client_unavailable" in trace["degraded_reasons"]

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
        _ = retriever.search("test", top_k=2, use_rerank=True)
        # rerank 返回 [(1, 0.99), (0, 0.88)] → valid_paths[1]=page_b, valid_paths[0]=page_a
        detailed = retriever.search_detailed("test", top_k=2, use_rerank=True)
        assert detailed[0][0] == "page_b.md"
        assert detailed[0][1] == pytest.approx(0.99)
        assert detailed[0][2] == pytest.approx(0.85)  # page_embedding_score
        assert detailed[1][0] == "page_a.md"
        assert detailed[1][1] == pytest.approx(0.88)
        assert detailed[1][2] == pytest.approx(0.95)  # page_embedding_score
        trace = retriever.get_last_trace()
        assert trace["rerank_configured"] is True
        assert trace["rerank_attempted"] is True
        assert trace["rerank_api_called"] is True
        assert trace["rerank_applied"] is True
        assert trace["rerank_degraded"] is False

    def test_rerank_failure_fallback(self, tmp_wiki):
        """rerank 失败时回退到融合排序"""
        page_results = [("page_a.md", 0.95), ("page_b.md", 0.85), ("page_c.md", 0.75)]
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
        trace = retriever.get_last_trace()
        assert trace["rerank_attempted"] is True
        assert trace["rerank_api_called"] is True
        assert trace["rerank_applied"] is False
        assert trace["rerank_degraded"] is True
        assert "rerank_failed" in trace["degraded_reasons"]

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
