# -*- coding: utf-8 -*-
"""
RelationEmbeddingManager 单元测试
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.embeddings.relation_manager import RelationEmbeddingManager


class TestRelationEmbeddingManager:
    @pytest.fixture
    def mgr(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            idx_dir = Path(tmp) / "index"
            mock_client = MagicMock()
            mock_client.embed_single.return_value = [0.1] * 1024
            yield RelationEmbeddingManager(db_path=db, index_dir=idx_dir, client=mock_client)

    def test_add_and_search(self, mgr):
        mgr.add_relation_context(1, "部署 Redis 集群需要预先配置 Docker 环境")
        results = mgr.search("部署环境准备", top_k=5)
        assert len(results) > 0
        assert results[0][0] == 1  # relation_id

    def test_remove_relation_context(self, mgr):
        mgr.add_relation_context(1, "context text")
        assert mgr.remove_relation_context(1) is True
        results = mgr.search("test", top_k=5)
        assert len(results) == 0

    def test_add_empty_context_skipped(self, mgr):
        assert mgr.add_relation_context(1, "") is False
        assert mgr.add_relation_context(2, "   ") is False

    def test_duplicate_add_idempotent(self, mgr):
        mgr.add_relation_context(1, "context text")
        # 第二次应直接返回成功（已存在）
        assert mgr.add_relation_context(1, "different text") is True
        results = mgr.search("test", top_k=5)
        assert len(results) == 1

    def test_stats(self, mgr):
        mgr.add_relation_context(1, "a")
        mgr.add_relation_context(2, "b")
        stats = mgr.get_stats()
        assert stats["total_relations"] == 2
