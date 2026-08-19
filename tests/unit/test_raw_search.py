# -*- coding: utf-8 -*-
"""Unit tests for core.app.raw_search."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3

import pytest

import core.app.raw_search as raw_search_module
from core.app.raw_search import RawIndex
from core.ops.durable_io import DurableIOError
from core.sync_framework.raw_event_store import RawEventStore


@pytest.fixture
def raw_index(tmp_path):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    db_path = tmp_path / "raw_index.db"
    return RawIndex(raw_dir=raw_dir, db_path=db_path)


def _write_note(raw_dir: Path, rel_path: str, text: str):
    path = raw_dir / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_raw_index_late_schema_failure_restores_preimage_and_closes_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LateSchemaAbort(BaseException):
        pass

    database = tmp_path / "raw_index.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE preimage_sentinel (value TEXT PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO preimage_sentinel(value) VALUES ('unchanged')"
        )

    original_execute = RawIndex._execute_schema_statements
    original_ensure = RawIndex._ensure_schema
    observed_instances: list[RawIndex] = []

    def fail_after_complete_schema(
        connection: sqlite3.Connection,
        script: str,
    ) -> None:
        original_execute(connection, script)
        raise LateSchemaAbort("sentinel late raw index schema failure")

    def observed_ensure(index: RawIndex) -> None:
        observed_instances.append(index)
        original_ensure(index)

    monkeypatch.setattr(
        RawIndex,
        "_execute_schema_statements",
        staticmethod(fail_after_complete_schema),
    )
    monkeypatch.setattr(RawIndex, "_ensure_schema", observed_ensure)

    with pytest.raises(
        LateSchemaAbort,
        match="sentinel late raw index schema failure",
    ):
        RawIndex(
            raw_dir=tmp_path / "raw",
            db_path=database,
            raw_event_store=object(),
        )

    assert len(observed_instances) == 1
    assert observed_instances[0]._conn is None  # noqa: SLF001
    with sqlite3.connect(database) as connection:
        user_objects = connection.execute(
            """
            SELECT type, name
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
        sentinel_rows = connection.execute(
            "SELECT value FROM preimage_sentinel"
        ).fetchall()
    assert user_objects == [("table", "preimage_sentinel")]
    assert sentinel_rows == [("unchanged",)]


