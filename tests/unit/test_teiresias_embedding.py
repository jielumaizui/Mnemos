"""
PredictivePushEngine embedding 语义召回测试

覆盖：
- embedding 启用时的语义召回
- 无 frontmatter 页面通过语义召回被推送
- 规则召回与语义召回融合取高分
- embedding 不可用时回退到规则召回
"""

import pytest

from core.kia.teiresias import (
    ContextSignal,
    PredictivePushEngine,
)


@pytest.fixture
def wiki_with_untagged_page(tmp_path):
    """创建一个带无标签页面的 Wiki 目录"""
    page = tmp_path / "03-Tech" / "docker-image-optimization.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
类型: 问题-解决
---
# Docker Image Optimization

## 核心内容
排查 Docker 镜像体积过大时，可以使用多阶段构建、压缩层、删除缓存。
""",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def wiki_with_tagged_page(tmp_path):
    """创建一个带 frontmatter 标签的 Wiki 目录"""
    page = tmp_path / "03-Tech" / "python-debug.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        """---
类型: 问题-解决
关键词:
  核心概念: [调试]
  场景标签: [报错]
  工具实体: [Python]
---
# Python Debug

## 核心内容
排查 Python 报错时先定位堆栈和复现步骤。
""",
        encoding="utf-8",
    )
    return tmp_path


class FakeEmbeddingIndexManager:
    """模拟 EmbeddingIndexManager.search 返回固定语义结果"""

    def __init__(self, results):
        self.results = results

    def search(self, query, top_k=10, similarity_threshold=0.5, use_rerank=False):
        return list(self.results)


class FakeEmbeddingClient:
    def health_check(self):
        return {"available": True}


class DisabledEmbeddingClient:
    def health_check(self):
        return {"available": False}


def _make_engine(wiki_base, monkeypatch, client_available=True, semantic_results=None):
    engine = PredictivePushEngine(
        wiki_base=str(wiki_base),
        db_path=str(wiki_base / ".kg" / "push.db"),
    )

    # mock embedding client
    client = FakeEmbeddingClient() if client_available else DisabledEmbeddingClient()
    monkeypatch.setattr("core.embeddings.siliconflow_client.get_embedding_client", lambda: client)

    # mock EmbeddingIndexManager if semantic results provided
    if semantic_results is not None:

        def _fake_idx(wiki_base=None, **kwargs):
            return FakeEmbeddingIndexManager(semantic_results)

        monkeypatch.setattr("core.embeddings.index_manager.EmbeddingIndexManager", _fake_idx)

    # 强制 embedding 配置启用
    monkeypatch.setattr(
        "core.kia.teiresias.get_config",
        lambda: {"embedding.enabled": True, "embedding.use_rerank": False},
    )
    return engine


def test_semantic_search_recover_untagged_page(wiki_with_untagged_page, monkeypatch):
    """无 frontmatter 标签的页面可通过语义召回被推送"""
    wiki = wiki_with_untagged_page
    rel = "03-Tech/docker-image-optimization.md"
    engine = _make_engine(
        wiki,
        monkeypatch,
        semantic_results=[(rel, 0.72)],
    )

    signal = ContextSignal(
        signal_type="question",
        keywords=["docker", "镜像", "变小"],
        mentioned_tools=[],
        confidence=0.8,
    )
    matches = engine.match_knowledge(signal)

    assert any("docker-image-optimization" in m.page_path for m in matches)
    semantic_hit = next(m for m in matches if "docker-image-optimization" in m.page_path)
    assert semantic_hit.match_score > 0.4
    assert "语义召回" in semantic_hit.match_reason


def test_rule_and_embedding_fusion_takes_max_score(wiki_with_tagged_page, monkeypatch):
    """规则与语义同时命中同一页面时取最高分"""
    wiki = wiki_with_tagged_page
    rel = "03-Tech/python-debug.md"
    engine = _make_engine(
        wiki,
        monkeypatch,
        semantic_results=[(rel, 0.55)],  # 映射后约 0.486，低于规则召回
    )

    decision = engine.decide_push("Python 报错怎么处理", session_id="s1")

    assert decision.should_push is True
    assert decision.matches[0].page_title == "Python Debug"
    # 规则召回分高于语义召回，应保留规则召回
    assert decision.matches[0].match_score >= 0.6
    assert "工具匹配" in decision.matches[0].match_reason


def test_embedding_exception_falls_back_to_rule(wiki_with_tagged_page, monkeypatch):
    """embedding 搜索抛异常时应回退到规则召回，不崩溃"""
    wiki = wiki_with_tagged_page
    engine = PredictivePushEngine(
        wiki_base=str(wiki),
        db_path=str(wiki / ".kg" / "push.db"),
    )

    monkeypatch.setattr(
        "core.kia.teiresias.get_config",
        lambda: {"embedding.enabled": True, "embedding.use_rerank": False},
    )
    monkeypatch.setattr(
        "core.embeddings.siliconflow_client.get_embedding_client",
        lambda: FakeEmbeddingClient(),
    )

    class BoomIndexManager:
        def search(self, *args, **kwargs):
            raise RuntimeError("embedding search boom")

    monkeypatch.setattr("core.embeddings.index_manager.EmbeddingIndexManager", BoomIndexManager)

    decision = engine.decide_push("Python 报错怎么处理", session_id="s2")

    assert decision.should_push is True
    assert decision.matches[0].page_title == "Python Debug"


def test_embedding_disabled_falls_back_to_rule(wiki_with_tagged_page, monkeypatch):
    """embedding 未启用时只走规则召回"""
    wiki = wiki_with_tagged_page
    engine = PredictivePushEngine(
        wiki_base=str(wiki),
        db_path=str(wiki / ".kg" / "push.db"),
    )

    monkeypatch.setattr(
        "core.kia.teiresias.get_config",
        lambda: {"embedding.enabled": False},
    )

    decision = engine.decide_push("Python 报错怎么处理", session_id="s3")

    assert decision.should_push is True
    assert decision.matches[0].page_title == "Python Debug"


def test_map_semantic_score_monotonic():
    """语义分数映射单调且范围正确"""
    engine = PredictivePushEngine(wiki_base="/tmp")
    assert engine._map_semantic_score(0.5) == pytest.approx(0.4, abs=0.01)
    assert engine._map_semantic_score(0.85) == pytest.approx(1.0, abs=0.01)
    assert engine._map_semantic_score(0.675) == pytest.approx(0.7, abs=0.02)
    assert engine._map_semantic_score(1.0) == 1.0
    assert engine._map_semantic_score(0.3) == 0.0
