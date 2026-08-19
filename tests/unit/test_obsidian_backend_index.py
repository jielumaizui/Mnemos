"""Tests for ObsidianBackend integration with RawIndex."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from integrations.backends.obsidian_backend import ObsidianBackend, _make_frontmatter


@pytest.fixture
def backend(tmp_path):
    """返回一个禁用自动注册 Obsidian vault 的 backend 实例。"""
    with patch.object(ObsidianBackend, "_ensure_vault_recognized", lambda self: None):
        yield ObsidianBackend(vault_path=tmp_path, daily_size_threshold=800 * 1024)


def _tags(session: str, turn: int = 1, source: str = "kimi") -> list[str]:
    return [
        f"source={source}",
        f"session={session}",
        f"turn={turn}",
        "time=20260101",
    ]


class TestObsidianBackendIndexIntegration:
    def test_raw_index_database_is_scoped_to_backend_vault(self, backend):
        """RawIndex 数据库应跟随 backend vault，避免多 vault 共享全局锁与相对路径冲突。"""
        idx = backend._get_raw_index_instance()

        assert idx is not None
        assert idx.db_path == backend.chatlog_dir / ".raw_index.db"

    def test_save_indexes_file_for_search(self, backend):
        """save() 后 search() 应能通过 RawIndex 找到内容，无需全文件扫描。"""
        now = datetime(2026, 1, 1, 14, 0)
        backend.save("unique indexed phrase here\n", _tags("idx-sess", 1), "title", now=now)

        results = backend.search("unique indexed phrase")
        assert len(results) >= 1
        assert any("unique indexed phrase" in r.content for r in results)

    def test_save_indexes_tags_for_list_by_tags(self, backend):
        """save() 后 list_by_tags() 应能通过 RawIndex 标签表查询。"""
        now = datetime(2026, 1, 1, 14, 0)
        backend.save("tagged content\n", _tags("tag-sess", 1, source="claude"), "title", now=now)

        results = backend.list_by_tags(["source=claude"])
        assert len(results) >= 1
        assert any("tagged content" in r.content for r in results)

    def test_update_tags_updates_index(self, backend):
        """update_tags() 后索引应反映新标签。"""
        now = datetime(2026, 1, 1, 14, 0)
        saved = backend.save("update tag content\n", _tags("upd-sess", 1), "title", now=now)
        uid = saved[0].uid

        backend.update_tags(uid, add_tags=["priority=p0"])
        results = backend.list_by_tags(["priority=p0"])
        assert len(results) == 1
        assert results[0].uid == uid

    def test_search_falls_back_when_index_unavailable(self, backend, tmp_path, monkeypatch):
        """RawIndex 不可用时 search() 应回退到文件扫描。"""
        monkeypatch.setattr(
            "integrations.backends.obsidian_backend._get_raw_index", lambda: None
        )
        now = datetime(2026, 1, 1, 14, 0)
        backend.save("fallback scan content\n", _tags("fb-sess", 1), "title", now=now)

        results = backend.search("fallback scan content")
        assert len(results) >= 1


class TestObsidianBackendIndexMaintenance:
    def test_rebuild_index_writes_meta_and_clears_stale(self, backend):
        """rebuild_index() 应生成索引元数据并让 is_index_stale() 返回 False。"""
        now = datetime(2026, 1, 1, 14, 0)
        backend.save("rebuild me\n", _tags("rebuild-sess", 1), "title", now=now)

        assert backend.is_index_stale() is True
        count = backend.rebuild_index()
        assert count >= 1
        assert backend.is_index_stale() is False
        assert backend._index_meta_path.exists()

    def test_auto_rebuild_when_stale(self, backend):
        """search() 检测到索引过期时应自动触发重建。"""
        now = datetime(2026, 1, 1, 14, 0)
        backend.save("stale check\n", _tags("stale-sess", 1), "title", now=now)
        # 手动破坏元数据，让 vault mtime 大于记录值
        backend._write_index_meta()
        meta = backend._read_index_meta()
        meta["vault_mtime"] = 0.0
        backend._index_meta_path.write_text(__import__("json").dumps(meta), encoding="utf-8")

        assert backend.is_index_stale() is True
        results = backend.search("stale check")
        assert len(results) >= 1
        assert backend.is_index_stale() is False

    def test_rebuild_index_propagates_programming_errors(self, backend, monkeypatch):
        idx = backend._get_raw_index_instance()
        monkeypatch.setattr(idx, "sync_index", lambda: (_ for _ in ()).throw(
            AssertionError("index contract bug")
        ))

        with pytest.raises(AssertionError, match="index contract bug"):
            backend.rebuild_index()


class TestMakeFrontmatter:
    """Snapshot-style tests for _make_frontmatter output format.

    Tag extraction depends on the exact YAML serialization (key order, list
    indentation, empty-list syntax, bool lowercase, string quoting), so these
    tests pin the current format during the D-level refactor.
    """

    def test_frontmatter_full_format(self):
        parsed = {
            "source": "kimi",
            "session": "sess-1",
            "turn": "3",
            "layer": "L1",
            "status": "active",
            "project": "mnemos, docs",
            "model": "kimi-k2",
            "has-code": "true",
            "_plain_tags": ["tag1", "tag2"],
        }
        output = _make_frontmatter(
            parsed, "kimi", "sess-1", 3, "deadbeef", "2026-01-15T14:30:45"
        )
        expected = (
            "---\n"
            'date: "2026-01-15"\n'
            'time: "14:30"\n'
            'session_id: "sess-1"\n'
            "turn: 3\n"
            'source: "kimi"\n'
            'content_hash: "deadbeef"\n'
            "tags:\n"
            "  - session=sess-1\n"
            "  - layer=L1\n"
            "  - status=active\n"
            "  - tag1\n"
            "  - tag2\n"
            "projects:\n"
            "  - mnemos\n"
            "  - docs\n"
            'layer: "L1"\n'
            'status: "active"\n'
            'project: "mnemos, docs"\n'
            'model: "kimi-k2"\n'
            'has-code: "true"\n'
            "_plain_tags:\n"
            "  - tag1\n"
            "  - tag2\n"
            "---\n"
        )
        assert output == expected

    def test_frontmatter_empty_tags(self):
        output = _make_frontmatter({}, "unknown", "", 0, "", "2026-01-15T14:30:45")
        expected = (
            "---\n"
            'date: "2026-01-15"\n'
            'time: "14:30"\n'
            'session_id: ""\n'
            "turn: 0\n"
            'source: "unknown"\n'
            'content_hash: ""\n'
            "tags: []\n"
            "---\n"
        )
        assert output == expected

    def test_frontmatter_bool_and_numbers(self):
        from integrations.backends.obsidian_backend import _serialize_frontmatter

        fm = {
            "date": "2026-01-15",
            "time": "14:30",
            "session_id": "s",
            "turn": 1,
            "source": "src",
            "content_hash": "hash",
            "tags": [],
            "visible": True,
            "count": 42,
            "score": 3.14,
        }
        output = _serialize_frontmatter(fm)
        assert "turn: 1\n" in output
        assert "visible: true\n" in output
        assert "count: 42\n" in output
        assert "score: 3.14\n" in output