class TestRawIndex:
    def test_index_rejects_invalid_utf8_instead_of_certifying_partial_text(
        self,
        raw_index,
    ):
        note = raw_index.raw_dir / "invalid.md"
        note.write_bytes(
            b"---\nsession_id: invalid\nsource: codex\n---\nvisible\xffhidden"
        )

        assert raw_index.index_file(note) is False
        assert raw_index.get_by_path("invalid.md") is None

    def test_read_only_index_never_follows_a_leaf_database_symlink(
        self,
        tmp_path,
    ):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        target = tmp_path / "raw_index.real.db"
        with RawIndex(raw_dir=raw_dir, db_path=target):
            pass
        link = tmp_path / "raw_index.db"
        link.symlink_to(target)
        index = RawIndex(
            raw_dir=raw_dir,
            db_path=link,
            read_only=True,
        )
        try:
            with pytest.raises(
                DurableIOError,
                match="readonly_sqlite_path_not_regular",
            ):
                index._connect()  # noqa: SLF001
        finally:
            index.close()

    def test_index_rejects_symlink_escape_without_reading_external_content(
        self,
        raw_index,
        tmp_path,
    ):
        external = tmp_path / "external-private.md"
        external.write_text("must never enter Raw index", encoding="utf-8")
        link = raw_index.raw_dir / "escape.md"
        link.symlink_to(external)

        assert raw_index.index_file(link) is False
        assert raw_index.get_by_path("escape.md") is None

    def test_index_rejects_a_file_that_changes_during_descriptor_read(
        self,
        raw_index,
        monkeypatch,
    ):
        note = raw_index.raw_dir / "changing.md"
        note.write_bytes(b"a" * (1024 * 1024 + 64))
        original_read = raw_search_module.os.read
        mutated = False

        def mutate_after_first_chunk(descriptor, count):
            nonlocal mutated
            chunk = original_read(descriptor, count)
            if chunk and not mutated:
                mutated = True
                with note.open("ab") as handle:
                    handle.write(b"late mutation")
                    handle.flush()
                    os.fsync(handle.fileno())
            return chunk

        monkeypatch.setattr(raw_search_module.os, "read", mutate_after_first_chunk)

        assert raw_index.index_file(note) is False
        assert mutated is True
        assert raw_index.get_by_path("changing.md") is None

    def test_ensure_schema_creates_tables(self, raw_index):
        conn = raw_index._connect()
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "raw_index" in tables
        assert "raw_fts" in tables
        assert "raw_tags" in tables

    def test_sync_index_incremental(self, raw_index, tmp_path):
        _write_note(tmp_path / "raw", "2024/01/01/sess.md", "hello world")
        stats1 = raw_index.sync_index()
        assert stats1["indexed"] == 1
        assert stats1["skipped"] == 0

        stats2 = raw_index.sync_index()
        assert stats2["indexed"] == 0
        assert stats2["skipped"] == 1

    def test_sync_index_rechecks_content_when_mtime_is_unchanged(
        self,
        raw_index,
        tmp_path,
    ):
        note_path = tmp_path / "raw" / "same-mtime.md"
        note_path.write_text("alpha payload", encoding="utf-8")
        raw_index.sync_index()
        original_stat = note_path.stat()

        note_path.write_text("bravo payload", encoding="utf-8")
        os.utime(
            note_path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )

        stats = raw_index.sync_index()

        assert stats["indexed"] == 1
        assert stats["skipped"] == 0
        record = raw_index.get_by_path("same-mtime.md")
        assert record is not None
        assert record["content"] == "bravo payload"

    def test_sync_index_failure_restores_the_complete_previous_generation(
        self,
        raw_index,
        tmp_path,
        monkeypatch,
    ):
        raw_dir = tmp_path / "raw"
        _write_note(raw_dir, "a.md", "previous generation")
        raw_index.sync_index()
        _write_note(raw_dir, "a.md", "candidate generation")
        _write_note(raw_dir, "b.md", "must not partially commit")

        original_index_file = RawIndex._index_file

        def fail_second_file(index, file_path, cursor, *, stable_source=None):
            if file_path.name == "b.md":
                return False
            return original_index_file(
                index,
                file_path,
                cursor,
                stable_source=stable_source,
            )

        monkeypatch.setattr(RawIndex, "_index_file", fail_second_file)

        with pytest.raises(RuntimeError, match="raw_index_sync_incomplete:1"):
            raw_index.sync_index(force_full=True)

        connection = raw_index._connect()
        rows = connection.execute(
            "SELECT file_path, content FROM raw_index ORDER BY file_path"
        ).fetchall()
        assert rows == [("a.md", "previous generation")]
        assert connection.in_transaction is False

    def test_search_finds_keyword(self, raw_index, tmp_path):
        _write_note(
            tmp_path / "raw",
            "2024/01/01/sess.md",
            "---\nsession_id: s1\ndate: 2024-01-01\nsource: claude\ntime: 10:00\n---\nhello world target phrase",  # noqa: E501
        )
        raw_index.sync_index()
        results = raw_index.search("target")
        assert len(results) >= 1
        assert any("target phrase" in r.matched_line for r in results)

    def test_search_filter_by_session_id(self, raw_index, tmp_path):
        _write_note(
            tmp_path / "raw",
            "a.md",
            "---\nsession_id: alpha\ndate: 2024-01-01\nsource: claude\ntime: 10:00\n---\ncontent alpha",  # noqa: E501
        )
        _write_note(
            tmp_path / "raw",
            "b.md",
            "---\nsession_id: beta\ndate: 2024-01-01\nsource: claude\ntime: 10:00\n---\ncontent beta",  # noqa: E501
        )
        raw_index.sync_index()
        results = raw_index.search("content", session_id="alpha")
        assert len(results) == 1
        assert results[0].session_id == "alpha"

    def test_search_filter_by_source(self, raw_index, tmp_path):
        _write_note(
            tmp_path / "raw",
            "a.md",
            "---\nsession_id: s1\ndate: 2024-01-01\nsource: claude\ntime: 10:00\n---\nhello",
        )
        _write_note(
            tmp_path / "raw",
            "b.md",
            "---\nsession_id: s2\ndate: 2024-01-01\nsource: kimi\ntime: 10:00\n---\nhello",
        )
        raw_index.sync_index()
        results = raw_index.search("hello", source="kimi")
        assert len(results) == 1
        assert results[0].source == "kimi"

    def test_search_preserves_incompatible_acl_instead_of_promoting_it(
        self,
        raw_index,
        tmp_path,
    ):
        _write_note(
            tmp_path / "raw",
            "invalid-acl.md",
            "---\n"
            "session_id: s1\n"
            "source: claude\n"
            "scope: private\n"
            "acl_schema_version: 2\n"
            "acl_metadata_complete: false\n"
            "acl_reconciliation_status: future_schema\n"
            "---\nsecret sentinel",
        )
        raw_index.sync_index()

        result = raw_index.search("sentinel")[0]

        assert result.acl_schema_version == 2
        assert result.acl_metadata_complete is False
        assert result.acl_reconciliation_status == "future_schema"

    def test_search_records_metrics_only_after_authorization(self, tmp_path):
        raw_dir = tmp_path / "raw"
        raw_dir.mkdir()
        store = RawEventStore(db_path=tmp_path / "raw_events.db")
        try:
            event_id = store.upsert_turn(
                source_agent="claude",
                session_id="s1",
                turn_number=0,
                user_content="hello",
                assistant_content="target phrase",
            )
            index = RawIndex(
                raw_dir=raw_dir,
                db_path=tmp_path / "raw_index.db",
                raw_event_store=store,
            )
            _write_note(
                raw_dir,
                "a.md",
                "---\nsession_id: s1\ndate: 2024-01-01\nsource: claude\ntime: 10:00\nturn: 1\n---\nhello target phrase",
            )
            index.sync_index()

            results = index.search("target")
            assert len(results) == 1
            metrics = store.get_metrics(event_id)
            assert metrics["search_count"] == 0
            assert metrics["result_count"] == 0

            index.record_authorized_results("target", results)
            index.record_result_access(results[0], "view", consumer="obsidian")
            index.record_result_access(results[0], "hit", consumer="context")

            metrics = store.get_metrics(event_id)
            assert metrics["search_count"] == 1
            assert metrics["result_count"] == 1
            assert metrics["view_count"] == 1
            assert metrics["hit_count"] == 1
        finally:
            store.close()

    def test_health_check_reports_counts(self, raw_index, tmp_path):
        _write_note(tmp_path / "raw", "note.md", "text content")
        raw_index.sync_index()
        health = raw_index.health_check()
        assert health["status"] == "ok"
        assert health["indexed_files"] == 1
        assert health["fts_entries"] == 1

    def test_get_by_path(self, raw_index, tmp_path):
        _write_note(
            tmp_path / "raw",
            "note.md",
            "---\nsession_id: s1\ndate: 2024-01-01\nsource: claude\ntime: 10:00\n---\nbody",
        )
        raw_index.sync_index()
        record = raw_index.get_by_path("note.md")
        assert record is not None
        assert record["session_id"] == "s1"
        assert record["source"] == "claude"

    def test_sync_index_populates_raw_tags(self, raw_index, tmp_path):
        """sync_index 应将 frontmatter 中的 tags 写入 raw_tags 归一化表。"""
        _write_note(
            tmp_path / "raw",
            "tagged.md",
            "---\nsession_id: s1\ndate: 2024-01-01\nsource: claude\ntime: 10:00\ntags: [foo, bar]\n---\nbody",
        )
        raw_index.sync_index()
        conn = raw_index._connect()
        rows = conn.execute(
            "SELECT tag FROM raw_tags WHERE file_path = ? ORDER BY tag", ("tagged.md",)
        ).fetchall()
        assert [row[0] for row in rows] == ["bar", "foo"]

    def test_index_file_without_full_scan(self, raw_index, tmp_path):
        """index_file 应能增量索引单个文件，无需全目录扫描。"""
        note_path = tmp_path / "raw" / "single.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "---\nsession_id: s1\ndate: 2024-01-01\nsource: claude\ntime: 10:00\n---\nindexed single",
            encoding="utf-8",
        )
        assert raw_index.index_file(note_path) is True
        results = raw_index.search("indexed")
        assert len(results) == 1
        assert results[0].file_path == "single.md"

    def test_index_file_rolls_back_partial_index_when_any_consumer_fails(
        self,
        raw_index,
        tmp_path,
        monkeypatch,
    ):
        note_path = tmp_path / "raw" / "partial.md"
        note_path.write_text("partial", encoding="utf-8")

        def partial_failure(_file_path, cursor):
            cursor.execute(
                """
                INSERT INTO raw_index
                    (file_path, abs_path, content, frontmatter, tags, mtime)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("partial.md", str(note_path), "partial", "", "[]", 0.0),
            )
            return False

        monkeypatch.setattr(raw_index, "_index_file", partial_failure)

        assert raw_index.index_file(note_path) is False
        assert raw_index._connect().execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM raw_index WHERE file_path='partial.md'"
        ).fetchone()[0] == 0

    def test_index_file_updates_existing_record(self, raw_index, tmp_path):
        """对同一文件再次 index_file 应更新内容与标签。"""
        note_path = tmp_path / "raw" / "update.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "---\nsession_id: s1\ndate: 2024-01-01\nsource: claude\ntime: 10:00\ntags: [old]\n---\nfirst",
            encoding="utf-8",
        )
        raw_index.index_file(note_path)
        note_path.write_text(
            "---\nsession_id: s1\ndate: 2024-01-01\nsource: claude\ntime: 10:00\ntags: [new]\n---\nsecond",
            encoding="utf-8",
        )
        raw_index.index_file(note_path)

        results = raw_index.search("second")
        assert len(results) == 1
        tags = raw_index.list_by_tags(["new"])
        assert len(tags) == 1
        assert tags[0]["file_path"] == "update.md"
        old_tags = raw_index.list_by_tags(["old"])
        assert len(old_tags) == 0

    def test_remove_file(self, raw_index, tmp_path):
        """remove_file 应同时清理 raw_index、raw_fts 与 raw_tags。"""
        _write_note(
            tmp_path / "raw",
            "remove_me.md",
            "---\nsession_id: s1\ndate: 2024-01-01\nsource: claude\ntime: 10:00\ntags: [x]\n---\nbody",
        )
        raw_index.sync_index()
        assert raw_index.remove_file("remove_me.md") is True

        conn = raw_index._connect()
        assert conn.execute(
            "SELECT 1 FROM raw_index WHERE file_path = ?", ("remove_me.md",)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM raw_tags WHERE file_path = ?", ("remove_me.md",)
        ).fetchone() is None

    def test_list_by_tags_plain(self, raw_index, tmp_path):
        """list_by_tags 应支持纯标签查询。"""
        _write_note(
            tmp_path / "raw",
            "a.md",
            "---\nsession_id: s1\ndate: 2024-01-01\nsource: claude\ntime: 10:00\ntags: [foo]\n---\nbody",
        )
        _write_note(
            tmp_path / "raw",
            "b.md",
            "---\nsession_id: s2\ndate: 2024-01-01\nsource: kimi\ntime: 10:00\ntags: [foo, bar]\n---\nbody",
        )
        _write_note(
            tmp_path / "raw",
            "c.md",
            "---\nsession_id: s3\ndate: 2024-01-01\nsource: claude\ntime: 10:00\ntags: [bar]\n---\nbody",
        )
        raw_index.sync_index()
        rows = raw_index.list_by_tags(["foo", "bar"])
        assert len(rows) == 1
        assert rows[0]["file_path"] == "b.md"

    def test_list_by_tags_key_value(self, raw_index, tmp_path):
        """list_by_tags 应支持 key=value 形式标签。"""
        _write_note(
            tmp_path / "raw",
            "kv.md",
            "---\nsession_id: s1\ndate: 2024-01-01\nsource: claude\n"
            "time: 10:00\nproject: mnemos\ntags: [note]\n---\nbody",
        )
        raw_index.sync_index()
        rows = raw_index.list_by_tags(["project=mnemos"])
        assert len(rows) == 1
        assert rows[0]["file_path"] == "kv.md"

    def test_list_by_tags_case_insensitive(self, raw_index, tmp_path):
        """list_by_tags 应对标签大小写不敏感。"""
        _write_note(
            tmp_path / "raw",
            "case.md",
            "---\nsession_id: s1\ndate: 2024-01-01\nsource: claude\ntime: 10:00\ntags: [FooBar]\n---\nbody",
        )
        raw_index.sync_index()
        rows = raw_index.list_by_tags(["foobar"])
        assert len(rows) == 1


class TestRawIndexTagIntegration:
    def test_sync_index_removes_stale_tags(self, raw_index, tmp_path):
        """文件被删除后 sync_index 应清理其标签。"""
        note_path = tmp_path / "raw" / "stale.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "---\nsession_id: s1\ndate: 2024-01-01\nsource: claude\ntime: 10:00\ntags: [stale]\n---\nbody",
            encoding="utf-8",
        )
        raw_index.sync_index()
        note_path.unlink()
        raw_index.sync_index()
        rows = raw_index.list_by_tags(["stale"])
        assert len(rows) == 0


class TestRawIndexRetention:
    def test_sync_index_removes_stale_rows_by_indexed_at(self, raw_index, tmp_path):
        """A live source purged by retention must be rebuilt in the same generation."""
        note_path = tmp_path / "raw" / "old.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text("hello world", encoding="utf-8")
        raw_index.sync_index()

        # 把 indexed_at 改到 200 天前
        conn = raw_index._connect()
        cutoff = (__import__("time").time() - 200 * 86400)
        conn.execute("UPDATE raw_index SET indexed_at = ? WHERE file_path = ?", (cutoff, "old.md"))
        conn.commit()

        stats = raw_index.sync_index()
        assert stats["removed"] >= 1
        assert stats["indexed"] == 1

        record = raw_index.get_by_path("old.md")
        assert record is not None
        assert record["content"] == "hello world"

    def test_vacuum_recovers_space(self, raw_index, tmp_path):
        """VACUUM 应能正常执行且不抛异常"""
        note_path = tmp_path / "raw" / "note.md"
        note_path.write_text("hello world", encoding="utf-8")
        raw_index.sync_index()
        raw_index.remove_file("note.md")
        raw_index.vacuum()
        health = raw_index.health_check()
        assert health["indexed_files"] == 0
