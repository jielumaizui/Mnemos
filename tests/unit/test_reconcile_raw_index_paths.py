"""Tests for narrow, backed-up Raw-index path reconciliation."""

from __future__ import annotations

import sqlite3

import pytest

from scripts.reconcile_raw_index_paths import (
    RawIndexPathReconcileError,
    apply_index_cleanup,
    inspect_index,
)


def _index_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE raw_index (
                id INTEGER PRIMARY KEY,
                file_path TEXT NOT NULL UNIQUE,
                abs_path TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE raw_fts USING fts5(content);
            CREATE TABLE raw_tags (file_path TEXT NOT NULL, tag TEXT NOT NULL);
            """
        )
        for row_id, rel, absolute in (
            (1, "codex/prod.md", "/production/raw/codex/prod.md"),
            (2, "codex/temp.md", "/tmp/rehearsal/raw/codex/temp.md"),
        ):
            conn.execute(
                "INSERT INTO raw_index (id, file_path, abs_path) VALUES (?, ?, ?)",
                (row_id, rel, absolute),
            )
            conn.execute("INSERT INTO raw_fts (rowid, content) VALUES (?, ?)", (row_id, rel))
            conn.execute("INSERT INTO raw_tags (file_path, tag) VALUES (?, ?)", (rel, "raw"))


def test_inspect_and_apply_remove_only_the_requested_foreign_root(tmp_path):
    db_path = tmp_path / "raw_index.db"
    _index_db(db_path)

    before = inspect_index(db_path, remove_abs_prefix="/tmp/rehearsal/raw")

    assert before["candidate_rows"] == 1
    assert before["candidate_fts_rows"] == 1
    assert before["ok"] is True

    result = apply_index_cleanup(
        db_path,
        remove_abs_prefix="/tmp/rehearsal/raw",
        vacuum=True,
    )

    assert result["applied_rows"] == 1
    assert result["raw_fts_rows_deleted"] == 1
    assert result["after"]["candidate_rows"] == 0
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT file_path FROM raw_index").fetchall() == [
            ("codex/prod.md",)
        ]
        assert conn.execute("SELECT COUNT(*) FROM raw_fts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM raw_tags").fetchone()[0] == 1


def test_reconciliation_rejects_an_empty_root(tmp_path):
    db_path = tmp_path / "raw_index.db"
    _index_db(db_path)

    with pytest.raises(RawIndexPathReconcileError, match="non-empty"):
        inspect_index(db_path, remove_abs_prefix="/")
