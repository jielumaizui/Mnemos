# -*- coding: utf-8 -*-
"""
P0-2 集成测试 — Memos → Wiki 追溯链路

验证：_link_session_memos_to_wiki 同时更新 memos_wiki_link 和 sync_log。
"""

import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def setup():
    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "sync_log.db"

    # 初始化 schema
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT,
                session_id TEXT,
                turn_number INTEGER,
                content_hash TEXT,
                memos_uids TEXT,
                status TEXT,
                synced_at TIMESTAMP,
                distill_status TEXT DEFAULT 'pending',
                distilled_at TIMESTAMP,
                wiki_page_paths TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memos_wiki_link (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memos_uid TEXT,
                wiki_page_path TEXT,
                link_type TEXT,
                created_at TIMESTAMP
            )
        """)
        # 预置一条 sync_log 记录
        conn.execute(
            "INSERT INTO sync_log (agent_name, session_id, turn_number, content_hash, memos_uids, status) VALUES (?, ?, ?, ?, ?, ?)",
            ("claude", "sess-trace-001", 0, "abc123", json.dumps(["uid-1", "uid-2"]), "new"),
        )
        conn.commit()

    with patch("core.config.get_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(data_dir=Path(tmpdir))
        # 必须重新导入，确保函数内部引用的是 patched get_config
        import importlib
        from core.hephaestus import wiki_builder
        importlib.reload(wiki_builder)
        yield {
            "db_path": db_path,
            "link_fn": wiki_builder._link_session_memos_to_wiki,
        }


def test_link_updates_both_tables(setup):
    """_link_session_memos_to_wiki 应同时更新 memos_wiki_link 和 sync_log"""
    db_path = setup["db_path"]
    link_fn = setup["link_fn"]

    memos = [
        {"uid": "uid-1", "tags": ["session=sess-trace-001", "layer=L1"]},
        {"uid": "uid-2", "tags": ["session=sess-trace-001", "layer=L1"]},
    ]
    wiki_paths = ["/wiki/00-Inbox/page1.md", "/wiki/00-Inbox/page2.md"]

    link_fn(memos, wiki_paths)

    with sqlite3.connect(str(db_path)) as conn:
        # 验证 memos_wiki_link
        rows = conn.execute(
            "SELECT memos_uid, wiki_page_path FROM memos_wiki_link ORDER BY memos_uid, wiki_page_path"
        ).fetchall()
        assert len(rows) == 4  # 2 uids × 2 paths

        # 验证 sync_log 更新
        row = conn.execute(
            "SELECT wiki_page_paths, distill_status, distilled_at FROM sync_log WHERE session_id=?",
            ("sess-trace-001",),
        ).fetchone()
        assert row is not None
        paths = json.loads(row[0])
        assert paths == wiki_paths
        assert row[1] == "distilled"
        assert row[2] is not None


def test_link_with_no_session_tag_still_writes_link_table(setup):
    """memos 中没有 session 标签时，memos_wiki_link 仍应写入，sync_log 不更新"""
    db_path = setup["db_path"]
    link_fn = setup["link_fn"]

    memos = [{"uid": "uid-3", "tags": ["layer=L1"]}]
    wiki_paths = ["/wiki/00-Inbox/page3.md"]

    link_fn(memos, wiki_paths)

    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM memos_wiki_link").fetchone()
        assert rows[0] == 1
        # sync_log 原有记录不应被无关 session 更新
        row = conn.execute(
            "SELECT wiki_page_paths FROM sync_log WHERE session_id=?", ("sess-trace-001",)
        ).fetchone()
        assert row[0] is None
