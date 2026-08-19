# -*- coding: utf-8 -*-
"""Unit tests for core.sync_framework.storage_backend contract."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from core.sync_framework.storage_backend import (
    StorageBackend,
    StorageResult,
    clear_storage_backend_factories,
    create_storage_backend,
    register_storage_backend,
)


class FakeStorageBackend(StorageBackend):
    """内存中的 StorageBackend 实现，用于测试接口契约。"""

    def __init__(self):
        self._records: Dict[str, StorageResult] = {}
        self._counter = 0

    def save(self, content: str, tags: List[str], title: str) -> List[StorageResult]:
        self._counter += 1
        uid = f"{title}-{self._counter}"
        result = StorageResult(
            uid=uid,
            content=content,
            tags=tags,
            metadata={"title": title},
            created_at="2024-01-01T00:00:00",
        )
        self._records[uid] = result
        return [result]

    def search(self, query: str, limit: Optional[int] = None) -> List[StorageResult]:
        results = [r for r in self._records.values() if query.lower() in r.content.lower()]
        if limit:
            results = results[:limit]
        return results

    def list_by_tags(self, tags: List[str], limit: Optional[int] = None) -> List[StorageResult]:
        results = [r for r in self._records.values() if all(t in r.tags for t in tags)]
        if limit:
            results = results[:limit]
        return results

    def get_by_id(self, uid: str) -> Optional[StorageResult]:
        return self._records.get(uid)

    def health_check(self) -> Dict[str, Any]:
        return {"status": "ok", "count": len(self._records)}

    def update_tags(
        self,
        uid: str,
        add_tags: Optional[List[str]] = None,
        remove_tags: Optional[List[str]] = None,
    ) -> Optional[StorageResult]:
        result = self._records.get(uid)
        if result is None:
            return None
        new_tags = set(result.tags)
        new_tags.update(add_tags or [])
        new_tags.difference_update(remove_tags or [])
        result.tags = list(new_tags)
        return result


@pytest.fixture
def backend():
    return FakeStorageBackend()


class TestStorageBackendContract:
    def test_save_returns_result(self, backend):
        results = backend.save("hello world", ["a=1"], "note")
        assert len(results) == 1
        assert results[0].uid == "note-1"
        assert results[0].content == "hello world"

    def test_get_by_id(self, backend):
        backend.save("content", [], "page")
        result = backend.get_by_id("page-1")
        assert result is not None
        assert result.content == "content"

    def test_search(self, backend):
        backend.save("alpha", [], "a")
        backend.save("beta", [], "b")
        assert len(backend.search("alpha")) == 1
        assert backend.search("alpha")[0].metadata["title"] == "a"

    def test_list_by_tags(self, backend):
        backend.save("x", ["source=claude", "layer=L1"], "x")
        backend.save("y", ["source=kimi", "layer=L1"], "y")
        results = backend.list_by_tags(["layer=L1"])
        assert len(results) == 2
        results = backend.list_by_tags(["source=claude", "layer=L1"])
        assert len(results) == 1

    def test_update_tags(self, backend):
        backend.save("x", ["a", "b"], "x")
        updated = backend.update_tags("x-1", add_tags=["c"], remove_tags=["a"])
        assert updated is not None
        assert set(updated.tags) == {"b", "c"}

    def test_health_check(self, backend):
        backend.save("x", [], "x")
        health = backend.health_check()
        assert health["status"] == "ok"
        assert health["count"] == 1


class TestStorageBackendFactory:
    def teardown_method(self):
        clear_storage_backend_factories()

    def test_create_uses_registered_factory(self):
        register_storage_backend("memory", lambda **kwargs: FakeStorageBackend())

        backend = create_storage_backend("memory")

        assert isinstance(backend, FakeStorageBackend)

    def test_create_obsidian_by_name(self, tmp_path):
        backend = create_storage_backend("obsidian", vault_path=tmp_path)
        from integrations.backends import ObsidianBackend

        assert isinstance(backend, ObsidianBackend)

    def test_create_default_reads_config(self, tmp_path, monkeypatch):
        class _Cfg:
            storage_backend = "obsidian"

        monkeypatch.setattr(
            "core.config.get_config",
            lambda: _Cfg(),
        )
        backend = create_storage_backend(vault_path=tmp_path)
        from integrations.backends import ObsidianBackend

        assert isinstance(backend, ObsidianBackend)

    def test_create_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="不支持的 storage backend"):
            create_storage_backend("sqlite")

    def test_plugin_env_var_rejects_outside_whitelist(self, monkeypatch):
        """MNEMOS_STORAGE_BACKEND_PLUGINS 中的越界模块名必须被拒绝加载。"""
        from core.sync_framework import storage_backend

        monkeypatch.setenv("MNEMOS_STORAGE_BACKEND_PLUGINS", "os.system")
        clear_storage_backend_factories()
        # 重新触发插件加载
        storage_backend._load_storage_backend_plugins()
        # os.system 不在白名单内，不应注册任何 factory
        assert "os.system" not in storage_backend._BACKEND_FACTORIES

    def test_plugin_env_var_accepts_whitelisted_module(self, monkeypatch, tmp_path):
        """MNEMOS_STORAGE_BACKEND_PLUGINS 中的白名单模块可被加载。"""
        from core.sync_framework import storage_backend

        # 使用 integrations.backends 作为已知可加载的白名单模块
        monkeypatch.setenv(
            "MNEMOS_STORAGE_BACKEND_PLUGINS", "integrations.backends"
        )
        clear_storage_backend_factories()
        storage_backend._load_storage_backend_plugins()
        # integrations.backends 会注册 obsidian factory
        assert "obsidian" in storage_backend._BACKEND_FACTORIES
