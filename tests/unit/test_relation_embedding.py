# -*- coding: utf-8 -*-
"""
RelationEmbeddingManager 单元测试
"""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.embeddings.relation_manager import RelationEmbeddingManager, HNSWLIB_AVAILABLE

pytestmark = pytest.mark.skipif(not HNSWLIB_AVAILABLE, reason="hnswlib not installed")  # noqa


class TestRelationEmbeddingManager:
    @pytest.fixture
    def mgr(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            idx_dir = Path(tmp) / "index"
            mock_client = MagicMock()
            mock_client.embed_single.return_value = [0.1] * 1024
            manager = RelationEmbeddingManager(db_path=db, index_dir=idx_dir, client=mock_client)
            try:
                yield manager
            finally:
                manager.close()

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

    def test_remove_treats_missing_hnsw_label_as_already_deleted(self, mgr, monkeypatch):
        mgr._rel_id_map[7] = 700
        mgr._hid_to_rel_id[700] = 7
        mgr._rel_id_map[8] = 800
        mgr._hid_to_rel_id[800] = 8
        index = MagicMock()
        index.mark_deleted.side_effect = RuntimeError("Label not found")
        monkeypatch.setattr(mgr, "_get_hnsw_index", lambda: index)

        assert mgr.remove_relation_context(7) is True
        assert 7 not in mgr._rel_id_map
        mgr._rel_id_map.clear()
        mgr._hid_to_rel_id.clear()

    def test_remove_failure_restores_mapping_for_retry(self, mgr, monkeypatch):
        mgr._rel_id_map[7] = 700
        mgr._hid_to_rel_id[700] = 7
        mgr._rel_id_map[8] = 800
        mgr._hid_to_rel_id[800] = 8
        index = MagicMock()
        index.mark_deleted.side_effect = [RuntimeError("disk unavailable"), None]
        monkeypatch.setattr(mgr, "_get_hnsw_index", lambda: index)

        assert mgr.remove_relation_context(7) is False
        assert mgr._rel_id_map[7] == 700
        assert mgr.remove_relation_context(7) is True
        assert 7 not in mgr._rel_id_map

    def test_add_empty_context_skipped(self, mgr):
        assert mgr.add_relation_context(1, "") is False
        assert mgr.add_relation_context(2, "   ") is False

    def test_duplicate_add_idempotent(self, mgr):
        mgr.add_relation_context(1, "context text")
        # 第二次应直接返回成功（已存在）
        assert mgr.add_relation_context(1, "different text") is True
        results = mgr.search("test", top_k=5)
        assert len(results) == 1

    def test_batch_add_embeds_multiple_contexts_in_one_client_call(self, mgr):
        mgr.client.embed.return_value = [[0.1] * 1024, [0.2] * 1024]

        result = mgr.add_relation_contexts(
            {11: "first relation context", 12: "second relation context"}
        )

        assert result == {"total": 2, "added": 2, "skipped": 0, "failed": 0}
        mgr.client.embed.assert_called_once_with(
            ["first relation context", "second relation context"]
        )
        assert set(mgr._rel_id_map) >= {11, 12}

    def test_failed_index_save_is_durably_rebuilt_on_retry(self, mgr, monkeypatch):
        mgr._batch_flush = False
        mgr.client.embed.return_value = [[0.1] * 1024]
        original_save = mgr._save_index_atomic
        attempts = {"count": 0}

        def fail_once(index):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise OSError("injected save failure")
            return original_save(index)

        monkeypatch.setattr(mgr, "_save_index_atomic", fail_once)
        first = mgr.add_relation_contexts({21: "durable relation"})
        assert first["failed"] == 1
        assert mgr._rebuild_required() is True

        second = mgr.add_relation_contexts({21: "durable relation"})
        assert second["failed"] == 0
        assert mgr._rebuild_required() is False
        assert (mgr.index_dir / "relation_index.bin").is_file()

    def test_persistent_rebuild_uses_durable_embedding_row_ids_as_hnsw_labels(
        self, mgr
    ):
        mgr.add_relation_context(1, "durable relation context")
        with sqlite3.connect(mgr.db_path) as conn:
            durable_label = int(
                conn.execute(
                    "SELECT id FROM relation_context_embeddings WHERE relation_id=1"
                ).fetchone()[0]
            )
        mgr._rel_id_map[1] = durable_label + 700
        mgr._hid_to_rel_id = {durable_label + 700: 1}
        mgr._next_id = durable_label + 701

        assert mgr.rebuild_persistent_index() is True

        import hnswlib

        index = hnswlib.Index(space="cosine", dim=1024)
        index.load_index(str(mgr.index_dir / "relation_index.bin"))
        assert set(index.get_ids_list()) == {durable_label}
        assert mgr._rel_id_map == {1: durable_label}
        assert mgr._hid_to_rel_id == {durable_label: 1}

    def test_stats(self, mgr):
        mgr.add_relation_context(1, "a")
        mgr.add_relation_context(2, "b")
        stats = mgr.get_stats()
        assert stats["total_relations"] == 2

    def test_sqlite_fallback_when_hnsw_missing(self, mgr):
        """[P1-5] 删除 hnsw 索引文件后，search 应通过 SQLite fallback 返回结果"""
        mgr.add_relation_context(1, "部署 Redis 集群")
        mgr.add_relation_context(2, "Docker 网络配置")

        # 删除 hnsw 索引文件，模拟损坏/缺失
        index_file = mgr.index_dir / "relation_index.bin"
        if index_file.exists():
            index_file.unlink()

        # 清除内存中的 hnsw index 引用，强制下次 search 重建或 fallback
        mgr._index = None

        results = mgr.search("Redis 部署", top_k=5)
        assert len(results) > 0
        # relation_id=1 的 "部署 Redis 集群" 应该匹配
        rel_ids = [r[0] for r in results]
        assert 1 in rel_ids

    def test_hnsw_acl_filter_refills_beyond_denied_top_twenty_five(
        self,
        mgr,
        monkeypatch,
    ):
        class FakeIndex:
            def __init__(self):
                self.requested_k = []

            def get_current_count(self):
                return 26

            def knn_query(self, vectors, *, k):
                del vectors
                self.requested_k.append(k)
                return [list(range(1, k + 1))], [[index / 1000 for index in range(1, k + 1)]]

        index = FakeIndex()
        mgr._batch_flush = False
        mgr._hid_to_rel_id = {index: index for index in range(1, 27)}
        monkeypatch.setattr(mgr, "_get_hnsw_index", lambda: index)

        results = mgr.search(
            "authorized tail",
            top_k=1,
            allowed_relation_ids={26},
        )

        assert [item[0] for item in results] == [26]
        assert index.requested_k == [1, 2, 4, 8, 16, 26]

    def test_sqlite_fallback_filters_acl_before_top_k(self, mgr, monkeypatch):
        mgr._batch_flush = False
        monkeypatch.setattr(mgr, "_get_hnsw_index", lambda: None)
        with sqlite3.connect(mgr.db_path) as connection:
            connection.executemany(
                """INSERT OR REPLACE INTO relation_context_embeddings
                   (relation_id, embedding, model_version)
                   VALUES (?, ?, 'test')""",
                [
                    (relation_id, json.dumps([float(27 - relation_id)] * 1024))
                    for relation_id in range(1, 27)
                ],
            )
            connection.commit()

        results = mgr.search(
            "authorized tail",
            top_k=1,
            allowed_relation_ids={26},
        )

        assert [item[0] for item in results] == [26]


class TestRelationEmbeddingBatchFlush:
    @pytest.fixture
    def batch_mgr(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            idx_dir = Path(tmp) / "index"
            mock_client = MagicMock()
            mock_client.embed_single.return_value = [0.1] * 1024
            with patch("core.embeddings.relation_manager.get_config") as mock_cfg:
                mock_cfg.return_value = _FakeConfig(batch_flush=True, flush_batch_size=3)
                mgr = RelationEmbeddingManager(db_path=db, index_dir=idx_dir, client=mock_client)
                try:
                    yield mgr
                finally:
                    mgr.close()

    def test_batch_flush_defers_save_until_batch_size(self, batch_mgr):
        """批量模式下，达到 batch_size 才落盘。"""
        index_file = batch_mgr.index_dir / "relation_index.bin"
        batch_mgr.add_relation_context(1, "a")
        batch_mgr.add_relation_context(2, "b")
        assert not index_file.exists() or index_file.stat().st_size == 0
        batch_mgr.add_relation_context(3, "c")
        # 达到 batch_size，触发 flush
        assert index_file.exists()
        assert index_file.stat().st_size > 0

    def test_search_flushes_pending_changes(self, batch_mgr):
        """批量模式下，search 前应 flush 未落盘的变更。"""
        batch_mgr.add_relation_context(1, "search flush text")
        results = batch_mgr.search("search flush", top_k=5)
        assert len(results) > 0
        assert results[0][0] == 1

    def test_explicit_batch_scope_suppresses_threshold_flush(self, batch_mgr):
        """Migration batches persist one index generation, not one per threshold."""

        index_file = batch_mgr.index_dir / "relation_index.bin"
        with batch_mgr.defer_automatic_flush():
            for relation_id in range(1, 7):
                assert batch_mgr.add_relation_context(relation_id, f"context {relation_id}")
            assert not index_file.exists() or index_file.stat().st_size == 0

        assert batch_mgr.flush() is True
        assert index_file.is_file()
        assert index_file.stat().st_size > 0

    def test_close_flushes_pending_changes(self, batch_mgr):
        """close 时应 flush 未落盘的变更。"""
        batch_mgr.add_relation_context(1, "close flush text")
        batch_mgr.close()
        index_file = batch_mgr.index_dir / "relation_index.bin"
        assert index_file.exists()
        assert index_file.stat().st_size > 0


class _FakeConfig:
    def __init__(self, batch_flush=False, flush_batch_size=10, flush_interval_seconds=60):
        self._data = {
            "relation_embedding.batch_flush": batch_flush,
            "relation_embedding.flush_batch_size": flush_batch_size,
            "relation_embedding.flush_interval_seconds": flush_interval_seconds,
        }
        self.database_dir = Path("/tmp")

    def get(self, key, default=None):
        return self._data.get(key, default)
