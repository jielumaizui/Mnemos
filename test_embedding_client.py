# -*- coding: utf-8 -*-
"""
Embedding 模块单元测试

测试 SiliconFlowEmbeddingClient 和 EmbeddingIndexManager 的核心逻辑。
所有 API 调用均 mock，不依赖真实网络。
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class FakeEmbeddingResponse:
    def __init__(self, embeddings):
        self.data = [MagicMock(embedding=e) for e in embeddings]


def test_embed_single():
    """单文本 embedding"""
    from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient

    client = SiliconFlowEmbeddingClient(api_key="test-key", base_url="https://test.com/v1")

    mock_openai = MagicMock()
    mock_openai.embeddings.create.return_value = FakeEmbeddingResponse([[0.1] * 1024])

    with patch.object(client, "_get_client", return_value=mock_openai):
        vec = client.embed_single("hello")

    assert len(vec) == 1024
    assert vec[0] == 0.1


def test_embed_batch():
    """批量 embedding，空字符串占位"""
    from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient

    client = SiliconFlowEmbeddingClient(api_key="test-key")

    mock_openai = MagicMock()
    mock_openai.embeddings.create.return_value = FakeEmbeddingResponse([
        [0.1] * 1024,
        [0.2] * 1024,
    ])

    with patch.object(client, "_get_client", return_value=mock_openai):
        results = client.embed(["hello", "", "world"])

    assert len(results) == 3
    assert results[0][0] == 0.1
    assert sum(abs(x) for x in results[1]) == 0.0  # 空字符串占位
    assert results[2][0] == 0.2


def test_cosine_similarity():
    """余弦相似度计算"""
    from core.embeddings.siliconflow_client import cosine_similarity

    a = [1.0, 0.0, 0.0]
    b = [1.0, 0.0, 0.0]
    assert cosine_similarity(a, b) == pytest.approx(1.0)

    c = [0.0, 1.0, 0.0]
    assert cosine_similarity(a, c) == pytest.approx(0.0)


def test_embedding_index_manager_memory_fallback():
    """索引管理器内存 fallback 模式"""
    from core.embeddings.index_manager import EmbeddingIndexManager
    from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient

    mock_client = MagicMock(spec=SiliconFlowEmbeddingClient)
    mock_client.embed_single.return_value = [0.1] * 1024
    mock_client.embed.return_value = [[0.1] * 1024, [0.2] * 1024]
    mock_client.health_check.return_value = {"available": True}

    with tempfile.TemporaryDirectory() as tmp, \
         patch("core.embeddings.index_manager.embedding_available", return_value=True):
        wiki = Path(tmp) / "wiki"
        wiki.mkdir()
        (wiki / "page1.md").write_text("# Page 1\ncontent about python")
        (wiki / "page2.md").write_text("# Page 2\ncontent about rust")

        idx = EmbeddingIndexManager(
            wiki_base=wiki,
            index_dir=Path(tmp) / "index",
            client=mock_client,
        )
        result = idx.build_index()

    assert result["status"] == "ok"
    assert result["total"] == 2

    # 搜索
    mock_client.embed_single.return_value = [0.1] * 1024
    with patch("core.embeddings.index_manager.embedding_available", return_value=True):
        results = idx.search("python tutorial", top_k=5)
    assert len(results) > 0


def test_embedding_disabled_returns_empty():
    """embedding 未启用时返回空"""
    from core.embeddings.index_manager import EmbeddingIndexManager

    with tempfile.TemporaryDirectory() as tmp:
        idx = EmbeddingIndexManager(
            wiki_base=Path(tmp) / "wiki",
            index_dir=Path(tmp) / "index",
            client=None,
        )
        result = idx.build_index()
        assert result["status"] == "no_client"

        results = idx.search("test")
        assert results == []


def test_config_embedding_section():
    """配置文件中存在 embedding 配置节"""
    from core.config import DEFAULT_CONFIG

    assert "embedding" in DEFAULT_CONFIG
    assert DEFAULT_CONFIG["embedding"]["enabled"] is False
    assert DEFAULT_CONFIG["embedding"]["embedding_model"] == "BAAI/bge-m3"
    assert DEFAULT_CONFIG["embedding"]["similarity_threshold"] == 0.72
