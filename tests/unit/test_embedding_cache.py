# -*- coding: utf-8 -*-
"""
EmbeddingCache 单元测试
"""

import tempfile
from pathlib import Path

import pytest

from core.embeddings.cache import EmbeddingCache


class TestEmbeddingCache:
    @pytest.fixture
    def cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "cache.db"
            yield EmbeddingCache(db_path=db, model_version="test-model")

    def test_compute_hash(self):
        h1 = EmbeddingCache.compute_hash("hello")
        h2 = EmbeddingCache.compute_hash("hello")
        h3 = EmbeddingCache.compute_hash("hello ")
        assert h1 == h2
        assert h1 == h3  # strip 后相同

    def test_set_and_get(self, cache):
        emb = [0.1, 0.2, 0.3]
        cache.set("hello", emb)
        result = cache.get("hello")
        assert result == emb

    def test_get_miss(self, cache):
        assert cache.get("not-exist") is None

    def test_model_version_isolation(self, cache):
        emb = [0.1, 0.2]
        cache.set("text", emb, model_version="model-a")
        assert cache.get("text", model_version="model-a") == emb
        assert cache.get("text", model_version="model-b") is None

    def test_batch_operations(self, cache):
        texts = ["a", "b", "c"]
        embs = [[1.0], [2.0], [3.0]]
        cache.set_batch(texts, embs)
        results, missing = cache.get_batch(texts)
        assert len(results) == 3
        assert missing == []
        assert results[0] == [1.0]
        assert results[1] == [2.0]
        assert results[2] == [3.0]

    def test_batch_partial_miss(self, cache):
        cache.set("a", [1.0])
        cache.set("c", [3.0])
        results, missing = cache.get_batch(["a", "b", "c"])
        assert results[0] == [1.0]
        assert results[1] is None
        assert results[2] == [3.0]
        assert missing == [1]

    def test_invalidate_model(self, cache):
        cache.set("x", [1.0], model_version="old-model")
        cache.set("y", [2.0], model_version="old-model")
        cache.set("z", [3.0], model_version="new-model")
        deleted = cache.invalidate_model("old-model")
        assert deleted == 2
        assert cache.get("x", model_version="old-model") is None
        assert cache.get("z", model_version="new-model") == [3.0]

    def test_stats(self, cache):
        cache.set("a", [1.0], model_version="m1")
        cache.set("b", [2.0], model_version="m1")
        cache.set("c", [3.0], model_version="m2")
        stats = cache.get_stats()
        assert stats["total_entries"] == 3
        assert stats["by_model"]["m1"] == 2
        assert stats["by_model"]["m2"] == 1
