# -*- coding: utf-8 -*-
"""
P2-3 单元测试 — 历史截断数据标记
"""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from scripts.mark_truncated import TRUNCATION_RE, get_truncated_count


@pytest.mark.parametrize("content,expected", [
    ("[⚠️ 内容过长已截断：38237 字节 → 7459 字节]", True),
    ("chars truncated with `[... 文件内容已截断 ...]` notice", True),
    ("... (truncated, total 1234 chars)", True),
    ("[...内容过长，已截断...]", True),
    ("... (内容截断)", True),
    ("... (共 5000 字符，已截断)", True),
    ("...(truncated)", True),
    ("...(session truncated)", True),
    ("这是一条正常的知识记录，没有任何截断", False),
    ("[⚠️ 内容过长已截断：12345 字节 → 6789 字节]", True),
])
def test_truncation_regex(content, expected):
    """TRUNCATION_RE 应正确识别各种截断标记"""
    assert bool(TRUNCATION_RE.search(content)) is expected


def test_get_truncated_count_reads_db():
    """get_truncated_count 应从 mnemos.db 读取计数"""
    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "mnemos.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE truncated_memos (
                memos_uid TEXT PRIMARY KEY,
                marker_snippet TEXT,
                discovered_at TEXT
            )
        """)
        conn.executemany(
            "INSERT INTO truncated_memos VALUES (?, ?, ?)",
            [("uid-1", "snippet1", "2026-01-01"), ("uid-2", "snippet2", "2026-01-02")],
        )
        conn.commit()

    with patch("core.config.get_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(data_dir=Path(tmpdir))
        assert get_truncated_count() == 2


def test_get_truncated_count_returns_zero_when_no_table():
    """truncated_memos 表不存在时应返回 0"""
    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "mnemos.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE other_table (id INTEGER)")
        conn.commit()

    with patch("core.config.get_config") as mock_cfg:
        mock_cfg.return_value = MagicMock(data_dir=Path(tmpdir))
        assert get_truncated_count() == 0
