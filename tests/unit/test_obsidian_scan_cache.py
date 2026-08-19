# -*- coding: utf-8 -*-
"""Tests for ObsidianBackend scan cache LRU/TTL behavior (S32)."""

from __future__ import annotations

import time

from integrations.backends.obsidian_backend import ObsidianBackend


def _fake_config(tmp_path, ttl):
    return type(
        "C",
        (),
        {
            "obsidian_vault_path": tmp_path,
            "get": lambda self, key, default=None: {
                "storage.obsidian.scan_cache_ttl_seconds": ttl,
                "raw_projection.enabled": False,
            }.get(key, default),
        },
    )()


class TestObsidianScanCache:
    def test_scan_cache_honors_ttl(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "integrations.backends.obsidian_backend.get_config",
            lambda: _fake_config(tmp_path, 0.01),
        )
        backend = ObsidianBackend(vault_path=tmp_path)
        backend._set_cached_scan("search", ("q",), ["result"])
        assert backend._get_cached_scan("search", ("q",)) == ["result"]
        time.sleep(0.02)
        assert backend._get_cached_scan("search", ("q",)) is None

    def test_scan_cache_lru_eviction(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "integrations.backends.obsidian_backend.get_config",
            lambda: _fake_config(tmp_path, 3600),
        )
        backend = ObsidianBackend(vault_path=tmp_path)
        backend._scan_cache_max_entries = 3
        for i in range(5):
            backend._set_cached_scan("op", (i,), f"v{i}")
        # Only the 3 most recent entries should remain.
        assert len(backend._scan_cache) == 3
        assert backend._get_cached_scan("op", (0,)) is None
        assert backend._get_cached_scan("op", (1,)) is None
        assert backend._get_cached_scan("op", (2,)) == "v2"
        assert backend._get_cached_scan("op", (3,)) == "v3"
        assert backend._get_cached_scan("op", (4,)) == "v4"

    def test_scan_cache_access_moves_entry_to_end(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "integrations.backends.obsidian_backend.get_config",
            lambda: _fake_config(tmp_path, 3600),
        )
        backend = ObsidianBackend(vault_path=tmp_path)
        backend._scan_cache_max_entries = 3
        for i in range(3):
            backend._set_cached_scan("op", (i,), f"v{i}")
        # Access the oldest entry; it should not be evicted next.
        backend._get_cached_scan("op", (0,))
        backend._set_cached_scan("op", (3,), "v3")
        assert backend._get_cached_scan("op", (0,)) == "v0"
        assert backend._get_cached_scan("op", (1,)) is None

    def test_update_tags_invalidates_scan_cache(self, tmp_path, monkeypatch):
        """update_tags 修改标签后应失效当前 scope 的扫描缓存。"""
        monkeypatch.setattr(
            "integrations.backends.obsidian_backend.get_config",
            lambda: _fake_config(tmp_path, 3600),
        )
        backend = ObsidianBackend(vault_path=tmp_path)
        results = backend.save("content", ["session=s1", "source=test"], "note")
        assert results
        uid = results[0].uid

        backend._set_cached_scan("list_by_tags", (("status=raw",),), ["cached"])
        assert backend._get_cached_scan("list_by_tags", (("status=raw",),)) == ["cached"]

        backend.update_tags(uid, add_tags=["status=distilled"])

        assert backend._get_cached_scan("list_by_tags", (("status=raw",),)) is None
