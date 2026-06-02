# -*- coding: utf-8 -*-
"""
Embedding 模块集成测试 —— 缓存 + 限流 + Rerank
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.embeddings.siliconflow_client import SiliconFlowEmbeddingClient
from core.embeddings.cache import EmbeddingCache
from core.embeddings.rate_limiter import SiliconFlowRateLimiter


class FakeEmbeddingResponse:
    def __init__(self, embeddings, total_tokens=0):
        self.data = [MagicMock(embedding=e) for e in embeddings]
        self.usage = MagicMock(total_tokens=total_tokens)


class TestClientWithCacheAndLimiter:
    def test_embed_uses_cache_on_second_call(self):
        cache = EmbeddingCache(db_path=Path(tempfile.mktemp(suffix=".db")), model_version="BAAI/bge-m3")
        limiter = SiliconFlowRateLimiter(rpm=1000, tpm=1000000)
        client = SiliconFlowEmbeddingClient(api_key="test", base_url="https://test.com/v1", cache=cache, limiter=limiter)

        mock_openai = MagicMock()
        mock_openai.embeddings.create.return_value = FakeEmbeddingResponse([[0.1] * 1024], total_tokens=10)

        with patch.object(client, "_get_client", return_value=mock_openai):
            # 第一次调用 —— 应走 API
            r1 = client.embed(["hello"])
            assert mock_openai.embeddings.create.call_count == 1

            # 第二次调用 —— 应命中缓存
            r2 = client.embed(["hello"])
            assert mock_openai.embeddings.create.call_count == 1  # 不再增加
            assert r1 == r2

    def test_embed_respects_limiter(self):
        limiter = SiliconFlowRateLimiter(rpm=1, tpm=1000000, window_sec=60.0)
        client = SiliconFlowEmbeddingClient(api_key="test", base_url="https://test.com/v1", limiter=limiter)

        mock_openai = MagicMock()
        mock_openai.embeddings.create.return_value = FakeEmbeddingResponse([[0.1] * 1024], total_tokens=10)

        with patch.object(client, "_get_client", return_value=mock_openai):
            client.embed(["hello"])  # 第一次，应成功
            wait = limiter.acquire(estimated_tokens=10)
            assert wait > 0  # 第二次应被限流

    def test_embed_batch_partial_cache_hit(self):
        cache = EmbeddingCache(db_path=Path(tempfile.mktemp(suffix=".db")), model_version="BAAI/bge-m3")
        client = SiliconFlowEmbeddingClient(api_key="test", base_url="https://test.com/v1", cache=cache)

        # 预缓存一个
        cache.set("hello", [0.1] * 1024)

        mock_openai = MagicMock()
        mock_openai.embeddings.create.return_value = FakeEmbeddingResponse([[0.2] * 1024], total_tokens=10)

        with patch.object(client, "_get_client", return_value=mock_openai):
            results = client.embed(["hello", "world"])
            # hello 命中缓存，world 走 API
            assert mock_openai.embeddings.create.call_count == 1
            assert len(results) == 2
            assert results[0] == [0.1] * 1024  # 缓存
            assert results[1] == [0.2] * 1024  # API


class TestIndexManagerRerank:
    def test_search_with_rerank(self):
        from core.embeddings.index_manager import EmbeddingIndexManager

        with tempfile.TemporaryDirectory() as tmp:
            wiki_base = Path(tmp) / "wiki"
            wiki_base.mkdir()
            index_dir = Path(tmp) / "index"
            index_dir.mkdir()

            # 创建两个测试页面（文件名决定遍历顺序）
            (wiki_base / "aaa.md").write_text("---\ntitle: Redis\n---\n\nRedis is an in-memory database.")
            (wiki_base / "bbb.md").write_text("---\ntitle: Docker\n---\n\nDocker is a container platform.")

            # mock 客户端 —— 根据实际 documents 顺序调整返回值
            mock_client = MagicMock()
            mock_client.embed_single.return_value = [0.1] * 1024
            mock_client.embed.return_value = [[0.1] * 1024, [0.2] * 1024]

            def mock_rerank(query, documents, top_n=None):
                # 根据 documents 内容返回 rerank 结果
                # Docker (bbb) 应该排第一
                docker_idx = 0
                redis_idx = 1
                for i, doc in enumerate(documents):
                    if "Docker" in doc:
                        docker_idx = i
                    elif "Redis" in doc:
                        redis_idx = i
                return [(docker_idx, 0.95), (redis_idx, 0.80)]

            mock_client.rerank.side_effect = mock_rerank

            idx = EmbeddingIndexManager(wiki_base=wiki_base, index_dir=index_dir, client=mock_client)
            idx.build_index()

            # 启用 rerank
            results = idx.search("container", top_k=2, use_rerank=True)
            assert len(results) == 2
            assert results[0][0] == "bbb.md"
            assert results[1][0] == "aaa.md"

    def test_search_rerank_fallback_on_failure(self):
        from core.embeddings.index_manager import EmbeddingIndexManager

        with tempfile.TemporaryDirectory() as tmp:
            wiki_base = Path(tmp) / "wiki"
            wiki_base.mkdir()
            index_dir = Path(tmp) / "index"
            index_dir.mkdir()

            (wiki_base / "page1.md").write_text("---\ntitle: A\n---\n\nContent A")

            mock_client = MagicMock()
            mock_client.embed_single.return_value = [0.1] * 1024
            mock_client.embed.return_value = [[0.1] * 1024]
            mock_client.rerank.side_effect = Exception("API error")

            idx = EmbeddingIndexManager(wiki_base=wiki_base, index_dir=index_dir, client=mock_client)
            idx.build_index()

            # rerank 失败应回退到 ANN 排序
            results = idx.search("test", top_k=1, use_rerank=True)
            assert len(results) == 1
            assert results[0][0] == "page1.md"
