# -*- coding: utf-8 -*-
"""
P0-1 集成测试 — L1 Memos 端兜底去重

验证：删除本地 sync_log 后，第二次同步同一 turn 应通过 Memos 端查重
不重复写入，并正确记录 skipped_memos 状态。
"""

import json
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@dataclass
class _FakeMemory:
    uid: str
    content: str
    tags: list = field(default_factory=list)


class FakeMemosClient:
    """模拟 MemosClient：内存存储 + content_hash 标签支持"""

    def __init__(self):
        self._store = []  # list of _FakeMemory
        self._counter = 0

    def save(self, content, tags=None, visibility="PRIVATE", **kwargs):
        self._counter += 1
        uid = f"fake-uid-{self._counter}"
        mem = _FakeMemory(uid=uid, content=content, tags=list(tags or []))
        self._store.append(mem)
        return mem

    def save_long_content(self, content, tags=None, visibility="PRIVATE", title=None, **kwargs):
        return [self.save(content, tags, visibility, **kwargs)]

    def list_by_tags(self, tags, limit=None):
        """all-match 标签查询"""
        results = []
        for m in self._store:
            if all(t in m.tags for t in tags):
                results.append(m)
                if limit and len(results) >= limit:
                    break
        return results


@pytest.fixture
def setup():
    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "sync_log.db"

    # 初始化 sync_log schema
    from core.sync_framework.sync_engine import SyncEngine
    with patch("core.sync_framework.sync_engine.get_config") as mock_cfg:
        cfg = MagicMock()
        cfg.data_dir = Path(tmpdir)
        cfg.memos_token = "fake"
        cfg.memos_api_url = "http://fake"
        cfg.get.side_effect = lambda k, d=None: {
            "sync.shard_threshold_bytes": 8192,
        }.get(k, d)
        mock_cfg.return_value = cfg
        engine = SyncEngine(client=FakeMemosClient(), db_path=str(db_path))

    yield {"engine": engine, "db_path": db_path, "tmpdir": tmpdir}


class _TestAgentSource:
    """最小 AgentSource 实现，用于测试"""
    name = "claude"
    model_tag = "sonnet"

    def build_extra_tags(self, turn):
        return ["agent=claude"]


def _make_turn(user="hi", assistant="hello", turn_number=0):
    from core.sync_framework.sync_engine import Turn, SessionInfo
    return (
        _TestAgentSource(),
        SessionInfo(session_id="sess-dup-001", source_path=Path("/tmp/fake")),
        Turn(turn_number=turn_number, user_content=user, assistant_content=assistant),
    )


def test_memos_duplicate_fallback_after_sync_log_deleted(setup):
    """删除 sync_log 后，第二次同步应被 Memos 端查重拦截。"""
    engine = setup["engine"]
    db_path = setup["db_path"]

    source, session, turn = _make_turn()

    # 1. 第一次同步 → new
    r1 = engine.sync_single_turn(source, session, turn, incremental=False)
    assert r1.action == "new"
    assert len(r1.memos_uids) == 1
    first_uid = r1.memos_uids[0]

    # 验证 sync_log 写入正确
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT status, memos_uids FROM sync_log WHERE session_id=? AND turn_number=?",
            ("sess-dup-001", 0),
        ).fetchone()
    assert row is not None
    assert row[0] == "new"
    # memos_uids 必须是 JSON list
    uids = json.loads(row[1])
    assert isinstance(uids, list)
    assert uids == [first_uid]

    # 2. 删除本地 sync_log（模拟丢数据）
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM sync_log")
        conn.commit()

    # 3. 第二次同步相同内容 → 应被 Memos 端查重拦截
    r2 = engine.sync_single_turn(source, session, turn, incremental=False)
    assert r2.action == "skipped"
    # memos_uids 应复用第一次的 UID
    assert r2.memos_uids == [first_uid]

    # 4. sync_log 应记录为 skipped_memos
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT status, memos_uids FROM sync_log WHERE session_id=? AND turn_number=?",
            ("sess-dup-001", 0),
        ).fetchone()
    assert row is not None
    assert row[0] == "skipped_memos"
    uids2 = json.loads(row[1])
    assert uids2 == [first_uid]


def test_different_content_same_session_turn_not_duplicate(setup):
    """同一 session+turn 但内容不同，不应判重。"""
    engine = setup["engine"]

    source, session, turn1 = _make_turn(user="question A")
    r1 = engine.sync_single_turn(source, session, turn1, incremental=False)
    assert r1.action == "new"

    # 删除 sync_log，让第二次只能通过 Memos 端查重
    with sqlite3.connect(str(setup["db_path"])) as conn:
        conn.execute("DELETE FROM sync_log")
        conn.commit()

    # 相同 session+turn，不同内容
    source2, session2, turn2 = _make_turn(user="question B")
    r2 = engine.sync_single_turn(source2, session2, turn2, incremental=False)
    # 内容不同 → 不应判重（但 session+turn 相同，tags 匹配）
    # 由于 content_hash 标签不同，_check_memos_duplicate 不应匹配
    assert r2.action == "new"


def test_list_by_tags_all_match(setup):
    """list_by_tags 必须 all-match，不能只匹配任一标签。"""
    engine = setup["engine"]
    client = engine.client

    # 写入 3 条记录
    client.save("msg1", tags=["source=claude", "session=s1", "turn=1"])
    client.save("msg2", tags=["source=claude", "session=s1", "turn=2"])
    client.save("msg3", tags=["source=kimi", "session=s1", "turn=1"])

    # all-match 应只返回同时满足三个标签的
    results = client.list_by_tags(["source=claude", "session=s1", "turn=1"])
    assert len(results) == 1
    assert results[0].content == "msg1"


def test_sync_log_memos_uids_is_json_list(setup):
    """所有 sync_log 的 memos_uids 必须是合法 JSON list。"""
    engine = setup["engine"]
    db_path = setup["db_path"]

    source, session, turn = _make_turn()
    engine.sync_single_turn(source, session, turn, incremental=False)

    with sqlite3.connect(str(db_path)) as conn:
        # sqlite3 可能没有 json_valid，用 try/except 验证
        rows = conn.execute("SELECT memos_uids FROM sync_log").fetchall()
        for row in rows:
            val = json.loads(row[0])
            assert isinstance(val, list), f"memos_uids must be JSON list, got {type(val)}: {row[0]}"
