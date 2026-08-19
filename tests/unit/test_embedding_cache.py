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

    def test_batch_duplicate_texts_share_one_cached_embedding(self, cache):
        cache.set("same context", [1.0, 2.0])

        results, missing = cache.get_batch(
            ["same context", "uncached", "same context"]
        )

        assert results == [[1.0, 2.0], None, [1.0, 2.0]]
        assert missing == [1]

        duplicate_misses, missing = cache.get_batch(["new context", "new context"])
        assert duplicate_misses == [None, None]
        assert missing == [0, 1]

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

    def test_subject_delete_globally_flushes_unattributable_cache_with_receipt(self, cache):
        """Content-hash cache cannot prove narrow ownership, so it must flush."""

        cache.set("deleted subject embedding", [1.0])
        cache.set("unrelated embedding", [2.0])
        result = EmbeddingCache.delete_subject_scope(
            db_path=cache.db_path,
            request_id="delete-cache-test",
            scope_kind="session",
            scope_value_hash="a" * 64,
        )

        assert result == {
            "status": "applied",
            "target_count": 2,
            "receipt_count": 1,
            "deleted_entry_count": 2,
            "after_entry_count": 0,
            "verified": True,
            "mode": "global_unattributable_cache_flush",
        }
        assert cache.get("deleted subject embedding") is None
        assert cache.get("unrelated embedding") is None
        assert cache.get_stats()["total_entries"] == 0

        retry = EmbeddingCache.delete_subject_scope(
            db_path=cache.db_path,
            request_id="delete-cache-test",
            scope_kind="session",
            scope_value_hash="a" * 64,
        )
        assert retry["status"] == "existing"
        assert retry["verified"] is True
