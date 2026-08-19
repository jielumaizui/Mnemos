# -*- coding: utf-8 -*-
"""
Embedding 模块单元测试

测试 SiliconFlowEmbeddingClient 和 EmbeddingIndexManager 的核心逻辑。
所有 API 调用均 mock，不依赖真实网络。
"""

import sqlite3
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
    mock_openai.embeddings.create.return_value = FakeEmbeddingResponse(
        [
            [0.1] * 1024,
            [0.2] * 1024,
        ]
    )

    with patch.object(client, "_get_client", return_value=mock_openai):
        results = client.embed(["hello", "", "world"])

    assert len(results) == 3
    assert results[0][0] == 0.1
    assert results[1] is None  # [P1-30] 空文本返回 None，避免零向量除零
    assert results[2][0] == 0.2


def test_unbound_client_preserves_legacy_ledger_schema_failure(fake_config):
    """An existing noncanonical default ledger fails before provider dispatch."""
    from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient
    from core.ops.config_scope import use_config
    from core.telemetry.prompt_call_log import ModelCallLedgerInvariantError

    ledger_path = fake_config.database_dir / "model_call_ledger.db"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ledger_path) as conn:
        conn.execute("CREATE TABLE model_call_entries (entry_id TEXT PRIMARY KEY)")

    mock_openai = MagicMock()
    with use_config(fake_config):
        client = SiliconFlowEmbeddingClient(
            api_key="test-key",
            base_url="https://test.example/v1",
        )
        with patch.object(client, "_get_client", return_value=mock_openai):
            with pytest.raises(
                ModelCallLedgerInvariantError,
                match="backup-gated reconciliation",
            ):
                client.embed_single("test input")

    mock_openai.embeddings.create.assert_not_called()


def test_client_resolves_independent_embedding_and_reranker_env(monkeypatch):
    """Embedding 与 reranker 可分别配置 key/base_url/model。"""
    from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient

    monkeypatch.setenv("MNEMOS_EMBEDDING_API_KEY", "embed-key")
    monkeypatch.setenv("MNEMOS_EMBEDDING_BASE_URL", "https://embed.example.test/v1/")
    monkeypatch.setenv("MNEMOS_EMBEDDING_MODEL", "embed-model")
    monkeypatch.setenv("MNEMOS_RERANKER_API_KEY", "rerank-key")
    monkeypatch.setenv("MNEMOS_RERANKER_BASE_URL", "https://rerank.example.test/v1/")
    monkeypatch.setenv("MNEMOS_RERANKER_MODEL", "rerank-model")

    client = SiliconFlowEmbeddingClient()

    assert client.api_key == "embed-key"
    assert client.base_url == "https://embed.example.test/v1"
    assert client.embedding_model == "embed-model"
    assert client.rerank_api_key == "rerank-key"
    assert client.rerank_base_url == "https://rerank.example.test/v1"
    assert client.rerank_model == "rerank-model"


def test_client_uses_configured_embedding_and_reranker_request_fields():
    """Embedding/reranker 请求应使用用户配置的 base_url、key 和 model。"""
    from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient

    client = SiliconFlowEmbeddingClient(
        api_key="embedding-key",
        base_url="https://embedding.gateway.test/v1",
        embedding_model="custom-embedding-model",
        rerank_api_key="reranker-key",
        rerank_base_url="https://rerank.gateway.test/v1",
        rerank_model="custom-reranker-model",
    )

    mock_openai = MagicMock()
    mock_openai.embeddings.create.return_value = FakeEmbeddingResponse([[0.1] * 1024])
    with patch.object(client, "_get_client", return_value=mock_openai):
        client.embed(["hello"])

    mock_openai.embeddings.create.assert_called_once_with(
        model="custom-embedding-model",
        input=["hello"],
        encoding_format="float",
    )

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "results": [{"index": 0, "relevance_score": 0.9}],
        "usage": {"total_tokens": 5},
    }
    with patch("requests.post", return_value=mock_response) as mock_post:
        assert client.rerank("hello", ["doc"], top_n=1) == [(0, 0.9)]

    mock_post.assert_called_once()
    url = mock_post.call_args.args[0]
    kwargs = mock_post.call_args.kwargs
    assert url == "https://rerank.gateway.test/v1/rerank"
    assert kwargs["headers"]["Authorization"] == "Bearer reranker-key"
    assert kwargs["json"]["model"] == "custom-reranker-model"
    assert kwargs["json"]["query"] == "hello"
    assert kwargs["json"]["documents"] == ["doc"]
    assert kwargs["json"]["top_n"] == 1


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

    with (
        tempfile.TemporaryDirectory() as tmp,
        patch("core.embeddings.index_manager.embedding_available", return_value=True),
    ):
        wiki = Path(tmp) / "wiki"
        wiki.mkdir()
        acl = (
            "---\nscope: public\nsource_agent: human\nacl_schema_version: 1\n"
            "acl_metadata_complete: true\n"
            "acl_reconciliation_status: server_principal\n---\n\n"
        )
        (wiki / "page1.md").write_text(acl + "# Page 1\ncontent about python")
        (wiki / "page2.md").write_text(acl + "# Page 2\ncontent about rust")

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
    from unittest.mock import MagicMock

    with tempfile.TemporaryDirectory() as tmp:
        # 传入一个明确不可用的 mock client
        mock_client = MagicMock()
        mock_client.health_check.return_value = {"available": False}
        idx = EmbeddingIndexManager(
            wiki_base=Path(tmp) / "wiki",
            index_dir=Path(tmp) / "index",
            client=mock_client,
        )
        result = idx.build_index()
        assert result["status"] == "no_change"

        results = idx.search("test")
        assert results == []


def test_config_embedding_section():
    """配置文件中存在 embedding 配置节"""
    from core.config import DEFAULT_CONFIG

    assert "embedding" in DEFAULT_CONFIG
    assert DEFAULT_CONFIG["embedding"]["enabled"] is True
    assert DEFAULT_CONFIG["embedding"]["api_key"] == ""
    assert DEFAULT_CONFIG["embedding"]["api_key_env"] == "MNEMOS_EMBEDDING_API_KEY"
    assert DEFAULT_CONFIG["embedding"]["model"] == "BAAI/bge-m3"
    assert DEFAULT_CONFIG["embedding"]["embedding_model"] == "BAAI/bge-m3"
    assert DEFAULT_CONFIG["embedding"]["use_rerank"] is True
    assert DEFAULT_CONFIG["reranker"]["api_key"] == ""
    assert DEFAULT_CONFIG["reranker"]["api_key_env"] == "MNEMOS_RERANKER_API_KEY"
    assert DEFAULT_CONFIG["reranker"]["model"] == "BAAI/bge-reranker-v2-m3"
    assert DEFAULT_CONFIG["llm"]["api_key_env"] == "MNEMOS_LLM_API_KEY"
